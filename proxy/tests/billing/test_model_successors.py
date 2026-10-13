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
    # 5.6 Sol → GPT-6 Sol → GPT-6.1 Sol; Sonnet 5 → Sonnet 5.5.
    assert app_config.successor_model("gpt-5.6-sol") == "gpt-6.1-sol"
    assert app_config.successor_model("gpt-6-sol") == "gpt-6.1-sol"
    assert app_config.successor_model("gpt-5.6-luna") == "gpt-6-luna"
    assert app_config.successor_model("claude-sonnet-5") == "claude-sonnet-5-5"
    assert app_config.successor_model("claude-haiku-4-5") == "claude-haiku-5-5"
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


def test_haiku_5_5_succeeds_haiku_4_5_losslessly():
    # Haiku 4.5's tuple was (1.0, 5.0, 1.25, 0.10) on direct-llm with a 200K
    # window: the successor is cheaper on every element even at its upper
    # tier, serves both engines, and keeps the retired row's provider.
    e = app_config.MODEL_REGISTRY["claude-haiku-5-5"]
    assert e["provider"] == "anthropic" and e["tier"] == 4
    assert set(e["layers"]) >= {"claude-code-cli", "direct-llm"}
    assert e["context_window"] >= 200_000
    over, in_m, out_m = app_config.get_model_long_context("claude-haiku-5-5")
    assert (over, in_m, out_m) == (100_000, 5.0, 5.0)
    p_in, p_out, p_cw, p_cr = e["pricing"]
    assert (p_in * in_m, p_out * out_m, p_cw * in_m, p_cr * in_m) <= (1.0, 5.0, 1.25, 0.10)
    assert e["supports_reasoning"] and e["supports_xhigh"] and e["server_tools"]
    # No other builtin is priced by prompt length.
    assert app_config.get_model_long_context("claude-sonnet-5-5") == (0, 1.0, 1.0)
    assert app_config.get_model_long_context("no-such-model") == (0, 1.0, 1.0)


def test_price_usage_applies_the_long_context_tier_to_every_token():
    cost = app_config.price_usage
    # Under the line: the base tuple ($0.10 / $0.50 / $0.125 / $0.01 per 1M).
    assert cost("claude-haiku-5-5", input_tokens=50_000, output_tokens=1_000,
                cache_write_tokens=20_000, cache_read_tokens=30_000) == (
        50_000 * 0.10 + 1_000 * 0.50 + 20_000 * 0.125 + 30_000 * 0.01) / 1_000_000
    # Past it (input + cache reads + cache writes = 100,001): x5 on every element.
    assert cost("claude-haiku-5-5", input_tokens=50_001, output_tokens=1_000,
                cache_write_tokens=20_000, cache_read_tokens=30_000) == (
        50_001 * 0.50 + 1_000 * 2.50 + 20_000 * 0.625 + 30_000 * 0.05) / 1_000_000
    # Exactly on the line is under it; a model without a tier never multiplies.
    assert cost("claude-haiku-5-5", input_tokens=100_000) == 100_000 * 0.10 / 1_000_000
    assert cost("claude-sonnet-5-5", input_tokens=900_000, output_tokens=10) == (
        900_000 * 2.0 + 10 * 10.0) / 1_000_000


def test_the_gpt_rows_carry_their_window_and_the_272k_tier():
    # OpenAI bills the WHOLE request at 2x input / 1.5x output once its input
    # passes 272K; the rows carried that line as their window until the tier
    # could be priced, now they carry the vendor's 1.05M window and the tier.
    for model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-terra", "gpt-6-luna"):
        assert app_config.MODEL_REGISTRY[model]["context_window"] == 1_050_000, model
        assert app_config.get_model_long_context(model) == (272_000, 2.0, 1.5), model
    cost = app_config.price_usage
    # Terra ($2 / $12 / $2.50 / $0.20): under the line the base tuple, one token
    # past it every rate at its multiplier, output at 1.5x.
    assert cost("gpt-5.6-terra", input_tokens=200_000, output_tokens=1_000,
                cache_read_tokens=72_000) == pytest.approx(
        (200_000 * 2.0 + 1_000 * 12.0 + 72_000 * 0.20) / 1_000_000)
    assert cost("gpt-5.6-terra", input_tokens=200_001, output_tokens=1_000,
                cache_read_tokens=72_000) == pytest.approx(
        (200_001 * 4.0 + 1_000 * 18.0 + 72_000 * 0.40) / 1_000_000)


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

    def test_the_walk_refreshes_the_agent_cache_it_wrote_under(self):
        # The agents API answers from an in-process cache; the walk writes
        # the rows with SQL. A boot that read the agents before the walk (the
        # scheduler's reconcile does) kept serving the retired id, and the
        # dashboard seeded new chats from the first offered model instead
        # (seen on T1 at the 1.7.1 bump: Fable 5.1 for a Sonnet default).
        agent_store.create_agent("walk-e", "Walk E", execution_path="claude-code-cli",
                                 default_model="claude-opus-5")
        assert (agent_store.get_agent("walk-e") or {}).get("default_model") == "claude-opus-5"  # cached
        subscription_store.remap_retired_models({"claude-opus-5": "claude-opus-5-5"})
        assert (agent_store.get_agent("walk-e") or {}).get("default_model") == "claude-opus-5-5"

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


def _raw_pin(slug: str, model: str) -> None:
    """A pin written after the boot walk (an old MCP call, a restore): the
    walk runs at boot only, so the row keeps the retired id."""
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE agents SET default_model = %s WHERE slug = %s", (model, slug))
        conn.commit()
    agent_store._invalidate_cache()


class TestLiveModelId:
    def test_a_current_or_unknown_id_is_itself_without_a_lookup(self, monkeypatch):
        def _no_lookup(model_id):
            raise AssertionError("no row lookup for an id that is not retired")
        monkeypatch.setattr(subscription_store, "custom_model_exists", _no_lookup)
        assert app_config.live_model_id("claude-opus-5-5") == "claude-opus-5-5"
        assert app_config.live_model_id("my-custom") == "my-custom"
        assert app_config.live_model_id("") == ""

    def test_a_retired_id_is_its_successor_unless_an_admin_kept_it(self, monkeypatch):
        monkeypatch.setattr(subscription_store, "custom_model_exists", lambda m: False)
        assert app_config.live_model_id("claude-opus-4-8[1m]") == "claude-opus-5-5"
        assert app_config.live_model_id("gpt-6-sol") == "gpt-6.1-sol"
        monkeypatch.setattr(subscription_store, "custom_model_exists", lambda m: m == "claude-opus-5")
        assert app_config.live_model_id("claude-opus-5") == "claude-opus-5"

    def test_a_lookup_error_answers_the_successor(self, monkeypatch):
        def _boom(model_id):
            raise RuntimeError("db down")
        monkeypatch.setattr(subscription_store, "custom_model_exists", _boom)
        assert app_config.live_model_id("claude-sonnet-5") == "claude-sonnet-5-5"


@pytest.mark.usefixtures("temp_db")
class TestReadTimeSuccession:
    @pytest.fixture(autouse=True)
    def _builtins(self, temp_db):
        # The builtin rows, synced the way boot syncs them: a layer-aware
        # resolution reads them.
        from core.session.session_manager import get_all_capabilities
        for path, caps in get_all_capabilities().items():
            subscription_store.sync_builtin_models(path, caps.get("models", []))

    def test_an_agent_pin_written_after_the_walk_runs_on_its_successor(self):
        agent_store.create_agent("late-a", "Late A", execution_path="claude-code-cli")
        _raw_pin("late-a", "claude-opus-5")
        assert app_config.resolve_agent_model("late-a") == "claude-opus-5-5"

    def test_a_run_and_its_chat_row_name_the_same_model(self):
        # The runner stamps the chat row (billing reads it) and the config
        # builder starts the session: ONE rule for both.
        agent_store.create_agent("late-b", "Late B", execution_path="codex-cli",
                                 default_model="gpt-6.1-sol")
        assert app_config.resolve_run_model("late-b", "gpt-5.6-luna") == "gpt-6-luna"
        assert app_config.resolve_run_model("late-b", None) == app_config.resolve_run_model(
            "late-b", None, layer="codex-cli")
        assert app_config.resolve_run_model("late-b", "") == "gpt-6.1-sol"
