"""``GET /health`` is the public liveness (status, service, the dashboard
build) and nothing else; the version payload and the loop's telemetry moved
behind ``GET /v1/admin/health`` for a platform admin's cookie.

Run: cd proxy && venv/bin/pytest tests/api/test_health.py -v
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import app
from auth.providers import create_session_jwt
from auth.session_token import create_session_token

PUBLIC = {"status", "service", "build"}
ADMIN_ONLY = {"version", "claude_cli_version", "codex_cli_version", "cli_versions",
              "satellite_min_version", "loop", "log", "db", "fds"}


def _cookie_client(sub: str, email: str, name: str, role: str) -> TestClient:
    return TestClient(app, cookies={"session": create_session_jwt(sub, email, name, role)})


def test_health_is_the_public_liveness_only(temp_db):
    body = TestClient(app).get("/health").json()
    assert set(body) == PUBLIC and body["status"] == "ok" and body["service"] == "otodock"
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


def test_admin_health_refuses_a_member_and_a_session_token(temp_db):
    member = _cookie_client("user-viewer", "viewer@test.com", "Viewer User", "member")
    assert member.get("/v1/admin/health").status_code == 403
    assert TestClient(app).get("/v1/admin/health").status_code == 401
    # A session token of an admin-owned session is a bearer principal:
    # agent code inside the sandbox never reads the admin's telemetry.
    token = create_session_token("sid-health", "pa", user_sub="user-admin")
    r = TestClient(app).get("/v1/admin/health", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
