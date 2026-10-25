"""Background tasks (satellite/background.py): held until done, a death
handed to the loop's exception handler under its owner and name, a cancel or
an exit silent."""

from __future__ import annotations

import asyncio
import gc

import pytest

from satellite import background


@pytest.fixture(autouse=True)
def _empty_registry():
    background._tasks.clear()
    yield
    background._tasks.clear()


def _run_recording(scenario) -> list[dict]:
    """Run ``scenario`` with a recording exception handler on its loop."""
    seen: list[dict] = []

    async def main():
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: seen.append(ctx))
        await scenario()

    asyncio.run(main())
    return seen


def test_a_task_is_held_until_it_is_done():
    async def scenario():
        done = asyncio.Event()

        async def work():
            await done.wait()

        task = background.spawn(work(), name="work", owner="test")
        assert task.get_name() == "test:work"
        del task
        gc.collect()
        # The registry is the only strong reference: the task survives.
        assert len(background._tasks) == 1
        done.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert background._tasks == set()

    assert _run_recording(scenario) == []


def test_a_task_that_dies_reaches_the_loop_handler_with_its_name():
    async def scenario():
        async def boom():
            raise RuntimeError("the reason")

        background.spawn(boom(), name="sweep", owner="call")
        for _ in range(3):
            await asyncio.sleep(0)

    seen = _run_recording(scenario)
    assert len(seen) == 1
    assert "call:sweep" in seen[0]["message"]
    assert isinstance(seen[0]["exception"], RuntimeError)
    assert background._tasks == set()


def test_a_cancelled_task_is_silent():
    async def scenario():
        nap = background.spawn(asyncio.sleep(30), name="nap", owner="test")
        await asyncio.sleep(0)
        nap.cancel()
        with pytest.raises(asyncio.CancelledError):
            await nap

    assert _run_recording(scenario) == []
    assert background._tasks == set()


def test_an_exit_is_not_a_death():
    # A task that ends the process (SystemExit) re-raises out of the loop, so
    # its done callback is driven by hand on a finished future.
    async def scenario():
        fut = asyncio.get_running_loop().create_future()
        fut.set_exception(SystemExit(0))
        background._tasks.add(fut)
        background._finished(fut)
        assert background._tasks == set()

    assert _run_recording(scenario) == []


def test_spawn_off_the_loop_raises():
    async def work():
        pass

    coro = work()
    with pytest.raises(RuntimeError):
        background.spawn(coro, name="x", owner="test")
    coro.close()
