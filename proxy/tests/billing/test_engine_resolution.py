"""Engines resolve per person (``services/engines/engine_resolution.py``):
the table of the order, the order by the person's use, the holdings read,
the registry helpers, the local-only model rule and the fields the agent
routes carry.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from services.engines import engine_resolution as er

CLAUDE, CODEX, DIRECT = "claude-code-cli", "codex-cli", "direct-llm"
ADMIN, MEMBER = "user-admin", "user-viewer"


def _rows(*pairs: tuple[str, str], disabled: tuple[tuple[str, str], ...] = ()) -> tuple[er.ModelRow, ...]:
    return tuple(er.ModelRow(m, p, True) for m, p in pairs) + tuple(
        er.ModelRow(m, p, False) for m, p in disabled)


CATALOG = er.EngineCatalog(
    models={
        CLAUDE: _rows(("opus", "anthropic"), ("sonnet", "anthropic"), disabled=(("fable", "anthropic"),)),
        CODEX: _rows(("sol", "openai"), ("qwen", "ollama")),
        DIRECT: _rows(("sonnet", "anthropic"), ("gpt", "openai"), ("llama", "ollama")),
    },
    local_providers={CLAUDE: frozenset(), CODEX: frozenset({"ollama"}), DIRECT: frozenset({"ollama"})},
)


def _agent(primary: str = CLAUDE, extras: list[str] | None = None, *, pinned: str = "",
           local_only: list[str] | None = None) -> dict:
    return {
        "slug": "demo", "execution_path": primary,
        "execution_paths": json.dumps(extras) if extras else "",
        "default_model": pinned,
        "local_only_engines": json.dumps(local_only) if local_only else "",
    }


def _holds(own: dict | None = None, pool: dict | None = None, seed: str = "",
           resting: dict | None = None, use: dict | None = None, first_shown: dict | None = None,
           connected: dict | None = None) -> er.Holdings:
    return er.Holdings(
        own={k: frozenset(v) for k, v in (own or {}).items()},
        pool={k: frozenset(v) for k, v in (pool or {}).items()},
        resting={k: frozenset(v) for k, v in (resting or {}).items()},
        use=use or {}, first_shown=first_shown or {}, connected=connected or {},
        seed=seed,
    )


ALL = [CODEX, DIRECT]   # the extras of an agent whose primary is Claude


# One row per branch of the order and per edge case: (agent, holdings) ->
# (engine, model, reason, allowed).
TABLE = [
    # A preferred engine stored before the order seeds the first place
    # while the agent's window has no chats.
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CODEX: {"openai"}}, seed=CODEX),
                 (CODEX, "sol", er.REASON_SEED, (CODEX,)), id="the seed held and allowed"),
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CLAUDE: {"anthropic"}, CODEX: {"openai"}}, seed=CODEX),
                 (CODEX, "sol", er.REASON_SEED, (CODEX, CLAUDE)), id="the seed beats the default"),
    pytest.param(_agent(CLAUDE, [DIRECT]), _holds(own={CLAUDE: {"anthropic"}, CODEX: {"openai"}}, seed=CODEX),
                 (CLAUDE, "opus", er.REASON_AGENT_DEFAULT, (CLAUDE,)), id="the seed not allowed: the default"),
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CLAUDE: {"anthropic"}}, seed=CODEX),
                 (CLAUDE, "opus", er.REASON_AGENT_DEFAULT, (CLAUDE,)), id="the seed not held: the default"),
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CLAUDE: {"anthropic"}, CODEX: {"openai"}}, seed=CODEX,
                                             use={"demo": {CLAUDE: 1}}),
                 (CLAUDE, "opus", er.REASON_USE, (CLAUDE, CODEX)), id="the seed gives way once the window has chats"),
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CLAUDE: {"anthropic"}, CODEX: {"openai"}}, seed=CODEX,
                                             use={"other": {CLAUDE: 1}}),
                 (CLAUDE, "opus", er.REASON_USE, (CLAUDE, CODEX)), id="the seed gives way to chats on another agent"),
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CLAUDE: {"anthropic"}, CODEX: {"openai"}}, seed=CODEX,
                                             use={"other": {"acme-cli": 4}}),
                 (CODEX, "sol", er.REASON_SEED, (CODEX, CLAUDE)), id="chats on an engine not listed leave the seed"),
    pytest.param(_agent(CODEX, [CLAUDE]), _holds(own={CLAUDE: {"anthropic"}, CODEX: {"openai"}}),
                 (CODEX, "sol", er.REASON_AGENT_DEFAULT, (CODEX, CLAUDE)), id="no chats: the default"),
    pytest.param(_agent(CODEX, [CLAUDE, DIRECT]), _holds(own={DIRECT: {"anthropic"}}, pool={CLAUDE: {"anthropic"}}),
                 (DIRECT, "sonnet", er.REASON_OWN, (CLAUDE, DIRECT)), id="own account beats a pooled one"),
    pytest.param(_agent(CODEX, [CLAUDE, DIRECT]), _holds(pool={DIRECT: {"anthropic"}}),
                 (DIRECT, "sonnet", er.REASON_PLATFORM, (DIRECT,)), id="the platform's accounts last"),
    pytest.param(_agent(CODEX, [CLAUDE, DIRECT]), _holds(pool={CLAUDE: {"anthropic"}, DIRECT: {"anthropic"}}),
                 (CLAUDE, "opus", er.REASON_PLATFORM, (CLAUDE, DIRECT)), id="pooled engines in the agent's order"),
    pytest.param(_agent(CLAUDE, ALL), _holds(),
                 ("", "", er.REASON_NONE, ()), id="nothing held"),
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CODEX: {"anthropic"}}),
                 ("", "", er.REASON_NONE, ()), id="a provider the engine's models do not use"),
    pytest.param(_agent(CLAUDE, ALL, pinned="sonnet"), _holds(own={CLAUDE: {"anthropic"}}),
                 (CLAUDE, "sonnet", er.REASON_AGENT_DEFAULT, (CLAUDE,)), id="the pin when its provider is held"),
    pytest.param(_agent(CLAUDE, ALL, pinned="fable"), _holds(own={CLAUDE: {"anthropic"}}),
                 (CLAUDE, "fable", er.REASON_AGENT_DEFAULT, (CLAUDE,)), id="a disabled pin still holds"),
    pytest.param(_agent(CODEX, [], pinned="qwen"), _holds(own={CODEX: {"openai"}}),
                 (CODEX, "sol", er.REASON_AGENT_DEFAULT, (CODEX,)), id="a pin of a provider not held is skipped"),
    pytest.param(_agent(CODEX, [], local_only=[CODEX]), _holds(own={CODEX: {"openai"}}),
                 ("", "", er.REASON_NONE, ()), id="local-only refuses a subscription"),
    pytest.param(_agent(CODEX, [], local_only=[CODEX]), _holds(own={CODEX: {"openai"}}, pool={CODEX: {"ollama"}}),
                 (CODEX, "qwen", er.REASON_AGENT_DEFAULT, (CODEX,)), id="local-only takes the local server"),
    pytest.param(_agent(CODEX, [CLAUDE], local_only=[CODEX], pinned="sol"), _holds(own={CODEX: {"openai"}, CLAUDE: {"anthropic"}}),
                 (CLAUDE, "opus", er.REASON_OWN, (CLAUDE,)), id="local-only skips a vendor pin and moves on"),
    pytest.param(_agent(CLAUDE, [CODEX]), _holds(own={CODEX: {"openai"}}, seed=DIRECT),
                 (CODEX, "sol", er.REASON_OWN, (CODEX,)), id="a seed outside the allowlist"),
    # "Holds" in step 2 includes the platform's borrowable accounts: the
    # agent's default reached through a pooled admin key comes before the
    # person's own subscription on another engine (step 2 of the order comes before step 3).
    pytest.param(_agent(CLAUDE, [CODEX]), _holds(own={CODEX: {"openai"}}, pool={CLAUDE: {"anthropic"}}),
                 (CLAUDE, "opus", er.REASON_AGENT_DEFAULT, (CLAUDE, CODEX)),
                 id="the default held through the platform beats an own account elsewhere"),
    # A resting account is still held: its engine stays listed, the pick
    # moves to an awake one; with nothing awake the pick ignores the rest.
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CLAUDE: {"anthropic"}, CODEX: {"openai"}},
                                             resting={CLAUDE: {"anthropic"}}),
                 (CODEX, "sol", er.REASON_OWN, (CLAUDE, CODEX)), id="a resting default: the pick moves on"),
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CLAUDE: {"anthropic"}}, resting={CLAUDE: {"anthropic"}}),
                 (CLAUDE, "opus", er.REASON_AGENT_DEFAULT, (CLAUDE,)), id="every account resting: picked anyway"),
    pytest.param(_agent(DIRECT, []), _holds(own={DIRECT: {"anthropic", "openai"}}, resting={DIRECT: {"anthropic"}}),
                 (DIRECT, "gpt", er.REASON_AGENT_DEFAULT, (DIRECT,)), id="a resting provider: the awake one's model"),
    pytest.param(_agent(CLAUDE, ALL), _holds(own={CLAUDE: {"anthropic"}, CODEX: {"openai"}}, seed=CLAUDE,
                                             resting={CLAUDE: {"anthropic"}}),
                 (CODEX, "sol", er.REASON_SEED, (CLAUDE, CODEX)), id="a resting seed is listed first, the pick skips it"),
]


@pytest.mark.parametrize("agent, holdings, want", TABLE)
def test_the_order(agent, holdings, want):
    got = er.resolve_for_person(agent, holdings, CATALOG)
    assert (got.execution_path, got.model, got.reason, got.allowed) == want
    assert set(got.models_for_me) == set(got.allowed)
    assert all(got.engines_for_me[p] for p in got.allowed)


def test_models_for_me_names_each_allowed_engines_model():
    got = er.resolve_for_person(
        _agent(CLAUDE, ALL, pinned="sonnet"),
        _holds(own={CLAUDE: {"anthropic"}, CODEX: {"ollama"}}, pool={DIRECT: {"openai"}}), CATALOG)
    assert got.models_for_me == {CLAUDE: "sonnet", CODEX: "qwen", DIRECT: "gpt"}


def test_a_resting_engine_is_listed_with_its_model():
    # The picker's per-engine default: the awake provider's model where one
    # is awake, else the held one's.
    got = er.resolve_for_person(
        _agent(CLAUDE, ALL),
        _holds(own={CLAUDE: {"anthropic"}, DIRECT: {"anthropic", "openai"}},
               resting={CLAUDE: {"anthropic"}, DIRECT: {"anthropic"}}), CATALOG)
    assert got.execution_path == DIRECT and got.model == "gpt"
    assert got.engines_for_me == {CLAUDE: True, CODEX: False, DIRECT: True}
    assert got.models_for_me == {CLAUDE: "opus", DIRECT: "gpt"}


BOTH = {CLAUDE: {"anthropic"}, CODEX: {"openai"}}


# The order by the person's own use: (holdings) -> (the pick, the list).
# The agent is "demo" on Claude allowing every engine; the person holds
# Claude and Codex of their own unless the row says otherwise.
USE = [
    pytest.param(_holds(own=BOTH, use={"demo": {CODEX: 7, CLAUDE: 3}}),
                 CODEX, (CODEX, CLAUDE), id="this agent's chats order the list"),
    pytest.param(_holds(own=BOTH, use={"demo": {CODEX: 5, CLAUDE: 4}}),
                 CODEX, (CODEX, CLAUDE), id="no first remembered: the most used"),
    pytest.param(_holds(own=BOTH, use={"demo": {CODEX: 5, CLAUDE: 4}}, first_shown={"demo": CLAUDE}),
                 CLAUDE, (CLAUDE, CODEX), id="five in nine: the first place stays"),
    pytest.param(_holds(own=BOTH, use={"demo": {CODEX: 6, CLAUDE: 4}}, first_shown={"demo": CLAUDE}),
                 CODEX, (CODEX, CLAUDE), id="six in ten: a clear lead takes it"),
    pytest.param(_holds(own={**BOTH, DIRECT: {"anthropic"}}, use={"demo": {CODEX: 5, DIRECT: 4}},
                        first_shown={"demo": CLAUDE}),
                 CODEX, (CODEX, DIRECT, CLAUDE), id="a first with no chat now does not hold"),
    pytest.param(_holds(own={CLAUDE: {"anthropic"}}, use={"demo": {CLAUDE: 1, CODEX: 3}}, first_shown={"demo": CODEX}),
                 CLAUDE, (CLAUDE,), id="a remembered first no longer held"),
    pytest.param(_holds(own=BOTH, use={"demo": {CODEX: 2, CLAUDE: 2}, "other": {CODEX: 3}}),
                 CODEX, (CODEX, CLAUDE), id="a tie broken by the chats on every agent"),
    pytest.param(_holds(own=BOTH, use={"other": {CODEX: 1}}),
                 CODEX, (CODEX, CLAUDE), id="no chats here: the chats on every agent"),
    pytest.param(_holds(own=BOTH, use={"other": {CODEX: 5, CLAUDE: 4}}, first_shown={"demo": CLAUDE}),
                 CLAUDE, (CLAUDE, CODEX), id="no chats here: the remembered first holds too"),
    pytest.param(_holds(own=BOTH, use={"demo": {CODEX: 1, CLAUDE: 1}},
                        connected={CODEX: "2026-01-01T00:00:00+00:00", CLAUDE: "2026-02-01T00:00:00+00:00"}),
                 CODEX, (CODEX, CLAUDE), id="a tie broken by the connection order"),
    pytest.param(_holds(own=BOTH, use={"demo": {CODEX: 1, CLAUDE: 1}}),
                 CLAUDE, (CLAUDE, CODEX), id="a tie in everything: the allowlist"),
    pytest.param(_holds(own=BOTH, connected={CODEX: "2026-01-01T00:00:00+00:00", CLAUDE: "2026-02-01T00:00:00+00:00"}),
                 CLAUDE, (CODEX, CLAUDE), id="no chats at all: the connection order lists, the ladder picks"),
    pytest.param(_holds(own=BOTH, use={"demo": {CODEX: 8, CLAUDE: 2}}, resting={CODEX: {"openai"}}),
                 CLAUDE, (CODEX, CLAUDE), id="a resting first stays listed, the pick moves on"),
    pytest.param(_holds(own={CODEX: {"openai"}}, use={"demo": {CODEX: 3}}, resting={CODEX: {"openai"}}),
                 CODEX, (CODEX,), id="every account resting: picked anyway"),
    pytest.param(_holds(own=BOTH, use={"demo": {DIRECT: 9, CODEX: 1}}),
                 CODEX, (CODEX, CLAUDE), id="chats on an engine not listed do not count"),
    pytest.param(_holds(own={CODEX: {"openai"}}, pool={DIRECT: {"anthropic"}}, use={"other": {DIRECT: 4}}),
                 DIRECT, (DIRECT, CODEX), id="a borrowed engine used elsewhere leads"),
]


@pytest.mark.parametrize("holdings, pick, listed", USE)
def test_the_order_by_use(holdings, pick, listed):
    got = er.resolve_for_person(_agent(CLAUDE, ALL), holdings, CATALOG)
    assert (got.execution_path, got.allowed) == (pick, listed)
    assert got.order_for_me[:len(listed)] == listed
    assert set(got.order_for_me) == {CLAUDE, CODEX, DIRECT}


def test_the_first_to_remember():
    agent = _agent(CLAUDE, ALL)
    fresh = er.resolve_for_person(agent, _holds(own=BOTH, use={"demo": {CODEX: 3}}), CATALOG)
    assert (fresh.first_shown, fresh.first_is_new, fresh.reason) == (CODEX, True, er.REASON_USE)
    kept = er.resolve_for_person(agent, _holds(own=BOTH, use={"demo": {CODEX: 3}}, first_shown={"demo": CODEX}),
                                 CATALOG)
    assert (kept.first_shown, kept.first_is_new) == (CODEX, False)
    cold = er.resolve_for_person(agent, _holds(own=BOTH), CATALOG)
    assert (cold.first_shown, cold.first_is_new, cold.reason) == ("", False, er.REASON_AGENT_DEFAULT)
    seeded = er.resolve_for_person(agent, _holds(own=BOTH, seed=CODEX), CATALOG)
    assert (seeded.first_shown, seeded.first_is_new) == (CODEX, True)


def test_person_order_with_nothing_listed():
    assert er.person_order("demo", [], _holds(use={"demo": {CLAUDE: 3}}, seed=CLAUDE)) == ((), "")


def test_the_engine_map_and_the_order_for_me():
    got = er.resolve_for_person(_agent(CLAUDE, ALL), _holds(own={DIRECT: {"anthropic"}, CODEX: {"openai"}},
                                                           use={"demo": {DIRECT: 4}}), CATALOG)
    assert got.allowed == (DIRECT, CODEX)
    assert got.order_for_me == (DIRECT, CODEX, CLAUDE)
    assert got.engines_for_me == {DIRECT: True, CODEX: True, CLAUDE: False}


class TestEngineForModel:
    def _models(self, monkeypatch, by_layer: dict[str, list[tuple[str, bool]]]):
        from storage.billing import subscription_store
        monkeypatch.setattr(subscription_store, "list_models", lambda layer=None: [
            {"model_id": m, "enabled": on} for m, on in by_layer.get(layer, [])])

    def test_the_first_engine_wins_when_it_offers_the_model(self, monkeypatch):
        self._models(monkeypatch, {CLAUDE: [("sonnet", True)], DIRECT: [("sonnet", True)]})
        agent = _agent(CLAUDE, ALL)
        assert er.engine_for_model(agent, "sonnet", first=DIRECT) == DIRECT
        assert er.engine_for_model(agent, "sonnet", first=CLAUDE) == CLAUDE
        assert er.engine_for_model(agent, "sonnet") == CLAUDE   # the allowlist's order

    def test_the_model_is_found_on_another_allowed_engine(self, monkeypatch):
        self._models(monkeypatch, {CLAUDE: [("opus", True)], CODEX: [("sol", True)]})
        assert er.engine_for_model(_agent(CLAUDE, ALL), "sol", first=CLAUDE) == CODEX

    def test_a_disabled_or_unknown_model_names_no_engine(self, monkeypatch):
        self._models(monkeypatch, {CLAUDE: [("fable", False)]})
        agent = _agent(CLAUDE, ALL)
        assert er.engine_for_model(agent, "fable", first=CLAUDE) == ""
        assert er.engine_for_model(agent, "nope") == ""

    def test_an_engine_outside_the_allowlist_never_answers(self, monkeypatch):
        self._models(monkeypatch, {CODEX: [("sol", True)]})
        assert er.engine_for_model(_agent(CLAUDE, [DIRECT]), "sol", first=CODEX) == ""


def test_model_allowed_on_applies_the_local_only_rule_alone():
    agent = _agent(CODEX, [CLAUDE], local_only=[CODEX])
    assert er.model_allowed_on(agent, CODEX, "qwen", CATALOG)
    assert not er.model_allowed_on(agent, CODEX, "sol", CATALOG)
    assert er.model_allowed_on(agent, CLAUDE, "opus", CATALOG)
    assert er.model_allowed_on(agent, CODEX, "unknown", CATALOG)


def test_the_registry_helpers_follow_the_page_order():
    order = er.engine_order()
    assert set(order) == {CLAUDE, CODEX, DIRECT}
    assert order[0] == CLAUDE
    assert er.every_engine(CLAUDE) == [p for p in order if p != CLAUDE]
    assert er.local_providers_of(CLAUDE) == frozenset()
    assert "ollama" in er.local_providers_of(CODEX)


class TestHoldings:
    @patch("services.engines.engine_resolution.subscription_pool", create=True)
    def test_own_and_pooled_providers_per_engine(self, _unused, temp_db):
        from storage.billing import subscription_store
        subscription_store.add_subscription(CLAUDE, "anthropic", "oauth", owner_sub=MEMBER)
        subscription_store.add_subscription(CODEX, "openai", "api_key", owner_sub=ADMIN,
                                            use_personal=False, contribute_platform=True)
        subscription_store.add_subscription(DIRECT, "anthropic", "oauth", owner_sub=ADMIN,
                                            use_personal=True, contribute_platform=True)
        h = er.person_holdings(MEMBER, [CLAUDE, CODEX, DIRECT])
        assert h.own == {CLAUDE: {"anthropic"}, CODEX: frozenset(), DIRECT: frozenset()}
        # Platform Auth off: nothing borrowed.
        assert h.pool == {CLAUDE: frozenset(), CODEX: frozenset(), DIRECT: frozenset()}
        subscription_store.set_user_allow_platform_auth(MEMBER, True)
        h = er.person_holdings(MEMBER, [CLAUDE, CODEX, DIRECT])
        # The pooled key is borrowable; the admin's OAuth login never is.
        assert h.pool == {CLAUDE: frozenset(), CODEX: {"openai"}, DIRECT: frozenset()}
        assert h.seed == ""
        assert h.connected == {CLAUDE: subscription_store.list_personal(CLAUDE, MEMBER)[0]["created_at"]}

    def test_a_resting_account_is_held_and_marked(self, temp_db):
        from services.engines import subscription_pool
        from storage.billing import subscription_store
        row = subscription_store.add_subscription(CLAUDE, "anthropic", "oauth", owner_sub=MEMBER)
        with patch.object(subscription_pool, "account_resting", side_effect=lambda sid: sid == row["id"]):
            h = er.person_holdings(MEMBER, [CLAUDE])
        assert h.own == {CLAUDE: {"anthropic"}}
        assert h.resting == {CLAUDE: {"anthropic"}}
        # A second account of the provider that is awake keeps it awake.
        subscription_store.add_subscription(CLAUDE, "anthropic", "api_key", owner_sub=MEMBER)
        with patch.object(subscription_pool, "account_resting", side_effect=lambda sid: sid == row["id"]):
            h = er.person_holdings(MEMBER, [CLAUDE])
        assert h.resting == {CLAUDE: frozenset()}

    def test_a_stored_preferred_engine_is_read_as_the_seed(self, temp_db):
        _store_preference(MEMBER, CODEX)
        assert er.person_holdings(MEMBER, [CLAUDE]).seed == CODEX
        assert er.person_holdings("", [CLAUDE]) == er.Holdings()

    def test_the_connection_order_is_the_oldest_own_account(self, temp_db):
        from storage.billing import subscription_store
        first = subscription_store.add_subscription(CODEX, "openai", "oauth", owner_sub=MEMBER)
        subscription_store.add_subscription(CODEX, "openai", "api_key", owner_sub=MEMBER)
        subscription_store.add_subscription(CLAUDE, "anthropic", "oauth", owner_sub=MEMBER)
        _set(f"UPDATE execution_layer_subscriptions SET created_at = '2026-01-01T00:00:00+00:00' "
             f"WHERE id = '{first['id']}'")
        h = er.person_holdings(MEMBER, [CLAUDE, CODEX])
        assert h.connected[CODEX] == "2026-01-01T00:00:00+00:00"
        assert h.connected[CLAUDE] > h.connected[CODEX]

    def test_only_the_persons_own_dashboard_chats_count(self, temp_db):
        from storage import database as task_store
        for i in range(2):
            task_store.create_chat(f"own-codex-{i}", MEMBER, "alpha", execution_path=CODEX)
        task_store.create_chat("own-claude", MEMBER, "alpha", execution_path=CLAUDE)
        task_store.create_chat("own-beta", MEMBER, "beta", execution_path=CLAUDE)
        # Never counted: a task run, a delegated lane, the satellite's own CLI
        # session, a phone call, another person's chat, a chat that never
        # named an engine, a Shared-only agent's shared chat, an old chat.
        task_store.create_chat("task-run-1", MEMBER, "alpha", execution_path=CODEX, source_type="task")
        task_store.create_chat("task-legacy", MEMBER, "alpha", execution_path=CODEX)
        task_store.create_chat("lane", MEMBER, "alpha", execution_path=CODEX, origin="delegated")
        task_store.create_chat("cli", MEMBER, "alpha", execution_path=CODEX, origin="otodock")
        task_store.create_chat("call", MEMBER, "alpha", execution_path=CODEX, source_type="phone")
        task_store.create_chat("theirs", ADMIN, "alpha", execution_path=CODEX)
        task_store.create_chat("no-engine", MEMBER, "alpha")
        task_store.create_chat("shared", "agent::alpha", "alpha", execution_path=CODEX)
        task_store.create_chat("old", MEMBER, "alpha", execution_path=CODEX)
        _set("UPDATE chats SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = 'old'")
        # A chat switched to another engine counts for the one it is on now.
        task_store.create_chat("switched", MEMBER, "alpha", execution_path=CODEX)
        _set("UPDATE chats SET execution_path = 'claude-code-cli' WHERE id = 'switched'")
        assert er.person_holdings(MEMBER, [CLAUDE]).use == {
            "alpha": {CODEX: 2, CLAUDE: 2}, "beta": {CLAUDE: 1}}

    def test_the_use_is_read_once_a_minute(self, temp_db):
        from storage.chat import engine_use
        calls = []
        real = engine_use.own_chat_counts

        def _counting(sub, since):
            calls.append(sub)
            return real(sub, since)
        with patch.object(engine_use, "own_chat_counts", side_effect=_counting):
            er.person_holdings(MEMBER, [CLAUDE])
            er.person_holdings(MEMBER, [CLAUDE])
            assert calls == [MEMBER]
            er.clear_use_cache()
            er.person_holdings(MEMBER, [CLAUDE])
            assert calls == [MEMBER, MEMBER]

    def test_the_use_read_expires_after_a_minute(self, temp_db):
        from storage.chat import engine_use
        clock = [1000.0]
        with patch.object(er.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(engine_use, "own_chat_counts", return_value={}) as read:
            er.person_holdings(MEMBER, [CLAUDE])
            clock[0] += er._USE_TTL - 1
            er.person_holdings(MEMBER, [CLAUDE])
            assert read.call_count == 1
            clock[0] += 2
            er.person_holdings(MEMBER, [CLAUDE])
            assert read.call_count == 2

    def test_the_kept_reads_are_pruned_of_the_expired_past_the_cap(self, temp_db):
        from storage.chat import engine_use
        er.clear_use_cache()
        with er._use_lock:
            for i in range(er._USE_CACHE_PRUNE_AT):
                er._use_cache[f"gone-{i}"] = (0.0, {}, {})
            er._use_cache["live"] = (10_000.0, {}, {})
        with patch.object(er.time, "monotonic", return_value=5_000.0), \
                patch.object(engine_use, "own_chat_counts", return_value={}):
            er.person_holdings(MEMBER, [CLAUDE])
        assert set(er._use_cache) == {"live", MEMBER}

    def test_a_store_error_is_not_kept(self, temp_db):
        import psycopg

        from storage.chat import engine_use
        with patch.object(engine_use, "own_chat_counts", side_effect=psycopg.OperationalError("down")):
            failed = er.person_holdings(MEMBER, [CLAUDE])
        assert (failed.use, failed.use_read) == ({}, False)
        from storage import database as task_store
        task_store.create_chat("later", MEMBER, "alpha", execution_path=CLAUDE)
        assert er.person_holdings(MEMBER, [CLAUDE]).use == {"alpha": {CLAUDE: 1}}

    def test_the_remembered_firsts(self, temp_db):
        from storage.prefs import user_ui_prefs_store
        user_ui_prefs_store.upsert_prefs(MEMBER, {"last_execution_mode": {"alpha": "interactive"}})
        assert er.person_holdings(MEMBER, [CLAUDE]).first_shown == {}
        er.remember_firsts(MEMBER, {"alpha": CODEX})
        # The kept read follows the write: the next fetch does not write again.
        assert er.person_holdings(MEMBER, [CLAUDE]).first_shown == {"alpha": CODEX}
        er.remember_firsts(MEMBER, {"beta": CLAUDE})
        er.clear_use_cache()
        assert er.person_holdings(MEMBER, [CLAUDE]).first_shown == {"alpha": CODEX, "beta": CLAUDE}
        # The rest of the bag is kept.
        assert user_ui_prefs_store.get_prefs(MEMBER)["last_execution_mode"] == {"alpha": "interactive"}


def _set(sql: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute(sql)
        conn.commit()


def _store_preference(sub: str, engine: str) -> None:
    """A preferred engine stored before the order followed use (nothing
    writes the column now)."""
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET preferred_execution_path = %s WHERE sub = %s", (engine, sub))
        conn.commit()


def _cookie(sub: str, email: str, role: str) -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"}


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


@pytest.fixture
def seeded(temp_db):
    """An agent allowing every engine, the builtin models synced, the
    admin with a Claude login and the member with the platform's Codex key
    behind Platform Auth."""
    from core.session.session_manager import get_all_capabilities
    from storage import database as task_store
    from storage.agents import agent_store
    from storage.billing import subscription_store
    for path, caps in get_all_capabilities().items():
        subscription_store.sync_builtin_models(path, caps.get("models", []))
    agent_store.create_agent("demo", "Demo", created_by=ADMIN,
                             execution_paths=er.every_engine(CLAUDE))
    task_store.set_user_agents(MEMBER, ["demo"], ADMIN, agent_roles={"demo": "editor"})
    subscription_store.add_subscription(CLAUDE, "anthropic", "oauth", owner_sub=ADMIN,
                                        use_personal=True, contribute_platform=True)
    subscription_store.add_subscription(CODEX, "openai", "api_key", owner_sub=ADMIN,
                                        use_personal=False, contribute_platform=True)
    subscription_store.set_user_allow_platform_auth(MEMBER, True)
    return "demo"


class TestTheAgentRoutes:
    def test_the_info_route_resolves_for_the_caller(self, client, seeded):
        admin = client.get("/v1/agents/demo/info", headers=_cookie(ADMIN, "a@t", "admin")).json()
        assert admin["engine_for_me"] == CLAUDE
        assert admin["model_for_me"]
        assert admin["engines_for_me"] == {CLAUDE: True, CODEX: False, DIRECT: False}
        assert admin["local_only_engines"] == []
        member = client.get("/v1/agents/demo/info", headers=_cookie(MEMBER, "m@t", "member")).json()
        # The admin's login is never lent; the pooled Codex key is borrowed.
        assert member["engine_for_me"] == CODEX
        assert member["engines_for_me"] == {CLAUDE: False, CODEX: True, DIRECT: False}

    def test_the_list_route_carries_the_same_fields(self, client, seeded):
        rows = client.get("/v1/agents", headers=_cookie(MEMBER, "m@t", "member")).json()["agents"]
        row = next(r for r in rows if r["name"] == "demo")
        assert row["engine_for_me"] == CODEX
        assert row["engines_for_me"][CODEX] is True
        assert row["execution_paths"] == [CLAUDE, CODEX, DIRECT]

    def test_a_stored_preference_seeds_the_fields(self, client, seeded):
        from storage.billing import subscription_store
        subscription_store.add_subscription(CODEX, "openai", "oauth", owner_sub=ADMIN)
        _store_preference(ADMIN, CODEX)
        admin = client.get("/v1/agents/demo/info", headers=_cookie(ADMIN, "a@t", "admin")).json()
        assert admin["engine_for_me"] == CODEX
        assert admin["engine_order_for_me"] == [CODEX, CLAUDE, DIRECT]

    def test_a_failed_use_read_remembers_nothing(self, client, seeded):
        """With the use unread, the seed would lead everywhere: it must not
        be written over the firsts the picker really showed."""
        import psycopg

        from storage.billing import subscription_store
        from storage.chat import engine_use
        from storage.prefs import user_ui_prefs_store
        subscription_store.add_subscription(CODEX, "openai", "oauth", owner_sub=ADMIN)
        _store_preference(ADMIN, CODEX)
        user_ui_prefs_store.remember_engine_firsts(ADMIN, {"demo": CLAUDE})
        headers = _cookie(ADMIN, "a@t", "admin")
        with patch.object(engine_use, "own_chat_counts", side_effect=psycopg.OperationalError("down")):
            info = client.get("/v1/agents/demo/info", headers=headers).json()
        assert info["engine_for_me"] == CODEX
        assert user_ui_prefs_store.engine_firsts(ADMIN) == {"demo": CLAUDE}
        assert er.person_holdings(ADMIN, [CLAUDE]).use_read is True

    def test_a_failed_remember_write_is_logged_and_the_answer_stands(self, client, seeded, caplog):
        import psycopg

        from storage import database as task_store
        from storage.prefs import user_ui_prefs_store
        task_store.create_chat("demo-1", ADMIN, "demo", execution_path=CLAUDE)
        with patch.object(user_ui_prefs_store, "remember_engine_firsts",
                          side_effect=psycopg.OperationalError("down")), caplog.at_level("WARNING"):
            r = client.get("/v1/agents/demo/info", headers=_cookie(ADMIN, "a@t", "admin"))
        assert r.status_code == 200
        assert r.json()["engine_for_me"] == CLAUDE
        assert "could not be remembered" in caplog.text
        # Not kept as remembered: the next fetch tries again.
        assert er.person_holdings(ADMIN, [CLAUDE]).first_shown == {}

    def test_only_the_pickers_fetch_remembers(self, client, seeded):
        """The roster, the runner and a session's resolution read the
        remembered first and never write it; the agent route does."""
        from types import SimpleNamespace

        from services.mcp import dynamic_context
        from services.scheduler import runner
        from storage import database as task_store
        from storage.prefs import user_ui_prefs_store
        task_store.create_chat("demo-1", ADMIN, "demo", execution_path=CLAUDE)
        task = SimpleNamespace(id="t-1", agent="demo", scope="user", created_by=ADMIN,
                               override_execution_path="", override_model="")
        with patch.object(user_ui_prefs_store, "remember_engine_firsts") as remember:
            roster = dynamic_context.build_delegation_roster(["demo"], ADMIN)
            assert runner._task_engine(task)[0] == CLAUDE
            assert er.resolve_session_engine("demo", ADMIN).first_is_new
            remember.assert_not_called()
            assert roster["demo"][0]["path"] == CLAUDE and roster["demo"][0]["is_default"]
            client.get("/v1/agents/demo/info", headers=_cookie(ADMIN, "a@t", "admin"))
        remember.assert_called_once_with(ADMIN, {"demo": CLAUDE})

    def test_ten_chats_on_one_engine_move_it_first_on_that_agent(self, client, seeded):
        from storage import database as task_store
        from storage.agents import agent_store
        from storage.billing import subscription_store
        from storage.prefs import user_ui_prefs_store
        subscription_store.add_subscription(CODEX, "openai", "oauth", owner_sub=ADMIN)
        agent_store.create_agent("other", "Other", created_by=ADMIN, execution_paths=er.every_engine(CLAUDE))
        headers = _cookie(ADMIN, "a@t", "admin")
        before = client.get("/v1/agents/demo/info", headers=headers).json()
        # No chats yet: the agent's default, the engines in connection order.
        assert before["engine_for_me"] == CLAUDE
        assert before["engine_order_for_me"] == [CLAUDE, CODEX, DIRECT]
        assert user_ui_prefs_store.engine_firsts(ADMIN) == {}
        for i in range(2):
            task_store.create_chat(f"other-{i}", ADMIN, "other", execution_path=CLAUDE)
        for i in range(10):
            task_store.create_chat(f"demo-{i}", ADMIN, "demo", execution_path=CODEX)
        er.clear_use_cache()
        rows = {r["name"]: r for r in client.get("/v1/agents", headers=headers).json()["agents"]}
        assert rows["demo"]["engine_for_me"] == CODEX
        assert rows["demo"]["engine_order_for_me"] == [CODEX, CLAUDE, DIRECT]
        assert rows["other"]["engine_for_me"] == CLAUDE
        assert rows["other"]["engine_order_for_me"] == [CLAUDE, CODEX, DIRECT]
        # The picker's fetch remembers each first place once.
        firsts = user_ui_prefs_store.engine_firsts(ADMIN)
        assert (firsts["demo"], firsts["other"]) == (CODEX, CLAUDE)
        with patch.object(user_ui_prefs_store, "remember_engine_firsts") as remember:
            client.get("/v1/agents", headers=headers)
            er.clear_use_cache()
            client.get("/v1/agents/demo/info", headers=headers)
        remember.assert_not_called()

    def test_a_session_token_does_not_remember(self, client, seeded):
        from storage import database as task_store
        from storage.prefs import user_ui_prefs_store
        from tests.conftest import live_session_token
        task_store.create_chat("demo-1", ADMIN, "demo", execution_path=CLAUDE)
        headers = {"Authorization": f"Bearer {live_session_token('sess-y', 'demo', ADMIN)}"}
        info = client.get("/v1/agents/demo/info", headers=headers).json()
        assert info["engine_for_me"] == CLAUDE
        assert user_ui_prefs_store.engine_firsts(ADMIN) == {}

    def test_a_service_principal_gets_no_fields(self, client, seeded):
        from tests.conftest import live_session_token
        headers = {"Authorization": f"Bearer {live_session_token('sess-x', 'demo')}"}
        info = client.get("/v1/agents/demo/info", headers=headers).json()
        assert "engine_for_me" not in info
        assert info["execution_paths"] == [CLAUDE, CODEX, DIRECT]
