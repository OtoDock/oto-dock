"""Retired-model succession (2026-09-24, the CLI-upgrade lane).

``config.MODEL_SUCCESSORS`` is walked at boot by
``subscription_store.remap_retired_models``: every persisted pin of a retired
id lands on the END of its chain whatever the dict's order, and an id an admin
has re-added as a custom row is left alone. The pins the walk never sees
resolve through ``config.successor_model`` at read time.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/billing/test_model_successors.py -q
"""

import pytest

import config as app_config
from storage.agents import agent_store
from storage.billing import subscription_store


def test_successor_model_follows_the_chain_to_its_end():
    # Opus 4.8 → Opus 5 → Opus 5.5: the intermediate id is retired too.
    assert app_config.successor_model("claude-opus-4-8[1m]") == "claude-opus-5-5"
    assert app_config.successor_model("claude-opus-5") == "claude-opus-5-5"
    assert app_config.successor_model("gpt-5.6-sol") == "gpt-6-sol"
    assert app_config.successor_model("gpt-5.6-luna") == "gpt-6-luna"
    # A current id, a custom id and nothing pass through unchanged.
    assert app_config.successor_model("claude-opus-5-5") == "claude-opus-5-5"
    assert app_config.successor_model("my-custom") == "my-custom"
    assert app_config.successor_model("") == ""


def test_successor_model_is_cycle_safe(monkeypatch):
    monkeypatch.setattr(app_config, "MODEL_SUCCESSORS", {"a": "b", "b": "a"})
    assert app_config.successor_model("a") in {"a", "b"}


def test_every_successor_is_a_current_builtin_and_every_retired_id_is_not():
    for old, new in app_config.MODEL_SUCCESSORS.items():
        end = app_config.successor_model(old)
        assert end in app_config.MODEL_REGISTRY, (old, end)
        assert old not in app_config.MODEL_REGISTRY, old
        assert new != old


def _pin(slug: str) -> str:
    agent_store._invalidate_cache()  # the walk writes raw SQL under the cache
    return (agent_store.get_agent(slug) or {}).get("default_model") or ""


@pytest.mark.usefixtures("temp_db")
class TestBootWalk:
    def test_a_chained_pin_lands_on_the_end_whatever_the_dict_order(self):
        agent_store.create_agent("walk-a", "Walk A", execution_path="claude-code-cli",
                                 default_model="claude-opus-4-8[1m]")
        agent_store.create_agent("walk-b", "Walk B", execution_path="claude-code-cli",
                                 default_model="claude-opus-5")
        # The chain's later link listed FIRST — insertion order must not matter.
        remapped = subscription_store.remap_retired_models({
            "claude-opus-5": "claude-opus-5-5",
            "claude-opus-4-8[1m]": "claude-opus-5",
        })
        assert _pin("walk-a") == "claude-opus-5-5"
        assert _pin("walk-b") == "claude-opus-5-5"
        assert remapped["claude-opus-5"] >= 1 and remapped["claude-opus-4-8[1m]"] >= 1

    def test_a_retired_id_an_admin_re_added_keeps_its_pins(self):
        subscription_store.add_model("claude-code-cli", "claude-opus-5", "Opus 5 (kept)",
                                     provider="anthropic", is_builtin=False)
        agent_store.create_agent("walk-c", "Walk C", execution_path="claude-code-cli",
                                 default_model="claude-opus-5")
        assert subscription_store.custom_model_exists("claude-opus-5")
        assert not subscription_store.custom_model_exists("claude-opus-5-5")
        remapped = subscription_store.remap_retired_models({"claude-opus-5": "claude-opus-5-5"})
        assert "claude-opus-5" not in remapped  # skipped, not zero rows
        assert _pin("walk-c") == "claude-opus-5"

    def test_the_walk_is_idempotent_and_survives_one_bad_entry(self, monkeypatch):
        agent_store.create_agent("walk-d", "Walk D", execution_path="codex-cli",
                                 default_model="gpt-5.6-luna")
        real = subscription_store.remap_retired_model

        def _boom(old_id, new_id):
            if old_id == "explodes":
                raise RuntimeError("db hiccup")
            return real(old_id, new_id)

        monkeypatch.setattr(subscription_store, "remap_retired_model", _boom)
        first = subscription_store.remap_retired_models({"explodes": "x", "gpt-5.6-luna": "gpt-6-luna"})
        second = subscription_store.remap_retired_models({"gpt-5.6-luna": "gpt-6-luna"})
        assert first == {"gpt-5.6-luna": 1} and second == {"gpt-5.6-luna": 0}
        assert _pin("walk-d") == "gpt-6-luna"
