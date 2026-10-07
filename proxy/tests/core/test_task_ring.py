"""The parked tasks' ring (core/concurrency._acquire_task): creators take
turns when room appears, a heavy head never blocks a lighter head of another
creator, a cancelled waiter leaves the ring, and the rotation wakes the next
head at once.
"""

import asyncio

import pytest

from core import concurrency


@pytest.fixture
def ring(monkeypatch):
    """A fresh admission state with a fake room gate the test opens by hand:
    ``room["mb"]`` is what fits now; an admit spends its estimate."""
    concurrency.init()
    concurrency._sessions.clear()
    concurrency._session_est.clear()
    concurrency._session_owner.clear()
    concurrency._task_ring.clear()
    concurrency._task_ring_order.clear()
    room = {"mb": 0}

    def _has_room(est, *, is_task):
        return room["mb"] >= est

    real_add = concurrency._add

    def _add(session_id, kind, est, owner=""):
        room["mb"] -= est
        real_add(session_id, kind, est, owner)

    monkeypatch.setattr(concurrency, "_has_room", _has_room)
    monkeypatch.setattr(concurrency, "_add", _add)
    yield room
    concurrency._task_ring.clear()
    concurrency._task_ring_order.clear()
    for sid in list(concurrency._sessions):
        concurrency.release(sid)


async def _park(sid: str, creator: str, est: int = 100) -> asyncio.Task:
    task = asyncio.create_task(
        concurrency._acquire_task(sid, "task", est, ring_key=creator))
    await asyncio.sleep(0)
    return task


async def _open(room: dict, mb: int) -> None:
    room["mb"] += mb
    async with concurrency._cond:
        concurrency._cond.notify_all()
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_creators_take_turns_when_room_appears(ring):
    admitted = []
    tasks = {}
    for sid, creator in (("a1", "alice"), ("a2", "alice"), ("a3", "alice"), ("b1", "bob")):
        tasks[sid] = await _park(sid, creator)
    assert concurrency._parked_tasks == 4
    for _ in range(4):
        await _open(ring, 100)
        admitted.extend(sid for sid, t in tasks.items() if t.done() and sid not in admitted)
    assert admitted == ["a1", "b1", "a2", "a3"]
    assert concurrency._parked_tasks == 0 and not concurrency._task_ring


@pytest.mark.asyncio
async def test_a_heavy_head_never_blocks_a_lighter_head_of_another_creator(ring):
    heavy = await _park("h1", "alice", est=800)
    light = await _park("l1", "bob", est=100)
    await _open(ring, 100)
    assert light.done() and not heavy.done()
    await _open(ring, 800)
    assert heavy.done()


@pytest.mark.asyncio
async def test_a_cancelled_waiter_leaves_the_ring_and_the_next_head_is_woken(ring):
    first = await _park("c1", "carol")
    second = await _park("c2", "carol")
    other = await _park("d1", "dave")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert concurrency._task_ring["carol"][0] == ("c2", 100)
    assert concurrency._parked_tasks == 2
    await _open(ring, 200)
    assert second.done() and other.done()


@pytest.mark.asyncio
async def test_an_admit_with_room_and_an_empty_ring_never_parks(ring):
    ring["mb"] = 100
    assert (await concurrency._acquire_task("free", "task", 100, ring_key="eve")).ok
    assert concurrency._parked_tasks == 0
