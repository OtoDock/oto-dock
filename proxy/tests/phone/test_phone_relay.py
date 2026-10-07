"""The /v1/phone/calls relay: auth, the phone-mcp assignment gate, forwarding.

phone-mcp reaches the phone daemon THROUGH these endpoints (session JWT over
PROXY_URL — loopback locally, the satellite tunnel remotely), so a session
machine never needs daemon reachability and PHONE_API_SECRET stays
proxy-side. A bare session JWT must not grant calling: the relay requires
phone-mcp among the calling agent's enabled MCPs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import config
from api.phone import phone_relay
from tests.conftest import live_session_token


class _FakeResponse:
    def __init__(self, status_code=200, content=b'{"ok": true}',
                 content_type="application/json"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class _FakeAsyncClient:
    """Captures the forwarded request; returns a canned daemon response."""

    calls: list[dict] = []
    response = _FakeResponse()

    def __init__(self, timeout=None):
        self._timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, params=None, json=None, headers=None):
        _FakeAsyncClient.calls.append({
            "method": method, "url": url, "params": params,
            "json": json, "headers": headers, "timeout": self._timeout,
        })
        return _FakeAsyncClient.response


@pytest.fixture
def fake_daemon(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse()
    monkeypatch.setattr(phone_relay.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://phone-daemon:9093")
    monkeypatch.setattr(config, "PHONE_API_SECRET", "tel-secret")
    return _FakeAsyncClient


def _assigned(monkeypatch, names):
    from services.mcp import mcp_registry
    monkeypatch.setattr(
        mcp_registry, "get_agent_mcps",
        lambda agent, **kw: [SimpleNamespace(name=n) for n in names],
    )


def test_relay_requires_auth():
    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._require_phone_agent(None))
    assert e.value.status_code == 401

    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._require_phone_agent("Bearer not-a-token"))
    assert e.value.status_code == 401


def test_relay_requires_phone_mcp_assignment(monkeypatch, temp_db):
    from storage import database as task_store
    task_store.upsert_user("user-1", "user-1@test.com", "User 1", "member")
    _assigned(monkeypatch, ["notifications-mcp"])
    token = live_session_token("sid-1", "some-agent", "user-1")
    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._require_phone_agent(f"Bearer {token}"))
    assert e.value.status_code == 403

    _assigned(monkeypatch, ["notifications-mcp", "phone-mcp"])
    caller = asyncio.run(phone_relay._require_phone_agent(f"Bearer {token}"))
    assert (caller.agent, caller.user_sub, caller.sid) == ("some-agent", "user-1", "sid-1")


def test_relay_refuses_the_master_key(monkeypatch):
    """The daemon never calls the relay and the service-key confinement
    already refuses the master key on this route: only a session token
    names a caller here."""
    monkeypatch.setattr(config, "is_master_key", lambda t: t == "master")
    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._require_phone_agent("Bearer master"))
    assert e.value.status_code == 401


def test_relay_forwards_with_daemon_secret(fake_daemon):
    resp = asyncio.run(phone_relay._relay(
        "POST", "/api/calls",
        json_body={"phone_number": "+301234", "task_description": "book"},
    ))
    assert resp.status_code == 200
    call = fake_daemon.calls[0]
    assert call["url"] == "http://phone-daemon:9093/api/calls"
    assert call["json"]["phone_number"] == "+301234"
    # PHONE_API_SECRET attached proxy-side — never travels with the MCP
    assert call["headers"]["Authorization"] == "Bearer tel-secret"


def test_relay_wait_extends_read_timeout(fake_daemon):
    asyncio.run(phone_relay._relay(
        "GET", "/api/calls/c1/wait", params={"timeout": "120"},
        read_timeout=150.0,
    ))
    assert fake_daemon.calls[0]["timeout"].read == 150.0


def test_relay_daemon_unreachable_maps_to_502(monkeypatch):
    import httpx as _httpx

    class _Boom(_FakeAsyncClient):
        async def request(self, *a, **kw):
            raise _httpx.ConnectError("getaddrinfo failed")

    monkeypatch.setattr(phone_relay.httpx, "AsyncClient", _Boom)
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://phone-daemon:9093")
    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._relay("GET", "/api/calls/c1"))
    assert e.value.status_code == 502
    assert "phone-daemon:9093" in e.value.detail


def test_relay_passes_daemon_status_through(fake_daemon):
    fake_daemon.response = _FakeResponse(status_code=404, content=b'{"error": "no call"}')
    resp = asyncio.run(phone_relay._relay("GET", "/api/calls/nope"))
    assert resp.status_code == 404
    assert b"no call" in resp.body


# ---------------------------------------------------------------------------
# Origination policy: the tier, the pinned route, the rebuilt body, the caps,
# the destination allowlist, and the call id bound to its principal
# ---------------------------------------------------------------------------

AGENT = "agent-x"


def _user(sub, role):
    from storage import database as task_store
    task_store.upsert_user(sub, f"{sub}@test.com", sub, "member")
    task_store.add_user_agent(sub, AGENT, role, "test")
    return sub


def _route(route_id, **over):
    from storage.phone import phone_route_store, phone_server_store
    servers = phone_server_store.get_all_servers()
    server = servers[0] if servers else phone_server_store.create_server(
        {"name": "pbx", "adapter_type": "asterisk_manual"})
    data = {"id": route_id, "direction": "outbound", "agent": AGENT, "name": route_id,
            "enabled": True, "identity_mode": "caller", "phone_server_id": server["id"]}
    data.update(over)
    return phone_route_store.create_route(data)


@pytest.fixture
def relay_app(monkeypatch, fake_daemon, temp_db):
    """The relay router on a bare app: agent-x has phone-mcp, its instance
    pins route r-x, and the daemon answers 202 with a call id."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from storage.mcp import mcp_store
    _assigned(monkeypatch, ["phone-mcp"])
    monkeypatch.setattr(
        mcp_store, "get_instance_for_agent_env_delivery",
        lambda mcp, agent: (
            {"field_values": {"OUTBOUND_ROUTE_ID": "r-x"}} if agent == AGENT else None),
    )
    fake_daemon.response = _FakeResponse(
        status_code=202, content=b'{"call_id": "c-1", "status": "dialing"}')
    phone_relay._call_windows.clear()
    phone_relay._call_owners.clear()
    _route("r-x")
    app = FastAPI()
    app.include_router(phone_relay.router)
    return TestClient(app)


def _post(client, sub, body=None, sid="sid-1"):
    token = live_session_token(sid, AGENT, sub)
    return client.post(
        "/v1/phone/calls",
        json=body or {"phone_number": "+302101234567", "task_description": "book a table"},
        headers={"Authorization": f"Bearer {token}"},
    )


def _with_live_context(sid, role, username=""):
    from auth.path_policy import SecurityContext
    from core.session import session_state
    session_state.set_session_security(sid, SecurityContext(
        role=role, username=username, agent=AGENT, is_admin_agent=False))


def _drop_live_context(sid):
    from core.session import session_state
    session_state._session_security.pop(sid, None)


@pytest.mark.parametrize("role", ["viewer", "contributor"])
def test_below_editor_cannot_originate(relay_app, fake_daemon, role):
    r = _post(relay_app, _user(f"pm-{role}", role))
    assert r.status_code == 403, r.text
    assert fake_daemon.calls == []


def test_editor_originates_with_the_rebuilt_body(relay_app, fake_daemon):
    r = _post(relay_app, _user("ed", "editor"), body={
        "phone_number": "+302101234567", "task_description": "book",
        "instructions": "polite", "wait": True, "route_id": "r-x",
        "caller_id": "anything", "extra": 1,
    })
    assert r.status_code == 202, r.text
    assert fake_daemon.calls[0]["json"] == {
        "phone_number": "+302101234567", "task_description": "book",
        "instructions": "polite", "route_id": "r-x", "wait": False,
    }
    assert fake_daemon.calls[0]["headers"]["Authorization"] == "Bearer tel-secret"


def test_a_route_other_than_the_pinned_one_is_refused(relay_app, fake_daemon):
    sub = _user("ed2", "editor")
    _route("r-y")
    r = _post(relay_app, sub, {"phone_number": "+30210", "task_description": "t", "route_id": "r-y"})
    assert r.status_code == 403
    assert fake_daemon.calls == []


def test_no_pinned_route_is_refused(relay_app, fake_daemon, monkeypatch):
    from storage.mcp import mcp_store
    monkeypatch.setattr(mcp_store, "get_instance_for_agent_env_delivery",
                        lambda *a: {"field_values": {}})
    assert _post(relay_app, _user("ed3", "editor")).status_code == 403
    monkeypatch.setattr(mcp_store, "get_instance_for_agent_env_delivery", lambda *a: None)
    assert _post(relay_app, _user("ed3", "editor")).status_code == 403
    assert fake_daemon.calls == []


@pytest.mark.parametrize("over", [
    {"enabled": False}, {"direction": "inbound"}, {"agent": "agent-y"},
])
def test_a_pinned_route_that_is_not_this_agents_enabled_outbound_route_is_refused(
        relay_app, fake_daemon, over):
    from storage.phone import phone_route_store
    phone_route_store.update_route("r-x", over)
    assert _post(relay_app, _user("ed4", "editor")).status_code == 403
    assert fake_daemon.calls == []


def test_a_missing_pinned_route_is_refused(relay_app, fake_daemon):
    from storage.phone import phone_route_store
    phone_route_store.delete_route("r-x")
    assert _post(relay_app, _user("ed4b", "editor")).status_code == 403
    assert fake_daemon.calls == []


def test_user_mode_route_requires_the_tied_user(relay_app, fake_daemon):
    from storage.phone import phone_route_store
    tied = _user("tied", "editor")
    other = _user("other", "editor")
    phone_route_store.update_route(
        "r-x", {"identity_mode": "user", "identity_user_sub": tied})
    assert _post(relay_app, other).status_code == 403
    assert fake_daemon.calls == []
    assert _post(relay_app, tied).status_code == 202
    # An agent-scope session names no person: never the tied user.
    _with_live_context("sid-task", "manager")
    try:
        assert _post(relay_app, "", sid="sid-task").status_code == 403
    finally:
        _drop_live_context("sid-task")
    assert len(fake_daemon.calls) == 1


def test_an_agent_scope_session_originates_on_its_live_context(relay_app, fake_daemon):
    _with_live_context("sid-task", "manager")
    try:
        assert _post(relay_app, "", sid="sid-task").status_code == 202
    finally:
        _drop_live_context("sid-task")
    # No live context and no person: nothing to judge the caller by.
    assert _post(relay_app, "", sid="sid-nolive").status_code == 403
    assert len(fake_daemon.calls) == 1


def test_the_live_context_is_the_acting_role(relay_app, fake_daemon):
    """A registered context outranks the row: a below-editor creator's task
    carries the creator's role even when the same person's row says more,
    and an editor's live context passes without a row lookup."""
    sub = _user("mgr", "manager")
    _with_live_context("sid-c", "contributor", username="mgr")
    try:
        assert _post(relay_app, sub, sid="sid-c").status_code == 403
    finally:
        _drop_live_context("sid-c")
    assert fake_daemon.calls == []


def test_external_and_stale_tokens_are_refused(relay_app, fake_daemon):
    sub = _user("ed5", "editor")
    ext = live_session_token("sid-e", AGENT, sub, external="phone:x")
    r = relay_app.post("/v1/phone/calls",
                       json={"phone_number": "+30210", "task_description": "t"},
                       headers={"Authorization": f"Bearer {ext}"})
    assert r.status_code == 403
    ghost = live_session_token("sid-g", AGENT, "nobody-here")
    r = relay_app.post("/v1/phone/calls",
                       json={"phone_number": "+30210", "task_description": "t"},
                       headers={"Authorization": f"Bearer {ghost}"})
    assert r.status_code == 401
    assert fake_daemon.calls == []


def test_the_hourly_agent_cap_refuses_the_next_call(relay_app, fake_daemon, monkeypatch):
    monkeypatch.setattr(config, "PHONE_CALLS_PER_AGENT_PER_HOUR", 2)
    monkeypatch.setattr(config, "PHONE_CALLS_PER_USER_PER_HOUR", 5)
    monkeypatch.setattr(config, "PHONE_CALLS_PER_AGENT_PER_DAY", 50)
    sub = _user("ed6", "editor")
    assert _post(relay_app, sub).status_code == 202
    assert _post(relay_app, sub).status_code == 202
    r = _post(relay_app, sub)
    assert r.status_code == 429 and "PHONE_CALLS_PER_AGENT_PER_HOUR" in r.text
    assert int(r.headers["retry-after"]) > 0
    assert len(fake_daemon.calls) == 2
    # The window slides: an hour later the agent may call again.
    now = phone_relay._now()
    monkeypatch.setattr(phone_relay, "_now", lambda: now + 3601)
    assert _post(relay_app, sub).status_code == 202


def test_the_user_and_daily_caps(relay_app, fake_daemon, monkeypatch):
    monkeypatch.setattr(config, "PHONE_CALLS_PER_AGENT_PER_HOUR", 100)
    monkeypatch.setattr(config, "PHONE_CALLS_PER_USER_PER_HOUR", 1)
    monkeypatch.setattr(config, "PHONE_CALLS_PER_AGENT_PER_DAY", 3)
    a, b, c, d = (_user(n, "editor") for n in ("ua", "ub", "uc", "ud"))
    assert _post(relay_app, a).status_code == 202
    r = _post(relay_app, a)
    assert r.status_code == 429 and "PHONE_CALLS_PER_USER_PER_HOUR" in r.text
    assert _post(relay_app, b).status_code == 202
    assert _post(relay_app, c).status_code == 202
    r = _post(relay_app, d)
    assert r.status_code == 429 and "PHONE_CALLS_PER_AGENT_PER_DAY" in r.text
    assert len(fake_daemon.calls) == 3
    # 0 = no cap.
    monkeypatch.setattr(config, "PHONE_CALLS_PER_AGENT_PER_DAY", 0)
    assert _post(relay_app, d).status_code == 202


def test_a_daemon_failure_still_counts_against_the_caps(relay_app, fake_daemon, monkeypatch):
    monkeypatch.setattr(config, "PHONE_CALLS_PER_AGENT_PER_HOUR", 1)
    fake_daemon.response = _FakeResponse(status_code=502, content=b'{"error": "originate"}')
    sub = _user("ed6b", "editor")
    assert _post(relay_app, sub).status_code == 502
    assert _post(relay_app, sub).status_code == 429


def test_the_destination_allowlist(relay_app, fake_daemon, monkeypatch):
    monkeypatch.setattr(config, "PHONE_CALL_ALLOWED_PREFIXES", ["+30", "+44"])
    sub = _user("ed7", "editor")
    r = _post(relay_app, sub, {"phone_number": "+491234567", "task_description": "t"})
    assert r.status_code == 403
    assert fake_daemon.calls == []
    r = _post(relay_app, sub, {"phone_number": "+302101234", "task_description": "t"})
    assert r.status_code == 202
    monkeypatch.setattr(config, "PHONE_CALL_ALLOWED_PREFIXES", [])
    r = _post(relay_app, sub, {"phone_number": "+491234567", "task_description": "t"})
    assert r.status_code == 202


@pytest.mark.parametrize("body", [
    {"task_description": "t"},
    {"phone_number": "abc", "task_description": "t"},
    {"phone_number": "+30210"},
    {"phone_number": "+30210", "task_description": ""},
    {"phone_number": "+30210", "task_description": "x" * 4001},
    {"phone_number": "+30210", "task_description": "t", "instructions": 5},
    {"phone_number": "+30210", "task_description": "t", "instructions": "y" * 8001},
    ["not", "an", "object"],
])
def test_an_invalid_body_is_400_and_never_forwarded(relay_app, fake_daemon, body):
    assert _post(relay_app, _user("ed8", "editor"), body).status_code == 400
    assert fake_daemon.calls == []


def test_call_ids_are_bound_to_the_placing_principal(relay_app, fake_daemon):
    owner, stranger = _user("owner", "editor"), _user("stranger", "editor")
    assert _post(relay_app, owner, sid="sid-a1").status_code == 202  # the daemon says c-1

    def _get(sub, sid, suffix=""):
        tok = live_session_token(sid, AGENT, sub)
        return relay_app.get(f"/v1/phone/calls/c-1{suffix}",
                             headers={"Authorization": f"Bearer {tok}"})

    def _answer(sub, sid):
        tok = live_session_token(sid, AGENT, sub)
        return relay_app.post("/v1/phone/calls/c-1/answer", json={"answer": "yes"},
                              headers={"Authorization": f"Bearer {tok}"})

    assert _get(stranger, "sid-b").status_code == 404
    assert _get(stranger, "sid-b", "/wait?timeout=1").status_code == 404
    assert _answer(stranger, "sid-b").status_code == 404
    # The same person from a re-warmed session keeps the call.
    assert _get(owner, "sid-a2").status_code == 202
    assert _get(owner, "sid-a2", "/wait?timeout=1").status_code == 202
    assert _answer(owner, "sid-a2").status_code == 202
    # An unknown call id, and an agent-scope session against a person's call.
    tok = live_session_token("sid-a2", AGENT, owner)
    assert relay_app.get("/v1/phone/calls/c-nope",
                         headers={"Authorization": f"Bearer {tok}"}).status_code == 404
    _with_live_context("sid-task", "manager")
    try:
        assert _get("", "sid-task").status_code == 404
    finally:
        _drop_live_context("sid-task")
    # Only the origination and the three reads the owner made reached the daemon.
    assert len(fake_daemon.calls) == 4


def test_agent_scope_calls_belong_to_the_agent(relay_app, fake_daemon):
    _with_live_context("sid-t1", "manager")
    _with_live_context("sid-t2", "manager")
    try:
        assert _post(relay_app, "", sid="sid-t1").status_code == 202
        tok = live_session_token("sid-t2", AGENT, "")
        assert relay_app.get("/v1/phone/calls/c-1",
                             headers={"Authorization": f"Bearer {tok}"}).status_code == 202
    finally:
        _drop_live_context("sid-t1")
        _drop_live_context("sid-t2")
    person = _user("p1", "editor")
    tok = live_session_token("sid-p", AGENT, person)
    assert relay_app.get("/v1/phone/calls/c-1",
                         headers={"Authorization": f"Bearer {tok}"}).status_code == 404
