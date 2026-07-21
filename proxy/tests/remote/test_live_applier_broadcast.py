"""The live satellite→platform applier refreshes open dashboards.

`_apply_file_changed` writes a satellite-reported change to the platform agent
tree, then must emit a `file_updated` event so an open dashboard workspace view
refetches its file tree. Without it, a file an agent/MCP
creates mid-session (e.g. a transcribe `.srt`) syncs to disk but never appears in
the dashboard until a manual reload — the bug this closes.

Both a write and a delete broadcast (a write adds/updates the file in the
refetched tree, a delete makes it vanish). The satellite's own user is NOT
excluded — they are the one watching the dashboard for the change. A write-back
the role guard DENIES, or one with no session context, must NOT broadcast (the
guard returns before anything touches the platform tree).
"""

import base64
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))


async def _run(role, username, path, action="write"):
    """Drive `_apply_file_changed` with a stubbed session ctx and every external
    side-effect patched (DB-free), returning the broadcast_file_updated mock."""
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    msg = {
        "agent_slug": "my-agent",
        "path": path,
        "action": action,
        "session_id": "sess-1",
        "hash": "sha256:abc",
        "content_b64": base64.b64encode(b"x").decode() if action == "write" else "",
    }
    sec = SimpleNamespace(role=role, username=username, agent="my-agent")
    bcast = AsyncMock()

    with patch("core.session.session_state.get_session_security", return_value=sec), \
         patch.object(cm, "_apply_file_changed_inner", new=AsyncMock()), \
         patch.object(cm, "_capture_pre_overwrite",
                      new=AsyncMock(return_value=(None, None))), \
         patch.object(cm, "_mtime_of", return_value=0.0), \
         patch("storage.sync_state_store.get_one", return_value=None), \
         patch("storage.sync_state_store.record_one"), \
         patch("storage.sync_state_store.clear_one"), \
         patch("storage.file_author_store.record"), \
         patch("storage.file_author_store.clear"), \
         patch("storage.file_tombstones_store.record"), \
         patch("services.remote.workspace_fanout.fanout_targets", return_value=[]), \
         patch("services.remote.workspace_fanout.fan_out_delete", new=AsyncMock()), \
         patch("services.notifications.notification_manager.broadcast_file_updated", new=bcast):
        await cm._apply_file_changed("machine-1", msg)
    return bcast


@pytest.mark.asyncio
async def test_write_broadcasts_file_updated_to_dashboard():
    # viewer writing their OWN user dir is authorized → applies → broadcasts.
    bcast = await _run("viewer", "alice", "users/alice/workspace/sub.srt")
    bcast.assert_awaited_once()
    args, kwargs = bcast.await_args
    assert args[0] == "my-agent"
    assert args[1] == "users/alice/workspace/sub.srt"
    assert kwargs.get("source") == "disk"


@pytest.mark.asyncio
async def test_delete_broadcasts_file_updated_to_dashboard():
    # A live delete also refreshes the tree (the file vanishes on refetch).
    bcast = await _run("manager", "alice", "workspace/gone.md", action="delete")
    bcast.assert_awaited_once()
    args, kwargs = bcast.await_args
    assert args[1] == "workspace/gone.md"
    assert kwargs.get("source") == "disk"


@pytest.mark.asyncio
async def test_denied_writeback_does_not_broadcast():
    # A viewer cannot write the SHARED workspace → guard returns before apply,
    # nothing changed on the platform tree → no dashboard refresh.
    bcast = await _run("viewer", "alice", "workspace/out.md")
    bcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_session_ctx_does_not_broadcast():
    # Fail-closed: no authenticated security context → drop, never broadcast.
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    msg = {
        "agent_slug": "my-agent", "path": "users/alice/workspace/x.srt",
        "action": "write", "session_id": "sess-x",
        "content_b64": base64.b64encode(b"x").decode(),
    }
    bcast = AsyncMock()
    with patch("core.session.session_state.get_session_security", return_value=None), \
         patch("services.notifications.notification_manager.broadcast_file_updated", new=bcast):
        await cm._apply_file_changed("machine-1", msg)
    bcast.assert_not_awaited()
