"""Codex fixes ``model_provider`` in config.toml at session start, so the
dashboard refuses a mid-chat model change that would cross providers (local
endpoint ↔ OpenAI) and tells the user to start a new chat."""

import json
import sys

from tests._paths import PROXY_DIR as _PROXY_DIR

if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

import pytest

import config
import ws.dashboard  # noqa: F401  — the chat module is imported through the WS package
from services.engines import subscription_pool
from storage.billing import subscription_store
from ws.dashboard_chat import _provider_switch_blocker


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
    reason = _provider_switch_blocker("s1", "qwen3.6-35b-a3b", layer="codex-cli")
    assert "Start a new chat" in reason
    assert "OpenAI" in reason and "OpenAI-compatible server" in reason  # the descriptor's labels


def test_same_provider_switch_is_allowed(monkeypatch):
    _wire(monkeypatch, "openai_compatible")
    assert _provider_switch_blocker("s1", "qwen3.6-35b-a3b", layer="codex-cli") == ""


def test_unbound_session_is_not_guarded(monkeypatch):
    _wire(monkeypatch, "openai", sub_id=None)
    assert _provider_switch_blocker("s1", "qwen3.6-35b-a3b", layer="codex-cli") == ""
    _wire(monkeypatch, "openai", sub_id="default")
    assert _provider_switch_blocker("s1", "qwen3.6-35b-a3b", layer="codex-cli") == ""


@pytest.mark.parametrize("recorded,local,changed", [
    ("openai", False, False), ("oto_local", True, False),
    ("openai", True, True), ("oto_local", False, True), ("", True, False),
])
def test_a_terminal_resume_across_a_provider_change_is_spotted(tmp_path, recorded, local, changed):
    """Codex 0.166 refuses to resume a thread recorded with another model
    provider and its TUI exits: the spawn reads the rollout's provider and
    starts fresh instead (reseeded from the chat's history)."""
    from core.layers.codex.layer import _resume_provider_changed
    day = tmp_path / "sessions" / "2026" / "10" / "30"
    day.mkdir(parents=True)
    meta = {"type": "session_meta", "payload": {"model_provider": recorded}} if recorded else {}
    (day / "rollout-2026-10-30T01-00-00-tid-1.jsonl").write_text(json.dumps(meta) + "\n")
    assert _resume_provider_changed(str(tmp_path), "tid-1", local) is changed
    assert _resume_provider_changed(str(tmp_path), "tid-gone", local) is False
