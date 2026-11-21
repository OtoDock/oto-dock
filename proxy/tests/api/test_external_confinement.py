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
from auth.path_policy import SecurityContext
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
    """With no registered context (nothing attached): the hook callbacks
    and the call tools alone. No memory route."""
    allowed = sorted(
        f"{m} {p}" for m, p in _routes() if is_external_endpoint_allowed(m, _concrete(p))
    )
    assert allowed == [
        "POST /v1/hooks/permission",
        "POST /v1/hooks/resolve-path",
        "POST /v1/hooks/resolve-tool-arg-paths",
        "POST /v1/hooks/stop",
        "POST /v1/hooks/subagent",
        "POST /v1/hooks/tool-result",
        "POST /v1/phone/call-control",
    ]


def _ctx(**facts) -> SecurityContext:
    return SecurityContext(role="external", username="", agent="support", is_admin_agent=False,
                           session_scope="agent", principal="external", external_claim="phone:+1", **facts)


def _manifest(name, *, callbacks=False, server_name=""):
    from pathlib import Path

    import config
    from services.mcp.mcp_registry import CredentialConfig, McpManifest, ServerConfig
    return McpManifest(
        name=name, label=name, description="", version="1.0.0", category="custom",
        server=ServerConfig(runtime="python", transport="stdio", proxy_callbacks=callbacks),
        credentials=CredentialConfig(), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], server_name=server_name,
        mcp_dir=Path(config.MCPS_DIR) / "custom" / name,
    )


def test_the_callbacks_and_the_gateway_follow_the_attached_tools(monkeypatch):
    """A session whose picks attached a server reaches that server's gateway
    path; another server's gateway path never. The file and media callbacks
    open for any attached tool (file-tools and the stdio image, music and
    video tools post them alike), never for the channel's own call tools
    alone."""
    from services.mcp import mcp_manifest_store
    from services.phone.call_control import CALL_CONTROL_MCP
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {
        "crm": _manifest("crm"), "files": _manifest("files", callbacks=True),
        CALL_CONTROL_MCP: _manifest(CALL_CONTROL_MCP, server_name="call-control"),
    })
    calls_alone = _ctx(outside_grants=(("call-control", None),))
    ctx = _ctx(outside_grants=(("crm", ("find",)), ("call-control", None)))
    with_files = _ctx(outside_grants=(("call-control", None), ("files", ("read_document",))))
    for mcp, expect in (("crm", True), ("call-control", True), ("other", False)):
        for verb in ("GET", "POST", "DELETE"):
            assert is_external_endpoint_allowed(verb, f"/v1/mcp-gateway/{mcp}/mcp", ctx) is expect, (mcp, verb)
    assert not is_external_endpoint_allowed("PUT", "/v1/mcp-gateway/crm/mcp", ctx)
    for cb in ("file", "file-written", "document-preview", "images", "image-generating", "image-gen-failed", "media"):
        assert is_external_endpoint_allowed("POST", f"/v1/hooks/{cb}", with_files), cb
        assert is_external_endpoint_allowed("POST", f"/v1/hooks/{cb}", ctx), cb
        assert not is_external_endpoint_allowed("POST", f"/v1/hooks/{cb}", calls_alone), cb
        assert not is_external_endpoint_allowed("POST", f"/v1/hooks/{cb}", _ctx())
        assert not is_external_endpoint_allowed("GET", f"/v1/hooks/{cb}", with_files)
    assert not is_external_endpoint_allowed("POST", "/v1/internal/memory/op", ctx)
    assert is_external_endpoint_allowed("POST", "/v1/hooks/permission", _ctx())


def test_the_middleware_reads_the_sessions_context(monkeypatch):
    """The registered context opens the picked server's gateway path to the
    session's token and no other, and the file callbacks for a picked tool
    that makes them."""
    from services.mcp import mcp_manifest_store
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {"crm": _manifest("crm", callbacks=True)})
    sid = str(uuid.uuid4())
    session_state.mark_starting(sid, 3600)
    session_state.register_session_state(sid, "auto", _ctx(outside_grants=(("crm", None),)))
    try:
        assert not _blocked(client.post("/v1/mcp-gateway/crm/mcp", headers=_headers(sid), content=b"{}"))
        assert _blocked(client.post("/v1/mcp-gateway/other/mcp", headers=_headers(sid), content=b"{}"))
        assert not _blocked(client.post("/v1/hooks/file", headers=_headers(sid), json={}))
        assert _blocked(client.post("/v1/internal/memory/op", headers=_headers(sid), json={}))
    finally:
        session_state.cleanup_session_permission_state(sid)


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
    assert not is_external_endpoint_allowed("POST", "/v1/hooks/../hooks/permission")
    assert not is_external_endpoint_allowed("GET", "/v1/hooks/permission")
    assert is_external_endpoint_allowed("POST", "/v1/hooks/permission?x=1")
