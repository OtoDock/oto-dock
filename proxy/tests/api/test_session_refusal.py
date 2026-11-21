"""The session refusal (``middleware._session_refusal``): a bearer session
token is accepted only while its session is live, and a token minted for an
earlier life of the same session id is refused.

Every session token presented as the bearer on either listener is judged
before any database read: a sid in no registry, not starting, not closing
and not in the headless set answers 401 everywhere; a token whose ``iat`` is
older than the session's floor (the config mint of the session's current
life) answers 401 while the session is held. The master key and cookies are
untouched.

Run: cd proxy && venv/bin/pytest tests/api/test_session_refusal.py -v
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.external_endpoints import SESSION_DEAD_DETAIL
from auth.session_token import create_session_token
from core.session import session_state

client = TestClient(app)

# Routing-only: resolves the caller's own session by its token sid, so any
# live session token is a 200 and the refusal is the only 401 in the way.
ROUTE = "/v1/session/current"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sid() -> str:
    return str(uuid.uuid4())


class TestDeadSession:
    def test_a_token_for_a_session_nothing_holds_is_refused(self):
        r = client.get(ROUTE, headers=_bearer(create_session_token(_sid(), "alpha", "user-admin")))
        assert r.status_code == 401
        assert r.json()["detail"] == SESSION_DEAD_DETAIL

    def test_a_no_user_token_is_refused_the_same_way(self):
        r = client.get(ROUTE, headers=_bearer(create_session_token(_sid(), "alpha")))
        assert r.status_code == 401
        assert r.json()["detail"] == SESSION_DEAD_DETAIL

    def test_a_hook_route_refuses_it_before_its_own_auth(self):
        sid = _sid()
        r = client.post("/v1/hooks/stop", json={"session_id": sid},
                        headers=_bearer(create_session_token(sid, "alpha")))
        assert r.status_code == 401
        assert r.json()["detail"] == SESSION_DEAD_DETAIL


class TestLiveSession:
    def test_a_starting_session_passes(self):
        sid = _sid()
        session_state.mark_starting(sid, 60)
        r = client.get(ROUTE, headers=_bearer(create_session_token(sid, "alpha", "user-admin")))
        assert r.status_code == 200

    def test_a_closing_session_passes_inside_its_window(self):
        sid = _sid()
        session_state.mark_closing(sid)
        r = client.get(ROUTE, headers=_bearer(create_session_token(sid, "alpha", "user-admin")))
        assert r.status_code == 200

    def test_a_headless_exec_session_passes(self):
        sid = f"appx-{uuid.uuid4().hex[:12]}"
        session_state.mark_headless_live(sid)
        r = client.get(ROUTE, headers=_bearer(create_session_token(sid, "alpha", "user-admin")))
        assert r.status_code == 200

    def test_the_master_key_and_a_cookie_are_untouched(self):
        import config
        # A service route the master key may reach: its answer is the
        # route's own, never the session refusal.
        r = client.delete(f"/v1/sessions/{_sid()}", headers=_bearer(config.API_KEY))
        assert r.status_code != 401
        from tests.fixtures.ws_dashboard_harness import session_cookie
        r = client.get("/auth/me", headers={"Cookie": f"session={session_cookie('user-admin')}"})
        assert r.status_code == 200


class TestStaleToken:
    def test_a_token_minted_before_the_floor_is_refused_while_held(self):
        from core.layers.direct.session import _direct_sessions
        sid = _sid()
        now = int(time.time())
        old = create_session_token(sid, "alpha", issued_at=now - 200)
        session_state.mark_starting(sid, 60)
        session_state.register_session_state(sid, "default", None, token_minted_at=now - 100)
        _direct_sessions[sid] = object()
        try:
            r = client.get(ROUTE, headers=_bearer(old))
            assert r.status_code == 401
            assert r.json()["detail"] == SESSION_DEAD_DETAIL
            fresh = create_session_token(sid, "alpha", issued_at=now - 100)
            assert client.get(ROUTE, headers=_bearer(fresh)).status_code == 200
        finally:
            _direct_sessions.pop(sid, None)

    def test_the_comparison_is_skipped_while_only_starting(self):
        sid = _sid()
        now = int(time.time())
        old = create_session_token(sid, "alpha", issued_at=now - 200)
        session_state.mark_starting(sid, 60)
        session_state.register_session_state(sid, "default", None, token_minted_at=now - 100)
        assert client.get(ROUTE, headers=_bearer(old)).status_code == 200


class TestEverySite:
    """The sites no middleware stands in front of (the app proxy's own
    check, which the app socket's auth frames reach; the phone relay's; the
    principal resolver on a WebSocket scope) refuse a token of a session
    nothing holds and one minted before its session's floor."""

    @staticmethod
    def _dead_and_stale():
        from core.layers.direct.session import _direct_sessions
        now = int(time.time())
        dead = create_session_token(_sid(), "alpha", "user-admin")
        held = _sid()
        session_state.mark_starting(held, 60)
        session_state.register_session_state(held, "default", None, token_minted_at=now - 100)
        _direct_sessions[held] = object()
        stale = create_session_token(held, "alpha", "user-admin", issued_at=now - 200)
        return {"dead": dead, "stale": stale}, lambda: _direct_sessions.pop(held, None)

    @pytest.mark.asyncio
    async def test_the_app_proxy_refuses_before_any_read(self, monkeypatch):
        from fastapi import HTTPException

        from api.apps import app_proxy

        async def _no_read(*a, **k):
            raise AssertionError("the refusal comes before the holder read")

        monkeypatch.setattr(app_proxy, "run_db", _no_read)
        tokens, done = self._dead_and_stale()
        try:
            for kind, token in tokens.items():
                with pytest.raises(HTTPException) as e:
                    await app_proxy.caller_from_token(token, {"id": str(uuid.uuid4()), "agent": "alpha"})
                assert e.value.status_code == 401, kind
                assert e.value.detail == "the session token is no longer valid", kind
        finally:
            done()

    @pytest.mark.asyncio
    async def test_the_phone_relay_refuses_before_any_read(self, monkeypatch):
        from fastapi import HTTPException

        from api.phone import phone_relay

        async def _no_read(*a, **k):
            raise AssertionError("the refusal comes before the holder read")

        monkeypatch.setattr(phone_relay, "run_db_fast", _no_read)
        tokens, done = self._dead_and_stale()
        try:
            for kind, token in tokens.items():
                with pytest.raises(HTTPException) as e:
                    await phone_relay._require_phone_agent(f"Bearer {token}")
                assert e.value.status_code == 401, kind
        finally:
            done()

    @pytest.mark.asyncio
    async def test_the_principal_on_a_websocket_scope_names_nobody(self):
        from starlette.requests import HTTPConnection

        from auth.providers import _resolve_principal
        tokens, done = self._dead_and_stale()
        try:
            for kind, token in tokens.items():
                conn = HTTPConnection({
                    "type": "websocket", "path": "/v1/apps/x/ws", "query_string": b"",
                    "headers": [(b"authorization", f"Bearer {token}".encode())],
                })
                assert await _resolve_principal(conn) is None, kind
        finally:
            done()


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    session_state.reset_liveness_for_tests()


# --- where a session's token is accepted --------------------------------------

# The test client arrives on port 80: an internal listener bound there puts
# it inside, one bound elsewhere puts it on the public side.
_INSIDE, _ELSEWHERE = 80, 45123
_CALLBACKS = ("resolve-path", "file", "file-written", "document-preview", "images")


@pytest.fixture
def public_side(monkeypatch):
    import config
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _ELSEWHERE)


@pytest.fixture
def inside(monkeypatch):
    import config
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INSIDE)


def _live(user_sub: str = "user-admin") -> tuple[str, dict]:
    from tests.conftest import live_session_token
    sid = _sid()
    return sid, _bearer(live_session_token(sid, "alpha", user_sub))


def _callback(sid: str, agent: str = "alpha", user_sub: str = "user-admin") -> dict:
    """The bearer a Docker tool calls back with for ``sid``."""
    from auth.session_token import create_callback_token
    return _bearer(create_callback_token(sid, agent, user_sub))


def _refused_outside(r) -> bool:
    from middleware import SESSION_OUTSIDE_DETAIL
    return r.status_code == 401 and r.json().get("detail") == SESSION_OUTSIDE_DETAIL


def _off_list(r) -> bool:
    from auth.external_endpoints import SESSION_ROUTE_DETAIL
    if r.status_code != 403:
        return False
    try:
        return r.json().get("detail") == SESSION_ROUTE_DETAIL
    except ValueError:
        return False


class TestListener:
    """A session's token works in full on the internal listener, where
    sandboxes and machine tunnels arrive. On the public one it reaches the
    Docker tools' file callbacks and nothing else."""

    @pytest.mark.parametrize("user_sub", ["user-admin", ""])
    def test_from_the_public_side_a_live_token_is_refused(self, public_side, user_sub):
        _sid_, headers = _live(user_sub)
        assert _refused_outside(client.get(ROUTE, headers=headers))
        # The probe that found it: an hourly task made from outside.
        r = client.post("/v1/tasks/scheduled", headers=headers, json={
            "name": "n", "agent": "alpha", "prompt": "p", "schedule": "0 * * * *",
            "scope": "user", "notification_mode": "none"})
        assert _refused_outside(r)
        for path in ("/v1/hooks/stop", "/v1/hooks/permission", "/v1/hooks/tool-result",
                     "/v1/delegation/delegate", "/v1/agents/alpha/files"):
            assert _refused_outside(client.post(path, headers=headers, json={})), path

    @pytest.mark.parametrize("user_sub", ["user-admin", ""])
    def test_inside_the_same_token_works(self, inside, user_sub):
        _sid_, headers = _live(user_sub)
        assert client.get(ROUTE, headers=headers).status_code == 200

    def test_with_no_internal_listener_the_rule_is_off_and_the_log_says_so_once(
            self, monkeypatch, caplog):
        """A bare ``uvicorn app:app`` binds no internal listener: the rule
        is off, as the credential gateway's is, and nobody would know.
        One warning per process says it."""
        import logging

        import config
        import middleware
        monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 0)
        monkeypatch.setattr(middleware, "_listener_off_warned", False)
        _sid_, headers = _live()
        with caplog.at_level(logging.WARNING, logger="otodock"):
            assert client.get(ROUTE, headers=headers).status_code == 200
            assert client.get(ROUTE, headers=headers).status_code == 200
        said = [r for r in caplog.records if "No internal listener is bound" in r.getMessage()]
        assert len(said) == 1 and said[0].levelno == logging.WARNING

    @pytest.mark.parametrize("name", _CALLBACKS)
    def test_a_docker_tools_callback_token_passes_on_either_side(self, public_side, monkeypatch, name):
        """The Docker tools call back with a token of their own kind: a POST
        to one of the five callbacks answers (the route's own checks, its own
        body errors), on the public side where the container arrives and
        inside; another method or another route is refused with its own
        sentence, and a session token on the public side reaches nothing."""
        import config
        from auth.external_endpoints import CALLBACK_OUTSIDE_DETAIL
        sid, headers = _live()
        callback = _callback(sid)
        for listener in (_ELSEWHERE, _INSIDE):
            monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", listener)
            r = client.post(f"/v1/hooks/{name}", headers=callback, json={"session_id": sid})
            assert not _refused_outside(r) and r.status_code != 401, (name, r.status_code, r.text[:200])
            for bad in (client.get(f"/v1/hooks/{name}", headers=callback),
                        client.post(f"/v1/hooks/{name}/more", headers=callback, json={}),
                        client.post("/v1/hooks/stop", headers=callback, json={"session_id": sid}),
                        client.get("/v1/tasks", headers=callback)):
                assert (bad.status_code, bad.json()["detail"]) == (401, CALLBACK_OUTSIDE_DETAIL)
        monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _ELSEWHERE)
        assert _refused_outside(client.post(f"/v1/hooks/{name}", headers=headers, json={"session_id": sid}))

    def test_a_callback_token_of_a_dead_session_reads_dead(self, public_side):
        from auth.session_token import create_callback_token
        r = client.post("/v1/hooks/file", headers=_bearer(create_callback_token(_sid(), "alpha", "")),
                        json={"session_id": "x"})
        assert (r.status_code, r.json()["detail"]) == (401, SESSION_DEAD_DETAIL)

    def test_the_callback_list_is_these_five_post_routes(self):
        """The list is what a Docker tool may call back with its own token
        (``server.proxy_callbacks``), each name a route; the five take the
        callback kind in their own verifier, the other hooks do not."""
        from auth.external_endpoints import (
            CALLBACK_ROUTES,
            is_callback_endpoint_allowed,
        )
        from tests.api.test_route_table_characterization import _walk
        listed = set(CALLBACK_ROUTES.pattern.split("(")[1].split(")")[0].split("|"))
        assert listed == set(_CALLBACKS)
        table: list = []
        _walk(app.routes, "", table)
        routes = {row["path"] for row in table if "POST" in row["methods"]}
        assert {f"/v1/hooks/{name}" for name in listed} <= routes
        for name in listed:
            assert is_callback_endpoint_allowed("POST", f"/v1/hooks/{name}")
            assert not is_callback_endpoint_allowed("GET", f"/v1/hooks/{name}")
        assert not is_callback_endpoint_allowed("POST", "/v1/hooks/media")
        # Inside, the callback kind reaches a hook outside the five through
        # no verifier: the route's own 401, never the route's answer.
        sid, _headers = _live()
        import config
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(config, "INTERNAL_LISTENER_PORT", _INSIDE)
            r = client.post("/v1/hooks/stop", headers=_callback(sid), json={"session_id": sid})
        assert r.status_code == 401

    def test_media_is_no_docker_callback(self, public_side, monkeypatch):
        """No Docker tool calls ``/v1/hooks/media``: the speech, music, video
        and display tools do, from inside a sandbox with the session's token.
        A callback token is refused on it, a session token answers inside."""
        import config
        from auth.external_endpoints import CALLBACK_OUTSIDE_DETAIL
        sid, headers = _live()
        body = {"session_id": sid, "source": "/workspace/a.mp4"}
        assert _refused_outside(client.post("/v1/hooks/media", headers=headers, json=body))
        r = client.post("/v1/hooks/media", headers=_callback(sid), json=body)
        assert (r.status_code, r.json()["detail"]) == (401, CALLBACK_OUTSIDE_DETAIL)
        monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INSIDE)
        assert not _refused_outside(client.post("/v1/hooks/media", headers=headers, json=body))

    def test_a_newline_after_a_route_buys_nothing(self, public_side):
        """``%0A`` after a path reaches the route it follows. A callback
        spelled that way is not on the callback list, and a credential route
        spelled that way is still one: neither is a way past the listener."""
        from auth.external_endpoints import CALLBACK_OUTSIDE_DETAIL
        sid, headers = _live()
        for path in ("/v1/hooks/file%0A", "/v1/hooks/resolve-path%0A", "/v1/hooks/images%0A"):
            r = client.post(path, headers=_callback(sid), json={"session_id": sid})
            assert (r.status_code, r.json()["detail"]) == (401, CALLBACK_OUTSIDE_DETAIL), path
        r = client.post("/v1/hooks/cli-credential%0A", headers=headers, json={"session_id": sid})
        assert _refused_outside(r)

    def test_the_newline_form_reaches_the_handler_when_nothing_refuses(self, inside):
        """Why the rule above is needed: on the internal listener the same
        spelling is answered by the credential route itself (its own 401 for
        a missing token), not by a 404."""
        r = client.post("/v1/hooks/mcp-credentials%0A", json={})
        assert r.status_code == 401 and r.json()["detail"] == "Missing capability token"

    def test_a_dead_token_reads_dead_on_either_side(self, public_side):
        r = client.get(ROUTE, headers=_bearer(create_session_token(_sid(), "alpha", "user-admin")))
        assert (r.status_code, r.json()["detail"]) == (401, SESSION_DEAD_DETAIL)

    @pytest.mark.parametrize("path", [
        "/v1/hooks/cli-credential", "/v1/hooks/mcp-credentials",
        # The router's pattern ends in ``$``, which matches before a final
        # newline too: the same handler, so the same answer.
        "/v1/hooks/cli-credential%0A", "/v1/hooks/mcp-credentials%0A",
        # A tool's saved answers and its start, on its fetch token.
        "/v1/hooks/tool-listing", "/v1/hooks/tool-start", "/v1/hooks/tool-start%0A",
    ])
    def test_the_routes_that_hand_out_credentials_answer_inside_only(self, monkeypatch, path):
        """Their tokens are of another kind (git's, a tool program's), so the
        listener is asked by the
        path: no bearer, any bearer, the same."""
        import config
        monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _ELSEWHERE)
        method = "GET" if path.startswith("/v1/hooks/tool-listing") else "POST"
        for headers in ({}, _bearer("not-a-session-token"), _live()[1]):
            r = client.request(method, path, headers=headers, json=None if method == "GET" else {})
            assert _refused_outside(r), headers
        monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INSIDE)
        r = client.request(method, path, headers={}, json=None if method == "GET" else {})
        # Inside, the route itself answers (its own refusal of a missing
        # token, or its validation), never the listener rule nor a 405.
        assert not _refused_outside(r) and r.status_code != 405, r.status_code

    def test_the_master_key_and_a_cookie_meet_no_listener_rule(self, public_side):
        import config
        r = client.get("/v1/internal/session-count", headers=_bearer(config.API_KEY))
        assert not _refused_outside(r)
        assert not _refused_outside(client.get("/auth/me"))


# --- the routes a session's token reaches -------------------------------------

_UUID = "0123abcd-0123-4abc-8abc-0123456789ab"
# One representative route per caller family (the plan's list; the route
# answers itself, never the sentence). The token's agent is ``alpha``.
_ON_LIST = [
    ("POST", "/v1/hooks/permission"), ("POST", "/v1/hooks/stop"), ("POST", "/v1/hooks/cli-credential"),
    ("POST", "/v1/hooks/ssh-agent"), ("POST", "/v1/hooks/file"), ("POST", "/v1/hooks/media"), ("POST", "/v1/hooks/apps/pin"),
    ("POST", "/v1/hooks/files/pin"), ("POST", "/v1/mcp-gateway/github/mcp/"),
    ("GET", "/v1/session/current"), ("GET", "/v1/execution-layers"),
    ("POST", "/v1/tasks/scheduled"), ("GET", "/v1/tasks"), ("GET", f"/v1/tasks/{_UUID}"),
    ("POST", f"/v1/tasks/{_UUID}/run"), ("GET", "/v1/tasks/runs"), ("POST", f"/v1/tasks/runs/{_UUID}/cancel"),
    ("POST", "/v1/continuations"), ("GET", "/v1/notifications"), ("DELETE", f"/v1/notifications/{_UUID}"),
    ("POST", f"/v1/notifications/{_UUID}/pause"), ("GET", "/v1/triggers"), ("POST", f"/v1/triggers/{_UUID}/fire"),
    ("GET", "/v1/subscriptions"), ("POST", "/v1/meetings"), ("POST", f"/v1/meetings/{_UUID}/leave"),
    ("POST", "/v1/delegation/spawn"), ("GET", "/v1/delegation/sessions"), ("GET", "/v1/delegation/notebook"),
    ("POST", "/v1/chats/agent-move"), ("POST", "/v1/internal/memory/op"),
    ("GET", "/v1/internal/memory/agent-settings/alpha"), ("GET", "/v1/agents/alpha/checks"),
    ("GET", "/v1/checks/attached"), ("POST", "/v1/checks/run"), ("GET", "/v1/agents/alpha/info"),
    ("PATCH", "/v1/agents/alpha"), ("GET", "/v1/agents/alpha/files"),
    ("PUT", "/v1/agents/alpha/files/config/agent.md"), ("GET", "/v1/agents/alpha/outside"),
    ("GET", "/v1/agents/alpha/knowledge-attachments"), ("GET", "/v1/knowledge-libraries"),
    ("GET", "/v1/departments"), ("POST", "/v1/agents/alpha/reload-session"),
    ("GET", "/v1/community/agents"), ("POST", "/v1/cards"), ("GET", f"/v1/cards/{_UUID}"),
    ("GET", "/v1/agents/local-template/building-blocks"), ("GET", "/v1/agents/alpha/mcps"),
    ("GET", "/v1/agents/alpha/skills"), ("GET", "/v1/agents/alpha/mcp-requests"),
    ("GET", "/v1/community/mcps"), ("POST", "/v1/mcps/local-package/validate"),
    ("GET", "/v1/settings/catalog"), ("POST", "/v1/settings/card"), ("POST", "/v1/cards/list"),
    ("GET", "/v1/agents/alpha/ssh-hosts"), ("POST", "/v1/location/request"),
    ("GET", "/v1/audio/tts/voices"), ("POST", "/v1/audio/tts/generate"), ("POST", "/v1/audio/transcribe"),
    ("POST", "/v1/images/temp"), ("POST", "/v1/phone/call-control"), ("GET", "/v1/conversations"),
    ("GET", "/v1/conversations/kind"), ("POST", "/v1/conversations/record"), ("POST", "/v1/phone/calls"),
    ("GET", f"/v1/phone/calls/{_UUID}/wait"), ("POST", f"/v1/apps/{_UUID}/api/items"),
    ("POST", f"/v1/apps/{_UUID}/platform/files.write"), ("POST", f"/v1/apps/{_UUID}/push"),
    ("PATCH", f"/v1/apps/{_UUID}/state"),
]
# One route per family the list leaves out (a session token reaches it
# today through the route's own gate, or the gate refuses it already).
_OFF_LIST = [
    ("GET", "/v1/chats"), ("GET", f"/v1/chats/{_UUID}"),
    ("POST", "/v1/users/me/execution-layers/claude-code-cli/subscriptions"), ("POST", "/v1/push/subscribe"),
    ("POST", "/v1/internal/memory/clear-my-memory"), ("POST", "/v1/internal/memory/clear-all"),
    ("GET", "/v1/internal/memory/settings"), ("GET", "/v1/internal/git/log"), ("POST", "/v1/subscriptions"),
    ("GET", f"/v1/subscriptions/{_UUID}/signing-secret"), ("POST", f"/v1/apps/{_UUID}/actions/go"),
    ("POST", "/v1/upload"), ("POST", "/v1/media/token"), ("GET", f"/v1/sessions/{_UUID}/pending"),
    ("GET", "/v1/models"), ("PATCH", f"/v1/departments/{_UUID}"),
    ("PUT", "/v1/users/me/audio-prefs"), ("POST", "/v1/audio/stt/session"), ("POST", "/v1/duplex/session"),
    ("GET", "/v1/agents/alpha/user-context"), ("GET", "/v1/agents/alpha/people"),
    ("GET", "/v1/agents/alpha/users"), ("GET", "/v1/agents/alpha/files/workspace/a.txt"),
    ("PUT", "/v1/agents/alpha/files/workspace/a.txt"), ("GET", "/v1/agents"), ("GET", "/v1/tasks/stats"),
    ("PATCH", f"/v1/tasks/{_UUID}"), ("POST", "/v1/notifications/mark-all-read"),
    ("PATCH", f"/v1/triggers/{_UUID}"), ("GET", "/v1/meetings"), ("GET", "/v1/notebook"),
    ("GET", f"/v1/apps/{_UUID}"), ("GET", "/v1/apps"), ("GET", "/v1/usage/me"),
    ("GET", "/v1/admin/users"), ("GET", "/v1/admin/execution-layers"), ("GET", "/v1/api/agents"),
    ("GET", "/v1/user-api-keys"), ("GET", "/auth/me"), ("PUT", "/v1/users/me/password"),
    ("GET", "/v1/remote-machines"), ("POST", "/v1/agents"), ("GET", "/v1/agents/alpha/api-keys"),
    ("POST", "/v1/internal/fire-task"), ("POST", "/v1/phone/calls/report"),
    ("POST", f"/v1/apps/{_UUID}/approve"), ("GET", "/metrics"),
]


def _concrete(path: str) -> str:
    """A route-table path with every parameter filled: an id's shape for an
    id, the one file the agent-config tool writes for a file path."""
    import re
    path = path.replace("{path:path}", "config/agent.md").replace("{rest:path}", "mcp")
    return re.sub(r"\{[^}]+\}", _UUID, path)


def _table() -> list[tuple[str, str]]:
    from tests.api.test_route_table_characterization import _walk
    table: list = []
    _walk(app.routes, "", table)
    return [(m, row["path"]) for row in table for m in row["methods"] if m not in ("HEAD", "OPTIONS")]


class TestRouteList:
    """A session's token works on the routes the platform's tools and hooks
    call and on no other: off the list it is refused with one sentence
    before the route runs (decision 4)."""

    @pytest.mark.parametrize(("method", "path"), _ON_LIST, ids=lambda v: v)
    def test_a_live_token_reaches_each_callers_route(self, inside, monkeypatch, method, path):
        """The route itself answers: never the sentence, never a method the
        path lacks, never the router's own "Not Found" (a row that names no
        route)."""
        _sid_, headers = _live()
        r = client.request(method, path, headers=headers)
        assert not _off_list(r), (method, path, r.status_code, r.text[:200])
        assert r.status_code != 405, (method, path)
        assert not (r.status_code == 404 and r.text == '{"detail":"Not Found"}'), (method, path)

    @pytest.mark.parametrize(("method", "path"), _OFF_LIST, ids=lambda v: v)
    def test_off_the_list_the_sentence_and_nothing_else(self, inside, method, path):
        """The route's own gate never runs: 403 and the sentence, for a
        person's token and for a no-person one."""
        for user_sub in ("user-admin", ""):
            _sid_, headers = _live(user_sub)
            r = client.request(method, path, headers=headers)
            assert _off_list(r), (method, path, user_sub, r.status_code, r.text[:200])

    def test_a_per_agent_row_binds_the_tokens_agent(self, inside):
        """A per-agent route answers the token's own agent alone; a token
        with no agent claim reaches no per-agent route."""
        _sid_, headers = _live()
        assert not _off_list(client.get("/v1/agents/alpha/info", headers=headers))
        assert _off_list(client.get("/v1/agents/beta/info", headers=headers))
        assert _off_list(client.put("/v1/agents/beta/mcps", headers=headers, json={}))
        from tests.conftest import live_session_token
        bare = _bearer(live_session_token(_sid(), "", "user-admin"))
        assert _off_list(client.get("/v1/agents/alpha/info", headers=bare))
        assert not _off_list(client.get("/v1/session/current", headers=bare))

    def test_the_master_key_a_cookie_and_an_api_key_meet_no_list(self, inside):
        """The three other principals answer their routes: the list judges
        session tokens alone. The API key is a real one, minted as the
        store mints them, so it is judged as a key and reaches its route."""
        import config
        from services.infra import api_key_manager as akm
        from storage.identity import api_key_store
        # The master key meets its own list (the service endpoints), never this one.
        r = client.post("/v1/sessions/warmup", headers=_bearer(config.API_KEY), json={"model": "x"})
        assert not _off_list(r) and r.status_code == 403 and "service key" in r.json()["detail"]
        assert client.get("/v1/chats", headers={"Cookie": f"session={_cookie()}"}).status_code == 200
        from datetime import UTC, datetime, timedelta
        raw, prefix = akm._generate_raw_key()
        api_key_store.create_user_api_key(
            user_sub="user-admin", name="off-the-list", prefix=prefix, key_hash=akm._hash_key(raw),
            permissions=["chat"], expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
        r = client.get("/v1/api/agents", headers=_bearer(raw))
        assert not _off_list(r) and r.status_code == 200, r.text[:200]

    def test_the_route_table_answers_a_token_on_the_list_or_with_the_sentence(self, inside):
        """Every route of the table, for a token of agent ``x`` (the
        parameters' value): on the list the route answers itself, off it the
        sentence and nothing else. No third way exists."""
        import re

        from auth.external_endpoints import is_session_endpoint_allowed
        from tests.conftest import live_session_token
        sid = _sid()
        headers = _bearer(live_session_token(sid, "x", "user-admin"))
        # A handler met with an empty body may raise: its 500 is the route's
        # own answer, which is the point, never the sentence.
        sweep = TestClient(app, raise_server_exceptions=False)
        for method, path in _table():
            concrete = re.sub(r"\{[^}]+\}", "x", path.replace("{path:path}", "config/agent.md")
                              .replace("{rest:path}", "mcp"))
            r = sweep.request(method, concrete, headers=headers)
            allowed = is_session_endpoint_allowed(method, concrete, "x")
            if allowed:
                assert not _off_list(r), (method, path, r.status_code)
            else:
                assert _off_list(r), (method, path, r.status_code, r.text[:160])

    def test_every_row_names_a_route_of_the_table(self):
        """No dead row: each pattern of the list is met by a route of the
        table with one of its methods."""
        from auth.external_endpoints import SESSION_ROUTES
        hit = set()
        for method, path in _table():
            concrete = _concrete(path)
            for i, (pattern, methods) in enumerate(SESSION_ROUTES):
                if method in methods and pattern.fullmatch(concrete):
                    hit.add(i)
        dead = [SESSION_ROUTES[i][0].pattern for i in range(len(SESSION_ROUTES)) if i not in hit]
        assert not dead, dead

    def test_the_narrower_lists_and_the_tunnels_agree_with_the_list(self):
        """An outside caller's route and a wrap-up's are on the session list
        (they are narrower lists of the same tokens), and every route on the
        session list is admitted by both tunnel lists, so a machine's session
        reaches what a local one does."""
        import sys

        from auth.external_endpoints import (
            is_external_endpoint_allowed,
            is_session_endpoint_allowed,
            is_wrapup_endpoint_allowed,
        )
        from core.remote.satellite_http_tunnel import _is_allowed_path
        from tests._paths import REPO_ROOT
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from satellite.transport.http_tunnel import _is_allowed_path as _sat_allowed
        for method, path in _table():
            concrete = _concrete(path)
            on_list = is_session_endpoint_allowed(method, concrete, _UUID)
            if is_external_endpoint_allowed(method, concrete) or is_wrapup_endpoint_allowed(method, concrete):
                assert on_list, (method, path)
            if on_list:
                assert _is_allowed_path(concrete) and _sat_allowed(concrete), (method, path)


def _cookie() -> str:
    from tests.fixtures.ws_dashboard_harness import session_cookie
    return session_cookie("user-admin")


_LIMITED = (("notifications-mcp", ("list_notifications",)),)


class TestRouteLimits:
    """On a first-party tool's route the session's tool limits hold as they
    do on the tool call (``auth/external_endpoints.ROUTE_TOOLS``): the
    agent's shell, calling the route with the session's own token, gets the
    limit's sentence and nothing else; the tools' granted routes, an
    unlimited server's routes, a route of the list no tool calls, and a
    session with no limit answer themselves."""

    @pytest.fixture(autouse=True)
    def _contexts(self):
        self.sids: list[str] = []
        yield
        for sid in self.sids:
            session_state.cleanup_session_permission_state(sid)

    def _session(self, limits) -> dict:
        from auth.path_policy import SecurityContext
        from tests.conftest import live_session_token
        sid = _sid()
        session_state.register_session_state(sid, "dontAsk", SecurityContext(
            role="manager", username="admin", agent="alpha", is_admin_agent=False,
            session_scope="user", tool_limits=limits))
        self.sids.append(sid)
        return _bearer(live_session_token(sid, "alpha", "user-admin"))

    def test_a_limited_tools_route_answers_the_limits_sentence(self, inside, temp_db):
        from services.mcp import tool_limits
        headers = self._session(_LIMITED)
        r = client.post("/v1/notifications", headers=headers, json={"title": "t", "body": "b"})
        assert r.status_code == 403, r.text[:200]
        assert r.json()["detail"] == tool_limits.refusal(_LIMITED, "notifications-mcp", "create_notification")
        assert "may not use create_notification" in r.json()["detail"]
        # The granted tool's route, an unlimited server's and a route no
        # tool calls answer themselves.
        assert client.get("/v1/notifications", headers=headers).status_code == 200
        assert client.get("/v1/tasks", headers=headers).status_code == 200
        assert client.post("/v1/hooks/stop", headers=headers, json={}).status_code != 403

    def test_the_same_route_answers_with_no_limit(self, inside, temp_db):
        from storage import pg
        with pg.get_conn() as conn:   # the target the route resolves is the person's username
            conn.cursor().execute("UPDATE users SET username = 'user-admin' WHERE sub = 'user-admin'")
            conn.commit()
        body = {"title": "t", "body": "b"}
        assert client.post("/v1/notifications", headers=self._session(()), json=body).status_code == 200
        # A session nothing registered a context for is not held either.
        _sid_, bare = _live()
        assert client.post("/v1/notifications", headers=bare, json=body).status_code == 200

    def test_a_route_two_tools_share_stays_open_while_one_is_allowed(self, inside, temp_db):
        """``GET /v1/session/current`` is read before a notification and
        before a meeting: with the notification tool off it still answers
        the meeting tool, and is refused once both are off."""
        assert client.get("/v1/session/current", headers=self._session(_LIMITED)).status_code == 200
        both = self._session(_LIMITED + (("meetings-mcp", ("end_meeting",)),))
        r = client.get("/v1/session/current", headers=both)
        assert r.status_code == 403 and "may not use create_notification" in r.json()["detail"]

    def test_an_agents_own_run_is_held_on_the_wall(self, inside, temp_db):
        """A run with no person behind it (an agent-scope task, the token
        with no ``user_sub`` and no ``ext``): the wall reads its registered
        pairs as a person's."""
        from auth.path_policy import SecurityContext
        from tests.conftest import live_session_token
        sid = _sid()
        session_state.register_session_state(sid, "auto", SecurityContext(
            role="manager", username="", agent="alpha", is_admin_agent=False,
            session_scope="agent", tool_limits=_LIMITED))
        self.sids.append(sid)
        headers = _bearer(live_session_token(sid, "alpha", ""))
        r = client.post("/v1/notifications", headers=headers, json={"title": "t", "body": "b"})
        assert r.status_code == 403 and "may not use create_notification" in r.json()["detail"], r.text[:200]
        assert client.get("/v1/notifications", headers=headers).status_code == 200

    def test_the_live_tools_rest_twins_are_held_with_their_hooks(self, inside, temp_db):
        """display's ``app_push`` and ``app_state`` have a REST twin the
        shell calls (``app_proxy.push_rest``, ``state_rest``): held with the
        hook under a display limit, the route's own answer with no pair."""
        held = self._session((("display", ("display_images",)),))
        hook = client.post("/v1/hooks/apps/push", headers=held, json={"session_id": "x", "slug": "a", "payload": {}})
        twin = client.post(f"/v1/apps/{_UUID}/push", headers=held, json={"payload": {}})
        state = client.patch(f"/v1/apps/{_UUID}/state", headers=held, json={"patch": {}})
        assert hook.status_code == 403 and "may not use app_push" in hook.json()["detail"]
        assert twin.status_code == 403 and twin.json()["detail"] == hook.json()["detail"], twin.text[:200]
        assert state.status_code == 403 and "may not use app_state" in state.json()["detail"], state.text[:200]
        open_ = self._session(())
        assert client.post(f"/v1/apps/{_UUID}/push", headers=open_, json={"payload": {}}).status_code != 403
        assert client.patch(f"/v1/apps/{_UUID}/state", headers=open_, json={"patch": {}}).status_code != 403

    def test_a_hand_built_outside_context_with_the_apps_tool_held_is_refused(self, inside, temp_db):
        """The apps tool is platform-assigned, so a real outside session
        carries no pair for it; a context built with one is judged on the
        app's method all the same."""
        from auth.path_policy import SecurityContext
        from tests.conftest import live_session_token
        sid = _sid()
        session_state.register_session_state(sid, "dontAsk", SecurityContext(
            role="viewer", username="", agent="alpha", is_admin_agent=False, session_scope="agent",
            outside_grants=(("outside-apps", None),), tool_limits=(("outside-apps", ()),)))
        self.sids.append(sid)
        headers = _bearer(live_session_token(sid, "alpha", "", external="phone"))
        r = client.post(f"/v1/apps/{_UUID}/api/ping", headers=headers, json={})
        assert r.status_code == 403 and "may not use use_app" in r.json()["detail"], r.text[:200]
