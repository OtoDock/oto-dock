"""Unit tests for core.remote.transfer_registry (Feature E, 1.4.0).

The workspace transfer-progress registry: items keyed by transfer_id with one
MachineRow per target, rows born 'queued', throttled progress ticks, terminal
transfer_done semantics, snapshot-state connect replay, done-linger + stale
sweep, and per-user recipient scoping via ``should_sync_to_target`` (the same
predicate as broadcast_file_updated / the fan-out itself).
"""

import time

import pytest

from core.remote import transfer_registry as tr


# The genuine resolver, saved before the autouse fixture stubs the module
# attribute — the scoping test exercises it directly.
_REAL_RESOLVE = tr._resolve_recipients


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch):
    tr._items.clear()
    tr.set_broadcaster(None)
    # Deterministic machine names + recipients unless a test overrides.
    monkeypatch.setattr(tr, "_machine_name", lambda mid: f"name-{mid}")
    monkeypatch.setattr(
        tr, "_resolve_recipients", lambda a, p: frozenset({"user-1"}),
    )
    yield
    tr._items.clear()
    tr.set_broadcaster(None)


class _Recorder:
    def __init__(self):
        self.events: list[tuple[dict, list[str]]] = []

    async def __call__(self, event, recipients):
        self.events.append((event, list(recipients)))

    def types(self):
        return [e["type"] for e, _ in self.events]


@pytest.mark.asyncio
async def test_begin_creates_queued_rows_and_emits_started():
    rec = _Recorder()
    tr.set_broadcaster(rec)
    tid = await tr.begin(
        "agent-1", "workspace/big.mp4", kind="upload", bytes_total=100,
        machine_ids=["m1", "m2"],
    )
    assert tid
    item = tr.get(tid)
    assert item is not None
    assert {r.state for r in item.machines.values()} == {"queued"}
    assert item.machines["m1"].name == "name-m1"
    assert rec.types() == ["transfer_started"]
    ev = rec.events[0][0]
    assert ev["filename"] == "big.mp4"
    assert len(ev["machines"]) == 2
    assert rec.events[0][1] == ["user-1"]


@pytest.mark.asyncio
async def test_explicit_transfer_id_is_used():
    tid = await tr.begin(
        "agent-1", "workspace/x.bin", kind="upload", bytes_total=10,
        machine_ids=["m1"], transfer_id="fixed-id",
    )
    assert tid == "fixed-id"
    assert tr.get("fixed-id") is not None


@pytest.mark.asyncio
async def test_state_transitions_and_done_semantics():
    rec = _Recorder()
    tr.set_broadcaster(rec)
    tid = await tr.begin(
        "agent-1", "workspace/x.bin", kind="sync", bytes_total=10,
        machine_ids=["m1", "m2"],
    )
    await tr.set_state(tid, "m1", "active")
    await tr.set_state(tid, "m1", "done")
    assert tr.get(tid).done_at is None  # m2 still pending
    await tr.set_state(tid, "m2", "failed", error="offline")
    item = tr.get(tid)
    assert item.done_at is not None
    done_events = [e for e, _ in rec.events if e["type"] == "transfer_done"]
    assert len(done_events) == 1
    assert done_events[0]["ok"] is False  # one row failed
    # Repeated terminal set is idempotent — no second transfer_done.
    await tr.set_state(tid, "m2", "failed")
    assert len([e for e, _ in rec.events if e["type"] == "transfer_done"]) == 1


@pytest.mark.asyncio
async def test_all_done_is_ok_true():
    rec = _Recorder()
    tr.set_broadcaster(rec)
    tid = await tr.begin(
        "agent-1", "workspace/x.bin", kind="sync", bytes_total=10,
        machine_ids=["m1"],
    )
    await tr.set_state(tid, "m1", "done")
    done = [e for e, _ in rec.events if e["type"] == "transfer_done"]
    assert done and done[0]["ok"] is True
    # done rows report full bytes
    assert tr.get(tid).machines["m1"].bytes_sent == 10


@pytest.mark.asyncio
async def test_progress_throttled_but_final_always_emits(monkeypatch):
    rec = _Recorder()
    tr.set_broadcaster(rec)
    tid = await tr.begin(
        "agent-1", "w.bin", kind="sync", bytes_total=100, machine_ids=["m1"],
    )
    await tr.progress(tid, "m1", 10, 100)   # first tick emits
    await tr.progress(tid, "m1", 20, 100)   # inside throttle window — dropped
    await tr.progress(tid, "m1", 100, 100)  # final — always emits
    prog = [e for e, _ in rec.events if e["type"] == "transfer_progress"]
    assert [p["bytes_sent"] for p in prog] == [10, 100]
    # A tick on a queued row flips it active implicitly.
    assert tr.get(tid).machines["m1"].state == "active"


@pytest.mark.asyncio
async def test_snapshot_event_shape_and_inflight():
    tid = await tr.begin(
        "agent-1", "users/alice/workspace/v.mp4", kind="upload",
        bytes_total=50, machine_ids=["m1"],
    )
    items = tr.snapshot_inflight()
    assert [i.transfer_id for i in items] == [tid]
    ev = tr.snapshot_event(items[0])
    assert ev["type"] == "transfer_state"
    assert ev["rel_path"] == "users/alice/workspace/v.mp4"
    assert ev["machines"][0]["state"] == "queued"
    assert "done" not in ev


@pytest.mark.asyncio
async def test_sweep_evicts_lingered_done_and_fails_stale(monkeypatch):
    rec = _Recorder()
    tr.set_broadcaster(rec)
    done_tid = await tr.begin(
        "agent-1", "a.bin", kind="sync", bytes_total=1, machine_ids=["m1"],
    )
    await tr.set_state(done_tid, "m1", "done")
    stale_tid = await tr.begin(
        "agent-1", "b.bin", kind="sync", bytes_total=1, machine_ids=["m2"],
    )
    # Age both past their windows.
    tr.get(done_tid).done_at = time.monotonic() - tr.DONE_LINGER_S - 1
    tr.get(stale_tid).last_event_ts = time.monotonic() - tr.STALE_AGE_S - 1
    removed = await tr.sweep_stale()
    assert removed == 2
    assert tr.get(done_tid) is None and tr.get(stale_tid) is None
    # The stale item's pending row was failed out + transfer_done(ok=false).
    tail = [e for e, _ in rec.events if e.get("transfer_id") == stale_tid]
    assert [e["type"] for e in tail][-2:] == ["transfer_machine_state", "transfer_done"]
    assert tail[-2]["state"] == "failed"
    assert tail[-1]["ok"] is False


@pytest.mark.asyncio
async def test_broadcaster_error_never_raises():
    async def _boom(event, recipients):
        raise RuntimeError("delivery broke")

    tr.set_broadcaster(_boom)
    tid = await tr.begin(
        "agent-1", "x.bin", kind="sync", bytes_total=1, machine_ids=["m1"],
    )
    assert tid is not None
    await tr.set_state(tid, "m1", "done")  # must not raise


@pytest.mark.asyncio
async def test_recipient_scoping_uses_isolation_predicate(monkeypatch):
    """The real resolver keeps personal paths owner-only (admins included)
    and shares workspace/ with all members, config/ with owner-tier only."""
    from storage import database as task_store
    from services.notifications import notification_manager

    users = {
        "sub-alice": {"role": "creator"},
        "sub-bob": {"role": "creator"},
        "sub-root": {"role": "admin"},
    }
    roles = {
        "sub-alice": {"agent-1": "manager"},
        "sub-bob": {"agent-1": "viewer"},
        "sub-root": {},
    }
    names = {"sub-alice": "alice", "sub-bob": "bob", "sub-root": "root"}
    monkeypatch.setattr(
        notification_manager, "resolve_targets",
        lambda scope, target: list(users.keys()),
    )
    monkeypatch.setattr(task_store, "get_user", users.get)
    monkeypatch.setattr(
        task_store, "get_user_agent_roles", lambda sub: roles.get(sub, {}),
    )
    monkeypatch.setattr(
        task_store, "get_username_by_sub", names.get,
    )
    personal = _REAL_RESOLVE("agent-1", "users/alice/workspace/f.bin")
    assert personal == frozenset({"sub-alice"})  # not bob, not even admin

    shared = _REAL_RESOLVE("agent-1", "workspace/f.bin")
    assert shared == frozenset({"sub-alice", "sub-bob", "sub-root"})

    config_path = _REAL_RESOLVE("agent-1", "config/prompt.md")
    assert config_path == frozenset({"sub-alice", "sub-root"})  # owner-tier
