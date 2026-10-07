"""``change_model`` on the Direct LLM layer (2026-09-07).

A CROSS-provider switch used to keep ``session.messages`` in the old provider's
shape — OpenAI ``tool_calls`` / ``role: tool`` / the Responses ``reasoning``
items, Anthropic content blocks / ``tool_result`` user messages, provider image
blocks — and the next request 400'd (``messages.2.reasoning: Extra inputs are
not permitted``) on every later turn. The switch now keeps the conversation as
plain text turns (the shape the DB resume builds) and swaps the provider's own
server tools; a same-provider switch touches nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.layers.direct.layer import _text_only_history
from concurrent.futures import Future


def test_text_only_history_keeps_the_visible_text_of_every_stored_shape():
    rs = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "E"}
    messages = [
        {"role": "user", "content": "q1"},
        # OpenAI: text + call (with a reasoning item), the tool message, the final text
        {"role": "assistant", "content": "let me check", "reasoning": [rs], "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "42"},
        {"role": "assistant", "content": "it is 42"},
        # a user turn with a chat-shape image
        {"role": "user", "content": [
            {"type": "text", "text": "q2"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]},
        # Anthropic: thinking / text / tool_use blocks, the tool_result user message,
        # then server-tool blocks around the final text
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "s"},
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "tu1", "name": "t", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"},
        ]},
        {"role": "assistant", "content": [
            {"type": "server_tool_use", "id": "srv", "name": "web_search", "input": {"query": "x"}},
            {"type": "web_search_tool_result", "tool_use_id": "srv", "content": []},
            {"type": "text", "text": "found it"},
        ]},
        # an Anthropic image user turn
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
            {"type": "text", "text": "q3"},
        ]},
        # an aborted tool loop: nothing visible
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]},
    ]
    assert _text_only_history(messages) == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "let me check\n\nit is 42"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "looking\n\nfound it"},
        {"role": "user", "content": "q3"},
    ]
    # The input is left alone.
    assert messages[1]["tool_calls"] and messages[6]["content"][0]["type"] == "tool_result"


def test_text_only_history_drops_leading_assistant_turns_and_empties():
    assert _text_only_history([]) == []
    assert _text_only_history([
        {"role": "assistant", "content": "orphan"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": []},
    ]) == [{"role": "user", "content": "hi"}]
    assert _text_only_history([{"role": "tool", "tool_call_id": "c", "content": "x"}]) == []


_CLIENT_TOOL = {"name": "mcp__s__t", "description": "d", "input_schema": {"type": "object"}}
_OPENAI_HISTORY = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "content": None, "reasoning": [
        {"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": "E"}],
     "tool_calls": [{"id": "c1", "type": "function",
                     "function": {"name": "mcp__s__t", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "assistant", "content": "a"},
]


@pytest.fixture
def switch_env(monkeypatch):
    """A registered Direct session on the openai provider plus a subscription
    pool that hands out a handle for whatever provider is asked."""
    from core.layers.direct import layer as layer_mod
    from core.layers.direct import session as session_mod
    from services.engines import subscription_pool

    monkeypatch.setattr("core.layers.direct.session.config.get_agent_model", lambda agent: "")
    session = session_mod.DirectSession(
        session_id="switch-test", agent_name="pa", system_prompt="s", provider="openai",
    )
    session.model = "gpt-5.6-luna"
    session.api_key = "old-key"
    session.tools = [dict(_CLIENT_TOOL)]
    session.messages = [dict(m) for m in _OPENAI_HISTORY]
    monkeypatch.setitem(session_mod._direct_sessions, session.session_id, session)

    calls: list = []
    handles = {
        "anthropic": SimpleNamespace(provider="anthropic", auth_type="api_key", api_key="AK",
                                     endpoint_url=None, subscription_id="sub-a"),
        "openai": SimpleNamespace(provider="openai", auth_type="api_key", api_key="OK",
                                  endpoint_url=None, subscription_id="sub-o"),
    }
    monkeypatch.setattr(subscription_pool, "release_subscription",
                        lambda sid: calls.append(("release", sid)))
    monkeypatch.setattr(subscription_pool, "acquire_subscription",
                        lambda layer, user_sub, provider=None: handles.get(provider))
    def _bind(*a, **kw):
        calls.append(("bind", a[1]))
        done = Future()        # the writer's future the spawn flow awaits
        done.set_result(None)
        return done

    monkeypatch.setattr(subscription_pool, "bind_session", _bind)
    monkeypatch.setattr(layer_mod.app_config, "get_model_provider",
                        lambda m: "anthropic" if m.startswith("claude") else "openai")
    return layer_mod.DirectLLMExecutionLayer(), session, calls


@pytest.mark.asyncio
async def test_cross_provider_switch_keeps_the_history_as_text_and_swaps_the_server_tools(
    switch_env,
):
    layer, session, calls = switch_env
    await layer.change_model(session.session_id, "claude-sonnet-5-5")

    assert (session.provider, session.model, session.api_key) == ("anthropic", "claude-sonnet-5-5", "AK")
    assert calls == [("release", "switch-test"), ("bind", "sub-a")]
    # Plain text turns only — nothing the Anthropic API would reject.
    assert session.messages == [{"role": "user", "content": "q"},
                                {"role": "assistant", "content": "a"}]
    # The client tool stays; Anthropic's own server tools are added.
    names = [t.get("name") for t in session.tools]
    assert names[0] == "mcp__s__t" and "web_search" in names and "web_fetch" in names
    assert all("input_schema" in t or "type" in t for t in session.tools)

    # Back to OpenAI: the Anthropic server entries go, the client tool stays.
    await layer.change_model(session.session_id, "gpt-5.6-luna")
    assert (session.provider, session.api_key) == ("openai", "OK")
    assert [t.get("name") for t in session.tools] == ["mcp__s__t"]
    assert session.messages == [{"role": "user", "content": "q"},
                                {"role": "assistant", "content": "a"}]


@pytest.mark.asyncio
async def test_same_provider_switch_touches_nothing(switch_env):
    layer, session, calls = switch_env
    before_messages = [dict(m) for m in session.messages]
    before_tools = list(session.tools)
    await layer.change_model(session.session_id, "gpt-5.6-terra")
    assert (session.provider, session.model, session.api_key) == ("openai", "gpt-5.6-terra", "old-key")
    assert session.messages == before_messages and session.tools == before_tools
    assert calls == []


@pytest.mark.asyncio
async def test_switch_without_a_subscription_keeps_the_history(switch_env, monkeypatch):
    from services.engines import subscription_pool
    layer, session, calls = switch_env
    monkeypatch.setattr(subscription_pool, "acquire_subscription",
                        lambda layer, user_sub, provider=None: None)
    before = [dict(m) for m in session.messages]
    await layer.change_model(session.session_id, "claude-sonnet-5")
    # Today's behaviour: the model is set, the provider and the history are not
    # touched (the next turn reports the missing subscription).
    assert (session.provider, session.model) == ("openai", "claude-sonnet-5")
    assert session.messages == before
