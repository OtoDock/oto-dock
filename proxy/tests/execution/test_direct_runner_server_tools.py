"""run_direct_stream and SERVER-side tools (Anthropic web_search / web_fetch /
code_execution): the adapter's ``tool_result`` event becomes the dashboard's
``tool_end`` so the tool row stops spinning (live-hit 2026-09-07 — every
server tool row spun until the page reloaded); the runner never executes
such a tool and never stores a tool message for it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from core.layers.providers.base import (
    ProviderAdapter,
    ProviderStreamEvent,
    ProviderUsage,
)
from core.layers.providers.registry import register_adapter

_STUB_PROVIDER = "stub-server-tools-test"


class _StubAdapter(ProviderAdapter):
    script: list = []

    @property
    def provider_name(self) -> str:
        return _STUB_PROVIDER

    async def stream_response(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
        for ev in self.script:
            yield ev

    def format_tool_results(self, results):
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["tool_use_id"], "content": r["content"]}
            for r in results
        ]}]

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
        session_id="server-tools-session", agent_name="test-agent",
        system_prompt="You are a test agent.", provider=_STUB_PROVIDER,
    )
    session.model = "test-model"
    session.api_key = "stub-test-key"
    session.mcp_manager = None
    return session


@pytest.mark.asyncio
async def test_server_tool_result_ends_the_tool_row(direct_session):
    _ADAPTER.script = [
        ProviderStreamEvent(type="tool_start", tool_name="web_search", tool_id="srvtoolu_1"),
        ProviderStreamEvent(type="tool_result", tool_name="web_search", tool_id="srvtoolu_1",
                            text="3 result(s)"),
        ProviderStreamEvent(type="text_delta", text="Sunny."),
        ProviderStreamEvent(type="usage", usage=ProviderUsage(input_tokens=5, output_tokens=2)),
        ProviderStreamEvent(type="content", raw_content="Sunny."),
        ProviderStreamEvent(type="stop", stop_reason="end_turn"),
    ]
    from core.layers.direct.session import run_direct_stream
    events = []
    async for ev in run_direct_stream(direct_session, "weather?"):
        events.append(ev)
    types = [e["type"] for e in events]
    assert types.index("tool_start") < types.index("tool_end") < types.index("text")
    end = [e for e in events if e["type"] == "tool_end"][0]["data"]
    assert end == {"tool_use_id": "srvtoolu_1", "result_preview": "3 result(s)"}
    assert types[-2:] == ["metadata", "done"]
    # Nothing was executed locally and no tool message entered the history.
    assert [m["role"] for m in direct_session.messages] == ["user", "assistant"]
    assert direct_session.messages[-1]["content"] == "Sunny."
