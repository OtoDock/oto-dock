"""Memory store tests (settings + per-agent toggles only).

Memory CONTENT lives in markdown files; ``memory_store`` only carries the
platform feature toggles, the prompt-injection inline budget, and the
turn-counter nudge knob. Content-level tests live in ``test_memory_file.py``
and ``test_memory_api.py``.
"""

from __future__ import annotations

import pytest

from storage.agents import agent_store
from storage.agents import memory_store


# ---------------------------------------------------------------------------
# Singleton settings row
# ---------------------------------------------------------------------------

def test_settings_singleton_initialized(temp_db):
    settings = memory_store.get_settings()
    assert settings["user_memory_enabled"] is True
    assert settings["agent_memory_enabled"] is True
    assert settings["inline_budget_bytes"] == 8192
    assert settings["nudge_turns"] == 10
    # Dropped v3 dream/gate knobs — offline consolidation is gone.
    for gone in (
        "dream_execution_path", "dream_model", "gate_min_pending_bytes",
        "gate_min_interval_hours", "throttle_min_minutes",
        "gate_min_new_events", "audit_retention_days",
    ):
        assert gone not in settings


def test_update_settings_roundtrip(temp_db):
    memory_store.update_settings(
        inline_budget_bytes=16384,
        nudge_turns=0,
        agent_memory_enabled=False,
    )
    s = memory_store.get_settings()
    assert s["inline_budget_bytes"] == 16384
    assert s["nudge_turns"] == 0
    assert s["agent_memory_enabled"] is False


def test_update_settings_unknown_field_ignored(temp_db):
    """Unknown keys silently dropped — they don't reach the SET clause.
    Crucially the dead v3 dream knobs no longer round-trip."""
    memory_store.update_settings(
        bogus_field=42, dream_model="gpt-x", inline_budget_bytes=2048,
    )
    s = memory_store.get_settings()
    assert s["inline_budget_bytes"] == 2048
    assert "bogus_field" not in s
    assert "dream_model" not in s


def test_update_after_truncate_lazy_upserts_row(temp_db):
    """TRUNCATE wipes the singleton; the next UPDATE must still land."""
    from storage.pg import get_conn
    with get_conn() as c:
        c.execute("TRUNCATE memory_settings")
        c.commit()
    memory_store.update_settings(nudge_turns=25)
    s = memory_store.get_settings()
    assert s["nudge_turns"] == 25


# ---------------------------------------------------------------------------
# Per-agent toggles
# ---------------------------------------------------------------------------

def test_agent_toggle_defaults_then_override(temp_db):
    agent_store.create_agent("acme", "Acme")
    t = memory_store.get_agent_toggles("acme")
    assert t["user_memory_enabled"] is True
    memory_store.set_agent_toggle("acme", "user_memory_enabled", False)
    t = memory_store.get_agent_toggles("acme")
    assert t["user_memory_enabled"] is False


def test_set_agent_toggle_unknown_key_raises(temp_db):
    with pytest.raises(ValueError):
        memory_store.set_agent_toggle("acme", "nope", True)
    # The dead dream knobs are no longer settable either.
    with pytest.raises(ValueError):
        memory_store.set_agent_toggle("acme", "dream_model", "x")


def test_get_agent_toggles_returns_defaults_for_unknown(temp_db):
    """An agent without an explicit row inherits ON."""
    t = memory_store.get_agent_toggles("unknown-agent")
    assert t["user_memory_enabled"] is True
    assert t["agent_memory_enabled"] is True
    assert "dream_execution_path" not in t
