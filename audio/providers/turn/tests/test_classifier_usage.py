"""Turn-classifier token-usage capture (local per-agent cost tracking).

The Groq classifier accumulates the token usage of each non-streaming call and the
dispatcher drains it once per phone call. Smart-Turn-only routes (no Groq) drain to
(0, 0). The mock vendor stands in via httpx.MockTransport.
"""

import asyncio

import httpx

from audio.providers.turn.dispatcher import TurnClassifierDispatcher
from audio.providers.turn.groq import GroqTurnClassifier

_RESP = {
    "choices": [{"message": {"content": "complete"}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 2},
}


def _stub_groq(payload: dict) -> GroqTurnClassifier:
    c = GroqTurnClassifier(api_key="k")
    c._client = httpx.AsyncClient(
        base_url=c._base_url,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)),
    )
    return c


def test_groq_accumulates_across_calls_and_drains():
    c = _stub_groq(_RESP)

    async def run():
        await c.classify("hello", "ctx")
        await c.classify("world", "ctx")
        await c.classify_continuation("orig", "more", "ctx")
        await c.close()

    asyncio.run(run())
    assert c.drain_usage() == (360, 6)      # 3 calls × (120, 2)
    assert c.drain_usage() == (0, 0)        # reset after drain
    assert c.model == "openai/gpt-oss-120b"


def test_groq_missing_usage_is_safe():
    c = _stub_groq({"choices": [{"message": {"content": "complete"}}]})  # no usage block

    async def run():
        await c.classify("hi", "ctx")
        await c.close()

    asyncio.run(run())
    assert c.drain_usage() == (0, 0)


def test_dispatcher_drain_none_safe_without_groq():
    disp = TurnClassifierDispatcher(
        smart_turn=None, groq=None, lang_map={}, default_backend="smart_turn")
    assert disp.drain_usage() == (0, 0)
    assert disp.classifier_model() == ""


def test_dispatcher_drain_delegates_to_groq():
    c = _stub_groq(_RESP)
    disp = TurnClassifierDispatcher(
        smart_turn=None, groq=c, lang_map={"el": "groq"}, default_backend="groq")

    async def run():
        await disp.classify("hi", "ctx", "el")
        await c.close()

    asyncio.run(run())
    assert disp.drain_usage() == (120, 2)
    assert disp.classifier_model() == "openai/gpt-oss-120b"
