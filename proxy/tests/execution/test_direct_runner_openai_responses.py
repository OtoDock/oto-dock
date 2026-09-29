"""run_direct_stream on the REAL OpenAI adapter over the Responses API: one turn
with a tool loop (call → result → final text), the SDK client faked.

Pins the contract between the runner and the adapter's history translation:
the assistant message is stored in the chat shape (content + tool_calls +
the reasoning items), the tool result as a ``tool`` message, and the SECOND
request of the turn replays reasoning → function_call → function_call_output
without server ids, so OpenAI accepts a stateless (store: false) loop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.session.session_state import set_session_mode

AGENT = "pa"
_TOOL = {"name": "mcp__stub-server__ping", "description": "d",
         "input_schema": {"type": "object", "properties": {}}}


def _rs(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def _usage(input_tokens=100, output_tokens=10, cached=0):
    return SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=1),
    )


def _fc(arguments):
    return SimpleNamespace(type="function_call", id="fc_1", call_id="call_1",
                           name="mcp__stub-server__ping", arguments=arguments,
                           status="completed")


def _reasoning():
    return SimpleNamespace(type="reasoning", id="rs_1", encrypted_content="ENC",
                           summary=[SimpleNamespace(type="summary_text", text="plan")])


_CALL_TURN = [
    _rs("response.output_item.added", output_index=0, item=_reasoning()),
    _rs("response.reasoning_summary_text.delta", delta="plan", output_index=0),
    _rs("response.output_item.done", output_index=0, item=_reasoning()),
    _rs("response.output_item.added", output_index=1, item=_fc("")),
    _rs("response.function_call_arguments.delta", output_index=1, delta="{\"op\": \"list\"}"),
    _rs("response.output_item.done", output_index=1, item=_fc("{\"op\": \"list\"}")),
    _rs("response.completed", response=SimpleNamespace(status="completed", usage=_usage())),
]

_FINAL_TURN = [
    _rs("response.output_text.delta", delta="done", output_index=0),
    _rs("response.completed", response=SimpleNamespace(
        status="completed", usage=_usage(cached=80))),
]


class _FakeClient:
    scripts: list[list] = []
    calls: list[dict] = []

    def __init__(self, **kw):
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        type(self).calls.append(kwargs)
        script = type(self).scripts.pop(0)

        async def gen():
            for ev in script:
                yield ev
        return gen()

    async def close(self):
        pass


class _FakeMCP:
    def __init__(self):
        self.calls: list = []

    async def execute_tools(self, tool_calls):
        self.calls.extend(tool_calls)
        return [{"tool_use_id": tc["id"], "content": f"mcp:{tc['name']}"} for tc in tool_calls]


@pytest.fixture
def openai_session(monkeypatch):
    import openai as _openai
    from core.layers.direct.session import DirectSession
    from core.layers.providers import openai_adapter as oa

    monkeypatch.setattr(_openai, "AsyncOpenAI", _FakeClient)
    monkeypatch.setattr("core.layers.direct.session.config.get_agent_model", lambda agent: "")
    monkeypatch.setattr(oa.app_config, "model_supports_reasoning", lambda m: True)
    monkeypatch.setattr(oa.app_config, "DIRECT_LLM_OPENAI_API", "responses", raising=False)
    monkeypatch.setattr(oa.OpenAIAdapter, "_summary_unsupported", False)
    _FakeClient.scripts = []
    _FakeClient.calls = []

    session = DirectSession(
        session_id="openai-responses-runner", agent_name=AGENT,
        system_prompt="You are a test agent.", provider="openai",
    )
    session.model = "gpt-5.6-luna"
    session.api_key = "k"
    session.effort = "high"
    session.tools = [_TOOL]              # the builtins are not under test here
    session.mcp_manager = _FakeMCP()
    set_session_mode(session.session_id, "dontAsk")
    # MCP calls go through the one permission authority (HOOKS.md row 11),
    # which fails closed without the session's SecurityContext — a live
    # session always has one; give this fixture its own.
    from auth.path_policy import SecurityContext
    from core.session.session_state import cleanup_session_permission_state, set_session_security
    set_session_security(session.session_id, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False,
    ))
    yield session
    cleanup_session_permission_state(session.session_id)


async def _collect(session, prompt="hi"):
    from core.layers.direct.session import run_direct_stream
    out = []
    async for ev in run_direct_stream(session, prompt):
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_tool_loop_replays_reasoning_and_call_without_ids(openai_session):
    session = openai_session
    _FakeClient.scripts = [list(_CALL_TURN), list(_FINAL_TURN)]

    events = await _collect(session)
    types = [e["type"] for e in events]
    assert types[-2:] == ["metadata", "done"]
    assert "tool_start" in types and "tool_end" in types
    assert [e["data"]["phase"] for e in events if e["type"] == "thinking"][:1] == ["start"]
    assert "".join(e["data"]["content"] for e in events if e["type"] == "text") == "done"
    assert [tc["name"] for tc in session.mcp_manager.calls] == ["mcp__stub-server__ping"]

    # Two API calls, both on the Responses API with effort AND tools.
    assert len(_FakeClient.calls) == 2
    first, second = _FakeClient.calls
    for kw in (first, second):
        assert kw["reasoning"] == {"effort": "high", "summary": "auto"}
        assert kw["tools"][0]["name"] == "mcp__stub-server__ping"
        assert kw["store"] is False and kw["include"] == ["reasoning.encrypted_content"]
    assert first["input"][-1]["role"] == "user"

    # The second request replays the turn so far — reasoning (with its id),
    # then the call and its output WITHOUT server ids.
    tail = second["input"][-3:]
    assert tail[0] == {"type": "reasoning", "id": "rs_1",
                       "summary": [{"type": "summary_text", "text": "plan"}],
                       "encrypted_content": "ENC"}
    assert tail[1] == {"type": "function_call", "call_id": "call_1",
                       "name": "mcp__stub-server__ping", "arguments": "{\"op\": \"list\"}"}
    assert tail[2] == {"type": "function_call_output", "call_id": "call_1",
                       "output": "mcp:mcp__stub-server__ping"}

    # The history itself stays in the chat shape.
    roles = [m["role"] for m in session.messages]
    assert roles[-4:] == ["user", "assistant", "tool", "assistant"]
    assert session.messages[-3]["tool_calls"][0]["id"] == "call_1"
    assert session.messages[-3]["reasoning"][0]["encrypted_content"] == "ENC"
    assert session.messages[-2] == {"role": "tool", "tool_call_id": "call_1",
                                    "content": "mcp:mcp__stub-server__ping"}
    assert session.messages[-1] == {"role": "assistant", "content": "done"}

    # Cost + context from the decomposed usage (last call: 20 plain + 80 cached).
    meta = [e for e in events if e["type"] == "metadata"][0]["data"]
    assert meta["cache_read"] == 80 and meta["context_used"] == 100 + 10


@pytest.mark.asyncio
async def test_next_turn_does_not_replay_the_previous_turn_reasoning(openai_session):
    session = openai_session
    _FakeClient.scripts = [list(_CALL_TURN), list(_FINAL_TURN), list(_FINAL_TURN)]
    await _collect(session, "first")
    await _collect(session, "second")
    third = _FakeClient.calls[2]["input"]
    # Earlier turns: the call and its output stay (history), the reasoning is gone.
    assert not any(item.get("type") == "reasoning" for item in third)
    assert [i.get("type") for i in third if i.get("type")] == [
        "function_call", "function_call_output",
    ]
    assert third[-1] == {"role": "user", "content": "second"}


def _ws(id_="ws_1", status="completed"):
    return SimpleNamespace(
        type="web_search_call", id=id_, status=status,
        action=SimpleNamespace(type="search", query="weather athens", queries=None, sources=None),
    )


# A response that searches the web (server-side), then calls a client tool.
_SEARCH_THEN_CALL_TURN = [
    _rs("response.output_item.added", output_index=0, item=_reasoning()),
    _rs("response.output_item.done", output_index=0, item=_reasoning()),
    _rs("response.output_item.added", output_index=1, item=_ws(status="in_progress")),
    _rs("response.output_item.done", output_index=1, item=_ws()),
    _rs("response.output_item.added", output_index=2, item=_fc("")),
    _rs("response.function_call_arguments.delta", output_index=2, delta="{\"op\": \"list\"}"),
    _rs("response.output_item.done", output_index=2, item=_fc("{\"op\": \"list\"}")),
    _rs("response.completed", response=SimpleNamespace(status="completed", usage=_usage())),
]


@pytest.mark.asyncio
async def test_web_search_row_ends_without_execution_and_is_replayed_in_the_tool_loop(
    openai_session, monkeypatch,
):
    from core.layers.providers import openai_adapter as oa
    monkeypatch.setattr(oa.app_config, "model_supports_server_tools", lambda m: True)
    session = openai_session
    _FakeClient.scripts = [list(_SEARCH_THEN_CALL_TURN), list(_FINAL_TURN)]

    events = await _collect(session)
    # The search is a server tool row: started, ended with its query, never executed.
    starts = [e["data"] for e in events if e["type"] == "tool_start"]
    ends = [e["data"] for e in events if e["type"] == "tool_end"]
    assert [s["name"] for s in starts] == ["web_search", "mcp__stub-server__ping"]
    assert ends[0] == {"tool_use_id": "ws_1", "result_preview": "weather athens"}
    assert [tc["name"] for tc in session.mcp_manager.calls] == ["mcp__stub-server__ping"]
    assert "".join(e["data"]["content"] for e in events if e["type"] == "text") == "done"

    # Both requests carried the built-in tool next to the client tool.
    first, second = _FakeClient.calls
    for kw in (first, second):
        assert [t["type"] for t in kw["tools"]] == ["function", "web_search"]
    # The second request replays reasoning → the search → the call → its output.
    tail = second["input"][-4:]
    assert tail[0]["type"] == "reasoning" and tail[0]["id"] == "rs_1"
    assert tail[1] == {"type": "web_search_call", "id": "ws_1", "status": "completed",
                       "action": {"type": "search", "query": "weather athens"}}
    assert tail[2]["type"] == "function_call" and tail[3]["type"] == "function_call_output"
    # The history keeps the search in the assistant message's replay items.
    assert [i["type"] for i in session.messages[-3]["reasoning"]] == ["reasoning", "web_search_call"]
    # The per-call fee is in the turn's cost.
    meta = [e for e in events if e["type"] == "metadata"][0]["data"]
    assert meta["cost_usd"] >= 0.01
