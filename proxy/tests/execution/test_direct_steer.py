"""A message typed during a Direct LLM turn is read at its next tool boundary.

Load-bearing: an accepted text is delivered exactly once, at the next tool
boundary, or as the next call of the same turn when the model has finished;
the door closes before the turn can no longer read one, so a later text goes
to the queue; a parked permission card refuses; a message with photos waits
for the next turn; an abort drops what waits, and every other way out carries
it into the next turn.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from auth.path_policy import SecurityContext
from core.layers.direct import layer as direct_layer, session as direct_session_module
from core.layers.direct.session import DirectSession, run_direct_stream
from core.layers.providers.base import (
    ProviderAdapter,
    ProviderError,
    ProviderStreamEvent,
    ProviderUsage,
)
from core.layers.providers.registry import register_adapter
from core.session.session_state import set_session_mode, set_session_security

_PROVIDER = "stub-steer-test"
AGENT = "pa"


class _Adapter(ProviderAdapter):
    """Replays one scripted event list per API call; ``during`` runs inside
    a call, before it finishes, as a person typing does."""

    scripts: list[list] = []
    seen: list[list[dict]] = []
    during: list = []

    @property
    def provider_name(self) -> str:
        return _PROVIDER

    async def stream_response(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
        self.seen.append([dict(m) for m in kwargs["messages"]])
        if self.during:
            await self.during.pop(0)()
        script = self.scripts.pop(0) if self.scripts else _final("done")
        for ev in script:
            if isinstance(ev, Exception):
                raise ev
            yield ev

    def format_tool_results(self, results):
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["tool_use_id"], "content": r["content"]}
            for r in results
        ]}]

    def serialize_assistant_content(self, raw_content):
        return raw_content


_ADAPTER = _Adapter()
register_adapter(_ADAPTER)


def _tool_call(tool_id: str = "t1") -> list[ProviderStreamEvent]:
    return [
        ProviderStreamEvent(type="tool_start", tool_name="mcp__stub__ping", tool_id=tool_id),
        ProviderStreamEvent(type="tool_stop", tool_name="mcp__stub__ping", tool_id=tool_id,
                            tool_input_json=json.dumps({})),
        ProviderStreamEvent(type="usage", usage=ProviderUsage(input_tokens=1, output_tokens=1)),
        ProviderStreamEvent(type="content", raw_content="(calling)"),
        ProviderStreamEvent(type="stop", stop_reason="tool_use"),
    ]


def _final(text: str) -> list[ProviderStreamEvent]:
    return [
        ProviderStreamEvent(type="text_delta", text=text),
        ProviderStreamEvent(type="usage", usage=ProviderUsage(input_tokens=1, output_tokens=1)),
        ProviderStreamEvent(type="content", raw_content=text),
        ProviderStreamEvent(type="stop", stop_reason="end_turn"),
    ]


class _Tools:
    def __init__(self):
        self.calls: list = []

    def tools_lost(self):
        return False

    async def execute_tools(self, tool_calls):
        self.calls.extend(tool_calls)
        return [{"tool_use_id": tc["id"], "content": "pong"} for tc in tool_calls]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr("core.layers.direct.session.config.get_agent_model", lambda agent: "")
    session = DirectSession(
        session_id=f"steer-{uuid.uuid4().hex[:8]}", agent_name=AGENT,
        system_prompt="A test agent.", provider=_PROVIDER,
    )
    session.model = "test-model"
    session.api_key = "stub-key"
    session.mcp_manager = _Tools()
    set_session_security(session.session_id, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False,
    ))
    set_session_mode(session.session_id, "dontAsk")
    _ADAPTER.scripts = []
    _ADAPTER.seen = []
    _ADAPTER.during = []
    layer = direct_layer.DirectLLMExecutionLayer()
    monkeypatch.setitem(direct_session_module._direct_sessions, session.session_id, session)
    # The running turn, as ``_send_turn`` registers it.
    monkeypatch.setattr(direct_layer.DirectLLMExecutionLayer, "_active_streams", {session.session_id: object()})
    return session, layer


async def _turn(session, prompt="hi"):
    out = []
    async for ev in run_direct_stream(session, prompt):
        out.append(ev)
    return out


def _user_texts(messages: list[dict]) -> list:
    return [m["content"] for m in messages if m["role"] == "user"]


# -- the boundary -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_typed_during_a_tool_call_is_read_at_the_next_boundary(world):
    session, layer = world

    async def type_it():
        assert await layer.steer(session.session_id, "also check the logs") is True

    _ADAPTER.scripts = [_tool_call(), _final("done")]
    _ADAPTER.during = [type_it]
    events = await _turn(session)
    assert [e["type"] for e in events][-2:] == ["metadata", "done"]
    # Sent once, in the call after the tools, as its own user message beside their results.
    before, after = _ADAPTER.seen
    assert not any("logs" in json.dumps(m) for m in before)
    assert sum("also check the logs" in json.dumps(m) for m in after) == 1
    assert after[-1] == {"role": "user", "content": [{"type": "text", "text": "also check the logs"}]}
    assert session.steers == []


@pytest.mark.asyncio
async def test_a_message_typed_after_the_last_tool_is_the_next_call_of_the_same_turn(world):
    session, layer = world

    async def type_it():
        assert await layer.steer(session.session_id, "one more thing") is True

    _ADAPTER.scripts = [_final("first answer"), _final("second answer")]
    _ADAPTER.during = [type_it]
    events = await _turn(session)
    assert len(_ADAPTER.seen) == 2
    assert _ADAPTER.seen[1][-1]["content"] == [{"type": "text", "text": "one more thing"}]
    assert [e["type"] for e in events].count("done") == 1


@pytest.mark.asyncio
async def test_two_messages_arrive_together(world):
    session, layer = world

    async def type_them():
        assert await layer.steer(session.session_id, "first")
        assert await layer.steer(session.session_id, "second")

    _ADAPTER.scripts = [_tool_call(), _final("done")]
    _ADAPTER.during = [type_them]
    await _turn(session)
    assert _ADAPTER.seen[1][-1]["content"] == [{"type": "text", "text": "first\n\nsecond"}]


# -- the door -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_running_turn_takes_no_message(world):
    session, layer = world
    direct_layer.DirectLLMExecutionLayer._active_streams.clear()
    assert await layer.steer(session.session_id, "hello") is False
    session.steer_open = True
    assert await layer.steer(session.session_id, "hello") is False
    assert session.steers == []


@pytest.mark.asyncio
async def test_an_unknown_session_or_an_empty_text_is_refused(world):
    session, layer = world
    session.steer_open = True
    assert await layer.steer("no-such-session", "hello") is False
    assert await layer.steer(session.session_id, "") is False


@pytest.mark.asyncio
async def test_a_parked_permission_card_refuses(world, monkeypatch):
    session, layer = world
    session.steer_open = True
    from core.session import session_state
    monkeypatch.setattr(session_state, "has_pending_prompt", lambda _sid: True)
    assert await layer.steer(session.session_id, "hello") is False
    assert session.steers == []


@pytest.mark.asyncio
async def test_the_door_closes_before_the_turns_last_frames(world):
    """After the model's last call nothing can read a message, so the turn's
    metadata and its end are behind a closed door."""
    session, layer = world
    _ADAPTER.scripts = [_final("done")]
    during_tail: list = []
    async for ev in run_direct_stream(session, "hi"):
        if ev["type"] in ("metadata", "done"):
            during_tail.append(await layer.steer(session.session_id, "too late"))
    assert during_tail == [False, False]
    assert session.steer_open is False and session.steers == []


@pytest.mark.asyncio
async def test_the_door_is_shut_between_turns(world):
    session, layer = world
    _ADAPTER.scripts = [_final("done")]
    assert session.steer_open is False
    await _turn(session)
    assert session.steer_open is False
    assert await layer.steer(session.session_id, "between turns") is False


# -- the other ways out -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_abort_drops_what_waits(world):
    session, layer = world

    async def type_and_abort():
        await layer.steer(session.session_id, "never mind")
        raise asyncio.CancelledError

    _ADAPTER.scripts = [_tool_call()]
    _ADAPTER.during = [type_and_abort]
    with pytest.raises(asyncio.CancelledError):
        await _turn(session)
    assert session.steers == [] and session.steer_open is False
    assert not any("never mind" in json.dumps(m) for m in session.messages)


@pytest.mark.asyncio
async def test_a_provider_error_carries_the_text_into_the_next_turn(world):
    session, layer = world

    async def type_it():
        await layer.steer(session.session_id, "keep this")

    _ADAPTER.scripts = [[ProviderError("the provider is down", status_code=500)]]
    _ADAPTER.during = [type_it]
    events = await _turn(session)
    assert any(e["type"] == "error" for e in events)
    # Beside the error branch's own clean-up of a trailing bare user string.
    assert session.messages[-1] == {"role": "user", "content": [{"type": "text", "text": "keep this"}]}
    assert session.steers == [] and session.steer_open is False


@pytest.mark.asyncio
async def test_the_tool_loops_cap_carries_the_text(world, monkeypatch):
    session, layer = world
    monkeypatch.setattr(direct_session_module, "MAX_TOOL_LOOPS", 1)

    async def type_it():
        await layer.steer(session.session_id, "late at the cap")

    _ADAPTER.scripts = [_tool_call()]
    _ADAPTER.during = [type_it]
    await _turn(session)
    assert any("late at the cap" in json.dumps(m) for m in session.messages)
    assert session.steers == []


def test_a_direct_engine_declares_steering():
    assert direct_layer._DIRECT_CAPABILITIES.behaviour.supports_steer is True


# -- what the dashboard asks -----------------------------------------------------


def _dispatch(images: bool):
    """What ``_steer_possible`` reads of a connection, a pump and a message."""
    from core.session import session_kind
    from ws import dashboard_dispatch as dd
    layer = SimpleNamespace(capabilities_for=lambda _sid: SimpleNamespace(behaviour=SimpleNamespace(
        supports_steer=True, attach_images_inline=images)))
    conn = SimpleNamespace(session_id="s-1", layer=layer)
    pump = SimpleNamespace(source_type=session_kind.DASHBOARD.source_type)
    return dd, conn, pump


def _steer_possible():
    import inspect

    from ws import dashboard_dispatch as dd
    for _name, klass in inspect.getmembers(dd, inspect.isclass):
        if "_steer_possible" in klass.__dict__:
            return klass.__dict__["_steer_possible"]
    raise AssertionError("the dashboard's steer rule is gone")


def test_a_message_with_photos_waits_on_an_engine_that_takes_them_with_a_turn():
    _dd, conn, pump = _dispatch(images=True)
    rule = _steer_possible()
    assert rule(conn, pump, SimpleNamespace(images=[{"base64": "x"}])) is False
    assert rule(conn, pump, SimpleNamespace(images=[])) is True
    assert rule(conn, pump) is True


def test_a_cli_engine_steers_a_message_with_photos():
    """Its steer carries the photos' paths in the text."""
    _dd, conn, pump = _dispatch(images=False)
    assert _steer_possible()(conn, pump, SimpleNamespace(images=[{"path": "a.png"}])) is True
