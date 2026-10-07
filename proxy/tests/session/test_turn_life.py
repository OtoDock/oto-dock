"""The life check an engine-side loop asks on every silent slice: the
ceiling depends on an open tool, and a prompt waiting on a person, running
background work or a recent hook keep a silent turn alive."""

import time
import uuid


import config
from core.events import turn_life
from core.session import session_state


def _sid() -> str:
    return f"sess-{uuid.uuid4().hex[:8]}"


def test_the_ceiling_follows_the_open_tool(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 600)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    assert turn_life.ceiling_for(False) == 600.0
    assert turn_life.ceiling_for(True) == 7200.0


def test_silence_under_the_ceiling_never_ends(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 600)
    assert turn_life.ends(_sid(), tools_open=False, silent_for=599.0) is False
    assert turn_life.ends(_sid(), tools_open=False, silent_for=601.0) is True


def test_an_open_tool_takes_the_turn_ceiling(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 600)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    sid = _sid()
    assert turn_life.ends(sid, tools_open=True, silent_for=3600.0) is False
    assert turn_life.ends(sid, tools_open=True, silent_for=7201.0) is True


def test_a_pending_prompt_keeps_the_turn(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    sid = _sid()
    monkeypatch.setattr(session_state, "has_pending_prompt", lambda s: s == sid)
    assert turn_life.spared(sid, silent_for=1000.0) == "a prompt waiting on a person"
    assert turn_life.ends(sid, tools_open=False, silent_for=1000.0) is False
    assert turn_life.ends(_sid(), tools_open=False, silent_for=1000.0) is True


def test_running_background_work_keeps_the_turn(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    sid = _sid()
    from core.events.bg_command_state import get_bg_command_registry
    reg = get_bg_command_registry(sid)
    reg.register_spawn("task-1", "tool-1", label="sleep 900")
    assert "background command" in turn_life.spared(sid, silent_for=1000.0)
    assert turn_life.ends(sid, tools_open=False, silent_for=1000.0) is False
    reg.mark_done("task-1")
    assert turn_life.ends(sid, tools_open=False, silent_for=1000.0) is True


def test_a_recent_hook_keeps_the_turn_an_old_one_does_not(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    sid = _sid()
    session_state.record_hook_activity(sid)
    assert turn_life.spared(sid, silent_for=1000.0) == "recent hook activity"
    session_state._session_hook_activity[sid] = time.monotonic() - 61.0
    assert turn_life.spared(sid, silent_for=1000.0) == ""
    session_state._session_hook_activity.pop(sid, None)


def test_a_task_turn_takes_the_turn_ceiling(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 600)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    sid = _sid()
    assert turn_life.ceiling_for(False, task=True) == 7200.0
    assert turn_life.ends(sid, tools_open=False, silent_for=3600.0, task=True) is False
    assert turn_life.ends(sid, tools_open=False, silent_for=7201.0, task=True) is True


def test_background_work_spares_a_silent_turn_up_to_the_turn_ceiling(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 3600)
    sid = _sid()
    from core.events.bg_command_state import get_bg_command_registry
    get_bg_command_registry(sid).register_spawn("task-1", "tool-1", label="npm run dev")
    assert turn_life.ends(sid, tools_open=False, silent_for=3000.0) is False
    # A server that never ends no longer keeps a hung turn past the ceiling.
    assert turn_life.ends(sid, tools_open=False, silent_for=3601.0) is True
