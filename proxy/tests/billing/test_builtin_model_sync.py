"""``sync_builtin_models`` writes only what drifted: a second sync of the
same registry changes no row (and commits nothing), a missing built-in is
inserted, a changed price or tier is written back, an admin's edit of a
built-in's price keeps being reverted, a custom row the registry adopts is
promoted, and a built-in the registry dropped is deleted.
"""

from storage.billing import subscription_store as store

LAYER = "claude-code-cli"


def _registry(**over) -> list[dict]:
    base = {
        "value": "claude-test-1", "label": "Claude Test 1", "provider": "anthropic",
        "context_window": 200000, "pricing_input": 3.0, "pricing_output": 15.0,
        "pricing_cache_write": 3.75, "pricing_cache_read": 0.3,
        "supports_reasoning": True, "supports_xhigh": False, "tier": 2, "good_at": "code",
    }
    base.update(over)
    return [{"value": "", "label": "System Default"}, base]


def _row(model_id: str = "claude-test-1") -> dict:
    return next(m for m in store.list_models(layer=LAYER) if m["model_id"] == model_id)


def test_the_second_sync_writes_nothing(temp_db):
    assert store.sync_builtin_models(LAYER, _registry()) == 1
    first = _row()
    assert store.sync_builtin_models(LAYER, _registry()) == 0
    assert _row()["updated_at"] == first["updated_at"]


def test_a_drifted_field_is_written_and_only_that_row(temp_db):
    store.sync_builtin_models(LAYER, _registry())
    assert store.sync_builtin_models(LAYER, _registry(pricing_input=4.0)) == 1
    assert _row()["pricing_input"] == 4.0
    assert store.sync_builtin_models(LAYER, _registry(pricing_input=4.0, tier=1)) == 1
    assert _row()["tier"] == 1
    assert store.sync_builtin_models(LAYER, _registry(pricing_input=4.0, tier=1)) == 0


def test_an_admin_edit_of_a_builtin_price_is_reverted_and_enabled_is_kept(temp_db):
    store.sync_builtin_models(LAYER, _registry())
    row = _row()
    store.update_model(row["id"], pricing_input=9.0, enabled=False)
    assert store.sync_builtin_models(LAYER, _registry()) == 1
    row = _row()
    assert row["pricing_input"] == 3.0 and row["enabled"] is False


def test_adoption_insertion_and_removal_still_write(temp_db):
    store.add_model(LAYER, "claude-test-2", "Mine", provider="anthropic", pricing_input=1.0,
                    pricing_output=1.0, context_window=1000)
    assert _row("claude-test-2")["is_builtin"] is False
    registry = _registry() + [{**_registry()[1], "value": "claude-test-2", "label": "Claude Test 2"}]
    # The custom row is promoted and the other built-in inserted: two writes.
    assert store.sync_builtin_models(LAYER, registry) == 2
    assert _row("claude-test-2")["is_builtin"] is True
    # The registry drops one: the stale built-in is deleted.
    assert store.sync_builtin_models(LAYER, _registry()) == 1
    assert all(m["model_id"] != "claude-test-2" for m in store.list_models(layer=LAYER))
