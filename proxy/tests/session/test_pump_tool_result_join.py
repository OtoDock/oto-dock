"""The hook's tool result joins its own tool block (headless Claude): the
forwarder's result and the CLI's own tool_result frame reach the pump on two
channels, so the block may already be closed when the result arrives, and a
parallel call of the same tool may still be running."""

import asyncio

import pytest

from core.events.stream_pump import ChatStreamPump, _active_pumps
from ws import wire_events as wire


def _pump() -> ChatStreamPump:
    producer = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(chat_id="chat-join", session_id="sid-join", producer=producer,
                          event_queue=asyncio.Queue(), perm_queue=asyncio.Queue(),
                          source_type="chat")


def _tool(tool_id: str, name: str = "Bash", *, active: bool) -> dict:
    return {"type": wire.PERSISTED_TOOL, "name": name, "tool_id": tool_id, "summary": "",
            "active": active, "tool_input": None}


def _result(tool_use_id: str, content: str, name: str = "Bash") -> dict:
    return {"event_type": wire.ITEM_TOOL_RESULT, "tool_name": name, "tool_use_id": tool_use_id,
            "summary": content[:20], "result_content": content, "is_error": False}


@pytest.mark.asyncio
async def test_a_result_for_a_closed_call_reaches_its_block_not_a_running_one():
    """Two parallel Bash calls: the first closed by the stream before its
    hook result arrives, the second still running. The first's output lands
    on the first block; the second keeps nothing until its own result."""
    pump = _pump()
    try:
        first, second = _tool("toolu_ls", active=False), _tool("toolu_echo", active=True)
        pump._turn_blocks.append(first)
        pump._active_tools["toolu_echo"] = second
        await pump._handle_perm_event(_result("toolu_ls", "total 8"))
        assert first["tool_result"] == "total 8"
        assert "tool_result" not in second
        await pump._handle_perm_event(_result("toolu_echo", "smoke-1"))
        assert second["tool_result"] == "smoke-1"
    finally:
        pump.producer.cancel()
        _active_pumps.pop(pump.chat_id, None)


@pytest.mark.asyncio
async def test_a_result_with_an_unknown_id_attaches_nowhere():
    pump = _pump()
    try:
        running = _tool("toolu_a", active=True)
        pump._active_tools["toolu_a"] = running
        await pump._handle_perm_event(_result("toolu_gone", "out"))
        assert "tool_result" not in running
    finally:
        pump.producer.cancel()
        _active_pumps.pop(pump.chat_id, None)


@pytest.mark.asyncio
async def test_an_older_cli_without_an_id_still_matches_by_name():
    pump = _pump()
    try:
        running = _tool("Bash", active=True)
        pump._active_tools["Bash"] = running
        await pump._handle_perm_event(_result("", "out"))
        assert running["tool_result"] == "out"
    finally:
        pump.producer.cancel()
        _active_pumps.pop(pump.chat_id, None)
