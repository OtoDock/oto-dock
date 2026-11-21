"""Codex's plan capability and the translation of a compaction.

Plan mode needs the capability flag and the mode in ``permission_modes``;
a compaction arrives as the ``contextCompaction`` item (or the older
``thread/compacted``) and the next token usage carries the compacted size.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/session/test_codex_steer.py -q
"""


def test_codex_capabilities_expose_plan_mode():
    """Plan mode needs BOTH the capability flag (FE shows the toggle) AND "plan"
    in permission_modes (the backend mode-change validator rejects anything not
    listed). Missing the latter silently drops mode_change('plan') for codex."""
    from core.layers.codex.layer import _CODEX_CAPABILITIES
    assert _CODEX_CAPABILITIES.supports_plan_mode is True
    assert "plan" in _CODEX_CAPABILITIES.permission_modes


class TestCompactedTranslation:
    def test_thread_compacted_is_first_class_context_compact(self):
        from core.events.common_events import CONTEXT_COMPACT
        from core.layers.codex.events import CodexEvent
        from core.layers.codex.translator import CodexEventTranslator

        tr = CodexEventTranslator(model="m")
        events = tr.translate(CodexEvent("thread/compacted", {}))
        assert [e.type for e in events] == [CONTEXT_COMPACT]
        assert events[0].data["phase"] == "completed"
        assert events[0].data["trigger"] == "auto"

    def test_context_compaction_item_is_first_class(self):
        # The canonical v2 signal — auto-compaction arrives as this item.
        from core.events.common_events import CONTEXT_COMPACT
        from core.layers.codex.events import CodexEvent
        from core.layers.codex.translator import CodexEventTranslator

        tr = CodexEventTranslator(model="m")
        events = tr.translate(CodexEvent("item/completed", {
            "item": {"id": "i9", "type": "contextCompaction"},
        }))
        assert [e.type for e in events] == [CONTEXT_COMPACT]
        assert events[0].data["phase"] == "completed"

    def test_post_compaction_token_usage_moves_the_gauge(self):
        # A compaction at/after turn end has no follow-up METADATA frame, so
        # the NEXT tokenUsage (the compacted prompt size) must reach the
        # context gauge as a one-shot "usage" phase — no chip, no waiting for
        # the next turn to complete.
        from core.events.common_events import CONTEXT_COMPACT
        from core.layers.codex.events import CodexEvent
        from core.layers.codex.translator import CodexEventTranslator

        tr = CodexEventTranslator(model="m")
        tr.translate(CodexEvent("item/completed", {
            "item": {"id": "i9", "type": "contextCompaction"},
        }))
        events = tr.translate(CodexEvent("thread/tokenUsage/updated", {
            "tokenUsage": {
                "last": {"inputTokens": 8123, "cachedInputTokens": 0,
                         "outputTokens": 42},
                "modelContextWindow": 258400,
            },
        }))
        assert [e.type for e in events] == [CONTEXT_COMPACT]
        assert events[0].data == {
            "phase": "usage", "post_tokens": 8123, "context_max": 258400,
        }
        # One-shot: the next tokenUsage is bookkeeping only, no event.
        again = tr.translate(CodexEvent("thread/tokenUsage/updated", {
            "tokenUsage": {"last": {"inputTokens": 9000},
                           "modelContextWindow": 258400},
        }))
        assert again == []

    def test_legacy_compacted_also_arms_the_gauge_update(self):
        from core.events.common_events import CONTEXT_COMPACT
        from core.layers.codex.events import CodexEvent
        from core.layers.codex.translator import CodexEventTranslator

        tr = CodexEventTranslator(model="m")
        tr.translate(CodexEvent("thread/compacted", {}))
        events = tr.translate(CodexEvent("thread/tokenUsage/updated", {
            "tokenUsage": {"last": {"inputTokens": 5555},
                           "modelContextWindow": 100000},
        }))
        assert [e.type for e in events] == [CONTEXT_COMPACT]
        assert events[0].data["phase"] == "usage"
        assert events[0].data["post_tokens"] == 5555
