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


def _forwarded(monkeypatch, hook_input: dict, session_id: str) -> dict:
    """The body the real forwarder posts for one hook input."""
    import importlib.util
    import io
    import json

    from tests._paths import PROXY_DIR
    spec = importlib.util.spec_from_file_location("tool_result_forwarder_join",
                                                  PROXY_DIR / "hooks" / "tool_result_forwarder.py")
    forwarder = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(forwarder)
    posted: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        posted.append(json.loads(req.data.decode()))
        return _Resp()

    monkeypatch.setattr(forwarder.sys, "stdin", io.StringIO(json.dumps(hook_input)))
    monkeypatch.setattr(forwarder.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
    monkeypatch.setenv("OTO_SESSION_ID", session_id)
    monkeypatch.setenv("PROXY_URL", "http://proxy.test")
    monkeypatch.setenv("PROXY_API_KEY", "k")
    forwarder.main()
    assert len(posted) == 1
    return posted[0]


async def _through_the_route(monkeypatch, body: dict) -> dict:
    """The receiver's queue item for a forwarded body."""
    from unittest.mock import AsyncMock

    from api.hooks import lifecycle
    from core.session import session_state
    monkeypatch.setattr(lifecycle, "verify_session_match_async", AsyncMock())
    await lifecycle.hook_tool_result(lifecycle.HookToolResultRequest(**body), authorization="Bearer x")
    return session_state.get_permission_queue(body["session_id"]).get_nowait()


@pytest.mark.asyncio
async def test_a_stopped_commands_sentence_reaches_its_card(monkeypatch):
    """Claude Code reports a failed call through PostToolUseFailure (the
    memory guard's stop is one): the forwarder posts the text the model read,
    the route queues it and the card shows it, flagged."""
    pump = _pump()
    try:
        block = _tool("toolu_stop", active=True)
        pump._active_tools["toolu_stop"] = block
        body = _forwarded(monkeypatch, {
            "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "python3 grow.py"}, "tool_use_id": "toolu_stop",
            "error": "Exit code 1\n\nThat command used too much memory and was stopped.",
            "is_interrupt": False,
        }, pump.session_id)
        await pump._handle_perm_event(await _through_the_route(monkeypatch, body))
        assert "That command used too much memory and was stopped." in block["tool_result"]
        assert block["is_error"] is True
        assert block["result_summary"] == "error: Exit code 1"
    finally:
        pump.producer.cancel()
        _active_pumps.pop(pump.chat_id, None)


@pytest.mark.asyncio
async def test_a_failed_mcp_call_is_not_charged(monkeypatch):
    """An MCP call that failed (the error flag, a JSON-RPC error) reaches the
    card flagged before the stream closes its block, and the fee is skipped;
    the same call that succeeded is charged."""
    from core.events.common_events import TOOL_RESULT, CommonEvent
    from services.mcp import mcp_cost_engine
    monkeypatch.setattr(mcp_cost_engine, "find_costs_block_for_tool",
                        lambda name: ("image-gen-mcp", "generate_image", object()))
    monkeypatch.setattr(mcp_cost_engine, "evaluate", lambda *a, **k: mcp_cost_engine.CostHit(
        amount=0.04, provider="openai", model="gpt-image-1", currency="USD"))
    name = "mcp__image-gen-mcp__generate_image"
    pump = _pump()
    try:
        for tool_id, hook_input in (
            ("toolu_bad", {"hook_event_name": "PostToolUseFailure", "error": "rpc failure on purpose"}),
            ("toolu_ok", {"hook_event_name": "PostToolUse", "tool_response": [{"type": "text", "text": "Saved."}]}),
        ):
            block = _tool(tool_id, name, active=True)
            pump._active_tools[tool_id] = block
            body = _forwarded(monkeypatch, {"tool_name": name, "tool_input": {"model": "gpt-image"},
                                            "tool_use_id": tool_id, **hook_input}, pump.session_id)
            await pump._handle_perm_event(await _through_the_route(monkeypatch, body))
            await pump._process_event(CommonEvent(type=TOOL_RESULT, data={"name": name, "tool_id": tool_id}))
            if tool_id == "toolu_bad":
                assert block["is_error"] is True and block["tool_result"] == "rpc failure on purpose"
                assert pump._total_cost_delta == 0
        assert pump._total_cost_delta == pytest.approx(0.04)
    finally:
        pump.producer.cancel()
        _active_pumps.pop(pump.chat_id, None)
