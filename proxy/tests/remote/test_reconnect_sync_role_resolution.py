"""Regression: `sync_all_agents_on_reconnect` resolves (username, role) from the
machine PAIRING and must not NameError.

`owner_is_admin` was referenced but never defined — a latent NameError that
silently killed the ENTIRE reconnect catch-up sync (trigger #2 of the workspace
sync model) for any machine that had synced agents (the best-effort caller
swallowed it). These pin the resolution it must produce:
  * admin-PAIRED machine  → admin-shared (target_username=None);
  * platform-admin OWNER  → target_role="admin" on every agent (skip per-agent lookup);
  * normal user-paired    → the owner's username + the owner's per-agent role.
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
async def test_platform_admin_owner_user_paired_skips_per_agent_role():
    # A platform admin's OWN (user-paired) machine: scoped to them by username,
    # but role "admin" on every agent (the per-agent role lookup is skipped).
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-admin", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "admin"}), \
         patch("storage.database.get_username_by_sub", return_value="adminuser"), \
         patch("storage.database.get_user_agent_roles", return_value={}) as roles, \
         patch("storage.sync_state_store.agents_for_machine", return_value={"agent-1"}):
        await layer.sync_all_agents_on_reconnect("m1")
    assert calls == [("agent-1", "adminuser", "admin")]
    roles.assert_not_called()  # platform admin → per-agent role lookup skipped


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
