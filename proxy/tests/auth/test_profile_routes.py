"""The self-service profile routes (display name, default agent) write the
users row off the event loop."""

import asyncio

from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from storage import database as db

client = TestClient(app)


def _off_loop(fn):
    """``fn``, refusing to run on a thread with a running event loop (a
    worker thread has none)."""
    def guarded(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return fn(*a, **kw)
        raise AssertionError(f"{fn.__name__} ran on the event loop")
    return guarded


def test_profile_and_default_agent_write_off_the_loop(monkeypatch):
    async def _me():
        return UserContext(sub="user-viewer", email="viewer@test.com", name="Viewer User",
                           role="member", agents=["support"])

    app.dependency_overrides[get_current_user] = _me
    monkeypatch.setattr(db, "update_user_display_name", _off_loop(db.update_user_display_name))
    monkeypatch.setattr(db, "set_user_default_agent", _off_loop(db.set_user_default_agent))
    try:
        resp = client.put("/v1/users/me/profile", json={"display_name": "  Vee  "})
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "Vee"
        resp = client.put("/v1/users/me/default-agent", json={"default_agent": "support"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["default_agent"] == "support"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert db.get_user("user-viewer")["display_name"] == "Vee"
    assert db.get_user_default_agent("user-viewer") == "support"
