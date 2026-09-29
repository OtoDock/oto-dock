"""Tests for run_direct_stream — focused on chat-attached image handling.

The Direct LLM path attaches photos as native vision content blocks on the
user message (no built-in Read tool, unlike CLI/Codex). This test fixture
swaps in a stub provider adapter so we can exercise the message-construction
logic without actual API calls.

Verifies:
- ``images=[{base64, media_type}]`` → user message becomes a content-block list
  with one text block + N image blocks formatted via the adapter.
- ``images=None`` → user message stays a plain string (regression-safe).
- Multiple images → multiple image blocks in order.
"""

from typing import AsyncIterator

import pytest

from core.layers.providers.base import (
    ProviderAdapter, ProviderStreamEvent, ProviderUsage,
)
from core.layers.providers.registry import register_adapter


_STUB_PROVIDER = "stub-vision-test"


class _StubAdapter(ProviderAdapter):
    """Minimal adapter that yields a single usage event then exits.

    Uses Anthropic's image-block shape so the test can assert the exact
    content structure run_direct_stream produces with non-empty ``images``.
    """

    @property
    def provider_name(self) -> str:
        return _STUB_PROVIDER

    async def stream_response(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
        # No text, no tool calls — just a usage event so the loop exits.
        yield ProviderStreamEvent(
            type="usage",
            usage=ProviderUsage(input_tokens=10, output_tokens=0),
        )

    def format_tool_results(self, results):
        return []

    def serialize_assistant_content(self, raw_content):
        return ""

    def format_image_content_block(self, *, media_type: str, base64_data: str) -> dict:
        # Mirror Anthropic's shape so tests can assert clearly.
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64_data,
            },
        }


# Register the stub adapter once at module import. Idempotent across test runs
# (registry overwrites on duplicate provider name).
register_adapter(_StubAdapter())


@pytest.fixture
def direct_session(monkeypatch):
    """Build a DirectSession that uses the stub adapter."""
    from core.layers.direct.session import DirectSession
    # Skip DB resolution of the agent model — we never reach a real API call.
    monkeypatch.setattr(
        "core.layers.direct.session.config.get_agent_model",
        lambda agent: "",
    )
    session = DirectSession(
        session_id="test-session",
        agent_name="test-agent",
        system_prompt="You are a test agent.",
        provider=_STUB_PROVIDER,
    )
    session.model = "test-model"
    # Non-empty api_key = the normal pool-provisioned case; the keyless tests
    # below blank it to exercise the clean no-credentials error path.
    session.api_key = "stub-test-key"
    return session


@pytest.mark.asyncio
async def test_run_direct_stream_attaches_image_block_when_images_present(direct_session):
    """``images=[one image]`` produces a content-block list: text + image."""
    from core.layers.direct.session import run_direct_stream

    images = [{"base64": "AAAA", "media_type": "image/jpeg"}]
    async for _ in run_direct_stream(direct_session, "What is this?", images=images):
        pass  # drain — we only care about the resulting messages list

    user_msg = direct_session.messages[-1]
    assert user_msg["role"] == "user"
    content = user_msg["content"]
    assert isinstance(content, list), "content must be a list when images are attached"
    assert len(content) == 2
    assert content[0] == {"type": "text", "text": "What is this?"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[1]["source"]["data"] == "AAAA"


@pytest.mark.asyncio
async def test_run_direct_stream_no_images_keeps_string_content(direct_session):
    """``images=None`` (default) — user content stays a plain string. This is
    the regression check: text-only chats must not become content-block lists,
    or downstream message-history handling and prompt caching could behave
    differently than today."""
    from core.layers.direct.session import run_direct_stream

    async for _ in run_direct_stream(direct_session, "Hello world"):
        pass

    user_msg = direct_session.messages[-1]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "Hello world"
    assert isinstance(user_msg["content"], str)


@pytest.mark.asyncio
async def test_run_direct_stream_multiple_images(direct_session):
    """Two photos → text block + two image blocks in input order."""
    from core.layers.direct.session import run_direct_stream

    images = [
        {"base64": "AAAA", "media_type": "image/jpeg"},
        {"base64": "BBBB", "media_type": "image/png"},
    ]
    async for _ in run_direct_stream(direct_session, "Compare these:", images=images):
        pass

    content = direct_session.messages[-1]["content"]
    assert len(content) == 3
    assert content[0]["text"] == "Compare these:"
    assert content[1]["source"]["data"] == "AAAA"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[2]["source"]["data"] == "BBBB"
    assert content[2]["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_run_direct_stream_empty_images_list_treated_as_none(direct_session):
    """``images=[]`` is functionally identical to ``images=None`` — we don't
    want to send a content-block list with zero image blocks (which would just
    be ``[{"type": "text", "text": "..."}]`` — semantically equivalent to a
    plain string but with a more complex shape and no benefit)."""
    from core.layers.direct.session import run_direct_stream

    async for _ in run_direct_stream(direct_session, "Hi", images=[]):
        pass

    assert direct_session.messages[-1]["content"] == "Hi"


@pytest.mark.asyncio
async def test_run_direct_stream_keyless_cloud_provider_clean_error(direct_session):
    """A credential-less session on a keyed provider must surface ONE clean
    error event — not the legacy AttributeError from the removed
    config.ANTHROPIC_API_KEY fallback (live-hit on relay-typed subscriptions
    whose install isn't connected to hosted credits)."""
    from core.layers.direct.session import run_direct_stream

    direct_session.api_key = ""
    events = []
    async for ev in run_direct_stream(direct_session, "Hello"):
        events.append(ev)

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "No LLM credentials" in errors[0]["data"]["message"]
    assert direct_session.provider in errors[0]["data"]["message"]


@pytest.mark.asyncio
async def test_run_direct_stream_keyless_ok_when_adapter_has_default(direct_session, monkeypatch):
    """Keyless LOCAL providers (ollama / openai-compatible) supply their own
    placeholder key via default_api_key — the turn must proceed."""
    from core.layers.direct.session import run_direct_stream
    from core.layers.direct import session as S

    adapter = S.get_adapter(direct_session.provider)
    monkeypatch.setattr(adapter.__class__, "default_api_key",
                        lambda self: "local-default", raising=False)
    direct_session.api_key = ""
    events = []
    async for ev in run_direct_stream(direct_session, "Hello"):
        events.append(ev)

    assert not [e for e in events if e["type"] == "error"]
