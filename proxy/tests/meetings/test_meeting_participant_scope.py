"""Meeting participant scope — a Shared-only agent in a USER-scope meeting must
run AGENT-scope (platform-pool credentials, no per-user dirs, one shared
history), never leaking the meeting creator's subscription.

``services/meeting_orchestrator.build_meeting_agent_config`` resolves each
participant via ``resolve_task_identity(agent, meeting["scope"], created_by)`` +
``resolve_visibility(scope_override=identity.scope)`` — the SAME path the task
builder uses. Merely clamping the visibility MOUNT isn't enough: the credentials
follow ``identity.creds_user_sub`` / ``identity.scope`` into
``build_session_mcp_config``, so the identity itself must force agent scope. This
test pins that resolution sequence.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/meetings/test_meeting_participant_scope.py -q
"""

from __future__ import annotations

from core.config.task_config_builder import resolve_task_identity
from core.session.visibility import resolve_visibility
from storage import agent_store
from storage import database as task_store


def _mk_user(sub: str, name: str, role: str = "member") -> str:
    task_store.upsert_user(sub, f"{sub}@x.test", name, role)
    return task_store.get_username_by_sub(sub)


def test_shared_only_participant_in_user_meeting_runs_agent_scope(temp_db):
    agent_store.create_agent(
        "caller", "Caller", default_scope="agent", collaborative=False,
    )
    creator = "sub-host"
    _mk_user(creator, "Hank")

    # The meeting was created in user scope by `creator`.
    identity = resolve_task_identity("caller", "user", creator)
    # Credentials clamp to agent scope → platform pool, NOT the creator's sub.
    assert identity.scope == "agent"
    assert identity.creds_user_sub is None
    assert identity.username == ""

    vis = resolve_visibility(
        "caller",
        username=identity.username or "",
        user_role=identity.role or "",
        user_sub=identity.creds_user_sub or "",
        scope_override=identity.scope,
    )
    # Mount + prompt scope clamp to agent (no per-user dirs, one shared space).
    assert vis.mount_scope == "agent"
    assert vis.mount_username == ""
    assert vis.available_scopes == ("agent",)


def test_manager_convened_agent_meeting_gets_no_knowledge_rw(temp_db):
    # Meetings record the real convener (api/meetings records created_by), so
    # a naive provenance thread would grant a manager-convened agent-scope
    # meeting knowledge RW. The meeting path calls resolve_task_identity
    # WITHOUT the opt-in, and meeting_context additionally pins the
    # SecurityContext field False — knowledge stays RO end to end.
    agent_store.create_agent("caller", "Caller", default_scope="agent",
                             collaborative=False)
    convener = "sub-mgr-host"
    _mk_user(convener, "Mia", role="admin")  # even a platform admin
    identity = resolve_task_identity("caller", "agent", convener)
    assert identity.knowledge_rw is False


def test_collaborative_participant_in_user_meeting_stays_user_scope(temp_db):
    # Control: a normal collaborative agent in a user meeting runs USER scope —
    # the creator's identity + credentials, mounting their per-user dirs.
    agent_store.create_agent("ops", "Ops")  # collaborative, default_scope=user
    creator = "sub-host2"
    uname = _mk_user(creator, "Ivy")

    identity = resolve_task_identity("ops", "user", creator)
    assert identity.scope == "user"
    assert identity.creds_user_sub == creator
    assert identity.username == uname

    vis = resolve_visibility(
        "ops", username=uname, user_role=identity.role or "",
        user_sub=creator, scope_override=identity.scope,
    )
    assert vis.mount_scope == "user"
