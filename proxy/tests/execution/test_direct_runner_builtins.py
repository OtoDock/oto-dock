"""run_direct_stream dispatch of the client-side builtins (Read / Write / …):

- a builtin tool call executes IN-PROCESS and its result lands in the
  provider-formatted tool-result message, while an MCP tool call in the same
  turn still goes to the MCP manager;
- the edit tier prompts in ``default`` mode through the session's permission
  queue (the dashboard's block-and-wait) and runs after approval;
- a denial (path policy) becomes the tool result text, never an exception.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from auth.path_policy import SecurityContext
from core.layers.providers.base import (
    ProviderAdapter, ProviderStreamEvent, ProviderUsage,
)
from core.layers.providers.registry import register_adapter
from core.sandbox.sandbox import SandboxConfig
from core.session.session_state import (
    get_permission_queue, resolve_permission, set_session_mode, set_session_security,
)

_STUB_PROVIDER = "stub-builtins-test"
AGENT = "pa"


class _StubAdapter(ProviderAdapter):
    """Replays one scripted event list PER API CALL (a tool loop makes several)."""

    scripts: list[list] = []
    seen_messages: list = []

    @property
    def provider_name(self) -> str:
        return _STUB_PROVIDER

    async def stream_response(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
        self.seen_messages.append([dict(m) for m in kwargs["messages"]])
        script = self.scripts.pop(0) if self.scripts else [
            ProviderStreamEvent(type="content", raw_content=""),
            ProviderStreamEvent(type="stop", stop_reason="end_turn"),
        ]
        for ev in script:
            yield ev

    def format_tool_results(self, results):
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["tool_use_id"], "content": r["content"]}
            for r in results
        ]}]

    def serialize_assistant_content(self, raw_content):
        return raw_content


_ADAPTER = _StubAdapter()
register_adapter(_ADAPTER)


def _tool_turn(calls: list[tuple[str, str, dict]]) -> list[ProviderStreamEvent]:
    import json
    evs = []
    for tool_id, name, args in calls:
        evs.append(ProviderStreamEvent(type="tool_start", tool_name=name, tool_id=tool_id))
        evs.append(ProviderStreamEvent(
            type="tool_stop", tool_name=name, tool_id=tool_id, tool_input_json=json.dumps(args),
        ))
    evs.append(ProviderStreamEvent(type="usage", usage=ProviderUsage(input_tokens=1, output_tokens=1)))
    evs.append(ProviderStreamEvent(type="content", raw_content="(calling tools)"))
    evs.append(ProviderStreamEvent(type="stop", stop_reason="tool_use"))
    return evs


def _final_text(text: str) -> list[ProviderStreamEvent]:
    return [
        ProviderStreamEvent(type="text_delta", text=text),
        ProviderStreamEvent(type="usage", usage=ProviderUsage(input_tokens=1, output_tokens=1)),
        ProviderStreamEvent(type="content", raw_content=text),
        ProviderStreamEvent(type="stop", stop_reason="end_turn"),
    ]


class _FakeMCP:
    def __init__(self):
        self.calls: list = []

    async def execute_tools(self, tool_calls):
        self.calls.extend(tool_calls)
        return [{"tool_use_id": tc["id"], "content": f"mcp:{tc['name']}"} for tc in tool_calls]


@pytest.fixture
def direct_session(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    for d in ("workspace", "knowledge/memory", "users/alice/workspace", "users/alice/.claude"):
        (agents / AGENT / d).mkdir(parents=True)
    (agents / AGENT / "workspace" / "notes.md").write_text("alpha\nbeta\n")
    (tmp_path / "mcps").mkdir()
    cfg = SandboxConfig(
        role="manager", username="alice", agent_name=AGENT, is_admin_agent=False,
        host_agents_dir=agents.resolve(), host_mcps_dir=(tmp_path / "mcps").resolve(),
        host_claude_dir=(agents / AGENT / "users/alice/.claude").resolve(),
        net_forwards=["8400"],
    )
    from core.layers.direct.session import DirectSession
    monkeypatch.setattr("core.layers.direct.session.config.get_agent_model", lambda agent: "")
    session = DirectSession(
        session_id="builtins-runner", agent_name=AGENT,
        system_prompt="You are a test agent.", provider=_STUB_PROVIDER,
    )
    session.model = "test-model"
    session.api_key = "stub-test-key"
    session.sandbox_cfg = cfg
    session.mcp_manager = _FakeMCP()
    set_session_security(session.session_id, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False,
    ))
    _ADAPTER.scripts = []
    _ADAPTER.seen_messages = []
    return session, agents


async def _collect(session, prompt="hi"):
    from core.layers.direct.session import run_direct_stream
    out = []
    async for ev in run_direct_stream(session, prompt):
        out.append(ev)
    return out


def _tool_results(session) -> dict[str, str]:
    """tool_use_id → result content from the provider-formatted history."""
    found: dict[str, str] = {}
    for m in session.messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for block in m["content"]:
                if block.get("type") == "tool_result":
                    found[block["tool_use_id"]] = block["content"]
    return found


@pytest.mark.asyncio
async def test_builtin_runs_in_process_and_mcp_tools_still_go_to_the_manager(direct_session):
    session, _ = direct_session
    # dontAsk + an unknown server: the MCP call is allowed without a prompt
    # whatever tier the real registry would assign (the MCP gate is not
    # under test here).
    set_session_mode(session.session_id, "dontAsk")
    _ADAPTER.scripts = [
        _tool_turn([
            ("t1", "Read", {"file_path": "/workspace/notes.md"}),
            ("t2", "mcp__stub-server__ping", {"op": "list"}),
        ]),
        _final_text("done"),
    ]
    events = await _collect(session)
    assert [e["type"] for e in events][-2:] == ["metadata", "done"]
    results = _tool_results(session)
    assert results["t1"] == "     1\talpha\n     2\tbeta"
    assert results["t2"] == "mcp:mcp__stub-server__ping"
    # The builtin never reached the MCP manager.
    assert [tc["name"] for tc in session.mcp_manager.calls] == ["mcp__stub-server__ping"]
    ends = [e for e in events if e["type"] == "tool_end"]
    assert {e["data"]["tool_use_id"] for e in ends} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_write_prompts_in_default_mode_and_runs_after_approval(direct_session):
    session, agents = direct_session
    set_session_mode(session.session_id, "default")
    _ADAPTER.scripts = [
        _tool_turn([("w1", "Write", {"file_path": "/workspace/out.md", "content": "hi\n"})]),
        _final_text("written"),
    ]

    async def _approve():
        queue = get_permission_queue(session.session_id)
        req = await asyncio.wait_for(queue.get(), timeout=5)
        assert req["event_type"] == "permission_prompt"
        assert req["tool_name"] == "Write"
        assert req["tool_input"]["file_path"] == "/workspace/out.md"
        resolve_permission(req["request_id"], True)

    approver = asyncio.create_task(_approve())
    await _collect(session)
    await approver
    assert (agents / AGENT / "workspace" / "out.md").read_text() == "hi\n"
    assert _tool_results(session)["w1"].startswith("Created /workspace/out.md")


@pytest.mark.asyncio
async def test_write_denied_by_the_user_becomes_result_text(direct_session):
    session, agents = direct_session
    set_session_mode(session.session_id, "default")
    _ADAPTER.scripts = [
        _tool_turn([("w2", "Write", {"file_path": "/workspace/no.md", "content": "x"})]),
        _final_text("ok"),
    ]

    async def _deny():
        queue = get_permission_queue(session.session_id)
        req = await asyncio.wait_for(queue.get(), timeout=5)
        resolve_permission(req["request_id"], False)

    denier = asyncio.create_task(_deny())
    await _collect(session)
    await denier
    assert not (agents / AGENT / "workspace" / "no.md").exists()
    assert _tool_results(session)["w2"] == "Tool use denied by user."


@pytest.mark.asyncio
async def test_mcp_tools_go_through_the_one_permission_authority(direct_session):
    """The direct loop's MCP branch is ``session_events.pre_tool`` →
    ``decide_tool_permission`` (HOOKS.md row 11): a dashboard chat in default
    mode prompts on the session's queue for a standard-tier tool, the
    session allow-memory covers the second call, and the record of the turn
    lists both calls."""
    from core.session import session_events
    from core.session.session_state import _record_session_use
    session, _ = direct_session
    # The fixture reuses one session id across tests; run_direct_stream is
    # one turn and the layer's send_message is what ends it — start clean.
    session_events.cleanup_session(session.session_id)
    _record_session_use(session.session_id, client_type="dashboard", agent=AGENT)
    set_session_mode(session.session_id, "default")
    _ADAPTER.scripts = [
        _tool_turn([("m1", "mcp__stub-server__ping", {"op": "list"})]),
        _tool_turn([("m2", "mcp__stub-server__ping", {"op": "list"})]),
        _final_text("done"),
    ]
    prompts = []

    async def _approve_once():
        queue = get_permission_queue(session.session_id)
        req = await asyncio.wait_for(queue.get(), timeout=5)
        prompts.append(req)
        assert req["event_type"] == "permission_prompt"
        assert req["tool_name"] == "mcp__stub-server__ping"
        resolve_permission(req["request_id"], True)

    approver = asyncio.create_task(_approve_once())
    await _collect(session)
    await approver
    # One prompt for two calls: the allow-memory remembered the first Allow.
    assert len(prompts) == 1
    assert [tc["id"] for tc in session.mcp_manager.calls] == ["m1", "m2"]
    # The turn's record: both calls, from the direct loop, neither an error.
    recs = session_events.tool_records(session.session_id)
    assert [(r.tool_name, r.source, r.is_error) for r in recs] == [
        ("mcp__stub-server__ping", "direct", False)] * 2
    session_events.cleanup_session(session.session_id)


@pytest.mark.asyncio
async def test_a_denied_mcp_call_carries_the_authority_reason(direct_session, monkeypatch):
    """Unattended (client_type task) + a critical-tier tool: the authority
    denies and informs; the reason is the tool result text."""
    from core.session.session_state import _record_session_use
    from services.mcp import mcp_permissions
    session, _ = direct_session
    _record_session_use(session.session_id, client_type="task", agent=AGENT)
    set_session_mode(session.session_id, "auto")
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda server, tool: "critical")
    _ADAPTER.scripts = [
        _tool_turn([("c1", "mcp__stub-server__wipe", {})]),
        _final_text("ok"),
    ]
    await _collect(session)
    assert session.mcp_manager.calls == []
    assert "unattended" in _tool_results(session)["c1"]


@pytest.mark.asyncio
async def test_deferred_tool_called_by_name_is_loaded_on_first_use(direct_session, monkeypatch):
    """A weaker model skips tool_search and calls a catalog entry outright:
    the runner loads the definition and executes the call."""
    session, _ = direct_session
    set_session_mode(session.session_id, "dontAsk")
    from core.layers.direct import session as session_mod
    ping = {"name": "mcp__stub-server__ping", "description": "Ping the stub",
            "input_schema": {"type": "object", "properties": {}}}
    monkeypatch.setattr(session_mod.config, "DIRECT_LLM_TOOL_SEARCH", "on")
    monkeypatch.setattr(session_mod, "_registry_facts", lambda: (set(), {}))
    session.tools = [ping] + session.tools
    session_mod._apply_deferred_tools(session)
    assert session.catalog is not None and "mcp__stub-server__ping" in session.catalog.deferred
    assert "mcp__stub-server__ping" not in [t["name"] for t in session.tools]

    _ADAPTER.scripts = [
        _tool_turn([("d1", "mcp__stub-server__ping", {"op": "x"})]),
        _final_text("ok"),
    ]
    await _collect(session)
    assert _tool_results(session)["d1"] == "mcp:mcp__stub-server__ping"
    assert session.catalog.is_loaded("mcp__stub-server__ping")
    assert "mcp__stub-server__ping" in [t["name"] for t in session.tools]


@pytest.mark.asyncio
async def test_identical_calls_are_never_refused(direct_session):
    """No loop breaker on identical calls: a browser snapshot after each
    click or polling a task result repeats the same input legitimately.
    MAX_TOOL_LOOPS is the only bound."""
    session, _ = direct_session
    set_session_mode(session.session_id, "dontAsk")
    same = {"op": "list"}
    _ADAPTER.scripts = [
        _tool_turn([("r1", "mcp__stub-server__ping", same)]),
        _tool_turn([("r2", "mcp__stub-server__ping", same)]),
        _tool_turn([("r3", "mcp__stub-server__ping", same)]),
        _final_text("done"),
    ]
    await _collect(session)
    results = _tool_results(session)
    assert all(results[k] == "mcp:mcp__stub-server__ping" for k in ("r1", "r2", "r3"))


@pytest.mark.asyncio
async def test_path_policy_denial_never_prompts(direct_session):
    session, _ = direct_session
    set_session_mode(session.session_id, "acceptEdits")
    _ADAPTER.scripts = [
        _tool_turn([("m1", "Write", {"file_path": "/knowledge/memory/x.md", "content": "x"})]),
        _final_text("ok"),
    ]
    await _collect(session)
    assert "memory" in _tool_results(session)["m1"]
    assert get_permission_queue(session.session_id).empty()
