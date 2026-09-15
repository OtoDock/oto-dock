"""System Default (``resolve_agent_model``) prefers a provider the platform
pool can serve. Registry order alone landed a local-only install on the first
builtin (Haiku 4.5 on direct-llm) and failed with "no credentials"."""

import sys

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

import config
from storage.agents import agent_store
from storage.billing import subscription_store


_MODELS = [
    {"model_id": "claude-haiku-4-5", "provider": "anthropic", "is_builtin": True,
     "enabled": True, "created_at": "2026-01-01"},
    {"model_id": "gpt-5.6-luna", "provider": "openai", "is_builtin": True,
     "enabled": True, "created_at": "2026-01-01"},
    {"model_id": "qwen3.6-35b-a3b", "provider": "openai_compatible", "is_builtin": False,
     "enabled": True, "created_at": "2026-09-05"},
]


def _wire(monkeypatch, pool_providers, default_model=""):
    monkeypatch.setattr(agent_store, "get_agent", lambda name: {
        "execution_path": "direct-llm", "default_model": default_model,
    })
    monkeypatch.setattr(subscription_store, "list_models",
                        lambda layer=None: [dict(m) for m in _MODELS])
    monkeypatch.setattr(subscription_store, "list_platform_pool",
                        lambda layer=None, provider=None: [
                            {"provider": p} for p in pool_providers])


def test_local_only_pool_lands_on_the_local_model(monkeypatch):
    _wire(monkeypatch, ["openai_compatible"])
    assert config.resolve_agent_model("caller") == "qwen3.6-35b-a3b"


def test_pool_with_anthropic_keeps_registry_order(monkeypatch):
    _wire(monkeypatch, ["anthropic", "openai_compatible"])
    assert config.resolve_agent_model("caller") == "claude-haiku-4-5"


def test_empty_pool_keeps_registry_order(monkeypatch):
    _wire(monkeypatch, [])
    assert config.resolve_agent_model("caller") == "claude-haiku-4-5"


def test_explicit_default_model_wins(monkeypatch):
    _wire(monkeypatch, ["openai_compatible"], default_model="gpt-5.6-luna")
    assert config.resolve_agent_model("caller") == "gpt-5.6-luna"


def test_pool_provider_without_enabled_model_falls_back(monkeypatch):
    _wire(monkeypatch, ["groq"])
    assert config.resolve_agent_model("caller") == "claude-haiku-4-5"
