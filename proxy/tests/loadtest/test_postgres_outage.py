"""Checks 6 and 7: Postgres goes away for ten seconds, by a restart (every
connection reset, new ones refused) or a freeze (every byte held), while the
loop keeps making the store reads it still makes on the loop and the
``run_db`` lanes keep reading. The outage is staged by a relay process in
front of the test Postgres; no container is paused."""

import asyncio
import statistics
import time
from urllib.parse import urlparse

import pytest

pytestmark = [pytest.mark.loadtest, pytest.mark.timeout(300, method="thread")]

OUTAGE_S = 10.0
# What a store read may raise while Postgres is away (storage/pg.py).
_DB_ERRORS = {"DatabaseUnavailable", "DatabaseUnresponsive", "OperationalError", "PoolTimeout"}


async def _outage(tmp_path, stage: str, restore: str) -> dict:
    """Relay the database, stage the outage for ``OUTAGE_S`` and read on
    the loop every 200 ms and on the lanes every 100 ms until an on-loop
    read succeeds after the restore."""
    import config
    from storage import database as sdb
    from storage import pg
    from tests.loadtest import _harness as h

    real_url = config.DATABASE_URL
    u = urlparse(real_url)
    relay = await h.spawn(h.RELAY, u.hostname or "127.0.0.1", str(u.port or 5432))
    loop_reads: list[dict] = []
    lane_reads: list[dict] = []
    stop = asyncio.Event()

    async def read_on_loop():
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                sdb.get_platform_setting("loadtest_probe")
                loop_reads.append({"t": t0, "s": time.monotonic() - t0, "error": ""})
            except Exception as exc:
                loop_reads.append({"t": t0, "s": time.monotonic() - t0, "error": type(exc).__name__})
            await asyncio.sleep(0.2)

    async def read_on_lane():
        async def one(t0):
            try:
                await pg.run_db(sdb.get_platform_setting, "loadtest_probe")
                lane_reads.append({"t": t0, "s": time.monotonic() - t0, "error": ""})
            except Exception as exc:
                lane_reads.append({"t": t0, "s": time.monotonic() - t0, "error": type(exc).__name__})

        pending = set()
        while not stop.is_set():
            task = asyncio.create_task(one(time.monotonic()))
            pending.add(task)
            task.add_done_callback(pending.discard)
            await asyncio.sleep(0.1)
        if pending:
            await asyncio.wait(pending, timeout=15)

    try:
        port = (await h.read_line(relay, 30))["port"]
        config.DATABASE_URL = u._replace(netloc=f"{u.username}:{u.password}@127.0.0.1:{port}").geturl()
        pg.shutdown_db_executor()
        pg.close_pool()
        async with h.production_loop(tmp_path):
            for _ in range(3):
                sdb.get_platform_setting("loadtest_probe")
                await pg.run_db(sdb.get_platform_setting, "loadtest_probe")
            loop_pool = pg.loop_pool()
            async with h.Window() as window:
                readers = [asyncio.create_task(read_on_loop()), asyncio.create_task(read_on_lane())]
                await asyncio.sleep(1.0)
                t_out = time.monotonic()
                await h.tell(relay, stage)
                await h.read_line(relay, 10)
                await asyncio.sleep(OUTAGE_S)
                await h.tell(relay, restore)
                await h.read_line(relay, 10)
                t_back = time.monotonic()
                async with asyncio.timeout(60):
                    while not any(r["t"] >= t_back and not r["error"] for r in loop_reads):
                        await asyncio.sleep(0.05)
                await asyncio.sleep(1.0)
                stop.set()
                await asyncio.gather(*readers)
            stats = loop_pool.stats() if loop_pool is not None else {}
    finally:
        stop.set()
        # The relay first: pools closing over frozen sockets would wait on them.
        await h.end(relay)
        pg.disarm_loop_pool()
        pg.shutdown_db_executor()
        pg.close_pool(timeout=1.0)
        config.DATABASE_URL = real_url
    return {"t_out": t_out, "t_back": t_back, "loop_reads": loop_reads, "lane_reads": lane_reads,
            "loop_pool": stats, "window": window}


def _first_ok_after(reads: list[dict], t: float) -> float | None:
    ok = [r["t"] + r["s"] for r in reads if r["t"] >= t and not r["error"]]
    return min(ok) - t if ok else None


def test_a_ten_second_postgres_restart_costs_the_loop_one_short_wait(tmp_path):
    from tests.loadtest import _harness as h

    run = h.run_loop(lambda: _outage(tmp_path, "cut", "up"), 240)
    window = run["window"]
    during = [r for r in run["loop_reads"] if run["t_out"] <= r["t"] < run["t_back"]]
    lanes_during = [r for r in run["lane_reads"] if run["t_out"] <= r["t"] < run["t_back"]]
    h.record("postgres-cut", outage_s=OUTAGE_S,
             loop_reads={"during": len(during), "failed": sum(1 for r in during if r["error"]),
                         "errors": sorted({r["error"] for r in during if r["error"]}),
                         "max_ms": h.ms(max((r["s"] for r in during), default=0.0))},
             lane_reads={"during": len(lanes_during),
                         "failed": sum(1 for r in lanes_during if r["error"]),
                         "max_ms": h.ms(max((r["s"] for r in lanes_during), default=0.0))},
             first_ok_after_restore_s=_first_ok_after(run["loop_reads"], run["t_back"]),
             loop_pool=run["loop_pool"], **window.summary())

    window.ticker.assert_covered()
    errors = {r["error"] for r in during + lanes_during if r["error"]}
    assert errors <= _DB_ERRORS, errors
    assert during and any(r["error"] for r in during), "the on-loop reads never saw the cut"
    assert lanes_during and any(r["error"] or r["s"] > 1.0 for r in lanes_during), (
        "the lane reads never saw the cut: the lanes did not go through the relay")
    assert window.ticker.stats()["max_ms"] < h.CUT_MAX_S * 1000, window.ticker.stats()


def test_a_ten_second_postgres_freeze_costs_the_loop_one_bounded_stall(tmp_path):
    from tests.loadtest import _harness as h

    run = h.run_loop(lambda: _outage(tmp_path, "freeze", "thaw"), 240)
    window = run["window"]
    during = [r for r in run["loop_reads"] if run["t_out"] <= r["t"] < run["t_back"]]
    unresponsive = [r for r in during if r["error"] == "DatabaseUnresponsive"]
    refused = [r for r in during if r["error"] == "DatabaseUnavailable"
               and (not unresponsive or r["t"] > unresponsive[0]["t"])]
    long_ticks = window.ticker.over(0.100)
    back = _first_ok_after(run["loop_reads"], run["t_back"])
    h.record("postgres-freeze", outage_s=OUTAGE_S,
             loop_reads={"during": len(during), "errors": sorted({r["error"] for r in during if r["error"]}),
                         "unresponsive": len(unresponsive), "refused": len(refused),
                         "refused_median_ms": h.ms(statistics.median(r["s"] for r in refused)) if refused else None,
                         "refused_max_ms": h.ms(max((r["s"] for r in refused), default=0.0))},
             ticks_over_100ms=[h.ms(x) for x in long_ticks], first_ok_after_thaw_s=back,
             loop_pool=run["loop_pool"], **window.summary())

    window.ticker.assert_covered()
    assert {r["error"] for r in during if r["error"]} <= _DB_ERRORS, during
    assert len(unresponsive) >= 1, "no read met the freeze: nothing was measured"
    assert len(long_ticks) <= 1, long_ticks
    assert not long_ticks or long_ticks[0] <= h.FREEZE_STALL_S, long_ticks
    assert refused, "no refusal after the stall"
    assert statistics.median(r["s"] for r in refused) < h.REFUSAL_MEDIAN_S, refused
    assert max(r["s"] for r in refused) < h.FREEZE_REFUSAL_MAX_S, refused
    assert back <= h.FREEZE_RECOVERY_S, back
