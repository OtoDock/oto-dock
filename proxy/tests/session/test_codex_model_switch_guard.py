"""Codex fixes ``model_provider`` in config.toml at session start, so the
dashboard refuses a mid-chat model change that would cross providers (local
endpoint ↔ OpenAI) and tells the user to start a new chat."""

import sys

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

import config
from services.engines import subscription_pool
from storage.billing import subscription_store
import ws.dashboard  # noqa: F401  — the chat module is imported through the WS package
from ws.dashboard_chat import _codex_provider_switch_blocker


def _wire(monkeypatch, bound_provider, sub_id="sub-1"):
    monkeypatch.setattr(subscription_pool, "get_session_subscription", lambda sid: sub_id)
    monkeypatch.setattr(subscription_store, "get_subscription",
                        lambda sid: {"id": sid, "provider": bound_provider} if sid else None)
    monkeypatch.setattr(config, "get_model_provider", lambda model, layer="": {
        "gpt-5.6-sol": "openai",
        "qwen3.6-35b-a3b": "openai_compatible",
    }[model])


def test_cross_provider_switch_is_refused(monkeypatch):
    _wire(monkeypatch, "openai")
    reason = _codex_provider_switch_blocker("s1", "qwen3.6-35b-a3b")
    assert "Start a new chat" in reason
    assert "OpenAI" in reason and "local OpenAI-compatible endpoint" in reason


def test_same_provider_switch_is_allowed(monkeypatch):
    _wire(monkeypatch, "openai_compatible")
    assert _codex_provider_switch_blocker("s1", "qwen3.6-35b-a3b") == ""


def test_unbound_session_is_not_guarded(monkeypatch):
    _wire(monkeypatch, "openai", sub_id=None)
    assert _codex_provider_switch_blocker("s1", "qwen3.6-35b-a3b") == ""
    _wire(monkeypatch, "openai", sub_id="default")
    assert _codex_provider_switch_blocker("s1", "qwen3.6-35b-a3b") == ""
