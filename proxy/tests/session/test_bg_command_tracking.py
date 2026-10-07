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

from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# The review nobody views: after the monitor's guard, and a visible chat turn
# ---------------------------------------------------------------------------

class _ReviewLayer(_MonitorLayer):
    """A session a monitor polls and then reviews in: ``on_drain`` resolves
    the job, ``send_message`` answers one turn (running ``during_turn``)."""

    def __init__(self, on_drain, *, during_turn=None, self_wakes=False):
        super().__init__(on_drain)
        self.sends: list[str] = []
        self._during_turn = during_turn
        self._self_wakes = self_wakes
        self._locks: dict = {}

    def session_lock(self, sid):
        import asyncio
        return self._locks.setdefault(sid, asyncio.Lock())

    async def send_message(self, sid, prompt, **kwargs):
        from core.events.common_events import CommonEvent, TEXT, DONE
        self.sends.append(prompt)
        if self._during_turn:
            self._during_turn()
        yield CommonEvent(type=TEXT, data={"content": "reviewed"})
        yield CommonEvent(type=DONE, data={})

    async def session_self_wakes(self, sid):
        return self._self_wakes

    def remote_stream_severed(self, sid):
        return False

    def session_idle_seconds(self, sid):
        return 0.0

    async def probe_session_process_dead(self, sid):
        return False

    async def prepare_resume(self, sid):
        return


def _resolver(sid: str, task_id: str):
    from core.session.session_state import resolve_bg_command_frame

    def _drain():
        return resolve_bg_command_frame(sid, {
            "type": "system", "subtype": "task_updated",
            "task_id": task_id, "patch": {"status": "completed"}})
    return _drain


@pytest.fixture
def review_env(monkeypatch, temp_db):
    """A chat bound to its session, the pump's broadcasts recorded, and the
    per-session state cleaned up after."""
    from core.events import pump_bg_monitors, stream_pump
    from core.events.bg_command_state import _bg_command_registries
    from core.session.session_state import _subagent_registries
    from storage import database as task_store
    statuses: list[tuple[str, str]] = []
    monkeypatch.setattr(stream_pump.notification_manager, "broadcast_chat_status",
                        lambda owner, cid, status, agent="": statuses.append((cid, status)))

    async def _quiet(*a, **k):
        return None
    monkeypatch.setattr(stream_pump.notification_manager, "fire_ephemeral", _quiet)
    made: list[tuple[str, str]] = []

    def _bind(chat_id: str, sid: str, *, row_sid: str = "", **kw):
        owner = kw.pop("owner", "user-1")
        task_store.create_chat(chat_id, owner, "agent-x", **kw)
        task_store.update_chat(chat_id, session_id=row_sid or sid)
        made.append((chat_id, sid))
    yield SimpleNamespace(bind=_bind, statuses=statuses)
    for chat_id, sid in made:
        _bg_command_registries.pop(sid, None)
        _subagent_registries.pop(sid, None)
        stream_pump._active_pumps.pop(chat_id, None)
        pump_bg_monitors._bg_command_monitors_running.discard(sid)
        pump_bg_monitors._bg_monitors_running.discard(sid)


def _rows(chat_id: str) -> list[tuple[str, str, str]]:
    from storage import database as task_store
    return [(r["role"], r.get("event_type") or "", r.get("content") or "")
            for r in task_store.get_chat_messages(chat_id)]


async def _monitor(layer, sid: str, chat_id: str, *, agents: bool = False) -> None:
    import asyncio
    from core.events import chat_writer, pump_bg_monitors
    run = pump_bg_monitors._bg_agent_monitor if agents else pump_bg_monitors._bg_command_monitor
    await asyncio.wait_for(run(layer, sid, chat_id, 1), timeout=10)
    await chat_writer.drain(chat_id)


@pytest.mark.asyncio
async def test_a_review_nobody_views_is_a_streamed_chat_turn(review_env):
    from storage import database as task_store
    sid, chat_id = "s-rv1", "c-rv1"
    review_env.bind(chat_id, sid)
    get_bg_command_registry(sid).register_spawn("bR1", "tuR1", label="bR1 — build")
    layer = _ReviewLayer(_resolver(sid, "bR1"))

    await _monitor(layer, sid, chat_id)

    assert len(layer.sends) == 1 and "bR1 — build" in layer.sends[0]
    rows = _rows(chat_id)
    assert rows[0][:2] == ("event", "bg_command_nudge")
    assert [r for r in rows if r[0] == "assistant"] == [("assistant", "", "reviewed")]
    assert (chat_id, "streaming") in review_env.statuses
    assert (chat_id, "ready") in review_env.statuses
    assert (task_store.get_chat(chat_id) or {}).get("last_response_at")


class _BlipReviewLayer(_ReviewLayer):
    """The session's machine drops: the session reads gone while its reconnect
    grace holds it (``away`` looks), then it is back or not, and its process
    answers the probe."""

    def __init__(self, on_drain, *, away: int, back: bool = True, probe_dead: bool = False):
        super().__init__(on_drain)
        self.away, self.back, self.probe_dead = away, back, probe_dead

    async def is_session_alive(self, session_id: str) -> bool:
        return self.away <= 0 and self.back

    def is_session_grace_held(self, session_id: str) -> bool:
        if self.away > 0:
            self.away -= 1
            return True
        return False

    async def probe_session_process_dead(self, sid):
        return self.probe_dead


@pytest.fixture
def fast_grace(monkeypatch):
    from core.events import pump_bg_monitors
    monkeypatch.setattr(pump_bg_monitors, "GRACE_POLL_S", 0.001)


@pytest.mark.asyncio
async def test_a_monitor_rides_out_its_machines_blip(review_env, fast_grace):
    sid, chat_id = "s-rvb1", "c-rvb1"
    review_env.bind(chat_id, sid)
    get_bg_command_registry(sid).register_spawn("bRB1", "tuRB1", label="bRB1 — build")
    layer = _BlipReviewLayer(_resolver(sid, "bRB1"), away=3)

    await _monitor(layer, sid, chat_id)

    assert len(layer.sends) == 1 and "bRB1 — build" in layer.sends[0]


@pytest.mark.asyncio
async def test_an_agent_monitor_rides_out_its_machines_blip(review_env, fast_grace):
    from core.session.session_state import get_subagent_registry
    sid, chat_id = "s-rvb2", "c-rvb2"
    review_env.bind(chat_id, sid)
    reg = get_subagent_registry(sid)
    reg.register_spawn("sub-b", "tu-b", label="audit")

    class _Layer(_BlipReviewLayer):
        def is_session_grace_held(self, session_id: str) -> bool:
            held = super().is_session_grace_held(session_id)
            if not held:
                reg.mark_done("sub-b")      # it finished while the machine was away
            return held

    layer = _Layer(lambda: False, away=2)

    await _monitor(layer, sid, chat_id, agents=True)

    assert len(layer.sends) == 1 and "audit" in layer.sends[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("back, probe_dead", [(False, False), (True, True)])
async def test_a_monitor_whose_session_does_not_come_back_exits(
        review_env, fast_grace, back, probe_dead):
    sid, chat_id = f"s-rvb3-{back}", f"c-rvb3-{back}"
    review_env.bind(chat_id, sid)
    get_bg_command_registry(sid).register_spawn("bRB3", "tuRB3")
    layer = _BlipReviewLayer(lambda: False, away=2, back=back, probe_dead=probe_dead)

    await _monitor(layer, sid, chat_id)

    assert layer.sends == []


@pytest.mark.asyncio
async def test_a_review_that_starts_new_work_gets_its_own_monitor(review_env, monkeypatch):
    import asyncio
    from core.events import pump_bg_monitors
    sid, chat_id = "s-rv2", "c-rv2"
    review_env.bind(chat_id, sid)
    get_bg_command_registry(sid).register_spawn("bR2", "tuR2", label="bR2 — build")
    real = pump_bg_monitors._bg_command_monitor
    launched: list[tuple[str, str, int]] = []

    async def _recorded(layer, s, c, count):
        launched.append((s, c, count))
    monkeypatch.setattr(pump_bg_monitors, "_bg_command_monitor", _recorded)
    layer = _ReviewLayer(_resolver(sid, "bR2"), during_turn=lambda: get_bg_command_registry(
        sid).register_spawn("bR2b", "tuR2b", label="bR2b — deploy"))

    await asyncio.wait_for(real(layer, sid, chat_id, 1), timeout=10)
    await asyncio.sleep(0)

    assert launched == [(sid, chat_id, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tag, kw", [
    ("worker", {"delegate_role": "worker"}),
    ("task", {"source_type": "task"}),
])
async def test_a_worker_or_task_chat_outside_a_report_gets_the_chat_turn(review_env, tag, kw):
    sid, chat_id = f"s-rv1-{tag}", f"c-rv1-{tag}"
    review_env.bind(chat_id, sid, **kw)
    get_bg_command_registry(sid).register_spawn("bR1w", "tuR1w", label="bR1w — build")
    layer = _ReviewLayer(_resolver(sid, "bR1w"))

    await _monitor(layer, sid, chat_id)

    assert len(layer.sends) == 1
    assert (chat_id, "streaming") in review_env.statuses


@pytest.mark.asyncio
@pytest.mark.parametrize("tag, kw", [
    ("meeting", {"row_sid": "s-the-chats-own"}),       # a participant's session
    ("phone", {"source_type": "phone", "owner": "phone"}),
])
async def test_elsewhere_the_review_is_the_direct_send(review_env, tag, kw):
    sid, chat_id = f"s-rv3-{tag}", f"c-rv3-{tag}"
    review_env.bind(chat_id, sid, **kw)
    get_bg_command_registry(sid).register_spawn("bR3", "tuR3", label="bR3 — build")
    layer = _ReviewLayer(_resolver(sid, "bR3"))

    await _monitor(layer, sid, chat_id)

    assert len(layer.sends) == 1
    assert ("assistant", "", "reviewed") in _rows(chat_id)
    assert not [s for s in review_env.statuses if s[0] == chat_id]


class _HeldPump:
    """A pump holding the chat that drains no system prompts; ``hold`` is how
    long it keeps the chat, ``on_end`` what its turn did."""

    def __init__(self, chat_id: str, hold: float = 0.0, on_end=None):
        import asyncio
        from core.events.stream_pump import _active_pumps
        self.is_done = False

        async def _run():
            await asyncio.sleep(hold)
            if on_end:
                on_end()
            self.is_done = True
            if _active_pumps.get(chat_id) is self and hold:
                _active_pumps.pop(chat_id, None)
        self._task = asyncio.ensure_future(_run())
        _active_pumps[chat_id] = self


@pytest.mark.asyncio
async def test_a_consumer_pump_takes_the_review(review_env, monkeypatch):
    from core.events import pump_bg_monitors
    sid, chat_id = "s-rv4", "c-rv4"
    review_env.bind(chat_id, sid)
    get_bg_command_registry(sid).register_spawn("bR4", "tuR4", label="bR4 — build")
    queued: list[str] = []
    # The monitor's own Path 2 finds no consumer; the review round does.
    monkeypatch.setattr(pump_bg_monitors, "queue_pump_prompt",
                        lambda cid, text, system=False: queued.append(text) or len(queued) > 1)
    held = _HeldPump(chat_id, hold=60)
    layer = _ReviewLayer(_resolver(sid, "bR4"))

    await _monitor(layer, sid, chat_id)
    held._task.cancel()

    assert layer.sends == [] and len(queued) == 2


@pytest.mark.asyncio
async def test_a_pump_that_holds_the_chat_is_waited_for(review_env):
    sid, chat_id = "s-rv5", "c-rv5"
    review_env.bind(chat_id, sid)
    get_bg_command_registry(sid).register_spawn("bR5", "tuR5", label="bR5 — build")
    _HeldPump(chat_id, hold=0.2)
    layer = _ReviewLayer(_resolver(sid, "bR5"))

    await _monitor(layer, sid, chat_id)

    assert len(layer.sends) == 1
    assert (chat_id, "streaming") in review_env.statuses


@pytest.mark.asyncio
async def test_a_turn_that_read_the_completion_meanwhile_ends_the_review(review_env):
    from core.events.bg_command_state import reset_bg_command_registry
    sid, chat_id = "s-rv6", "c-rv6"
    review_env.bind(chat_id, sid)
    get_bg_command_registry(sid).register_spawn("bR6", "tuR6", label="bR6 — build")
    _HeldPump(chat_id, hold=0.2, on_end=lambda: reset_bg_command_registry(sid))
    layer = _ReviewLayer(_resolver(sid, "bR6"))

    await _monitor(layer, sid, chat_id)

    assert layer.sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize("self_wakes, reviewed", [(True, False), (False, True)])
async def test_an_agent_review_after_another_turn_follows_the_engine(
        review_env, monkeypatch, self_wakes, reviewed):
    from core.events import pump_bg_monitors
    monkeypatch.setattr(pump_bg_monitors, "WAKE_GRACE_S", 0.05)
    sid, chat_id = f"s-rv7-{self_wakes}", f"c-rv7-{self_wakes}"
    # The engine decides (``runtime.self_wakes``), read from the chat's.
    review_env.bind(chat_id, sid,
                    execution_path="claude-code-cli" if self_wakes else "codex-cli")
    reg = get_subagent_registry(sid)
    reg.register_spawn("aR7", "tuaR7", label="aR7 — research")
    reg.mark_done("aR7")
    # Holds past the monitor's wake grace (one 0.5 s drain pass), so the
    # review meets the pump.
    _HeldPump(chat_id, hold=2.0)
    layer = _ReviewLayer(lambda: False, self_wakes=self_wakes)

    await _monitor(layer, sid, chat_id, agents=True)

    assert bool(layer.sends) is reviewed


@pytest.mark.asyncio
async def test_a_chat_busy_for_every_round_logs_once(review_env, monkeypatch, caplog):
    import logging
    from core.events import pump_bg_monitors
    monkeypatch.setattr(pump_bg_monitors, "REVIEW_WAIT_S", 0.05)
    sid, chat_id = "s-rv8", "c-rv8"
    review_env.bind(chat_id, sid)
    get_bg_command_registry(sid).register_spawn("bR8", "tuR8", label="bR8 — build")
    held = _HeldPump(chat_id, hold=60)
    layer = _ReviewLayer(_resolver(sid, "bR8"))

    with caplog.at_level(logging.WARNING, logger="claude-proxy"):
        await _monitor(layer, sid, chat_id)
    held._task.cancel()

    assert layer.sends == []
    assert len([r for r in caplog.records if "stayed busy" in r.getMessage()]) == 1


# ---------------------------------------------------------------------------
# A review owed while a run on the chat or its session owes its report: it
# waits for the report window, then gives only what is still owed.
# ---------------------------------------------------------------------------

@pytest.fixture
def fast_window(monkeypatch):
    from core.events import pump_bg_monitors
    monkeypatch.setattr(pump_bg_monitors, "REPORT_POLL_S", 0.01)


async def _release_after(key: str, delay: float, during=None) -> None:
    import asyncio
    from services.scheduler import lanes
    await asyncio.sleep(delay)
    if during:
        during()
    lanes.release_report(key)


@pytest.mark.asyncio
async def test_a_review_waits_for_the_report_window_then_runs(review_env, fast_window):
    import asyncio
    from services.scheduler import lanes
    sid, chat_id = "s-rw1", "c-rw1"
    review_env.bind(chat_id, sid, delegate_role="worker")
    get_bg_command_registry(sid).register_spawn("bW1", "tuW1", label="bW1 — build")
    layer = _ReviewLayer(_resolver(sid, "bW1"))
    lanes.hold_report(chat_id)
    sends_in_window: list[int] = []
    releaser = asyncio.ensure_future(_release_after(
        chat_id, 0.3, during=lambda: sends_in_window.append(len(layer.sends))))

    await _monitor(layer, sid, chat_id)
    await releaser

    assert sends_in_window == [0]                 # nothing ran inside the window
    assert len(layer.sends) == 1 and "bW1 — build" in layer.sends[0]
    assert (chat_id, "streaming") in review_env.statuses   # the chat's own turn


@pytest.mark.asyncio
async def test_a_command_the_runs_producer_reviewed_is_not_reviewed_again(
        review_env, fast_window):
    import asyncio
    from services.scheduler import lanes
    sid, chat_id = "s-rw2", "c-rw2"
    review_env.bind(chat_id, sid, delegate_role="worker")
    reg = get_bg_command_registry(sid)
    reg.register_spawn("bW2", "tuW2", label="bW2 — build")
    layer = _ReviewLayer(_resolver(sid, "bW2"))
    lanes.hold_report(sid)
    releaser = asyncio.ensure_future(_release_after(sid, 0.3, during=reg.clear_unsurfaced))

    await _monitor(layer, sid, chat_id)
    await releaser

    assert layer.sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize("producer_named_it", [True, False])
async def test_an_inherited_codex_agent_is_reviewed_exactly_once(
        review_env, fast_window, producer_named_it):
    import asyncio
    from core.session.session_state import get_subagent_registry
    from services.scheduler import lanes
    sid, chat_id = f"s-rw3-{producer_named_it}", f"c-rw3-{producer_named_it}"
    review_env.bind(chat_id, sid, delegate_role="worker", execution_path="codex-cli")
    reg = get_subagent_registry(sid)
    reg.register_spawn("sub-c", "sub-c", label="audit")
    reg.mark_done("sub-c")
    layer = _ReviewLayer(lambda: False)
    lanes.hold_report(chat_id)
    during = (lambda: reg.mark_reviewed({"sub-c"})) if producer_named_it else None
    releaser = asyncio.ensure_future(_release_after(chat_id, 0.3, during=during))

    await _monitor(layer, sid, chat_id, agents=True)
    await releaser

    assert len(layer.sends) == (0 if producer_named_it else 1)
    assert "sub-c" in reg.reviewed


@pytest.mark.asyncio
async def test_a_claude_agent_is_not_reviewed_after_the_window(review_env, fast_window):
    import asyncio
    from core.session.session_state import get_subagent_registry
    from services.scheduler import lanes
    sid, chat_id = "s-rw4", "c-rw4"
    review_env.bind(chat_id, sid, delegate_role="worker", execution_path="claude-code-cli")
    reg = get_subagent_registry(sid)
    reg.register_spawn("sub-k", "tu-k", label="probe")
    reg.mark_done("sub-k")
    layer = _ReviewLayer(lambda: False)
    lanes.hold_report(chat_id)
    releaser = asyncio.ensure_future(_release_after(chat_id, 0.3))

    await _monitor(layer, sid, chat_id, agents=True)
    await releaser

    assert layer.sends == []
