"""Unit tests for SettleController state decisions.

Settle decisions are now driven by the per-session SubagentRegistry
(pending subagents) + hook activity — not the removed job_done / bg counters.
"""

from __future__ import annotations

import time

from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.session.session_state import get_subagent_registry, reset_subagent_registry

_SID = "sid"


def _mk(settle_after_result: float = 30.0, *, hook_activity=None, pending=0):
    """Build a translator + SettleController with a pluggable hook-activity clock.

    `pending` registers N subagents in the session's registry (not completed)
    so the controller sees them as still-running background work.
    """
    # Full reset for test isolation. Production reset() deliberately PRESERVES
    # still-pending bg subagents (talk-while-bg: a follow-up turn must not wipe a
    # running bg agent), so a prior test's pending would otherwise bleed into this
    # one — drain leftovers, then reset, so every test starts with a clean registry.
    _r = get_subagent_registry(_SID)
    for _tid in list(_r.spawned - _r.completed):
        _r.mark_done(_tid)
    reset_subagent_registry(_SID)
    t = ClaudeCLIEventTranslator(_SID)
    reg = get_subagent_registry(_SID)
    for i in range(pending):
        reg.register_spawn(f"task{i}", f"tu{i}")
    hook_activity = hook_activity or (lambda sid: None)
    s = SettleController(_SID, settle_after_result, t, get_hook_activity=hook_activity)
    return t, s


def test_interactive_done_when_no_settle():
    _, s = _mk(settle_after_result=0)
    assert s.is_interactive_done() is True


def test_effective_timeout_pre_settle_is_60s():
    _, s = _mk()
    assert s.effective_timeout() == 60.0


def test_effective_timeout_settle_no_agents_fast_settles():
    # No subagents → 5s grace (fast-settle), not the full base timeout.
    t, s = _mk(settle_after_result=30)
    s.enter_settle()
    assert get_subagent_registry(_SID).has_pending is False
    assert s.effective_timeout() == 5.0


def test_effective_timeout_settle_with_bg_pending_uses_5s():
    _, s = _mk(settle_after_result=30, pending=2)
    s.enter_settle()
    assert s.effective_timeout() == 5.0


def test_should_exit_on_silence_returns_false_pre_settle():
    _, s = _mk()
    assert s.should_exit_on_silence(60.0) is False


def test_should_exit_on_silence_exits_when_no_pending_agents():
    # Nothing pending → turn is over regardless of hook activity.
    _, s = _mk()
    s.enter_settle()
    assert s.should_exit_on_silence(5.0) is True


def test_should_exit_on_silence_keeps_waiting_when_pending_and_hooks_recent():
    now = time.monotonic()
    # Pending agent + a hook fired 1s ago (within the 10s window) → keep waiting.
    _, s = _mk(settle_after_result=30, hook_activity=lambda sid: now - 1.0, pending=1)
    s.enter_settle()
    assert s.should_exit_on_silence(10.0) is False


def test_should_exit_on_silence_keeps_waiting_while_pending_even_if_hooks_silent():
    # Hook SILENCE must NOT be read as "done": a sleeping/slow subagent fires no
    # hooks, so settling here would exit before it finishes (and let a delegate
    # report back missing its results). Trust the SubagentStop hook — keep waiting.
    now = time.monotonic()
    _, s = _mk(settle_after_result=30, hook_activity=lambda sid: now - 60.0, pending=1)
    s.enter_settle()
    assert s.should_exit_on_silence(10.0) is False


def test_should_exit_on_silence_keeps_waiting_while_pending_with_no_hook_activity():
    _, s = _mk(settle_after_result=30, hook_activity=lambda sid: None, pending=1)
    s.enter_settle()
    assert s.should_exit_on_silence(10.0) is False


def test_should_exit_on_silence_settles_at_pending_ceiling():
    # Backstop for a genuinely lost SubagentStop: after the ceiling, settle even
    # with subagents still pending so a dropped hook can't hold the lock forever.
    from core.layers.cli.settle import _SETTLE_PENDING_CEILING
    _, s = _mk(settle_after_result=30, pending=1)
    s.enter_settle()
    s._settle_start = time.monotonic() - (_SETTLE_PENDING_CEILING + 5)
    assert s.should_exit_on_silence(5.0) is True


def test_enter_settle_triggers_translator_reset_for_settle():
    t, s = _mk(settle_after_result=10)
    t.agents_spawned = 5
    t._tool_inputs = {0: ["partial"]}
    t.has_emitted_text = True
    s.enter_settle()
    assert t.agents_spawned == 5  # counters preserved
    assert t._tool_inputs == {}
    assert t.has_emitted_text is False
    assert s.settling is True


# --- Foreign-result gating helpers (resume handshake / stale flush) ---------

def test_is_foreign_result_shapes():
    from core.layers.cli.settle import (
        RESUME_HANDSHAKE_RESULT, is_foreign_result,
    )
    ok = {"type": "result", "subtype": "success", "is_error": False}
    # Zero content streamed → foreign regardless of text.
    assert is_foreign_result({**ok, "result": "anything"}, 0) is True
    # Handshake sentinel with a stray chunk or two → foreign.
    assert is_foreign_result(
        {**ok, "result": RESUME_HANDSHAKE_RESULT}, 1) is True
    assert is_foreign_result(
        {**ok, "result": RESUME_HANDSHAKE_RESULT}, 3) is False
    # Real content-bearing results close the turn.
    assert is_foreign_result({**ok, "result": "done"}, 4) is False
    # Error results ALWAYS close the turn (abort path depends on it).
    assert is_foreign_result(
        {"type": "result", "subtype": "error", "is_error": True,
         "result": ""}, 0) is False


def test_chunk_is_content_ignores_progress_pings():
    from core.layers.cli.helpers import ClaudeStreamChunk
    from core.layers.cli.settle import chunk_is_content
    assert chunk_is_content(ClaudeStreamChunk(text="hi")) is True
    assert chunk_is_content(ClaudeStreamChunk(text="")) is False
    # thinking_tokens gauges must not count as content…
    assert chunk_is_content(ClaudeStreamChunk(
        event_type="thinking",
        event_data={"phase": "progress", "estimated_tokens": 42},
    )) is False
    # …but real thinking phases do.
    assert chunk_is_content(ClaudeStreamChunk(
        event_type="thinking", event_data={"phase": "start"},
    )) is True
    assert chunk_is_content(ClaudeStreamChunk(
        event_type="tool_start", event_data={"name": "Bash"},
    )) is True
    assert chunk_is_content(ClaudeStreamChunk(
        event_type="metadata", event_data={},
    )) is False


# --- ForeignSkipGate (the skip regime's state machine) ----------------------

def _mk_gate():
    t = ClaudeCLIEventTranslator(_SID)
    from core.layers.cli.settle import ForeignSkipGate
    return t, ForeignSkipGate(_SID, t)


def test_gate_arms_on_skip_and_slides_on_noise(monkeypatch):
    from core.layers.cli import settle as settle_mod
    monkeypatch.setattr(settle_mod, "FOREIGN_SKIP_SILENCE_S", 10.0)
    _, g = _mk_gate()
    assert g.deadline is None
    g.note_event()          # unarmed: no-op
    assert g.deadline is None
    g.record_skip({"result": ""})
    first = g.deadline
    assert first is not None
    time.sleep(0.01)
    g.note_event()          # armed: SLIDES — never disarms
    assert g.deadline is not None and g.deadline > first


def test_gate_content_clears_regime_and_counts_since_skip():
    _, g = _mk_gate()
    g.note_content()
    g.note_content()
    assert g.content_since_skip == 2
    g.record_skip({"result": ""})
    # Skip restarts the content window (a burst's own junk text must not
    # flip the NEXT zero-content result to "real")…
    assert g.content_since_skip == 0
    assert g.deadline is not None
    # …and proven content ends the regime.
    g.note_content()
    assert g.content_since_skip == 1
    assert g.deadline is None


def test_gate_expiry_and_rearm(monkeypatch):
    from core.layers.cli import settle as settle_mod
    monkeypatch.setattr(settle_mod, "FOREIGN_SKIP_SILENCE_S", 0.0)
    _, g = _mk_gate()
    g.record_skip({"result": ""})
    assert g.expired() is True
    monkeypatch.setattr(settle_mod, "FOREIGN_SKIP_SILENCE_S", 10.0)
    g.re_arm()
    assert g.expired() is False


def test_gate_record_skip_resets_translator_without_settle():
    t, g = _mk_gate()
    t.has_emitted_text = True
    t._tool_inputs = {0: ["partial"]}
    g.record_skip({"result": "No response requested."})
    # Parse state cleared so the driven turn's result-text fallback works…
    assert t.has_emitted_text is False
    assert t._tool_inputs == {}
    # …but settle semantics NOT entered (bg completions must still surface).
    assert t._in_settle is False


def test_translator_suppresses_handshake_sentinel_fallback():
    from core.layers.cli.translator import RESUME_HANDSHAKE_RESULT
    t = ClaudeCLIEventTranslator(_SID)
    chunks = t._handle_result({
        "type": "result", "subtype": "success", "is_error": False,
        "result": RESUME_HANDSHAKE_RESULT,
    })
    # No text chunk for the CLI's resume-handshake plumbing — metadata only.
    assert all(not c.text for c in chunks)
    # A real un-streamed answer still falls back to the result text.
    chunks = t._handle_result({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "actual answer",
    })
    assert any(c.text == "actual answer" for c in chunks)
