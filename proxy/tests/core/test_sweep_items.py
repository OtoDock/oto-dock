"""The 60 s sweep's items run concurrently (startup.run_sweep_items): a bound
on how many at once, each failure isolated, and an item still running from
the last tick skipped rather than started twice.
"""

import asyncio

import pytest

import startup


@pytest.fixture(autouse=True)
def _clean():
    startup._sweeps_running.clear()
    yield
    startup._sweeps_running.clear()


@pytest.mark.asyncio
async def test_items_run_at_once_under_the_bound_and_a_failure_is_isolated(monkeypatch):
    monkeypatch.setattr(startup, "_SWEEP_CONCURRENCY", 3)
    running = 0
    peak = 0
    done: list[str] = []

    def item(name: str, fail: bool = False):
        async def _run():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            if fail:
                raise RuntimeError(name)
            done.append(name)
        return (name, _run)

    items = [item("a"), item("b", fail=True), item("c"), item("d"), item("e")]
    ran = await startup.run_sweep_items(items)
    assert ran == ["a", "b", "c", "d", "e"]
    assert set(done) == {"a", "c", "d", "e"}
    assert 1 < peak <= 3


@pytest.mark.asyncio
async def test_an_item_still_running_is_skipped_next_tick_not_doubled():
    gate = asyncio.Event()
    starts = 0

    async def hung():
        nonlocal starts
        starts += 1
        await gate.wait()

    quick: list[int] = []

    async def fast():
        quick.append(1)

    first = asyncio.create_task(startup.run_sweep_items([("hung", hung), ("fast", fast)]))
    await asyncio.sleep(0.02)
    assert starts == 1 and quick == [1]
    # The next tick: the hung item is skipped, the fast one runs again.
    ran = await startup.run_sweep_items([("hung", hung), ("fast", fast)])
    assert ran == ["fast"] and starts == 1 and quick == [1, 1]
    gate.set()
    await first
    assert (await startup.run_sweep_items([("hung", hung)])) == ["hung"]
    gate.clear()
