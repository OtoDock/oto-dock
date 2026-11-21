"""``GET /health`` is the public liveness (status, service, the dashboard
build, the state, whether the server takes work and one word per part) and
nothing else; ``GET /health/ready`` the readiness probe; the version payload
and the loop's telemetry sit behind ``GET /v1/admin/health`` for a platform
admin's cookie.

Run: cd proxy && venv/bin/pytest tests/api/test_health.py -v
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import create_session_jwt
from core import drain
from services.infra import health_parts
from services.mcp import tool_envs
from tests.conftest import live_session_token

PUBLIC = {"status", "service", "build", "state", "ready", "parts"}
PARTS = {"database", "tools", "scheduler", "compute"}
ADMIN_ONLY = {"version", "claude_cli_version", "codex_cli_version", "cli_versions",
              "satellite_min_version", "loop", "log", "db", "fds", "work"}


@pytest.fixture(autouse=True)
def _fresh_parts(monkeypatch):
    from services.scheduler import scheduler
    health_parts.reset_for_tests()
    tool_envs.reset_for_tests()
    drain.reset_for_tests()
    monkeypatch.setattr(scheduler.get_scheduler(), "state", 1)
    # A compute machine is signed in: without one nothing runs, which is its
    # own test below.
    from services.apps import app_compute
    monkeypatch.setattr(app_compute, "place", object)
    yield
    health_parts.reset_for_tests()
    tool_envs.reset_for_tests()
    drain.reset_for_tests()


def _cookie_client(sub: str, email: str, name: str, role: str) -> TestClient:
    return TestClient(app, cookies={"session": create_session_jwt(sub, email, name, role)})


def test_health_is_the_public_liveness_only(temp_db):
    body = TestClient(app).get("/health").json()
    assert set(body) == PUBLIC and body["status"] == "ok" and body["service"] == "otodock"
    assert set(body["parts"]) == PARTS and all(isinstance(v, str) for v in body["parts"].values())
    # A credential changes nothing: an admin's cookie gets the same answer,
    # and an anonymous request costs no database read.
    admin = _cookie_client("user-admin", "admin@test.com", "Admin User", "admin")
    assert set(admin.get("/health").json()) == PUBLIC


def test_admin_health_carries_the_telemetry_for_an_admin_cookie(temp_db):
    admin = _cookie_client("user-admin", "admin@test.com", "Admin User", "admin")
    r = admin.get("/v1/admin/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert ADMIN_ONLY <= set(body)
    assert "lane_queues" in body["db"]
    assert set(body["fds"]) == {"open", "limit"}
    assert "claude" in body["cli_versions"]
    assert body["work"]["state"] == "running" and "turns" in body["work"]["open"]
    assert set(body["parts"]) >= PARTS | {"phone", "tools_failed", "scheduler_mode"}


def test_admin_health_refuses_a_member_and_a_session_token(temp_db):
    member = _cookie_client("user-viewer", "viewer@test.com", "Viewer User", "member")
    assert member.get("/v1/admin/health").status_code == 403
    assert TestClient(app).get("/v1/admin/health").status_code == 401
    # A session token of an admin-owned session is a bearer principal:
    # agent code inside the sandbox never reads the admin's telemetry.
    token = live_session_token("sid-health", "pa", user_sub="user-admin")
    r = TestClient(app).get("/v1/admin/health", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_health_answers_head_like_get_with_no_body(temp_db):
    """An uptime probe's HEAD gets the GET's status and headers, no body."""
    client = TestClient(app)
    get = client.get("/health")
    head = client.head("/health")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.headers["content-length"] == get.headers["content-length"]
    # Only the safe pair: another method is still refused.
    assert client.post("/health").status_code == 405


def _public(client: TestClient) -> dict:
    return client.get("/health").json()


def test_the_state_while_the_start_holds_new_work(temp_db):
    """A boot that waits for its compute machine: alive (200), starting, not
    ready, and the readiness probe says 503 until the hold ends."""
    client = TestClient(app)
    drain.hold_for_start()
    body = _public(client)
    assert client.get("/health").status_code == 200
    assert (body["state"], body["ready"], body["parts"]["tools"]) == ("starting", False, "ok")
    assert client.get("/health/ready").status_code == 503
    asyncio.run(drain.end_start_hold())
    body = _public(client)
    assert (body["state"], body["ready"]) == ("ready", True)
    assert client.get("/health/ready").status_code == 200


def test_a_tool_whose_environment_is_not_built_reads_failed_and_never_preparing(temp_db):
    """A boot builds nothing, so nothing is being prepared: the tools the
    scan found stale read ``failed`` from the first answer on. The server
    takes work without them, and only an admin reads their names."""
    client = TestClient(app)
    tool_envs.note_stale({"b-mcp", "a-mcp"})
    body = _public(client)
    assert (body["state"], body["ready"], body["parts"]["tools"]) == ("degraded", True, "failed")
    assert client.get("/health/ready").status_code == 200
    assert "a-mcp" not in client.get("/health").text, "a stranger reads no tool's name"
    admin = _cookie_client("user-admin", "admin@test.com", "Admin User", "admin")
    parts = admin.get("/v1/admin/health").json()["parts"]
    assert (parts["tools"], parts["tools_failed"]) == ("failed", ["a-mcp", "b-mcp"])
    assert "tools_preparing" not in parts
    tool_envs.note_stale(set())
    assert _public(client)["parts"]["tools"] == "ok"


def test_a_database_that_does_not_answer_degrades_and_is_not_ready(temp_db, monkeypatch):
    from storage import pg

    def frozen():
        raise TimeoutError("no answer")
    monkeypatch.setattr(health_parts, "_ping", frozen)
    asyncio.run(health_parts.sample_once())
    client = TestClient(app)
    body = _public(client)
    assert (body["parts"]["database"], body["state"], body["ready"]) == ("down", "degraded", False)
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code == 503
    monkeypatch.setattr(health_parts, "_ping", lambda: None)
    asyncio.run(health_parts.sample_once())
    assert _public(client)["parts"]["database"] == "ok"
    # The loop pool's breaker open reads down at once, with no read.
    monkeypatch.setattr(pg, "pool_stats", lambda: {"loop": {"state": "open"}})
    assert _public(client)["parts"]["database"] == "down"


def test_a_drained_server_is_alive_and_not_ready(temp_db):
    async def scene():
        await drain.drain(60)
        client = TestClient(app)
        alive, ready = client.get("/health"), client.get("/health/ready")
        await drain.resume()
        return alive, ready
    alive, ready = asyncio.run(scene())
    assert alive.status_code == 200 and alive.json()["state"] == "ready"
    assert ready.status_code == 503 and ready.json()["ready"] is False


def test_with_no_compute_machine_the_server_is_alive_degraded_and_not_ready(temp_db, monkeypatch):
    """Nothing runs without a compute machine that shares the platform's
    folders: the liveness answer stays 200 (Docker, the installer and the
    updater wait on it), the part reads down, the state degraded, and the
    readiness probe says 503 until one is signed in."""
    from services.apps import app_compute
    monkeypatch.setattr(app_compute, "place", lambda: None)
    client = TestClient(app)
    alive = client.get("/health")
    body = alive.json()
    assert alive.status_code == 200
    assert (body["parts"]["compute"], body["state"], body["ready"]) == ("down", "degraded", False)
    ready = client.get("/health/ready")
    assert ready.status_code == 503 and ready.json()["ready"] is False
    assert health_parts.detail()["compute"] == "down"
    monkeypatch.setattr(app_compute, "place", object)
    body = _public(client)
    assert (body["parts"]["compute"], body["state"], body["ready"]) == ("ok", "ready", True)
    assert client.get("/health/ready").status_code == 200


def test_the_health_answer_reads_no_store(temp_db, monkeypatch):
    """Polled by Docker and by every page: memory only."""
    from storage import pg

    def no(*_a, **_k):
        raise AssertionError("the health answer read the database")
    monkeypatch.setattr(pg, "get_conn", no)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_a_frozen_database_holds_one_read_at_a_time(monkeypatch):
    """A read stuck on a frozen database keeps its lane thread: the next
    rounds wait for it instead of taking another thread each."""
    import threading
    release = threading.Event()
    started: list[int] = []

    def stuck():
        started.append(1)
        release.wait(5)
    monkeypatch.setattr(health_parts, "_ping", stuck)
    monkeypatch.setattr(health_parts, "DB_BOUND_S", 0.05)

    async def scene():
        await health_parts.sample_once()
        await health_parts.sample_once()
        await health_parts.sample_once()
        release.set()
        await asyncio.sleep(0.1)
    asyncio.run(scene())
    assert started == [1] and health_parts.parts()["database"] == "down"


def test_the_phone_part(monkeypatch):
    from services.phone import phone_config
    assert health_parts.parts()["phone"] == "off"
    monkeypatch.setattr(health_parts, "_phone_configured", True)
    monkeypatch.setattr(phone_config, "_management_clients", set())
    assert health_parts.parts()["phone"] == "down"
    assert health_parts.state() == "ready", "a phone service that is down degrades nothing"
    monkeypatch.setattr(phone_config, "_management_clients", {object()})
    assert health_parts.parts()["phone"] == "ok"
    assert "phone" not in health_parts.answer("b")["parts"], \
        "a stranger is not told whether telephony is set up"
    assert health_parts.detail()["phone"] == "ok"


def test_the_scheduler_part(monkeypatch):
    import config
    from services.scheduler import scheduler
    monkeypatch.setattr(scheduler.get_scheduler(), "state", 0)
    assert health_parts.parts()["scheduler"] == "down"
    monkeypatch.setattr(config, "SCHEDULER_MODE", "standalone")
    assert health_parts.parts()["scheduler"] == "ok", "standalone names no hosted install"


# ── /metrics: the numbers an operator watches, and who reads them ─────────

METRIC_NAMES = ("otodock_up 1", "otodock_ready ", "otodock_build_info{version=",
                'otodock_part_ok{part="database"}', 'otodock_open_work{kind="turns"}',
                "otodock_idle_seconds ", "otodock_sessions ", "otodock_queued_messages ",
                "otodock_loop_stalls_total ", "otodock_db_pool_size ")


def test_metrics_for_an_admin_signed_in(temp_db):
    from storage import database as task_store
    task_store.upsert_user("user-zed", "zed@example.com", "Zed Quartermaine", "member")
    admin = _cookie_client("user-admin", "admin@test.com", "Admin User", "admin")
    r = admin.get("/metrics")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    for name in METRIC_NAMES:
        assert name in r.text, name
    assert 'part="phone"' not in r.text, "a part that is off is left out"
    for person in ("Zed", "zed@", "user-zed", "admin@test.com", "Admin User"):
        assert person not in r.text, "no person's data in the counters"


def test_metrics_refuse_everyone_else(temp_db, monkeypatch):
    import config
    from services.infra import api_key_manager as akm
    assert TestClient(app).get("/metrics").status_code == 401
    member = _cookie_client("user-viewer", "viewer@test.com", "Viewer User", "member")
    assert member.get("/metrics").status_code == 403
    # An admin-owned session's token: agent code in the sandbox reads nothing.
    token = live_session_token("sid-metrics", "pa", user_sub="user-admin")
    r = TestClient(app).get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    # A person's API key, an admin's included: the route is on no key's list.
    _row, raw = akm.create_user_key(user_sub="user-admin", name="m", permissions=["chat"],
                                    agents=[], expires_in_days=7, run_without_asking=False)
    assert TestClient(app).get(
        "/metrics", headers={"Authorization": f"Bearer {raw}"}).status_code == 403
    # The master key from anywhere but the server's own machine.
    r = TestClient(app).get("/metrics", headers={"Authorization": f"Bearer {config.API_KEY}"})
    assert r.status_code == 403


def test_metrics_for_the_scrapers_token_and_the_master_key_on_this_machine(temp_db, monkeypatch):
    import config
    from api.internal import internal
    token = "m" * config.METRICS_TOKEN_MIN
    monkeypatch.setattr(config, "METRICS_TOKEN", token)
    ok = TestClient(app).get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and "otodock_up 1" in ok.text
    wrong = TestClient(app).get("/metrics", headers={"Authorization": f"Bearer {token}x"})
    assert wrong.status_code == 401
    monkeypatch.setattr(config, "METRICS_TOKEN", "")
    off = TestClient(app).get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert off.status_code == 401, "an empty key turns the bearer off"
    monkeypatch.setattr(internal, "_from_this_machine", lambda _r: True)
    local = TestClient(app).get("/metrics", headers={"Authorization": f"Bearer {config.API_KEY}"})
    assert local.status_code == 200


def test_the_busy_or_idle_counters_are_the_drains(temp_db, monkeypatch):
    monkeypatch.setattr(drain, "_counters", {**drain._counters, "turns": lambda: 2})
    admin = _cookie_client("user-admin", "admin@test.com", "Admin User", "admin")
    assert 'otodock_open_work{kind="turns"} 2' in admin.get("/metrics").text
    assert admin.get("/v1/admin/health").json()["work"]["open"]["turns"] == 2
