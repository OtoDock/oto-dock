"""task_produce bg-work review-turn decision.

The producer must send the "review the output and continue" nudge only for
background work the model has NOT seen: bash completions resolved after the
model's final message (settle drain / post-turn drain), or a completed
subagent cohort (spawn-tally contract). Still-RUNNING work must never be
claimed "completed": a never-exiting backgrounded shell once held a task run
in a 2-minute false-nudge loop for hours (144 turns, run-54edb191839d) until
a manual abort. The review phase is bounded — per-type give-up ceilings
re-armed on observed completions, a stall-nudge for completed work stuck
behind a never-ending sibling, and a max-rounds belt — and it suppresses the
chat-path bg monitors (it owns completion handling; the lock is taken per
send, so an armed monitor would otherwise race the producer's drains).
"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from core.events.common_events import (
    CommonEvent, SUBAGENT_START, BG_COMMAND_START, QUEUE_TURN,
)
from core.events.bg_command_state import (
    _bg_command_registries, get_bg_command_registry,
)
from core.events.pump_bg_monitors import (
    bg_monitor_running, bg_command_monitor_running,
)
from core.events.task_producer import task_produce
from core.session.session_state import get_subagent_registry


class _FakeLayer:
    """Minimal ExecutionLayer stand-in: canned event lists per send_message.

    ``drain_bg_commands`` mirrors the real layers' locking contract: it
    timeout-acquires the SAME lock ``session_lock`` yields. A producer that
    (wrongly) holds the lock across the review phase turns every drain into a
    permanent no-op — so any test whose command resolves via ``on_drain``
    doubles as proof the producer releases the lock between sends.
    """

    def __init__(self, turns, on_drain=None, on_nudge_turn=None):
        self.turns = list(turns)
        self.prompts: list[str] = []
        self.on_drain = on_drain            # fn(call_no) -> progressed: bool
        self.on_nudge_turn = on_nudge_turn  # fn(nudge_no) at each nudge send
        self.drain_calls = 0
        self._locks: dict = {}

    def _lock(self, sid) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    @asynccontextmanager
    async def session_lock(self, session_id):
        async with self._lock(session_id):
            yield

    async def send_message(self, session_id, prompt, **kw):
        self.prompts.append(prompt)
        if len(self.prompts) > 1 and self.on_nudge_turn:
            self.on_nudge_turn(len(self.prompts) - 1)
        for event in (self.turns.pop(0) if self.turns else []):
            yield event

    async def wait_for_bg_subagents(self, session_id, timeout=120.0):
        await asyncio.sleep(min(timeout, 0.01))
        return 0

    async def drain_bg_commands(self, session_id, *, budget=2.0):
        lock = self._lock(session_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return False
        try:
            self.drain_calls += 1
            if self.on_drain:
                return bool(self.on_drain(self.drain_calls))
            return False
        finally:
            lock.release()

    async def is_session_alive(self, session_id):
        return True


def _bg_cmd_start():
    return CommonEvent(type=BG_COMMAND_START, data={})


def _bg_sub_start():
    return CommonEvent(type=SUBAGENT_START, data={"run_in_background": True})


async def _run(layer, session_id, **kw):
    queue: asyncio.Queue = asyncio.Queue()
    kw.setdefault("bg_poll", 0.02)
    kw.setdefault("bg_cmd_ceiling", 0.3)
    kw.setdefault("bg_sub_ceiling", 0.3)
    kw.setdefault("bg_stall_nudge", 10.0)  # off unless a test opts in
    await task_produce(layer, session_id, "do the task", queue, "run12345", **kw)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _nudges(events):
    return [e for e in events if e.type == QUEUE_TURN]


@pytest.fixture
def clean_registries():
    """Unique-session tests still clean up so nothing leaks across runs."""
    created: list[str] = []
    yield created
    for sid in created:
        _bg_command_registries.pop(sid, None)
        reg = get_subagent_registry(sid)
        reg.spawned.clear()
        reg.completed.clear()


@pytest.mark.asyncio
async def test_all_commands_surfaced_inline_skips_nudge(clean_registries):
    """Every bash completion was read by the model during the turn → the
    producer must return after the main turn, no nudge."""
    sid = "tp-surfaced"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-a", "t1")
    bgreg.register_spawn("cmd-b", "t2")
    bgreg.mark_done("cmd-a")  # surfaced (default) — mid-generation resolve
    bgreg.mark_done("cmd-b")

    layer = _FakeLayer(turns=[[_bg_cmd_start(), _bg_cmd_start()]])
    events = await _run(layer, sid)

    assert layer.prompts == ["do the task"]
    assert not _nudges(events)


@pytest.mark.asyncio
async def test_settle_resolved_command_still_nudges(clean_registries):
    """A completion the model never saw (settle/post-turn resolve) keeps the
    delegation contract: exactly one review turn."""
    sid = "tp-unsurfaced"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-a", "t1")
    bgreg.mark_done("cmd-a", surfaced=False)

    layer = _FakeLayer(turns=[[_bg_cmd_start()], []])
    events = await _run(layer, sid)

    assert len(layer.prompts) == 2
    assert "1 background command(s)" in layer.prompts[1]
    assert len(_nudges(events)) == 1


@pytest.mark.asyncio
async def test_pending_command_drain_resolves_then_nudges(clean_registries):
    """Still-running command at turn end: the producer's post-turn DRAIN
    resolves it (which requires the session lock to be free between sends —
    the fake's drain no-ops under a held lock), then one review turn."""
    sid = "tp-pending"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-a", "t1")

    layer = _FakeLayer(
        turns=[[_bg_cmd_start()], []],
        on_drain=lambda n: bgreg.mark_done("cmd-a", surfaced=False) if n >= 2 else False,
    )
    events = await _run(layer, sid)

    assert len(layer.prompts) == 2
    assert "1 background command(s)" in layer.prompts[1]
    assert len(_nudges(events)) == 1


@pytest.mark.asyncio
async def test_pending_command_never_resolves_gives_up_without_nudge(clean_registries):
    """The incident shape: a command that never exits. The producer must wait
    out ONE bounded ceiling, then finish WITHOUT nudging — never a false
    "completed their work" turn, never an unbounded loop."""
    sid = "tp-stuck"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-stuck", "t1")

    layer = _FakeLayer(turns=[[_bg_cmd_start()]])
    events = await _run(layer, sid)

    assert layer.prompts == ["do the task"]
    assert not _nudges(events)
    assert layer.drain_calls > 0  # it actively tried to observe completion


@pytest.mark.asyncio
async def test_bg_subagent_never_completes_gives_up(clean_registries):
    """A pending bg subagent whose SubagentStop never arrives: bounded wait,
    then give-up without a nudge (the old code nudged 'completed' forever)."""
    sid = "tp-sub-stuck"
    clean_registries.append(sid)
    reg = get_subagent_registry(sid)
    reg.register_spawn("sub-a", "t1")

    layer = _FakeLayer(turns=[[_bg_sub_start()]])
    events = await _run(layer, sid)

    assert layer.prompts == ["do the task"]
    assert not _nudges(events)


@pytest.mark.asyncio
async def test_bg_subagent_spawn_tally_still_nudges(clean_registries):
    """Background SUBAGENTS keep the spawn-tally contract: a bg spawn forces
    the review turn even with both registries drained."""
    sid = "tp-subagent"
    clean_registries.append(sid)

    layer = _FakeLayer(turns=[[_bg_sub_start()], []])
    events = await _run(layer, sid)

    assert len(layer.prompts) == 2
    assert "1 background agent(s)" in layer.prompts[1]
    # Bash didn't participate — the nudge must not mention commands.
    assert "command(s)" not in layer.prompts[1]
    assert len(_nudges(events)) == 1


@pytest.mark.asyncio
async def test_mixed_agents_done_command_stuck_stall_nudges(clean_registries):
    """Completed agents must not wait out a stuck command's full ceiling:
    after the stall window their review turn fires (agents only — the nudge
    never counts the still-running command), then the command's ceiling
    passes and the producer finishes without a second nudge."""
    sid = "tp-mixed"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-stuck", "t1")

    layer = _FakeLayer(turns=[[_bg_sub_start(), _bg_cmd_start()], []])
    events = await _run(layer, sid, bg_stall_nudge=0.05, bg_cmd_ceiling=0.4)

    assert len(layer.prompts) == 2
    assert "1 background agent(s)" in layer.prompts[1]
    assert "command(s)" not in layer.prompts[1]
    assert len(_nudges(events)) == 1


@pytest.mark.asyncio
async def test_completion_rearms_deadline(clean_registries):
    """Completions trickling in past the initial ceiling keep the phase
    alive: each observed completion re-arms the horizon, so a legit chain of
    slow commands resolves fully and gets its (single) review turn."""
    sid = "tp-rearm"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-a", "t1")
    bgreg.register_spawn("cmd-b", "t2")

    import time as _time
    t0: list[float] = []

    def on_drain(n):
        now = _time.monotonic()
        if not t0:
            t0.append(now)
        elapsed = now - t0[0]
        # cmd-a resolves inside the initial 0.6s ceiling; cmd-b only lands
        # ~0.8s in — past the ORIGINAL horizon, inside the re-armed one
        # (cmd-a's completion pushed the deadline to ~1.05s).
        if elapsed > 0.45 and "cmd-a" not in bgreg.completed:
            return bgreg.mark_done("cmd-a", surfaced=False)
        if elapsed > 0.80 and "cmd-a" in bgreg.completed:
            return bgreg.mark_done("cmd-b", surfaced=False)
        return False

    layer = _FakeLayer(turns=[[_bg_cmd_start(), _bg_cmd_start()], []],
                       on_drain=on_drain)
    events = await _run(layer, sid, bg_cmd_ceiling=0.6)

    assert len(layer.prompts) == 2
    assert "2 background command(s)" in layer.prompts[1]
    assert len(_nudges(events)) == 1


@pytest.mark.asyncio
async def test_max_rounds_cap(clean_registries):
    """A review turn that spawns-and-resolves new bg work every round is
    capped by the max-rounds belt instead of looping forever."""
    sid = "tp-rounds"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-0", "t0")
    bgreg.mark_done("cmd-0", surfaced=False)

    def on_nudge_turn(n):
        bgreg.register_spawn(f"cmd-{n}", f"t{n}")
        bgreg.mark_done(f"cmd-{n}", surfaced=False)

    layer = _FakeLayer(turns=[[_bg_cmd_start()]] + [[]] * 10,
                       on_nudge_turn=on_nudge_turn)
    events = await _run(layer, sid, bg_max_rounds=3)

    assert len(layer.prompts) == 1 + 3
    assert len(_nudges(events)) == 3


@pytest.mark.asyncio
async def test_second_round_does_not_remention_reviewed_agents(clean_registries):
    """Agents reviewed in round 1 must not be re-listed when a later round
    nudges for a command completion."""
    sid = "tp-two-rounds"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)

    def on_nudge_turn(n):
        if n == 1:  # the agents' review turn leaves an unseen command behind
            bgreg.register_spawn("cmd-late", "t9")
            bgreg.mark_done("cmd-late", surfaced=False)

    layer = _FakeLayer(turns=[[_bg_sub_start()], [], []],
                       on_nudge_turn=on_nudge_turn)
    events = await _run(layer, sid)

    assert len(layer.prompts) == 3
    assert "1 background agent(s)" in layer.prompts[1]
    assert "1 background command(s)" in layer.prompts[2]
    assert "agent(s)" not in layer.prompts[2]
    assert len(_nudges(events)) == 2


@pytest.mark.asyncio
async def test_producer_suppresses_chat_bg_monitors(clean_registries):
    """While task_produce runs, both chat-path bg monitors read as already
    running for the session (idempotency-guard suppression) — and the hold is
    released afterwards."""
    sid = "tp-monitors"
    clean_registries.append(sid)
    seen: list[tuple[bool, bool]] = []

    class _Layer(_FakeLayer):
        async def send_message(self, session_id, prompt, **kw):
            seen.append((bg_monitor_running(session_id),
                         bg_command_monitor_running(session_id)))
            async for e in super().send_message(session_id, prompt, **kw):
                yield e

    layer = _Layer(turns=[[]])
    await _run(layer, sid)

    assert seen == [(True, True)]
    assert not bg_monitor_running(sid)
    assert not bg_command_monitor_running(sid)


@pytest.mark.asyncio
async def test_no_bg_work_no_nudge(clean_registries):
    sid = "tp-none"
    clean_registries.append(sid)
    layer = _FakeLayer(turns=[[]])
    events = await _run(layer, sid)
    assert layer.prompts == ["do the task"]
    assert not _nudges(events)
