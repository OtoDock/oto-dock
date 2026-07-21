"""Regression tests for token-authoritative identity (the identity-bleed fix).

Background: an inbound phone call (an agent-scope session whose JWT carried NO
real ``user_sub``) was able to create a task AS a real admin user, because the
server trusted a client-supplied ``created_by`` / ``X-On-Behalf-Of`` that had
itself been bled from ``/v1/session/current``'s agent-name recency scan.

The fix makes per-call identity come SOLELY from the authenticated token:

  * ``UserContext.acting_sub`` / ``is_no_user_session`` classify the caller.
  * ``_resolve_creator_identity`` / ``_check_task_permission`` attribute and
    gate from the token — client ``created_by`` / on-behalf are ignored.
  * ``/v1/session/current`` resolves the caller's OWN session by its token
    ``sid`` (no agent-name scan) and returns NO identity fields.

The four caller classes under test:
  - dashboard cookie        → real sub, is_api_key=False
  - real-user session token → real sub, is_api_key=True  (interactive agent)
  - no-user session token   → sub="session:<sid>", is_api_key=True (phone/svc)
  - master key              → sub="api-key", is_api_key=True (s2s)

Run: cd proxy && python -m pytest tests/tasks/test_task_identity_authority.py -v
"""

import sys

import pytest
from fastapi import HTTPException

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth.providers import UserContext  # noqa: E402
from api.tasks import tasks  # noqa: E402


# ───────────────────────────────────────────────────────────────────────────
# Caller factories
# ───────────────────────────────────────────────────────────────────────────


def _master_key():
    return UserContext(
        sub="api-key", email="api@internal", name="API Key", role="admin",
        is_api_key=True,
    )


def _no_user_session(sid="s-phone", agent="personal-assistant"):
    """A phone / trigger / meeting service session: token carried no user_sub."""
    return UserContext(
        sub=f"session:{sid}", email="session@internal", name="Session Token",
        role="admin", is_api_key=True, session_id=sid, agent=agent,
    )


def _real_user_session(sub="user-alice", sid="s-chat", agent="personal-assistant",
                       role="creator"):
    """An interactive agent subprocess whose JWT carried the real user_sub."""
    return UserContext(
        sub=sub, email="alice@test.com", name="Alice", role=role,
        is_api_key=True, session_id=sid, agent=agent,
    )


def _dashboard(sub="user-alice", role="creator"):
    return UserContext(sub=sub, email="alice@test.com", name="Alice", role=role)


# ───────────────────────────────────────────────────────────────────────────
# UserContext classification — the single identity seam
# ───────────────────────────────────────────────────────────────────────────


def test_acting_sub_classification():
    assert _master_key().acting_sub is None           # s2s, no user
    assert _no_user_session().acting_sub is None       # phone/service, no user
    assert _real_user_session(sub="u1").acting_sub == "u1"
    assert _dashboard(sub="u1").acting_sub == "u1"


def test_is_no_user_session_classification():
    assert _no_user_session().is_no_user_session is True
    assert _master_key().is_no_user_session is False   # master key is NOT no-user
    assert _real_user_session().is_no_user_session is False
    assert _dashboard().is_no_user_session is False


# ───────────────────────────────────────────────────────────────────────────
# _resolve_creator_identity — task attribution from the token only
# ───────────────────────────────────────────────────────────────────────────


def test_no_user_session_cannot_create_user_scope_task():
    with pytest.raises(HTTPException) as exc:
        tasks._resolve_creator_identity(_no_user_session(), "user", "personal-assistant")
    assert exc.value.status_code == 403


def test_master_key_cannot_create_user_scope_task():
    with pytest.raises(HTTPException) as exc:
        tasks._resolve_creator_identity(_master_key(), "user", "personal-assistant")
    assert exc.value.status_code == 400


def test_real_user_session_user_scope_attributes_to_self():
    created_by, acting = tasks._resolve_creator_identity(
        _real_user_session(sub="user-alice"), "user", "personal-assistant",
    )
    assert created_by == "user-alice"
    assert acting == "user-alice"


def test_dashboard_user_scope_attributes_to_self():
    created_by, acting = tasks._resolve_creator_identity(
        _dashboard(sub="user-alice"), "user", "personal-assistant",
    )
    assert created_by == "user-alice"
    assert acting == "user-alice"


def test_no_user_session_agent_scope_attributes_to_agent():
    created_by, acting = tasks._resolve_creator_identity(
        _no_user_session(agent="personal-assistant"), "agent", "personal-assistant",
    )
    assert created_by == "personal-assistant"
    assert acting is None


def test_master_key_agent_scope_uses_x_agent_name_then_api():
    cb, acting = tasks._resolve_creator_identity(_master_key(), "agent", "billing")
    assert cb == "billing" and acting is None
    cb2, acting2 = tasks._resolve_creator_identity(_master_key(), "agent", None)
    assert cb2 == "api" and acting2 is None


def test_creator_identity_takes_no_client_created_by():
    """Structural guarantee: the resolver's only inputs are the token-derived
    UserContext, the scope, and X-Agent-Name — there is NO client created_by /
    on-behalf parameter to forge. A real user is always attributed to self."""
    # 3 positional params: (user, scope, x_agent_name). No created_by channel.
    import inspect
    params = list(inspect.signature(tasks._resolve_creator_identity).parameters)
    assert params == ["user", "scope", "x_agent_name"]


# ───────────────────────────────────────────────────────────────────────────
# _check_task_permission — mutation gate from the token only
# ───────────────────────────────────────────────────────────────────────────


def test_no_user_session_cannot_mutate_user_scope_task():
    task = {"scope": "user", "created_by": "user-alice", "agent": "personal-assistant"}
    with pytest.raises(HTTPException) as exc:
        tasks._check_task_permission(task, _no_user_session())
    assert exc.value.status_code == 403


def test_no_user_session_may_manage_agent_scope_task():
    task = {"scope": "agent", "created_by": "personal-assistant", "agent": "personal-assistant"}
    # Must not raise — a phone/service session manages its agent's own tasks.
    tasks._check_task_permission(task, _no_user_session())


def test_master_key_full_mutation_access():
    user_task = {"scope": "user", "created_by": "user-bob", "agent": "x"}
    agent_task = {"scope": "agent", "created_by": "x", "agent": "x"}
    tasks._check_task_permission(user_task, _master_key())   # no raise
    tasks._check_task_permission(agent_task, _master_key())  # no raise


def test_real_user_cannot_mutate_another_users_task():
    task = {"scope": "user", "created_by": "user-bob", "agent": "x"}
    with pytest.raises(HTTPException) as exc:
        tasks._check_task_permission(task, _real_user_session(sub="user-alice"))
    assert exc.value.status_code == 403


def test_real_user_can_mutate_own_task():
    task = {"scope": "user", "created_by": "user-alice", "agent": "x"}
    tasks._check_task_permission(task, _real_user_session(sub="user-alice"))  # no raise


def test_agent_scope_editor_only_own(monkeypatch):
    """A real-user (per-agent editor) may mutate only their own agent-scope task."""
    from storage import database
    monkeypatch.setattr(database, "get_user", lambda sub: {"sub": sub, "role": "member"})
    monkeypatch.setattr(database, "get_user_agent_roles", lambda sub: {"billing": "editor"})

    own = {"scope": "agent", "created_by": "user-alice", "agent": "billing"}
    tasks._check_task_permission(own, _real_user_session(sub="user-alice"))  # no raise

    other = {"scope": "agent", "created_by": "user-bob", "agent": "billing"}
    with pytest.raises(HTTPException) as exc:
        tasks._check_task_permission(other, _real_user_session(sub="user-alice"))
    assert exc.value.status_code == 403


# ───────────────────────────────────────────────────────────────────────────
# /v1/session/current — own-session routing, NO identity, NO agent scan
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_current_resolves_own_session_no_identity(monkeypatch):
    from core.session import session_state
    from storage import database
    monkeypatch.setattr(session_state, "_sessions", {
        "s-chat": {"agent": "personal-assistant", "last_active": "2026-01-01T00:00:00"},
    })
    monkeypatch.setattr(database, "get_chat_by_session",
                        lambda sid: {"id": "chat-1"} if sid == "s-chat" else None)

    res = await tasks.get_current_session(
        user=_real_user_session(sid="s-chat"), x_agent_name="personal-assistant",
    )
    assert res == {"session_id": "s-chat", "chat_id": "chat-1",
                   "last_active": "2026-01-01T00:00:00"}
    # Identity fields are gone — an MCP can never learn "who the user is" here.
    assert "user_sub" not in res
    assert "user_name" not in res
    assert "username" not in res


@pytest.mark.asyncio
async def test_session_current_no_agent_recency_bleed(monkeypatch):
    """Two concurrent sessions on the SAME agent each resolve ONLY their own
    session — the phone session can never be handed the user's session (and
    vice-versa), which is the structural fix for the bleed."""
    from core.session import session_state
    from storage import database
    monkeypatch.setattr(session_state, "_sessions", {
        # The phone session is MORE recently active — under the old recency
        # scan it would have been returned to the user's caller (the bleed).
        "s-phone": {"agent": "personal-assistant", "last_active": "2026-02-02T00:00:00"},
        "s-chat":  {"agent": "personal-assistant", "last_active": "2026-01-01T00:00:00"},
    })
    monkeypatch.setattr(database, "get_chat_by_session",
                        lambda sid: {"id": f"chat-{sid}"})

    user_res = await tasks.get_current_session(
        user=_real_user_session(sid="s-chat"), x_agent_name="personal-assistant",
    )
    phone_res = await tasks.get_current_session(
        user=_no_user_session(sid="s-phone"), x_agent_name="personal-assistant",
    )
    assert user_res["session_id"] == "s-chat"    # NOT the more-recent phone session
    assert phone_res["session_id"] == "s-phone"  # each resolves its own


# ───────────────────────────────────────────────────────────────────────────
# Editors/managers who are platform "members" can schedule tasks
# (the redundant platform-level require_write(u) was dropped from task create
# + cancel; _enforce_task_scope + require_agent_access gate per-agent)
# ───────────────────────────────────────────────────────────────────────────


def _member(sub, agent, agent_role):
    return UserContext(
        sub=sub, email=f"{sub}@t.com", name=sub, role="member",
        agents=[agent], agent_roles={agent: agent_role},
    )


def test_member_with_agent_editor_can_create_agent_scope(monkeypatch):
    """A platform 'member' who is a per-agent editor passes the task scope gate
    for agent-scope — the platform-level require_write(u) that used to reject
    every non-creator/non-admin is gone."""
    from storage import agent_store
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda a: {"collaborative": True, "default_scope": "user"})
    tasks._enforce_task_scope(_member("u-ed", "acme", "editor"), "agent", "acme")  # no raise


def test_member_viewer_still_blocked_from_agent_scope(monkeypatch):
    """Viewers remain read-only — dropping require_write must not open agent
    scope to them; _enforce_task_scope still requires editor+."""
    from storage import agent_store
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda a: {"collaborative": True, "default_scope": "user"})
    with pytest.raises(HTTPException) as exc:
        tasks._enforce_task_scope(_member("u-vw", "acme", "viewer"), "agent", "acme")
    assert exc.value.status_code == 403


def test_member_can_create_own_user_scope(monkeypatch):
    """Any authenticated user with agent access may create their OWN user-scope
    task — the platform require_write gate that used to block plain members is
    gone (a user-scope task runs as, and is visible only to, its creator)."""
    from storage import agent_store
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda a: {"collaborative": True, "default_scope": "user"})
    tasks._enforce_task_scope(_member("u-vw", "acme", "viewer"), "user", "acme")  # no raise
