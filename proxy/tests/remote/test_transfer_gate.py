"""Unit tests for core.remote.transfer_gate (Feature F, 1.4.0).

The global outbound-transfer semaphore: concurrency cap across callers,
0=unlimited bypass, size-threshold bypass, queued→active on_state lifecycle
with a single INFO log, best-effort callbacks.
"""

import asyncio

import pytest

import config
from core.remote import transfer_gate as tg


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(config, "SYNC_FANOUT_MIN_MB", 1, raising=False)
    tg.reset_for_tests()
    yield
    tg.reset_for_tests()


MB = 1024 * 1024


async def _hold(machine, seconds, state, on_state=None):
    async with tg.slot(machine, "agent-1", "workspace/big.bin", 8 * MB, on_state=on_state):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(seconds)
        state["active"] -= 1


@pytest.mark.asyncio
async def test_cap_enforced_all_complete():
    state = {"active": 0, "max_active": 0}
    await asyncio.gather(*(_hold(f"m{i}", 0.02, state) for i in range(6)))
    assert state["max_active"] == 2


@pytest.mark.asyncio
async def test_zero_limit_unlimited_no_callbacks(monkeypatch):
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 0, raising=False)
    tg.reset_for_tests()
    calls = []

    async def on_state(s):
        calls.append(s)

    state = {"active": 0, "max_active": 0}
    await asyncio.gather(*(_hold(f"m{i}", 0.02, state, on_state) for i in range(6)))
    assert state["max_active"] == 6  # fully unbounded
    assert calls == []               # bypass emits nothing
    assert tg._sem is None           # no semaphore even created


@pytest.mark.asyncio
async def test_threshold_bypass_while_slots_held(monkeypatch):
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 1, raising=False)
    tg.reset_for_tests()
    parked = asyncio.Event()
    release = asyncio.Event()

    async def _park():
        async with tg.slot("m-big", "agent-1", "big.bin", 8 * MB):
            parked.set()
            await release.wait()

    task = asyncio.create_task(_park())
    await parked.wait()
    # The only slot is held — a below-threshold push must not wait.
    async with asyncio.timeout(1.0):
        async with tg.slot("m-small", "agent-1", "small.md", 100):
            pass
    release.set()
    await task


@pytest.mark.asyncio
async def test_queued_then_active_and_single_info_log(caplog, monkeypatch):
    monkeypatch.setattr(config, "SYNC_FANOUT_CONCURRENCY", 1, raising=False)
    tg.reset_for_tests()
    caplog.set_level("INFO", logger="claude-proxy.transfer-gate")
    order: list[str] = []
    parked = asyncio.Event()
    release = asyncio.Event()

    async def _first():
        async with tg.slot("m1", "agent-1", "big.bin", 8 * MB):
            parked.set()
            await release.wait()

    async def on_state(s):
        order.append(s)
        if s == "queued":
            release.set()  # let the holder finish once we're provably queued

    async def _second():
        async with tg.slot("m2", "agent-1", "big.bin", 8 * MB, on_state=on_state):
            pass

    t1 = asyncio.create_task(_first())
    await parked.wait()
    await _second()
    await t1
    assert order == ["queued", "active"]
    queued_logs = [r for r in caplog.records if "fan-out queued" in r.message]
    assert len(queued_logs) == 1
    assert "(0 ahead)" in queued_logs[0].message


@pytest.mark.asyncio
async def test_fast_path_still_reports_active():
    order: list[str] = []

    async def on_state(s):
        order.append(s)

    async with tg.slot("m1", "agent-1", "big.bin", 8 * MB, on_state=on_state):
        pass
    assert order == ["active"]  # no wait → no 'queued', but lifecycle holds


@pytest.mark.asyncio
async def test_raising_on_state_never_blocks():
    async def on_state(s):
        raise RuntimeError("registry broke")

    entered = False
    async with tg.slot("m1", "agent-1", "big.bin", 8 * MB, on_state=on_state):
        entered = True
    assert entered
