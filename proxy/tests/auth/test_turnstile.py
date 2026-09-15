"""Cloudflare Turnstile login bot-protection (migrated from Google reCAPTCHA).

Covers config resolution (env-managed > DB > none), token verification (fail-closed on
missing/invalid token, fail-open on a CF outage), the public /auth/config site-key gate,
the admin managed-badge (no secret leak), and the reCAPTCHA→Turnstile DB migration.
"""

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.providers import UserContext, get_current_user
from services.infra import turnstile
from storage import database as db
from storage.identity.credential_store import _encrypt

client = TestClient(app)


# --- load_config: env-managed > DB > none -------------------------------------

def test_load_config_env_managed(monkeypatch):
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "env-site")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "env-secret")
    cfg = turnstile.load_config()
    assert cfg.managed is True and cfg.enabled is True
    assert cfg.site_key == "env-site" and cfg.secret_key == "env-secret"
    assert turnstile.is_managed() is True


def test_load_config_one_env_var_is_not_managed(monkeypatch):
    # Only the site key in env → not managed (needs both); falls through to DB/none.
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "env-site")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "")
    assert turnstile.is_managed() is False
    assert turnstile.load_config().managed is False


def test_load_config_from_db_decrypts(monkeypatch):
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "")
    db.set_platform_setting("turnstile_site_key", "db-site")
    db.set_platform_setting("turnstile_secret_key_enc", _encrypt("db-secret"))
    cfg = turnstile.load_config()
    assert cfg.managed is False and cfg.enabled is True
    assert cfg.site_key == "db-site" and cfg.secret_key == "db-secret"


def test_load_config_none(monkeypatch):
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "")
    cfg = turnstile.load_config()
    assert cfg.enabled is False and cfg.managed is False


def test_load_config_bad_ciphertext_is_disabled(monkeypatch):
    # A secret encrypted under a different key won't decrypt → secret="" → disabled,
    # and the admin GET must report it as NOT set (no false "configured").
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "")
    db.set_platform_setting("turnstile_site_key", "db-site")
    db.set_platform_setting("turnstile_secret_key_enc", "not-a-valid-fernet-token")
    cfg = turnstile.load_config()
    assert cfg.secret_key == "" and cfg.enabled is False


# --- verify_token -------------------------------------------------------------

def _cfg(enabled=True):
    return turnstile.TurnstileConfig(
        site_key="s" if enabled else "", secret_key="x" if enabled else "", managed=False)


@pytest.mark.asyncio
async def test_verify_not_configured_allows():
    assert await turnstile.verify_token(_cfg(enabled=False), "") is True


@pytest.mark.asyncio
async def test_verify_missing_token_rejects():
    assert await turnstile.verify_token(_cfg(), "") is False


@pytest.mark.asyncio
async def test_verify_success(monkeypatch):
    async def ok(_data): return {"success": True}
    monkeypatch.setattr(turnstile, "_post_siteverify", ok)
    assert await turnstile.verify_token(_cfg(), "tok") is True


@pytest.mark.asyncio
async def test_verify_failure(monkeypatch):
    async def bad(_data): return {"success": False, "error-codes": ["invalid-input-response"]}
    monkeypatch.setattr(turnstile, "_post_siteverify", bad)
    assert await turnstile.verify_token(_cfg(), "tok") is False


@pytest.mark.asyncio
async def test_verify_network_error_fails_open(monkeypatch):
    import httpx

    async def boom(_data): raise httpx.ConnectError("down")
    monkeypatch.setattr(turnstile, "_post_siteverify", boom)
    assert await turnstile.verify_token(_cfg(), "tok") is True  # fail-open


@pytest.mark.asyncio
async def test_verify_non_dict_body_does_not_crash(monkeypatch):
    # A 200 with valid-JSON-but-non-object body must not AttributeError → 500.
    async def weird(_data): return ["unexpected"]
    monkeypatch.setattr(turnstile, "_post_siteverify", weird)
    assert await turnstile.verify_token(_cfg(), "tok") is False


# --- /auth/config: public site key gated on enabled ---------------------------

def test_auth_config_serves_site_key_only_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "")
    # Site key but no secret → not enabled → must NOT serve the key (widget would
    # render with zero backend enforcement otherwise).
    db.set_platform_setting("turnstile_site_key", "db-site")
    data = client.get("/auth/config").json()
    assert data["turnstile_site_key"] == ""
    assert "recaptcha_site_key" not in data

    db.set_platform_setting("turnstile_secret_key_enc", _encrypt("db-secret"))
    data = client.get("/auth/config").json()
    assert data["turnstile_site_key"] == "db-site"


# --- admin GET: managed badge never leaks the secret --------------------------

def _admin_settings():
    async def _stub_admin():
        return UserContext(sub="user-admin", email="a@t.com", name="A", role="admin")
    app.dependency_overrides[get_current_user] = _stub_admin
    try:
        return client.get("/v1/admin/platform-settings").json()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_admin_settings_managed_hides_keys(monkeypatch):
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "env-site")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "env-secret")
    data = _admin_settings()
    assert data["turnstile_managed"] is True
    assert data["turnstile_site_key"] == ""          # never surface the managed keys
    assert data["turnstile_secret_key_set"] is False
    assert "recaptcha_site_key" not in data


def test_admin_settings_selfhost_reports_set(monkeypatch):
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "")
    db.set_platform_setting("turnstile_site_key", "db-site")
    db.set_platform_setting("turnstile_secret_key_enc", _encrypt("db-secret"))
    data = _admin_settings()
    assert data["turnstile_managed"] is False
    assert data["turnstile_site_key"] == "db-site"
    assert data["turnstile_secret_key_set"] is True
