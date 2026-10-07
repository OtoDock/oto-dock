"""Part 2 (Option A): deferred large-pull warmup offload.

`_partition_deferred_pulls` decides which initial-sync merge actions run in the
background — ONLY large, conflict-free PULLS; everything else (all pushes,
deletes, scrubs, noops, conflict-capture pulls, small pulls) stays foreground.
`_run_deferred_pulls` applies the deferred pulls off the warmup path (pull +
base/author/tombstone bookkeeping + dashboard refresh), mirroring the foreground
pull branch minus capture/notify.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.remote.remote_execution import (
    _DEFER_PULL_MIN_BYTES,
    RemoteExecutionLayer,
    _partition_deferred_pulls,
)
from core.remote.file_sync import FileAction

BIG = _DEFER_PULL_MIN_BYTES + 1
SMALL = _DEFER_PULL_MIN_BYTES - 1


def test_partition_defers_only_large_conflict_free_pulls():
    big_pull = FileAction("workspace/big.bin", "pull", base_hash="s1")
    small_pull = FileAction("workspace/small.txt", "pull", base_hash="s2")
    big_push = FileAction("workspace/big_push.bin", "push", base_hash="p1")
    conflict_pull = FileAction(
        "workspace/conf.bin", "pull", base_hash="s3", capture_side="platform",
    )
    delete = FileAction("workspace/gone.txt", "delete_satellite", clear_base=True)
    noop = FileAction("workspace/same.txt", "noop", base_hash="s4")
    actions = [big_pull, small_pull, big_push, conflict_pull, delete, noop]
    remote_size = {
        "workspace/big.bin": BIG,
        "workspace/small.txt": SMALL,
        "workspace/big_push.bin": BIG,   # a push is never deferred, size irrelevant
        "workspace/conf.bin": BIG,       # conflict-capture pull stays foreground
    }

    fg, deferred = _partition_deferred_pulls(actions, remote_size)

    assert deferred == [big_pull]
    # Foreground keeps everything else, in original order, nothing lost/duplicated.
    assert fg == [small_pull, big_push, conflict_pull, delete, noop]
    assert len(fg) + len(deferred) == len(actions)


def test_partition_no_deferrals_returns_all_foreground_as_copy():
    actions = [
        FileAction("workspace/a.txt", "push", base_hash="p"),
        FileAction("workspace/b.txt", "pull", base_hash="s"),  # size 0 → small
    ]
    fg, deferred = _partition_deferred_pulls(actions, {})
    assert deferred == []
    assert fg == actions
    assert fg is not actions  # a copy, so the caller can't mutate plan.actions


@pytest.mark.asyncio
async def test_run_deferred_pulls_applies_and_broadcasts(tmp_path):
    cm = MagicMock()
    cm.pull_file_to_path = AsyncMock(return_value=True)
    layer = RemoteExecutionLayer(cm)
    action = FileAction(
        "workspace/big.bin", "pull", base_hash="sha256:s", drop_tombstone=True,
    )

    with patch("core.remote.remote_workspace_sync.logger"), \
         patch("config.AGENTS_DIR", tmp_path), \
         patch("core.remote.remote_file_flow._acquire_global_path_lock",
               new=AsyncMock(return_value=asyncio.Lock())), \
         patch("storage.files.sync_state_store.record_one") as rec, \
         patch("storage.files.file_author_store.record") as auth, \
         patch("storage.files.file_tombstones_store.drop") as drop, \
         patch("services.notifications.notification_manager.broadcast_file_updated",
               new=AsyncMock()) as bcast:
        await layer._run_deferred_pulls("m1", "agent-1", [action], "alice")

    cm.pull_file_to_path.assert_awaited_once()
    rec.assert_called_once()                      # base advanced
    assert rec.call_args.args[3] == "sha256:s"    # with the action's base_hash
    auth.assert_called_once()                      # author recorded (satellite_user truthy)
    drop.assert_called_once()                       # tombstone dropped
    bcast.assert_awaited_once()                     # dashboard refresh fired


@pytest.mark.asyncio
async def test_run_deferred_pulls_failure_skips_bookkeeping_and_broadcast(tmp_path):
    cm = MagicMock()
    cm.pull_file_to_path = AsyncMock(return_value=False)  # pull failed / WS dropped
    layer = RemoteExecutionLayer(cm)
    action = FileAction(
        "workspace/big.bin", "pull", base_hash="sha256:s", drop_tombstone=True,
    )

    with patch("core.remote.remote_workspace_sync.logger"), \
         patch("config.AGENTS_DIR", tmp_path), \
         patch("core.remote.remote_file_flow._acquire_global_path_lock",
               new=AsyncMock(return_value=asyncio.Lock())), \
         patch("storage.files.sync_state_store.record_one") as rec, \
         patch("services.notifications.notification_manager.broadcast_file_updated",
               new=AsyncMock()) as bcast:
        await layer._run_deferred_pulls("m1", "agent-1", [action], "alice")

    cm.pull_file_to_path.assert_awaited_once()
    rec.assert_not_called()       # no base advance on a failed pull
    bcast.assert_not_awaited()    # no dashboard refresh for a file that didn't land


@pytest.mark.asyncio
async def test_a_deferred_pull_is_handed_the_named_path(tmp_path):
    """The destination is the path as named, never where an in-tree link
    on the way points, so the pull opens it by components and refuses the
    link; a path that is not canonical is never pulled."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.infra import safe_fs
    ws = tmp_path / "agent-1" / "workspace"
    (ws / "real").mkdir(parents=True)
    (ws / "alias").symlink_to("real")
    cm = MagicMock()
    cm.pull_file_to_path = AsyncMock(return_value=False)
    layer = RemoteExecutionLayer(cm)
    actions = [
        FileAction("workspace/real/../../config/x.md", "pull", base_hash="sha256:s"),
        FileAction("workspace/alias/big.bin", "pull", base_hash="sha256:s"),
    ]

    with patch("core.remote.remote_workspace_sync.logger"), \
         patch("config.AGENTS_DIR", tmp_path), \
         patch("core.remote.remote_file_flow._acquire_global_path_lock",
               new=AsyncMock(return_value=asyncio.Lock())):
        await layer._run_deferred_pulls("m1", "agent-1", actions, "alice")
        cm.pull_file_to_path.assert_awaited_once()
        dest = cm.pull_file_to_path.await_args.args[2]
        assert dest == ws / "alias" / "big.bin"
        with pytest.raises(safe_fs.SymlinkRefused):
            SatelliteConnectionManager._open_pull_root(dest)
