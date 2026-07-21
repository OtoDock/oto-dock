"""Scope × role × target-mode matrix for delegated-worker spawns.

``services/delegation/spawn_authz.authorize_spawn`` is the single gate both
delegate surfaces (task + chat) call before any row exists. Caller classes
mirror test_task_identity_authority.py: dashboard cookie, real-user session
token, no-user service session, master key.

Run: cd proxy && python -m pytest tests/tasks/test_spawn_authz.py -v
"""

import uuid

import pytest
from fastapi import HTTPException

from auth.providers import UserContext
from services.delegation.spawn_authz import authorize_spawn
from storage import agent_store, mcp_store
from storage import database as task_store


# ───────────────────────────────────────────────────────────────────────────
# Caller factories
# ───────────────────────────────────────────────────────────────────────────


def _master_key():
    return UserContext(
        sub="api-key", email="api@internal", name="API Key", role="admin",
        is_api_key=True,
    )


def _no_user_session(agent):
    return UserContext(
        sub="session:s-svc", email="session@internal", name="Session Token",
        role="admin", is_api_key=True, session_id="s-svc", agent=agent,
    )


def _real_user(agent, agent_role, *, sub="user-alice", platform_role="member"):
    """Dashboard-cookie caller with a per-agent role on ``agent``."""
    return UserContext(
        sub=sub, email="alice@test.com", name="Alice", role=platform_role,
        agents=[agent], agent_roles={agent: agent_role},
    )


def _admin(sub="user-root"):
    return UserContext(sub=sub, email="root@test.com", name="Root", role="admin")


# ───────────────────────────────────────────────────────────────────────────
# Fixtures — one agent per visibility mode + the kill-switch row
# ───────────────────────────────────────────────────────────────────────────


COLLAB_USER = "collab-user"
COLLAB_AGENT = "collab-agent"
PERSONAL_ONLY = "personal-only"
SHARED_ONLY = "shared-only"


@pytest.fixture
def delegation_env(temp_db):
    agent_store.create_agent(COLLAB_USER, "CU", collaborative=True, default_scope="user")
    agent_store.create_agent(COLLAB_AGENT, "CA", collaborative=True, default_scope="agent")
    agent_store.create_agent(PERSONAL_ONLY, "PO", collaborative=False, default_scope="user")
    agent_store.create_agent(SHARED_ONLY, "SO", collaborative=False, default_scope="agent")
    mcp_store.set_mcp_enabled("delegation-mcp", True)
    return temp_db


def _spawn(user, target=COLLAB_USER, scope="user", **kw):
    return authorize_spawn(user, target_agent=target, requested_scope=scope, **kw)


def _denied(user, status, target=COLLAB_USER, scope="user", **kw):
    with pytest.raises(HTTPException) as exc:
        _spawn(user, target=target, scope=scope, **kw)
    assert exc.value.status_code == status
    return exc.value


def _add_active_run(created_by, agent=COLLAB_USER):
    task_store.create_run(
        f"run-{uuid.uuid4().hex[:8]}", f"dyn-{uuid.uuid4().hex[:8]}", agent,
        "delegate", None, "work", task_type="delegate",
        scope="user", created_by=created_by,
    )


# ───────────────────────────────────────────────────────────────────────────
# Kill-switch
# ───────────────────────────────────────────────────────────────────────────


class TestKillSwitch:
    def test_missing_state_row_denies(self, temp_db):
        # No manifest scan ever ran (the public-cut shape) → dormant.
        agent_store.create_agent(COLLAB_USER, "CU")
        _denied(_admin(), 403)

    def test_disabled_row_denies(self, delegation_env):
        mcp_store.set_mcp_enabled("delegation-mcp", False)
        _denied(_admin(), 403)

    def test_enabled_allows(self, delegation_env):
        assert _spawn(_admin()).scope == "user"


# ───────────────────────────────────────────────────────────────────────────
# User scope
# ───────────────────────────────────────────────────────────────────────────


class TestUserScope:
    @pytest.mark.parametrize("role", ["viewer", "editor", "manager"])
    def test_real_user_any_role_allowed(self, delegation_env, role):
        authz = _spawn(_real_user(COLLAB_USER, role))
        assert authz.created_by == "user-alice"
        assert authz.acting_sub == "user-alice"
        assert authz.scope == "user"
        assert authz.scope_note == ""
        assert authz.chat_owner == "user-alice"

    def test_no_user_session_denied_403(self, delegation_env):
        _denied(_no_user_session(COLLAB_USER), 403, target=COLLAB_USER)

    def test_master_key_denied_400(self, delegation_env):
        _denied(_master_key(), 400, x_agent_name=COLLAB_USER)

    def test_shared_only_target_clamps_to_agent(self, delegation_env):
        authz = _spawn(_real_user(SHARED_ONLY, "editor"), target=SHARED_ONLY)
        assert authz.scope == "agent"
        assert "clamped" in authz.scope_note
        assert authz.chat_owner == f"agent::{SHARED_ONLY}"

    def test_shared_only_clamp_still_gates_viewer(self, delegation_env):
        # The clamp lands the request in agent scope, where viewers are
        # read-only — same bar as agent-scope tasks.
        _denied(_real_user(SHARED_ONLY, "viewer"), 403, target=SHARED_ONLY)

    def test_no_user_session_on_shared_only_clamps_and_allows(self, delegation_env):
        authz = _spawn(_no_user_session(SHARED_ONLY), target=SHARED_ONLY)
        assert authz.scope == "agent"
        assert authz.created_by == SHARED_ONLY
        assert authz.chat_owner == f"agent::{SHARED_ONLY}"


# ───────────────────────────────────────────────────────────────────────────
# Agent scope
# ───────────────────────────────────────────────────────────────────────────


class TestAgentScope:
    def test_viewer_denied(self, delegation_env):
        _denied(_real_user(COLLAB_AGENT, "viewer"), 403,
                target=COLLAB_AGENT, scope="agent")

    @pytest.mark.parametrize("role", ["editor", "manager"])
    def test_editor_tier_allowed(self, delegation_env, role):
        authz = _spawn(_real_user(COLLAB_AGENT, role), target=COLLAB_AGENT, scope="agent")
        assert authz.scope == "agent"
        assert authz.created_by == "user-alice"
        # Collaborative target + real creator: the creator owns the worker.
        assert authz.chat_owner == "user-alice"

    def test_platform_admin_allowed(self, delegation_env):
        authz = _spawn(_admin(), target=COLLAB_AGENT, scope="agent")
        assert authz.created_by == "user-root"

    def test_no_user_session_own_agent_allowed(self, delegation_env):
        authz = _spawn(_no_user_session(COLLAB_AGENT), target=COLLAB_AGENT, scope="agent")
        assert authz.created_by == COLLAB_AGENT
        assert authz.acting_sub is None
        # No per-user dir to hang the worker on → shared pool.
        assert authz.chat_owner == f"agent::{COLLAB_AGENT}"

    def test_personal_only_target_clamps_to_user(self, delegation_env):
        authz = _spawn(_real_user(PERSONAL_ONLY, "viewer"),
                       target=PERSONAL_ONLY, scope="agent")
        assert authz.scope == "user"
        assert "clamped" in authz.scope_note
        assert authz.chat_owner == "user-alice"

    def test_personal_only_target_no_user_denied(self, delegation_env):
        _denied(_no_user_session(PERSONAL_ONLY), 403,
                target=PERSONAL_ONLY, scope="agent")


# ───────────────────────────────────────────────────────────────────────────
# Cross-agent
# ───────────────────────────────────────────────────────────────────────────


class TestCrossAgent:
    def test_roster_target_allowed(self, delegation_env):
        agent_store.set_delegation_targets(COLLAB_USER, [COLLAB_AGENT])
        caller = _no_user_session(COLLAB_USER)
        authz = _spawn(caller, target=COLLAB_AGENT, scope="agent")
        assert authz.source_agent == COLLAB_USER
        assert authz.created_by == COLLAB_USER

    def test_off_roster_denied(self, delegation_env):
        _denied(_no_user_session(COLLAB_USER), 403,
                target=COLLAB_AGENT, scope="agent")

    def test_real_user_needs_target_access(self, delegation_env):
        agent_store.set_delegation_targets(COLLAB_USER, [COLLAB_AGENT])
        # Session minted on collab-user for a user with NO access to the target.
        caller = UserContext(
            sub="user-alice", email="a@t", name="A", role="member",
            is_api_key=True, session_id="s1", agent=COLLAB_USER,
            agents=[COLLAB_USER], agent_roles={COLLAB_USER: "editor"},
        )
        _denied(caller, 403, target=COLLAB_AGENT, scope="user")

    def test_unknown_target_404(self, delegation_env):
        _denied(_admin(), 404, target="no-such-agent")

    def test_source_agent_is_token_authoritative(self, delegation_env):
        # A session on collab-user cannot claim a different source to reach an
        # off-roster target.
        caller = _no_user_session(COLLAB_USER)
        with pytest.raises(HTTPException) as exc:
            authorize_spawn(caller, target_agent=COLLAB_AGENT,
                            requested_scope="agent", source_agent=COLLAB_AGENT)
        assert exc.value.status_code == 403


# ───────────────────────────────────────────────────────────────────────────
# Per-creator spawn cap
# ───────────────────────────────────────────────────────────────────────────


class TestSpawnCap:
    def test_cap_denies_before_any_row(self, delegation_env):
        mcp_store.set_mcp_config_values("delegation-mcp", {"MAX_PARALLEL_SPAWNS": "2"})
        _add_active_run("user-alice")
        _add_active_run("user-alice")
        exc = _denied(_real_user(COLLAB_USER, "editor"), 403)
        assert "Delegation limit reached" in exc.detail

    def test_finished_runs_do_not_count(self, delegation_env):
        mcp_store.set_mcp_config_values("delegation-mcp", {"MAX_PARALLEL_SPAWNS": "1"})
        _add_active_run("user-alice")
        runs = task_store.list_runs(created_by="user-alice")
        task_store.update_run(runs[0]["id"], status="completed")
        assert _spawn(_real_user(COLLAB_USER, "editor")).created_by == "user-alice"

    def test_other_creators_do_not_count(self, delegation_env):
        mcp_store.set_mcp_config_values("delegation-mcp", {"MAX_PARALLEL_SPAWNS": "1"})
        _add_active_run("user-bob")
        assert _spawn(_real_user(COLLAB_USER, "editor")).created_by == "user-alice"

    def test_default_cap_is_four(self, delegation_env):
        for _ in range(4):
            _add_active_run("user-alice")
        _denied(_real_user(COLLAB_USER, "editor"), 403)

    def test_invalid_config_value_falls_back(self, delegation_env):
        mcp_store.set_mcp_config_values("delegation-mcp", {"MAX_PARALLEL_SPAWNS": "lots"})
        assert _spawn(_real_user(COLLAB_USER, "editor")).scope == "user"


# ───────────────────────────────────────────────────────────────────────────
# Invalid input
# ───────────────────────────────────────────────────────────────────────────


def test_invalid_scope_400(delegation_env):
    _denied(_admin(), 400, scope="global")


class TestValidateSpawnOverrides:
    """delegate() model/layer override validation — the accepted set must be
    exactly what the delegation roster advertises (enabled models only)."""

    def _agent(self, monkeypatch, path="claude-code-cli", extras=""):
        from services.delegation import spawn_authz
        monkeypatch.setattr(
            spawn_authz.agent_store, "get_agent",
            lambda slug: {"execution_path": path, "execution_paths": extras})

    def _models(self, monkeypatch, rows):
        from storage import subscription_store
        monkeypatch.setattr(subscription_store, "list_models",
                            lambda layer=None: rows)

    def test_enabled_model_accepted(self, monkeypatch):
        from services.delegation.spawn_authz import validate_spawn_overrides
        self._agent(monkeypatch)
        self._models(monkeypatch, [{"model_id": "m1", "enabled": True}])
        validate_spawn_overrides("w", None, "m1")  # no raise

    def test_disabled_model_rejected(self, monkeypatch):
        from fastapi import HTTPException
        from services.delegation.spawn_authz import validate_spawn_overrides
        self._agent(monkeypatch)
        self._models(monkeypatch, [{"model_id": "m1", "enabled": False}])
        with pytest.raises(HTTPException):
            validate_spawn_overrides("w", None, "m1")

    def test_unknown_model_rejected(self, monkeypatch):
        from fastapi import HTTPException
        from services.delegation.spawn_authz import validate_spawn_overrides
        self._agent(monkeypatch)
        self._models(monkeypatch, [{"model_id": "m1", "enabled": True}])
        with pytest.raises(HTTPException):
            validate_spawn_overrides("w", None, "m-nope")

    def test_registry_error_fails_open(self, monkeypatch):
        from services.delegation.spawn_authz import validate_spawn_overrides
        from storage import subscription_store

        def _boom(layer=None):
            raise RuntimeError("db down")

        self._agent(monkeypatch)
        monkeypatch.setattr(subscription_store, "list_models", _boom)
        validate_spawn_overrides("w", None, "m1")  # no raise

    def test_disallowed_layer_rejected(self, monkeypatch):
        from fastapi import HTTPException
        from services.delegation.spawn_authz import validate_spawn_overrides
        self._agent(monkeypatch, path="claude-code-cli")
        with pytest.raises(HTTPException):
            validate_spawn_overrides("w", "codex-cli", None)
