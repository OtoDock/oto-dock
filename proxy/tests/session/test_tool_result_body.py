"""Tool-result bodies on the pump's TOOL_RESULT path (Codex normal chats).

The CLI layer captures tool output via the PostToolUse hook (a `tool_result`
perm-queue item the pump attaches in `_handle_perm_event`); Codex has no hook
on the app-server path, so its translator now ships the completed item's
output ON the TOOL_RESULT CommonEvent (`result_content`/`is_error`). This
module locks the pump half: attach to the block, truncate with the shared
policy, forward the hook path's live `tool_result` frame (with tool_use_id —
name-matching mis-targets parallel same-name tools), keep the body out of
tool_end, and persist through `_save_turn_blocks` — the row the dashboard's
history reload renders the Output section from.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_tool_result_body.py -q
"""

import asyncio
import json

import pytest

from core.events.common_events import (
    CommonEvent, DONE, TOOL_INPUT, TOOL_RESULT, TOOL_USE,
)
from core.events.stream_pump import ChatStreamPump
from storage import database as task_store


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )


def _drain(q: asyncio.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


async def _run_tool(pump, *, result_data):
    await pump._process_event(CommonEvent(TOOL_USE, {
        "name": "Bash", "tool_id": "call_1",
    }))
    await pump._process_event(CommonEvent(TOOL_INPUT, {
        "name": "Bash", "summary": "printf alpha",
        "tool_input": {"command": "printf alpha"},
    }))
    await pump._process_event(CommonEvent(TOOL_RESULT, {
        "name": "Bash", "tool_id": "call_1", **result_data,
    }))


@pytest.mark.asyncio
async def test_result_body_attaches_forwards_and_persists(temp_db):
    temp_db.create_chat("tr1", "user-admin", "a1")
    pump = _mk_pump("tr1")
    try:
        q = pump.attach()
        await _run_tool(pump, result_data={
            "result_content": "alpha\nbeta\n", "is_error": False,
        })

        events = [f["event"] for f in _drain(q) if f["pump_type"] == "ws_event"]
        by_type = {e["type"]: e for e in events}
        # The hook path's live frame, with the exact-id targeting handle.
        res = by_type["tool_result"]
        assert res["tool_use_id"] == "call_1"
        assert res["result_content"] == "alpha\nbeta\n"
        assert res["summary"] == "3 lines"
        # tool_result rides its own frame — tool_end stays lean.
        assert "result_content" not in by_type["tool_end"]
        # Frame order matches the CLI path: PostToolUse before tool_end.
        assert [e["type"] for e in events][-2:] == ["tool_result", "tool_end"]

        # The block carries the body into _turn_blocks…
        blk = pump._turn_blocks[-1]
        assert blk["type"] == "tool" and blk["tool_result"] == "alpha\nbeta\n"
        assert blk["result_summary"] == "3 lines"
        assert blk["is_error"] is False

        # …and _save_turn_blocks persists it (the history-reload source) —
        # an off-loop writer job; await it.
        await pump._save_turn_blocks()
        rows = [m for m in task_store.get_chat_messages("tr1")
                if m.get("event_type") == "tool"]
        assert len(rows) == 1
        stored = json.loads(rows[0]["event_data"])
        assert stored["tool_result"] == "alpha\nbeta\n"
        assert stored["tool_input"] == {"command": "printf alpha"}
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_error_result_flags_block(temp_db):
    temp_db.create_chat("tr2", "user-admin", "a1")
    pump = _mk_pump("tr2")
    try:
        pump.attach()
        await _run_tool(pump, result_data={
            "result_content": "(exit 3)", "is_error": True,
        })
        blk = pump._turn_blocks[-1]
        assert blk["is_error"] is True and blk["tool_result"] == "(exit 3)"
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_result_body_is_truncated_by_the_shared_policy(temp_db):
    temp_db.create_chat("tr3", "user-admin", "a1")
    pump = _mk_pump("tr3")
    try:
        q = pump.attach()
        big = "\n".join(f"line {i}" for i in range(800))
        await _run_tool(pump, result_data={"result_content": big})
        blk = pump._turn_blocks[-1]
        assert blk["tool_result"].endswith("... (300 more lines)")
        assert "line 499" in blk["tool_result"]
        assert "line 500" not in blk["tool_result"]
        # The live frame ships the SAME truncated body (never the raw one).
        res = [f["event"] for f in _drain(q)
               if f["pump_type"] == "ws_event"
               and f["event"]["type"] == "tool_result"][0]
        assert res["result_content"] == blk["tool_result"]
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_bodyless_result_keeps_old_shape(temp_db):
    """A TOOL_RESULT without result_content (Claude CLI path, webSearch,
    bare mcp completions) behaves exactly as before — no tool_result frame,
    no body fields on the block."""
    temp_db.create_chat("tr4", "user-admin", "a1")
    pump = _mk_pump("tr4")
    try:
        q = pump.attach()
        await _run_tool(pump, result_data={})
        events = [f["event"]["type"] for f in _drain(q)
                  if f["pump_type"] == "ws_event"]
        assert "tool_result" not in events
        blk = pump._turn_blocks[-1]
        assert "tool_result" not in blk
        await pump._process_event(CommonEvent(DONE, {}))
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_an_mcp_results_pictures_go_to_the_result_files_service(temp_db, monkeypatch):
    """A Codex MCP result's `result_blocks` are handed to
    `services/mcp/result_files.keep` (which decides whether the tool is kept)
    and never ride a frame or the persisted block."""
    from services.mcp import result_files
    calls = []

    async def _keep(session_id, tool_name, blobs, text="", **kw):
        calls.append((session_id, tool_name, [b.kind for b in blobs], text,
                      kw.get("wait_for_push"), kw.get("is_error")))
        return None

    monkeypatch.setattr(result_files, "keep", _keep)
    temp_db.create_chat("tr-rf", "user-admin", "a1")
    pump = _mk_pump("tr-rf")
    try:
        q = pump.attach()
        image = {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
        await pump._process_event(CommonEvent(TOOL_USE, {
            "name": "mcp__maps__static_map", "tool_id": "m1"}))
        await pump._process_event(CommonEvent(TOOL_RESULT, {
            "name": "mcp__maps__static_map", "tool_id": "m1",
            "result_content": "Route", "result_blocks": [image]}))
        # A meeting participant's result names its own session.
        await pump._process_event(CommonEvent(TOOL_USE, {
            "name": "mcp__maps__static_map", "tool_id": "m2"}))
        await pump._process_event(CommonEvent(TOOL_RESULT, {
            "name": "mcp__maps__static_map", "tool_id": "m2", "is_error": True,
            "result_blocks": [image], "result_session_id": "participant-1"}))
        assert calls == [
            ("sess-tr-rf", "mcp__maps__static_map", ["image"], "Route", False, False),
            ("participant-1", "mcp__maps__static_map", ["image"], "", False, True),
        ]
        frames = json.dumps([f for f in _drain(q)])
        assert "iVBORw0KGgo=" not in frames
        assert "result_blocks" not in json.dumps(pump._turn_blocks)
        assert "result_session_id" not in frames
    finally:
        pump.producer.cancel()
