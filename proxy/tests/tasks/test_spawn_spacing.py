"""Same-tick spawn spacing: scheduled fires reserve session-start slots at
least TASK_SPAWN_SPACING_SECONDS apart so a shared fire time (several 06:30
digests) cannot start every CLI + sandbox in the same second."""

import asyncio

import pytest

import config
from services.scheduler import scheduler


@pytest.fixture(autouse=True)
def _reset_slot(monkeypatch):
    monkeypatch.setattr(scheduler, "_next_spawn_slot", 0.0)
    yield


def test_concurrent_reservations_are_spaced(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)

    async def run():
        return await asyncio.gather(*[
            scheduler._reserve_spawn_slot() for _ in range(3)
        ])

    waits = sorted(asyncio.run(run()))
    assert waits[0] == pytest.approx(0.0, abs=0.5)
    assert waits[1] == pytest.approx(10.0, abs=0.5)
    assert waits[2] == pytest.approx(20.0, abs=0.5)


def test_lone_fire_waits_zero(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)

    async def run():
        first = await scheduler._reserve_spawn_slot()
        # Simulate the herd having passed long ago: the reserved horizon is
        # behind now, so a later lone fire must start immediately.
        scheduler._next_spawn_slot = (
            asyncio.get_running_loop().time() - 60.0
        )
        second = await scheduler._reserve_spawn_slot()
        return first, second

    first, second = asyncio.run(run())
    assert first == pytest.approx(0.0, abs=0.5)
    assert second == pytest.approx(0.0, abs=0.5)


def test_zero_spacing_disables(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 0)

    async def run():
        return await asyncio.gather(*[
            scheduler._reserve_spawn_slot() for _ in range(5)
        ])

    assert all(w == 0.0 for w in asyncio.run(run()))


def test_gate_sleeps_the_reserved_wait(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    async def run():
        await scheduler._spawn_spacing_gate("dyn-a")
        await scheduler._spawn_spacing_gate("dyn-b")

    asyncio.run(run())
    # First fire starts immediately (no sleep call); second sleeps ~10s.
    assert len(slept) == 1
    assert slept[0] == pytest.approx(10.0, abs=0.5)
