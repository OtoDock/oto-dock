"""Sync-performance W1-W3 (2026-07-19): windowed pipelined pushes, progress
ticks, and the enable-click pre-sync.

W1: the initial sync used to await each per-file ack before SENDING the next —
RTT × N wire serialization (the 25-minute first-remote-turn report). The
windowed apply keeps ≤8 actions in flight; per-path ordering stays with the
global path lock inside _apply and failures still log-and-continue.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.remote.remote_execution import RemoteExecutionLayer  # noqa: E402


def _push_action(rp: str):
    return SimpleNamespace(
        op="push", rel_path=rp, capture_side="", capture_reason="",
        notify_user="", base_hash="", drop_tombstone=False, clear_base=False,
    )


def _make_cm(push_delays: dict):
    """Fake connection manager: push_file resolves after a per-file delay so
    acks return OUT OF ORDER; records the max in-flight concurrency."""
    cm = MagicMock()
    state = {"active": 0, "max_active": 0, "pushed": []}

    @asynccontextmanager
    async def _lock(*a, **kw):
        yield

    cm.get_sync_lock = _lock
    cm.get_clock_offset = MagicMock(return_value=0.0)
    cm.send_command = AsyncMock(return_value={"files": []})

    async def _push_file(machine_id, ref, content, agent_slug=""):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(push_delays.get(ref.value, 0.01))
        state["active"] -= 1
        state["pushed"].append(ref.value)
        return True

    cm.push_file = _push_file
    return cm, state


async def _run_sync(tmp_path, monkeypatch, actions, cm, progress_cb=None):
    import config as _cfg
    from core.remote import file_sync
    from core.session import visibility
    from storage import sync_state_store, file_tombstones_store

    agent_dir = tmp_path / "test-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for a in actions:
        p = agent_dir / a.rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"content-" + a.rel_path.encode())

    monkeypatch.setattr(_cfg, "AGENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(visibility, "is_shared_only", lambda slug: False)
    monkeypatch.setattr(file_sync, "compute_manifest",
                        lambda *a, **kw: [])
    monkeypatch.setattr(sync_state_store, "load_for_machine_agent",
                        lambda *a: {})
    monkeypatch.setattr(file_tombstones_store, "load_for_agent", lambda *a: {})
    monkeypatch.setattr(
        file_sync, "diff_manifests",
        lambda *a, **kw: SimpleNamespace(actions=actions, to_scrub=[]),
    )

    layer = RemoteExecutionLayer(cm)
    await layer._initial_workspace_sync(
        "machine-1", "test-agent", target_username=None, target_role="admin",
        progress_cb=progress_cb,
    )


@pytest.mark.asyncio
async def test_windowed_pushes_pipeline_and_all_complete(tmp_path, monkeypatch):
    # Reverse-sorted delays → the FIRST-sent pushes ack LAST. All must still
    # complete, and >1 must have been in flight at once (the window works).
    actions = [_push_action(f"workspace/f{i}.txt") for i in range(20)]
    delays = {a.rel_path: 0.001 * (20 - i) for i, a in enumerate(actions)}
    cm, state = _make_cm(delays)
    ticks = []

    async def _progress(done, total):
        ticks.append((done, total))

    await _run_sync(tmp_path, monkeypatch, actions, cm, progress_cb=_progress)

    assert sorted(state["pushed"]) == sorted(a.rel_path for a in actions)
    assert state["max_active"] > 1, "pushes never overlapped — window inert"
    assert state["max_active"] <= 8, "window bound exceeded"
    # Final tick always lands on (total, total) even under throttling.
    assert ticks and ticks[-1] == (20, 20)


@pytest.mark.asyncio
async def test_failed_push_logs_and_continues(tmp_path, monkeypatch):
    actions = [_push_action(f"workspace/g{i}.txt") for i in range(6)]
    cm, state = _make_cm({})
    real_push = cm.push_file

    async def _flaky(machine_id, ref, content, agent_slug=""):
        if ref.value.endswith("g2.txt"):
            return False  # ack'd failure — must not abort the rest
        if ref.value.endswith("g3.txt"):
            raise RuntimeError("wire dropped")  # raised failure — same
        return await real_push(machine_id, ref, content, agent_slug=agent_slug)

    cm.push_file = _flaky
    await _run_sync(tmp_path, monkeypatch, actions, cm)
    assert sorted(state["pushed"]) == sorted(
        a.rel_path for a in actions
        if not a.rel_path.endswith(("g2.txt", "g3.txt"))
    )


@pytest.mark.asyncio
async def test_presync_machine_agent_offline_noop():
    cm = MagicMock()
    cm.is_connected = MagicMock(return_value=False)
    layer = RemoteExecutionLayer(cm)
    layer._initial_workspace_sync = AsyncMock()
    await layer.presync_machine_agent("m1", "agent-x")
    layer._initial_workspace_sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_presync_machine_agent_runs_with_pairing_identity():
    cm = MagicMock()
    cm.is_connected = MagicMock(return_value=True)
    layer = RemoteExecutionLayer(cm)
    layer.resolve_machine_sync_identity = AsyncMock(return_value=("alice", "editor"))
    layer._initial_workspace_sync = AsyncMock()
    await layer.presync_machine_agent("m1", "agent-x")
    layer._initial_workspace_sync.assert_awaited_once_with(
        "m1", "agent-x", target_username="alice", target_role="editor",
    )
