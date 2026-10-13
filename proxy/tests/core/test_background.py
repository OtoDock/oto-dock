"""Background tasks (core/background.py): tracked until done, a death logged
under its owner, cancelled at shutdown; and no task the proxy starts at
statement level is left untracked."""

from __future__ import annotations

import ast
import asyncio
import gc
import logging
from pathlib import Path

import pytest

from core import background


@pytest.fixture(autouse=True)
def _empty_registry():
    background._tasks.clear()
    yield
    background._tasks.clear()


def test_a_finished_task_leaves_the_registry():
    async def scenario():
        done = asyncio.Event()

        async def work():
            await done.wait()
            return 7

        task = background.spawn(work(), name="work", owner="test")
        assert task.get_name() == "test:work"
        assert background.running("test") == [task]
        del task
        gc.collect()
        # The registry is the only strong reference: the task survives.
        assert len(background.running()) == 1
        done.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return background.running(), len(background._tasks)

    still, kept = asyncio.run(scenario())
    assert still == [] and kept == 0


def test_a_task_that_dies_is_logged_with_its_owner_and_name(caplog):
    caplog.set_level(logging.ERROR, logger="otodock.background")

    async def scenario():
        async def boom():
            raise RuntimeError("the reason")

        background.spawn(boom(), name="boom", owner="sched")
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    records = [r for r in caplog.records if r.name == "otodock.background"]
    assert len(records) == 1
    assert "sched:boom" in records[0].getMessage()
    assert "the reason" in records[0].getMessage()
    assert records[0].exc_info is not None
    assert background._tasks == set()


def test_a_cancelled_task_is_silent(caplog):
    caplog.set_level(logging.DEBUG, logger="otodock.background")

    async def scenario():
        task = background.spawn(asyncio.sleep(30), name="nap", owner="test")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [r for r in caplog.records if r.name == "otodock.background"] == []
    assert background._tasks == set()


def test_cancel_all_cancels_and_waits_for_the_teardown():
    torn_down: list[str] = []

    async def scenario():
        async def holds():
            try:
                await asyncio.sleep(30)
            finally:
                await asyncio.sleep(0)
                torn_down.append("holds")

        background.spawn(holds(), name="holds", owner="a")
        background.spawn(holds(), name="holds", owner="b")
        await asyncio.sleep(0)
        n = await background.cancel_all(grace=0.05, timeout=2.0)
        return n, background.running()

    n, left = asyncio.run(scenario())
    assert n == 2 and left == []
    assert torn_down == ["holds", "holds"]


def test_the_grace_lets_a_task_finish_its_work():
    finished: list[str] = []

    async def scenario():
        async def short():
            await asyncio.sleep(0.05)
            finished.append("short")

        background.spawn(short(), name="short", owner="test")
        return await background.cancel_all(grace=2.0, timeout=1.0)

    assert asyncio.run(scenario()) == 0
    assert finished == ["short"]


def test_cancelling_a_watcher_leaves_the_task_it_watches_running():
    # A spawned task waits on a pump it does not own through asyncio.wait:
    # the shutdown cancel ends the watcher, never the pump.
    async def scenario():
        pump = asyncio.ensure_future(asyncio.sleep(30))

        async def watch():
            await asyncio.wait({pump})

        background.spawn(watch(), name="watch", owner="test")
        await asyncio.sleep(0)
        n = await background.cancel_all(grace=0.05, timeout=1.0)
        alive = not pump.done()
        pump.cancel()
        return n, alive

    n, alive = asyncio.run(scenario())
    assert n == 1 and alive


def test_a_loop_callback_may_spawn():
    async def scenario():
        loop = asyncio.get_running_loop()
        ran = asyncio.Event()

        async def work():
            ran.set()

        loop.call_soon(lambda: background.spawn(work(), name="cb", owner="test"))
        await asyncio.wait_for(ran.wait(), 2)

    asyncio.run(scenario())


def test_spawn_off_the_loop_raises():
    async def work():
        pass

    coro = work()
    with pytest.raises(RuntimeError):
        background.spawn(coro, name="x", owner="test")
    coro.close()


def test_the_shutdown_tail_cancels_the_spawned_tasks(monkeypatch):
    import startup
    from storage import pg as pg_pool

    order: list[str] = []
    monkeypatch.setattr(startup, "_ARM_EXIT_FAILSAFE", False)

    async def _fake_flush(logger):
        order.append("flush")

    async def _fake_cancel_all(**kw):
        order.append("background")
        return 0

    monkeypatch.setattr(startup, "_flush_active_pumps", _fake_flush)
    monkeypatch.setattr(startup.task_store, "list_orphaned_runs", lambda: [])
    monkeypatch.setattr(startup.task_store, "mark_orphaned_runs_failed", lambda exclude_ids=None: 0)
    monkeypatch.setattr(startup.task_store, "mark_orphaned_meetings_failed", lambda: 0)
    monkeypatch.setattr(background, "cancel_all", _fake_cancel_all)
    monkeypatch.setattr(startup.scheduler, "stop", lambda: order.append("scheduler"))
    monkeypatch.setattr(pg_pool, "close_pool", lambda timeout=3.0: order.append("pool"))
    from core.layers.direct import mcp as direct_mcp
    monkeypatch.setattr(direct_mcp, "stop_mcp_thread", lambda join_timeout=2.0: None)

    asyncio.run(startup._shutdown_cleanup(startup.logger))
    assert order.index("flush") < order.index("background") < order.index("pool")


# --- the sweep stays swept ----------------------------------------------------

PROXY = Path(__file__).resolve().parents[2]
_STARTERS = {"create_task", "ensure_future"}
# Files that still start a task at statement level, each for a reason: the
# vendored stdlib-only twin cannot import the proxy; the rest belong to a lane
# that had them open when the sweep ran and convert at their next change.
_UNCONVERTED = {
    "core/layers/codex/app_server_client.py",
}


def _statement_level_starts(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _STARTERS:
                lines.append(node.lineno)
    return lines


def test_no_task_is_started_and_dropped_outside_the_helper():
    offenders = []
    for path in sorted(PROXY.rglob("*.py")):
        rel = path.relative_to(PROXY).as_posix()
        if rel.startswith(("tests/", "venv/")) or "/tests/" in rel or rel in _UNCONVERTED:
            continue
        for line in _statement_level_starts(path):
            offenders.append(f"{rel}:{line}")
    assert offenders == [], (
        "a task started and never stored can be collected mid-flight: "
        "use core.background.spawn(coro, name=..., owner=...)\n" + "\n".join(offenders)
    )


def test_the_unconverted_list_has_no_stale_entry():
    stale = [rel for rel in sorted(_UNCONVERTED)
             if not (PROXY / rel).exists() or not _statement_level_starts(PROXY / rel)]
    assert stale == [], f"converted or gone, drop from _UNCONVERTED: {stale}"
