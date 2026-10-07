"""Tests for session-token binding on hook endpoints.

A session JWT is scoped to its session_id. Hook endpoints cross-check the
token's sid against the request body session_id (``verify_session_match_async``)
so a compromised MCP can't request resources for other sessions."""

from fastapi import HTTPException

import pytest


@pytest.mark.asyncio
async def test_matching_session_accepted(temp_db):
    """Token for session-A + request body session-A → passes."""
    from api.sessions.sessions import verify_session_match_async
    from auth.session_token import create_session_token

    token = create_session_token("session-A", "agent-1")
    await verify_session_match_async(f"Bearer {token}", "session-A")  # must not raise


@pytest.mark.asyncio
async def test_mismatched_session_forbidden(temp_db):
    """Token for session-A + request body session-B → 403."""
    from api.sessions.sessions import verify_session_match_async
    from auth.session_token import create_session_token

    token = create_session_token("session-A", "agent-1")
    with pytest.raises(HTTPException) as exc:
        await verify_session_match_async(f"Bearer {token}", "session-B")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_master_api_key_bypasses_session_check(temp_db):
    """The master PROXY_API_KEY is service-to-service and not bound to any
    session — Docker MCPs on the platform use it."""
    import config
    from api.sessions.sessions import verify_session_match_async

    # Works for any session_id
    await verify_session_match_async(f"Bearer {config.API_KEY}", "session-anything")


@pytest.mark.asyncio
async def test_missing_authorization_rejected(temp_db):
    """Missing Authorization header is 401."""
    from api.sessions.sessions import verify_session_match_async
    with pytest.raises(HTTPException) as exc:
        await verify_session_match_async(None, "session-A")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_rejected(temp_db):
    """Malformed / unknown token is 401."""
    from api.sessions.sessions import verify_session_match_async
    with pytest.raises(HTTPException) as exc:
        await verify_session_match_async("Bearer not-a-real-token", "session-A")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_pending_endpoint_rejects_foreign_session_token(temp_db):
    """A token for session A must not drain session B's pending result —
    retrieval is destructive and the payload carries prompt + response."""
    from api.sessions.sessions import get_session_pending
    from auth.session_token import create_session_token

    token = create_session_token("session-A", "agent-1")
    with pytest.raises(HTTPException) as exc:
        await get_session_pending("session-B", f"Bearer {token}")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_pending_endpoint_own_session_passes_binding(temp_db):
    """Token for session A + request for session A passes the binding and
    reaches the lookup (404 here — no pending result stored)."""
    from api.sessions.sessions import get_session_pending
    from auth.session_token import create_session_token

    token = create_session_token("session-A", "agent-1")
    with pytest.raises(HTTPException) as exc:
        await get_session_pending("session-A", f"Bearer {token}")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_plan_endpoints_reject_foreign_or_absent_session(temp_db):
    """Plan list/read are session-bound: a session JWT must name its own
    session — foreign session_id → 403, missing session_id → 400."""
    from api.sessions.sessions import get_plan_file, list_plans
    from auth.session_token import create_session_token

    token = create_session_token("session-A", "agent-1")

    with pytest.raises(HTTPException) as exc:
        await list_plans(f"Bearer {token}", session_id="session-B")
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await list_plans(f"Bearer {token}", session_id=None)
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        await get_plan_file("x.md", f"Bearer {token}", session_id="session-B")
    assert exc.value.status_code == 403


def test_user_sub_round_trips_through_jwt(temp_db):
    """When the session JWT is minted with a user_sub, validate_session_token
    returns it — so the auth path can resolve to the real user and avoid
    the legacy ``session:<sid>`` synthetic identity."""
    from auth.session_token import create_session_token, validate_session_token

    token = create_session_token("session-A", "agent-1", user_sub="user-manager")
    payload = validate_session_token(token)
    assert payload is not None
    assert payload["sid"] == "session-A"
    assert payload["user_sub"] == "user-manager"

    # Backwards compat: tokens minted without user_sub still validate, just
    # with an empty string for the new field.
    legacy = create_session_token("session-B", "agent-2")
    legacy_payload = validate_session_token(legacy)
    assert legacy_payload is not None
    assert legacy_payload.get("user_sub") == ""


@pytest.mark.asyncio
async def test_plan_file_is_confined_to_the_plans_dir(temp_db, tmp_path, monkeypatch):
    """A plan filename is a single segment, but a symlink dropped in the plans
    dir could still point outside it — the read resolves the path and refuses
    that with the same 404 as a missing plan."""
    from api.sessions import sessions
    from auth.session_token import create_session_token

    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "ok.md").write_text("# plan")
    secret = tmp_path / "secret.md"
    secret.write_text("private")
    (plans / "evil.md").symlink_to(secret)
    monkeypatch.setattr(sessions, "_get_plans_dir", lambda sid: plans)
    token = create_session_token("session-A", "agent-1")

    out = await sessions.get_plan_file("ok.md", f"Bearer {token}", session_id="session-A")
    assert out == {"content": "# plan", "filename": "ok.md"}
    with pytest.raises(HTTPException) as exc:
        await sessions.get_plan_file("evil.md", f"Bearer {token}", session_id="session-A")
    assert exc.value.status_code == 404
