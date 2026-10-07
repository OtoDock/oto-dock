"""External-session confinement (``middleware._session_refusal`` +
``auth/external_endpoints.py``).

Parametrised over the WHOLE router table: for an external principal (a
session token carrying ``ext`` and no user) every endpoint outside the
allowlist answers 403 before its handler runs; allowlisted endpoints reach
their handler. A token whose session is not live answers 401 everywhere,
``ext`` or not. A user-tied phone token (``ext`` + ``user_sub``) and a
plain session token are only liveness-checked.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import app
from auth.external_endpoints import (
    EXTERNAL_BLOCKED_DETAIL,
    SESSION_DEAD_DETAIL,
    is_external_endpoint_allowed,
)
from auth.session_token import create_session_token
from core.session import session_state

LIVE_SID = str(uuid.uuid4())
DEAD_SID = str(uuid.uuid4())

client = TestClient(app)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _walk(routes):
    """Every APIRoute, descending into included routers and mounts (FastAPI
    keeps ``include_router`` results as router objects on ``app.routes``)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif getattr(r, "original_router", None) is not None:   # FastAPI ≥ 0.13x
            yield from _walk(r.original_router.routes)
        elif hasattr(r, "routes"):
            yield from _walk(r.routes)


def _routes() -> list[tuple[str, str]]:
    out = []
    for r in _walk(app.routes):
        for m in sorted(r.methods or ()):
            if m in ("HEAD", "OPTIONS"):
                continue
            out.append((m, r.path))
    assert len(out) > 100, "route table walk found too few routes"
    return out


@pytest.fixture(autouse=True)
def _live_registry():
    session_state.mark_starting(LIVE_SID, 3600)


def _headers(sid: str, *, user_sub: str = "", external: str | None = "phone:+3021") -> dict:
    return {"Authorization": f"Bearer {create_session_token(sid, 'support', user_sub, external=external)}"}


def _blocked(resp) -> bool:
    if resp.status_code != 403:
        return False
    try:
        return resp.json().get("detail") == EXTERNAL_BLOCKED_DETAIL
    except ValueError:
        return False


@pytest.mark.parametrize("method, path", _routes(), ids=lambda v: v if isinstance(v, str) else "")
def test_router_table(method, path):
    """Every route: external principals see exactly the allowlist."""
    concrete = _concrete(path)
    resp = client.request(method, concrete, headers=_headers(LIVE_SID))
    if is_external_endpoint_allowed(method, concrete):
        assert not _blocked(resp), f"{method} {path} is allowlisted but blocked"
        assert resp.status_code != 401 or resp.json().get("detail") != SESSION_DEAD_DETAIL
    else:
        assert _blocked(resp), f"{method} {path} reachable by an external session: {resp.status_code}"


def test_allowlist_is_small_and_exact():
    allowed = sorted(
        f"{m} {p}" for m, p in _routes() if is_external_endpoint_allowed(m, _concrete(p))
    )
    assert allowed == [
        "DELETE /v1/mcp-gateway/{mcp}/{rest:path}",
        "GET /v1/mcp-gateway/{mcp}/{rest:path}",
        "POST /v1/hooks/document-preview",
        "POST /v1/hooks/file",
        "POST /v1/hooks/file-written",
        "POST /v1/hooks/image-gen-failed",
        "POST /v1/hooks/image-generating",
        "POST /v1/hooks/images",
        "POST /v1/hooks/media",
        "POST /v1/hooks/permission",
        "POST /v1/hooks/resolve-path",
        "POST /v1/hooks/resolve-tool-arg-paths",
        "POST /v1/hooks/stop",
        "POST /v1/hooks/subagent",
        "POST /v1/hooks/tool-result",
        "POST /v1/internal/memory/op",
        "POST /v1/mcp-gateway/{mcp}/{rest:path}",
    ]


def test_dead_session_token_is_rejected_everywhere():
    for method, path in (("POST", "/v1/internal/memory/op"), ("GET", "/v1/tasks")):
        resp = client.request(method, path, headers=_headers(DEAD_SID))
        assert resp.status_code == 401 and resp.json()["detail"] == SESSION_DEAD_DETAIL
    # ...including a user-tied phone token.
    resp = client.get("/v1/tasks", headers=_headers(DEAD_SID, user_sub="user-admin"))
    assert resp.status_code == 401 and resp.json()["detail"] == SESSION_DEAD_DETAIL


def test_user_tied_phone_token_is_not_confined():
    resp = client.get("/v1/tasks", headers=_headers(LIVE_SID, user_sub="user-admin"))
    assert not _blocked(resp)


def test_plain_session_token_is_only_liveness_checked():
    resp = client.get("/v1/tasks", headers=_headers(LIVE_SID, external=None))
    assert resp.status_code != 401 or resp.json().get("detail") != SESSION_DEAD_DETAIL
    assert not _blocked(resp)
    resp = client.get("/v1/tasks", headers=_headers(DEAD_SID, external=None))
    assert resp.status_code == 401 and resp.json()["detail"] == SESSION_DEAD_DETAIL


def test_traversal_never_matches_the_allowlist():
    assert not is_external_endpoint_allowed("POST", "/v1/hooks/../internal/memory/op")
    assert not is_external_endpoint_allowed("GET", "/v1/internal/memory/op")
    assert is_external_endpoint_allowed("POST", "/v1/internal/memory/op?x=1")
