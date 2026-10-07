"""Tests for provider adapter helpers — focused on vision content blocks.

The Direct LLM path attaches chat-uploaded photos directly as content blocks
on the user message (no built-in Read tool, unlike Claude Code CLI / Codex).
Each provider adapter formats the block in its native shape:

- Anthropic: ``{"type": "image", "source": {"type": "base64", ...}}``
- OpenAI-compat (OpenAI / Groq / Ollama / LiteLLM): ``{"type": "image_url",
  "image_url": {"url": "data:..."}}``

The base ``ProviderAdapter.format_image_content_block`` returns the OpenAI
shape; ``AnthropicAdapter`` overrides. Subclasses that don't override (Groq,
Ollama, LiteLLM via ``OpenAIAdapter`` inheritance) get the default for free.
"""

import asyncio


_SAMPLE_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/wcAAwAB/epv2AIAAAAASUVORK5CYII="


def test_anthropic_format_image_content_block():
    """Anthropic adapter returns the ``image`` / ``source.base64`` shape."""
    from core.layers.providers.anthropic_adapter import AnthropicAdapter

    block = AnthropicAdapter().format_image_content_block(
        media_type="image/jpeg",
        base64_data=_SAMPLE_B64,
    )
    assert block == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": _SAMPLE_B64,
        },
    }


def test_openai_format_image_content_block():
    """OpenAI adapter returns the ``image_url`` shape with a data URL."""
    from core.layers.providers.openai_adapter import OpenAIAdapter

    block = OpenAIAdapter().format_image_content_block(
        media_type="image/png",
        base64_data=_SAMPLE_B64,
    )
    assert block == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{_SAMPLE_B64}"},
    }


def test_groq_inherits_openai_format():
    """Groq subclasses ``OpenAIAdapter`` — gets the default ``image_url`` shape
    for free since it doesn't override ``format_image_content_block``."""
    from core.layers.providers.openai_compat_adapter import GroqAdapter

    block = GroqAdapter().format_image_content_block(
        media_type="image/jpeg",
        base64_data=_SAMPLE_B64,
    )
    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == f"data:image/jpeg;base64,{_SAMPLE_B64}"


def test_ollama_inherits_openai_format():
    """Ollama subclasses ``OpenAIAdapter`` (the OpenAI-compatible API surface
    most local backends like llava / llama-3.2-vision expose)."""
    from core.layers.providers.openai_compat_adapter import OllamaAdapter

    block = OllamaAdapter().format_image_content_block(
        media_type="image/png",
        base64_data=_SAMPLE_B64,
    )
    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == f"data:image/png;base64,{_SAMPLE_B64}"


# --- reasoning-effort gating (adaptive thinking only on reasoning models) -----
# Regression: the Anthropic adapter must NOT send `thinking`/`output_config` for a
# non-reasoning model (e.g. Haiku 4.5), or the API 400s with "adaptive thinking is
# not supported on this model". Mirrors the OpenAI adapter's supports_reasoning gate.

from core.layers.providers import anthropic_adapter
from core.layers.providers.anthropic_adapter import AnthropicAdapter, _with_history_breakpoint


class _FakeMsg:
    usage = None
    content = None


class _FakeStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return _FakeMsg()


class _FakeMessages:
    def __init__(self, captured):
        self._c = captured

    def stream(self, **kwargs):
        self._c.update(kwargs)
        return _FakeStream()


class _FakeClient:
    def __init__(self, captured):
        self.messages = _FakeMessages(captured)

    async def close(self):
        pass


def _capture_stream_kwargs(monkeypatch, model, effort):
    captured: dict = {}
    monkeypatch.setattr(
        anthropic_adapter.anthropic, "AsyncAnthropic",
        lambda **kw: _FakeClient(captured),
    )

    async def go():
        async for _ in AnthropicAdapter().stream_response(
            api_key="k", model=model, system_prompt="s",
            messages=[{"role": "user", "content": "hi"}], tools=[],
            max_tokens=64, effort=effort,
        ):
            pass

    asyncio.run(go())
    return captured


def test_anthropic_non_reasoning_model_drops_thinking(monkeypatch):
    kw = _capture_stream_kwargs(monkeypatch, "claude-haiku-4-5", "high")
    assert "thinking" not in kw
    assert "output_config" not in kw


def test_anthropic_reasoning_model_keeps_thinking(monkeypatch):
    kw = _capture_stream_kwargs(monkeypatch, "claude-sonnet-5-5", "high")
    assert kw.get("thinking") == {"type": "adaptive"}
    assert kw.get("output_config") == {"effort": "high"}


# ---------------------------------------------------------------------------
# OpenAI usage decomposition (cached + written cache tokens out of prompt_tokens)
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from core.layers.providers.openai_adapter import OpenAIAdapter


def _usage(prompt, completion, cached=None, written=None):
    details = None
    if cached is not None or written is not None:
        details = SimpleNamespace(cached_tokens=cached or 0)
        if written is not None:
            details.cache_write_tokens = written
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                           prompt_tokens_details=details)


def test_openai_usage_subtracts_cached_tokens():
    u = OpenAIAdapter._decompose_usage(_usage(1000, 50, cached=800))
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens,
            u.output_tokens) == (200, 800, 0, 50)


def test_openai_usage_subtracts_cache_writes_gpt56():
    # gpt-5.6+: prompt_tokens includes written-to-cache tokens too; they bill
    # at 1.25x and must come OUT of the plain-rate input column.
    u = OpenAIAdapter._decompose_usage(_usage(1000, 50, cached=600, written=300))
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens) == (100, 600, 300)


def test_openai_usage_no_details_is_all_plain_input():
    u = OpenAIAdapter._decompose_usage(_usage(1000, 50))
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens) == (1000, 0, 0)


def test_openai_usage_cost_matches_openai_bill_gpt56():
    # End-to-end with the shared calculate_cost: terra rates (2.00 in, 12 out,
    # 2.50 write = 1.25x, 0.20 read = 0.1x — the 2026-07-30 cut) — the
    # decomposed row must price to exactly what OpenAI bills.
    u = OpenAIAdapter._decompose_usage(
        _usage(1_000_000, 100_000, cached=600_000, written=300_000))
    cost = OpenAIAdapter().calculate_cost("gpt-5.6-terra", u)
    #   100k plain * 2.00 + 300k written * 2.50 + 600k read * 0.20 + 100k out * 12
    assert abs(cost - (0.20 + 0.75 + 0.12 + 1.2)) < 1e-9


# ---------------------------------------------------------------------------
# Anthropic conversation-prefix caching (moving breakpoint on the last message)
# ---------------------------------------------------------------------------


def test_history_breakpoint_wraps_string_content():
    msgs = [{"role": "user", "content": "hello"}]
    out = _with_history_breakpoint(msgs)
    assert out[-1]["content"] == [{
        "type": "text", "text": "hello",
        "cache_control": {"type": "ephemeral"},
    }]
    # copy-on-write: the session's stored history must stay unmarked
    assert msgs[-1]["content"] == "hello"


def test_history_breakpoint_marks_last_block_only():
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
        ]},
    ]
    out = _with_history_breakpoint(msgs)
    assert "cache_control" not in out[0].get("content", [{}])[0] if isinstance(out[0]["content"], list) else True
    assert "cache_control" not in out[-1]["content"][0]
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in msgs[-1]["content"][-1]  # original untouched


def test_history_breakpoint_skips_empty_and_thinking():
    assert _with_history_breakpoint([]) == []
    msgs = [{"role": "user", "content": ""}]
    assert _with_history_breakpoint(msgs) is msgs
    msgs = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "…"}]}]
    assert _with_history_breakpoint(msgs) is msgs


# ---------------------------------------------------------------------------
# Thinking deltas — reasoning_content / reasoning fields, inline <think> spans,
# Anthropic thinking blocks. The thinking text must never reach the assistant
# content that is sent back to the provider.
# ---------------------------------------------------------------------------

from core.layers.providers.openai_adapter import _ThinkTagSplitter


def _delta(**kw):
    base = {"content": None, "tool_calls": None}
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeChunk:
    def __init__(self, delta=None, finish=None, usage=None):
        self.choices = (
            [SimpleNamespace(delta=delta, finish_reason=finish)]
            if delta is not None or finish else []
        )
        self.usage = usage


class _FakeOpenAIClient:
    """Fake SDK client: ``chat.completions.create`` replays ``chunks``;
    ``responses.create`` replays ``responses_events`` (a list of scripted
    event objects, or an exception to raise on create). Every create call's
    kwargs land in ``captured`` (the last call) and ``captured_calls``."""

    def __init__(self, chunks, captured=None, responses_events=None):
        self._chunks = chunks
        self._captured = captured
        self._responses_events = responses_events
        self.captured_calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.responses = SimpleNamespace(create=self._create_responses)

    async def _create(self, **kwargs):
        if self._captured is not None:
            self._captured.update(kwargs)
        self.captured_calls.append(("chat", kwargs))

        async def gen():
            for c in self._chunks:
                yield c
        return gen()

    async def _create_responses(self, **kwargs):
        if self._captured is not None:
            self._captured.clear()
            self._captured.update(kwargs)
        self.captured_calls.append(("responses", kwargs))
        script = self._responses_events
        if isinstance(script, list) and script and isinstance(script[0], Exception):
            # First create raises, the retry replays the rest.
            exc = script[0]
            self._responses_events = script[1:]
            raise exc
        if isinstance(script, Exception):
            raise script

        async def gen():
            for ev in (script or []):
                if isinstance(ev, Exception):
                    raise ev
                yield ev
        return gen()

    async def close(self):
        pass


_CHAT_GATEWAY = "https://gateway.test/v1"   # a custom base URL → chat completions


def _run_openai_capture(monkeypatch, chunks, adapter=None, *, model="m",
                        effort="", tools=None, endpoint_url=_CHAT_GATEWAY,
                        messages=None, responses_events=None, client_box=None):
    """Drive ``stream_response`` through a fake client; returns
    ``(events, create_kwargs)``. The default ``endpoint_url`` is a custom
    gateway, which keeps the ``openai`` adapter on the chat-completions path
    these tests exercise; pass ``endpoint_url=None`` for the Responses path."""
    import openai as _openai
    captured: dict = {}

    def _factory(**kw):
        client = _FakeOpenAIClient(chunks, captured, responses_events)
        if client_box is not None:
            client_box.append(client)
        return client

    monkeypatch.setattr(_openai, "AsyncOpenAI", _factory)
    events = []

    async def go():
        async for ev in (adapter or OpenAIAdapter()).stream_response(
            api_key="k", model=model, system_prompt="s",
            messages=messages or [{"role": "user", "content": "hi"}],
            tools=tools or [], max_tokens=64, effort=effort,
            endpoint_url=endpoint_url,
        ):
            events.append(ev)

    asyncio.run(go())
    return events, captured


def _run_openai(monkeypatch, chunks, adapter=None):
    return _run_openai_capture(monkeypatch, chunks, adapter)[0]


def _kinds(events):
    return [(e.type, e.text) for e in events if e.type in ("thinking_delta", "text_delta")]


def test_openai_reasoning_content_streams_as_thinking(monkeypatch):
    events = _run_openai(monkeypatch, [
        _FakeChunk(_delta(reasoning_content="Let me")),
        _FakeChunk(_delta(reasoning_content=" think")),
        _FakeChunk(_delta(content="Answer")),
        _FakeChunk(_delta(), finish="stop"),
    ])
    assert _kinds(events) == [
        ("thinking_delta", "Let me"), ("thinking_delta", " think"),
        ("text_delta", "Answer"),
    ]
    raw = [e for e in events if e.type == "content"][0].raw_content
    assert raw["content"] == "Answer"


def test_openai_groq_reasoning_field_streams_as_thinking(monkeypatch):
    events = _run_openai(monkeypatch, [
        _FakeChunk(_delta(reasoning="r1")),
        _FakeChunk(_delta(content="ok"), finish="stop"),
    ])
    assert _kinds(events) == [("thinking_delta", "r1"), ("text_delta", "ok")]


def test_openai_inline_think_tags_split_across_chunks(monkeypatch):
    events = _run_openai(monkeypatch, [
        _FakeChunk(_delta(content="<thi")),
        _FakeChunk(_delta(content="nk>plan")),
        _FakeChunk(_delta(content=" more</th")),
        _FakeChunk(_delta(content="ink>final")),
        _FakeChunk(_delta(), finish="stop"),
    ])
    assert _kinds(events) == [
        ("thinking_delta", "plan"), ("thinking_delta", " more"),
        ("text_delta", "final"),
    ]
    raw = [e for e in events if e.type == "content"][0].raw_content
    assert raw["content"] == "final"


def test_think_tag_splitter_plain_text_and_stray_close():
    s = _ThinkTagSplitter()
    assert s.feed("hello ") == [("text", "hello ")]
    assert s.feed("world") == [("text", "world")]
    assert s.flush() == []
    s = _ThinkTagSplitter()
    # A closing tag with no opener is ordinary text.
    assert s.feed("a</think>b") + s.flush() == [("text", "a</think>b")]
    s = _ThinkTagSplitter()
    # Held-back partial prefix that never becomes a tag is flushed as text.
    assert s.feed("x<th") == [("text", "x")]
    assert s.flush() == [("text", "<th")]


class _FakeEventStream:
    def __init__(self, events):
        self._events = list(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def get_final_message(self):
        return _FakeMsg()


def test_anthropic_thinking_delta_streams_as_thinking(monkeypatch):
    stream_events = [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="thinking")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(thinking="hmm")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(signature="sig")),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="done")),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn")),
    ]

    class _Msgs:
        def stream(self, **kwargs):
            return _FakeEventStream(stream_events)

    class _Client:
        messages = _Msgs()

        async def close(self):
            pass

    monkeypatch.setattr(anthropic_adapter.anthropic, "AsyncAnthropic", lambda **kw: _Client())
    events = []

    async def go():
        async for ev in AnthropicAdapter().stream_response(
            api_key="k", model="claude-sonnet-5", system_prompt="s",
            messages=[{"role": "user", "content": "hi"}], tools=[],
            max_tokens=64,
        ):
            events.append(ev)

    asyncio.run(go())
    assert _kinds(events) == [("thinking_delta", "hmm"), ("text_delta", "done")]
    assert [e.stop_reason for e in events if e.type == "stop"] == ["end_turn"]


def test_anthropic_server_tool_result_block_yields_tool_result(monkeypatch):
    """A server-side tool's result block ends its tool row: tool_start for the
    server_tool_use block, tool_result (with a preview) for the *_tool_result
    block that follows; a server tool the stream never answers is closed at
    the end of the stream."""
    stream_events = [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
            type="server_tool_use", name="web_search", id="srv_1")),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
            type="web_search_tool_result", tool_use_id="srv_1",
            content=[SimpleNamespace(url="a"), SimpleNamespace(url="b")])),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
            type="server_tool_use", name="code_execution", id="srv_2")),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="done")),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn")),
    ]

    class _Msgs:
        def stream(self, **kwargs):
            return _FakeEventStream(stream_events)

    class _Client:
        messages = _Msgs()

        async def close(self):
            pass

    monkeypatch.setattr(anthropic_adapter.anthropic, "AsyncAnthropic", lambda **kw: _Client())
    events = []

    async def go():
        async for ev in AnthropicAdapter().stream_response(
            api_key="k", model="claude-sonnet-5", system_prompt="s",
            messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=64,
        ):
            events.append(ev)

    asyncio.run(go())
    tools = [(e.type, e.tool_id, e.tool_name, e.text) for e in events
             if e.type in ("tool_start", "tool_result", "tool_stop")]
    assert tools == [
        ("tool_start", "srv_1", "web_search", ""),
        ("tool_result", "srv_1", "web_search", "2 result(s)"),
        ("tool_start", "srv_2", "code_execution", ""),
        ("tool_result", "srv_2", "code_execution", ""),   # closed at stream end
    ]
    assert _kinds(events) == [("text_delta", "done")]


# ---------------------------------------------------------------------------
# Effort → request parameters. OpenAI keeps the no-tools condition (verified
# live 2026-09-06: chat completions 400 on reasoning_effort + function tools);
# Groq is verified to accept the pair; the local adapters always send
# reasoning_effort plus their server's thinking switch.
# ---------------------------------------------------------------------------

from core.layers.providers import openai_adapter as _oa_mod
from core.layers.providers.openai_compat_adapter import (
    GroqAdapter, OllamaAdapter, OpenAICompatibleAdapter,
)

_TOOL = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
_STOP = [_FakeChunk(_delta(content="ok"), finish="stop")]


def _effort_kwargs(monkeypatch, adapter, *, effort, tools, reasoning=True, model="m"):
    monkeypatch.setattr(_oa_mod.app_config, "model_supports_reasoning", lambda m: reasoning)
    return _run_openai_capture(
        monkeypatch, list(_STOP), adapter, model=model, effort=effort, tools=tools,
    )[1]


def test_openai_custom_endpoint_keeps_chat_and_the_no_tools_condition(monkeypatch):
    # A custom base URL on the openai provider (an OpenAI-compatible gateway)
    # stays on chat completions, where OpenAI rejects effort next to tools.
    kw = _effort_kwargs(monkeypatch, OpenAIAdapter(), effort="high", tools=_TOOL)
    assert "messages" in kw and "reasoning_effort" not in kw
    kw = _effort_kwargs(monkeypatch, OpenAIAdapter(), effort="high", tools=[])
    assert kw["reasoning_effort"] == "high"


def test_groq_effort_sent_with_tools(monkeypatch):
    kw = _effort_kwargs(monkeypatch, GroqAdapter(), effort="low", tools=_TOOL,
                        model="openai/gpt-oss-120b")
    assert kw["reasoning_effort"] == "low"
    assert "extra_body" not in kw


def test_groq_qwen_pinned_to_no_thinking(monkeypatch):
    kw = _effort_kwargs(monkeypatch, GroqAdapter(), effort="high", tools=_TOOL,
                        model="qwen/qwen3.6-27b")
    assert kw["reasoning_effort"] == "none"


def test_local_low_effort_turns_thinking_off_regardless_of_flag(monkeypatch):
    kw = _effort_kwargs(monkeypatch, OpenAICompatibleAdapter(), effort="low",
                        tools=_TOOL, reasoning=False)
    assert kw["reasoning_effort"] == "low"
    assert kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    kw = _effort_kwargs(monkeypatch, OllamaAdapter(), effort="medium", tools=_TOOL,
                        reasoning=False)
    assert kw["reasoning_effort"] == "medium"
    assert kw["extra_body"] == {"think": True}


def test_local_unset_effort_sends_nothing(monkeypatch):
    kw = _effort_kwargs(monkeypatch, OpenAICompatibleAdapter(), effort="", tools=_TOOL)
    assert "reasoning_effort" not in kw and "extra_body" not in kw


# ---------------------------------------------------------------------------
# OpenAI Responses API path (2026-09-07): OpenAI's own endpoint and the relay
# speak /v1/responses — effort WITH tools, reasoning summaries as thinking,
# stateless reasoning replay inside the turn's tool loop, usage decomposed
# like the chat path. Custom endpoints, the escape hatch and the compatible
# subclasses stay on chat completions.
# ---------------------------------------------------------------------------

import httpx
import openai as _openai_sdk
import pytest

from core.layers.providers.base import ProviderError
from core.layers.providers.registry import get_adapter


def _rs(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def _fc_item(call_id="call_1", name="t", arguments="", status="completed"):
    return SimpleNamespace(type="function_call", id="fc_1", call_id=call_id,
                           name=name, arguments=arguments, status=status)


def _reasoning_item(id_="rs_1", enc="ENC", text="thinking"):
    return SimpleNamespace(type="reasoning", id=id_, encrypted_content=enc,
                           summary=[SimpleNamespace(type="summary_text", text=text)])


def _usage_r(input_tokens=1000, output_tokens=50, cached=0, written=None):
    itd = SimpleNamespace(cached_tokens=cached)
    if written is not None:
        itd.cache_write_tokens = written
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                           input_tokens_details=itd,
                           output_tokens_details=SimpleNamespace(reasoning_tokens=10))


def _completed(usage=None):
    return _rs("response.completed",
               response=SimpleNamespace(status="completed", usage=usage or _usage_r()))


_TEXT_STREAM = [
    _rs("response.created", response=SimpleNamespace(status="in_progress")),
    _rs("response.output_text.delta", delta="Hel", output_index=0),
    _rs("response.output_text.delta", delta="lo", output_index=0),
    _completed(_usage_r(cached=600)),
]


def _responses_run(monkeypatch, events, *, model="gpt-6-luna", effort="high",
                   tools=None, reasoning=True, endpoint_url=None, messages=None,
                   adapter=None, client_box=None):
    monkeypatch.setattr(_oa_mod.app_config, "model_supports_reasoning", lambda m: reasoning)
    monkeypatch.setattr(_oa_mod.app_config, "DIRECT_LLM_OPENAI_API", "responses", raising=False)
    monkeypatch.setattr(_oa_mod.app_config, "DIRECT_LLM_OPENAI_REASONING_MAX_TOKENS", 32768,
                        raising=False)
    monkeypatch.setattr(OpenAIAdapter, "_summary_unsupported", False)
    return _run_openai_capture(
        monkeypatch, [], adapter, model=model, effort=effort,
        tools=_TOOL if tools is None else tools, endpoint_url=endpoint_url,
        messages=messages, responses_events=events, client_box=client_box,
    )


def test_openai_default_endpoint_uses_the_responses_api_with_effort_and_tools(monkeypatch):
    box: list = []
    events, kw = _responses_run(monkeypatch, _TEXT_STREAM, client_box=box)
    assert box[0].captured_calls[0][0] == "responses"
    assert "messages" not in kw and "stream_options" not in kw
    assert kw["instructions"] == "s"
    assert kw["input"] == [{"role": "user", "content": "hi"}]
    assert kw["tools"] == [
        {"type": "function", "name": "t", "description": "d",
         "parameters": {"type": "object", "properties": {}}, "strict": False},
        {"type": "web_search"},          # gpt-6-luna is a server-tools model
    ]
    assert kw["reasoning"] == {"effort": "high", "summary": "auto"}
    assert kw["include"] == ["reasoning.encrypted_content"]
    assert kw["store"] is False and kw["stream"] is True
    assert kw["max_output_tokens"] == 32768      # reasoning tokens count here
    assert _kinds(events) == [("text_delta", "Hel"), ("text_delta", "lo")]


def test_openai_relay_endpoint_uses_the_responses_api(monkeypatch):
    monkeypatch.setattr(_oa_mod.app_config, "OTODOCK_RELAY_BASE", "https://relay.test",
                        raising=False)
    _, kw = _responses_run(monkeypatch, _TEXT_STREAM,
                           endpoint_url="https://relay.test/v1/relay/openai/v1")
    assert "input" in kw and "messages" not in kw
    # A look-alike host is a custom endpoint → chat completions.
    _, kw = _responses_run(monkeypatch, _TEXT_STREAM,
                           endpoint_url="https://relay.test.example/v1/relay/openai/v1")
    assert "messages" in kw and "input" not in kw


def test_openai_chat_escape_hatch_and_compat_subclasses_stay_on_chat(monkeypatch):
    monkeypatch.setattr(_oa_mod.app_config, "model_supports_reasoning", lambda m: True)
    monkeypatch.setattr(_oa_mod.app_config, "DIRECT_LLM_OPENAI_API", "chat", raising=False)
    _, kw = _run_openai_capture(monkeypatch, list(_STOP), OpenAIAdapter(),
                                effort="high", tools=_TOOL, endpoint_url=None)
    assert "messages" in kw and "reasoning_effort" not in kw   # the old condition
    monkeypatch.setattr(_oa_mod.app_config, "DIRECT_LLM_OPENAI_API", "responses",
                        raising=False)
    for adapter in (GroqAdapter(), OllamaAdapter(), OpenAICompatibleAdapter()):
        _, kw = _run_openai_capture(monkeypatch, list(_STOP), adapter,
                                    effort="high", tools=_TOOL, endpoint_url=None)
        assert "messages" in kw and "input" not in kw, adapter.provider_name


def test_responses_reasoning_model_without_effort_gets_include_only(monkeypatch):
    _, kw = _responses_run(monkeypatch, _TEXT_STREAM, effort="")
    assert kw["include"] == ["reasoning.encrypted_content"]
    assert "reasoning" not in kw and kw["max_output_tokens"] == 64
    _, kw = _responses_run(monkeypatch, _TEXT_STREAM, effort="high", reasoning=False)
    assert "reasoning" not in kw and "include" not in kw
    assert kw["max_output_tokens"] == 64


def test_responses_summary_rejected_retries_without_it_and_remembers(monkeypatch):
    req = httpx.Request("POST", "https://api.openai.test/v1/responses")
    rejection = _openai_sdk.APIStatusError(
        "reasoning.summary is not available: organization not verified",
        response=httpx.Response(400, request=req), body=None,
    )
    box: list = []
    events, kw = _responses_run(monkeypatch, [rejection, *_TEXT_STREAM], client_box=box)
    calls = box[0].captured_calls
    assert [c[0] for c in calls] == ["responses", "responses"]
    assert calls[0][1]["reasoning"] == {"effort": "high", "summary": "auto"}
    assert calls[1][1]["reasoning"] == {"effort": "high"}
    assert OpenAIAdapter._summary_unsupported is True
    assert _kinds(events) == [("text_delta", "Hel"), ("text_delta", "lo")]
    # Remembered: the next request never sends a summary.
    box2: list = []
    _run_openai_capture(monkeypatch, [], None, model="gpt-6-luna", effort="high",
                        tools=_TOOL, endpoint_url=None, responses_events=_TEXT_STREAM,
                        client_box=box2)
    assert box2[0].captured_calls[0][1]["reasoning"] == {"effort": "high"}
    # An unrelated 400 is not retried.
    other = _openai_sdk.APIStatusError(
        "invalid model", response=httpx.Response(400, request=req), body=None,
    )
    with pytest.raises(ProviderError):
        _responses_run(monkeypatch, other)


def test_responses_history_translation_replays_reasoning_only_for_the_current_turn():
    rs1 = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "E1"}
    rs2 = {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "E2"}
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": None, "reasoning": [rs1], "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "t", "arguments": "{\"x\": 1}"}},
            {"id": "call_orphan", "type": "function",       # never answered
             "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": [
            {"type": "text", "text": "q2"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]},
        {"role": "assistant", "content": "calling", "reasoning": [rs2], "tool_calls": [
            {"id": "call_b", "type": "function",
             "function": {"name": "t", "arguments": {"y": 2}}},
        ]},
        {"role": "tool", "tool_call_id": "call_b", "content": {"r": 1}},
    ]
    items = OpenAIAdapter._responses_input(messages)
    assert items == [
        {"role": "user", "content": "q1"},
        # turn 1: no reasoning replay; the orphan call is skipped; no ids
        {"type": "function_call", "call_id": "call_a", "name": "t", "arguments": "{\"x\": 1}"},
        {"type": "function_call_output", "call_id": "call_a", "output": "ok"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": [
            {"type": "input_text", "text": "q2"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA", "detail": "auto"},
        ]},
        # turn 2 (after the last user message): reasoning → text → call
        rs2,
        {"role": "assistant", "content": "calling"},
        {"type": "function_call", "call_id": "call_b", "name": "t", "arguments": "{\"y\": 2}"},
        {"type": "function_call_output", "call_id": "call_b", "output": "{\"r\": 1}"},
    ]
    # Images are sent verbatim through stream_response too (no chat blocks leak).
    assert "image_url" not in str(items[4]["content"][1].get("type"))


def test_responses_event_mapping_tool_call_reasoning_usage_and_stop(monkeypatch):
    events_in = [
        _rs("response.created", response=SimpleNamespace(status="in_progress")),
        _rs("response.output_item.added", output_index=0, item=_reasoning_item()),
        _rs("response.reasoning_summary_text.delta", delta="plan", output_index=0),
        _rs("response.output_item.done", output_index=0, item=_reasoning_item()),
        _rs("response.output_item.added", output_index=1, item=_fc_item(arguments="")),
        _rs("response.function_call_arguments.delta", output_index=1, delta="{\"a\":"),
        _rs("response.function_call_arguments.delta", output_index=1, delta=" 1}"),
        _rs("response.output_item.done", output_index=1,
            item=_fc_item(arguments="{\"a\": 1}")),
        _completed(_usage_r(input_tokens=1000, cached=600, written=100, output_tokens=50)),
    ]
    events, _ = _responses_run(monkeypatch, events_in)
    kinds = [(e.type, e.text or e.tool_input_json or e.stop_reason) for e in events]
    assert kinds[:3] == [("thinking_delta", "plan"), ("tool_start", ""), ("tool_input_delta", "{\"a\":")]
    assert kinds[3] == ("tool_input_delta", " 1}")
    stop = [e for e in events if e.type == "tool_stop"][0]
    assert (stop.tool_name, stop.tool_id, stop.tool_input_json) == ("t", "call_1", "{\"a\": 1}")
    usage = [e for e in events if e.type == "usage"][0].usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
            usage.output_tokens) == (300, 600, 100, 50)
    assert [e.stop_reason for e in events if e.type == "stop"] == ["tool_use"]
    raw = [e for e in events if e.type == "content"][0].raw_content
    assert raw["content"] is None
    assert raw["tool_calls"] == [{
        "id": "call_1", "type": "function",
        "function": {"name": "t", "arguments": "{\"a\": 1}"},
    }]
    assert raw["reasoning"] == [{
        "type": "reasoning", "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "thinking"}],
        "encrypted_content": "ENC",
    }]
    serialized = OpenAIAdapter().serialize_assistant_content(raw)
    assert serialized == {"content": None, "tool_calls": raw["tool_calls"],
                          "reasoning": raw["reasoning"]}


def test_responses_text_only_serializes_to_a_string(monkeypatch):
    events, _ = _responses_run(monkeypatch, _TEXT_STREAM)
    raw = [e for e in events if e.type == "content"][0].raw_content
    assert raw == {"content": "Hello", "tool_calls": [], "reasoning": []}
    assert OpenAIAdapter().serialize_assistant_content(raw) == "Hello"
    assert [e.stop_reason for e in events if e.type == "stop"] == ["end_turn"]
    usage = [e for e in events if e.type == "usage"][0].usage
    assert (usage.input_tokens, usage.cache_read_tokens) == (400, 600)


def test_responses_incomplete_drops_calls_and_stops_with_length(monkeypatch):
    events_in = [
        _rs("response.output_item.added", output_index=0, item=_fc_item()),
        _rs("response.output_item.done", output_index=0, item=_fc_item(arguments="{}")),
        _rs("response.output_text.delta", delta="partial", output_index=1),
        _rs("response.incomplete", response=SimpleNamespace(
            status="incomplete", usage=_usage_r(),
            incomplete_details=SimpleNamespace(reason="max_output_tokens"))),
    ]
    events, _ = _responses_run(monkeypatch, events_in)
    assert [e.stop_reason for e in events if e.type == "stop"] == ["length"]
    raw = [e for e in events if e.type == "content"][0].raw_content
    assert raw["content"] == "partial" and raw["tool_calls"] == []


def test_responses_incomplete_without_text_is_a_provider_error(monkeypatch):
    events_in = [_rs("response.incomplete", response=SimpleNamespace(
        status="incomplete", usage=None,
        incomplete_details=SimpleNamespace(reason="max_output_tokens")))]
    with pytest.raises(ProviderError) as ei:
        _responses_run(monkeypatch, events_in)
    assert "max_output_tokens" in str(ei.value)


def test_responses_failed_and_error_events_raise(monkeypatch):
    failed = [_rs("response.output_text.delta", delta="x", output_index=0),
              _rs("response.failed", response=SimpleNamespace(
                  status="failed", error=SimpleNamespace(message="boom")))]
    with pytest.raises(ProviderError) as ei:
        _responses_run(monkeypatch, failed)
    assert "boom" in str(ei.value)
    with pytest.raises(ProviderError):
        _responses_run(monkeypatch, [_rs("error", message="stream error", code="x")])


def test_chat_path_strips_reasoning_and_drops_calls_on_a_length_finish(monkeypatch):
    monkeypatch.setattr(_oa_mod.app_config, "model_supports_reasoning", lambda m: False)
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "reasoning": [{"type": "reasoning"}]},
        {"role": "user", "content": "hi"},
    ]
    _, kw = _run_openai_capture(monkeypatch, list(_STOP), messages=history)
    assert kw["messages"][2] == {"role": "assistant", "content": "a"}
    # A call cut off by the output cap never reaches the history.
    chunks = [
        _FakeChunk(_delta(tool_calls=[SimpleNamespace(
            index=0, id="call_1", function=SimpleNamespace(name="t", arguments="{\"a"))])),
        _FakeChunk(_delta(), finish="length"),
    ]
    events = _run_openai(monkeypatch, chunks)
    raw = [e for e in events if e.type == "content"][0].raw_content
    assert raw["tool_calls"] == [] and not [e for e in events if e.type == "tool_stop"]
    assert [e.stop_reason for e in events if e.type == "stop"] == ["length"]


def test_registry_falls_back_to_the_compatible_adapter():
    # An unknown provider must NOT get the openai adapter (Responses API) —
    # it is an OpenAI-compatible server by definition.
    assert get_adapter("some-gateway").provider_name == "openai_compatible"
    assert get_adapter("openai").provider_name == "openai"


# ---------------------------------------------------------------------------
# OpenAI web search (2026-09-07): the Responses API's built-in tool rides with
# the client tools on server-tool models; a web_search_call item is a server
# tool row (tool_start / tool_result), counted for the per-call fee and
# replayed within the turn.
# ---------------------------------------------------------------------------

def _ws_item(id_="ws_1", status="completed", query="weather athens"):
    action = SimpleNamespace(type="search", query=query, queries=None,
                             sources=[SimpleNamespace(type="url", url="https://x")])
    return SimpleNamespace(type="web_search_call", id=id_, status=status, action=action)


def test_responses_web_search_tool_rides_only_with_client_tools_on_server_tool_models(monkeypatch):
    monkeypatch.setattr(_oa_mod.app_config, "model_supports_server_tools",
                        lambda m: m == "gpt-6-luna")
    _, kw = _responses_run(monkeypatch, _TEXT_STREAM)
    assert kw["tools"][0]["type"] == "function"
    assert kw["tools"][-1] == {"type": "web_search"}
    # No client tools (a chat-title request) → no built-in tool either.
    _, kw = _responses_run(monkeypatch, _TEXT_STREAM, tools=[])
    assert "tools" not in kw
    # A model without server tools → function tools only.
    _, kw = _responses_run(monkeypatch, _TEXT_STREAM, model="gpt-custom")
    assert [t["type"] for t in kw["tools"]] == ["function"]
    # The chat-completions path never carries it.
    monkeypatch.setattr(_oa_mod.app_config, "DIRECT_LLM_OPENAI_API", "chat", raising=False)
    monkeypatch.setattr(_oa_mod.app_config, "model_supports_reasoning", lambda m: True)
    _, kw = _run_openai_capture(monkeypatch, list(_STOP), OpenAIAdapter(), model="gpt-6-luna",
                                tools=_TOOL, endpoint_url=None)
    assert [t["type"] for t in kw["tools"]] == ["function"]


def test_responses_web_search_events_map_to_a_server_tool_row(monkeypatch):
    monkeypatch.setattr(_oa_mod.app_config, "model_supports_server_tools", lambda m: True)
    events_in = [
        _rs("response.created", response=SimpleNamespace(status="in_progress")),
        _rs("response.output_item.added", output_index=0, item=_reasoning_item()),
        _rs("response.output_item.done", output_index=0, item=_reasoning_item()),
        _rs("response.output_item.added", output_index=1, item=_ws_item(status="in_progress")),
        _rs("response.web_search_call.searching", output_index=1, item_id="ws_1"),
        _rs("response.output_item.done", output_index=1, item=_ws_item()),
        _rs("response.output_item.done", output_index=2,
            item=_ws_item(id_="ws_2", status="failed", query="x")),      # no added event
        _rs("response.output_item.added", output_index=3,
            item=_ws_item(id_="ws_3", status="in_progress")),            # never done
        _rs("response.output_text.delta", delta="Sunny", output_index=4),
        _completed(_usage_r(input_tokens=1000, output_tokens=50)),
    ]
    events, _ = _responses_run(monkeypatch, events_in)
    tools = [(e.type, e.tool_id, e.tool_name, e.text) for e in events
             if e.type in ("tool_start", "tool_result", "tool_stop")]
    assert tools == [
        ("tool_start", "ws_1", "web_search", ""),
        ("tool_result", "ws_1", "web_search", "weather athens"),
        ("tool_start", "ws_2", "web_search", ""),          # opened at its done event
        ("tool_result", "ws_2", "web_search", "failed: x"),
        ("tool_start", "ws_3", "web_search", ""),
        ("tool_result", "ws_3", "web_search", ""),         # closed at stream end
    ]
    assert _kinds(events) == [("text_delta", "Sunny")]
    assert [e.stop_reason for e in events if e.type == "stop"] == ["end_turn"]  # not a client call
    usage = [e for e in events if e.type == "usage"][0].usage
    assert usage.web_search_requests == 1               # ws_2 failed, ws_3 never completed
    assert (usage.input_tokens, usage.output_tokens) == (1000, 50)
    raw = [e for e in events if e.type == "content"][0].raw_content
    assert raw["tool_calls"] == []
    assert raw["reasoning"] == [
        {"type": "reasoning", "id": "rs_1",
         "summary": [{"type": "summary_text", "text": "thinking"}], "encrypted_content": "ENC"},
        {"type": "web_search_call", "id": "ws_1", "status": "completed",
         "action": {"type": "search", "query": "weather athens"}},
        {"type": "web_search_call", "id": "ws_2", "status": "failed",
         "action": {"type": "search", "query": "x"}},
    ]
    assert OpenAIAdapter().serialize_assistant_content(raw) == {
        "content": "Sunny", "reasoning": raw["reasoning"],
    }


def test_responses_history_replays_web_search_items_within_the_turn_only():
    rs = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "E"}
    ws = {"type": "web_search_call", "id": "ws_1", "status": "completed",
          "action": {"type": "search", "query": "q"}}
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "old", "reasoning": [rs, ws]},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": None, "reasoning": [rs, ws], "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
    ]
    assert OpenAIAdapter._responses_input(messages) == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "old"},             # earlier turn: items dropped
        {"role": "user", "content": "q2"},
        rs, ws,                                              # this turn: in output order
        {"type": "function_call", "call_id": "call_a", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_a", "output": "ok"},
    ]
    assert all("reasoning" not in m for m in OpenAIAdapter._chat_history(messages))


def test_calculate_cost_adds_the_web_search_fee(monkeypatch):
    from core.layers.providers.base import ProviderUsage
    monkeypatch.setattr(_oa_mod.app_config, "get_model_pricing",
                        lambda m, p="": (1.0, 2.0, 0.0, 0.0))
    monkeypatch.setattr(_oa_mod.app_config, "WEB_SEARCH_USD_PER_REQUEST", 0.01, raising=False)
    cost = OpenAIAdapter().calculate_cost(
        "m", ProviderUsage(input_tokens=1_000_000, web_search_requests=3))
    assert abs(cost - 1.03) < 1e-9
    assert OpenAIAdapter().calculate_cost("m", ProviderUsage(input_tokens=1_000_000)) == 1.0


def test_anthropic_usage_reports_the_web_searches(monkeypatch):
    stream_events = [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="ok")),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn")),
    ]

    class _Stream(_FakeEventStream):
        async def get_final_message(self):
            return SimpleNamespace(content=[], usage=SimpleNamespace(
                input_tokens=60, output_tokens=800, cache_creation_input_tokens=900,
                cache_read_input_tokens=57000,
                server_tool_use=SimpleNamespace(web_search_requests=2, web_fetch_requests=1),
            ))

    class _Msgs:
        def stream(self, **kwargs):
            return _Stream(stream_events)

    class _Client:
        messages = _Msgs()

        async def close(self):
            pass

    monkeypatch.setattr(anthropic_adapter.anthropic, "AsyncAnthropic", lambda **kw: _Client())
    events = []

    async def go():
        async for ev in AnthropicAdapter().stream_response(
            api_key="k", model="claude-sonnet-5", system_prompt="s",
            messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=64,
        ):
            events.append(ev)

    asyncio.run(go())
    usage = [e for e in events if e.type == "usage"][0].usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
            usage.output_tokens, usage.web_search_requests) == (60, 57000, 900, 800, 2)
