"""The state files written without holding the loop (``core/file_commit.StateFile``)
and the disk work left behind a close (``file_commit.behind`` and ``settled``).

A file the platform rewrites whole from memory (the session index, the
security index, the Docker tools' held sessions) is encoded on the loop at
each change and written on a thread of its own: a stalled disk queues that
file's writes and nothing else. The write is held here on an event the test
controls, standing in for the stalled disk.

Run: cd proxy && venv/bin/pytest tests/core/test_state_file.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

from core import file_commit


class _Held:
    """The file's writes, held until the test releases them."""

    def __init__(self, monkeypatch) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.blobs: list[str] = []
        self.threads: list[int] = []
        real = file_commit.atomic_replace

        def held(path, blob, *, fsync):
            self.threads.append(threading.get_ident())
            self.entered.set()
            assert self.release.wait(10), "the test never released the write"
            self.blobs.append(blob)
            return real(path, blob, fsync=fsync)

        monkeypatch.setattr(file_commit, "atomic_replace", held)

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 5), "no write reached the disk"


def _file(tmp_path, state: dict, *, delay: float = 0.0, name: str = "t") -> file_commit.StateFile:
    path = tmp_path / f"{name}.json"

    def encode() -> str:
        return json.dumps(state, separators=(",", ":"))

    return file_commit.StateFile(name, lambda: path, encode, fsync=True, delay=lambda: delay)


def _read(tmp_path, name: str = "t") -> dict:
    return json.loads((tmp_path / f"{name}.json").read_text())


async def _ticks_while(cond, *, limit_s: float) -> int:
    """Loop ticks counted while ``cond()`` holds: the loop is free when they grow."""
    n = 0
    deadline = asyncio.get_running_loop().time() + limit_s
    while cond() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
        n += 1
    return n


def test_without_a_loop_the_write_is_immediate(tmp_path):
    f = _file(tmp_path, {"a": 1})
    f.mark_dirty()
    assert _read(tmp_path) == {"a": 1}


def test_the_write_leaves_inside_the_call_and_the_loop_ticks_while_it_is_held(tmp_path, monkeypatch):
    held = _Held(monkeypatch)
    f = _file(tmp_path, {"s1": 1})

    async def scenario():
        f.mark_dirty()
        # Handed to the file's thread before the call returned: no timer.
        assert f._inflight is not None and f._handle is None
        waiter = asyncio.ensure_future(f.written(timeout=10))
        await held.wait_entered()
        assert held.threads[0] != threading.get_ident()
        ticks = await _ticks_while(lambda: not waiter.done(), limit_s=0.3)
        assert ticks > 10 and not waiter.done()
        held.release.set()
        assert await waiter is True

    asyncio.run(scenario())
    assert _read(tmp_path) == {"s1": 1}


def test_each_change_keeps_the_snapshot_of_its_moment(tmp_path, monkeypatch):
    """A change made while a write is held is encoded at once, so a later
    change of memory without a mark (a stop popping its bindings) never
    rewrites what that change saved."""
    held = _Held(monkeypatch)
    state: dict = {"n": 0}
    f = _file(tmp_path, state)

    async def scenario():
        f.mark_dirty()
        await held.wait_entered()
        state["n"] = 1
        f.mark_dirty()
        state["n"] = 2                         # no mark: must not reach the disk
        held.release.set()
        assert await f.written(timeout=10)

    asyncio.run(scenario())
    assert held.blobs == ['{"n":0}', '{"n":1}']


def test_changes_during_a_held_write_arm_exactly_one_more_with_the_newest_state(tmp_path, monkeypatch):
    held = _Held(monkeypatch)
    state: dict = {"n": 0}
    f = _file(tmp_path, state)

    async def scenario():
        f.mark_dirty()
        await held.wait_entered()
        for i in range(1, 6):
            state["n"] = i
            f.mark_dirty()
        waiter = asyncio.ensure_future(f.written(timeout=10))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        held.release.set()
        assert await waiter is True

    asyncio.run(scenario())
    assert held.blobs == ['{"n":0}', '{"n":5}']
    assert _read(tmp_path) == {"n": 5}


def test_the_waiting_write_starts_the_instant_the_one_in_flight_ends(tmp_path, monkeypatch):
    import time
    entered_at: list[float] = []
    left_at: list[float] = []
    release = threading.Event()
    real = file_commit.atomic_replace

    def held(path, blob, *, fsync):
        entered_at.append(time.monotonic())
        if len(entered_at) == 1:
            assert release.wait(10)
        try:
            return real(path, blob, fsync=fsync)
        finally:
            left_at.append(time.monotonic())

    monkeypatch.setattr(file_commit, "atomic_replace", held)
    state: dict = {"n": 0}
    f = _file(tmp_path, state)

    async def scenario():
        f.mark_dirty()
        await asyncio.to_thread(lambda: [time.sleep(0.01) for _ in range(500) if not entered_at])
        state["n"] = 1
        f.mark_dirty()
        release.set()
        assert await f.written(timeout=10)

    asyncio.run(scenario())
    assert len(entered_at) == 2
    assert entered_at[1] - left_at[0] < 0.25    # no timer between the two writes


def test_written_with_nothing_pending_answers_at_once(tmp_path):
    f = _file(tmp_path, {})

    async def scenario():
        assert await asyncio.wait_for(f.written(timeout=10), 0.5) is True

    asyncio.run(scenario())


def test_an_older_count_never_replaces_a_newer_one(tmp_path):
    f = _file(tmp_path, {})
    path = tmp_path / "t.json"
    f._install(path, '{"new":{}}', 2)
    f._install(path, '{"old":{}}', 1)
    assert _read(tmp_path) == {"new": {}}


def test_a_failed_write_answers_false_keeps_the_old_file_and_the_next_change_heals_it(tmp_path, monkeypatch):
    path = tmp_path / "t.json"
    path.write_text('{"kept":{}}')
    state = {"lost": 1}
    f = _file(tmp_path, state)
    real = os.rename

    def boom(*args, **kwargs):
        raise OSError("disk full")

    async def scenario():
        monkeypatch.setattr(os, "rename", boom)
        f.mark_dirty()
        assert await f.written(timeout=10) is False
        monkeypatch.setattr(os, "rename", real)
        assert _read(tmp_path) == {"kept": {}}
        assert [p.name for p in tmp_path.iterdir()] == ["t.json"]
        state["healed"] = 2
        f.mark_dirty()
        assert await f.written(timeout=10) is True

    asyncio.run(scenario())
    assert _read(tmp_path) == {"lost": 1, "healed": 2}


def test_an_encode_that_raises_answers_false_and_never_hangs_a_waiter(tmp_path, caplog):
    path = tmp_path / "t.json"

    def bad() -> str:
        raise TypeError("not serializable")

    f = file_commit.StateFile("bad", lambda: path, bad, fsync=False)

    async def scenario():
        f.mark_dirty()
        assert await asyncio.wait_for(f.written(timeout=10), 1) is False

    asyncio.run(scenario())
    assert not path.exists()
    assert "bad" in caplog.text


def test_a_write_that_cannot_start_never_raises_into_the_caller(tmp_path, monkeypatch, caplog):
    f = _file(tmp_path, {"a": 1})

    class _Dead:
        def submit(self, *args, **kwargs):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(f, "_thread", _Dead)

    async def scenario():
        f.mark_dirty()                         # must not raise into a close
        assert await f.written(timeout=1) is False

    asyncio.run(scenario())
    assert "could not start" in caplog.text


def test_the_wait_is_bounded_and_the_write_still_lands(tmp_path, monkeypatch, caplog):
    held = _Held(monkeypatch)
    f = _file(tmp_path, {"a": 1})

    async def scenario():
        f.mark_dirty()
        assert await f.written(timeout=0.1) is False
        held.release.set()
        assert await f.written(timeout=10) is True

    asyncio.run(scenario())
    assert "not on disk after" in caplog.text


def test_the_shutdown_flush_writes_the_newest_after_the_write_in_flight(tmp_path, monkeypatch):
    held = _Held(monkeypatch)
    state = {"n": 1}
    f = _file(tmp_path, state)

    async def scenario():
        f.mark_dirty()
        await held.wait_entered()
        state["n"] = 2
        f.mark_dirty()                         # rides the next write, after the one in flight
        threading.Timer(0.1, held.release.set).start()
        f.flush_sync(wait_s=10)                # the shutdown's call, on the loop's thread
        assert _read(tmp_path) == {"n": 2}
        assert await asyncio.wait_for(f.written(timeout=10), 1) is True

    asyncio.run(scenario())


def test_the_shutdown_flush_is_bounded_by_a_disk_that_never_answers(tmp_path, monkeypatch, caplog):
    held = _Held(monkeypatch)
    state = {"n": 1}
    f = _file(tmp_path, state)

    async def scenario():
        f.mark_dirty()
        await held.wait_entered()
        state["n"] = 2
        f.mark_dirty()
        f.flush_sync(wait_s=0.1)               # returns: the write stays queued
        assert "still queued" in caplog.text
        held.release.set()
        await asyncio.to_thread(f._thread().submit(lambda: None).result, 10)

    asyncio.run(scenario())
    assert _read(tmp_path) == {"n": 2}         # it landed once the disk answered


def test_flush_all_writes_every_state_file(tmp_path):
    a_state, b_state = {"a": 1}, {"b": 1}
    a = _file(tmp_path, a_state, delay=60, name="a")
    b = _file(tmp_path, b_state, delay=60, name="b")

    async def scenario():
        a.mark_dirty()
        b.mark_dirty()
        file_commit.flush_all()

    asyncio.run(scenario())
    assert _read(tmp_path, "a") == {"a": 1} and _read(tmp_path, "b") == {"b": 1}


def test_a_delay_coalesces_the_changes_into_one_write(tmp_path):
    state: dict = {}
    f = _file(tmp_path, state, delay=0.05)
    writes = []
    real = f._install

    def counting(path, blob, gen):
        writes.append(blob)
        return real(path, blob, gen)

    f._install = counting

    async def scenario():
        for i in range(20):
            state[f"s{i}"] = i
            f.mark_dirty()
        assert not (tmp_path / "t.json").exists()
        assert await f.written(timeout=10)

    asyncio.run(scenario())
    assert len(writes) == 1
    assert len(_read(tmp_path)) == 20


def test_a_change_from_another_thread_lands_on_the_owner_loop(tmp_path):
    state: dict = {}
    f = _file(tmp_path, state, delay=0.05)

    async def scenario():
        state["loop"] = 1
        f.mark_dirty()

        def other():
            state["thread"] = 1
            f.mark_dirty()

        t = threading.Thread(target=other)
        t.start()
        t.join()
        await asyncio.sleep(0.01)
        assert await f.written(timeout=10)

    asyncio.run(scenario())
    assert set(_read(tmp_path)) == {"loop", "thread"}


def test_each_file_writes_on_its_own_watched_thread(tmp_path, monkeypatch):
    from core import loop_watchdog
    held = _Held(monkeypatch)
    f = _file(tmp_path, {"a": 1}, name="mine")

    async def scenario():
        f.mark_dirty()
        await held.wait_entered()
        held.release.set()
        assert await f.written(timeout=10)

    asyncio.run(scenario())
    assert "state-file:mine" in loop_watchdog.stats()["executors"]
    assert threading.current_thread().ident not in held.threads


def test_a_closed_owner_loop_is_replaced(tmp_path):
    state = {"n": 1}
    f = _file(tmp_path, state, delay=0.05)

    async def first():
        f.mark_dirty()                         # armed, never written: the loop ends first

    asyncio.run(first())
    state["n"] = 2

    async def second():
        f.mark_dirty()
        assert await f.written(timeout=10)

    asyncio.run(second())
    assert _read(tmp_path) == {"n": 2}


def test_the_path_is_the_one_of_the_change_not_of_the_write(tmp_path, monkeypatch):
    """A write that lands late goes where its change pointed, never where a
    later change points (a test's folder, after its monkeypatch is undone)."""
    held = _Held(monkeypatch)
    where = {"path": tmp_path / "first.json"}
    f = file_commit.StateFile("moving", lambda: where["path"], lambda: "{}", fsync=False)

    async def scenario():
        f.mark_dirty()
        await held.wait_entered()
        where["path"] = tmp_path / "second.json"
        held.release.set()
        assert await f.written(timeout=10)

    asyncio.run(scenario())
    assert (tmp_path / "first.json").exists() and not (tmp_path / "second.json").exists()


@pytest.mark.parametrize("fsync", [True, False])
def test_atomic_replace_syncs_only_when_asked(tmp_path, monkeypatch, fsync):
    calls: list[int] = []
    real = os.fsync

    def counting(fd: int) -> None:
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", counting)
    assert file_commit.atomic_replace(tmp_path / "x.json", "{}", fsync=fsync)
    assert bool(calls) is fsync
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


# ── the work behind a close ──────────────────────────────────────────────────


def test_behind_without_a_loop_runs_at_once(tmp_path):
    target = tmp_path / "gone"
    target.mkdir()
    file_commit.behind(os.rmdir, target, name="t", key="s1")
    assert not target.exists()


def test_settled_waits_for_the_work_left_under_its_key_only(tmp_path):
    release = threading.Event()
    done: list[str] = []

    def slow(label: str) -> None:
        assert release.wait(10)
        done.append(label)

    async def scenario():
        file_commit.behind(slow, "mine", name="t", key="s1")
        file_commit.behind(done.append, "other", name="t", key="s2")
        assert await file_commit.settled("s2", 5) is True
        assert await file_commit.settled("s1", 0.1) is False
        release.set()
        assert await file_commit.settled("s1", 5) is True
        assert await file_commit.settled("never", 0) is True

    asyncio.run(scenario())
    assert sorted(done) == ["mine", "other"]


def test_a_failure_behind_is_logged_and_never_raised(caplog):
    def boom() -> None:
        raise OSError("gone")

    async def scenario():
        file_commit.behind(boom, name="removal", key="s3")
        assert await file_commit.settled("s3", 5) is True

    asyncio.run(scenario())
    assert "disk work removal failed" in caplog.text


def test_a_start_waits_for_the_work_its_last_life_left(tmp_path):
    from core.session import session_closing
    release = threading.Event()
    order: list[str] = []

    def removal() -> None:
        assert release.wait(10)
        order.append("removed")

    async def scenario():
        file_commit.behind(removal, name="t", key="sid-life")
        start = asyncio.ensure_future(session_closing.wait_closed("sid-life"))
        await asyncio.sleep(0.05)
        assert not start.done()
        release.set()
        await asyncio.wait_for(start, 5)
        order.append("started")

    asyncio.run(scenario())
    assert order == ["removed", "started"]


def test_settled_past_its_bound_cancels_the_removals_that_have_not_started():
    """A start that waited out its bound goes on: a removal of its last life
    still queued then must never run, or it could delete the new life's files."""
    pool = file_commit.executor()
    release = threading.Event()
    ran: list[str] = []

    def busy() -> None:
        assert release.wait(10)

    async def scenario():
        for _ in range(pool._max_workers):
            file_commit.behind(busy, name="t", key="busy-key")
        file_commit.behind(ran.append, "late removal", name="t", key="life-key")
        assert await file_commit.settled("life-key", 0.1) is False
        release.set()
        assert await file_commit.settled("busy-key", 10)

    asyncio.run(scenario())
    assert ran == []


def _owner_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(5)
    return loop, thread


def _stop(loop, thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def test_a_wait_on_a_loop_that_does_not_own_the_file_answers_not_written(tmp_path, monkeypatch, caplog):
    """A change made from another loop is handed to the owner: the wait that
    follows cannot see it land, so it answers "not written" and says so,
    never "written" at once."""
    state = {"v": 0}
    f = _file(tmp_path, state)
    owner, thread = _owner_loop()
    held = None
    try:
        asyncio.run_coroutine_threadsafe(_mark_on_loop(f), owner).result(5)   # the owner
        held = _Held(monkeypatch)
        state["v"] = 1

        async def other_loop():
            f.mark_dirty()                      # handed to the owner
            return await f.written(timeout=1)

        assert asyncio.run(other_loop()) is False
        assert "another loop owns" in caplog.text
        assert held.entered.wait(5)             # the owner took the change
        held.release.set()
        f._thread().submit(lambda: None).result(10)
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), owner).result(5)
        assert _read(tmp_path) == {"v": 1}
    finally:
        if held is not None:
            held.release.set()
        _stop(owner, thread)


async def _mark_on_loop(f) -> None:
    f.mark_dirty()
    assert await f.written(timeout=10)


def test_a_closed_owner_still_answers_by_what_is_on_disk(tmp_path):
    state = {"v": 1}
    f = _file(tmp_path, state)
    owner, thread = _owner_loop()
    asyncio.run_coroutine_threadsafe(_mark_on_loop(f), owner).result(5)
    _stop(owner, thread)

    async def later():
        return await f.written(timeout=1)

    assert asyncio.run(later()) is True


def test_the_shutdown_flush_waits_for_a_write_in_flight(tmp_path, monkeypatch):
    """Nothing newer waits, but a write is in flight: the shutdown's flush
    waits for it within its bound, so the removal it carries is on disk."""
    held = _Held(monkeypatch)
    f = _file(tmp_path, {"v": 1})

    async def scenario():
        f.mark_dirty()
        await held.wait_entered()
        threading.Timer(0.2, held.release.set).start()
        f.flush_sync(wait_s=5)
        assert _read(tmp_path) == {"v": 1}

    asyncio.run(scenario())


def test_the_shutdown_flush_bounds_its_wait_for_a_write_in_flight(tmp_path, monkeypatch, caplog):
    held = _Held(monkeypatch)
    f = _file(tmp_path, {"v": 1})

    async def scenario():
        f.mark_dirty()
        await held.wait_entered()
        f.flush_sync(wait_s=0.1)               # returns: the write stays in flight
        assert "still queued" in caplog.text
        held.release.set()
        await asyncio.to_thread(f._thread().submit(lambda: None).result, 10)

    asyncio.run(scenario())
    assert _read(tmp_path) == {"v": 1}
