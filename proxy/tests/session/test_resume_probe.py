"""``core/session/resume_probe.settle_resume``: the wait on an undecided
resume probe, by where the machine stands, within a budget; and the probe
left out for a deferred turn's watcher (``await_resume_answer``)."""

from __future__ import annotations

import asyncio

import pytest

from core.execution_layer import MachineReach, ResumeVerdict
from core.session import resume_probe
from core.session.resume_probe import (
    ProbeWaitEnd,
    ResumeUndecided,
    await_resume_answer,
    settle_resume,
)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Layer:
    """A layer whose probe answers from a script, one verdict per call, and
    whose machine stands where ``reaches`` says (the last entry repeats);
    the outstanding probe's future is ``future`` when the test sets one."""

    def __init__(self, verdicts, reaches=(MachineReach.CONNECTED,)):
        self.verdicts = list(verdicts)
        self.reaches = list(reaches)
        self.calls = 0
        self.reach_calls = 0
        self.forgotten = 0
        self.future: asyncio.Future | None = None

    async def can_resume_session(self, sid, *, agent_name="", username="", external_home=""):
        self.calls += 1
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]

    def resume_machine_reach(self, sid, *, agent_name="", username=""):
        self.reach_calls += 1
        return self.reaches.pop(0) if len(self.reaches) > 1 else self.reaches[0]

    def resume_probe_future(self, sid, *, agent_name="", username=""):
        return self.future

    def forget_resume_probe(self, sid, *, agent_name="", username=""):
        self.forgotten += 1
        self.future = None


def _settle(layer, budget_s=90.0, **kw):
    clock = _Clock()
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)
        clock.now += s

    return settle_resume(layer, "sess-1", budget_s=budget_s, clock=clock, sleep=sleep, **kw), slept


@pytest.mark.asyncio
async def test_a_definite_answer_returns_at_once():
    for verdict in (ResumeVerdict.RESUMABLE, ResumeVerdict.FRESH):
        layer = _Layer([verdict])
        coro, slept = _settle(layer)
        assert await coro is verdict
        assert layer.calls == 1 and slept == [] and layer.forgotten == 0


@pytest.mark.asyncio
async def test_a_silent_machine_is_asked_again_until_it_answers():
    layer = _Layer([ResumeVerdict.UNKNOWN, ResumeVerdict.UNKNOWN, ResumeVerdict.RESUMABLE])
    coro, slept = _settle(layer)
    assert await coro is ResumeVerdict.RESUMABLE
    assert layer.calls == 3
    assert slept == [resume_probe._PROBE_PAUSE_S, resume_probe._PROBE_PAUSE_S]


@pytest.mark.asyncio
async def test_the_budget_ends_in_unknown_never_fresh_and_forgets_the_probe():
    layer = _Layer([ResumeVerdict.UNKNOWN])
    coro, slept = _settle(layer, budget_s=7.0)
    assert await coro is ResumeVerdict.UNKNOWN
    # 7 s of budget at a 2 s pause: probes at 0, 2, 4 and 6 s, the last pause
    # cut to the deadline, and one more probe that finds the budget spent.
    assert layer.calls == 5
    assert sum(slept) == pytest.approx(7.0)
    assert layer.forgotten == 1


@pytest.mark.asyncio
async def test_a_kept_probe_stays_out_for_the_watcher_on_a_connected_machine():
    layer = _Layer([ResumeVerdict.UNKNOWN])
    coro, _slept = _settle(layer, budget_s=3.0, keep_probe=True)
    assert await coro is ResumeVerdict.UNKNOWN
    assert layer.forgotten == 0
    # Kept only while the machine is connected: an away one forgets anyway.
    layer = _Layer([ResumeVerdict.UNKNOWN], reaches=[MachineReach.AWAY])
    coro, _slept = _settle(layer, keep_probe=True)
    assert await coro is ResumeVerdict.UNKNOWN
    assert layer.forgotten == 1


@pytest.mark.asyncio
async def test_a_machine_dropped_a_moment_ago_is_waited_for_then_asked():
    layer = _Layer(
        [ResumeVerdict.UNKNOWN, ResumeVerdict.RESUMABLE],
        reaches=[MachineReach.AWAY_RECENT, MachineReach.AWAY_RECENT, MachineReach.CONNECTED],
    )
    coro, slept = _settle(layer)
    assert await coro is ResumeVerdict.RESUMABLE
    assert layer.calls == 2
    assert slept == [resume_probe._MACHINE_POLL_S, resume_probe._MACHINE_POLL_S]


@pytest.mark.asyncio
async def test_a_machine_away_past_its_grace_is_refused_at_once():
    layer = _Layer([ResumeVerdict.UNKNOWN], reaches=[MachineReach.AWAY])
    coro, slept = _settle(layer)
    assert await coro is ResumeVerdict.UNKNOWN
    assert layer.calls == 1 and slept == []
    assert layer.forgotten == 1


@pytest.mark.asyncio
async def test_a_machine_that_never_returns_ends_the_budget_unknown():
    layer = _Layer([ResumeVerdict.UNKNOWN], reaches=[MachineReach.AWAY_RECENT])
    coro, slept = _settle(layer, budget_s=5.0, keep_probe=True)
    assert await coro is ResumeVerdict.UNKNOWN
    assert layer.calls == 1
    assert sum(slept) == pytest.approx(5.0)
    assert layer.forgotten == 1   # kept for no watcher: the machine is not connected


@pytest.mark.asyncio
async def test_a_local_disk_fault_is_retried_once_then_undecided():
    # Never fresh: a fresh session over an intact file is irreversible, and a
    # disk that failed twice decides nothing.
    layer = _Layer([ResumeVerdict.UNKNOWN], reaches=[MachineReach.LOCAL])
    coro, slept = _settle(layer)
    assert await coro is ResumeVerdict.UNKNOWN
    assert layer.calls == 2
    assert slept == [resume_probe._LOCAL_RETRY_S]


@pytest.mark.asyncio
async def test_a_layer_without_a_reach_is_the_platforms():
    class _Bare:
        def __init__(self):
            self.calls = 0

        async def can_resume_session(self, sid, **kw):
            self.calls += 1
            return ResumeVerdict.UNKNOWN

    layer = _Bare()
    coro, _slept = _settle(layer)
    assert await coro is ResumeVerdict.UNKNOWN
    assert layer.calls == 2


def test_the_undecided_error_names_the_session():
    err = ResumeUndecided("abcdefgh-1234")
    assert err.session_id == "abcdefgh-1234" and not err.local
    assert "abcdefgh" in str(err)
    local = ResumeUndecided("abcdefgh-1234", local=True)
    assert local.local and "session files" in str(local)


@pytest.mark.asyncio
async def test_the_watcher_waits_for_the_kept_probes_answer_then_forgets_it():
    layer = _Layer([ResumeVerdict.UNKNOWN])
    layer.future = asyncio.get_running_loop().create_future()

    async def _answer_soon():
        await asyncio.sleep(0.05)
        layer.future.set_result({"resumable": True})
    task = asyncio.create_task(_answer_soon())
    assert await await_resume_answer(layer, "sess-1", timeout_s=5.0) is ProbeWaitEnd.ANSWERED
    await task
    assert layer.forgotten == 1 and layer.future is None


@pytest.mark.asyncio
async def test_the_watcher_gives_up_after_its_wait_and_a_dropped_machine_is_the_reconnects():
    layer = _Layer([ResumeVerdict.UNKNOWN])
    layer.future = asyncio.get_running_loop().create_future()
    assert await await_resume_answer(layer, "sess-1", timeout_s=0.05) is ProbeWaitEnd.TIMED_OUT
    assert layer.forgotten == 1
    layer.future = asyncio.get_running_loop().create_future()
    layer.future.set_exception(RuntimeError("the machine dropped"))
    assert await await_resume_answer(layer, "sess-1", timeout_s=1.0) is ProbeWaitEnd.DROPPED
    # No probe out at all: the whole wait, then a re-arm as if it timed out.
    layer.future = None
    assert await await_resume_answer(layer, "sess-1", timeout_s=0.02) is ProbeWaitEnd.TIMED_OUT
