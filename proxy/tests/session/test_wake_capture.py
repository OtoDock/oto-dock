"""Self-wake turn capture (Claude Code ≥2.1.243) — core/events/wake_capture.py.

Fixtures under ``tests/fixtures/cli_wake/`` are the RAW stream-json a real
2.1.243 ``-p`` session emitted (probe rig 2026-08-27, private paths scrubbed):

  * ``probe-bash.jsonl``  — run_in_background Bash → turn ends → idle
    completion (task_updated + task_notification + the NEW
    background_tasks_changed snapshot) → the CLI's SELF-WAKE turn
    (its own system/init … result reporting the output).
  * ``probe-agent.jsonl`` — background Task/Agent spawn; the subagent's
    inner activity streams between turns (owned_by_subagent), then the wake.
  * ``probe-race.jsonl``  — a user message sent just before completion runs
    FIRST; the wake turn queues after it (its own init…result bracket).

These pin the 1.5 contract: between-turns readers CAPTURE a wake bracket as
a real chat turn (persist + WS + usage) instead of discarding it, the
snapshot subtype is suppressed + reconciles the registries, and a captured
wake stands the platform's own bg nudges down (they are the fallback now).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.events import wake_capture
from core.events.bg_command_state import get_bg_command_registry
from core.events.pump_bg_monitors import _wake_grace_covers
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.session.session_state import (
    get_subagent_registry,
    reconcile_background_snapshot,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cli_wake"


def _frames(name: str) -> list[dict]:
    return [json.loads(line)
            for line in (FIXTURES / f"probe-{name}.jsonl").read_text().splitlines()
            if line.strip()]


def _wake_bracket(frames: list[dict]) -> list[dict]:
    """The LAST init-bracket in a capture — the self-wake turn."""
    init_idxs = [i for i, f in enumerate(frames)
                 if f.get("type") == "system" and f.get("subtype") == "init"]
    assert len(init_idxs) >= 2, "fixture must contain a wake bracket"
    start = init_idxs[-1]
    end = max(i for i, f in enumerate(frames) if f.get("type") == "result")
    assert end > start
    return frames[start:end + 1]


# ── Fixture shape pins (drift alarms for future CLI bumps) ──────────────────

def test_bash_fixture_shapes_hold():
    frames = _frames("bash")
    started = [f for f in frames if f.get("subtype") == "task_started"]
    assert started and started[0]["task_type"] == "local_bash"
    assert started[0]["is_backgrounded"] is True
    assert started[0]["tool_use_id"].startswith("toolu_")
    updated = [f for f in frames if f.get("subtype") == "task_updated"]
    assert updated and updated[0]["patch"]["status"] == "completed"
    snaps = [f for f in frames if f.get("subtype") == "background_tasks_changed"]
    assert snaps and isinstance(snaps[0].get("tasks"), list)
    # The wake bracket exists and reports the marker output.
    bracket = _wake_bracket(frames)
    assert bracket[-1]["type"] == "result"
    assert "BG_MARKER_DONE" in str(bracket[-1].get("result", ""))


def test_agent_fixture_has_inner_activity_between_turns():
    frames = _frames("agent")
    started = [f for f in frames if f.get("subtype") == "task_started"]
    assert any(f.get("task_type") == "local_agent" and f.get("is_backgrounded")
               for f in started)
    # Inner (subagent-owned) frames stream between turns on 2.1.243.
    assert any(f.get("owned_by_subagent") for f in started)


def test_race_fixture_user_turn_wins_wake_queues_after():
    frames = _frames("race")
    results = [i for i, f in enumerate(frames) if f.get("type") == "result"]
    assert len(results) == 3  # STARTED, the user's "4", the wake report
    assert "4" in str(frames[results[1]].get("result", ""))
    assert "BG_MARKER_DONE" in str(frames[results[2]].get("result", ""))


# ── background_tasks_changed: suppression + reconciliation ──────────────────

def test_snapshot_suppressed_and_reconciles_lost_completion():
    sid = "s-wake-snap"
    t = ClaudeCLIEventTranslator(sid)
    assert t.feed({"type": "system", "subtype": "task_started",
                   "task_id": "b1", "tool_use_id": "tu1",
                   "description": "x", "is_backgrounded": True,
                   "task_type": "local_bash"}) is not None
    bgreg = get_bg_command_registry(sid)
    assert bgreg.has_pending
    # The empty snapshot yields NO chunks (no junk system row) and resolves
    # the pending command whose completion frame was lost.
    chunks = t.feed({"type": "system", "subtype": "background_tasks_changed",
                     "tasks": []})
    assert chunks == []
    assert not bgreg.has_pending


def test_reconcile_snapshot_resolves_subagents_too():
    sid = "s-wake-snap2"
    reg = get_subagent_registry(sid)
    reg.register_spawn("a1", "tu-a1")
    assert reg.has_pending
    handled = reconcile_background_snapshot(
        sid, {"type": "system", "subtype": "background_tasks_changed",
              "tasks": [{"task_id": "other", "task_type": "local_bash"}]})
    assert handled is True
    assert not reg.has_pending
    # Non-snapshot frames are not claimed.
    assert reconcile_background_snapshot(sid, {"type": "result"}) is False


# ── Wake-bracket capture → recorded chat turn ───────────────────────────────

def _scripted_reader(frames: list[dict]):
    it = iter(frames)

    async def _read(timeout: float) -> dict | None:
        try:
            return next(it)
        except StopIteration:
            return None
    return _read


@pytest.mark.asyncio
async def test_capture_records_wake_turn_to_chat(temp_db):
    from storage import database as task_store
    sid = "s-wake-cap-1"
    cid = "chat-wake-cap-1"
    task_store.create_chat(cid, "user-admin", "test-agent", "default",
                           model="claude-sonnet-5",
                           execution_path="claude-code-cli")
    task_store.update_chat(cid, session_id=sid)

    bracket = _wake_bracket(_frames("bash"))
    ok = await wake_capture.capture_wake_turn(
        sid, bracket[0], _scripted_reader(bracket[1:]), source="test")
    assert ok is True
    assert wake_capture.recently_captured(sid)

    msgs = task_store.get_chat_messages(cid)
    joined = json.dumps(msgs)
    assert "bg_wake" in joined                      # provenance marker row
    assert "BG_MARKER_DONE" in joined               # the wake turn's report


@pytest.mark.asyncio
async def test_capture_never_displaces_an_active_pump(temp_db):
    from core.events.stream_pump import _active_pumps
    from storage import database as task_store
    sid = "s-wake-cap-2"
    cid = "chat-wake-cap-2"
    task_store.create_chat(cid, "user-admin", "test-agent", "default",
                           model="claude-sonnet-5",
                           execution_path="claude-code-cli")
    task_store.update_chat(cid, session_id=sid)

    sentinel = object()
    _active_pumps[cid] = sentinel  # the NEXT user turn's pump (stale-drain race)
    try:
        bracket = _wake_bracket(_frames("bash"))
        ok = await wake_capture.capture_wake_turn(
            sid, bracket[0], _scripted_reader(bracket[1:]), source="test")
        assert ok is True
        assert _active_pumps.get(cid) is sentinel   # never displaced
        # …and the wake content still persisted for reload.
        assert "BG_MARKER_DONE" in json.dumps(task_store.get_chat_messages(cid))
    finally:
        _active_pumps.pop(cid, None)


@pytest.mark.asyncio
async def test_capture_without_chat_row_consumes_frames_only(temp_db):
    sid = "s-wake-cap-3"
    bracket = _wake_bracket(_frames("bash"))
    ok = await wake_capture.capture_wake_turn(
        sid, bracket[0], _scripted_reader(bracket[1:]), source="test")
    assert ok is False                 # nothing recorded…
    assert wake_capture.recently_captured(sid)  # …but the bracket closed


@pytest.mark.asyncio
async def test_unclosed_bracket_flushes_partial(temp_db):
    from storage import database as task_store
    sid = "s-wake-cap-4"
    cid = "chat-wake-cap-4"
    task_store.create_chat(cid, "user-admin", "test-agent", "default",
                           model="claude-sonnet-5",
                           execution_path="claude-code-cli")
    task_store.update_chat(cid, session_id=sid)

    bracket = _wake_bracket(_frames("bash"))
    truncated = bracket[:-1]  # no result — satellite drain ceiling etc.
    ok = await wake_capture.capture_wake_turn(
        sid, truncated[0], _scripted_reader(truncated[1:]), source="test")
    assert ok is False
    assert not wake_capture.recently_captured(sid)


# ── Nudge stand-down (the monitors' wake grace) ─────────────────────────────

class _FakeClaudeLayer:
    def __init__(self, sid: str, captures_on_drain: bool):
        self._sid = sid
        self._captures = captures_on_drain
        self.drains = 0

    async def session_self_wakes(self, session_id: str) -> bool:
        return True

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0):
        self.drains += 1
        if self._captures:
            wake_capture.note_captured(session_id)
            return True
        return False


class _FakeCodexLayer:
    """No session_self_wakes → the grace must not run at all."""

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0):
        raise AssertionError("codex layers must skip the wake grace")


@pytest.mark.asyncio
async def test_wake_grace_covers_when_drain_captures():
    sid = "s-wake-grace-1"
    wake_capture.forget_session(sid)
    layer = _FakeClaudeLayer(sid, captures_on_drain=True)
    assert await _wake_grace_covers(layer, sid) is True
    assert layer.drains >= 1


@pytest.mark.asyncio
async def test_wake_grace_skipped_for_non_waking_layers():
    assert await _wake_grace_covers(_FakeCodexLayer(), "s-wake-grace-2") is False


@pytest.mark.asyncio
async def test_wake_grace_instant_when_already_captured():
    sid = "s-wake-grace-3"
    wake_capture.note_captured(sid)
    layer = _FakeClaudeLayer(sid, captures_on_drain=False)
    assert await _wake_grace_covers(layer, sid) is True
    assert layer.drains == 0  # no drain needed — capture already noted
