"""Meeting-aware hook routing — ``api.hooks.hooks.resolve_hook_route``.

Meeting participants run their own CLI sessions but stream through the
MEETING's pump (session ``meeting-<id>``) into the parent chat, while every
hook posts the PARTICIPANT's session_id. Before the resolver, the out-of-band
hook families (tool results, images/media artifacts, SubagentStop) posted to
the participant's own permission queue — which no pump ever reads — so tool
result bodies and artifacts were silently dropped and subagent completions
never resolved. These tests lock the single-chokepoint rebinding.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import app
from api.hooks.hooks import resolve_hook_route, resolve_hook_chat_id
from core.events.bg_command_state import (
    _bg_command_registries, get_bg_command_registry,
)
from core.session import session_state
from core.session.session_state import (
    _chat_streaming_state,
    cleanup_meeting_session_info,
    get_permission_queue,
    get_subagent_registry,
    resolve_bg_command,
    set_meeting_session_info,
)
from services.meetings import meeting_orchestrator as mo


client = TestClient(app)


@pytest.fixture
def meeting_participant():
    """Register one meeting participant session mapping; clean up after."""
    sid = "part-sess-1"
    set_meeting_session_info(
        sid, "parent-sess-1", "meeting-m1", "agent-a", "parent-chat-1",
    )
    yield sid
    cleanup_meeting_session_info(sid)
    session_state._permission_emitters.pop(sid, None)
    session_state._permission_emitters.pop("meeting-m1", None)
    session_state._subagent_registries.pop(sid, None)
    _chat_streaming_state.pop("parent-chat-1", None)


def _drain(queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


# ───────────────────────── resolver unit behaviour ───────────────────────────


def test_resolver_identity_for_normal_session():
    route = resolve_hook_route("plain-sess")
    assert route.queue_session_id == "plain-sess"
    assert route.chat_id == ""
    assert route.meeting_agent == ""
    assert route.is_meeting is False


def test_resolver_rebinds_meeting_participant(meeting_participant):
    route = resolve_hook_route(meeting_participant)
    assert route.queue_session_id == "meeting-m1"
    assert route.chat_id == "parent-chat-1"
    assert route.meeting_agent == "agent-a"
    assert route.parent_session_id == "parent-sess-1"
    assert route.is_meeting is True


@pytest.mark.asyncio
async def test_hook_chat_id_uses_parent_chat_for_participant(meeting_participant):
    assert await resolve_hook_chat_id(meeting_participant) == "parent-chat-1"


@pytest.mark.asyncio
async def test_hook_chat_id_falls_back_to_session_chat_row(temp_db):
    temp_db.create_chat("chat-x", "user-admin", "a1")
    temp_db.update_chat("chat-x", session_id="sess-x")
    assert await resolve_hook_chat_id("sess-x") == "chat-x"
    assert await resolve_hook_chat_id("sess-unknown") == ""


# ───────────────────── hook endpoints route to the pump queue ────────────────


def test_tool_result_hook_routes_to_meeting_pump_queue(meeting_participant):
    with patch("api.hooks.hooks.verify_session_match"):
        resp = client.post(
            "/v1/hooks/tool-result",
            json={"session_id": meeting_participant, "tool_name": "Bash",
                  "tool_use_id": "tu1", "summary": "ls", "result_content": "ok"},
            headers={"Authorization": "Bearer dummy"},
        )
    assert resp.status_code == 200
    pump_items = _drain(get_permission_queue("meeting-m1"))
    assert [i["event_type"] for i in pump_items] == ["tool_result"]
    assert pump_items[0]["result_content"] == "ok"
    # The participant's own queue (never read by any pump) stays empty.
    assert _drain(get_permission_queue(meeting_participant)) == []


def test_images_hook_routes_to_meeting_pump_queue(meeting_participant):
    with patch("api.hooks.hooks.verify_session_match"):
        resp = client.post(
            "/v1/hooks/images",
            json={"session_id": meeting_participant,
                  "images": [{"url": "https://cdn.example.com/x.jpg"}]},
            headers={"Authorization": "Bearer dummy"},
        )
    assert resp.status_code == 200
    pump_items = _drain(get_permission_queue("meeting-m1"))
    assert [i["event_type"] for i in pump_items] == ["images"]
    assert _drain(get_permission_queue(meeting_participant)) == []


def test_url_hook_identity_for_normal_session():
    with patch("api.hooks.hooks.verify_session_match"):
        resp = client.post(
            "/v1/hooks/url",
            json={"session_id": "plain-sess-url", "url": "https://x", "title": "t"},
            headers={"Authorization": "Bearer dummy"},
        )
    assert resp.status_code == 200
    items = _drain(get_permission_queue("plain-sess-url"))
    assert [i["event_type"] for i in items] == ["url"]
    session_state._permission_emitters.pop("plain-sess-url", None)


# ───────────────────── SubagentStop resolves to the parent chat ──────────────


def test_subagent_stop_resolves_to_parent_chat(meeting_participant, temp_db):
    reg = get_subagent_registry(meeting_participant)
    reg.register_spawn("ag1", "tu-ag1")
    # Live state on the PARENT chat holds the badge the stop must clear.
    _chat_streaming_state["parent-chat-1"] = {
        "session_id": "meeting-m1",
        "active_agents": [{"tool_use_id": "tu-ag1", "active": True}],
    }
    with patch("api.hooks.hooks.verify_session_match"), \
         patch("api.hooks.hooks.push_pump_event", return_value=True) as push:
        resp = client.post(
            "/v1/hooks/subagent",
            json={"session_id": meeting_participant, "agent_id": "ag1"},
            headers={"Authorization": "Bearer dummy"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert reg.has_pending is False
    # Badge cleared on the parent chat's live state, WS event pushed there too.
    assert _chat_streaming_state["parent-chat-1"]["active_agents"][0]["active"] is False
    push.assert_called_once_with(
        "parent-chat-1", {"type": "bg_agent_done", "tool_use_id": "tu-ag1"},
    )


# ──────────────── orchestrator: participant bg-monitor starter ───────────────


@pytest.mark.asyncio
async def test_participant_bg_monitor_starter_binds_and_launches(monkeypatch):
    sid = "part-sess-mon"
    reg = get_subagent_registry(sid)
    reg.register_spawn("agX", "tu-agX")
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("shellY", "tu-shY")
    fake_layer = object()
    mo._meeting_session_layers[sid] = fake_layer
    started: list[tuple] = []

    async def fake_agent_monitor(layer, s, c, n):
        started.append(("agents", layer, s, c, n))

    async def fake_cmd_monitor(layer, s, c, n):
        started.append(("commands", layer, s, c, n))

    monkeypatch.setattr(mo, "_bg_agent_monitor", fake_agent_monitor)
    monkeypatch.setattr(mo, "_bg_command_monitor", fake_cmd_monitor)
    try:
        mo._start_participant_bg_monitors("agent-a", sid, "parent-chat-9")
        await asyncio.sleep(0)  # let create_task run the stub monitors
        assert reg.chat_id == "parent-chat-9"
        assert bgreg.chat_id == "parent-chat-9"
        assert sorted(started) == [
            ("agents", fake_layer, sid, "parent-chat-9", 1),
            ("commands", fake_layer, sid, "parent-chat-9", 1),
        ]
    finally:
        mo._meeting_session_layers.pop(sid, None)
        session_state._subagent_registries.pop(sid, None)
        _bg_command_registries.pop(sid, None)


@pytest.mark.asyncio
async def test_participant_bg_monitor_starter_noop_without_pending(monkeypatch):
    sid = "part-sess-idle"
    mo._meeting_session_layers[sid] = object()
    started: list[str] = []

    async def fake_monitor(layer, s, c, n):
        started.append(s)

    monkeypatch.setattr(mo, "_bg_agent_monitor", fake_monitor)
    monkeypatch.setattr(mo, "_bg_command_monitor", fake_monitor)
    try:
        mo._start_participant_bg_monitors("agent-a", sid, "parent-chat-9")
        await asyncio.sleep(0)
        assert started == []
        # The parent-chat binding is stamped regardless (hooks fall back to it).
        assert get_subagent_registry(sid).chat_id == "parent-chat-9"
    finally:
        mo._meeting_session_layers.pop(sid, None)
        session_state._subagent_registries.pop(sid, None)
        _bg_command_registries.pop(sid, None)


def test_bg_command_completion_clears_parent_chat_badge():
    """With the parent-chat binding stamped, the stdout-drain resolution path
    clears the badge on the MEETING's chat live state."""
    sid = "part-sess-cmd"
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("sh1", "tu-cmd1")
    bgreg.chat_id = "parent-chat-cmd"
    _chat_streaming_state["parent-chat-cmd"] = {
        "session_id": "meeting-m1",
        "active_commands": [{"tool_use_id": "tu-cmd1", "active": True}],
    }
    try:
        assert resolve_bg_command(sid, "sh1", "completed") is True
        badge = _chat_streaming_state["parent-chat-cmd"]["active_commands"][0]
        assert badge["active"] is False
        assert bgreg.has_pending is False
    finally:
        _bg_command_registries.pop(sid, None)
        _chat_streaming_state.pop("parent-chat-cmd", None)


# ─────────────── pump: speaker identity on non-text blocks ───────────────────


@pytest.mark.asyncio
async def test_pump_stamps_speaker_on_non_text_blocks(temp_db):
    from core.events.common_events import (
        CommonEvent, THINKING, TOOL_RESULT, TOOL_USE,
    )
    from core.events.stream_pump import ChatStreamPump

    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(
        chat_id="meeting-chat-stamp", session_id="meeting-mstamp",
        producer=producer, event_queue=asyncio.Queue(), perm_queue=None,
    )
    try:
        pump._meeting_agent = "agent-a"
        await pump._process_event(CommonEvent(
            type=TOOL_USE, data={"name": "Bash", "tool_id": "t1"}))
        await pump._process_event(CommonEvent(
            type=TOOL_RESULT, data={"name": "Bash", "tool_id": "t1"}))
        await pump._process_event(CommonEvent(type=THINKING, data={"phase": "start"}))
        await pump._process_event(CommonEvent(type=THINKING, data={"text": "hmm"}))
        await pump._process_event(CommonEvent(type=THINKING, data={"phase": "end"}))
        await pump._handle_perm_event(
            {"event_type": "images", "images": [{"url": "https://x"}]})
        by_type = {b["type"]: b for b in pump._turn_blocks}
        assert by_type["tool"]["_meeting_agent"] == "agent-a"
        assert by_type["thinking"]["_meeting_agent"] == "agent-a"
        assert by_type["images"]["_meeting_agent"] == "agent-a"
    finally:
        producer.cancel()
        _chat_streaming_state.pop("meeting-chat-stamp", None)


# ──────────────── orchestrator: system-note (bg nudge) delivery ──────────────


def test_drain_system_notes_routes_nudges_to_moderator():
    class _Pump:
        system_queue = ["Your 2 background agent(s) have completed."]

    transcript: list[dict] = []
    pending: dict[str, list] = {"mod": [], "other": []}
    mo._drain_system_notes(_Pump(), transcript, pending, "mod")
    assert _Pump.system_queue == []
    assert len(transcript) == 1
    assert transcript[0]["agent"] == "system"
    assert transcript[0]["role"] == "system"
    assert pending["mod"] == [transcript[0]]
    assert pending["other"] == []
