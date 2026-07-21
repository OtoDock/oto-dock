"""Tests for the execution-mode resolver + kill-switch."""
import pytest

from core import execution_mode as em


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(em, "is_interactive_enabled", lambda: True)


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(em, "is_interactive_enabled", lambda: False)


def test_meetings_always_headless_even_when_enabled(enabled):
    # Rule 1 overrides everything, including an interactive per-agent default.
    assert em.resolve_execution_mode(
        agent_default="interactive", chat_override="interactive", is_meeting=True,
    ) == em.HEADLESS


def test_kill_switch_forces_headless(disabled):
    # Rule 2: disabled → everything falls back to -p regardless of preferences.
    assert em.resolve_execution_mode(agent_default="interactive") == em.HEADLESS
    assert em.resolve_execution_mode(chat_override="interactive") == em.HEADLESS


def test_chat_override_wins_over_agent_default(enabled):
    assert em.resolve_execution_mode(
        agent_default="-p", chat_override="interactive",
    ) == em.INTERACTIVE
    assert em.resolve_execution_mode(
        agent_default="interactive", chat_override="-p",
    ) == em.HEADLESS


def test_agent_default_used_when_no_chat_override(enabled):
    assert em.resolve_execution_mode(agent_default="interactive") == em.INTERACTIVE
    assert em.resolve_execution_mode(agent_default="-p") == em.HEADLESS


def test_platform_default_is_headless(enabled):
    # Nothing set → platform default -p.
    assert em.resolve_execution_mode() == em.HEADLESS


def test_invalid_values_fall_through(enabled):
    # Garbage values are treated as unset.
    assert em.resolve_execution_mode(
        agent_default="garbage", chat_override="nonsense",
    ) == em.HEADLESS


def test_is_interactive_wrapper(enabled):
    assert em.is_interactive(agent_default="interactive") is True
    assert em.is_interactive(agent_default="interactive", is_meeting=True) is False


def test_parse_enabled_truth_table():
    # Shared with the admin settings API (GET mirrors the same parse).
    for on in ("1", "true", "yes", "on", "True", " ON "):
        assert em.parse_enabled(on) is True
    for off in ("", "0", "false", "no", "off", None, "garbage"):
        assert em.parse_enabled(off) is False


def test_kill_switch_reads_platform_setting(monkeypatch):
    monkeypatch.setattr(
        "storage.database.get_platform_setting", lambda *a, **k: "1",
    )
    assert em.is_interactive_enabled() is True

def test_kill_switch_unset_defaults_on(monkeypatch):
    # R1.5 flip: an install that never touched the setting has interactive ON;
    # an explicit "0" (an admin who turned it off) stays off.
    from storage import database
    store = {}
    monkeypatch.setattr(database, "get_platform_setting",
                       lambda key: store.get(key))
    assert em.is_interactive_enabled() is True
    store[em.KILL_SWITCH_KEY] = "0"
    assert em.is_interactive_enabled() is False
    store[em.KILL_SWITCH_KEY] = "1"
    assert em.is_interactive_enabled() is True


def test_effective_enabled_shared_semantics():
    assert em.effective_enabled("") is True       # unset → shipped default
    assert em.effective_enabled(None) is True
    assert em.effective_enabled("0") is False
    assert em.effective_enabled("false") is False
    assert em.effective_enabled("1") is True
