"""Re-warming a task always rebuilds in the TASK's stored scope.

A dead task session reopened from the dashboard (``/runs/{run_id}`` →
``task-{run_id}`` chat) must rebuild with the task's ORIGINAL scope/identity,
never the identity of whoever re-opened it. Otherwise an admin reopening a
dead **agent-scoped** task would get their own full-FS mounts (``/users``,
``/config`` RW) instead of the agent-scope sandbox (``/workspace`` RW,
``/knowledge`` RO).

Covers the three layers of the fix:
  1. ``resolve_task_identity`` — the single source of truth for task identity.
  2. ``_task_continue_allowed`` — the WS continue-gate predicate.
  3. ``build_agent_config(task_identity=...)`` — the override that threads the
     resolved identity into the AgentConfig / SecurityContext / sandbox scope.
  4. End-to-end: the resolved identity drives the agent-scope sandbox mounts.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/tasks/test_task_rewarm_scope.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from core.config.task_config_builder import (
    TaskIdentity, resolve_task_identity, task_allows_knowledge_rw,
)
from ws.dashboard import _rewarm_chat_allowed, _task_continue_allowed
from storage import agent_store
from storage import database as task_store


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _mk_user(sub: str, name: str, role: str = "member") -> str:
    """Create a user, return their username slug."""
    task_store.upsert_user(sub, f"{sub}@x.test", name, role)
    return task_store.get_username_by_sub(sub)


# ---------------------------------------------------------------------------
# resolve_task_identity — the identity a task session runs as
# ---------------------------------------------------------------------------

class TestResolveTaskIdentity:
    def test_agent_scope_regular_agent_is_manager_no_user(self, temp_db):
        agent_store.create_agent("ops", "Ops")
        ident = resolve_task_identity("ops", "agent", None)
        assert ident == TaskIdentity(
            username="", role="manager", scope="agent", creds_user_sub=None,
        )

    def test_agent_scope_admin_only_agent_is_admin_no_user(self, temp_db):
        agent_store.create_agent("secret", "Secret", admin_only=True)
        ident = resolve_task_identity("secret", "agent", None)
        assert ident == TaskIdentity(
            username="", role="admin", scope="agent", creds_user_sub=None,
        )

    def test_user_scope_creator_admin(self, temp_db):
        agent_store.create_agent("pa", "PA")
        _mk_user("sub-admin", "Ada", role="admin")
        ident = resolve_task_identity("pa", "user", "sub-admin")
        assert ident.scope == "user"
        assert ident.role == "admin"
        assert ident.creds_user_sub == "sub-admin"
        assert ident.username == task_store.get_username_by_sub("sub-admin")

    def test_user_scope_creator_per_agent_manager(self, temp_db):
        agent_store.create_agent("pa", "PA")
        _mk_user("sub-mgr", "Max", role="member")
        task_store.add_user_agent("sub-mgr", "pa", "manager", "admin")
        ident = resolve_task_identity("pa", "user", "sub-mgr")
        assert ident.role == "manager"
        assert ident.scope == "user"
        assert ident.creds_user_sub == "sub-mgr"

    def test_user_scope_creator_per_agent_editor(self, temp_db):
        agent_store.create_agent("pa", "PA")
        _mk_user("sub-ed", "Eve", role="member")
        task_store.add_user_agent("sub-ed", "pa", "editor", "admin")
        ident = resolve_task_identity("pa", "user", "sub-ed")
        assert ident.role == "editor"

    def test_user_scope_creator_unassigned_is_viewer(self, temp_db):
        agent_store.create_agent("pa", "PA")
        _mk_user("sub-none", "Noa", role="member")  # no per-agent assignment
        ident = resolve_task_identity("pa", "user", "sub-none")
        assert ident.role == "viewer"
        assert ident.scope == "user"

    def test_shared_only_agent_forces_agent_scope_even_if_user_passed(self, temp_db):
        # Shared-only agents always run agent-scoped regardless of stored scope —
        # this keeps their credentials on the platform pool (creds_user_sub=None),
        # never the task creator's subscription.
        agent_store.create_agent("voicebot", "VoiceBot",
                                 default_scope="agent", collaborative=False)
        _mk_user("sub-x", "Xan", role="member")
        ident = resolve_task_identity("voicebot", "user", "sub-x")
        assert ident.scope == "agent"
        assert ident.username == ""
        assert ident.creds_user_sub is None

    def test_user_scope_without_created_by_falls_to_agent(self, temp_db):
        # Defensive: a "user" run with no created_by can't resolve a creator.
        agent_store.create_agent("ops", "Ops")
        ident = resolve_task_identity("ops", "user", None)
        assert ident.scope == "agent"
        assert ident.username == ""


# ---------------------------------------------------------------------------
# knowledge_rw provenance — manager-created agent-scope task fires
# ---------------------------------------------------------------------------

class TestKnowledgeRwProvenance:
    """Truth table for the manager-provenance knowledge-write grant.

    The flag is computed ONLY when the caller opts in (the scheduler's
    scheduled / one-time / trigger fire paths and their WS re-warms), and
    only on the agent-scope branch, from the dynamic task's ``created_by``:
    a REAL platform user who is a platform admin or a per-agent MANAGER of
    the agent. Everything else — editors, viewers, deleted users, agent
    slugs, ``"api"``, delegate fires, meetings, headless apps — is False.
    """

    def _ident(self, agent="ops", created_by=None, *, allow=True):
        return resolve_task_identity(
            agent, "agent", created_by, allow_knowledge_rw=allow)

    def test_platform_admin_creator_grants(self, temp_db):
        agent_store.create_agent("ops", "Ops")
        _mk_user("sub-a", "Ada", role="admin")
        assert self._ident(created_by="sub-a").knowledge_rw is True

    def test_per_agent_manager_creator_grants(self, temp_db):
        agent_store.create_agent("ops", "Ops")
        _mk_user("sub-m", "Max", role="member")
        task_store.add_user_agent("sub-m", "ops", "manager", "admin")
        assert self._ident(created_by="sub-m").knowledge_rw is True

    def test_editor_creator_denied(self, temp_db):
        agent_store.create_agent("ops", "Ops")
        _mk_user("sub-e", "Eve", role="member")
        task_store.add_user_agent("sub-e", "ops", "editor", "admin")
        assert self._ident(created_by="sub-e").knowledge_rw is False

    def test_viewer_and_unassigned_creator_denied(self, temp_db):
        agent_store.create_agent("ops", "Ops")
        _mk_user("sub-v", "Vic", role="member")
        task_store.add_user_agent("sub-v", "ops", "viewer", "admin")
        assert self._ident(created_by="sub-v").knowledge_rw is False
        _mk_user("sub-n", "Noa", role="member")  # no assignment at all
        assert self._ident(created_by="sub-n").knowledge_rw is False

    def test_manager_on_other_agent_denied(self, temp_db):
        # Per-agent roles only — manager of a DIFFERENT agent grants nothing.
        agent_store.create_agent("ops", "Ops")
        agent_store.create_agent("pa", "PA")
        _mk_user("sub-o", "Omar", role="member")
        task_store.add_user_agent("sub-o", "pa", "manager", "admin")
        assert self._ident(created_by="sub-o").knowledge_rw is False

    def test_nonuser_creators_denied(self, temp_db):
        # created_by is mixed-type: agent slugs (self-scheduled tasks) and
        # "api" never resolve to a platform user → no self-escalation.
        agent_store.create_agent("ops", "Ops")
        assert self._ident(created_by="ops").knowledge_rw is False
        assert self._ident(created_by="api").knowledge_rw is False
        assert self._ident(created_by=None).knowledge_rw is False
        assert self._ident(created_by="sub-deleted").knowledge_rw is False

    def test_without_opt_in_even_admin_denied(self, temp_db):
        # The default (meetings, headless apps, direct construction) is OFF.
        agent_store.create_agent("ops", "Ops")
        _mk_user("sub-a", "Ada", role="admin")
        assert self._ident(created_by="sub-a", allow=False).knowledge_rw is False
        assert resolve_task_identity("ops", "agent", "sub-a").knowledge_rw is False

    def test_user_scope_identity_never_carries_it(self, temp_db):
        agent_store.create_agent("pa", "PA")
        _mk_user("sub-m", "Max", role="member")
        task_store.add_user_agent("sub-m", "pa", "manager", "admin")
        ident = resolve_task_identity(
            "pa", "user", "sub-m", allow_knowledge_rw=True)
        assert ident.scope == "user"
        assert ident.knowledge_rw is False

    def test_admin_only_agent_branch_carries_it(self, temp_db):
        agent_store.create_agent("secret", "Secret", admin_only=True)
        _mk_user("sub-a", "Ada", role="admin")
        ident = self._ident(agent="secret", created_by="sub-a")
        assert ident.role == "admin"
        assert ident.knowledge_rw is True

    def test_reresolve_at_fire_demote_between_fires(self, temp_db):
        # The flag is provenance re-resolved at EVERY fire: a creator demoted
        # from manager to editor loses it at the next resolve.
        agent_store.create_agent("ops", "Ops")
        _mk_user("sub-m", "Max", role="member")
        task_store.add_user_agent("sub-m", "ops", "manager", "admin")
        assert self._ident(created_by="sub-m").knowledge_rw is True
        task_store.set_user_agent_role("sub-m", "ops", "editor")
        assert self._ident(created_by="sub-m").knowledge_rw is False

    def test_task_type_gate(self):
        # Scheduler fire shapes AND delegate workers opt in (operator
        # decision 2026-08-24: a delegate's created_by is the real
        # delegating user, so the manager-provenance check applies the same
        # way — no-user delegations record the agent slug, which the
        # provenance check fails closed on). Continuations / unknowns stay
        # out.
        assert task_allows_knowledge_rw("scheduled") is True
        assert task_allows_knowledge_rw("one_time") is True
        assert task_allows_knowledge_rw("trigger") is True
        assert task_allows_knowledge_rw("delegate") is True
        assert task_allows_knowledge_rw("continuation") is False
        assert task_allows_knowledge_rw("") is False
        assert task_allows_knowledge_rw(None) is False

    def test_delegate_provenance_matrix(self, temp_db):
        # Manager-delegated agent-scope worker gets knowledge RW on the
        # TARGET (same reach as that user's own chat there); an editor's
        # delegation and an agent-to-agent delegation (created_by = source
        # agent slug) stay RO.
        agent_store.create_agent("ops", "Ops")
        _mk_user("sub-mgr", "Mia", role="member")
        task_store.add_user_agent("sub-mgr", "ops", "manager", "admin")
        _mk_user("sub-ed", "Ed", role="member")
        task_store.add_user_agent("sub-ed", "ops", "editor", "admin")
        assert self._ident(created_by="sub-mgr").knowledge_rw is True
        assert self._ident(created_by="sub-ed").knowledge_rw is False
        # No-user delegation: created_by is the SOURCE AGENT slug.
        assert self._ident(created_by="some-agent").knowledge_rw is False


# ---------------------------------------------------------------------------
# _task_continue_allowed — the WS continue-gate
# ---------------------------------------------------------------------------

class TestTaskContinueAllowed:
    def _run(self, scope: str, created_by: str | None = None) -> dict:
        return {"scope": scope, "created_by": created_by, "agent": "ops"}

    # --- agent-scoped: editor+ ---
    def test_agent_scope_viewer_denied(self):
        assert not _task_continue_allowed(
            self._run("agent"), effective_role="viewer", user_sub="u1",
        )

    def test_agent_scope_editor_allowed(self):
        assert _task_continue_allowed(
            self._run("agent"), effective_role="editor", user_sub="u1",
        )

    def test_agent_scope_manager_allowed(self):
        assert _task_continue_allowed(
            self._run("agent"), effective_role="manager", user_sub="u1",
        )

    def test_agent_scope_admin_allowed(self):
        assert _task_continue_allowed(
            self._run("agent"), effective_role="admin", user_sub="u1",
        )

    def test_agent_scope_default_when_scope_missing(self):
        # A run row with no scope defaults to agent → editor+ gate.
        assert not _task_continue_allowed(
            {"created_by": None, "agent": "ops"}, effective_role="viewer", user_sub="u1",
        )

    # --- user-scoped: creator or admin ---
    def test_user_scope_creator_allowed(self):
        assert _task_continue_allowed(
            self._run("user", created_by="u1"), effective_role="viewer", user_sub="u1",
        )

    def test_user_scope_non_creator_denied(self):
        # Even a manager of the agent can't continue someone else's user task.
        assert not _task_continue_allowed(
            self._run("user", created_by="u1"), effective_role="manager", user_sub="u2",
        )

    def test_user_scope_admin_allowed(self):
        # Platform admin (effective_role "admin") may continue any user task.
        assert _task_continue_allowed(
            self._run("user", created_by="u1"), effective_role="admin", user_sub="u2",
        )


# ---------------------------------------------------------------------------
# _rewarm_chat_allowed — the warmup's existing-chat reuse gate
# ---------------------------------------------------------------------------

class TestRewarmChatAllowed:
    """Task-run chats are owned by ``task::<agent>`` (agent-scope) or the
    creator (user-scope) — never the viewer. The warmup must still reuse the
    chat row (and thus read its stored session_id + hit the resume gate);
    the continue-gate above enforces WHO may do it. Regression: without the
    task clause every dead-session task continue silently spawned a fresh,
    context-less session and overwrote the chat's session binding."""

    def test_agent_scope_task_chat_allowed_for_non_owner(self):
        chat = {"user_sub": "task::ops", "agent": "ops"}
        assert _rewarm_chat_allowed(chat, "task-run-abc", "sub-admin", [])

    def test_user_scope_task_chat_allowed_for_admin_non_creator(self):
        chat = {"user_sub": "sub-creator", "agent": "pa"}
        assert _rewarm_chat_allowed(chat, "task-run-abc", "sub-admin", [])

    def test_own_chat_allowed(self):
        chat = {"user_sub": "sub-u1", "agent": "pa"}
        assert _rewarm_chat_allowed(chat, "chat-1", "sub-u1", [])

    def test_foreign_personal_chat_denied(self):
        chat = {"user_sub": "sub-u1", "agent": "pa"}
        assert not _rewarm_chat_allowed(chat, "chat-1", "sub-u2", ["pa"])

    def test_shared_owner_chat_requires_assignment(self):
        chat = {"user_sub": "agent::ops", "agent": "ops"}
        assert _rewarm_chat_allowed(chat, "chat-1", "sub-u1", ["ops"])
        assert not _rewarm_chat_allowed(chat, "chat-1", "sub-u1", ["other"])


# ---------------------------------------------------------------------------
# End-to-end: the resolved identity drives the sandbox mounts.
# Proves no escalation — an agent-scoped run → /workspace RW + /knowledge RO,
# NO /config, NO /users — independent of who reopened it.
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_agents(tmp_path):
    agents_dir = tmp_path / "agents"
    pa = agents_dir / "pa"
    (pa / "config" / "context").mkdir(parents=True)
    (pa / "workspace").mkdir(parents=True)
    (pa / "knowledge").mkdir(parents=True)
    (pa / "users" / "creator" / "workspace").mkdir(parents=True)
    (pa / "users" / "creator" / "context").mkdir(parents=True)
    mcps_dir = tmp_path / "mcps"
    (mcps_dir / "custom").mkdir(parents=True)
    return agents_dir, mcps_dir


def _sandbox_for(agents_dir, mcps_dir, *, username: str, role: str):
    from core.sandbox.sandbox import SandboxConfig, SandboxBuilder
    claude_dir = (
        agents_dir / "pa" / "users" / username / ".claude" if username
        else agents_dir / "pa" / "workspace" / ".claude"
    )
    claude_dir.mkdir(parents=True, exist_ok=True)
    cfg = SandboxConfig(
        role=role,
        username=username,
        agent_name="pa",
        is_admin_agent=False,
        host_agents_dir=agents_dir.resolve(),
        host_mcps_dir=mcps_dir.resolve(),
        host_claude_dir=claude_dir.resolve(),
        mcp_sandbox_mounts=[],
        # Egress is orthogonal to what these tests assert (mount identity);
        # supply a resolved value so build_command_prefix's "egress must be
        # resolved via resolve_sandbox_config()" guard doesn't trip.
        net_forwards=["8400"],
    )
    return SandboxBuilder(cfg)


class TestIdentityDrivesMounts:
    def test_agent_scope_identity_yields_agent_mounts(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        ident = TaskIdentity(username="", role="manager", scope="agent", creds_user_sub=None)
        sb = _sandbox_for(agents_dir, mcps_dir, username=ident.username, role=ident.role)
        cmd_str = " ".join(sb.build_command_prefix(["claude"]))
        agent_dir = str((agents_dir / "pa").resolve())
        assert "/workspace" in cmd_str
        # No escalation: no per-user dir, no behavior-layer config.
        assert f"{agent_dir}/users" not in cmd_str
        assert f"{agent_dir}/config" not in cmd_str
        # Knowledge is read-only in agent scope.
        bind_pairs = list(zip(sb.build_command_prefix(["claude"]),
                             sb.build_command_prefix(["claude"])[1:]))
        assert any(
            a == "--ro-bind" and b == f"{agent_dir}/knowledge"
            for a, b in bind_pairs
        )

    def test_user_scope_identity_yields_creator_user_dir(self, tmp_agents):
        agents_dir, mcps_dir = tmp_agents
        # User-scope task re-warms as its creator → that user's dir is mounted.
        ident = TaskIdentity(
            username="creator", role="manager", scope="user", creds_user_sub="sub-c",
        )
        sb = _sandbox_for(agents_dir, mcps_dir, username=ident.username, role=ident.role)
        cmd_str = " ".join(sb.build_command_prefix(["claude"]))
        agent_dir = str((agents_dir / "pa").resolve())
        assert f"{agent_dir}/users/creator" in cmd_str


# ---------------------------------------------------------------------------
# build_agent_config(task_identity=...) — the override threads identity into
# the AgentConfig. Heavy collaborators are stubbed so the test isolates the
# identity-resolution path (MCP/dynamic/subscription/remote are out of scope).
# ---------------------------------------------------------------------------

def _stub_heavy(monkeypatch, tmp_path):
    from core.config import config_builder as cb

    monkeypatch.setattr(
        cb.mcp_registry, "build_session_mcp_config",
        lambda *a, **k: (None, {}, {}, {}, set()),  # 5th = bash_env_keys
    )
    monkeypatch.setattr(cb.mcp_registry, "get_agent_mcps", lambda *a, **k: [])

    async def _no_dyn(*a, **k):
        return []
    monkeypatch.setattr(cb.dynamic_context, "get_dynamic_contexts", _no_dyn)
    monkeypatch.setattr(
        cb.subscription_pool, "resolve_subscription_env",
        # Return a resolved subscription id so the identity tests exercise identity
        # resolution, not the user-scope "no subscription" block (NoSubscriptionError).
        lambda *a, **k: ("test-sub", {}),
    )
    monkeypatch.setattr(
        cb.remote_store, "resolve_execution_target", lambda *a, **k: ("local", None),
    )
    monkeypatch.setattr(
        cb.remote_store, "get_target_metadata", lambda *a, **k: ("local", "Local"),
    )
    monkeypatch.setattr(cb.config, "build_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(cb.config, "get_cli_model", lambda *a, **k: "m")
    monkeypatch.setattr(cb.config, "get_cli_effort", lambda *a, **k: "")
    # Spy on the persistent-dir creation to capture the (username, scope) the
    # session was built with — the sandbox scope is derived from these.
    calls: dict = {}

    def _spy_dir(agent_name, *, username="", scope="user"):
        calls["username"] = username
        calls["scope"] = scope
        return tmp_dir
    tmp_dir = tmp_path
    # config_builder calls ensure_persistent_agent_dir, which dispatches to these
    # — patch both at their definition site (core.sandbox.session_config_dir) so the
    # in-module dispatch hits the spy regardless of the agent's execution layer.
    import core.sandbox.session_config_dir as _scd
    monkeypatch.setattr(_scd, "ensure_persistent_claude_dir", _spy_dir)
    monkeypatch.setattr(_scd, "ensure_persistent_codex_dir", _spy_dir)
    # Dashboard adapter appends UI context — neutralize so the prompt is stable.
    import adapters.dashboard as dash
    monkeypatch.setattr(dash.DashboardAdapter, "build_client_context", lambda self, mc: "")
    return calls


class TestBuildAgentConfigTaskIdentity:
    def test_agent_scope_task_identity_overrides_admin_viewer(self, temp_db, monkeypatch, tmp_path):
        """The escalation case: an ADMIN reopening a dead agent-scoped task.
        With the override the session is built as agent-scope (username="",
        role manager), NOT the admin's full-FS identity."""
        from core.config.config_builder import build_agent_config
        agent_store.create_agent("ops", "Ops")
        admin = {"username": "ada", "display_name": "Ada", "email": "a@x", "role": "admin"}
        _mk_user("sub-ada", "Ada", role="admin")
        calls = _stub_heavy(monkeypatch, tmp_path)
        ident = resolve_task_identity("ops", "agent", None)

        cfg = asyncio.run(build_agent_config(
            agent_name="ops", user=admin, user_sub="sub-ada", user_role="admin",
            client_type="dashboard", chat_id="task-run-abc", task_identity=ident,
        ))
        sc = cfg.security_context
        assert sc.username == ""           # NOT "ada"
        assert sc.role == "manager"        # agent-scope role, not admin
        assert sc.display_name == ""       # viewer identity not leaked
        assert sc.email == ""
        assert calls["scope"] == "agent"   # persistent dir is agent-scope
        assert calls["username"] == ""

    def test_user_scope_task_identity_builds_as_creator(self, temp_db, monkeypatch, tmp_path):
        """A user-scoped task reopened by an admin re-warms as the CREATOR."""
        from core.config.config_builder import build_agent_config
        agent_store.create_agent("pa", "PA")
        creator_name = _mk_user("sub-bob", "Bob", role="member")
        task_store.add_user_agent("sub-bob", "pa", "manager", "admin")
        admin = {"username": "ada", "display_name": "Ada", "email": "a@x", "role": "admin"}
        calls = _stub_heavy(monkeypatch, tmp_path)
        ident = resolve_task_identity("pa", "user", "sub-bob")

        cfg = asyncio.run(build_agent_config(
            agent_name="pa", user=admin, user_sub="sub-ada", user_role="admin",
            client_type="dashboard", chat_id="task-run-xyz", task_identity=ident,
        ))
        sc = cfg.security_context
        assert sc.username == creator_name   # the creator, not "ada"
        assert sc.role == "manager"
        assert calls["scope"] == "user"
        assert calls["username"] == creator_name

    def test_no_task_identity_uses_viewer_identity(self, temp_db, monkeypatch, tmp_path):
        """Without task_identity (an ordinary chat) the viewer identity is
        used — contrasts the override and guards against accidental coupling."""
        from core.config.config_builder import build_agent_config
        agent_store.create_agent("pa", "PA")
        admin = {"username": "ada", "display_name": "Ada", "email": "a@x", "role": "admin"}
        _mk_user("sub-ada", "Ada", role="admin")
        calls = _stub_heavy(monkeypatch, tmp_path)

        cfg = asyncio.run(build_agent_config(
            agent_name="pa", user=admin, user_sub="sub-ada", user_role="admin",
            client_type="dashboard", chat_id="chat-normal",
        ))
        sc = cfg.security_context
        assert sc.username == "ada"
        assert calls["scope"] == "user"
        assert calls["username"] == "ada"
