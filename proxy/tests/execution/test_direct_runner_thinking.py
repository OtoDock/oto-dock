"""run_direct_stream thinking bracket — the start/delta/end phase contract the
Codex layer's translator emits, reproduced by the direct path from the
adapters' ``thinking_delta`` events.

Verifies:
- reasoning fragments open one ``start``, stream as ``delta``, and the first
  non-reasoning event closes with ``end`` — before the text that follows;
- a reasoning-only API call still closes its block (on ``stop``);
- a provider error after reasoning closes the block before the error event;
- thinking text never lands in the assistant message history;
- the note mark: the text right before a tool call in the same model call
  is a progress note, said by an empty note text before the tool's start.
"""

from collections.abc import AsyncIterator

import pytest

from core.layers.providers.base import (
    ProviderAdapter,
    ProviderError,
    ProviderStreamEvent,
    ProviderUsage,
)
from core.layers.providers.registry import register_adapter

_STUB_PROVIDER = "stub-thinking-test"


class _StubAdapter(ProviderAdapter):
    """Replays a scripted event list; ``script`` is set per test."""

    script: list = []

    @property
    def provider_name(self) -> str:
        return _STUB_PROVIDER

    async def stream_response(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
        for ev in self.script:
            if isinstance(ev, Exception):
                raise ev
            yield ev

    def format_tool_results(self, results):
        return []

    def serialize_assistant_content(self, raw_content):
        return raw_content if isinstance(raw_content, str) else ""


_ADAPTER = _StubAdapter()
register_adapter(_ADAPTER)


@pytest.fixture
def direct_session(monkeypatch):
    from core.layers.direct.session import DirectSession
    monkeypatch.setattr(
        "core.layers.direct.session.config.get_agent_model", lambda agent: "",
    )
    session = DirectSession(
        session_id="thinking-session", agent_name="test-agent",
        system_prompt="You are a test agent.", provider=_STUB_PROVIDER,
    )
    session.model = "test-model"
    session.api_key = "stub-test-key"
    return session


async def _collect(session, prompt="hi"):
    from core.layers.direct.session import run_direct_stream
    out = []
    async for ev in run_direct_stream(session, prompt):
        out.append(ev)
    return out


def _shape(events):
    shaped = []
    for ev in events:
        if ev["type"] == "thinking":
            shaped.append(("thinking", ev["data"]["phase"], ev["data"].get("text", "")))
        elif ev["type"] == "text":
            shaped.append(("text", ev["data"]["content"]))
        else:
            shaped.append((ev["type"],))
    return shaped


@pytest.mark.asyncio
async def test_thinking_bracket_precedes_text(direct_session):
    _ADAPTER.script = [
        ProviderStreamEvent(type="thinking_delta", text="a"),
        ProviderStreamEvent(type="thinking_delta", text="b"),
        ProviderStreamEvent(type="text_delta", text="x"),
        ProviderStreamEvent(type="usage", usage=ProviderUsage(input_tokens=5, output_tokens=2)),
        ProviderStreamEvent(type="content", raw_content="x"),
        ProviderStreamEvent(type="stop", stop_reason="end_turn"),
    ]
    events = await _collect(direct_session)
    assert _shape(events)[:6] == [
        ("session",),
        ("thinking", "start", ""), ("thinking", "delta", "a"),
        ("thinking", "delta", "b"), ("thinking", "end", ""),
        ("text", "x"),
    ]
    assert [e["type"] for e in events][-2:] == ["metadata", "done"]
    assert direct_session.messages[-1] == {"role": "assistant", "content": "x"}


@pytest.mark.asyncio
async def test_reasoning_only_call_still_closes(direct_session):
    _ADAPTER.script = [
        ProviderStreamEvent(type="thinking_delta", text="only"),
        ProviderStreamEvent(type="stop", stop_reason="end_turn"),
    ]
    events = await _collect(direct_session)
    phases = [e["data"]["phase"] for e in events if e["type"] == "thinking"]
    assert phases == ["start", "delta", "end"]
    assert not any(e["type"] == "text" for e in events)


@pytest.mark.asyncio
async def test_provider_error_closes_thinking_first(direct_session):
    _ADAPTER.script = [
        ProviderStreamEvent(type="thinking_delta", text="…"),
        ProviderError("boom", status_code=500),
    ]
    shaped = _shape(await _collect(direct_session))
    assert shaped[-2:] == [("thinking", "end", ""), ("error",)]


def test_thinking_translates_to_common_event():
    from core.events.common_events import THINKING
    from core.layers.direct.layer import direct_event_to_common
    ev = direct_event_to_common({"type": "thinking", "data": {"phase": "delta", "text": "t"}})
    assert ev is not None and ev.type == THINKING
    assert ev.data == {"phase": "delta", "text": "t"}


@pytest.mark.asyncio
async def test_no_thinking_no_bracket(direct_session):
    _ADAPTER.script = [
        ProviderStreamEvent(type="text_delta", text="plain"),
        ProviderStreamEvent(type="content", raw_content="plain"),
        ProviderStreamEvent(type="stop", stop_reason="end_turn"),
    ]
    events = await _collect(direct_session)
    assert not any(e["type"] == "thinking" for e in events)


# --- the note mark: the text right before a tool call in one model call ----

async def _call_frames(session, script):
    """The frames of ONE model call (one assistant message)."""
    from core.layers.direct.session import _DirectCall, _DirectUsage, _stream_call
    _ADAPTER.script = script
    return [f async for f in _stream_call(session, _ADAPTER, "k", _DirectCall(), _DirectUsage())]


def _tool(tool_id):
    return [ProviderStreamEvent(type="tool_start", tool_id=tool_id, tool_name="Glob"),
            ProviderStreamEvent(type="tool_input_delta", tool_input_json='{"pattern": "*"}'),
            ProviderStreamEvent(type="tool_stop", tool_id=tool_id, tool_name="Glob",
                                tool_input_json='{"pattern": "*"}')]


@pytest.mark.asyncio
async def test_the_text_before_a_tool_call_in_its_call_is_marked_a_note(direct_session):
    from core.events.common_events import NOTE_MARK
    frames = await _call_frames(direct_session, [
        ProviderStreamEvent(type="text_delta", text="I'll look "),
        ProviderStreamEvent(type="text_delta", text="at the folder."),
        *_tool("t1"), *_tool("t2"),
        ProviderStreamEvent(type="stop", stop_reason="tool_use"),
    ])
    shaped = [(f["type"], f["data"]) for f in frames]
    assert shaped == [
        ("text", {"content": "I'll look "}), ("text", {"content": "at the folder."}),
        ("text", NOTE_MARK), ("tool_start", {"name": "Glob", "tool_use_id": "t1"}),
        ("tool_start", {"name": "Glob", "tool_use_id": "t2"}),
    ]


@pytest.mark.asyncio
async def test_text_that_ends_a_call_reasoning_between_and_an_error_get_no_mark(direct_session):
    from core.events.common_events import is_note_mark

    def marks(frames):
        return [f for f in frames if f["type"] == "text" and is_note_mark(f["data"])]

    # The answer ends its call; the next call (the next message) opens with a tool.
    first = await _call_frames(direct_session, [ProviderStreamEvent(type="text_delta", text="Done."),
                                                ProviderStreamEvent(type="stop", stop_reason="end_turn")])
    second = await _call_frames(direct_session, [*_tool("t1"),
                                                 ProviderStreamEvent(type="stop", stop_reason="tool_use")])
    assert marks(first) == marks(second) == []
    between = await _call_frames(direct_session, [ProviderStreamEvent(type="text_delta", text="Hm."),
                                                  ProviderStreamEvent(type="thinking_delta", text="…"),
                                                  *_tool("t2")])
    assert marks(between) == []
    errored = await _call_frames(direct_session, [ProviderStreamEvent(type="text_delta", text="Let me"),
                                                  ProviderStreamEvent(type="error", text="overloaded")])
    assert marks(errored) == [] and errored[-1]["type"] == "error"
    blank = await _call_frames(direct_session, [ProviderStreamEvent(type="text_delta", text="\n"), *_tool("t3")])
    assert marks(blank) == []  # whitespace is not words
    # A server tool's result (web search) between the words and the next call.
    searched = await _call_frames(direct_session, [
        ProviderStreamEvent(type="text_delta", text="Found it."),
        ProviderStreamEvent(type="tool_result", tool_id="ws1", text="3 results"), *_tool("t4")])
    assert marks(searched) == []


def test_the_mark_translates_to_the_note_mark_event_and_an_empty_text_to_nothing():
    from core.events.common_events import NOTE_MARK, TEXT
    from core.layers.direct.layer import direct_event_to_common
    ev = direct_event_to_common({"type": "text", "data": dict(NOTE_MARK)})
    assert ev is not None and (ev.type, ev.data) == (TEXT, NOTE_MARK)
    assert direct_event_to_common({"type": "text", "data": {"content": ""}}) is None
