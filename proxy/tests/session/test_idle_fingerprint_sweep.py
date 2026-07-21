"""Proxy fingerprint-gated idle-sync sweep.

`run_idle_fingerprint_sweep` runs the merge for a CONNECTED-IDLE (machine, agent)
only when the satellite-reported stat-fingerprint changed since the last completed
sync — catching OUT-OF-TURN satellite-side changes. `_idle_fingerprint_sync_one`
runs one merge and advances the synced baseline on success (using the fp that
TRIGGERED the run, so a change mid-merge re-triggers rather than being skipped).
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.remote.remote_execution import RemoteExecutionLayer  # noqa: E402


# --- run_idle_fingerprint_sweep: trigger gating -----------------------------

@pytest.mark.asyncio
async def test_sweep_triggers_only_changed_idle_tracked(monkeypatch):
    cm = MagicMock()
    cm.get_connected_machines.return_value = ["m1"]
    conn = SimpleNamespace(
        agent_fingerprints={
            "a-changed": "fp2",      # changed vs synced → should fire
            "a-unchanged": "fp1",    # == synced → skip
            "a-untracked": "fpX",    # not in sync_state → skip
            "a-active": "fp9",       # changed but has an active session → skip
        },
        synced_fingerprints={"a-changed": "fp1", "a-unchanged": "fp1", "a-active": "fp1"},
    )
    cm.get_connection.return_value = conn
    layer = RemoteExecutionLayer(cm)

    monkeypatch.setattr(
        "storage.sync_state_store.agents_for_machine",
        lambda mid: {"a-changed", "a-unchanged", "a-active"},  # a-untracked absent
    )
    monkeypatch.setattr(
        "services.remote.workspace_fanout._active_machine_ids",
        lambda slug: {"m1"} if slug == "a-active" else set(),
    )

    triggered = []

    async def _fake_sync_one(mid, slug, fp):
        triggered.append((mid, slug, fp))

    monkeypatch.setattr(layer, "_idle_fingerprint_sync_one", _fake_sync_one)

    await layer.run_idle_fingerprint_sweep()
    tasks = list(layer._deferred_sync_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    assert triggered == [("m1", "a-changed", "fp2")]


@pytest.mark.asyncio
async def test_sweep_noop_when_no_fingerprints(monkeypatch):
    cm = MagicMock()
    cm.get_connected_machines.return_value = ["m1"]
    cm.get_connection.return_value = SimpleNamespace(
        agent_fingerprints={}, synced_fingerprints={},
    )
    layer = RemoteExecutionLayer(cm)
    called = {"n": 0}
    monkeypatch.setattr(
        "storage.sync_state_store.agents_for_machine",
        lambda mid: called.__setitem__("n", called["n"] + 1) or {"x"},
    )
    monkeypatch.setattr(layer, "_idle_fingerprint_sync_one", AsyncMock())

    await layer.run_idle_fingerprint_sweep()
    # No fingerprints → skip the machine entirely (not even a sync_state lookup).
    assert called["n"] == 0
    layer._idle_fingerprint_sync_one.assert_not_called()


# --- _idle_fingerprint_sync_one: baseline advance ---------------------------

@pytest.mark.asyncio
async def test_sync_one_advances_baseline_on_success(monkeypatch):
    cm = MagicMock()
    conn = SimpleNamespace(synced_fingerprints={})
    cm.get_connection.return_value = conn
    layer = RemoteExecutionLayer(cm)
    monkeypatch.setattr(
        layer, "resolve_machine_sync_identity", AsyncMock(return_value=(None, "admin")),
    )
    init = AsyncMock()
    monkeypatch.setattr(layer, "_initial_workspace_sync", init)

    await layer._idle_fingerprint_sync_one("m1", "a1", "fpNEW")

    init.assert_awaited_once()
    assert conn.synced_fingerprints["a1"] == "fpNEW"   # baseline advanced


@pytest.mark.asyncio
async def test_sync_one_no_advance_on_merge_failure(monkeypatch):
    cm = MagicMock()
    conn = SimpleNamespace(synced_fingerprints={})
    cm.get_connection.return_value = conn
    layer = RemoteExecutionLayer(cm)
    monkeypatch.setattr(
        layer, "resolve_machine_sync_identity", AsyncMock(return_value=(None, "admin")),
    )
    monkeypatch.setattr(
        layer, "_initial_workspace_sync", AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr("core.remote.remote_workspace_sync.logger", MagicMock())

    await layer._idle_fingerprint_sync_one("m1", "a1", "fpNEW")
    assert "a1" not in conn.synced_fingerprints   # NOT advanced → retried next sweep


@pytest.mark.asyncio
async def test_sync_one_skips_when_identity_unresolved(monkeypatch):
    cm = MagicMock()
    conn = SimpleNamespace(synced_fingerprints={})
    cm.get_connection.return_value = conn
    layer = RemoteExecutionLayer(cm)
    monkeypatch.setattr(
        layer, "resolve_machine_sync_identity", AsyncMock(return_value=None),
    )
    init = AsyncMock()
    monkeypatch.setattr(layer, "_initial_workspace_sync", init)

    await layer._idle_fingerprint_sync_one("m1", "a1", "fp")
    init.assert_not_awaited()
    assert "a1" not in conn.synced_fingerprints
