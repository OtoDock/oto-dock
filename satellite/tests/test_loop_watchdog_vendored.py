"""The event-loop watchdog on the satellite: the proxy's module vendored
byte for byte (``satellite/_vendored/loop_watchdog.py``), pinned by
``SHARED_LOOP_WATCHDOG_HASH`` and started by ``__main__._main``."""

import asyncio
import logging
import time

import pytest

from satellite import config
from satellite._vendored import loop_watchdog as wd


@pytest.fixture(autouse=True)
def _clean():
    wd.stop()
    wd.reset_stats()
    yield
    wd.stop()
    wd.reset_stats()


def test_the_vendored_copy_is_the_pinned_one():
    assert wd.self_hash() == config.SHARED_LOOP_WATCHDOG_HASH


def _blocking_call_on_the_satellite_loop():
    time.sleep(0.7)


@pytest.mark.asyncio
async def test_a_blocked_loop_logs_its_stack_under_the_satellites_logging(caplog):
    """The lines reach the root logger (the satellite logs through it), with
    the laptop cadence ``_main`` uses."""
    caplog.set_level(logging.INFO, logger="otodock.loop-watchdog")
    assert wd.start(0.2, 0.1, tick_s=0.05, watch_s=0.025)
    await asyncio.sleep(0.3)
    _blocking_call_on_the_satellite_loop()
    await asyncio.sleep(0.6)
    wd.stop()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "event loop stalled" in warnings[0]
    assert "_blocking_call_on_the_satellite_loop" in warnings[0]
    assert wd.stats()["stalls"] == 1


@pytest.mark.asyncio
async def test_the_cadence_main_passes_is_coarser_than_the_proxys():
    from satellite import __main__ as main
    assert main._WATCHDOG_TICK_S > wd._TICK_S and main._WATCHDOG_WATCH_S > wd._WATCH_S
    assert wd.start(main._WATCHDOG_THRESHOLD_S, main._WATCHDOG_REPORT_S,
                    tick_s=main._WATCHDOG_TICK_S, watch_s=main._WATCHDOG_WATCH_S)
    assert wd._tick_s == main._WATCHDOG_TICK_S
    wd.stop()
