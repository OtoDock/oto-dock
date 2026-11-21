"""The planned stop's states, counts and hooks (``core/drain.py``)."""

import asyncio

import pytest

from core import drain


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    drain.reset_for_tests()
    monkeypatch.setattr(drain, "_POLL_S", 0.01)
    monkeypatch.setattr(drain, "QUIET_S", 0.05)
    monkeypatch.setattr(drain, "HELD_TTL_S", 30.0)
    yield
    drain.reset_for_tests()


async def _until(state: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while drain.state() != state:
        assert asyncio.get_running_loop().time() < deadline, f"never reached {state}: {drain.work()}"
        await asyncio.sleep(0.01)


def test_nothing_is_held_until_a_drain_or_the_stop():
    assert (drain.state(), drain.holding(), drain.stopping()) == ("running", False, False)
    drain.note_stopping()
    assert (drain.holding(), drain.stopping(), drain.state()) == (True, True, "running")


def test_a_counter_of_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        drain.register_counter("naps", lambda: 1)


def test_the_settling_wait_ends_before_anyone_reads_held():
    """The verb stops following at ``held`` and the script stops the server,
    so a wait that must come before the stop is over by then."""
    seen: list[str] = []

    async def scene():
        async def settle():
            seen.append(drain.state())
            await asyncio.sleep(0.05)
            seen.append(drain.work()["state"])
        drain.on_settle(settle)
        drain.on_held(lambda: seen.append("held-hook"))
        await drain.drain(0)
        await _until("held")
    asyncio.run(scene())
    assert seen == ["draining", "draining", "held-hook"]


def test_a_settling_wait_that_fails_does_not_stop_the_drain():
    def boom():
        raise RuntimeError("no")

    async def scene():
        drain.on_settle(boom)
        await drain.drain(0)
        await _until("held")
    asyncio.run(scene())
    assert drain.state() == "held"


def test_a_resume_ends_a_settling_wait():
    async def scene():
        ended = asyncio.Event()

        async def settle():
            try:
                await asyncio.sleep(60)
            finally:
                ended.set()
        drain.on_settle(settle)
        await drain.drain(0)
        await asyncio.sleep(0.05)
        await drain.resume()
        assert ended.is_set() and drain.state() == "running"
    asyncio.run(scene())


def test_an_idle_platform_is_held_and_drained_after_two_quiet_reads():
    seen: list[str] = []

    async def scene():
        drain.on_change(lambda: seen.append(drain.state()))
        drain.on_held(lambda: seen.append("held-hook"))
        first = await drain.drain(60)
        assert (first["state"], first["drained"], first["busy"]) == ("draining", False, False)
        assert drain.holding()
        await _until("held")
        return drain.work()

    work = asyncio.run(scene())
    assert (work["state"], work["drained"], work["bound_left_s"]) == ("held", True, 0)
    assert seen == ["draining", "held", "held-hook"]


def test_open_work_is_waited_for_and_work_that_starts_late_restarts_the_quiet():
    turns = {"n": 1}

    async def scene():
        drain.register_counter("turns", lambda: turns["n"])
        drain.register_waiting(lambda: 2)
        drain.register_queued(lambda: 3)
        work = await drain.drain(60)
        assert (work["busy"], work["open"]["turns"], work["waiting_on_people"], work["queued"]) == (
            True, 1, 2, 3)
        assert 55 <= work["bound_left_s"] <= 60
        await asyncio.sleep(0.1)
        assert drain.state() == "draining"
        turns["n"] = 0
        await asyncio.sleep(0.02)
        turns["n"] = 1                       # a turn's end started another
        await asyncio.sleep(0.1)
        assert drain.state() == "draining"
        turns["n"] = 0
        await _until("held")
        return drain.work()

    assert asyncio.run(scene())["drained"] is True


def test_the_bound_ends_the_wait_with_work_still_open():
    async def scene():
        drain.register_counter("runs", lambda: 1)
        await drain.drain(0.05)
        await _until("held")
        return drain.work()

    work = asyncio.run(scene())
    assert (work["drained"], work["busy"], work["open"]["runs"]) == (False, True, 1)


def test_a_bound_of_zero_holds_at_once():
    async def scene():
        drain.register_counter("turns", lambda: 0)
        await drain.drain(0)
        await _until("held", timeout=0.5)
        return drain.work()

    assert asyncio.run(scene())["drained"] is True


def test_background_work_is_waited_for_only_while_its_count_moves(monkeypatch):
    monkeypatch.setattr(drain, "BACKGROUND_STILL_S", 0.1)
    jobs = {"n": 2}

    async def scene():
        drain.register_counter("background", lambda: jobs["n"])
        await drain.drain(60)
        await asyncio.sleep(0.06)
        jobs["n"] = 1                        # it moved: still waited for
        await asyncio.sleep(0.06)
        assert drain.state() == "draining"
        await _until("held")                 # it stood still: no longer waited for
        return drain.work()

    work = asyncio.run(scene())
    assert (work["drained"], work["open"]["background"]) == (True, 1)


def test_resume_takes_new_work_again_and_runs_its_hooks():
    seen: list[str] = []

    async def scene():
        async def arm():
            seen.append("armed")
        drain.on_resume(arm)
        drain.on_resume(lambda: seen.append("fires"))
        drain.register_counter("turns", lambda: 1)
        await drain.drain(60)
        work = await drain.resume()
        assert (work["state"], drain.holding()) == ("running", False)
        # A resume with no drain on does nothing.
        await drain.resume()

    asyncio.run(scene())
    assert seen == ["armed", "fires"]


def test_a_second_drain_joins_the_first():
    async def scene():
        drain.register_counter("turns", lambda: 1)
        await drain.drain(60)
        again = await drain.drain(5)
        return again

    again = asyncio.run(scene())
    assert again["state"] == "draining" and again["bound_left_s"] > 50


def test_a_drain_asked_again_while_held_keeps_the_hold_from_then(monkeypatch):
    """A backup asks again for the length of its copy: each ask moves the
    lapse on. One that stops asking (a backup that was killed) still lets
    go after the one length."""
    now = {"t": 1000.0}
    monkeypatch.setattr(drain, "_clock", lambda: now["t"])
    monkeypatch.setattr(drain, "HELD_TTL_S", 900.0)

    async def scene():
        await drain.drain(0)
        await _until("held")
        for _ask in range(3):                   # 14 minutes on, three times: 42 minutes held
            now["t"] += 840.0
            again = await drain.drain(0)
            assert again["state"] == "held"
            await asyncio.sleep(0.05)
            assert drain.state() == "held"
        now["t"] += 899.0
        await asyncio.sleep(0.05)
        assert drain.state() == "held"          # 15 minutes count from the last ask
        now["t"] += 2.0
        await _until("running")

    asyncio.run(scene())


def test_a_hold_nobody_stopped_resumes_by_itself(monkeypatch):
    monkeypatch.setattr(drain, "HELD_TTL_S", 0.1)
    seen: list[str] = []

    async def scene():
        drain.on_resume(lambda: seen.append("resumed"))
        await drain.drain(0)
        await _until("held", timeout=0.5)
        await _until("running")

    asyncio.run(scene())
    assert seen == ["resumed"] and not drain.holding()


def test_a_broken_counter_or_hook_never_fails_the_drain():
    def boom() -> int:
        raise RuntimeError("no")

    async def scene():
        drain.register_counter("calls", boom)
        drain.on_held(boom)
        seen: list[str] = []
        drain.on_held(lambda: seen.append("after"))
        await drain.drain(0)
        await _until("held", timeout=0.5)
        return seen, drain.work()

    seen, work = asyncio.run(scene())
    assert seen == ["after"] and work["open"]["calls"] == 0


def test_a_stopping_process_starts_no_drain():
    async def scene():
        drain.note_stopping()
        return await drain.drain(60)

    assert asyncio.run(scene())["state"] == "running"


# ── the start's hold (a boot that builds the tools after it listens) ───────


def test_the_start_holds_new_work_until_its_end_and_runs_its_hooks_once():
    seen: list[str] = []
    drain.on_start_ended(lambda: seen.append("ended"))
    drain.hold_for_start()
    assert (drain.holding(), drain.starting(), drain.state()) == (True, True, "running")
    asyncio.run(drain.end_start_hold())
    asyncio.run(drain.end_start_hold())
    assert (drain.holding(), drain.starting(), seen) == (False, False, ["ended"])


def test_a_drain_during_the_start_owns_its_state_and_a_resume_keeps_the_start():
    async def scene():
        drain.hold_for_start()
        await drain.drain(30)
        await drain.resume()
        assert (drain.state(), drain.holding()) == ("running", True)
        await drain.drain(30)
        await drain.end_start_hold()
        assert (drain.state(), drain.holding(), drain.starting()) == ("draining", True, False)
        await drain.resume()
        assert drain.holding() is False
    asyncio.run(scene())


def test_the_start_dropped_at_the_stop_runs_no_hook():
    seen: list[str] = []
    drain.on_start_ended(lambda: seen.append("ended"))
    drain.hold_for_start()
    drain.drop_start_hold()
    assert (drain.starting(), seen) == (False, [])


def test_a_reset_forgets_the_start():
    drain.hold_for_start()
    drain.on_start_ended(lambda: None)
    drain.reset_for_tests()
    assert drain.starting() is False and drain._hooks["start_ended"] == []



# ── the one busy-or-idle answer ───────────────────────────────────────────


def test_work_answers_holding_starting_sessions_and_idle(monkeypatch):
    now = {"t": 100.0}
    monkeypatch.setattr(drain, "_clock", lambda: now["t"])
    drain.register_sessions(lambda: 3)
    first = drain.work()
    assert (first["holding"], first["starting"], first["sessions"], first["idle_s"]) == (
        False, False, 3, 0)
    now["t"] = 160.0
    assert drain.work()["idle_s"] == 60
    drain.hold_for_start()
    assert (drain.work()["holding"], drain.work()["starting"], drain.work()["idle_s"]) == (True, True, 0)


def test_idle_follows_the_drains_own_rule_for_background_work(monkeypatch):
    """A server an agent left running never ends: still for a minute, it
    keeps nothing busy, as the drain does not wait for it either."""
    now = {"t": 0.0}
    monkeypatch.setattr(drain, "_clock", lambda: now["t"])
    counts = {"turns": 0, "background": 1}
    drain.register_counter("turns", lambda: counts["turns"])
    drain.register_counter("background", lambda: counts["background"])
    assert drain.note_activity() == 0
    now["t"] = drain.BACKGROUND_STILL_S - 1
    assert drain.note_activity() == 0, "background work that moved lately still works"
    now["t"] = drain.BACKGROUND_STILL_S + 1
    assert drain.note_activity() == 2
    now["t"] += 120
    assert drain.note_activity() == 122
    counts["turns"] = 1
    assert drain.note_activity() == 0
    counts["turns"] = 0
    now["t"] += 30
    assert drain.note_activity() == 30


def test_a_queued_message_keeps_the_platform_busy(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr(drain, "_clock", lambda: now["t"])
    queued = {"n": 1}
    drain.register_queued(lambda: queued["n"])
    drain.note_activity()
    now["t"] = 500
    assert drain.note_activity() == 0
    queued["n"] = 0
    now["t"] = 530
    assert drain.note_activity() == 30


def test_a_turn_waiting_for_a_person_keeps_idle_at_zero(monkeypatch):
    """An approval card up is no idle install: the person may answer the
    next moment. The drain still does not wait for it (``busy`` stays false);
    ``idle_s`` counts again once the card is answered."""
    now = {"t": 0.0}
    monkeypatch.setattr(drain, "_clock", lambda: now["t"])
    waiting = {"n": 1}
    drain.register_waiting(lambda: waiting["n"])
    drain.note_activity()
    now["t"] = 900
    assert drain.note_activity() == 0
    assert drain.work()["busy"] is False and drain.work()["waiting_on_people"] == 1
    waiting["n"] = 0
    now["t"] = 960
    assert drain.note_activity() == 60
