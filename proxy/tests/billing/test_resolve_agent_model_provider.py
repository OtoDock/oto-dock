"""System Default (``resolve_agent_model``): the engine's DECLARED default
first, then a fallback that falls DOWN the tiers from it and never up, both
restricted to providers the platform pool can serve.

Direct LLM declares ``claude-sonnet-5-5`` (tier 3 — the first usable tier for
real work; the operator's call, phone conversations included). Before the
engine-contract lane the no-pin default was whatever came first in
``MODEL_REGISTRY`` insertion order — Haiku 4.5 here.
"""

import sys

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

import config
from storage.agents import agent_store
from storage.billing import subscription_store


# Direct LLM's rows as the store returns them (tier is on the row since the
# builtin sync writes it; a custom row may have none).
_MODELS = [
    {"model_id": "claude-sonnet-5-5", "provider": "anthropic", "is_builtin": True,
     "enabled": True, "tier": 3, "created_at": "2026-01-01"},
    {"model_id": "claude-haiku-4-5", "provider": "anthropic", "is_builtin": True,
     "enabled": True, "tier": 4, "created_at": "2026-01-01"},
    {"model_id": "gpt-5.6-terra", "provider": "openai", "is_builtin": True,
     "enabled": True, "tier": 3, "created_at": "2026-01-01"},
    {"model_id": "gpt-5.6-luna", "provider": "openai", "is_builtin": True,
     "enabled": True, "tier": 4, "created_at": "2026-01-01"},
    {"model_id": "qwen3.6-35b-a3b", "provider": "openai_compatible", "is_builtin": False,
     "enabled": True, "created_at": "2026-09-05"},
]


def _wire(monkeypatch, pool_providers, default_model="", models=None,
          layer="direct-llm"):
    rows = [dict(m) for m in (models if models is not None else _MODELS)]
    monkeypatch.setattr(agent_store, "get_agent", lambda name: {
        "execution_path": layer, "default_model": default_model,
    })
    monkeypatch.setattr(subscription_store, "list_models",
                        lambda layer=None: [dict(m) for m in rows])
    monkeypatch.setattr(subscription_store, "list_platform_pool",
                        lambda layer=None, provider=None: [
                            {"provider": p} for p in pool_providers])


def _disabled(model_id):
    return [dict(m, enabled=(m["model_id"] != model_id)) for m in _MODELS]


# --- the declared default -------------------------------------------------

def test_anthropic_pool_lands_on_the_declared_default(monkeypatch):
    _wire(monkeypatch, ["anthropic", "openai_compatible"])
    assert config.resolve_agent_model("caller") == "claude-sonnet-5-5"


def test_empty_pool_applies_no_filter_and_lands_on_the_declared_default(monkeypatch):
    # Personal accounts only: _pool_providers is empty, so nothing is filtered.
    _wire(monkeypatch, [])
    assert config.resolve_agent_model("caller") == "claude-sonnet-5-5"


def test_declared_default_without_a_row_falls_back(monkeypatch):
    # An install whose admin deleted the Sonnet row: step 2 is skipped and the
    # fall-down fallback runs among what is left.
    rows = [m for m in _MODELS if m["model_id"] != "claude-sonnet-5-5"]
    _wire(monkeypatch, ["anthropic"], models=rows)
    assert config.resolve_agent_model("caller") == "claude-haiku-4-5"


# --- the fallback falls DOWN, never up -------------------------------------

def test_disabled_declared_default_falls_down_a_tier(monkeypatch):
    _wire(monkeypatch, ["anthropic"], models=_disabled("claude-sonnet-5-5"))
    assert config.resolve_agent_model("caller") == "claude-haiku-4-5"


def test_disabled_default_on_claude_lands_on_sonnet_not_fable(monkeypatch):
    # The case the rule exists for: Opus 5.5 (tier 2, the declared default)
    # disabled must NOT promote Fable 5.1 (tier 1) — it falls to Sonnet 5.
    rows = [
        {"model_id": "claude-fable-5-1", "provider": "anthropic", "is_builtin": True,
         "enabled": True, "tier": 1, "created_at": "2026-01-01"},
        {"model_id": "claude-opus-5-5", "provider": "anthropic", "is_builtin": True,
         "enabled": False, "tier": 2, "created_at": "2026-01-01"},
        {"model_id": "claude-sonnet-5-5", "provider": "anthropic", "is_builtin": True,
         "enabled": True, "tier": 3, "created_at": "2026-01-01"},
    ]
    _wire(monkeypatch, ["anthropic"], models=rows, layer="claude-code-cli")
    assert config.resolve_agent_model("caller") == "claude-sonnet-5-5"


def test_disabled_default_on_codex_lands_on_terra_not_astra(monkeypatch):
    rows = [
        {"model_id": "gpt-6-astra", "provider": "openai", "is_builtin": True,
         "enabled": True, "tier": 1, "created_at": "2026-01-01"},
        {"model_id": "gpt-6-sol", "provider": "openai", "is_builtin": True,
         "enabled": False, "tier": 2, "created_at": "2026-01-01"},
        {"model_id": "gpt-5.6-terra", "provider": "openai", "is_builtin": True,
         "enabled": True, "tier": 3, "created_at": "2026-01-01"},
    ]
    _wire(monkeypatch, ["openai"], models=rows, layer="codex-cli")
    assert config.resolve_agent_model("caller") == "gpt-5.6-terra"


def test_tier_one_is_reached_only_when_nothing_at_or_below_the_default_is_enabled(monkeypatch):
    rows = [
        {"model_id": "claude-fable-5-1", "provider": "anthropic", "is_builtin": True,
         "enabled": True, "tier": 1, "created_at": "2026-01-01"},
        {"model_id": "claude-opus-5-5", "provider": "anthropic", "is_builtin": True,
         "enabled": False, "tier": 2, "created_at": "2026-01-01"},
        {"model_id": "claude-sonnet-5-5", "provider": "anthropic", "is_builtin": True,
         "enabled": False, "tier": 3, "created_at": "2026-01-01"},
    ]
    _wire(monkeypatch, ["anthropic"], models=rows, layer="claude-code-cli")
    assert config.resolve_agent_model("caller") == "claude-fable-5-1"


# --- the pool filter ---------------------------------------------------------

def test_local_only_pool_lands_on_the_local_model(monkeypatch):
    _wire(monkeypatch, ["openai_compatible"])
    assert config.resolve_agent_model("caller") == "qwen3.6-35b-a3b"


def test_pool_provider_without_an_enabled_model_ignores_the_filter(monkeypatch):
    # A groq-only pool serves none of these rows: the filter yields nothing,
    # so it is dropped and the declared default wins.
    _wire(monkeypatch, ["groq"])
    assert config.resolve_agent_model("caller") == "claude-sonnet-5-5"


def test_openai_only_pool_falls_to_the_best_served_tier(monkeypatch):
    # Sonnet is declared but not servable; Terra (tier 3) is the best served.
    _wire(monkeypatch, ["openai"])
    assert config.resolve_agent_model("caller") == "gpt-5.6-terra"


# --- the pin -----------------------------------------------------------------

def test_explicit_default_model_wins(monkeypatch):
    _wire(monkeypatch, ["openai_compatible"], default_model="gpt-5.6-luna")
    assert config.resolve_agent_model("caller") == "gpt-5.6-luna"


def test_pinned_model_is_honoured_even_when_disabled(monkeypatch):
    # The documented asymmetry: `enabled` moves the DEFAULT, never a pin (an
    # explicit human choice, honoured as before).
    _wire(monkeypatch, ["anthropic"], default_model="claude-haiku-4-5",
          models=_disabled("claude-haiku-4-5"))
    assert config.resolve_agent_model("caller", layer="direct-llm") == "claude-haiku-4-5"
