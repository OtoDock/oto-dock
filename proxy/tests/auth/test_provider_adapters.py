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
    kw = _capture_stream_kwargs(monkeypatch, "claude-sonnet-5", "high")
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
    # End-to-end with the shared calculate_cost: terra rates (2.50 in, 15 out,
    # 3.125 write = 1.25x, 0.25 read = 0.1x) — the decomposed row must price to
    # exactly what OpenAI bills.
    u = OpenAIAdapter._decompose_usage(
        _usage(1_000_000, 100_000, cached=600_000, written=300_000))
    cost = OpenAIAdapter().calculate_cost("gpt-5.6-terra", u)
    #   100k plain * 2.50 + 300k written * 3.125 + 600k read * 0.25 + 100k out * 15
    assert abs(cost - (0.25 + 0.9375 + 0.15 + 1.5)) < 1e-9


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
