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


# ---------------------------------------------------------------------------
# The admin command's routes: the planned stop
# ---------------------------------------------------------------------------

def _from(peer: str, user=None) -> TestClient:
    app = FastAPI()
    app.include_router(internal_api.router)
    if user is None:
        user = UserContext(sub="api-key", email="api@internal", name="API Key",
                           role="admin", agents=[], is_api_key=True)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, client=(peer, 50000))


def test_work_answers_the_state_and_the_counts():
    from core import drain
    drain.register_counter("turns", lambda: 2)
    drain.register_waiting(lambda: 1)
    body = _from("127.0.0.1").get("/v1/internal/work").json()
    assert body["state"] == "running" and body["busy"] is True
    assert body["open"]["turns"] == 2 and body["waiting_on_people"] == 1
    assert set(body["open"]) == set(drain.KINDS)


def test_drain_holds_and_resume_takes_it_back():
    from core import drain
    client = _from("127.0.0.1")
    with patch.object(drain.background, "spawn", side_effect=lambda coro, **_: coro.close()):
        body = client.post("/v1/internal/drain",
                           json={"action": "drain", "bound_s": 30, "away_s": 120}).json()
    assert body["state"] == "draining" and body["bound_left_s"] <= 30
    assert drain.holding() and drain.away_s() == 120
    assert client.get("/v1/internal/work").json()["state"] == "draining"
    assert client.post("/v1/internal/drain", json={"action": "resume"}).json()["state"] == "running"
    assert not drain.holding()


def test_drain_takes_its_bound_from_the_config_when_none_is_named():
    import config
    from core import drain
    asked = {}

    async def fake(bound_s, away):
        asked.update(bound_s=bound_s, away=away)
        return drain.work()

    with patch.object(config, "DRAIN_BOUND_S", 45), patch.object(drain, "drain", fake):
        assert _from("::1").post("/v1/internal/drain", json={"action": "drain"}).status_code == 200
    assert asked == {"bound_s": 45, "away": drain.DEFAULT_AWAY_S}


def test_drain_refuses_what_is_not_an_action_or_a_number():
    client = _from("127.0.0.1")
    for body in ({}, {"action": "stop"}, {"action": "drain", "bound_s": -1},
                 {"action": "drain", "bound_s": "600"}, {"action": "drain", "away_s": True}):
        assert client.post("/v1/internal/drain", json=body).status_code == 400, body
    assert client.post("/v1/internal/drain", content=b"[1]",
                       headers={"content-type": "application/json"}).status_code == 400


def test_the_stop_routes_answer_only_the_servers_own_machine():
    import config
    from core import drain
    # Another host holds the master key too (the phone server).
    far = _from("192.168.1.20")
    assert far.get("/v1/internal/work").status_code == 403
    assert far.post("/v1/internal/drain", json={"action": "drain"}).status_code == 403
    # An edge on this machine is a loopback peer: what it forwards is refused.
    edge = _from("127.0.0.1")
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
        assert edge.get("/v1/internal/work", headers={header: "203.0.113.9"}).status_code == 403
    assert not drain.holding()
    # A server bound to one address is asked on it, by itself.
    with patch.object(config, "HOST", "192.168.1.20"):
        assert far.get("/v1/internal/work").status_code == 200
        assert _from("192.168.1.21").get("/v1/internal/work").status_code == 403
    with patch.object(config, "HOST", "0.0.0.0"):
        assert _from("0.0.0.0").get("/v1/internal/work").status_code == 403


def test_the_stop_routes_need_the_service_key():
    person = UserContext(sub="u1", email="u@x", name="U", role="admin", agents=[])
    client = _from("127.0.0.1", person)
    assert client.get("/v1/internal/work").status_code == 403
    assert client.post("/v1/internal/drain", json={"action": "resume"}).status_code == 403


def test_the_service_key_may_call_the_stop_routes_and_nothing_near_them():
    from auth.service_endpoints import is_service_endpoint_allowed
    assert is_service_endpoint_allowed("GET", "/v1/internal/work")
    assert is_service_endpoint_allowed("POST", "/v1/internal/drain")
    assert not is_service_endpoint_allowed("POST", "/v1/internal/work")
    assert not is_service_endpoint_allowed("GET", "/v1/internal/drain")
    assert not is_service_endpoint_allowed("POST", "/v1/internal/drain/now")


def test_the_master_key_reaches_the_stop_routes_through_the_whole_app(temp_db):
    import config
    from app import app
    key = {"Authorization": f"Bearer {config.API_KEY}"}
    near = TestClient(app, client=("127.0.0.1", 50000))
    assert near.get("/v1/internal/work", headers=key).json()["state"] == "running"
    assert near.get("/v1/internal/work").status_code in (401, 403)
    assert TestClient(app, client=("10.0.0.9", 50000)).get(
        "/v1/internal/work", headers=key).status_code == 403
