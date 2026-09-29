"""Shutdown tail (``startup._shutdown_cleanup``) — ordering + failsafe.

The order is load-bearing: DB writers (pump flush, orphan cleanup) run before
the pool closes; the lifespan's background tasks are cancelled before the pool
closes; the hard-exit failsafe arms LAST and only when enabled.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

import startup
from storage import pg as pg_pool


class _FakePump:
    def __init__(self):
        self.aborted = False
        self._task = None

    def abort(self):
        self.aborted = True


@pytest.fixture(autouse=True)
def _no_failsafe(monkeypatch):
    monkeypatch.setattr(startup, "_ARM_EXIT_FAILSAFE", False)
    startup._bg_tasks.clear()
    yield
    startup._bg_tasks.clear()


class TestCleanupOrder:
    def test_db_writers_run_before_pool_close(self, monkeypatch):
        order: list[str] = []

        async def _fake_flush(logger):
            order.append("flush")

        monkeypatch.setattr(startup, "_flush_active_pumps", _fake_flush)
        monkeypatch.setattr(startup.task_store, "list_orphaned_runs",
                            lambda: order.append("orphans") or [])
        monkeypatch.setattr(startup.task_store, "mark_orphaned_runs_failed",
                            lambda exclude_ids=None: 0)
        monkeypatch.setattr(startup.task_store, "mark_orphaned_meetings_failed",
                            lambda: 0)
        monkeypatch.setattr(startup.scheduler, "stop",
                            lambda: order.append("scheduler"))
        monkeypatch.setattr(pg_pool, "close_pool",
                            lambda timeout=3.0: order.append("pool"))
        from core.layers.direct import mcp as direct_mcp
        monkeypatch.setattr(direct_mcp, "stop_mcp_thread",
                            lambda join_timeout=2.0: order.append("mcp-io"))

        asyncio.run(startup._shutdown_cleanup(startup.logger))

        assert order.index("flush") < order.index("pool")
        assert order.index("orphans") < order.index("pool")
        assert order.index("scheduler") < order.index("pool")
        assert order.index("pool") < order.index("mcp-io")

    def test_session_index_flushed_synchronously_before_the_watchdog_stops(self, monkeypatch):
        order: list[str] = []
        monkeypatch.setattr(startup.task_store, "list_orphaned_runs", lambda: [])
        monkeypatch.setattr(startup.task_store, "mark_orphaned_runs_failed",
                            lambda exclude_ids=None: 0)
        monkeypatch.setattr(startup.task_store, "mark_orphaned_meetings_failed",
                            lambda: 0)
        monkeypatch.setattr(startup.scheduler, "stop", lambda: None)
        monkeypatch.setattr(pg_pool, "close_pool",
                            lambda timeout=3.0: order.append("pool"))
        from core import loop_watchdog
        monkeypatch.setattr(loop_watchdog, "stop", lambda: order.append("watchdog"))
        from core.layers.direct import mcp as direct_mcp
        monkeypatch.setattr(direct_mcp, "stop_mcp_thread",
                            lambda join_timeout=2.0: None)
        from core.session import session_state
        monkeypatch.setattr(session_state, "flush_session_index",
                            lambda: order.append("index"))

        asyncio.run(startup._shutdown_cleanup(startup.logger))

        assert order.index("index") < order.index("watchdog") < order.index("pool")

    def test_bg_tasks_cancelled_before_pool_close(self, monkeypatch):
        events: list[str] = []
        monkeypatch.setattr(startup.task_store, "list_orphaned_runs", lambda: [])
        monkeypatch.setattr(startup.task_store, "mark_orphaned_runs_failed",
                            lambda exclude_ids=None: 0)
        monkeypatch.setattr(startup.task_store, "mark_orphaned_meetings_failed",
                            lambda: 0)
        monkeypatch.setattr(startup.scheduler, "stop", lambda: None)
        monkeypatch.setattr(pg_pool, "close_pool",
                            lambda timeout=3.0: events.append("pool"))
        from core.layers.direct import mcp as direct_mcp
        monkeypatch.setattr(direct_mcp, "stop_mcp_thread",
                            lambda join_timeout=2.0: None)

        async def _run():
            async def _forever():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    events.append("cancelled")
                    raise

            startup._bg_tasks.append(asyncio.create_task(_forever()))
            await asyncio.sleep(0)
            await startup._shutdown_cleanup(startup.logger)

        asyncio.run(_run())
        assert events.index("cancelled") < events.index("pool")
        assert startup._bg_tasks == []


class TestPumpFlush:
    def test_recovery_eligible_pump_left_untouched(self, monkeypatch):
        from core.events.stream_pump import _active_pumps
        from services.scheduler import run_recovery

        keep, flush = _FakePump(), _FakePump()
        _active_pumps["chat-keep"] = keep
        _active_pumps["chat-flush"] = flush
        monkeypatch.setattr(run_recovery, "is_recovery_eligible",
                            lambda chat_id: chat_id == "chat-keep")
        try:
            asyncio.run(startup._flush_active_pumps(startup.logger))
        finally:
            _active_pumps.pop("chat-keep", None)
            _active_pumps.pop("chat-flush", None)

        assert flush.aborted and not keep.aborted


class TestFailsafe:
    def test_fires_exit_code_3_after_grace(self, monkeypatch):
        fired: list[int] = []
        monkeypatch.setattr(startup, "_ARM_EXIT_FAILSAFE", True)
        monkeypatch.setattr(startup, "_EXIT_FAILSAFE_GRACE_S", 0.05)
        monkeypatch.setattr(os, "_exit", fired.append)
        monkeypatch.setattr(startup.logging, "shutdown", lambda: None)

        startup._arm_exit_failsafe()
        deadline = time.monotonic() + 2.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fired == [3]

    @pytest.mark.parametrize("drained", [True, False])
    def test_flushes_logging_only_when_the_log_writer_drained(self, monkeypatch,
                                                              drained):
        """A wedged writer holds the file handler's lock — logging.shutdown()
        would block on it and the hard exit would never happen."""
        from core import log_queue
        calls: list[str] = []
        monkeypatch.setattr(startup, "_ARM_EXIT_FAILSAFE", True)
        monkeypatch.setattr(startup, "_EXIT_FAILSAFE_GRACE_S", 0.05)
        monkeypatch.setattr(os, "_exit", lambda code: calls.append(f"exit:{code}"))
        monkeypatch.setattr(startup.logging, "shutdown",
                            lambda: calls.append("shutdown"))
        monkeypatch.setattr(log_queue, "drain",
                            lambda timeout_s: (calls.append(f"drain:{timeout_s}"),
                                               drained)[1])

        startup._arm_exit_failsafe()
        deadline = time.monotonic() + 2.0
        while not any(c.startswith("exit") for c in calls) and time.monotonic() < deadline:
            time.sleep(0.02)
        expected = ["drain:2.0", "shutdown", "exit:3"] if drained else ["drain:2.0", "exit:3"]
        assert calls == expected

    def test_flag_off_arms_nothing(self, monkeypatch):
        armed: list = []
        monkeypatch.setattr(startup, "_ARM_EXIT_FAILSAFE", False)
        monkeypatch.setattr(threading, "Timer",
                            lambda *a, **k: armed.append(a))
        startup._arm_exit_failsafe()
        assert armed == []


class TestStopMcpThread:
    def test_skips_close_when_thread_wont_stop(self):
        from unittest.mock import MagicMock
        from core.layers.direct import mcp as direct_mcp

        loop = MagicMock()
        thread = MagicMock()
        thread.is_alive.return_value = True
        direct_mcp._mcp_loop, direct_mcp._mcp_thread = loop, thread
        try:
            direct_mcp.stop_mcp_thread(join_timeout=0.01)
        finally:
            direct_mcp._mcp_loop = direct_mcp._mcp_thread = None
        loop.call_soon_threadsafe.assert_called_once()
        loop.close.assert_not_called()

    def test_closes_stopped_loop_and_is_idempotent(self):
        from unittest.mock import MagicMock
        from core.layers.direct import mcp as direct_mcp

        loop = MagicMock()
        thread = MagicMock()
        thread.is_alive.return_value = False
        direct_mcp._mcp_loop, direct_mcp._mcp_thread = loop, thread
        direct_mcp.stop_mcp_thread(join_timeout=0.01)
        loop.close.assert_called_once()
        # Second call: state cleared — a no-op.
        direct_mcp.stop_mcp_thread(join_timeout=0.01)
        loop.close.assert_called_once()


class TestRouteWarmup:
    """FastAPI builds each included router's route state lazily, on the
    first request that falls through it (0.38 s of loop on the proxy's
    routes): warm_routes builds it at boot, matching only."""

    def _app(self):
        from fastapi import APIRouter, FastAPI
        calls: list[str] = []
        inner = APIRouter()

        @inner.get("/inner/{x}")
        def _inner(x: str):
            calls.append(x)
            return {}

        outer = APIRouter()
        outer.include_router(inner, prefix="/nested")
        app = FastAPI()
        app.include_router(outer, prefix="/v1")

        @app.get("/{path:path}")
        def _spa(path: str):
            calls.append(path)
            return {}
        return app, calls

    def test_every_included_router_is_built_and_no_handler_runs(self):
        from fastapi.routing import _IncludedRouter
        app, calls = self._app()
        included = [r for r in app.router.routes if isinstance(r, _IncludedRouter)]
        assert included
        startup.warm_routes(app)
        for r in included:
            assert r._effective_candidates_version == r.original_router._get_routes_version()
        assert calls == []

    def test_a_route_whose_match_raises_does_not_stop_the_boot(self):
        app, _ = self._app()

        class _Broken:
            def matches(self, scope):
                raise RuntimeError("broken route")

        app.router.routes.insert(0, _Broken())
        startup.warm_routes(app)
