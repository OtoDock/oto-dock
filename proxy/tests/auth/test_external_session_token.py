"""The ``ext`` claim on session tokens and its principal semantics.

- ``create_session_token`` derives the claim from the session's REGISTERED
  SecurityContext (the layers register before they spawn), or takes it
  explicitly; a plain session mints no claim.
- ``get_current_user`` parses the claim; ``is_external`` is True only for a
  token with a claim and no real user.
- The persisted security index round-trips the new fields and drops
  external entries on reload.
"""

from __future__ import annotations

import uuid

import jwt
import pytest
from starlette.requests import Request

import config
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from auth.session_token import create_session_token, validate_session_token
from core.session import session_state


def _ctx(**kw) -> SecurityContext:
    base = dict(role="viewer", username="", agent="support", is_admin_agent=False)
    base.update(kw)
    return SecurityContext(**base)


def _decode(token: str) -> dict:
    return jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])


@pytest.fixture
def registered_external():
    sid = str(uuid.uuid4())
    session_state.set_session_security(sid, _ctx(
        principal="external", external_channel="phone", external_id="+3021",
        external_claim="phone:+3021", external_home="/tmp/x",
    ))
    yield sid
    session_state.cleanup_session_permission_state(sid)


class TestMint:
    def test_claim_is_derived_from_the_live_context(self, registered_external):
        token = create_session_token(registered_external, "support")
        assert _decode(token)["ext"] == "phone:+3021"
        assert validate_session_token(token)["ext"] == "phone:+3021"

    def test_no_context_no_claim(self):
        token = create_session_token(str(uuid.uuid4()), "support")
        assert "ext" not in _decode(token)

    def test_non_external_context_no_claim(self):
        sid = str(uuid.uuid4())
        session_state.set_session_security(sid, _ctx())
        try:
            assert "ext" not in _decode(create_session_token(sid, "support"))
        finally:
            session_state.cleanup_session_permission_state(sid)

    def test_explicit_claim_wins_and_empty_suppresses(self, registered_external):
        assert _decode(create_session_token(registered_external, "support", external="phone:"))["ext"] == "phone:"
        assert "ext" not in _decode(create_session_token(registered_external, "support", external=""))

    def test_cleanup_removes_the_claim_source(self, registered_external):
        session_state.cleanup_session_permission_state(registered_external)
        assert "ext" not in _decode(create_session_token(registered_external, "support"))


class TestPersistence:
    def test_round_trip_and_reload_drops_external(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_state, "_SECURITY_INDEX", tmp_path / "security.json")
        ext_sid, plain_sid = str(uuid.uuid4()), str(uuid.uuid4())
        ext_ctx = _ctx(principal="external", external_claim="phone:+3021",
                       external_home="/h", external_verified=True)
        session_state.set_session_security(ext_sid, ext_ctx)
        session_state.set_session_security(plain_sid, _ctx())
        try:
            # The serialised form carries the new fields...
            d = session_state._serialize_security_ctx(ext_ctx)
            assert d["principal"] == "external" and d["external_verified"] is True
            assert session_state._deserialize_security_ctx(d) == ext_ctx
            # ...and a reload keeps the plain session but drops the external one.
            session_state._session_security.pop(ext_sid)
            session_state._session_security.pop(plain_sid)
            session_state.load_session_security()
            assert session_state.get_session_security(plain_sid) is not None
            assert session_state.get_session_security(ext_sid) is None
        finally:
            session_state.cleanup_session_permission_state(ext_sid)
            session_state.cleanup_session_permission_state(plain_sid)


def _request_with_bearer(token: str) -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    return Request(scope)


def _request_with_cookie(token: str) -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"cookie", f"session={token}".encode())],
    }
    return Request(scope)


class TestPrincipalResolution:
    """The reads behind a principal run on the DB executor and happen once
    per connection (the memo on ``request.state``)."""

    @pytest.mark.asyncio
    async def test_cookie_principal_resolves_off_loop_and_once(self, temp_db, loop_db_guard, monkeypatch):
        from auth.providers import create_session_jwt
        from storage import database as task_store
        token = create_session_jwt("user-admin", "admin@test.com", "Admin User", "admin")
        req = _request_with_cookie(token)
        with loop_db_guard.active():
            u = await get_current_user(req)
        assert u is not None and u.sub == "user-admin" and not u.is_api_key

        def boom(*a, **kw):
            raise AssertionError("resolved twice on one request")

        monkeypatch.setattr(task_store, "get_user", boom)
        assert await get_current_user(req) is u
        # A fresh request resolves again (the memo is per connection).
        with pytest.raises(AssertionError):
            await get_current_user(_request_with_cookie(token))

    @pytest.mark.asyncio
    async def test_session_token_principal_resolves_off_loop(self, temp_db, loop_db_guard):
        token = create_session_token(str(uuid.uuid4()), "support", "user-admin")
        with loop_db_guard.active():
            u = await get_current_user(_request_with_bearer(token))
        assert u is not None and u.sub == "user-admin" and u.is_api_key


class TestPrincipal:
    @pytest.mark.asyncio
    async def test_no_user_token_with_claim_is_external(self, temp_db):
        token = create_session_token(str(uuid.uuid4()), "support", external="phone:+3021")
        u = await get_current_user(_request_with_bearer(token))
        assert u is not None
        assert u.is_no_user_session and u.is_external
        assert u.external_channel == "phone" and u.external_id == "+3021"
        assert u.acting_sub is None
        assert u.can_access_agent("support") and not u.can_access_agent("other")

    @pytest.mark.asyncio
    async def test_user_tied_token_with_claim_is_not_external(self, temp_db):
        token = create_session_token(str(uuid.uuid4()), "support", "user-admin",
                                     external="phone:+3021")
        u = await get_current_user(_request_with_bearer(token))
        assert u is not None and u.sub == "user-admin"
        assert u.external_claim == "phone:+3021"   # audit trail
        assert not u.is_external and u.acting_sub == "user-admin"

    @pytest.mark.asyncio
    async def test_plain_token_is_untouched(self, temp_db):
        token = create_session_token(str(uuid.uuid4()), "support")
        u = await get_current_user(_request_with_bearer(token))
        assert u is not None and not u.is_external and u.external_claim == ""

    @pytest.mark.asyncio
    async def test_malformed_claim_is_ignored(self, temp_db):
        token = create_session_token(str(uuid.uuid4()), "support", external="phone:not a number")
        u = await get_current_user(_request_with_bearer(token))
        assert u is not None and not u.is_external and u.external_claim == ""

    def test_property_shapes(self):
        ext = UserContext(sub="session:abc", email="", name="", role="agent",
                          is_api_key=True, agent="support", external_claim="phone:")
        assert ext.is_external
        user = UserContext(sub="user-1", email="", name="", role="member",
                           is_api_key=True, agent="support", external_claim="phone:+1")
        assert not user.is_external
        assert not UserContext(sub="api-key", email="", name="", role="admin",
                               is_api_key=True).is_external
