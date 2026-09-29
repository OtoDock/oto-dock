"""``/v1/internal/*``: the standalone scheduler's channel into the proxy.

The fire-task route reads the task row and fires it; a paused or gone row
is refused so the channel follows the runner's own rule (a disabled row
never fires).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.internal import internal as internal_api
from auth.providers import UserContext, get_current_user
from storage import database as task_store


def _client(user=None):
    app = FastAPI()
    app.include_router(internal_api.router)
    if user is None:
        user = UserContext(sub="api-key", email="api@internal", name="API Key",
                           role="admin", agents=[], is_api_key=True)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _fire(row):
    fired = AsyncMock(return_value="run-1")
    with patch.object(task_store, "get_dynamic_task", return_value=row), \
         patch("services.scheduler.scheduler.trigger_task_now", fired), \
         patch("services.scheduler.scheduler._row_to_task", return_value=object()):
        resp = _client().post("/v1/internal/fire-task", json={"task_id": "t1"})
    return resp, fired


def test_fire_task_refuses_a_paused_row():
    resp, fired = _fire({"id": "t1", "enabled": False, "agent": "a"})
    assert resp.status_code == 409
    fired.assert_not_awaited()


def test_fire_task_fires_an_enabled_row_and_refuses_a_gone_one():
    resp, fired = _fire({"id": "t1", "enabled": True, "agent": "a"})
    assert resp.status_code == 200 and resp.json()["run_id"] == "run-1"
    fired.assert_awaited_once()
    resp, fired = _fire(None)
    assert resp.status_code == 404
    fired.assert_not_awaited()


def test_fire_task_needs_the_service_key():
    person = UserContext(sub="u1", email="u@x", name="U", role="admin", agents=[])
    resp = _client(person).post("/v1/internal/fire-task", json={"task_id": "t1"})
    assert resp.status_code == 403
