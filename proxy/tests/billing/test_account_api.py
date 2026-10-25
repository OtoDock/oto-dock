"""Install-facing account-connect routes (api/billing/account.py).

The relay toggle routes read the license, the connect token and platform
settings and reconcile the system instances: all of it runs in a worker
thread, never on the event loop.
"""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


def _off_loop(fn):
    """``fn`` raises when called with a running loop; a worker thread has none."""
    def guarded(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return fn(*a, **kw)
        raise AssertionError(f"{fn.__name__} ran on the event loop")
    return guarded


def _app() -> FastAPI:
    from api.billing import account

    user = UserContext(
        sub="user-admin", email="admin@test.com", name="Admin",
        role="admin", agents=[], agent_roles={},
    )

    async def _stub_user():
        return user

    app = FastAPI()
    app.include_router(account.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


def test_relay_enable_runs_its_store_work_off_the_loop(temp_db, monkeypatch):
    from services.billing import hosted_instances, relay_client
    from storage import database as db

    reconciled: list[bool] = []

    def relay_offered():
        return True

    def is_connected():
        return True

    def reconcile_otodock_system_instances():
        reconciled.append(True)

    monkeypatch.setattr(relay_client, "relay_offered", _off_loop(relay_offered))
    monkeypatch.setattr(relay_client, "is_connected", _off_loop(is_connected))
    monkeypatch.setattr(
        hosted_instances, "reconcile_otodock_system_instances",
        _off_loop(reconcile_otodock_system_instances),
    )
    monkeypatch.setattr(db, "set_platform_setting", _off_loop(db.set_platform_setting))

    resp = TestClient(_app()).post("/v1/account/relay/enable")
    assert resp.status_code == 200
    assert resp.json() == {"status": "enabled"}
    assert reconciled == [True]
    assert db.get_platform_setting("otodock_api_relay_enabled") == "1"


def test_the_account_routes_refuse_an_admins_session_token():
    """The account routes take the framework's require_admin: an agent
    session token in an admin-owned session is refused before any work."""
    from api.billing import account
    token_principal = UserContext(sub="user-admin", email="a@x", name="a", role="admin",
                                  agents=[], is_api_key=True, session_id="s1", agent="pa")

    async def _stub():
        return token_principal

    app = FastAPI()
    app.include_router(account.router)
    app.dependency_overrides[get_current_user] = _stub
    c = TestClient(app)
    for path in ("/v1/account/relay/enable", "/v1/account/relay/disable", "/v1/account/disconnect"):
        r = c.post(path, json={})
        assert r.status_code == 403, (path, r.text)


# ---------------------------------------------------------------------------
# What a person reads: sentences, never a bare code or an exception
# ---------------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
    import re
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]


def test_enabling_a_relay_that_is_not_connected_keeps_its_code_beside_a_sentence(temp_db, monkeypatch):
    from services.billing import relay_client
    monkeypatch.setattr(relay_client, "relay_offered", lambda: True)
    monkeypatch.setattr(relay_client, "is_connected", lambda: False)
    resp = TestClient(_app()).post("/v1/account/relay/enable")
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "not_connected",
        "message": "This install is not connected to an OtoDock account. Connect it first.",
    }


def test_an_offline_install_and_a_missing_address_say_what_to_do(temp_db, monkeypatch):
    import config
    from services.billing import relay_client
    monkeypatch.setattr(relay_client, "relay_offered", lambda: False)
    resp = TestClient(_app()).post("/v1/account/relay/disable")
    assert resp.json()["detail"] == "This install works offline, so the hosted relay is not available."

    from api.billing import account
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    try:
        account._callback_uri()
    except Exception as exc:  # noqa: BLE001  # the HTTPException is what this reads
        assert exc.detail == ("This install has no public address. Set DASHBOARD_PUBLIC_URL in "
                              "config.env (.env on a Docker install) and restart.")
    else:
        raise AssertionError("no refusal")


def test_the_connect_pages_say_what_happened_and_log_the_answer(temp_db, caplog):
    client = TestClient(_app())
    with caplog.at_level("WARNING", logger="otodock.account-api"):
        page = client.get("/v1/account/connect/callback", params={"error": "access_denied" + "x" * 300}).text
    assert "OtoDock did not finish the connection. Try again." in page
    assert "access_denied" not in page
    assert "access_denied" in caplog.text and "x" * 150 not in caplog.text
    page = client.get("/v1/account/connect/callback", params={"code": "c"}).text
    assert "This link is not complete. Start the connection again." in page
    page = client.get("/v1/account/connect/callback", params={"code": "c", "state": "s"}).text
    assert "This connection request expired. Start it again." in page


def test_every_relay_code_reads_as_short_sentences_and_an_unknown_one_is_logged(caplog):
    from services.billing import relay_client
    for code, message in relay_client.RELAY_ERROR_MESSAGES.items():
        assert relay_client.relay_error_message(code) == message
        assert not any(ch in message for ch in "—–;→"), code
        assert all(len(s.split()) <= 15 for s in _sentences(message)), code
    # The connect callback's sentences follow the same rules.
    for code, message in relay_client.CALLBACK_ERROR_MESSAGES.items():
        assert not any(ch in message for ch in "—–;→"), code
        assert all(len(s.split()) <= 15 for s in _sentences(message)), code
    # The hosted GitHub App's refusals: a moved install's events, an app
    # the relay stopped offering.
    assert {"events_host_changed", "provider_not_app"} <= set(relay_client.RELAY_ERROR_MESSAGES)
    with caplog.at_level("WARNING", logger="otodock.relay-client"):
        assert relay_client.relay_error_message("http_404") == (
            "OtoDock could not do this right now. Try again later.")
    assert "http_404" in caplog.text


def test_the_license_messages_are_plain(temp_db, monkeypatch):
    from types import SimpleNamespace

    from api.auth import platform
    from auth import license as L
    from auth.providers import UserContext
    from services.billing import relay_client

    admin = UserContext(sub="user-admin", email="admin@test.com", name="Admin",
                        role="admin", agents=[], agent_roles={})
    monkeypatch.setattr(platform, "get_license_key", lambda: "")
    monkeypatch.setattr(platform, "set_license_key", lambda key: None)
    monkeypatch.setattr(platform, "get_current_license",
                        lambda: SimpleNamespace(status="active", activation_state=""))
    monkeypatch.setattr(L, "validate_license_key",
                        lambda key: SimpleNamespace(lifetime=True, license_mode="lifetime"))
    monkeypatch.setattr(relay_client, "is_available", lambda: True)

    def answer(failure: Exception) -> str:
        async def activate(key):
            raise failure
        monkeypatch.setattr(relay_client, "activate_license", activate)
        out = asyncio.run(platform.set_license(platform.LicenseKeyRequest(license_key="k"), user=admin))
        return out["message"]

    assert answer(relay_client.RelayNotConfigured("x")) == (
        "Saved. The license activates once OtoDock can be reached.")
    assert answer(RuntimeError("boom")) == "Saved. Activation failed, and it will try again by itself."
    assert answer(relay_client.RelayError("activation_limit_reached")) == (
        "This license is already active on another install. "
        "Deactivate it there first, or use Move license.")
