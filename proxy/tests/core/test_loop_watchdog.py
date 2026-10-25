"""Event-loop stall watchdog (core/loop_watchdog.py)."""

import asyncio
import logging
import threading
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
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
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
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
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
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
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
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
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


# --- the process froze: the watcher's own overshoot, and the cadence params ---


class _FrozenOnce(threading.Event):
    """An Event whose first ``times`` waits return ``for_s`` later than
    asked, with no CPU burned: the whole process did not run (a paused
    host, a SIGSTOP), watcher included."""

    def __init__(self, for_s: float = 0.8, times: int = 1):
        super().__init__()
        self.for_s = for_s
        self.left = times

    def wait(self, timeout=None):
        if self.left > 0:
            self.left -= 1
            time.sleep(self.for_s)
            return super().wait(0)
        return super().wait(timeout)


def _gil_held_call_for_the_test(n: int) -> None:
    # The C encoder never releases the GIL: the watcher thread cannot wake
    # until the call ends, exactly as it cannot through a freeze, but the
    # process is busy all through it.
    import json
    json.dumps(list(range(n)))


@pytest.mark.asyncio
async def test_a_frozen_process_is_named_and_never_read_as_a_stall(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
    monkeypatch.setattr(wd, "_stop", _FrozenOnce())
    assert wd.start(threshold_s=0.2)
    await asyncio.sleep(1.4)
    wd.stop()

    lines = [r.getMessage() for r in caplog.records]
    assert any("the process did not run for 0." in m for m in lines), lines
    assert not [r for r in caplog.records if r.levelno == logging.WARNING], lines
    assert wd.stats()["frozen"] == 1
    assert wd.stats()["stalls"] == 0


@pytest.mark.asyncio
async def test_a_call_holding_the_gil_is_a_stall_not_a_freeze(caplog, monkeypatch):
    """The watcher wakes as late as through a freeze, but the process
    burned CPU all the while: the loop was blocked, and the stall is
    counted with its stack instead of being read as a paused host."""
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
    monkeypatch.setattr(wd, "_FROZEN_S", 0.05)
    assert wd.start(threshold_s=0.1, tick_s=0.01, watch_s=0.005)
    await asyncio.sleep(0.1)  # the first tick lands
    _gil_held_call_for_the_test(4_000_000)   # about 0.3 s on this box, never under 0.15
    await asyncio.sleep(0.3)  # ticks resume: the recovery is seen
    wd.stop()

    lines = [r.getMessage() for r in caplog.records]
    assert not any("the process did not run" in m for m in lines), lines
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "event loop stalled" in warnings[0], lines
    s = wd.stats()
    assert s["frozen"] == 0
    assert s["stalls"] == 1
    assert 0.1 <= s["last_stall_s"] < 3.0


@pytest.mark.asyncio
async def test_a_loop_still_blocked_after_a_freeze_is_a_stall_from_the_thaw(caplog, monkeypatch):
    """The process did not run for 0.8 s and the loop stayed blocked after
    it: the freeze is named once, and the block is judged from the moment
    the process ran again, so it is reported as a stall of its own length
    (the freeze never counts, and a deadlock after a resume never hides)."""
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
    monkeypatch.setattr(wd, "_stop", _FrozenOnce(0.8))
    assert wd.start(threshold_s=0.2)
    await asyncio.sleep(0.05)   # the first tick lands while the watcher is "frozen"
    _blocking_call_for_the_test()   # 1.3 s: through the thaw and 0.5 s past it
    await asyncio.sleep(0.6)    # ticks resume: the recovery is seen
    wd.stop()

    lines = [r.getMessage() for r in caplog.records]
    assert sum("the process did not run for 0." in m for m in lines) == 1, lines
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "event loop stalled" in warnings[0], lines
    s = wd.stats()
    assert s["frozen"] == 1
    assert s["stalls"] == 1
    # Measured from the thaw (about 0.5 s of block left), never from the
    # tick before the freeze (which would read 1.3 s).
    assert 0.2 <= s["last_stall_s"] < 1.0, s


@pytest.mark.asyncio
async def test_the_frozen_line_is_rate_limited(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
    monkeypatch.setattr(wd, "_FROZEN_S", 0.05)
    monkeypatch.setattr(wd, "_stop", _FrozenOnce(0.12, times=7))
    assert wd.start(threshold_s=0.5, tick_s=0.01, watch_s=0.005)
    await asyncio.sleep(1.2)
    wd.stop()
    lines = [m for m in (r.getMessage() for r in caplog.records) if "the process did not run" in m]
    assert len(lines) == wd._REPORT_BURST, lines
    assert wd.stats()["frozen"] == 7
    assert wd.stats()["stalls"] == 0


@pytest.mark.asyncio
async def test_the_cadence_params_set_the_tick_and_the_watch():
    assert wd.start(threshold_s=2.0, report_s=0.5, tick_s=0.25, watch_s=0.1)
    assert wd._tick_s == 0.25 and wd._watch_s == 0.1
    started = time.monotonic()
    # A loaded box can hold the loop past a fixed sleep: wait for two ticks,
    # then bound the count by the time that passed (250 ms each; the proxy's
    # 50 ms would give five times as many).
    for _ in range(100):
        if wd.stats()["lateness_ms"]["1m"]["ticks"] >= 2:
            break
        await asyncio.sleep(0.05)
    elapsed = time.monotonic() - started
    s = wd.stats()
    wd.stop()
    assert 2 <= s["lateness_ms"]["1m"]["ticks"] <= elapsed / 0.25 + 1
    # The defaults come back for the next start.
    assert wd.start(threshold_s=2.0)
    assert wd._tick_s == wd._TICK_S and wd._watch_s == wd._WATCH_S
    wd.stop()


def test_self_hash_is_the_files_sha256():
    import hashlib
    from pathlib import Path
    assert wd.self_hash() == hashlib.sha256(Path(wd.__file__).read_bytes()).hexdigest()
