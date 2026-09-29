"""Meeting-aware hook routing — ``api.hooks.routing.resolve_hook_route``.

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
from unittest.mock import AsyncMock, patch

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
    with patch("api.hooks.lifecycle.verify_session_match_async", new_callable=AsyncMock):
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
    with patch("api.hooks.artifacts.verify_session_match_async"):
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
    with patch("api.hooks.artifacts.verify_session_match_async"):
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
    with patch("api.hooks.lifecycle.verify_session_match_async", new_callable=AsyncMock), \
         patch("api.hooks.lifecycle.push_pump_event", return_value=True) as push:
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


# ───────────────── the turn-end backstop in decide_tool_permission ───────────
#
# Once a meeting participant's hook has allowed a routing tool, every later
# tool call in the same turn is denied (the orchestrator routes only at the
# turn boundary — a participant that keeps working stalls the whole meeting;
# observed live 2026-09-09, 304 s and 27 API turns after the first direct_to).

from api.hooks.hooks import decide_tool_permission  # noqa: E402
from auth.path_policy import SecurityContext  # noqa: E402
from core.session.session_state import (  # noqa: E402
    clear_meeting_turn_routed,
    get_meeting_session_info,
    set_session_mode,
)


def _register_meeting_session(sid: str, *, is_moderator: bool) -> None:
    session_state.register_session_state(sid, "auto", SecurityContext(
        role="manager", username="", agent="agent-a", is_admin_agent=False,
        session_scope="agent",
    ))
    set_session_mode("parent-sess-b", "dontAsk")
    set_meeting_session_info(
        sid, "parent-sess-b", "meeting-m2", "agent-a", "parent-chat-2",
        is_moderator=is_moderator,
    )


@pytest.fixture
def moderator_session():
    sid = "mod-sess-1"
    _register_meeting_session(sid, is_moderator=True)
    yield sid
    cleanup_meeting_session_info(sid)
    session_state.cleanup_session_permission_state(sid)
    session_state._session_modes.pop("parent-sess-b", None)


@pytest.fixture
def participant_session():
    sid = "part-sess-2"
    _register_meeting_session(sid, is_moderator=False)
    yield sid
    cleanup_meeting_session_info(sid)
    session_state.cleanup_session_permission_state(sid)
    session_state._session_modes.pop("parent-sess-b", None)


async def _decide(sid, tool, tool_input=None):
    return await decide_tool_permission(sid, tool, tool_input or {"command": "ls"})


@pytest.mark.asyncio
async def test_tools_run_freely_before_routing(moderator_session):
    assert (await _decide(moderator_session, "Bash"))["decision"] == "allow"
    assert resolve_hook_route(moderator_session).routed_tool == ""


@pytest.mark.asyncio
async def test_direct_to_marks_the_turn_and_later_tools_are_denied(moderator_session):
    allowed = await _decide(moderator_session, "mcp__meetings-mcp__direct_to",
                            {"agents": ["agent-b"]})
    assert allowed["decision"] == "allow"
    assert resolve_hook_route(moderator_session).routed_tool == "direct_to"

    denied = await _decide(moderator_session, "Bash")
    assert denied["decision"] == "deny"
    assert "direct_to" in denied["reason"] and "next turn" in denied["reason"]
    # Read-only tools are denied too — the point is ending the turn.
    assert (await _decide(moderator_session, "Read", {"file_path": "/x"}))["decision"] == "deny"
    # Memory writes are the one exception, after any routing call.
    assert (await _decide(moderator_session, "mcp__memory-mcp__memory",
                          {"command": "view", "path": "/memories/agent/x.md"}))["decision"] == "allow"


@pytest.mark.asyncio
async def test_meeting_tools_and_their_schema_load_stay_allowed(moderator_session):
    await _decide(moderator_session, "mcp__meetings-mcp__direct_to", {"agents": ["agent-b"]})
    ok = await _decide(moderator_session, "ToolSearch",
                       {"query": "select:mcp__meetings-mcp__end_meeting"})
    assert ok["decision"] == "allow"
    other = await _decide(moderator_session, "ToolSearch", {"query": "select:Bash"})
    assert other["decision"] == "deny"
    again = await _decide(moderator_session, "mcp__meetings-mcp__end_meeting",
                          {"meeting_id": "m2"})
    assert again["decision"] == "allow"
    # The closing call supersedes the earlier direct_to…
    assert resolve_hook_route(moderator_session).routed_tool == "end_meeting"
    # …and everything but the meeting tools and memory stays denied.
    memory = await _decide(moderator_session, "mcp__memory-mcp__memory",
                           {"command": "str_replace", "path": "/memories/agent/x.md"})
    assert memory["decision"] == "allow"
    assert (await _decide(moderator_session, "Bash"))["decision"] == "deny"


@pytest.mark.asyncio
async def test_end_meeting_reason_depends_on_the_summary_written(moderator_session):
    await _decide(moderator_session, "mcp__meetings-mcp__end_meeting", {"meeting_id": "m2"})
    thin = await _decide(moderator_session, "Bash")
    assert "Write the meeting summary now" in thin["reason"]
    get_meeting_session_info(moderator_session)["turn_text_chars"] = 900
    full = await _decide(moderator_session, "Bash")
    assert "summary you wrote above" in full["reason"]


@pytest.mark.asyncio
async def test_participant_end_meeting_is_not_a_routing(participant_session):
    # The API refuses it (moderator only) and the model recovers with
    # propose_conclude — the turn must not be closed on it.
    await _decide(participant_session, "mcp__meetings-mcp__end_meeting", {"meeting_id": "m2"})
    assert resolve_hook_route(participant_session).routed_tool == ""
    assert (await _decide(participant_session, "Bash"))["decision"] == "allow"
    await _decide(participant_session, "mcp__meetings-mcp__propose_conclude", {"meeting_id": "m2"})
    denied = await _decide(participant_session, "Bash")
    assert denied["decision"] == "deny" and "propose_conclude" in denied["reason"]
    assert (await _decide(participant_session, "mcp__memory-mcp__memory",
                          {"command": "view", "path": "/memories/agent/x.md"}))["decision"] == "allow"


@pytest.mark.asyncio
async def test_clearing_the_turn_state_reopens_tools(moderator_session):
    await _decide(moderator_session, "mcp__meetings-mcp__direct_to", {"agents": ["agent-b"]})
    assert (await _decide(moderator_session, "Bash"))["decision"] == "deny"
    clear_meeting_turn_routed(moderator_session)
    assert (await _decide(moderator_session, "Bash"))["decision"] == "allow"
    assert get_meeting_session_info(moderator_session)["routed_tool"] == ""


@pytest.mark.asyncio
async def test_non_meeting_sessions_are_untouched():
    sid = "plain-sess-9"
    session_state.register_session_state(sid, "dontAsk", SecurityContext(
        role="manager", username="", agent="agent-a", is_admin_agent=False,
        session_scope="agent",
    ))
    try:
        await _decide(sid, "mcp__meetings-mcp__direct_to", {"agents": ["agent-b"]})
        assert (await _decide(sid, "Bash"))["decision"] == "allow"
    finally:
        session_state.cleanup_session_permission_state(sid)


@pytest.mark.asyncio
async def test_codex_named_meeting_tool_marks_the_turn(moderator_session, monkeypatch):
    # Codex's hook sanitizes the server key: mcp__meetings_mcp__direct_to.
    # The Codex layer's canonical_tool_name restores it on the way into the
    # authority, so the backstop (and the manifest's open tier) see the
    # same name Claude sends.
    from pathlib import Path
    import config as app_config
    from core.session.session_manager import get_layer_by_path
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session",
                        lambda sid: get_layer_by_path("codex-cli"))
    from services.mcp import mcp_registry as reg
    from services.mcp.mcp_manifest_types import (
        CredentialConfig, McpManifest, ServerConfig,
    )
    manifest = McpManifest(
        name="meetings-mcp", label="Meetings", description="d", version="1.0.0",
        category="core", server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], server_name="", permissions=None,
        mcp_dir=Path(app_config.MCPS_DIR) / "custom" / "meetings-mcp",
    )
    monkeypatch.setattr(reg, "_manifests", {"meetings-mcp": manifest})
    allowed = await _decide(moderator_session, "mcp__meetings_mcp__direct_to",
                            {"agents": ["agent-b"]})
    assert allowed["decision"] == "allow"
    assert resolve_hook_route(moderator_session).routed_tool == "direct_to"
    assert (await _decide(moderator_session, "Bash"))["decision"] == "deny"
