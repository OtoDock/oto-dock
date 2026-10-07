"""Boot-time credential-key canary (storage.identity.credential_store.startup_key_canary).

The Fernet key derives from JWT_SECRET; a recreated config.env orphans every
encrypted row. The canary samples each store at boot and logs one loud ERROR
naming the affected stores — these tests pin that behavior: silent when
everything decrypts, loud when a foreign-key ciphertext is found, and
crash-proof when tables are missing.
"""

import base64
import hashlib
import logging

import pytest

from storage.identity import credential_store
from storage.identity.credential_store import startup_key_canary
from storage.database import get_conn


def _foreign_ciphertext(value: str = "orphaned") -> str:
    """Encrypt under a DIFFERENT key than the store's active one."""
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(b"not-the-real-secret").digest())
    return Fernet(key).encrypt(value.encode()).decode()


@pytest.fixture
def _clean_infra():
    with get_conn() as conn:
        conn.execute("DELETE FROM infra_credentials WHERE mcp_name='canary-test'")
    yield
    with get_conn() as conn:
        conn.execute("DELETE FROM infra_credentials WHERE mcp_name='canary-test'")


class TestStartupKeyCanary:
    def test_silent_when_all_rows_decrypt(self, _clean_infra, caplog):
        credential_store.set_infra_credentials("canary-test", {"k": "v"})
        with caplog.at_level(logging.ERROR, logger="claude-proxy"):
            startup_key_canary()
        assert "CREDENTIAL KEY MISMATCH" not in caplog.text

    def test_loud_error_names_store_on_foreign_key(self, _clean_infra, caplog):
        # A row encrypted under a different JWT_SECRET — the folder-swap case.
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO infra_credentials "
                "(mcp_name, credential_key, credential_value_enc, "
                " created_at, updated_at) "
                "VALUES ('canary-test', 'k', %s, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                (_foreign_ciphertext(),),
            )
        with caplog.at_level(logging.ERROR):
            startup_key_canary()
        assert "CREDENTIAL KEY MISMATCH" in caplog.text
        assert "infrastructure credentials" in caplog.text
        assert "JWT_SECRET" in caplog.text

    def test_never_raises_without_tables(self, monkeypatch, caplog):
        # A DB error (e.g. mid-install, connection refused) must not take
        # the boot down — the canary is diagnosis only.
        def _boom():
            raise RuntimeError("db is gone")
        monkeypatch.setattr(credential_store, "get_conn", _boom)
        startup_key_canary()  # must not raise


# ---------------------------------------------------------------------------
# The credential key (F53): read through config.env, behind a MultiFernet
# ---------------------------------------------------------------------------

def _fernet_for(raw: str):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


@pytest.fixture
def _key_setting(monkeypatch):
    """Set the CREDENTIAL_ENCRYPTION_KEY line of config.env (never the
    process environment) and rebuild the store's key for each value."""
    import config
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(credential_store, "_fernet", None)

    def _set(value: str | None) -> None:
        if value is None:
            config._file_cfg.pop("CREDENTIAL_ENCRYPTION_KEY", None)
        else:
            config._file_cfg["CREDENTIAL_ENCRYPTION_KEY"] = value
        credential_store._fernet = None

    yield _set
    config._file_cfg.pop("CREDENTIAL_ENCRYPTION_KEY", None)
    credential_store._fernet = None


class TestCredentialKey:
    KEY = "k" * 40

    def test_the_config_file_line_takes_effect(self, _key_setting):
        _key_setting(self.KEY)
        token = credential_store.encrypt_secret("v")
        assert _fernet_for(self.KEY).decrypt(token.encode()) == b"v"

    def test_rows_written_before_the_key_still_open(self, _key_setting):
        import config
        _key_setting(None)
        old = credential_store.encrypt_secret("before")
        assert _fernet_for(config.JWT_SECRET).decrypt(old.encode()) == b"before"
        _key_setting(self.KEY)
        assert credential_store.decrypt_secret(old) == "before"

    def test_a_new_write_uses_the_configured_key_only(self, _key_setting):
        from cryptography.fernet import InvalidToken
        import config
        _key_setting(self.KEY)
        new = credential_store.encrypt_secret("after")
        with pytest.raises(InvalidToken):
            _fernet_for(config.JWT_SECRET).decrypt(new.encode())

    def test_extra_keys_only_open_older_rows(self, _key_setting):
        older = _fernet_for("p" * 40).encrypt(b"older").decode()
        _key_setting(f"{self.KEY}, {'p' * 40}")
        assert credential_store.decrypt_secret(older) == "older"
        written = credential_store.encrypt_secret("x")
        assert _fernet_for(self.KEY).decrypt(written.encode()) == b"x"

    def test_a_1_7_0_key_with_a_comma_still_opens_its_rows(self, _key_setting):
        """1.7.0 took the whole value as one key; read as a list now, the
        whole value still decrypts what it wrote, and the first entry
        encrypts."""
        whole = f"{self.KEY},tail"
        older = _fernet_for(whole).encrypt(b"older").decode()
        _key_setting(whole)
        assert credential_store.decrypt_secret(older) == "older"
        written = credential_store.encrypt_secret("x")
        assert _fernet_for(self.KEY).decrypt(written.encode()) == b"x"

    def test_the_canary_is_quiet_with_rows_under_both_keys(self, _key_setting, _clean_infra, caplog):
        _key_setting(None)
        credential_store.set_infra_credentials("canary-test", {"a": "1"})
        _key_setting(self.KEY)
        credential_store.set_infra_credentials("canary-test", {"b": "2"})
        with caplog.at_level(logging.ERROR):
            startup_key_canary()
        assert "CREDENTIAL KEY MISMATCH" not in caplog.text
        assert credential_store.get_infra_credentials("canary-test") == {"a": "1", "b": "2"}


# ---------------------------------------------------------------------------
# The secret floor (F53): warn on a value in use, refuse a new short one
# ---------------------------------------------------------------------------

@pytest.fixture
def _floor(monkeypatch, _key_setting):
    """A clean floor record, the JWT secret and the users answer under the
    test's control; the derived key follows the patched secret."""
    import config

    def _clear():
        with get_conn() as conn:
            conn.execute("DELETE FROM platform_settings WHERE key=%s",
                         (credential_store.FLOOR_SETTING,))

    _clear()
    state = {"users": True}
    monkeypatch.setattr(credential_store, "_install_has_users", lambda: state["users"])

    def _set(jwt: str, *, users: bool = True, key: str | None = None) -> None:
        monkeypatch.setattr(config, "JWT_SECRET", jwt)
        state["users"] = users
        _key_setting(key)

    yield _set
    _clear()


class TestSecretFloor:
    LONG = "L" * 64
    SHORT = "short-secret"

    def test_a_long_secret_passes_silently(self, _floor, caplog):
        _floor(self.LONG)
        with caplog.at_level(logging.WARNING):
            credential_store.judge_secret_floor()
        assert "JWT_SECRET" not in caplog.text

    def test_a_short_secret_in_use_at_the_upgrade_warns(self, _floor, caplog):
        _floor(self.SHORT, users=True)
        with caplog.at_level(logging.WARNING):
            credential_store.judge_secret_floor()
        assert "JWT_SECRET is 12 characters" in caplog.text
        # The next boot with the same value still only warns.
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            credential_store.judge_secret_floor()
        assert "JWT_SECRET is 12 characters" in caplog.text

    def test_a_short_secret_on_a_fresh_install_is_refused(self, _floor):
        _floor(self.SHORT, users=False)
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            credential_store.judge_secret_floor()

    def test_a_newly_set_short_secret_is_refused(self, _floor):
        _floor(self.LONG)
        credential_store.judge_secret_floor()
        _floor(self.SHORT)
        with pytest.raises(RuntimeError, match="at least 32 characters"):
            credential_store.judge_secret_floor()

    def test_restoring_a_short_secret_seen_before_only_warns(self, _floor, caplog):
        _floor(self.SHORT)
        credential_store.judge_secret_floor()       # grandfathered at the upgrade
        _floor(self.LONG)
        credential_store.judge_secret_floor()       # rotated
        _floor(self.SHORT)                          # restored, as the canary advises
        with caplog.at_level(logging.WARNING):
            credential_store.judge_secret_floor()
        assert "JWT_SECRET is 12 characters" in caplog.text

    def test_the_configured_key_is_held_to_the_floor(self, _floor):
        _floor(self.LONG)
        credential_store.judge_secret_floor()
        _floor(self.LONG, key="tiny-key")
        with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
            credential_store.judge_secret_floor()

    def test_an_extra_decrypt_only_key_is_not(self, _floor, caplog):
        _floor(self.LONG)
        credential_store.judge_secret_floor()
        _floor("n" * 40, key=f"{'w' * 40},{self.SHORT}")
        with caplog.at_level(logging.WARNING):
            credential_store.judge_secret_floor()
        assert "CREDENTIAL_ENCRYPTION_KEY" not in caplog.text

    def test_the_canary_runs_the_judge(self, _floor):
        _floor(self.SHORT, users=False)
        with pytest.raises(RuntimeError):
            startup_key_canary()

    def test_a_database_error_skips_the_judge(self, _floor, monkeypatch):
        _floor(self.SHORT, users=False)

        def _boom():
            raise RuntimeError("db is gone")
        monkeypatch.setattr(credential_store, "get_conn", _boom)
        credential_store.judge_secret_floor()  # must not raise
