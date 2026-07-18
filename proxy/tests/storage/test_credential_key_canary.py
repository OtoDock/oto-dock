"""Boot-time credential-key canary (storage.credential_store.startup_key_canary).

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

from storage import credential_store
from storage.credential_store import startup_key_canary
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
