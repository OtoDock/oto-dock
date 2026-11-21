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
    TEXT,
    TOOL_INPUT,
    TOOL_RESULT,
    TOOL_USE,
    CommonEvent,
)
from services.meetings import meeting_orchestrator as MO, meeting_sessions
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

from core.session.session_state import (
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


@pytest.mark.asyncio
async def test_a_parallel_batch_forwards_each_agents_text_and_tools_live_and_once():
    """A person watching sees a batch as it happens: each agent's text and tool
    rows reach the pump while that agent runs, tagged with the agent, once."""
    meeting = {
        "id": "m1", "topic": "t", "moderator": "mod",
        "parent_chat_id": "chat-relay-6",
        "participants": json.dumps(["mod", "p1", "p2"]),
    }
    gate = asyncio.Event()

    class _Held(_ScriptedLayer):
        async def send_message(self, sid, prompt):
            for ev in self._events:
                yield ev
            await gate.wait()

    layers = {f"sid-{a}": _Held([_text(f"{a} says hello."), _tool_use(f"{a}_tool")]) for a in ("p1", "p2")}
    queue: asyncio.Queue = asyncio.Queue()
    with patch.object(MO.agent_store, "get_agent", new=_agent_row_off_the_loop), \
         patch.object(MO, "build_turn_prompt", new=lambda *a, **k: "prompt"), \
         patch.dict(MO._meeting_session_layers, layers):
        batch = asyncio.ensure_future(MO._run_parallel_batch(
            ["p1", "p2"], {"p1": "sid-p1", "p2": "sid-p2"}, meeting, [], {}, queue, "m1"))
        seen = []

        async def next_text():
            while True:
                ev = await queue.get()
                seen.append(ev)
                if ev.type == TEXT:
                    return ev

        first = await asyncio.wait_for(next_text(), 5)         # with both agents still running
        assert not batch.done() and first.data["_meeting_agent"] in ("p1", "p2")
        gate.set()
        await asyncio.wait_for(batch, 5)
    while not queue.empty():
        seen.append(queue.get_nowait())
    texts = [(e.data["_meeting_agent"], e.data["content"]) for e in seen if e.type == TEXT]
    assert sorted(texts) == [("p1", "p1 says hello."), ("p2", "p2 says hello.")]
    tools = [(e.data["_meeting_agent"], e.data["name"]) for e in seen if e.type == TOOL_USE]
    assert sorted(tools) == [("p1", "p1_tool"), ("p2", "p2_tool")]


class _Scripted(_ScriptedLayer):
    """A participant's turn step by step: an event, a gate to wait at, a failure."""

    async def send_message(self, sid, prompt):
        for step in self._events:
            if isinstance(step, Exception):
                raise step
            if isinstance(step, asyncio.Event):
                await step.wait()
                continue
            yield step


async def _batch(layers, *, while_running=None):
    meeting = {"id": "m1", "topic": "t", "moderator": "mod", "parent_chat_id": "chat-relay-7",
               "participants": json.dumps(["mod", "p1", "p2"])}
    queue: asyncio.Queue = asyncio.Queue()
    with patch.object(MO.agent_store, "get_agent", new=_agent_row_off_the_loop), \
         patch.object(MO, "build_turn_prompt", new=lambda *a, **k: "prompt"), \
         patch.object(MO, "_start_participant_bg_monitors", new=lambda *a, **k: None), \
         patch.dict(MO._meeting_session_layers, layers):
        batch = asyncio.ensure_future(MO._run_parallel_batch(
            ["p1", "p2"], {"p1": "sid-p1", "p2": "sid-p2"}, meeting, [], {}, queue, "m1"))
        seen = await while_running(queue) if while_running is not None else []
        results = await asyncio.wait_for(batch, 5)
    while not queue.empty():
        seen.append(queue.get_nowait())
    return {r.agent: r for r in results}, seen


def _lines(seen):
    return [(e.data.get("subtype"), e.data.get("agent")) for e in seen if e.type == MO.SYSTEM]


@pytest.mark.asyncio
async def test_a_participant_that_fails_after_its_first_event_gets_its_failed_line():
    """The coordinator takes it at its first event, with no result yet. What it did
    stands, and the person reads why it stopped, as on a live turn."""
    gate = asyncio.Event()

    async def until_first_text(queue):
        seen = []
        while True:
            ev = await asyncio.wait_for(queue.get(), 5)
            seen.append(ev)
            if ev.type == TEXT:
                gate.set()
                return seen

    results, seen = await _batch(
        {"sid-p1": _Scripted([_text("p1 starts"), gate, RuntimeError("the machine went")]),
         "sid-p2": _Scripted([gate, _text("p2 answers")])}, while_running=until_first_text)
    lines = _lines(seen)
    assert "_failed" in results["p1"].tools_called
    assert ("meeting_agent_failed", "p1") in lines and ("meeting_turn_end", "p1") not in lines
    assert ("meeting_turn_end", "p2") in lines and ("meeting_agent_failed", "p2") not in lines
    failed = next(e for e in seen if e.data.get("subtype") == "meeting_agent_failed")
    assert failed.data["error"] == "the machine went"
    texts = [(e.data["_meeting_agent"], e.data["content"]) for e in seen if e.type == TEXT]
    assert sorted(texts) == [("p1", "p1 starts"), ("p2", "p2 answers")]


@pytest.mark.asyncio
async def test_a_participant_that_failed_before_its_turn_came_keeps_what_it_did():
    """It ran and failed while the coordinator forwarded the other: its text is
    shown with the line, and not dropped."""
    p1_chosen, p2_failed = asyncio.Event(), asyncio.Event()

    class _Marks(_Scripted):
        async def send_message(self, sid, prompt):
            try:
                async for event in super().send_message(sid, prompt):
                    yield event
            finally:
                p2_failed.set()

    async def until_p1_is_chosen(queue):
        seen = []
        while True:
            ev = await asyncio.wait_for(queue.get(), 5)
            seen.append(ev)
            if ev.type == TEXT:
                p1_chosen.set()
                return seen

    results, seen = await _batch(
        {"sid-p1": _Scripted([_text("p1 starts"), p2_failed, _text("p1 ends")]),
         "sid-p2": _Marks([p1_chosen, _text("p2 starts"), RuntimeError("it went")])},
        while_running=until_p1_is_chosen)
    lines = _lines(seen)
    assert "_failed" in results["p2"].tools_called
    assert ("meeting_turn_end", "p1") in lines
    assert ("meeting_turn_start", "p2") in lines and ("meeting_agent_failed", "p2") in lines
    assert ("p2", "p2 starts") in [(e.data["_meeting_agent"], e.data["content"]) for e in seen if e.type == TEXT]


@pytest.mark.asyncio
async def test_a_participant_that_failed_with_nothing_to_show_gets_the_line_alone():
    results, seen = await _batch({"sid-p1": _Scripted([RuntimeError("it never started")]),
                                  "sid-p2": _Scripted([_text("p2 answers")])})
    lines = _lines(seen)
    assert "_failed" in results["p1"].tools_called
    assert ("meeting_agent_failed", "p1") in lines and ("meeting_turn_start", "p1") not in lines
    failed = next(e for e in seen if e.data.get("subtype") == "meeting_agent_failed")
    assert failed.data["error"] == "it never started"


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


class _ReconnectingLayer(_ScriptedLayer):
    """A compute machine's layer whose link dropped: the session is held in
    the reconnect grace and a turn sent meanwhile is refused at once, as the
    real link refuses it. The wait brings the machine back."""

    def __init__(self, events):
        super().__init__(events)
        self.linked = False
        self.waits = 0

    def is_session_grace_held(self, sid):
        return not self.linked

    async def wait_session_reconnect(self, sid, *, timeout=10.0):
        self.waits += 1
        self.linked = True
        return True

    async def send_message(self, sid, prompt):
        if not self.linked:
            raise RuntimeError("Satellite compute-1 not connected")
        async for ev in super().send_message(sid, prompt):
            yield ev


@pytest.mark.asyncio
async def test_a_participants_turn_waits_while_its_compute_machine_reconnects():
    """One rule for every turn start: a participant whose compute machine is
    reconnecting waits for it, alone or in a parallel batch, and speaks once
    it is back. No failure line reaches the meeting."""
    from core.session.session_manager import LiveSession
    meeting = {
        "id": "m1", "topic": "t", "moderator": "mod",
        "parent_chat_id": "chat-relay-7",
        "participants": json.dumps(["mod", "p1", "p2"]),
    }
    layers = {f"sid-{a}": _ReconnectingLayer([_text(f"{a} is back.")]) for a in ("p1", "p2")}
    queue: asyncio.Queue = asyncio.Queue()
    with patch.object(MO.agent_store, "get_agent", new=lambda slug: {"display_name": slug}), \
         patch.object(MO, "build_turn_prompt", new=lambda *a, **k: "prompt"), \
         patch.object(meeting_sessions, "live_session", new=lambda sid: LiveSession(
             layers[sid], "compute-1", linked=layers[sid].linked, grace=not layers[sid].linked)), \
         patch.dict(MO._meeting_session_layers, layers):
        sessions = {"p1": "sid-p1", "p2": "sid-p2"}
        alone = await MO._run_live_turn("p1", sessions, meeting, [], {}, queue, "m1")
        layers["sid-p1"].linked = False
        batch = await MO._run_parallel_batch(["p1", "p2"], sessions, meeting, [], {}, queue, "m1")
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert [layer.waits for layer in layers.values()] == [2, 1]
    assert not [e for e in events if e.data.get("subtype") == "meeting_agent_failed"]
    assert "_failed" not in alone.tools_called and all("_failed" not in r.tools_called for r in batch)
    assert sorted(e.data["content"] for e in events if e.type == TEXT) == ["p1 is back.", "p1 is back.", "p2 is back."]


@pytest.mark.asyncio
async def test_a_participant_whose_session_was_lost_mid_turn_fails_and_the_meeting_goes_on():
    """A machine came back without the participant's session while its turn
    ran: the turn ends with the ``lost`` ending, and a meeting has no heal,
    so the turn fails through the runner's failure path, alone or in a batch,
    with the failure line the person reads."""
    from core.events import turn_ending
    from core.events.common_events import DONE, ERROR
    lost = turn_ending.TurnEnding(reason=turn_ending.LOST, detail="the machine came back without this session")
    gone = [_text("Half a sen"), CommonEvent(type=ERROR, data={"message": lost.line(), "ending": lost.as_dict()}),
            CommonEvent(type=DONE)]
    meeting = {
        "id": "m1", "topic": "t", "moderator": "mod",
        "parent_chat_id": "chat-relay-8",
        "participants": json.dumps(["mod", "p1", "p2"]),
    }
    layers = {"sid-p1": _ScriptedLayer(gone), "sid-p2": _ScriptedLayer([_text("p2 is fine.")])}
    queue: asyncio.Queue = asyncio.Queue()
    with patch.object(MO.agent_store, "get_agent", new=lambda slug: {"display_name": slug}), \
         patch.object(MO, "build_turn_prompt", new=lambda *a, **k: "prompt"), \
         patch.dict(MO._meeting_session_layers, layers):
        sessions = {"p1": "sid-p1", "p2": "sid-p2"}
        alone = await MO._run_live_turn("p1", sessions, meeting, [], {}, queue, "m1")
        batch = await MO._run_parallel_batch(["p1", "p2"], sessions, meeting, [], {}, queue, "m1")
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    failed = [e.data for e in events if e.data.get("subtype") == "meeting_agent_failed"]
    assert [f["agent"] for f in failed] == ["p1", "p1"]
    assert all(lost.line()[:40] in f["error"] for f in failed)
    assert "_failed" in alone.tools_called
    assert {r.agent: "_failed" in r.tools_called for r in batch} == {"p1": True, "p2": False}


@pytest.mark.asyncio
async def test_a_participant_the_memory_guard_stopped_gets_the_meetings_own_line_and_stays():
    """The machine's memory guard ended a participant's engine mid-turn:
    the chat's card for that ending never reaches the meeting, the meeting
    says so about that agent by name, and the agent is not taken out: its
    next turn starts its engine again."""
    from core.events import turn_ending
    from core.events.common_events import DONE, ERROR
    stopped = turn_ending.TurnEnding(reason=turn_ending.EXITED, detail=turn_ending.MEMORY)
    events = [_text("Half a sen"), CommonEvent(type=ERROR, data={"message": stopped.line(),
                                                                 "ending": stopped.as_dict()}),
              CommonEvent(type=DONE)]
    meeting = {
        "id": "m1", "topic": "t", "moderator": "mod",
        "parent_chat_id": "chat-relay-9",
        "participants": json.dumps(["mod", "p1", "p2"]),
    }
    layers = {"sid-p1": _ScriptedLayer(events), "sid-p2": _ScriptedLayer([_text("p2 is fine.")])}
    queue: asyncio.Queue = asyncio.Queue()
    with patch.object(MO.agent_store, "get_agent", new=lambda slug: {"display_name": slug.upper()}), \
         patch.object(MO, "build_turn_prompt", new=lambda *a, **k: "prompt"), \
         patch.dict(MO._meeting_session_layers, layers):
        sessions = {"p1": "sid-p1", "p2": "sid-p2"}
        alone = await MO._run_live_turn("p1", sessions, meeting, [], {}, queue, "m1")
        batch = await MO._run_parallel_batch(["p1", "p2"], sessions, meeting, [], {}, queue, "m1")
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    said = [e.data for e in seen if e.data.get("subtype") == "meeting_agent_stopped"]
    assert [s["agent"] for s in said] == ["p1", "p1"]
    assert said[0]["message"] == ("P1 used too much memory, so its engine was stopped. "
                                  "It answers again at its next turn.")
    # Never the chat's card, never a departure.
    assert not [e for e in seen if e.type == ERROR]
    assert not [e for e in seen if e.data.get("subtype") == "meeting_agent_failed"]
    assert "_failed" not in alone.tools_called
    assert all("_failed" not in r.tools_called for r in batch)


class _GoneLayer(_ScriptedLayer):
    """The layer that held a participant's session and no longer does (its
    machine came back without it between two turns)."""

    def __init__(self):
        super().__init__([])
        self.prepared: list[str] = []

    async def is_session_alive(self, sid):
        return False

    async def is_session_process_dead(self, sid):
        return True

    async def can_resume_session(self, sid, **kw):
        from core.execution_layer import ResumeVerdict
        return ResumeVerdict.RESUMABLE

    async def prepare_resume(self, sid):
        self.prepared.append(sid)

    async def send_message(self, sid, prompt):
        raise RuntimeError("Remote session not found")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_a_participant_whose_session_is_gone_is_started_again_before_its_turn(monkeypatch):
    """A participant idle when its compute machine restarted: the platform
    closed its session. Its next turn starts it again under the same id,
    with its config rebuilt as at the meeting's start and its conversation
    resumed, on the layer the rebuilt config names, its slot taken where it
    lands. The turn then runs: the agent is not lost to the meeting."""
    from types import SimpleNamespace as NS

    from core import concurrency, placement
    from core.config import config_builder
    from core.session.session_manager import LiveSession
    where = placement.from_machine(placement.KIND_COMPUTE, {"id": "compute-2", "pairing_scope": "compute"})
    cfg = NS(execution_path="claude-code-cli", execution_target="local", user_sub="", mcp_config_path="",
             resume=False, security_context=NS(placement=where, role="editor", mount_username="alice"))
    gone = _GoneLayer()
    back = _ScriptedLayer([_text("I am back.")])
    back.started = []

    async def _start(sid, config):
        back.started.append((sid, config.resume))
    back.start_session = _start

    async def _alive(sid):
        return True
    back.is_session_alive = _alive
    built: list = []

    async def _build(slug, meeting, sid):
        built.append((slug, sid))
        return cfg
    asks: list = []
    monkeypatch.setattr(meeting_sessions, "build_meeting_agent_config", _build)
    monkeypatch.setattr(meeting_sessions, "get_execution_layer", lambda slug, **kw: asks.append(kw) or back)
    monkeypatch.setattr(meeting_sessions, "live_session",
                        lambda sid: LiveSession(gone) if sid == "sid-1" else None)
    monkeypatch.setattr(meeting_sessions, "_save_sessions", lambda: None)
    moved: list = []

    async def _rehome(sid, **kw):
        moved.append((sid, kw["kind"], kw["target"]))
        return True
    monkeypatch.setattr(concurrency, "rehome_slot", _rehome)
    freed: list = []
    monkeypatch.setattr(config_builder, "release_config_seat", lambda sid, c: freed.append(sid))
    meeting = {"id": "m1", "topic": "t", "moderator": "mod", "parent_chat_id": "chat-relay-10",
               "participants": json.dumps(["mod", "p1"])}
    queue: asyncio.Queue = asyncio.Queue()
    with patch.object(MO.agent_store, "get_agent", new=lambda slug: {"display_name": slug}), \
         patch.object(MO, "build_turn_prompt", new=lambda *a, **k: "prompt"), \
         patch.dict(MO._meeting_session_layers, {"sid-1": gone}):
        result = await MO._run_live_turn("p1", {"p1": "sid-1"}, meeting, [], {}, queue, "m1")
        assert MO._meeting_session_layers["sid-1"] is back
    assert built == [("p1", "sid-1")] and gone.prepared == ["sid-1"]
    assert back.started == [("sid-1", True)] and cfg.resume is True
    assert asks[0]["placement_built"] is where and asks[0]["execution_target"] == "local"
    assert moved == [("sid-1", "meeting", "compute-2")] and freed == []
    assert result.content == "I am back." and "_failed" not in result.tools_called
    MO._sessions.pop("sid-1", None)

    # A restart that cannot start (no compute machine left) gives the seat
    # back and fails the turn: the meeting goes on without the participant.
    async def _refused(sid, config):
        raise placement.NoComputeMachine()
    back.start_session = _refused
    with patch.object(MO.agent_store, "get_agent", new=lambda slug: {"display_name": slug}), \
         patch.object(MO, "build_turn_prompt", new=lambda *a, **k: "prompt"), \
         patch.dict(MO._meeting_session_layers, {"sid-1": gone}):
        result = await MO._run_live_turn("p1", {"p1": "sid-1"}, meeting, [], {}, queue, "m1")
    assert "_failed" in result.tools_called and freed == ["sid-1"]
    failed = [e.data for e in list(queue._queue) if e.data.get("subtype") == "meeting_agent_failed"]
    assert failed and failed[-1]["error"] == placement.NO_COMPUTE_SENTENCE
