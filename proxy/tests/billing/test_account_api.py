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
