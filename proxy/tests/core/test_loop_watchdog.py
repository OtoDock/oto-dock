"""Event-loop stall watchdog (core/loop_watchdog.py)."""

import asyncio
import logging
import time

import pytest

from core import loop_watchdog as wd


@pytest.fixture(autouse=True)
def _clean():
    wd.stop()
    wd.reset_stats()
    yield
    wd.stop()
    wd.reset_stats()


def _blocking_call_for_the_test():
    time.sleep(1.3)


@pytest.mark.asyncio
async def test_stall_is_logged_with_loop_stack_and_counted(caplog):
    caplog.set_level(logging.INFO, logger="claude-proxy.loop-watchdog")
    assert wd.start(threshold_s=0.2)
    await asyncio.sleep(0.6)  # let the first tick land

    # Blocks the loop thread 1.3 s: the watcher samples every 0.5 s in step
    # with the ticks, so a block that ends on a sample instant is a coin
    # toss (missed, or measured as exactly the threshold); this one covers
    # the sample at 1.5 s with 0.4 s to spare on either side.
    _blocking_call_for_the_test()

    await asyncio.sleep(1.2)  # ticks resume → recovery is detected
    wd.stop()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no stall warning logged"
    assert "event loop stalled" in warnings[0].getMessage()
    assert "_blocking_call_for_the_test" in warnings[0].getMessage()
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("recovered after" in m for m in infos)
    s = wd.stats()
    assert s["stalls"] == 1
    # Measured from the tick the block skipped to the tick after it: about
    # 0.9 s here, with room for a loaded runner on both sides.
    assert 0.35 < s["last_stall_s"] < 3.0
    assert s["max_stall_s"] == s["last_stall_s"]


@pytest.mark.asyncio
async def test_disabled_when_threshold_zero():
    assert wd.start(threshold_s=0) is False
    assert not wd.is_running()
    assert wd.stats()["enabled"] is False


@pytest.mark.asyncio
async def test_stop_before_tick_cancel_logs_nothing(caplog):
    caplog.set_level(logging.INFO, logger="claude-proxy.loop-watchdog")
    assert wd.start(threshold_s=0.2)
    await asyncio.sleep(0.6)
    wd.stop()  # flags the thread first, then cancels the tick task
    await asyncio.sleep(0.8)  # no ticks now — must NOT read as a stall
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not wd.is_running()


@pytest.mark.asyncio
async def test_start_is_idempotent():
    assert wd.start(threshold_s=0.5)
    assert wd.start(threshold_s=0.5) is False
    wd.stop()
    wd.stop()  # idempotent


# --- Sub-threshold reports (LOOP_WATCHDOG_REPORT_S) and loop statistics ---

def _short_block_for_the_test(seconds):
    time.sleep(seconds)


@pytest.mark.asyncio
async def test_a_short_stall_is_reported_once_with_its_stack(caplog):
    caplog.set_level(logging.INFO, logger="claude-proxy.loop-watchdog")
    assert wd.start(threshold_s=2.0, report_s=0.2)
    await asyncio.sleep(0.3)
    _short_block_for_the_test(0.4)
    await asyncio.sleep(0.4)
    wd.stop()

    # A loaded box (the full suite under xdist) adds incidental lateness over
    # the 0.2 s report floor, so judge only the stall this test causes.
    lines = [r.getMessage() for r in caplog.records]
    reports = [m for m in lines if "slow event loop" in m]
    ours = [m for m in reports if "_short_block_for_the_test" in m]
    assert len(ours) == 1, lines
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    s = wd.stats()
    assert s["stalls"] == 0  # below the 2 s threshold
    assert s["slow"] >= 1
    assert s["histogram_ms"]["250"] >= 1
    assert s["lateness_ms"]["1m"]["max"] >= 300


@pytest.mark.asyncio
async def test_short_stall_reports_are_rate_limited(caplog):
    caplog.set_level(logging.INFO, logger="claude-proxy.loop-watchdog")
    assert wd.start(threshold_s=2.0, report_s=0.1)
    await asyncio.sleep(0.2)
    for _ in range(8):
        _short_block_for_the_test(0.2)
        await asyncio.sleep(0.15)
    wd.stop()
    reports = [r for r in caplog.records if "slow event loop" in r.getMessage()]
    assert len(reports) == 5
    assert wd.stats()["slow"] == 8


@pytest.mark.asyncio
async def test_lateness_and_executor_stats():
    from concurrent.futures import ThreadPoolExecutor

    ex = ThreadPoolExecutor(max_workers=3)
    wd.watch_executor("test", ex)
    try:
        assert wd.start(threshold_s=2.0, report_s=0.25)
        await asyncio.sleep(0.4)
        s = wd.stats()
        wd.stop()
        one = s["lateness_ms"]["1m"]
        assert one["ticks"] >= 4
        assert 0 <= one["p50"] <= one["p99"] <= max(one["max"], one["p99"])
        assert s["executors"]["test"]["workers"] == 3
        fds = wd.fd_stats()
        assert fds["open"] > 0 and fds["limit"] > 0
        assert "fds_open" not in s  # never in the public /health block
    finally:
        ex.shutdown(wait=False)
