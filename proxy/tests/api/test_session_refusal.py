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
