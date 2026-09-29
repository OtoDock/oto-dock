"""Tests for role-aware resolve_execution_target.

Covers the viewer-on-admin-remote security guard + fallback flag matrix.
"""

from unittest.mock import patch


def test_viewer_forced_to_local_when_agent_has_remote_target(temp_db):
    """A viewer's session on an agent with an admin-configured remote target
    must route to local — no bwrap isolation on remote, so viewer-scope file
    access can't be enforced. Viewer's own user-override is unaffected (see
    test_viewer_own_override_is_honored)."""
    from storage.remote_store import resolve_execution_target

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        target, reason = resolve_execution_target(
            "test-agent", user_sub=None, role="viewer",
        )
        assert target == "local"
        assert reason == "viewer-on-admin-remote"


def test_contributor_forced_to_local_like_a_viewer(temp_db):
    """The workspace tier is below manager: a contributor's chat on an
    admin-targeted agent runs locally too, and the reason travels with the
    target so the badge can say so."""
    from storage.remote_store import resolve_execution_target

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        target, reason = resolve_execution_target(
            "test-agent", user_sub="pm-1", role="contributor",
        )
        assert target == "local"
        assert reason == "viewer-on-admin-remote"


def test_manager_uses_agent_remote_target_when_online(temp_db):
    """Manager on agent with remote target — no fallback when online."""
    from storage.remote_store import resolve_execution_target

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        target, reason = resolve_execution_target(
            "test-agent", user_sub=None, role="manager",
        )
        assert target == "machine-admin"
        assert reason is None


def test_viewer_own_override_is_honored(temp_db):
    """Viewer's user-level override applies even though admin-remote is
    forbidden — viewer owns their hardware, they accept the trade-off."""
    from storage import remote_store

    with patch.object(remote_store, "get_user_remote_target",
                      return_value={"machine_id": "machine-mine"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        target, reason = remote_store.resolve_execution_target(
            "test-agent", user_sub="viewer-1", role="viewer",
        )
        assert target == "machine-mine"
        assert reason is None


def test_agent_default_offline_hard_fail(temp_db):
    """Agent-default target offline, fallback-agent-default=false (default) →
    sentinel that warmup detects and rejects."""
    from storage import remote_store
    from storage import database as db

    db.set_platform_setting("remote_fallback_agent_default", "0")

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=False):
        target, reason = remote_store.resolve_execution_target(
            "test-agent", user_sub=None, role="manager",
        )
        assert target.startswith("__offline__:")
        assert reason == "agent-default-offline-hard-fail"


def test_agent_default_offline_fallback_to_local(temp_db):
    """Same scenario but with remote_fallback_agent_default=true."""
    from storage import remote_store
    from storage import database as db

    db.set_platform_setting("remote_fallback_agent_default", "1")

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=False):
        target, reason = remote_store.resolve_execution_target(
            "test-agent", user_sub=None, role="manager",
        )
        assert target == "local"
        assert reason == "agent-default-offline"


def test_user_override_offline_soft_fallback_to_agent_default(temp_db):
    """User's own machine offline + agent default online → runs on agent
    default with fallback_reason='user-override-offline' (default behavior)."""
    from storage import remote_store

    # machine-mine offline; machine-admin online
    reachable_map = {"machine-mine": False, "machine-admin": True}
    with patch.object(remote_store, "get_user_remote_target",
                      return_value={"machine_id": "machine-mine"}), \
         patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable",
               side_effect=lambda mid: reachable_map.get(mid, False)):
        target, reason = remote_store.resolve_execution_target(
            "test-agent", user_sub="user-1", role="manager",
        )
        assert target == "machine-admin"
        assert reason == "user-override-offline"


def test_user_override_offline_hard_fail_when_fallback_disabled(temp_db):
    """User's machine offline and fallback disabled → sentinel."""
    from storage import remote_store
    from storage import database as db

    db.set_platform_setting("remote_fallback_user_override", "0")
    with patch.object(remote_store, "get_user_remote_target",
                      return_value={"machine_id": "machine-mine"}), \
         patch("services.remote.remote_status.is_reachable", return_value=False):
        target, reason = remote_store.resolve_execution_target(
            "test-agent", user_sub="user-1", role="manager",
        )
        assert target.startswith("__offline__:")
        assert reason == "user-override-offline-hard-fail"
