"""Shutdown tail additions (2026-09-04): pending satellite status persists are
drained, the loop watchdog is stopped and the DB executor shut down — all
BEFORE the pool closes (a persist landing after ``close_pool`` would raise;
the watchdog must keep reporting through the DB work)."""

from __future__ import annotations

import asyncio

import pytest

import startup
from storage import pg as pg_pool


@pytest.fixture(autouse=True)
def _no_failsafe(monkeypatch):
    monkeypatch.setattr(startup, "_ARM_EXIT_FAILSAFE", False)
    startup._bg_tasks.clear()
    yield
    startup._bg_tasks.clear()


def test_persists_drained_and_watchdog_stopped_before_pool_close(monkeypatch):
    order: list[str] = []

    async def _fake_flush(logger):
        order.append("flush")

    class _CM:
        async def drain_persists(self, timeout=5.0):
            order.append("drain")
            return True

    from core import loop_watchdog
    from core.layers.direct import mcp as direct_mcp
    from core.remote import satellite_connection as sc

    monkeypatch.setattr(startup, "_flush_active_pumps", _fake_flush)
    monkeypatch.setattr(startup.task_store, "list_orphaned_runs", lambda: [])
    monkeypatch.setattr(startup.task_store, "mark_orphaned_runs_failed",
                        lambda exclude_ids=None: 0)
    monkeypatch.setattr(startup.task_store, "mark_orphaned_meetings_failed", lambda: 0)
    monkeypatch.setattr(startup.scheduler, "stop", lambda: order.append("scheduler"))
    monkeypatch.setattr(sc, "get_connection_manager", _CM)
    monkeypatch.setattr(loop_watchdog, "stop", lambda join_timeout=2.0: order.append("watchdog"))
    monkeypatch.setattr(pg_pool, "shutdown_db_executor",
                        lambda wait=False: order.append("executor"))
    monkeypatch.setattr(pg_pool, "close_pool", lambda timeout=3.0: order.append("pool"))
    monkeypatch.setattr(direct_mcp, "stop_mcp_thread", lambda join_timeout=2.0: None)

    asyncio.run(startup._shutdown_cleanup(startup.logger))

    assert order.index("scheduler") < order.index("drain")
    assert order.index("drain") < order.index("watchdog") < order.index("executor")
    assert order.index("executor") < order.index("pool")


def test_slow_drain_is_bounded_and_never_blocks_close(monkeypatch, caplog):
    class _CM:
        async def drain_persists(self, timeout=5.0):
            await asyncio.sleep(min(timeout, 0.05))
            return False  # still pending (stalled DB)

    from core.layers.direct import mcp as direct_mcp
    from core.remote import satellite_connection as sc
    closed = []

    async def _fake_flush(logger):
        pass

    monkeypatch.setattr(startup, "_flush_active_pumps", _fake_flush)
    monkeypatch.setattr(startup.task_store, "list_orphaned_runs", lambda: [])
    monkeypatch.setattr(startup.task_store, "mark_orphaned_runs_failed",
                        lambda exclude_ids=None: 0)
    monkeypatch.setattr(startup.task_store, "mark_orphaned_meetings_failed", lambda: 0)
    monkeypatch.setattr(startup.scheduler, "stop", lambda: None)
    monkeypatch.setattr(sc, "get_connection_manager", _CM)
    monkeypatch.setattr(pg_pool, "close_pool", lambda timeout=3.0: closed.append(True))
    monkeypatch.setattr(direct_mcp, "stop_mcp_thread", lambda join_timeout=2.0: None)

    asyncio.run(startup._shutdown_cleanup(startup.logger))
    assert closed == [True]
    assert any("persists still pending" in r.getMessage() for r in caplog.records)
