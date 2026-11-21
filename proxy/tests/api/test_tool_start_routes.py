"""A tool stand-in's two routes (``api/hooks/tool_start.py``): taken on the
tool's fetch token alone, while its session still holds the tool's bundle
(the one check the secrets route makes, ``mcp_broker.live_token``); a saved
answer is served in its own scope only; ``start`` is the admission's word for
a late start; ``started`` keeps one answer per session and tool."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api.hooks import tool_start
from app import app
from auth.path_policy import SecurityContext
from auth.session_token import create_session_token
from core import concurrency
from core.credentials import mcp_broker
from core.session import session_state
from services.mcp import tool_listing

client = TestClient(app)
FP = "f" * 64
LISTING = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
           "lists": {"tools/list": [{"name": "echo"}]}}


@pytest.fixture
def session(monkeypatch):
    sid = str(uuid.uuid4())
    ctx = SecurityContext(role="editor", username="alice", agent="alpha", is_admin_agent=False)
    monkeypatch.setattr(session_state, "get_session_security", lambda s: ctx if s == sid else None)
    mcp_broker.provision(sid, {"echo-mcp": mcp_broker.SecretBundle()})
    tool_start._kept.clear()
    yield sid
    mcp_broker.purge_session(sid)
    tool_start._kept.clear()


def _auth(sid: str, mcp: str = "echo-mcp") -> dict:
    return {"Authorization": f"Bearer {mcp_broker.mint_token(sid, mcp)}"}


def test_the_fetch_token_lives_a_session():
    import time

    from auth import tokens
    now = time.time()
    fetch = tokens.decode(mcp_broker.mint_token("s", "echo-mcp"), tokens.MCP_CREDENTIALS)
    assert 24 * 3600 - 60 <= fetch["exp"] - now <= 24 * 3600 + 5


def test_live_token_answers_the_session_while_it_holds_the_tools_bundle(session):
    token = mcp_broker.mint_token(session, "echo-mcp")
    sid, mcp, bundle = mcp_broker.live_token(token)
    assert (sid, mcp) == (session, "echo-mcp") and isinstance(bundle, mcp_broker.SecretBundle)
    assert mcp_broker.live_token("not-a-token") == 401
    assert mcp_broker.live_token(mcp_broker.mint_token(session, "other-mcp")) == 404
    mcp_broker.purge_session(session)
    assert mcp_broker.live_token(token) == 404


def test_the_listing_route_serves_the_sessions_own_scope(session, monkeypatch):
    seen = []
    monkeypatch.setattr(tool_listing, "saved", lambda scope, server, fp: seen.append((scope, server, fp))
                        or (LISTING if scope == ("alpha", "alice") else None))
    r = client.get(f"/v1/hooks/tool-listing?fp={FP}", headers=_auth(session))
    assert r.status_code == 200 and r.json() == {"listing": LISTING}
    assert seen == [(("alpha", "alice"), "echo-mcp", FP)]
    monkeypatch.setattr(tool_listing, "saved", lambda *a: None)
    assert client.get(f"/v1/hooks/tool-listing?fp={FP}", headers=_auth(session)).status_code == 404


@pytest.mark.parametrize("bearer", ["session", "none", "closed"])
def test_the_routes_refuse_anything_but_a_live_fetch_token(session, bearer):
    headers = {
        "session": {"Authorization": f"Bearer {create_session_token(session, 'alpha', 'user-x')}"},
        "none": {},
        "closed": _auth(session),
    }[bearer]
    if bearer == "closed":
        mcp_broker.purge_session(session)
    for method, path, body in (("GET", f"/v1/hooks/tool-listing?fp={FP}", None),
                               ("POST", "/v1/hooks/tool-start", {"phase": "start"})):
        r = client.request(method, path, headers=headers, json=body)
        assert r.status_code in (401, 404), (bearer, path, r.status_code)


def test_start_is_the_admissions_word_for_a_late_start(session, monkeypatch):
    calls = []

    async def _grow(sid, *, judged, server=""):
        calls.append((sid, judged, server))
        return concurrency._DENY_TOOL_MEMORY if judged else concurrency._ADMITTED

    monkeypatch.setattr(concurrency, "grow", _grow)
    r = client.post("/v1/hooks/tool-start", headers=_auth(session), json={"phase": "start", "fp": FP})
    assert r.json() == {"ok": False, "message": concurrency._DENY_TOOL_MEMORY.user_message}
    r = client.post("/v1/hooks/tool-start", headers=_auth(session),
                    json={"phase": "start", "fp": FP, "with_session": True})
    assert r.json() == {"ok": True}
    assert calls == [(session, True, "echo-mcp"), (session, False, "echo-mcp")]
    given_back = []

    async def _shrink(sid, server=""):
        given_back.append((sid, server))

    monkeypatch.setattr(concurrency, "shrink", _shrink)
    r = client.post("/v1/hooks/tool-start", headers=_auth(session), json={"phase": "failed", "fp": FP})
    assert r.json() == {"ok": True} and given_back == [(session, "echo-mcp")]
    # A program stopped once its answers were kept gives its room back too.
    r = client.post("/v1/hooks/tool-start", headers=_auth(session), json={"phase": "stopped", "fp": FP})
    assert r.json() == {"ok": True} and given_back == [(session, "echo-mcp")] * 2


def test_started_keeps_one_answer_per_session_and_tool(session, monkeypatch):
    kept = []
    monkeypatch.setattr(tool_listing, "keep", lambda *a: kept.append(a) or True)
    body = {"phase": "started", "fp": FP, "ms": 812, "listing": LISTING}
    answers = [client.post("/v1/hooks/tool-start", headers=_auth(session), json=body).json()
               for _ in range(3)]
    # Kept once: the stand-in hears it then, and only then may stop its program.
    assert answers == [{"ok": True, "kept": True}] + [{"ok": True, "kept": False}] * 2
    assert kept == [(("alpha", "alice"), "echo-mcp", FP, LISTING)]
    assert client.post("/v1/hooks/tool-start", headers=_auth(session),
                       json={"phase": "nope"}).status_code == 422


def test_a_report_with_a_long_list_is_kept(session, monkeypatch):
    """Google Workspace's answers are 257 KB: past the unauthenticated
    tier, so the report is taken on its fetch token."""
    kept = []
    monkeypatch.setattr(tool_listing, "keep", lambda *a: kept.append(a) or True)
    tools = [{"name": f"tool_{i}", "description": "d" * 2000} for i in range(130)]
    listing = {"initialize": LISTING["initialize"], "lists": {"tools/list": tools}}
    r = client.post("/v1/hooks/tool-start", headers=_auth(session),
                    json={"phase": "started", "fp": FP, "with_session": True, "listing": listing})
    assert r.status_code == 200
    assert kept and kept[0][3] == listing


def test_listed_tells_the_terminal_the_engine_has_the_list(session):
    """A terminal's first message waits for its stand-ins' lists
    (``tool_listing.pending_listed``): ``listed`` and a start with the
    session both say the engine has it."""
    mcp_broker.provision(session, {"echo-mcp": mcp_broker.SecretBundle(),
                                   "other-mcp": mcp_broker.SecretBundle()})
    tool_listing.expect_listed(session, {"echo-mcp", "other-mcp"})
    try:
        assert client.post("/v1/hooks/tool-start", headers=_auth(session),
                           json={"phase": "listed"}).json() == {"ok": True}
        assert tool_listing.pending_listed(session) == frozenset({"other-mcp"})
        client.post("/v1/hooks/tool-start", headers=_auth(session, "other-mcp"),
                    json={"phase": "started", "fp": FP, "with_session": True})
        assert tool_listing.pending_listed(session) == frozenset()
    finally:
        tool_listing.drop_listed(session)


def test_a_program_that_did_not_start_holds_the_first_message_no_longer(session):
    """``failed``: the engine gets no list from that server, so a terminal's
    first message does not wait the whole bound for it."""
    mcp_broker.provision(session, {"echo-mcp": mcp_broker.SecretBundle()})
    tool_listing.expect_listed(session, {"echo-mcp"})
    try:
        assert client.post("/v1/hooks/tool-start", headers=_auth(session),
                           json={"phase": "failed"}).json() == {"ok": True}
        assert tool_listing.pending_listed(session) == frozenset()
    finally:
        tool_listing.drop_listed(session)


def test_an_abandoned_start_drops_what_its_first_message_waited_for():
    """A start that fails after it registered its stand-ins and before its
    terminal exists leaves nothing behind; a live session's release keeps
    its own."""
    from types import SimpleNamespace

    from core.config import config_builder
    cfg = SimpleNamespace(subscription_id="")
    tool_listing.expect_listed("s-gone", {"echo-mcp"})
    tool_listing.expect_listed("s-warm", {"echo-mcp"})
    try:
        config_builder.release_config_seat("s-gone", cfg)
        config_builder.release_config_seat("s-warm", cfg, keep_binding=True)
        assert tool_listing.pending_listed("s-gone") == frozenset()
        assert tool_listing.pending_listed("s-warm") == frozenset({"echo-mcp"})
    finally:
        tool_listing.drop_listed("s-warm")
