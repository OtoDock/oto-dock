"""The resume probe, settled: one answer for a dead session's conversation.

``ExecutionLayer.can_resume_session`` answers a ``ResumeVerdict``; the
remote layer answers ``UNKNOWN`` while the session's machine is away or
silent, and the local one when its disk misread the file. Every path that
decides between ``--resume`` and a fresh session calls ``settle_resume``
here, which waits on an UNKNOWN within a budget instead of reading it as
fresh: the fresh branch is irreversible (a chat's intact transcript replaced
by a digest because a probe timed out), so only a definite answer takes it.

The wait, by where the machine stands (``ExecutionLayer.resume_machine_reach``):

* ``LOCAL``: one retry after a second, then UNKNOWN (a local disk fault twice
  is a refusal too, never a fresh session over an intact file).
* ``CONNECTED`` but silent: probe again every ``_PROBE_PAUSE_S`` (the remote
  adapter keeps one probe outstanding, so this re-sends nothing) until an
  answer or the budget ends.
* ``AWAY_RECENT`` (dropped within its reconnect grace): poll until it is
  back, then probe again.
* ``AWAY`` (gone past the grace, or paused): UNKNOWN at once, no wait.

A caller that gets UNKNOWN refuses the turn and leaves the chat as it is
(``ResumeUndecided`` carries that refusal where an exception fits): nothing
is flagged, no digest is claimed, the next send asks again. The probe the
machine has not answered is forgotten at the budget's end unless the caller
asked to keep it (``keep_probe``): a deferred turn's watcher then waits on
it (``await_resume_answer``) and re-arms the turn when it answers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum

from core.execution_layer import (
    RECONNECT_WAIT_S,
    ExecutionLayer,
    MachineReach,
    ResumeVerdict,
)

logger = logging.getLogger("otodock")

# The background warmup: covers a machine that dropped a moment ago (its
# grace) and one connected but slow.
RESUME_PROBE_BUDGET_WARMUP_S = 90.0
# A probe on the socket's own dispatch (a send's turn start) or with a person
# on the line (the duplex voice heal, the phone heal).
RESUME_PROBE_BUDGET_INLINE_S = RECONNECT_WAIT_S
# A delegate continue, a one-shot delivery: nobody waits on the line.
RESUME_PROBE_BUDGET_BACKGROUND_S = 45.0
# A turn deferred on an unanswered probe of a connected machine waits this
# long for the answer before it is re-armed anyway (``ws/resume_wait``).
RESUME_REARM_WAIT_S = 60.0

_PROBE_PAUSE_S = 2.0
_MACHINE_POLL_S = 1.0
_LOCAL_RETRY_S = 1.0


class ResumeUndecided(RuntimeError):
    """The probe gave no answer within its budget: the turn is refused and
    nothing of the chat changed. ``local``: the session's files are on the
    platform and its disk could not read them (no machine to wait for)."""

    def __init__(self, session_id: str, *, local: bool = False) -> None:
        what = ("the session files of" if local
                else "the machine of session")
        super().__init__(f"{what} {session_id[:8]} did not answer the resume probe")
        self.session_id = session_id
        self.local = local


class ProbeWaitEnd(Enum):
    """How ``await_resume_answer`` ended."""

    ANSWERED = "answered"
    TIMED_OUT = "timed_out"
    DROPPED = "dropped"


async def settle_resume(
    layer: ExecutionLayer, session_id: str, *, agent_name: str = "",
    username: str = "", external_home: str = "", budget_s: float,
    keep_probe: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ResumeVerdict:
    """The verdict on ``session_id``'s conversation, waited for within
    ``budget_s`` seconds as the module docstring says. On an UNKNOWN end the
    outstanding probe is forgotten, unless ``keep_probe`` and the machine is
    connected: the caller's watcher takes it. ``clock`` and ``sleep`` are the
    tests' seams."""
    deadline = clock() + budget_s
    probes = 0
    reach = MachineReach.LOCAL
    while True:
        verdict = await layer.can_resume_session(
            session_id, agent_name=agent_name, username=username,
            external_home=external_home,
        )
        probes += 1
        if verdict is not ResumeVerdict.UNKNOWN:
            return verdict
        reach = _reach(layer, session_id, agent_name, username)
        if reach is MachineReach.LOCAL:
            if probes >= 2:
                logger.warning(
                    "resume of %s refused: its session files could not be read twice",
                    session_id[:8],
                )
                return ResumeVerdict.UNKNOWN
            await sleep(_LOCAL_RETRY_S)
            continue
        if reach is MachineReach.AWAY:
            logger.info(
                "resume of %s refused: its machine is away past the reconnect grace",
                session_id[:8],
            )
            _forget(layer, session_id, agent_name, username)
            return ResumeVerdict.UNKNOWN
        remaining = deadline - clock()
        if remaining <= 0:
            logger.info(
                "resume of %s undecided after %.0fs (%d probes, machine %s)",
                session_id[:8], budget_s, probes, reach.value,
            )
            if not (keep_probe and reach is MachineReach.CONNECTED):
                _forget(layer, session_id, agent_name, username)
            return ResumeVerdict.UNKNOWN
        if reach is MachineReach.AWAY_RECENT:
            logger.info(
                "resume of %s waits: the machine dropped a moment ago (%.0fs left)",
                session_id[:8], remaining,
            )
            while clock() < deadline:
                await sleep(min(_MACHINE_POLL_S, max(deadline - clock(), 0.0)))
                reach = _reach(layer, session_id, agent_name, username)
                if reach is not MachineReach.AWAY_RECENT:
                    break
            if reach is MachineReach.AWAY:
                _forget(layer, session_id, agent_name, username)
                return ResumeVerdict.UNKNOWN
            if clock() >= deadline:
                logger.info(
                    "resume of %s undecided after %.0fs: the machine did not return",
                    session_id[:8], budget_s,
                )
                _forget(layer, session_id, agent_name, username)
                return ResumeVerdict.UNKNOWN
            continue
        logger.info(
            "resume of %s waits: the machine is connected but has not answered (%.0fs left)",
            session_id[:8], remaining,
        )
        await sleep(min(_PROBE_PAUSE_S, remaining))


async def await_resume_answer(
    layer: ExecutionLayer, session_id: str, *, agent_name: str = "",
    username: str = "", timeout_s: float = RESUME_REARM_WAIT_S,
) -> ProbeWaitEnd:
    """Wait, at most ``timeout_s``, for the answer to the probe
    ``settle_resume`` left outstanding (``keep_probe``), then forget it. A
    layer with no probe out waits the whole time (the machine may answer a
    probe the next turn sends). DROPPED: the machine went (the reconnect
    pass re-arms what waited on it)."""
    getter = getattr(layer, "resume_probe_future", None)
    future = getter(session_id, agent_name=agent_name, username=username) if getter else None
    try:
        if future is None:
            await asyncio.sleep(timeout_s)
            return ProbeWaitEnd.TIMED_OUT
        done, _pending = await asyncio.wait({future}, timeout=timeout_s)
        if not done:
            return ProbeWaitEnd.TIMED_OUT
        if future.cancelled() or future.exception() is not None:
            return ProbeWaitEnd.DROPPED
        return ProbeWaitEnd.ANSWERED
    finally:
        _forget(layer, session_id, agent_name, username)


def _forget(layer, session_id: str, agent_name: str, username: str) -> None:
    forget = getattr(layer, "forget_resume_probe", None)
    if forget is not None:
        forget(session_id, agent_name=agent_name, username=username)


def _reach(layer, session_id: str, agent_name: str, username: str) -> MachineReach:
    probe = getattr(layer, "resume_machine_reach", None)
    if probe is None:
        return MachineReach.LOCAL
    return probe(session_id, agent_name=agent_name, username=username)
