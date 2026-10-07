"""Meeting turn text collection + thin-turn auto-restate.

Found 2026-07-11 filming: participants gathered data with tools, left their
findings in (non-streamed) thinking, and called ``direct_to`` believing the
report was delivered — the moderator received only meta-lines and had to ask
everyone to restate (a wasted round in every affected meeting). Only response
TEXT is relayed, so the orchestrator now (a) tracks ``tail_text`` — chars of
text after the last data-tool result — and auto-queues a one-shot "restate"
turn when a tool-using turn wrote none, and (b) suppresses post-meeting-tool
transcript echo again (the matcher checked the UUID ``tool_id`` before
``name``, so the documented suppression never fired).
"""

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.events.common_events import (
    CommonEvent, TEXT, TOOL_USE, TOOL_INPUT, TOOL_RESULT,
)
from services.meetings import meeting_orchestrator as MO
from services.meetings.meeting_context import build_turn_prompt


# ---------------------------------------------------------------------------
# Scripted-layer harness for the live turn runner
# ---------------------------------------------------------------------------

def _text(s):
    return CommonEvent(type=TEXT, data={"content": s})


def _tool_use(name):
    return CommonEvent(type=TOOL_USE, data={"name": name, "tool_id": "toolu_use"})


def _tool_input(name, **ti):
    return CommonEvent(type=TOOL_INPUT, data={"name": name, "tool_input": ti})


def _tool_result(name, tool_id="toolu_01AbCdEfGhIjKlMnOp"):
    # tool_id is deliberately UUID-shaped: the stream's tool_end always
    # carries one, and matching it before `name` is what killed suppression.
    return CommonEvent(type=TOOL_RESULT, data={"name": name, "tool_id": tool_id})


class _ScriptedLayer:
    def __init__(self, events):
        self._events = list(events)

    def session_lock(self, sid):
        return asyncio.Lock()

    async def send_message(self, sid, prompt):
        for ev in self._events:
            yield ev


async def _run_turn(events):
    meeting = {
        "id": "m1", "topic": "t", "moderator": "mod",
        "parent_chat_id": "chat-relay-1",
        "participants": json.dumps(["mod", "p1"]),
    }
    MO._meeting_session_layers["sid-1"] = _ScriptedLayer(events)
    try:
        return await MO._run_live_turn(
            "p1", {"p1": "sid-1"}, meeting,
            [], {}, asyncio.Queue(), "m1",
        )
    finally:
        MO._meeting_session_layers.pop("sid-1", None)


DIRECT_TO = "mcp__meetings-mcp__direct_to"
DATA_TOOL = "mcp__uptime-kuma__getMonitorSummary"


@pytest.mark.asyncio
async def test_post_meeting_tool_text_suppressed_despite_uuid_tool_id():
    result = await _run_turn([
        _text("Here is my report."),
        _tool_use(DIRECT_TO),
        _tool_input(DIRECT_TO, agents=["mod"]),
        _tool_result(DIRECT_TO),
        _text("TRANSCRIPT ECHO JUNK"),
    ])
    assert result.content == "Here is my report."
    assert result.directed_to == ["mod"]


@pytest.mark.asyncio
async def test_tail_text_zero_when_no_findings_after_tools():
    # The filmed failure shape: intro → data tools → direct_to with no
    # written findings → post-routing meta line (now suppressed too).
    result = await _run_turn([
        _text("Let me pull the current monitor status."),
        _tool_use(DATA_TOOL),
        _tool_result(DATA_TOOL),
        _tool_use(DIRECT_TO),
        _tool_input(DIRECT_TO, agents=["mod"]),
        _tool_result(DIRECT_TO),
        _text("I've delivered my infra report."),
    ])
    assert result.tail_text == 0
    assert result.content == "Let me pull the current monitor status."
    assert any(t["name"] == DATA_TOOL for t in result.tools)


@pytest.mark.asyncio
async def test_tail_text_survives_toolsearch_and_meeting_results():
    # Models legitimately run ToolSearch (schema load) between the written
    # report and direct_to — neither it nor the meeting tool's own result
    # may zero the tail.
    result = await _run_turn([
        _tool_use(DATA_TOOL),
        _tool_result(DATA_TOOL),
        _text("FINDINGS: all monitors green."),
        _tool_use("ToolSearch"),
        _tool_result("ToolSearch"),
        _tool_use(DIRECT_TO),
        _tool_input(DIRECT_TO, agents=["mod"]),
        _tool_result(DIRECT_TO),
    ])
    assert result.tail_text == len("FINDINGS: all monitors green.")


# ---------------------------------------------------------------------------
# Thin-turn auto-restate in meeting_produce
# ---------------------------------------------------------------------------

def _meeting_row():
    return {
        "id": "m1", "status": "active", "topic": "t",
        "moderator": "mod", "max_turns": 30,
        "parent_chat_id": "chat-relay-2",
        "active_participants": json.dumps(["mod", "p1"]),
        "participants": json.dumps(["mod", "p1"]),
    }


def _result(agent, content, *, tools=(), tail=None, directed=None, called=()):
    return MO.TurnResult(
        agent=agent, events=[], content=content,
        tools=[{"name": n} for n in tools],
        directed_to=list(directed) if directed is not None else None,
        tools_called=set(called),
        tail_text=len(content) if tail is None else tail,
    )


async def _drive_meeting(scripted_results):
    """Run meeting_produce with scripted TurnResults; returns the per-turn
    (agent, pending_snapshot) the runner saw, plus the saved turns."""
    results = deque(scripted_results)
    calls: list[tuple[str, list]] = []
    saved: list[tuple] = []

    async def fake_live(agent_slug, agent_sessions, meeting, transcript,
                        pending, event_queue, meeting_id):
        calls.append((agent_slug, list(pending.get(agent_slug, []))))
        return results.popleft()

    with patch.object(MO, "_run_live_turn", new=fake_live), \
         patch.object(MO.task_store, "get_meeting", return_value=_meeting_row()), \
         patch.object(MO.task_store, "update_meeting"), \
         patch.object(MO.task_store, "add_meeting_turn",
                      side_effect=lambda *a: saved.append(a)):
        pump = SimpleNamespace(chat_id="chat-mtg", system_queue=[])
        await MO.meeting_produce(
            "m1", {"mod": "s-mod", "p1": "s-p1"}, asyncio.Queue(), pump)
    return calls, saved


@pytest.mark.asyncio
async def test_thin_turn_gets_restated_before_routing():
    findings = "FINDINGS: nobody home until 18:00; window 14:00-16:00 is clear." * 2
    calls, saved = await _drive_meeting([
        _result("mod", "Please report on the maintenance window.", directed=["p1"]),
        # p1 runs a data tool and writes nothing after it — thin.
        _result("p1", "Let me pull the data.", tools=(DATA_TOOL,), tail=0,
                directed=["mod"]),
        # p1's restate turn — didn't call direct_to again; must inherit ["mod"].
        _result("p1", findings, tail=0),
        _result("mod", "Summary.", called=("end_meeting",)),
    ])

    agents = [c[0] for c in calls]
    assert agents == ["mod", "p1", "p1", "mod"]
    # The third turn was queued as a restate…
    assert calls[2][1] == [{"type": "restate"}]
    # …and the moderator was queued only AFTER it, receiving the restated
    # findings (not the thin entry alone).
    mod_pending = calls[3][1]
    assert any(e.get("content") == findings for e in mod_pending)
    # Both entries were still saved as normal meeting turns.
    assert len(saved) == 4


@pytest.mark.asyncio
async def test_restate_fires_only_once_per_agent():
    calls, _ = await _drive_meeting([
        _result("mod", "Please report.", directed=["p1"]),
        _result("p1", "Pulling data.", tools=(DATA_TOOL,), tail=0, directed=["mod"]),
        # The restate is ALSO thin — must route anyway (no loop).
        _result("p1", "Still nothing.", tools=(DATA_TOOL,), tail=0),
        _result("mod", "Summary.", called=("end_meeting",)),
    ])
    agents = [c[0] for c in calls]
    assert agents == ["mod", "p1", "p1", "mod"]
    assert calls[2][1] == [{"type": "restate"}]
    # Moderator got the (still thin) restated entry — meeting proceeds.
    assert any(e.get("content") == "Still nothing." for e in calls[3][1])


@pytest.mark.asyncio
async def test_zero_text_turn_restated_regardless_of_tool_mix():
    # Observed live (mtg-05a1091a6235 turn 3): a participant answered entirely
    # in thinking, running only ToolSearch + direct_to — no data tool, zero
    # text. Nothing to relay is always a restate, whatever tools ran.
    calls, _ = await _drive_meeting([
        _result("mod", "Please report.", directed=["p1"]),
        _result("p1", "", tools=("ToolSearch",), directed=["mod"]),
        _result("p1", "FINDINGS: Waypoint is the safe pick.", ),
        _result("mod", "Summary.", called=("end_meeting",)),
    ])
    agents = [c[0] for c in calls]
    assert agents == ["mod", "p1", "p1", "mod"]
    assert calls[2][1] == [{"type": "restate"}]
    assert any(e.get("content", "").startswith("FINDINGS") for e in calls[3][1])


@pytest.mark.asyncio
async def test_concise_post_tool_answer_is_not_restated():
    calls, _ = await _drive_meeting([
        _result("mod", "Quick check please.", directed=["p1"]),
        # Short but real: text was written AFTER the data tool.
        _result("p1", "Tonight 22:00-23:00 is clear, no backups scheduled.",
                tools=(DATA_TOOL,), directed=["mod"]),
        _result("mod", "Summary.", called=("end_meeting",)),
    ])
    assert [c[0] for c in calls] == ["mod", "p1", "mod"]


@pytest.mark.asyncio
async def test_restate_runs_alone_before_other_routed_agents():
    """The live race (mtg-802cab79c3a5): in a parallel batch p1 routed to the
    moderator while p2's turn was thin — the moderator then ran in PARALLEL
    with p2's restate, couldn't see the restated findings, and its
    end_meeting discarded them from the batch. While a restate is pending,
    only the restating agents may run."""
    meeting_row = {
        "id": "m1", "status": "active", "topic": "t",
        "moderator": "mod", "max_turns": 30,
        "parent_chat_id": "chat-relay-3",
        "active_participants": json.dumps(["mod", "p1", "p2"]),
        "participants": json.dumps(["mod", "p1", "p2"]),
    }
    live_results = deque([
        _result("mod", "Both of you: report please.", directed=["p1", "p2"]),
        # p2's restate — must run ALONE before the moderator.
        _result("p2", "RESTATED: my suggestion is Falcon."),
        _result("mod", "Summary.", called=("end_meeting",)),
    ])
    batch_results = deque([
        # The parallel round: p1 answers and routes to mod; p2 is thin.
        [_result("p1", "My suggestion is Otter.", directed=["mod"]),
         _result("p2", "", tools=("ToolSearch",), directed=["mod"])],
    ])
    rounds: list[list[str]] = []

    async def fake_live(agent_slug, agent_sessions, meeting, transcript,
                        pending, event_queue, meeting_id):
        rounds.append([agent_slug])
        return live_results.popleft()

    async def fake_batch(ready_agents, agent_sessions, meeting, transcript,
                         pending, event_queue, meeting_id):
        rounds.append(list(ready_agents))
        return list(batch_results.popleft())

    with patch.object(MO, "_run_live_turn", new=fake_live), \
         patch.object(MO, "_run_parallel_batch", new=fake_batch), \
         patch.object(MO.task_store, "get_meeting", return_value=meeting_row), \
         patch.object(MO.task_store, "update_meeting"), \
         patch.object(MO.task_store, "add_meeting_turn"):
        pump = SimpleNamespace(chat_id="chat-mtg", system_queue=[])
        await MO.meeting_produce(
            "m1", {"mod": "s0", "p1": "s1", "p2": "s2"}, asyncio.Queue(), pump)

    # mod opens → [p1, p2] batch → p2 restates ALONE → mod concludes with
    # both answers on the transcript.
    assert rounds == [["mod"], ["p1", "p2"], ["p2"], ["mod"]]


def test_build_turn_prompt_restate_footer():
    meeting = _meeting_row()
    transcript = [
        {"agent": "p1", "content": "Pulling data.", "thinking": "", "tools": []},
    ]
    prompt = build_turn_prompt(meeting, "p1", transcript, prompt_type="restate")
    assert "did not include your findings" in prompt
    assert "direct_to" in prompt


# ---------------------------------------------------------------------------
# Turn-end backstop state + end_meeting summary salvage (2026-09-09)
# ---------------------------------------------------------------------------
#
# Live incident mtg-55a9c2cf4c90: the moderator called direct_to, kept
# running tools for 304 s inside the same turn (the orchestrator routes only
# at the turn boundary, so the participant never spoke), and wrote its summary
# AFTER end_meeting — discarded by the echo guard. The hook now denies tools
# after a routing call (api/hooks); the runner clears that state around the
# turn and keeps a summary written after the MODERATOR's end_meeting.

from core.session.session_state import (  # noqa: E402
    cleanup_meeting_session_info,
    get_meeting_session_info,
    mark_meeting_turn_routed,
    set_meeting_session_info,
)

END_MEETING = "mcp__meetings-mcp__end_meeting"


class _RecordingLayer(_ScriptedLayer):
    """Scripted layer whose event list may carry callables (run in-stream,
    between events)."""

    async def send_message(self, sid, prompt):
        for ev in self._events:
            if callable(ev):
                ev()
                continue
            yield ev


async def _run_turn_as(agent, events, *, layer=None):
    meeting = {
        "id": "m1", "topic": "t", "moderator": "mod",
        "parent_chat_id": "chat-relay-4",
        "participants": json.dumps(["mod", "p1"]),
    }
    layer = layer or _RecordingLayer(events)
    MO._meeting_session_layers["sid-1"] = layer
    queue: asyncio.Queue = asyncio.Queue()
    try:
        result = await MO._run_live_turn(
            agent, {agent: "sid-1"}, meeting, [], {}, queue, "m1",
        )
    finally:
        MO._meeting_session_layers.pop("sid-1", None)
    forwarded_text = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev.type == TEXT:
            forwarded_text.append(ev.data["content"])
    return result, forwarded_text, layer


def _end_meeting_shape(pre_text, post_text):
    return [
        _text(pre_text),
        _tool_use(END_MEETING),
        _tool_input(END_MEETING, meeting_id="m1"),
        _tool_result(END_MEETING),
        _text(post_text),
    ]


@pytest.mark.asyncio
async def test_moderator_summary_after_end_meeting_is_kept():
    result, forwarded, _ = await _run_turn_as(
        "mod", _end_meeting_shape("Wrapping up.", "SUMMARY: ship 1.6 Friday."),
    )
    assert result.content == "Wrapping up.SUMMARY: ship 1.6 Friday."
    assert "end_meeting" in result.tools_called
    # The salvaged summary is displayed too, not only collected.
    assert "SUMMARY: ship 1.6 Friday." in forwarded


@pytest.mark.asyncio
async def test_moderator_echo_after_a_written_summary_is_dropped():
    summary = "SUMMARY: " + "the release is on track. " * 15  # > 300 chars
    result, forwarded, _ = await _run_turn_as(
        "mod", _end_meeting_shape(summary, "TRANSCRIPT ECHO JUNK"),
    )
    assert result.content == summary
    assert "TRANSCRIPT ECHO JUNK" not in forwarded


@pytest.mark.asyncio
async def test_participant_text_after_end_meeting_is_dropped():
    # A participant's end_meeting is refused by the API and routes nothing;
    # its trailing text stays under the echo guard like any other tool.
    result, forwarded, _ = await _run_turn_as(
        "p1", _end_meeting_shape("Trying to close.", "AFTER"),
    )
    assert result.content == "Trying to close."
    assert "AFTER" not in forwarded


@pytest.mark.asyncio
async def test_backstop_state_is_cleared_around_the_turn():
    set_meeting_session_info("sid-1", "parent-1", "meeting-m1", "mod", "chat-relay-4",
                             is_moderator=True)
    seen: list[str] = []
    try:
        # Left over from the previous turn (a direct_to that closed it).
        mark_meeting_turn_routed("sid-1", "direct_to")
        events = [
            lambda: seen.append(get_meeting_session_info("sid-1")["routed_tool"]),
            _text("Report."),
            lambda: seen.append(str(get_meeting_session_info("sid-1")["turn_text_chars"])),
            _tool_use(DIRECT_TO),
            _tool_input(DIRECT_TO, agents=["p1"]),
            _tool_result(DIRECT_TO),
        ]
        result, _, _ = await _run_turn_as("mod", events)
        info = get_meeting_session_info("sid-1")
        assert seen == ["", str(len("Report."))]  # cleared at start; text counted live
        assert result.directed_to == ["p1"]
        assert info["routed_tool"] == ""
        assert info["turn_text_chars"] == 0
    finally:
        cleanup_meeting_session_info("sid-1")


@pytest.mark.asyncio
async def test_participant_end_meeting_does_not_end_the_meeting():
    # tools_called is recorded at TOOL_INPUT, before the API's 403 — the
    # producer must only honour the moderator's end_meeting.
    calls, saved = await _drive_meeting([
        _result("mod", "Report please.", directed=["p1"]),
        _result("p1", "Done, closing.", directed=["mod"], called=("end_meeting",)),
        _result("mod", "Summary.", called=("end_meeting",)),
    ])
    assert [c[0] for c in calls] == ["mod", "p1", "mod"]
    assert len(saved) == 3


def _agent_row_off_the_loop(slug):
    """``agent_store.get_agent`` that refuses to run on an event loop: a
    cache miss reads every agent row from the database."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return {"display_name": slug.upper(), "color": "#123456"}
    raise AssertionError(f"agent row of {slug} read on the event loop")


@pytest.mark.asyncio
async def test_the_turn_start_reads_the_agent_row_off_the_loop():
    meeting = {
        "id": "m1", "topic": "t", "moderator": "mod",
        "parent_chat_id": "chat-relay-5",
        "participants": json.dumps(["mod", "p1", "p2"]),
    }
    layers = {f"sid-{a}": _ScriptedLayer([_text(f"{a} says hello.")]) for a in ("p1", "p2")}
    queue: asyncio.Queue = asyncio.Queue()
    # The prompt builder (meeting_context) is judged on its own; here only
    # the runners' own reads.
    with patch.object(MO.agent_store, "get_agent", new=_agent_row_off_the_loop), \
         patch.object(MO, "build_turn_prompt", new=lambda *a, **k: "prompt"), \
         patch.dict(MO._meeting_session_layers, layers):
        sessions = {"p1": "sid-p1", "p2": "sid-p2"}
        await MO._run_live_turn("p1", sessions, meeting, [], {}, queue, "m1")
        await MO._run_parallel_batch(["p1", "p2"], sessions, meeting, [], {}, queue, "m1")
    starts = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev.data.get("subtype") == "meeting_turn_start":
            starts.append(ev.data["agent_display_name"])
    assert sorted(starts) == ["P1", "P1", "P2"]


class _TurnBudget:
    """A ``max_turns`` that counts the round loop's condition checks: the
    loop re-tests ``total_turns < max_turns`` on every pass, a pass that runs
    no turn included, so a loop that would spin raises here (the producer
    reports it as an error) instead of running away."""

    def __init__(self, passes: int, turns: int = 30):
        self.left = passes
        self.turns = turns
        self.spun = False

    def __gt__(self, total_turns):
        self.left -= 1
        if self.left < 0:
            self.spun = True
            raise RuntimeError("the round loop spun")
        return total_turns < self.turns


@pytest.mark.asyncio
async def test_a_moderator_who_leaves_mid_meeting_never_stalls_the_round_loop():
    """The moderator leaves in a parallel batch while the others still have
    work: once nobody is ready the loop must end, never queue a wrap-up for
    a moderator who is no longer active and go round again."""
    budget = _TurnBudget(passes=200)
    row = {
        "id": "m1", "status": "active", "topic": "t",
        "moderator": "mod", "max_turns": budget, "parent_chat_id": "chat-relay-4",
        "active_participants": json.dumps(["mod", "p1", "p2"]),
        "participants": json.dumps(["mod", "p1", "p2"]),
    }
    script = {
        "mod": deque([_result("mod", "p1, report please.", directed=["p1"]),
                      _result("mod", "I have to go.", called=("leave_meeting",))]),
        "p1": deque([_result("p1", "Report for everyone."),
                     _result("p1", "Noted, nothing to add.", directed=[])]),
        "p2": deque([_result("p2", "p1, one more detail?", directed=["p1"])]),
    }

    def _next(agent):
        q = script.get(agent)
        return q.popleft() if q else _result(agent, "Summary.", called=("end_meeting",))

    rounds: list[list[str]] = []

    async def fake_live(agent_slug, *a):
        rounds.append([agent_slug])
        return _next(agent_slug)

    async def fake_batch(ready_agents, *a):
        rounds.append(list(ready_agents))
        return [_next(x) for x in ready_agents]

    closed: list[str] = []

    class _Layer:
        async def close_session(self, sid):
            closed.append(sid)

    q: asyncio.Queue = asyncio.Queue()
    with patch.object(MO, "_run_live_turn", new=fake_live), \
         patch.object(MO, "_run_parallel_batch", new=fake_batch), \
         patch.dict(MO._meeting_session_layers, {s: _Layer() for s in ("s0", "s1", "s2")}), \
         patch.object(MO.task_store, "get_meeting", return_value=row), \
         patch.object(MO.task_store, "update_meeting"), \
         patch.object(MO.task_store, "remove_active_participant", return_value=["p1", "p2"]), \
         patch.object(MO.task_store, "add_meeting_turn"), \
         patch.object(MO.agent_store, "get_agent", new=_agent_row_off_the_loop):
        pump = SimpleNamespace(chat_id="chat-mtg", system_queue=[])
        await MO.meeting_produce("m1", {"mod": "s0", "p1": "s1", "p2": "s2"}, q, pump)
    ends = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.data.get("subtype") == "meeting_concluded":
            ends.append(ev.data)
    assert not budget.spun, "the round loop spun once the moderator had left"
    assert ends and "error" not in ends[-1]
    # The moderator's leave ends the meeting after that round, as its
    # failure does.
    assert rounds == [["mod"], ["p1"], ["mod", "p2"]]
    assert closed == ["s0"]


def test_turn_prompts_say_replies_arrive_next_turn():
    meeting = _meeting_row()
    start = build_turn_prompt(meeting, "mod", [], prompt_type="start")
    assert "next turn" in start and "stop" in start
    from services.meetings.meeting_context import _MEETING_AGENT_SUFFIX
    assert "NEXT turn" in _MEETING_AGENT_SUFFIX
    assert "denies every tool call" in _MEETING_AGENT_SUFFIX


@pytest.mark.asyncio
async def test_a_round_reads_the_chats_queue_for_the_moderator():
    """A message typed while the meeting runs waits in the chat's queue (a
    meeting turn takes no steer): the next round takes it, the pump writes
    its row after the round's content (the QUEUE_TURN carries the item)."""
    from core.events import input_queue
    from core.events.common_events import QUEUE_TURN, TurnInput
    input_queue._registry.pop("chat-mtg", None)
    q = input_queue.get("chat-mtg")
    q.items.append(input_queue.QueuedInput(
        queue_id="q-1", chat_id="chat-mtg", author_sub="user-admin",
        item=TurnInput("check the gate", cli_text="check the gate /tmp/plan.pdf")))
    events: asyncio.Queue = asyncio.Queue()

    async def fake_live(agent_slug, agent_sessions, meeting, transcript,
                        pending, event_queue, meeting_id):
        return _result("mod", "Thanks.", called=("end_meeting",))

    try:
        with patch.object(MO, "_run_live_turn", new=fake_live), \
             patch.object(MO.task_store, "get_meeting", return_value=_meeting_row()), \
             patch.object(MO.task_store, "update_meeting"), \
             patch.object(MO.task_store, "add_meeting_turn"):
            await MO.meeting_produce("m1", {"mod": "s-mod", "p1": "s-p1"}, events,
                                     SimpleNamespace(chat_id="chat-mtg", system_queue=[]))
    finally:
        input_queue._registry.pop("chat-mtg", None)
    every = []
    while not events.empty():
        every.append(events.get_nowait())
    turns = [e for e in every if e.type == QUEUE_TURN]
    assert [qi.queue_id for t in turns for qi in t.data["inputs"]] == ["q-1"]
    assert q.items == []
