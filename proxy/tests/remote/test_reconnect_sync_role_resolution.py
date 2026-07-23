"""Machine-scope sync identity: (username, role) resolves from the PAIRING.

Pins the resolution `resolve_machine_sync_identity` must produce:
  * admin-PAIRED machine  → admin-shared (target_username=None); owned by a
    platform admin → target_role="admin" on every agent;
  * user-paired machine   → the owner's username + the owner's PER-AGENT
    role — for EVERYONE, platform admins included.

The last rule is regression coverage for the prompt-deletion incident: a
platform admin who was per-agent VIEWER used to get target_role="admin" on
their personal machine, granting config/ push + write-back — so a satellite
that lost its copy delete-attributed the agent's prompt at sync time.
`_machine_sync_role` (the session-start path's equivalent) is pinned here
too: same pairing-derived authority, session role only on admin-shared
machines.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.remote.remote_execution import RemoteExecutionLayer  # noqa: E402


def _layer_with_capture():
    """A layer whose _initial_workspace_sync just records (agent, username, role)."""
    layer = RemoteExecutionLayer(MagicMock())
    calls = []

    async def _fake_sync(machine_id, agent_slug, *, target_username=None, target_role=""):
        calls.append((agent_slug, target_username, target_role))

    layer._initial_workspace_sync = _fake_sync
    return layer, calls


@pytest.mark.asyncio
async def test_user_paired_resolves_owner_and_per_agent_role():
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-alice", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "user"}), \
         patch("storage.database.get_username_by_sub", return_value="alice"), \
         patch("storage.database.get_user_agent_roles",
               return_value={"agent-1": "editor", "agent-2": "viewer"}), \
         patch("storage.sync_state_store.agents_for_machine",
               return_value={"agent-1", "agent-2"}):
        await layer.sync_all_agents_on_reconnect("m1")
    by_agent = {a: (u, r) for a, u, r in calls}
    assert by_agent["agent-1"] == ("alice", "editor")
    assert by_agent["agent-2"] == ("alice", "viewer")


@pytest.mark.asyncio
async def test_admin_paired_resolves_admin_shared():
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-admin", "pairing_scope": "admin"}), \
         patch("storage.database.get_user", return_value={"role": "admin"}), \
         patch("storage.database.get_username_by_sub", return_value="adminuser"), \
         patch("storage.database.get_user_agent_roles", return_value={}), \
         patch("storage.sync_state_store.agents_for_machine", return_value={"agent-1"}):
        await layer.sync_all_agents_on_reconnect("m1")
    # admin-PAIRED → no per-user filter (None); platform-admin owner → role "admin".
    assert calls == [("agent-1", None, "admin")]


@pytest.mark.asyncio
async def test_platform_admin_owner_user_paired_uses_per_agent_role():
    # A platform admin's OWN (user-paired) machine: scoped to them by username,
    # with their PER-AGENT role — the platform role never inflates machine-scope
    # sync authority (the prompt-deletion incident: role "admin" here granted a
    # per-agent VIEWER's machine config/ write-back).
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-admin", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "admin"}), \
         patch("storage.database.get_username_by_sub", return_value="adminuser"), \
         patch("storage.database.get_user_agent_roles",
               return_value={"agent-1": "viewer"}), \
         patch("storage.sync_state_store.agents_for_machine", return_value={"agent-1"}):
        await layer.sync_all_agents_on_reconnect("m1")
    assert calls == [("agent-1", "adminuser", "viewer")]


@pytest.mark.asyncio
async def test_platform_admin_owner_without_explicit_role_fails_closed():
    # No explicit per-agent role → "" (viewer-equivalent sync: personal dirs
    # only), NOT "admin".
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-admin", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "admin"}), \
         patch("storage.database.get_username_by_sub", return_value="adminuser"), \
         patch("storage.database.get_user_agent_roles", return_value={}), \
         patch("storage.sync_state_store.agents_for_machine", return_value={"agent-1"}):
        await layer.sync_all_agents_on_reconnect("m1")
    assert calls == [("agent-1", "adminuser", "")]


# --- _machine_sync_role (session-start path's machine-scope role) ---


def test_session_role_kept_on_admin_paired_machine():
    from core.remote.remote_execution import _machine_sync_role
    machine = {"registered_by": "sub-x", "pairing_scope": "admin"}
    assert _machine_sync_role(machine, "agent-1", "editor") == "editor"
    assert _machine_sync_role(None, "agent-1", "admin") == "admin"


def test_session_role_replaced_by_per_agent_role_on_user_paired():
    from core.remote.remote_execution import _machine_sync_role
    machine = {"registered_by": "sub-admin", "pairing_scope": "user"}
    with patch("storage.database.get_user_agent_roles",
               return_value={"agent-1": "viewer"}):
        # The session says "admin" (platform-inflated) — the machine syncs
        # as the owner's per-agent viewer role.
        assert _machine_sync_role(machine, "agent-1", "admin") == "viewer"
        assert _machine_sync_role(machine, "other-agent", "admin") == ""


def test_user_paired_without_owner_fails_closed():
    from core.remote.remote_execution import _machine_sync_role
    machine = {"registered_by": "", "pairing_scope": "user"}
    assert _machine_sync_role(machine, "agent-1", "admin") == ""


@pytest.mark.asyncio
async def test_no_synced_agents_is_noop():
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "s", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "user"}), \
         patch("storage.database.get_username_by_sub", return_value="u"), \
         patch("storage.sync_state_store.agents_for_machine", return_value=set()):
        await layer.sync_all_agents_on_reconnect("m1")
    assert calls == []
