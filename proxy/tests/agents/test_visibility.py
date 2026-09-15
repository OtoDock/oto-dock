"""Tests for the visibility-mode resolver (``core/session/visibility.py``).

Exercises every cell of the 2×2 mode model × role × session type (chat / task
re-warm / service) and asserts the full resolution: mount scope/username,
available scopes, config visibility, memory availability, effective default
scope, and the chat-history owner.
"""

from __future__ import annotations

from core.session.visibility import resolve_visibility
from storage.agents import agent_store


# Four agents, one per mode.
def _seed_modes():
    agent_store.create_agent("ps", "PS", collaborative=True, default_scope="user")
    agent_store.create_agent("sp", "SP", collaborative=True, default_scope="agent")
    agent_store.create_agent("po", "PO", collaborative=False, default_scope="user")
    agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")


# ---------------------------------------------------------------------------
# Mode → available_scopes / mount_shared (agent-level, session-independent)
# ---------------------------------------------------------------------------

def test_available_scopes_per_mode(temp_db):
    _seed_modes()
    assert resolve_visibility("ps").available_scopes == ("user", "agent")
    assert resolve_visibility("sp").available_scopes == ("user", "agent")
    assert resolve_visibility("po").available_scopes == ("user",)
    assert resolve_visibility("so").available_scopes == ("agent",)
    assert resolve_visibility("ps").mount_shared is True
    assert resolve_visibility("po").mount_shared is False   # personal-only: no shared
    assert resolve_visibility("so").mount_shared is True


def test_mode_labels(temp_db):
    _seed_modes()
    assert resolve_visibility("ps").mode == "personal_shared"
    assert resolve_visibility("sp").mode == "shared_personal"
    assert resolve_visibility("po").mode == "personal_only"
    assert resolve_visibility("so").mode == "shared_only"


# ---------------------------------------------------------------------------
# Human chats — mount scope + config_visible per mode/role
# ---------------------------------------------------------------------------

def test_collaborative_manager_chat_mounts_user_scope(temp_db):
    _seed_modes()
    for slug in ("ps", "sp"):
        v = resolve_visibility(slug, username="alice", user_role="manager", user_sub="alice-sub")
        assert v.mount_scope == "user"
        assert v.mount_username == "alice"
        assert v.config_visible is True
        assert v.memory_user_enabled and v.memory_agent_enabled
        assert v.history_owner == "alice-sub"
        assert v.is_service is False
    # default scope arg differs: personal-leaning vs operational
    assert resolve_visibility("ps", username="a", user_role="manager").effective_default_scope == "user"
    assert resolve_visibility("sp", username="a", user_role="manager").effective_default_scope == "agent"


def test_personal_only_chat(temp_db):
    _seed_modes()
    v = resolve_visibility("po", username="alice", user_role="manager", user_sub="alice-sub")
    assert v.mount_scope == "user"
    assert v.mount_username == "alice"
    assert v.mount_shared is False          # NO shared workspace/knowledge
    assert v.config_visible is True         # manager still curates config
    assert v.memory_user_enabled is True
    assert v.memory_agent_enabled is False  # no agent memory in personal-only
    assert v.effective_default_scope == "user"
    assert v.history_owner == "alice-sub"   # per-user history


def test_shared_only_manager_chat_decouples_mount_from_user(temp_db):
    _seed_modes()
    v = resolve_visibility("so", username="alice", user_role="manager", user_sub="alice-sub")
    # The decouple: a human chats, but the MOUNT is agent-scope.
    assert v.mount_scope == "agent"
    assert v.mount_username == ""
    assert v.mount_shared is True
    assert v.config_visible is True         # owner-tier human → /config despite agent mount
    assert v.memory_user_enabled is False   # no user memory in shared-only
    assert v.memory_agent_enabled is True
    assert v.effective_default_scope == "agent"
    assert v.is_service is False            # there IS a human (attribution)
    # ONE shared history for every assigned user.
    assert v.history_owner == "agent::so"


def test_shared_only_viewer_chat_is_read_only(temp_db):
    _seed_modes()
    v = resolve_visibility("so", username="bob", user_role="viewer", user_sub="bob-sub")
    assert v.mount_scope == "agent"
    assert v.config_visible is False        # viewer: no config / knowledge RW
    assert v.effective_default_scope == "agent"   # user not available → can't clamp to user
    assert v.history_owner == "agent::so"   # same shared list as the manager


def test_two_users_share_one_shared_only_history(temp_db):
    _seed_modes()
    a = resolve_visibility("so", username="alice", user_role="manager", user_sub="alice-sub")
    b = resolve_visibility("so", username="bob", user_role="viewer", user_sub="bob-sub")
    assert a.history_owner == b.history_owner == "agent::so"


def test_personal_only_viewer_chat(temp_db):
    _seed_modes()
    v = resolve_visibility("po", username="bob", user_role="viewer", user_sub="bob-sub")
    assert v.mount_scope == "user"
    assert v.config_visible is False
    assert v.available_scopes == ("user",)
    assert v.effective_default_scope == "user"


# ---------------------------------------------------------------------------
# Service sessions (no human) — every mode mounts agent-scope, no config
# ---------------------------------------------------------------------------

def test_service_session_is_agent_scope_no_config(temp_db):
    _seed_modes()
    for slug in ("ps", "sp", "so"):
        v = resolve_visibility(slug, username="", user_role="manager")
        assert v.mount_scope == "agent"
        assert v.mount_username == ""
        assert v.config_visible is False    # admin-only-task /config regression guard
        assert v.is_service is True
        assert v.effective_default_scope == "agent"


def test_service_session_shared_only_history_is_shared(temp_db):
    _seed_modes()
    v = resolve_visibility("so", username="", user_role="manager", user_sub="")
    assert v.history_owner == "agent::so"


# ---------------------------------------------------------------------------
# Task re-warm — scope_override honored, clamped to the mode
# ---------------------------------------------------------------------------

def test_task_rewarm_honors_scope_override(temp_db):
    _seed_modes()
    # A collaborative agent's agent-scope task keeps the agent mount even though
    # a human (admin) re-opened it.
    v = resolve_visibility("ps", username="", user_role="manager", scope_override="agent")
    assert v.mount_scope == "agent"
    assert v.config_visible is False
    # A user-scope task re-warm keeps the user mount.
    v2 = resolve_visibility("ps", username="alice", user_role="manager", scope_override="user")
    assert v2.mount_scope == "user"
    assert v2.mount_username == "alice"


def test_scope_override_clamped_to_available(temp_db):
    _seed_modes()
    # A 'user' override on a Shared-only agent (no user scope) clamps to agent.
    v = resolve_visibility("so", username="alice", user_role="manager", scope_override="user")
    assert v.mount_scope == "agent"


def test_user_scope_manager_task_keeps_config(temp_db):
    """A user-scope task by a manager still mounts /config (preserves today)."""
    _seed_modes()
    v = resolve_visibility("ps", username="alice", user_role="manager", scope_override="user")
    assert v.config_visible is True


# ---------------------------------------------------------------------------
# Memory toggle composition
# ---------------------------------------------------------------------------

def test_per_agent_toggle_disables_memory(temp_db):
    from storage.agents import memory_store
    _seed_modes()
    memory_store.set_agent_toggle("ps", "agent_memory_enabled", False)
    v = resolve_visibility("ps", username="alice", user_role="manager")
    assert v.memory_user_enabled is True
    assert v.memory_agent_enabled is False   # toggle wins even though mode offers it


def test_unknown_agent_safe_defaults(temp_db):
    v = resolve_visibility("nonexistent", username="alice", user_role="manager", user_sub="s")
    assert v.collaborative is True
    assert v.available_scopes == ("user", "agent")
    assert v.mount_scope == "user"
    assert v.history_owner == "s"
