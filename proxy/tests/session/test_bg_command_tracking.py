"""Background bash-command tracking through the CLI translator.

Drives ClaudeCLIEventTranslator with the exact stream-json frame shapes a real
`claude -p` session emits for a `run_in_background` Bash (captured empirically):

  * Bash tool_use with run_in_background:true            → BG_COMMAND_START
  * system task_started {task_type:"local_bash", ...}    → registers in the
                                                            BackgroundCommandRegistry (no dashboard event)
  * system task_updated {patch.status:"completed"}       → BG_COMMAND_END
  * system task_notification {task_id}                    → BG_COMMAND_END (stdout backup)

Pure module (no DB / conftest): run with
    ./venv/bin/python tests/session/test_bg_command_tracking.py
or  ./venv/bin/python -m pytest tests/session/test_bg_command_tracking.py -q
"""

from __future__ import annotations

import pytest

from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.events.bg_command_state import get_bg_command_registry
from core.session.session_state import get_subagent_registry


def _bash_tool_call(idx: int, tool_id: str, input_json: str) -> list[dict]:
    return [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": idx,
            "content_block": {"type": "tool_use", "id": tool_id, "name": "Bash"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": idx,
            "delta": {"type": "input_json_delta", "partial_json": input_json}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": idx}},
    ]


def _sys(subtype: str, **kw) -> dict:
    return {"type": "system", "subtype": subtype, **kw}


def _events(translator, feed_list):
    out = []
    for e in feed_list:
        for c in translator.feed(e):
            for ev in cli_chunk_to_events(c):
                out.append((ev.type, dict(ev.data or {})))
    return out


def test_bg_bash_spawn_emits_bg_command_start():
    # The badge fires at task_started (post-approval), NOT at the Bash tool_use —
    # so a permission-rejected command never strands a badge. The tool_use only
    # emits the normal tool card (which carries the actual command).
    t = ClaudeCLIEventTranslator("s-bg-spawn")
    out = _events(t, _bash_tool_call(
        0, "tu1",
        '{"command":"sleep 5 && echo X","description":"bg sleep","run_in_background":true}',
    ))
    assert any(ty == "tool_input" for ty, _ in out), out            # normal card
    assert not any(ty == "bg_command_start" for ty, _ in out), out  # not yet
    # The command actually starts → task_started{local_bash} → the badge.
    out2 = _events(t, [_sys("task_started", task_id="b1", tool_use_id="tu1",
                            description="bg sleep", task_type="local_bash")])
    starts = [d for ty, d in out2 if ty == "bg_command_start"]
    assert len(starts) == 1, out2
    assert starts[0]["tool_use_id"] == "tu1"
    assert starts[0]["description"] == "bg sleep"
    # The REAL command rides the spawn event (staged at content_block_stop) —
    # the dashboard pill expands to it. It must never be the description twin.
    assert starts[0]["command"] == "sleep 5 && echo X"


def test_bg_command_start_without_staged_command():
    # task_started with no prior Bash tool_use in this translator (e.g. a frame
    # replay edge) → command is EMPTY, not the description — the dashboard pill
    # falls back to the paired Bash tool card's input.
    t = ClaudeCLIEventTranslator("s-bg-nostash")
    out = _events(t, [_sys("task_started", task_id="b9", tool_use_id="tu9",
                           description="mystery job", task_type="local_bash")])
    starts = [d for ty, d in out if ty == "bg_command_start"]
    assert len(starts) == 1, out
    assert starts[0]["command"] == ""
    assert starts[0]["description"] == "mystery job"


def test_rejected_bg_bash_no_badge():
    # A permission-rejected command never fires task_started → no badge, no
    # registry entry (the bug: it used to strand a never-clearing badge).
    sid = "s-rej"
    t = ClaudeCLIEventTranslator(sid)
    out = _events(t, _bash_tool_call(0, "tuR", '{"command":"x","run_in_background":true}'))
    assert not any(ty == "bg_command_start" for ty, _ in out), out
    assert get_bg_command_registry(sid).pending_count == 0


def test_bg_bash_full_lifecycle():
    sid = "s-bg-life"
    t = ClaudeCLIEventTranslator(sid)
    bgreg = get_bg_command_registry(sid)

    out1 = _events(t, _bash_tool_call(
        0, "tu1", '{"command":"sleep 5","run_in_background":true}'))
    assert not any(ty == "bg_command_start" for ty, _ in out1), out1  # not until task_started

    # task_started binds the shell id -> tool_use_id, gates the wait, AND emits the badge.
    out2 = _events(t, [_sys("task_started", task_id="b1", tool_use_id="tu1",
                            description="sleep 5", task_type="local_bash")])
    assert any(ty == "bg_command_start" for ty, _ in out2), out2
    assert bgreg.pending_count == 1 and bgreg.has_pending
    assert bgreg.tuid_for("b1") == "tu1"

    # Completion via task_updated (the primary signal).
    out3 = _events(t, [_sys("task_updated", task_id="b1", patch={"status": "completed"})])
    ends = [d for ty, d in out3 if ty == "bg_command_end"]
    assert len(ends) == 1, out3
    assert ends[0]["tool_use_id"] == "tu1" and ends[0]["status"] == "completed"
    assert bgreg.pending_count == 0 and not bgreg.has_pending

    # Idempotent: a duplicate completion frame emits nothing.
    out4 = _events(t, [_sys("task_updated", task_id="b1", patch={"status": "completed"})])
    assert out4 == [], out4


def test_foreground_bash_not_tracked():
    sid = "s-fg"
    t = ClaudeCLIEventTranslator(sid)
    out = _events(t, _bash_tool_call(0, "tu9", '{"command":"ls -la"}'))
    assert any(ty == "tool_input" for ty, _ in out), out      # normal tool card
    assert not any(ty == "bg_command_start" for ty, _ in out), out
    assert get_bg_command_registry(sid).pending_count == 0


def test_task_updated_unknown_taskid_ignored():
    sid = "s-unknown"
    t = ClaudeCLIEventTranslator(sid)
    # Never registered "ghost" — a completion for it must not emit anything.
    out = _events(t, [_sys("task_updated", task_id="ghost", patch={"status": "completed"})])
    assert out == [], out


def test_task_notification_backup_completes():
    sid = "s-notif"
    t = ClaudeCLIEventTranslator(sid)
    bgreg = get_bg_command_registry(sid)
    _events(t, _bash_tool_call(0, "tuN", '{"command":"x","run_in_background":true}'))
    _events(t, [_sys("task_started", task_id="bN", tool_use_id="tuN", task_type="local_bash")])
    assert bgreg.pending_count == 1
    # No task_updated this time — only the stdout task_notification backup fires.
    out = _events(t, [_sys("task_notification", task_id="bN")])
    ends = [d for ty, d in out if ty == "bg_command_end"]
    assert len(ends) == 1 and ends[0]["tool_use_id"] == "tuN", out
    assert bgreg.pending_count == 0


def test_registries_are_isolated():
    sid = "s-iso"
    t = ClaudeCLIEventTranslator(sid)
    # local_bash → bg registry only, NOT the subagent registry (it has no
    # SubagentStop, so it must never enter the subagent completion gate).
    _events(t, [_sys("task_started", task_id="bash1", tool_use_id="tub", task_type="local_bash")])
    assert get_bg_command_registry(sid).pending_count == 1
    assert get_subagent_registry(sid).pending_count == 0
    # local_agent → subagent registry only, NOT the bg-command registry.
    _events(t, [_sys("task_started", task_id="agent1", tool_use_id="tua", task_type="local_agent")])
    assert get_subagent_registry(sid).pending_count == 1
    assert get_bg_command_registry(sid).pending_count == 1   # unchanged


def test_resolve_bg_command_frame():
    # The post-turn drain paths (idle monitor + _drain_stale_output) resolve a
    # bg command directly from a raw stream-json frame, without the per-turn
    # translator. chat_id is unset here so no pump push is attempted.
    from core.session.session_state import resolve_bg_command_frame
    sid = "s-frame"
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("bf1", "tuf1")
    # task_updated{completed} → resolves (transition True)
    assert resolve_bg_command_frame(sid, {
        "type": "system", "subtype": "task_updated",
        "task_id": "bf1", "patch": {"status": "completed"}}) is True
    assert bgreg.pending_count == 0
    # idempotent
    assert resolve_bg_command_frame(sid, {
        "type": "system", "subtype": "task_updated",
        "task_id": "bf1", "patch": {"status": "completed"}}) is False
    # non-terminal status → not resolved
    bgreg.register_spawn("bf2", "tuf2")
    assert resolve_bg_command_frame(sid, {
        "type": "system", "subtype": "task_updated",
        "task_id": "bf2", "patch": {"status": "running"}}) is False
    assert bgreg.pending_count == 1
    # task_notification backup → resolves
    assert resolve_bg_command_frame(sid, {
        "type": "system", "subtype": "task_notification", "task_id": "bf2"}) is True
    assert bgreg.pending_count == 0
    # unrelated frames ignored
    assert resolve_bg_command_frame(sid, {"type": "user"}) is False
    assert resolve_bg_command_frame(sid, {"type": "system", "subtype": "init"}) is False


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


def test_interrupt_diagnostic_text_is_dropped():
    # CLI-synthesized "[ede_diagnostic] …" assistant text (emitted on a
    # graceful interrupt) must never surface as chat content.
    from core.layers.cli.session import ClaudeStreamChunk
    noise = ClaudeStreamChunk(
        event_type="text",
        text="[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use",
    )
    assert cli_chunk_to_events(noise) == []
    real = ClaudeStreamChunk(event_type="text", text="a real answer")
    assert [e.data["content"] for e in cli_chunk_to_events(real)
            if e.type == "text"] == ["a real answer"]


def test_interrupt_diagnostic_error_is_dropped():
    # The same marker can arrive as an is_error RESULT when the graceful
    # interrupt lands mid-tool-use (stop_reason=tool_use) — a duplex
    # barge-in during a web search rendered "Error: [ede_diagnostic] …"
    # in the chat (live-hit 2026-08-11). Drop it on the error path too;
    # real errors still surface.
    from core.layers.cli.session import ClaudeStreamChunk
    noise = ClaudeStreamChunk(
        text="[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use",
        is_error=True,
    )
    assert cli_chunk_to_events(noise) == []
    real = ClaudeStreamChunk(text="engine exploded", is_error=True)
    assert [e.data["message"] for e in cli_chunk_to_events(real)
            if e.type == "error"] == ["engine exploded"]


# ---------------------------------------------------------------------------
# Surfaced vs unsurfaced completions (task-producer review-turn decision)
# ---------------------------------------------------------------------------

def test_completion_during_generation_is_surfaced():
    """A completion resolved while the model is generating (before settle) is
    injected into the live turn by the CLI → it must NOT count as unsurfaced."""
    sid = "s-surf"
    t = ClaudeCLIEventTranslator(sid)
    bgreg = get_bg_command_registry(sid)
    _events(t, _bash_tool_call(0, "tuS", '{"command":"x","run_in_background":true}'))
    _events(t, [_sys("task_started", task_id="bS", tool_use_id="tuS", task_type="local_bash")])
    _events(t, [_sys("task_updated", task_id="bS", patch={"status": "completed"})])
    assert bgreg.pending_count == 0
    assert bgreg.unsurfaced_count == 0


def test_completion_during_settle_is_unsurfaced():
    """After reset_for_settle() the model's final text is out — a completion
    resolved there was never seen and must count as unsurfaced."""
    sid = "s-settle"
    t = ClaudeCLIEventTranslator(sid)
    bgreg = get_bg_command_registry(sid)
    _events(t, _bash_tool_call(0, "tuT", '{"command":"x","run_in_background":true}'))
    _events(t, [_sys("task_started", task_id="bT", tool_use_id="tuT", task_type="local_bash")])
    t.reset_for_settle()
    _events(t, [_sys("task_updated", task_id="bT", patch={"status": "completed"})])
    assert bgreg.pending_count == 0
    assert bgreg.unsurfaced_count == 1
    bgreg.clear_unsurfaced()
    assert bgreg.unsurfaced_count == 0


def test_new_turn_resets_settle_flag_and_unsurfaced():
    """reset_for_new_turn() must clear the settle flag (a reused translator
    would otherwise mark every next-turn completion unsurfaced), and the
    registry's per-turn reset drops resolved unsurfaced ids."""
    sid = "s-reset"
    t = ClaudeCLIEventTranslator(sid)
    bgreg = get_bg_command_registry(sid)
    _events(t, _bash_tool_call(0, "tuR", '{"command":"x","run_in_background":true}'))
    _events(t, [_sys("task_started", task_id="bR", tool_use_id="tuR", task_type="local_bash")])
    t.reset_for_settle()
    _events(t, [_sys("task_updated", task_id="bR", patch={"status": "completed"})])
    assert bgreg.unsurfaced_count == 1
    t.reset_for_new_turn()
    bgreg.reset()
    assert bgreg.unsurfaced_count == 0
    assert t._in_settle is False


def test_post_turn_drain_resolve_is_unsurfaced():
    """resolve_bg_command (idle drain / monitor path) always resolves after
    generation — it must mark the completion unsurfaced."""
    from core.session.session_state import resolve_bg_command_frame
    sid = "s-drain"
    t = ClaudeCLIEventTranslator(sid)
    bgreg = get_bg_command_registry(sid)
    _events(t, _bash_tool_call(0, "tuD", '{"command":"x","run_in_background":true}'))
    _events(t, [_sys("task_started", task_id="bD", tool_use_id="tuD", task_type="local_bash")])
    assert resolve_bg_command_frame(
        sid, {"type": "system", "subtype": "task_updated",
              "task_id": "bD", "patch": {"status": "completed"}})
    assert bgreg.pending_count == 0
    assert bgreg.unsurfaced_count == 1


# ---------------------------------------------------------------------------
# Nudge labels (2026-08-27 round): registration composes a model-facing label
# so completion nudges can name WHICH job finished.
# ---------------------------------------------------------------------------

def test_bg_bash_nudge_label_composed_at_spawn():
    # Claude: shell id FIRST (the handle the model saw in its Bash result and
    # what BashOutput/KillShell take), then the real command for recognition.
    sid = "s-bg-label"
    t = ClaudeCLIEventTranslator(sid)
    _events(t, _bash_tool_call(
        0, "tuL",
        '{"command":"sleep 5 && echo X","description":"bg sleep","run_in_background":true}',
    ))
    _events(t, [_sys("task_started", task_id="bL1", tool_use_id="tuL",
                     description="bg sleep", task_type="local_bash")])
    assert get_bg_command_registry(sid).label_for("bL1") == "bL1 — sleep 5 && echo X"


def test_bg_bash_label_falls_back_to_description():
    # No staged command (replay edge) → the description still names the job.
    sid = "s-bg-label-desc"
    t = ClaudeCLIEventTranslator(sid)
    _events(t, [_sys("task_started", task_id="b9x", tool_use_id="tu9x",
                     description="mystery job", task_type="local_bash")])
    assert get_bg_command_registry(sid).label_for("b9x") == "b9x — mystery job"


def test_subagent_label_from_task_started():
    # CLI subagents: description + the agentId the model holds as its
    # SendMessage handle.
    sid = "s-sub-label"
    t = ClaudeCLIEventTranslator(sid)
    _events(t, [_sys("task_started", task_id="a1b2c3", tool_use_id="tuA",
                     description="probe auth flow", task_type="local_agent")])
    assert get_subagent_registry(sid).label_for("a1b2c3") == '"probe auth flow" [a1b2c3]'


def test_label_helpers():
    from core.events.bg_command_state import shorten_label
    from core.events.pump_bg_monitors import format_job_list
    assert shorten_label("a\n  b\tc") == "a b c"
    capped = shorten_label("x" * 100)
    assert len(capped) == 80 and capped.endswith("…")
    assert format_job_list([]) == ""
    assert format_job_list(["", ""]) == ""            # all-empty → count-only nudge
    assert format_job_list(["a", "", "b"]) == "a; b"  # empties dropped, not counted
    assert format_job_list(["a", "b", "c", "d", "e"]) == "a; b; c (+2 more)"


def test_registry_reset_keeps_pending_labels():
    sid = "s-label-reset"
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("p1", "t1", label="p1 — keep")
    bgreg.register_spawn("d1", "t2", label="d1 — drop")
    bgreg.mark_done("d1")
    bgreg.reset()
    assert bgreg.label_for("p1") == "p1 — keep"
    assert bgreg.label_for("d1") == ""


# ---------------------------------------------------------------------------
# The post-turn monitor nudges only for a completion the model never saw
# ---------------------------------------------------------------------------

class _MonitorLayer:
    """An idle session the bg-command monitor polls; ``on_drain`` stands in
    for whatever resolves the command meanwhile."""

    def __init__(self, on_drain):
        self._on_drain = on_drain

    async def is_session_alive(self, session_id: str) -> bool:
        return True

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0) -> bool:
        return self._on_drain()


async def _run_monitor(monkeypatch, sid: str, on_drain) -> list[dict]:
    import asyncio
    from core.events import pump_bg_monitors
    from core.session.session_state import _dashboard_notify_queues
    monkeypatch.setattr(pump_bg_monitors.task_store, "get_chat", lambda cid: {})
    queue: asyncio.Queue = asyncio.Queue()
    _dashboard_notify_queues[sid] = queue
    try:
        await pump_bg_monitors._bg_command_monitor(_MonitorLayer(on_drain), sid, "c-" + sid, 1)
    finally:
        _dashboard_notify_queues.pop(sid, None)
    return [queue.get_nowait() for _ in range(queue.qsize())]


@pytest.mark.asyncio
async def test_monitor_skips_the_nudge_for_a_completion_a_later_turn_surfaced(monkeypatch):
    # The command outlives its turn; the person's next turn starts (the
    # registry reset keeps it pending) and the CLI hands its completion to
    # the model inside that turn. The monitor must not start a review turn.
    from core.events.bg_command_state import reset_bg_command_registry
    sid = "s-mon-surfaced"
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("bM1", "tuM1", label="bM1 — make test")

    def later_turn():
        reset_bg_command_registry(sid)
        t = ClaudeCLIEventTranslator(sid)
        _events(t, [_sys("task_updated", task_id="bM1", patch={"status": "completed"})])
        return False

    assert await _run_monitor(monkeypatch, sid, later_turn) == []
    assert bgreg.pending_count == 0 and bgreg.unsurfaced_count == 0


@pytest.mark.asyncio
async def test_monitor_nudges_for_a_completion_the_model_never_saw(monkeypatch):
    from core.session.session_state import resolve_bg_command_frame
    sid = "s-mon-unseen"
    get_bg_command_registry(sid).register_spawn("bM2", "tuM2", label="bM2 — make test")

    def idle_drain():
        return resolve_bg_command_frame(sid, {
            "type": "system", "subtype": "task_updated",
            "task_id": "bM2", "patch": {"status": "completed"}})

    nudge, = await _run_monitor(monkeypatch, sid, idle_drain)
    assert nudge["type"] == "bg_command_nudge"
    assert nudge["labels"] == ["bM2 — make test"]


@pytest.mark.asyncio
async def test_a_nudge_queued_on_a_running_pump_writes_its_row_off_the_loop(
        monkeypatch, temp_db, loop_db_guard):
    """The monitor's nudge event row rides the chat writer, not
    a synchronous store call on the loop."""
    from core.events import chat_writer, pump_bg_monitors
    from core.session.session_state import resolve_bg_command_frame
    from storage import database as task_store
    sid = "s-mon-offloop"
    chat_id = "c-" + sid
    task_store.create_chat(chat_id, "user-admin", "agent-x")
    get_bg_command_registry(sid).register_spawn("bM3", "tuM3", label="bM3 — build")
    monkeypatch.setattr(pump_bg_monitors, "queue_pump_prompt", lambda *a, **k: True)
    monkeypatch.setattr(pump_bg_monitors.task_store, "get_chat", lambda cid: {})

    def idle_drain():
        return resolve_bg_command_frame(sid, {
            "type": "system", "subtype": "task_updated",
            "task_id": "bM3", "patch": {"status": "completed"}})

    with loop_db_guard.active():
        await pump_bg_monitors._bg_command_monitor(_MonitorLayer(idle_drain), sid, chat_id, 1)
        await chat_writer.drain(chat_id)
    rows = task_store.get_chat_messages(chat_id)
    assert [r["event_type"] for r in rows] == ["bg_command_nudge"]


@pytest.mark.asyncio
async def test_the_abort_check_reads_the_chat_off_the_loop(monkeypatch, temp_db, loop_db_guard):
    """A cohort that completes after the person stopped the chat's last turn
    earns no nudge, and the read of that flag runs on the DB executor."""
    from core.events import pump_bg_monitors
    from core.session.session_state import resolve_bg_command_frame
    from storage import database as task_store
    sid = "s-mon-aborted"
    chat_id = "c-" + sid
    task_store.create_chat(chat_id, "user-admin", "agent-x")
    task_store.update_chat(chat_id, last_turn_aborted=True)
    get_bg_command_registry(sid).register_spawn("bM4", "tuM4", label="bM4: build")
    queued: list = []
    monkeypatch.setattr(pump_bg_monitors, "queue_pump_prompt",
                        lambda *a, **k: queued.append(a) or True)

    def idle_drain():
        return resolve_bg_command_frame(sid, {
            "type": "system", "subtype": "task_updated",
            "task_id": "bM4", "patch": {"status": "completed"}})

    with loop_db_guard.active():
        await pump_bg_monitors._bg_command_monitor(_MonitorLayer(idle_drain), sid, chat_id, 1)
    assert queued == []
