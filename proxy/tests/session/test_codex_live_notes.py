"""What a person sees while Codex works: the reasoning summaries every turn
asks for, their parts and their completed-item fallback, and a commentary
message (the preamble between steps) streamed as a progress note: TEXT with
``note``, still text for every reader of the agent's words."""

import asyncio
import logging

import pytest

from core.events.bg_command_state import _bg_command_registries
from core.events.common_events import (
    BG_COMMAND_START,
    DONE,
    TEXT,
    THINKING,
    is_note_mark,
)
from core.layers.codex.helpers import REASONING_SUMMARY
from core.layers.codex.session import CodexAppServerSession, CodexEvent
from core.layers.codex.translator import CodexEventTranslator

MAIN = "thread-MAIN"


def _tr(sid: str = "s-live") -> CodexEventTranslator:
    tr = CodexEventTranslator(model="gpt-6.1-sol", supervised_bg=True,
                              session_id=sid, supervised_bg_commands=True)
    tr._main_thread_id = MAIN
    return tr


def _feed(tr, method, **params):
    return tr.translate(CodexEvent(type=method, data={"threadId": MAIN, **params}))


def _shape(events):
    return [(e.type, e.data.get("phase"), e.data.get("text", e.data.get("content")),
             e.data.get("note", False)) for e in events]


# --- the request -------------------------------------------------------------

class _Client:
    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.is_alive = True

    async def request(self, method, params=None, *, timeout=None):
        self.requests.append((method, dict(params or {})))  # the session reuses its dict
        return {"turn": {"id": "turn-1"}} if method == "turn/start" else {}

    async def close(self):
        self.is_alive = False


@pytest.mark.asyncio
async def test_every_turn_asks_for_the_reasoning_summary_and_keeps_the_effort():
    s = CodexAppServerSession(
        session_id="s1", agent_name="a", model="gpt-6.1-sol",
        sandbox_mode="workspace-write", working_dir="/tmp",
        config_dir="/tmp/.codex", thread_id="thr-1", effort="high",
    )
    s._started = True
    s._client = _Client()

    async def feed():
        await asyncio.sleep(0.05)
        s._default_consumer.put_nowait(("turn/completed", {"threadId": "thr-1",
                                        "turn": {"status": "completed"}}))
    feeder = asyncio.create_task(feed())
    async for _ev in s.send_message("hello"):
        pass
    await feeder
    starts = [p for m, p in s._client.requests if m == "turn/start"]
    assert len(starts) == 1
    assert starts[0]["summary"] == REASONING_SUMMARY == "auto"
    assert starts[0]["effort"] == "high"


# --- reasoning summaries -----------------------------------------------------

def test_summary_parts_stream_as_one_thinking_block_with_a_break_between_parts():
    tr = _tr()
    out = _feed(tr, "item/started", item={"type": "reasoning", "id": "rs1"})
    out += _feed(tr, "item/reasoning/summaryPartAdded", itemId="rs1", summaryIndex=0)
    out += _feed(tr, "item/reasoning/summaryTextDelta", itemId="rs1", summaryIndex=0,
                 delta="**Planning**")
    out += _feed(tr, "item/reasoning/summaryPartAdded", itemId="rs1", summaryIndex=1)
    out += _feed(tr, "item/reasoning/summaryTextDelta", itemId="rs1", summaryIndex=1,
                 delta="Then the list.")
    out += _feed(tr, "item/reasoning/summaryTextDelta", itemId="rs1", summaryIndex=1,
                 delta=" Done.")
    out += _feed(tr, "item/completed", item={"type": "reasoning", "id": "rs1",
                                              "summary": ["**Planning**", "Then the list. Done."]})
    assert _shape(out) == [
        (THINKING, "start", None, False),
        (THINKING, "delta", "**Planning**", False),
        (THINKING, "delta", "\n\nThen the list.", False),
        (THINKING, "delta", " Done.", False),
        (THINKING, "end", "", False),
    ]


def test_a_completed_summary_that_never_streamed_is_sent_once_and_never_doubled():
    tr = _tr()
    out = _feed(tr, "item/started", item={"type": "reasoning", "id": "rs1"})
    out += _feed(tr, "item/completed", item={"type": "reasoning", "id": "rs1",
                                              "summary": ["**One**", "Two."], "content": []})
    assert _shape(out) == [
        (THINKING, "start", None, False),
        (THINKING, "delta", "**One**\n\nTwo.", False),
        (THINKING, "end", "", False),
    ]
    # The same item streamed: the completed summary is not sent again.
    out = _feed(tr, "item/started", item={"type": "reasoning", "id": "rs2"})
    out += _feed(tr, "item/reasoning/summaryTextDelta", itemId="rs2", summaryIndex=0, delta="Live")
    out += _feed(tr, "item/completed", item={"type": "reasoning", "id": "rs2", "summary": ["Live"]})
    assert [t for _k, _p, t, _n in _shape(out) if t] == ["Live"]


def test_raw_reasoning_content_is_the_fallback_when_there_is_no_summary():
    tr = _tr()
    out = _feed(tr, "item/started", item={"type": "reasoning", "id": "rs1"})
    out += _feed(tr, "item/completed", item={"type": "reasoning", "id": "rs1",
                                              "summary": [], "content": ["thinking it over"]})
    assert ("delta", "thinking it over") in [(p, t) for _k, p, t, _n in _shape(out)]


def test_a_model_that_gives_no_summary_is_an_empty_pair_the_pump_never_saves():
    tr = _tr()
    out = _feed(tr, "item/started", item={"type": "reasoning", "id": "rs1"})
    out += _feed(tr, "item/completed", item={"type": "reasoning", "id": "rs1", "summary": []})
    assert _shape(out) == [(THINKING, "start", None, False), (THINKING, "end", "", False)]


def test_a_completion_without_a_start_still_opens_the_block_it_fills():
    out = _feed(_tr(), "item/completed", item={"type": "reasoning", "id": "rs9", "summary": ["Late"]})
    assert [p for _k, p, _t, _n in _shape(out)] == ["start", "delta", "end"]
    assert _feed(_tr(), "item/completed", item={"type": "reasoning", "id": "rs9", "summary": []}) == []


def test_an_interrupted_turn_closes_its_open_reasoning():
    tr = _tr()
    out = _feed(tr, "item/started", item={"type": "reasoning", "id": "rs1"})
    out += _feed(tr, "item/reasoning/summaryTextDelta", itemId="rs1", summaryIndex=0, delta="Half")
    out += _feed(tr, "turn/completed", turn={"status": "interrupted"})
    assert [(e.type, e.data.get("phase")) for e in out][-2:] == [(THINKING, "end"), (DONE, None)]


# --- commentary: the progress note -------------------------------------------

def test_a_commentary_message_streams_as_a_note_and_the_answer_as_text():
    tr = _tr()
    out = _feed(tr, "item/started", item={"type": "agentMessage", "id": "m1", "phase": "commentary"})
    out += _feed(tr, "item/agentMessage/delta", itemId="m1", delta="I'll list it")
    out += _feed(tr, "item/agentMessage/delta", itemId="m1", delta=" first.")
    out += _feed(tr, "item/completed", item={"type": "agentMessage", "id": "m1",
                                              "phase": "commentary", "text": "I'll list it first."})
    out += _feed(tr, "item/started", item={"type": "agentMessage", "id": "m2", "phase": "final_answer"})
    out += _feed(tr, "item/agentMessage/delta", itemId="m2", delta="Listed.")
    out += _feed(tr, "item/completed", item={"type": "agentMessage", "id": "m2",
                                              "phase": "final_answer", "text": "Listed."})
    assert _shape(out) == [
        (TEXT, None, "I'll list it", True),
        (TEXT, None, " first.", True),
        (TEXT, None, "Listed.", False),
    ]


def test_commentary_known_only_at_completion_is_folded_by_the_note_mark_logged_once(caplog):
    tr = _tr()
    with caplog.at_level(logging.INFO, logger="codex-layer"):
        for n in (1, 2):
            out = _feed(tr, "item/started", item={"type": "agentMessage", "id": f"m{n}"})
            out += _feed(tr, "item/agentMessage/delta", itemId=f"m{n}", delta="Now the list.")
            out += _feed(tr, "item/completed", item={"type": "agentMessage", "id": f"m{n}",
                                                      "phase": "commentary", "text": "Now the list."})
            # Streamed as text, then the mark: the pump and the page fold it.
            assert _shape(out) == [(TEXT, None, "Now the list.", False), (TEXT, None, "", True)]
            assert is_note_mark(out[-1].data)
    assert sum("only at completion" in r.message for r in caplog.records) == 1


def test_an_answer_that_ends_as_the_final_answer_gets_no_mark():
    tr = _tr()
    for n, phase in enumerate(("final_answer", None)):
        out = _feed(tr, "item/started", item={"type": "agentMessage", "id": f"m{n}"})
        out += _feed(tr, "item/agentMessage/delta", itemId=f"m{n}", delta="Done.")
        out += _feed(tr, "item/completed", item={"type": "agentMessage", "id": f"m{n}",
                                                  "phase": phase, "text": "Done."})
        assert _shape(out) == [(TEXT, None, "Done.", False)]


def test_a_commentary_that_never_streamed_is_sent_whole_as_a_note():
    tr = _tr()
    out = _feed(tr, "item/started", item={"type": "agentMessage", "id": "m1"})
    out += _feed(tr, "item/completed", item={"type": "agentMessage", "id": "m1",
                                              "phase": "commentary", "text": "Checking."})
    assert _shape(out) == [(TEXT, None, "Checking.", True)]


def test_a_note_whose_phase_changes_keeps_its_words_and_warns(caplog):
    tr = _tr()
    with caplog.at_level(logging.WARNING, logger="codex-layer"):
        out = _feed(tr, "item/started", item={"type": "agentMessage", "id": "m1", "phase": "commentary"})
        out += _feed(tr, "item/agentMessage/delta", itemId="m1", delta="Answer.")
        out += _feed(tr, "item/completed", item={"type": "agentMessage", "id": "m1",
                                                  "phase": "final_answer", "text": "Answer."})
    assert _shape(out) == [(TEXT, None, "Answer.", True)]
    assert any("ended as" in r.message for r in caplog.records)


def test_a_sub_agent_threads_commentary_never_reaches_the_main_chat():
    tr = _tr()
    other = {"threadId": "thread-SUB"}
    assert tr.translate(CodexEvent(type="item/started", data={
        **other, "item": {"type": "agentMessage", "id": "s1", "phase": "commentary"}})) == []
    assert tr.translate(CodexEvent(type="item/agentMessage/delta", data={
        **other, "itemId": "s1", "delta": "sub prose"})) == []


def test_reasoning_and_note_deltas_light_an_open_command_as_a_text_delta_does():
    _bg_command_registries.clear()
    for method, start_item, delta_params in (
        ("item/reasoning/summaryTextDelta", {"type": "reasoning", "id": "rs1"},
         {"itemId": "rs1", "summaryIndex": 0, "delta": "Waiting on it"}),
        ("item/agentMessage/delta", {"type": "agentMessage", "id": "m1", "phase": "commentary"},
         {"itemId": "m1", "delta": "It runs on."}),
    ):
        tr = _tr(sid=f"s-{method}")
        _feed(tr, "item/started", item={"type": "commandExecution", "id": "c1",
                                         "command": "sleep 40", "processId": None})
        _feed(tr, "item/started", item=start_item)
        out = _feed(tr, method, **delta_params)
        assert [e.type for e in out][0] == BG_COMMAND_START
    _bg_command_registries.clear()


# --- an API that refuses the summary -------------------------------------------

_REFUSAL = {"message": "unexpected status 400 Bad Request: Your organization must be verified to "
                       "generate reasoning summaries.", "codexErrorInfo": "badRequest"}


class _RefusingClient(_Client):
    """The first turn is refused (an ``error`` then a failed
    ``turn/completed``); every later turn completes."""

    def __init__(self, session_ref: list, *, error_first: bool = True):
        super().__init__()
        self._session = session_ref
        self._error_first = error_first

    async def request(self, method, params=None, *, timeout=None):
        res = await super().request(method, params, timeout=timeout)
        if method == "turn/start":
            s = self._session[0]
            n = sum(1 for m, _ in self.requests if m == "turn/start")
            q = s._default_consumer
            if n == 1:
                if self._error_first:
                    q.put_nowait(("error", {"threadId": "thr-1", "error": _REFUSAL, "willRetry": False}))
                q.put_nowait(("turn/completed", {"threadId": "thr-1",
                                                 "turn": {"status": "failed", "error": _REFUSAL}}))
            else:
                q.put_nowait(("item/agentMessage/delta", {"threadId": "thr-1", "itemId": "m1", "delta": "Hi."}))
                q.put_nowait(("turn/completed", {"threadId": "thr-1", "turn": {"status": "completed"}}))
        return res


@pytest.mark.asyncio
@pytest.mark.parametrize("error_first", [True, False])
async def test_a_refused_summary_runs_the_turn_again_without_it_once_for_the_session(caplog, error_first):
    ref: list = []
    s = CodexAppServerSession(
        session_id="s-ref", agent_name="a", model="gpt-6.1-sol",
        sandbox_mode="workspace-write", working_dir="/tmp",
        config_dir="/tmp/.codex", thread_id="thr-1", effort="medium",
    )
    ref.append(s)
    s._started = True
    s._client = _RefusingClient(ref, error_first=error_first)
    with caplog.at_level(logging.WARNING, logger="codex-session"):
        events = [e async for e in s.send_message("hello")]
    # The refused turn never reaches the chat; the retried one does.
    assert [e.type for e in events] == ["item/agentMessage/delta", "turn/completed"]
    starts = [p for m, p in s._client.requests if m == "turn/start"]
    assert [("summary" in p) for p in starts] == [True, False]
    assert s.summary_refused is True
    assert sum("refused the reasoning summary" in r.message for r in caplog.records) == 1
    # The next turn does not ask again.
    events = [e async for e in s.send_message("again")]
    starts = [p for m, p in s._client.requests if m == "turn/start"]
    assert "summary" not in starts[-1]


def test_only_the_refusal_words_count_as_a_refused_summary():
    from core.layers.codex.helpers import summary_refused
    assert summary_refused({"error": _REFUSAL})
    assert summary_refused({"turn": {"status": "failed", "error": {
        "message": "400 Bad Request: invalid value for reasoning.summary"}}})
    assert not summary_refused({"error": {"message": "usage limit reached", "codexErrorInfo": "usageLimitExceeded"}})
    assert not summary_refused({"error": {"message": "reasoning.summary is great"}})
    assert not summary_refused(None)
