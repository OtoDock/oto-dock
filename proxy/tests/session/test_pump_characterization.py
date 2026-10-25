"""Characterization (golden master) of ``ChatStreamPump``: the event
dispatcher (``_process_event``), the hook-item handler (``_handle_perm_event``)
and the loop with its end (``_run``).

The recordings under ``tests/fixtures/characterization/`` (the README there
names each one's source): raw Claude Code stream-json captures replayed
through the real translator, a composed Codex app-server turn through the
real Codex translator, the hook items the proxy's own routes put on the
permission queue, and the events other modules inject. Two kinds of test:

- a **trace**: every step (one event or one hook item) handed to the
  dispatcher, then the frames every attached viewer received, the turn's
  blocks, the live state and the pump's own counters snapshotted;
- a **run**: the loop driven to its end from prefilled queues, then the
  frames, the persisted rows, the chat row, the usage rows and every side
  call (the sidebar status, the alert, the title request, the account rest,
  the library reconcile, the stored-wake replay) compared.

The goldens were written once from the tree BEFORE the pump was decomposed
(``CHARACTERIZATION_WRITE=1``) and are never rewritten by a refactor.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_pump_characterization.py -q
"""
import asyncio
import contextlib
import copy
import json

import pytest

from core.events import input_queue, stream_pump
from core.events.common_events import (
    ARTIFACT_TURN,
    BG_REVIEW_TURN,
    DONE,
    PRODUCER_DONE,
    QUEUE_TURN,
    SUBAGENT_START,
    TEXT,
    TOOL_INPUT,
    TOOL_USE,
    CommonEvent,
    TurnInput,
)
from core.events.stream_pump import (
    STEER_ACCEPTED,
    ChatStreamPump,
    _active_pumps,
    _chat_streaming_state,
    _pending_permissions,
)
from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.layers.codex.layer import CodexEventTranslator
from core.layers.codex.session import CodexEvent
from core.session import engine_prompts, session_kind, session_state
from core.session.session_state import get_session_mode, get_subagent_registry
from core.session.transcript_tool_events import result_summary
from storage import database as task_store
from storage.pg import get_conn
from tests.fixtures.characterization.golden_master import (
    check_golden,
    load_json,
    load_jsonl,
)

pytestmark = pytest.mark.asyncio

CLI_WAKE = "../cli_wake/probe-%s.jsonl"   # the background-work captures beside this folder


# ─── the seams the pump calls, recorded ─────────────────────────────────────

class _Seams:
    def __init__(self) -> None:
        self.status: list = []
        self.ephemeral: list = []
        self.titles: list = []
        self.rests: list = []
        self.reconciles: list = []
        self.replays: list = []
        self.kept: list = []
        self.tool_starts: list = []
        self.relocated: list = []
        # The manifests the TOOL branches read: none, unless a trace plants
        # one (an MCP manifest edited elsewhere must not move a golden).
        self.manifests: dict = {}
        self.costs: dict = {}

    def snapshot(self) -> dict:
        return copy.deepcopy({
            "status": self.status, "ephemeral": self.ephemeral, "titles": self.titles,
            "rests": self.rests, "reconciles": self.reconciles, "replays": self.replays,
            "kept": self.kept, "tool_starts": self.tool_starts, "relocated": self.relocated,
        })


@pytest.fixture
def seams(monkeypatch):
    """Every module-level seam the pump reaches, at the module that reads
    it; the delta and workflow coalescing windows at zero so every frame
    goes out at once (the windows depend on the clock)."""
    from services import title_generator
    from services.engines import subscription_pool
    from services.knowledge import library_projector
    from services.scheduler import run_recovery

    s = _Seams()
    monkeypatch.setattr(stream_pump, "_DELTA_FLUSH_S", 0.0)
    monkeypatch.setattr(stream_pump, "_WORKFLOW_PROGRESS_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(stream_pump.notification_manager, "broadcast_chat_status",
                        lambda *a, **kw: s.status.append([list(a), kw]))

    async def _ephemeral(*a, **kw):
        s.ephemeral.append([list(a), kw])
    monkeypatch.setattr(stream_pump.notification_manager, "fire_ephemeral", _ephemeral)

    async def _title(chat_id, *, assistant_excerpt=""):
        s.titles.append([chat_id, assistant_excerpt[:60]])
    monkeypatch.setattr(title_generator, "request_chat_title", _title)

    def _rest(session_id, typed, msg):
        s.rests.append([session_id, typed.as_dict() if typed is not None else None, msg])
    monkeypatch.setattr(subscription_pool, "rest_after_limit", _rest)
    monkeypatch.setattr(subscription_pool, "session_cost_billed", lambda sid: True)
    monkeypatch.setattr(library_projector, "schedule_reconcile_for_agent", s.reconciles.append)

    async def _replay(chat_id):
        s.replays.append(chat_id)
    monkeypatch.setattr(stream_pump, "_replay_stored_wakes", _replay)
    monkeypatch.setattr(run_recovery, "is_recovery_eligible", lambda cid: False)

    from services.mcp import (
        mcp_cost_engine,
        mcp_output_relocation,
        mcp_registry,
        result_files,
    )
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: dict(s.manifests))
    monkeypatch.setattr(mcp_cost_engine, "find_costs_block_for_tool", s.costs.get)
    monkeypatch.setattr(mcp_cost_engine, "evaluate",
                        lambda mcp, tool, tool_input, block: block)
    monkeypatch.setattr(mcp_output_relocation, "record_tool_start",
                        lambda sid, mcp: s.tool_starts.append([sid, mcp]))

    async def _relocate(sid, mcp, tool, result_text=None):
        s.relocated.append([sid, mcp, tool, result_text])
        return []
    monkeypatch.setattr(mcp_output_relocation, "relocate_and_push_for_tool", _relocate)

    async def _keep(sid, tool_name, blocks, text="", *, is_error=False):
        s.kept.append([sid, tool_name, blocks, text, is_error])
        return None
    monkeypatch.setattr(result_files, "keep_blocks", _keep)
    yield s


# ─── events from the recordings ─────────────────────────────────────────────

def _claude_events(name: str, sid: str) -> tuple[list[CommonEvent], dict]:
    """The CommonEvents the real translator makes of a raw capture, and the
    tool results the capture's own ``user`` lines carry (what the PostToolUse
    hook forwards as a ``tool_result`` item)."""
    tr = ClaudeCLIEventTranslator(sid)
    events: list[CommonEvent] = []
    results: dict[str, tuple[str, bool]] = {}
    for frame in load_jsonl(name):
        if frame.get("type") == "user":
            for b in (frame.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else "\n".join(
                        x.get("text", "") for x in (c or []) if isinstance(x, dict))
                    results[b.get("tool_use_id", "")] = (text, bool(b.get("is_error")))
        for chunk in tr.feed(frame):
            events.extend(cli_chunk_to_events(chunk))
        if frame.get("type") == "result":
            # The turn's end is the layer's, not the translator's: the
            # session yields DONE once the settle lets the turn go.
            events.append(CommonEvent(type=DONE, data={}))
    return events, results


def _with_hook_items(events: list[CommonEvent], results: dict) -> list[tuple]:
    """The capture's events with the hook's ``tool_result`` item after the
    tool's input (or after a subagent's spawn), where the PostToolUse hook
    lands in a real turn: before the CLI's own tool_end line."""
    steps: list[tuple] = []
    for ev in events:
        steps.append(("event", ev))
        if ev.type in (TOOL_INPUT, SUBAGENT_START):
            tid = ev.data.get("tool_id") or ev.data.get("tool_use_id") or ""
            if tid in results:
                text, is_error = results[tid]
                name = ev.data.get("name") or ("Agent" if ev.type == SUBAGENT_START else "")
                steps.append(("perm", {
                    "event_type": "tool_result", "tool_name": name, "tool_use_id": tid,
                    "summary": result_summary(name, text), "result_content": text,
                    "is_error": is_error,
                }))
    return steps


def _codex_events(sid: str) -> list[CommonEvent]:
    tr = CodexEventTranslator(model="gpt-6.1-sol", supervised_bg=True, session_id=sid,
                              supervised_bg_commands=True)
    events: list[CommonEvent] = []
    for method, params in load_json("codex-turn.json"):
        events.extend(tr.translate(CodexEvent(type=method, data=params)))
    return events


def _injected(name: str) -> CommonEvent:
    ev = load_json("injected-events.json")[name]
    return CommonEvent(type=ev["type"], data=copy.deepcopy(ev["data"]))


def _perm(name: str, **override) -> dict:
    item = copy.deepcopy(load_json("perm-items.json")[name])
    item.update(override)
    return item


# ─── snapshots ──────────────────────────────────────────────────────────────

def _drain(q: asyncio.Queue) -> list:
    out = []
    while True:
        try:
            out.append(copy.deepcopy(q.get_nowait()))
        except asyncio.QueueEmpty:
            return out


def _live(chat_id: str):
    live = _chat_streaming_state.get(chat_id)
    if live is None:
        return None
    out = copy.deepcopy(live)
    out.pop("started_at", None)
    return out


def _pump_state(pump: ChatStreamPump) -> dict:
    sid = pump.session_id
    reg = get_subagent_registry(sid)
    return {
        "pending_text": list(pump._pending_text),
        "thinking_text": pump._thinking_text,
        "active_tools": sorted(pump._active_tools),
        "tool_seen": pump._tool_seen,
        "title_armed": pump._title_armed,
        "todos": copy.deepcopy(pump._current_todos),
        "goal": None if pump._current_goal is stream_pump._GOAL_UNSET else pump._current_goal,
        "context": [pump._context_used, pump._context_max],
        "cache": [pump._cache_read, pump._cache_write],
        "tokens": [pump._input_tokens, pump._output_tokens],
        "cost_delta": round(pump._total_cost_delta, 6),
        "llm_cost_delta": round(pump._llm_cost_delta, 6),
        "mcp_cost_by_key": {f"{k[0]}/{k[1]}": v for k, v in pump._mcp_cost_by_key.items()},
        "last_session_cost": pump._last_session_cost,
        "cumulative_cost": stream_pump._session_cumulative_cost.get(sid),
        "permission_active": (pump._permission_active or {}).get("request_id"),
        "permission_buffer": [p.get("request_id") for p in pump._permission_buffer],
        "pending_permission": (_pending_permissions.get(sid) or {}).get("request_id"),
        "pinged": sorted(pump._pinged),
        "retried": sorted(pump._retried),
        "plan_filename": pump._plan_filename,
        "pending_previews": sorted(pump._pending_previews),
        "gc_snapshots_pending": pump._gc_snapshots_pending,
        "workflows_forwarded": sorted(pump._wf_last_forward),
        "meeting_agent": pump._meeting_agent,
        "session_mode": get_session_mode(sid),
        "has_pending_prompt": session_state.has_pending_prompt(sid),
        "engine_prompts": sorted(p.request_id for p in engine_prompts.pending_for_chat(pump.chat_id)),
        "registry": {"spawned": sorted(reg.spawned), "completed": sorted(reg.completed),
                     "has_pending": reg.has_pending},
        "last_error": pump.last_error,
        "last_ending": pump.last_ending.as_dict() if pump.last_ending else None,
        "completed": pump._completed,
        "done": pump._done,
        "abort_requested": pump._abort_requested,
        "accepts_in_flight": pump.accepts_in_flight,
        "steer_saved_ranges": list(pump._steer_saved_ranges),
        "recovery_eligible": pump._recovery_eligible,
        "queue_closed": pump.queue_closed,
        "leftover": [it.text for it in pump._leftover_messages],
        "viewers": [[q.bounded, q.overflowed, q.qsize()] for q in pump._ws_queues],
    }


def _rows(chat_id: str) -> list[dict]:
    out = []
    for r in task_store.get_chat_messages(chat_id):
        ed = r.get("event_data")
        if ed:
            with contextlib.suppress(TypeError, ValueError):
                ed = json.loads(ed)
        out.append({"role": r.get("role"), "content": r.get("content"),
                    "event_type": r.get("event_type"), "event_data": ed,
                    "author_sub": r.get("author_sub")})
    return out


_CHAT_KEYS = ("total_cost", "context_used", "context_max", "cache_read", "cache_write",
              "input_tokens", "output_tokens", "permission_mode", "codex_thread_id",
              "thread_goal", "title_generated", "engine_cost_total", "engine_cost_session_id",
              "last_turn_aborted", "last_abort_graceful", "plan_status")


def _chat_row(chat_id: str) -> dict:
    row = task_store.get_chat(chat_id) or {}
    out = {k: row.get(k) for k in _CHAT_KEYS if k in row}
    if isinstance(out.get("total_cost"), float):
        out["total_cost"] = round(out["total_cost"], 6)
    if isinstance(out.get("engine_cost_total"), float):
        out["engine_cost_total"] = round(out["engine_cost_total"], 6)
    out["last_response_at"] = bool(row.get("last_response_at"))
    return out


_USAGE_COLS = ("user_sub", "agent", "scope", "source_type", "source_id", "cost_usd", "input_tokens",
               "output_tokens", "cache_read", "cache_write", "message_count", "provider", "source_key",
               "model")


def _usage_rows(chat_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_USAGE_COLS)} FROM usage_records WHERE source_id=%s ORDER BY id",
            (chat_id,)).fetchall()
    out = []
    for r in rows:
        row = {k: r[k] for k in _USAGE_COLS}
        row["cost_usd"] = round(float(row["cost_usd"] or 0), 6)
        out.append(row)
    return out


async def _settle(pump: ChatStreamPump | None = None) -> None:
    """The spawned pings and fan-outs of the last step (every seam they
    reach is a recorder that returns at once)."""
    for _ in range(3):
        await asyncio.sleep(0)
    if stream_pump._pings:
        await asyncio.gather(*list(stream_pump._pings), return_exceptions=True)
    for _ in range(3):
        await asyncio.sleep(0)


def _rel(value: int, base: int) -> int:
    """A row id as its rank from the chat's first row (1-based); a value
    below the first row (the 0 of "no cutoff") stays."""
    return value - base + 1 if value >= base else value


_ID_KEYS = ("message_id", "message_ids", "since_id")


def _rel_ids(value, base: int):
    if isinstance(value, dict):
        return {k: ([_rel(v, base) for v in val] if k == "message_ids" and isinstance(val, list)
                    else _rel(val, base) if k in _ID_KEYS and isinstance(val, int)
                    else _rel_ids(val, base))
                for k, val in value.items()}
    if isinstance(value, list):
        return [_rel_ids(v, base) for v in value]
    return value


def _label(kind: str, payload) -> str:
    if kind == "event":
        d = payload.data
        extra = d.get("subtype") or d.get("phase") or d.get("name") or d.get("action") or ""
        return f"event:{payload.type}" + (f"/{extra}" if extra else "")
    if kind == "burst":
        return "burst:" + ",".join(ev.type for ev in payload)
    if kind in ("resolve", "attach"):
        return kind
    return f"perm:{payload.get('event_type')}"


# ─── the pump under test ────────────────────────────────────────────────────

def _mk_pump(chat_id: str, *, sid: str, source_type: str = "chat", events=(),
             perm_items=(), **kw) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    eq: asyncio.Queue = asyncio.Queue()
    for ev in events:
        eq.put_nowait(ev)
    pq: asyncio.Queue | None = None
    if perm_items:
        pq = asyncio.Queue()
        for item in perm_items:
            pq.put_nowait(item)
    return ChatStreamPump(chat_id=chat_id, session_id=sid, producer=producer,
                          event_queue=eq, perm_queue=pq, source_type=source_type, **kw)


#: The hook items that hold a turn on a person: the hook route opens their
#: wait before the item rides the queue, and the pump's ping reads that wait.
_BLOCKING_ITEMS = ("permission_prompt", "question_prompt", "plan_review")


def _open_wait(item: dict, sid: str, opened: list[str]) -> None:
    rid = str(item.get("request_id") or "")
    if item.get("event_type") in _BLOCKING_ITEMS and rid and rid not in opened:
        session_state.open_permission_wait(rid, sid)
        opened.append(rid)


def _cleanup(pump: ChatStreamPump, opened: list[str] = ()) -> None:
    pump.producer.cancel()
    _active_pumps.pop(pump.chat_id, None)
    _chat_streaming_state.pop(pump.chat_id, None)
    _pending_permissions.pop(pump.session_id, None)
    stream_pump._session_cumulative_cost.pop(pump.session_id, None)
    stream_pump._stored_wake_chats.discard(pump.chat_id)
    session_state._subagent_registries.pop(pump.session_id, None)
    for p in engine_prompts.pending_for_chat(pump.chat_id):
        engine_prompts.withdraw(p.request_id)
    for rid in opened:
        # A wait opened with no waiter would outlive the test (a later
        # ``wait_for_permission`` of the same id would adopt it).
        session_state._permission_events.pop(rid, None)
        session_state._question_events.pop(rid, None)


async def _trace(name: str, chat_id: str, sid: str, steps: list[tuple], seams: _Seams,
                 *, source_type: str = "chat", no_live: bool = False, viewer: bool = True,
                 settle_s: float = 0.0) -> None:
    task_store.create_chat(chat_id, "user-admin", "a1", source_type=source_type)
    pump = _mk_pump(chat_id, sid=sid, source_type=source_type)
    pump._init_live_state()
    if no_live:
        _chat_streaming_state.pop(chat_id, None)
    q = pump.attach(bounded=True) if viewer else None
    trace = []
    events = []
    opened: list[str] = []
    try:
        for kind, payload in steps:
            if kind == "event":
                events.append([payload.type, copy.deepcopy(payload.data)])
                await pump._process_event(payload)
            elif kind == "burst":
                # Several events with no settling between them (the delta
                # and workflow windows of the clock trace).
                for ev in payload:
                    events.append([ev.type, copy.deepcopy(ev.data)])
                    await pump._process_event(ev)
            elif kind == "resolve":
                await pump.resolve_active_permission()
            elif kind == "attach":
                q = pump.attach(bounded=True)
            else:
                _open_wait(payload, sid, opened)
                await pump._handle_perm_event(payload)
            await _settle(pump)
            if settle_s:
                await asyncio.sleep(settle_s)   # the clock trace's flush timers
            trace.append({
                "step": _label(kind, payload),
                "frames": _drain(q) if q is not None else [],
                "blocks": copy.deepcopy(pump._turn_blocks),
                "live": _live(chat_id),
                "state": _pump_state(pump),
            })
        # The turn's saves landed: the rows as a reload shows them.
        from core.events import chat_writer
        await chat_writer.drain(chat_id, timeout=5.0)
        check_golden(f"pump-trace-{name}", {
            "events": events, "trace": trace, "rows": _rows(chat_id),
            "chat": _chat_row(chat_id), "seams": seams.snapshot(),
        })
    finally:
        _cleanup(pump, opened)


async def _run(name: str, chat_id: str, sid: str, events: list, seams: _Seams, *,
               perm_items=(), source_type: str = "chat", before=None, extra: dict | None = None,
               **kw) -> None:
    task_store.create_chat(chat_id, "user-admin", "a1", source_type=source_type)
    pump = _mk_pump(chat_id, sid=sid, source_type=source_type, events=events,
                    perm_items=perm_items, **kw)
    _active_pumps[chat_id] = pump
    q = pump.attach(bounded=True)
    try:
        if before is not None:
            before(pump)
        await pump.start()
        await _settle(pump)
        # The row ids a frame or a steer range carries come from the table's
        # sequence, which the suite's order moves: relative to the chat's
        # first row they are the same in every run.
        ids = [int(r["id"]) for r in task_store.get_chat_messages(chat_id)]
        base = min(ids) if ids else 0
        state = _pump_state(pump)
        state["steer_saved_ranges"] = [[_rel(lo, base), _rel(hi, base)]
                                       for lo, hi in state["steer_saved_ranges"]]
        check_golden(f"pump-run-{name}", {
            "frames": _rel_ids(_drain(q), base), "rows": _rows(chat_id), "chat": _chat_row(chat_id),
            "usage": _usage_rows(chat_id), "seams": seams.snapshot(),
            "state": state, "live_after": _live(chat_id),
            "registered_after": _active_pumps.get(chat_id) is pump,
            "queue_seq": input_queue.get(chat_id).seq, **(extra or {}),
        })
    finally:
        _cleanup(pump)


# ─── traces: the recordings through the dispatcher ──────────────────────────

@pytest.mark.parametrize("capture", ["claude-tools", "claude-subagent", "claude-todo", "claude-plan"])
async def test_trace_claude_capture(temp_db, seams, capture):
    sid = f"sid-pc-{capture}"
    events, results = _claude_events(f"{capture}.jsonl", sid)
    await _trace(capture, f"chat-pc-{capture}", sid, _with_hook_items(events, results), seams)


@pytest.mark.parametrize("probe", ["bash", "agent", "race"])
async def test_trace_cli_wake_capture(temp_db, seams, probe):
    """The background-work captures: a backgrounded Bash, a background
    subagent and the user-turn race, turns and all."""
    sid = f"sid-pw-{probe}"
    events, results = _claude_events(CLI_WAKE % probe, sid)
    await _trace(f"cli-wake-{probe}", f"chat-pw-{probe}", sid, _with_hook_items(events, results), seams)


async def test_trace_codex_turn(temp_db, seams):
    sid = "sid-pc-codex"
    steps = [("event", ev) for ev in _codex_events(sid)]
    await _trace("codex-turn", "chat-pc-codex", sid, steps, seams)


async def test_trace_hook_items(temp_db, seams):
    """Every item kind the hook routes put on the queue, around a tool."""
    sid = "sid-pc-hooks"
    steps = [
        ("event", CommonEvent(type=TEXT, data={"content": "Reading the file. "})),
        ("event", CommonEvent(type=TOOL_USE, data={"name": "Read", "tool_id": "toolu_hk_read"})),
        ("event", CommonEvent(type=TOOL_INPUT, data={"name": "Read", "summary": "notes.txt",
                                                     "tool_input": {"file_path": "/users/dev-admin/workspace/notes.txt"}})),
        ("perm", _perm("tool_result", tool_name="Read", tool_use_id="toolu_hk_read")),
        ("event", CommonEvent(type="tool_result", data={"name": "Read", "tool_id": "toolu_hk_read"})),
        ("perm", _perm("question")),
        ("perm", _perm("permission_prompt")),
        ("perm", _perm("permission_prompt_meeting")),
        ("perm", _perm("prompt_retired")),
        ("perm", _perm("prompt_retired_gone")),
        ("perm", _perm("mode_restored")),
        ("perm", _perm("question_prompt")),
        ("perm", _perm("plan_review_named")),
        ("perm", _perm("plan_review_unnamed")),
        ("perm", _perm("images")),
        ("perm", _perm("image_generating")),
        ("perm", _perm("images")),
        ("perm", _perm("image_generating")),
        ("perm", _perm("image_gen_failed")),
        ("perm", _perm("url")),
        ("perm", _perm("file")),
        ("perm", _perm("media_processing")),
        ("perm", _perm("video_url")),
        ("perm", _perm("media_processing")),
        ("perm", _perm("media_failed")),
        ("perm", _perm("audio_token")),
        ("perm", _perm("document_preview")),
        ("perm", _perm("document_preview_again")),
        ("perm", _perm("ui")),
        ("event", CommonEvent(type=TEXT, data={"content": "Done."})),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    await _trace("hook-items", "chat-pc-hooks", sid, steps, seams)


async def test_trace_hook_items_unwatched(temp_db, seams):
    """The same prompts with no viewer attached: the parked cards and the
    pings a driver that tells nobody gets."""
    sid = "sid-pc-hooks-unwatched"
    steps = [
        ("perm", _perm("permission_prompt")),
        ("perm", _perm("question_prompt")),
        ("perm", _perm("prompt_retired", request_id="req-perm-0001")),
        ("perm", _perm("permission_prompt", request_id="req-perm-0003")),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    await _trace("hook-items-unwatched", "chat-pc-hooks-unwatched", sid, steps, seams, viewer=False)


async def test_trace_injected_events(temp_db, seams):
    sid = "sid-pc-injected"
    names = [
        "delegate_spawn", "check_verdict", "permission_request", "goal_update", "goal_update",
        "goal_cleared", "context_compact_started", "context_compact_completed",
        "context_compact_usage", "metadata_codex_thread", "metadata_cli_1", "metadata_cli_2",
        "metadata_cli_reset", "metadata_delta", "todo_update_cli", "todo_update_cli",
        "todo_update_live", "workflow_start", "workflow_progress", "workflow_progress",
        "workflow_end", "plan_mode_enter", "plan_mode_exit", "system_transient", "system_other",
    ]
    steps = [("event", _injected(n)) for n in names]
    steps.append(("event", CommonEvent(type=DONE, data={})))
    await _trace("injected", "chat-pc-injected", sid, steps, seams)


async def test_trace_meeting(temp_db, seams):
    """A meeting pump: the speaker stamps, the saves at each turn end."""
    sid = "sid-pc-meeting"
    steps = [
        ("event", _injected("meeting_started")),
        ("event", _injected("meeting_turn_start")),
        ("event", CommonEvent(type=TEXT, data={"content": "The launch is on Monday. "})),
        ("event", CommonEvent(type=TOOL_USE, data={"name": "WebSearch", "tool_id": "toolu_mt_1"})),
        ("event", CommonEvent(type=TOOL_INPUT, data={"name": "WebSearch", "summary": "launch dates",
                                                     "tool_input": {"query": "launch dates"}})),
        ("event", CommonEvent(type="tool_result", data={"name": "WebSearch", "tool_id": "toolu_mt_1"})),
        ("perm", _perm("permission_prompt_meeting")),
        ("perm", _perm("prompt_retired_gone")),
        ("event", _injected("meeting_turn_end")),
        ("event", _injected("meeting_cost")),
        ("event", _injected("meeting_agent_failed")),
        ("event", _injected("meeting_agent_left")),
        ("event", _injected("meeting_turn_start")),
        ("event", CommonEvent(type=TEXT, data={"content": "Agreed."})),
        ("event", _injected("meeting_turn_end")),
        ("event", _injected("meeting_concluded")),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    await _trace("meeting", "chat-pc-meeting", sid, steps, seams,
                 source_type=session_kind.MEETING.source_type)


async def test_trace_meeting_failed(temp_db, seams):
    sid = "sid-pc-meeting-failed"
    steps = [("event", _injected("meeting_started")), ("event", _injected("meeting_failed"))]
    await _trace("meeting-failed", "chat-pc-meeting-failed", sid, steps, seams,
                 source_type=session_kind.MEETING.source_type)


async def test_trace_no_live_state(temp_db, seams):
    """The live dict gone (a chat the death-clear popped): every branch
    guarded on it skips, the blocks and the frames are unchanged."""
    sid = "sid-pc-nolive"
    steps = [
        ("event", CommonEvent(type=TEXT, data={"content": "Hi. "})),
        ("event", CommonEvent(type="thinking", data={"phase": "start"})),
        ("event", CommonEvent(type="thinking", data={"phase": "progress", "estimated_tokens": 40})),
        ("event", CommonEvent(type="thinking", data={"text": "hmm"})),
        ("event", CommonEvent(type="thinking", data={"phase": "end", "text": ""})),
        ("event", CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "toolu_nl_1"})),
        ("event", CommonEvent(type=TOOL_INPUT, data={"name": "Bash", "summary": "ls", "tool_input": {"command": "ls"}})),
        ("event", CommonEvent(type="tool_result", data={"name": "Bash", "tool_id": "toolu_nl_1",
                                                        "result_content": "a\nb\n", "is_error": False})),
        ("event", _injected("delegate_spawn")),
        ("event", _injected("workflow_start")),
        ("event", _injected("workflow_progress")),
        ("event", _injected("todo_update_live")),
        ("event", _injected("goal_update")),
        ("event", _injected("meeting_started")),
        ("perm", _perm("images")),
        ("perm", _perm("document_preview")),
        ("perm", _perm("question")),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    await _trace("no-live", "chat-pc-nolive", sid, steps, seams, no_live=True)


async def test_trace_memory_and_mcp_tools(temp_db, seams):
    """The memory tool's nudge reset and an MCP tool's start and result
    through the registry (no manifest here: the branches run and find none)."""
    sid = "sid-pc-mcp"
    from services.memory import memory_nudge
    memory_nudge._counters[sid] = 3
    steps = [
        ("event", CommonEvent(type=TOOL_USE, data={"name": "mcp__memory-mcp__memory", "tool_id": "toolu_mem_1"})),
        ("event", CommonEvent(type=TOOL_INPUT, data={"name": "mcp__memory-mcp__memory", "summary": "view",
                                                     "tool_input": {"command": "view"}})),
        ("event", CommonEvent(type="tool_result", data={"name": "mcp__memory-mcp__memory", "tool_id": "toolu_mem_1"})),
        ("event", CommonEvent(type=TOOL_USE, data={"name": "memory", "tool_id": "toolu_mem_2"})),
        ("event", CommonEvent(type="tool_result", data={"name": "memory", "tool_id": "toolu_mem_2"})),
        ("event", CommonEvent(type=TOOL_USE, data={"name": "mcp__display__send_url", "tool_id": "toolu_mcp_1"})),
        ("event", CommonEvent(type=TOOL_INPUT, data={"name": "mcp__display__send_url", "summary": "Example",
                                                     "tool_input": {"url": "https://example.com", "title": "Example"}})),
        ("event", CommonEvent(type="tool_result", data={"name": "mcp__display__send_url", "tool_id": "toolu_mcp_1",
                                                        "result_content": "Link sent", "is_error": False})),
        # A result for a tool the pump never saw start: the name fallback.
        ("event", CommonEvent(type="tool_result", data={"name": "Bash", "tool_id": "toolu_unknown",
                                                        "result_content": "late", "is_error": False})),
        ("event", CommonEvent(type=TOOL_USE, data={"name": "Write", "tool_id": "toolu_w_1"})),
        ("event", CommonEvent(type=TOOL_INPUT, data={"name": "Write", "summary": "plan.md",
                                                     "file_path": "C:\\Users\\p\\.claude\\plans\\my-plan.md",
                                                     "tool_input": {"file_path": "C:\\Users\\p\\.claude\\plans\\my-plan.md"}})),
        ("event", CommonEvent(type="tool_result", data={"name": "Write", "tool_id": "toolu_w_1"})),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    try:
        await _trace("memory-and-mcp", "chat-pc-mcp", sid, steps, seams)
    finally:
        memory_nudge._counters.pop(sid, None)


# ─── runs: the loop to its end ──────────────────────────────────────────────

async def test_run_claude_tools_turn(temp_db, seams):
    """A whole Claude turn through the loop: the hook items prefilled (the
    loop takes one per event), the result's metadata, the turn-end job, the
    alert and the title request after the short reply."""
    sid = "sid-pr-tools"
    events, results = _claude_events("claude-tools.jsonl", sid)
    items = [p for k, p in _with_hook_items(events, results) if k == "perm"]
    items.append(_perm("url"))
    items.append(_perm("images"))
    await _run("claude-tools", "chat-pr-tools", sid, [*events, CommonEvent(type=PRODUCER_DONE, data={})],
               seams, perm_items=items, chat_owner="user-admin", chat_agent="a1")


async def test_run_codex_turn(temp_db, seams):
    sid = "sid-pr-codex"
    events = _codex_events(sid)
    await _run("codex-turn", "chat-pr-codex", sid, [*events, CommonEvent(type=PRODUCER_DONE, data={})], seams)


async def test_run_long_reply_title_early(temp_db, seams):
    """A reply past the title threshold: the title request fires from the
    stream, not from the turn's end."""
    sid = "sid-pr-long"
    events = [CommonEvent(type=TEXT, data={"content": "word " * 40}) for _ in range(12)]
    events += [CommonEvent(type=DONE, data={}), CommonEvent(type=PRODUCER_DONE, data={})]
    await _run("long-reply", "chat-pr-long", sid, events, seams)


async def test_run_error_typed(temp_db, seams):
    sid = "sid-pr-err-typed"
    events = [CommonEvent(type=TEXT, data={"content": "Partial "}), _injected("error_typed"),
              CommonEvent(type=TEXT, data={"content": "never read"})]
    await _run("error-typed", "chat-pr-err-typed", sid, events, seams)


async def test_run_error_untyped(temp_db, seams):
    sid = "sid-pr-err-untyped"
    events = [CommonEvent(type=TEXT, data={"content": "Partial "}), _injected("error_untyped")]
    await _run("error-untyped", "chat-pr-err-untyped", sid, events, seams)


async def test_run_queue_steer_artifact(temp_db, seams):
    """The accepted inputs: a drained batch, a steer, the interactions, a
    nudge's text; each lands in stream order with its frame."""
    sid = "sid-pr-queue"
    cid = "chat-pr-queue"
    loop = asyncio.get_running_loop()
    accepted = loop.create_future()
    qi = input_queue.QueuedInput(queue_id="", chat_id=cid, author_sub="user-admin",
                                 item=TurnInput("queued words", image_meta=[{"name": "a.png", "path": "/x/a.png"}]))
    steer = input_queue.QueuedInput(queue_id="", chat_id=cid, author_sub="user-admin",
                                    item=TurnInput("a steer"))
    events = [
        CommonEvent(type=TEXT, data={"content": "First turn. "}),
        CommonEvent(type=DONE, data={}),
        CommonEvent(type=QUEUE_TURN, data={"inputs": [qi], "accepted": accepted}),
        CommonEvent(type=TEXT, data={"content": "Second turn. "}),
        CommonEvent(type=ARTIFACT_TURN, data={"interactions": [
            {"token": "tok-ui-1", "title": "A calculator", "payload": {"x": 1}, "payload_json": '{"x":1}'},
            {"kind": "app_action", "app_id": "app-1", "slug": "calc", "title": "Calc",
             "action_id": "reset", "label": "Reset", "prompt": "Reset the calculator"},
        ], "text": "framed"}),
        CommonEvent(type=TEXT, data={"content": "Third turn. "}),
        CommonEvent(type=QUEUE_TURN, data={"text": "a nudge", "event_data": {"images": [], "files": []}}),
        CommonEvent(type=DONE, data={}),
        CommonEvent(type=PRODUCER_DONE, data={}),
    ]

    events.insert(3, CommonEvent(type=STEER_ACCEPTED,
                                 data={"item": steer, "extra_meta": None, "frame_extra": None}))

    def _before(pump):
        pump.accepts_in_flight += 1      # what record_steer counts for the event above

    await _run("queue-steer-artifact", cid, sid, events, seams, before=_before,
               extra={"accepted": None})
    assert accepted.done() and accepted.result()


async def test_run_review_turn_writes_nudge_rows_in_turn_order(temp_db, seams):
    """A run's review of its background work (the task producer's
    ``BG_REVIEW_TURN``): no person's row, one nudge row per kind after the
    turn's blocks and before the review's, the named jobs' badges cleared,
    each kind's frame flagged ``review``. Asserted, not a golden: the event
    is newer than the recordings."""
    sid = "sid-pr-review"
    cid = "chat-pr-review"
    nudges = [
        {"event_type": "bg_nudge", "count": 2, "labels": ['"probe" [a1]', '"scan" [a2]'],
         "tool_use_ids": ["tu-a1", "tu-a2"]},
        {"event_type": "bg_command_nudge", "count": 1, "labels": ["b1 — make test"],
         "tool_use_ids": ["tu-b1"]},
    ]
    task_store.create_chat(cid, "user-admin", "a1", source_type="task")
    pump = _mk_pump(cid, sid=sid, source_type="task", events=[
        CommonEvent(type=TEXT, data={"content": "Started the jobs."}),
        CommonEvent(type=DONE, data={}),
        CommonEvent(type=BG_REVIEW_TURN, data={"nudges": nudges}),
        CommonEvent(type=TEXT, data={"content": "All three finished."}),
        CommonEvent(type=DONE, data={}),
        CommonEvent(type=PRODUCER_DONE, data={}),
    ])
    _active_pumps[cid] = pump
    q = pump.attach(bounded=True)
    try:
        await pump.start()
        await _settle(pump)
        rows = [(r["role"], r["content"], r["event_type"], r["event_data"]) for r in _rows(cid)]
        assert rows == [
            ("assistant", "Started the jobs.", "", ""),
            ("event", "", "bg_nudge", {"count": 2, "labels": ['"probe" [a1]', '"scan" [a2]']}),
            ("event", "", "bg_command_nudge", {"count": 1, "labels": ["b1 — make test"]}),
            ("assistant", "All three finished.", "", ""),
        ]
        frames = [f["event"] for f in _drain(q) if f.get("pump_type") == "ws_event"]
        flagged = [f for f in frames if f.get("review")]
        assert flagged == [{"type": "bg_agents_complete", "count": 2, "review": True},
                           {"type": "bg_commands_complete", "count": 1, "review": True}]
        # Each kind's jobs are cleared right before its frame.
        boundary = [f for f in frames if f["type"] in (
            "bg_agent_done", "bg_command_done", "bg_agents_complete", "bg_commands_complete")]
        assert boundary == [
            {"type": "bg_agent_done", "tool_use_id": "tu-a1"},
            {"type": "bg_agent_done", "tool_use_id": "tu-a2"},
            {"type": "bg_agents_complete", "count": 2, "review": True},
            {"type": "bg_command_done", "tool_use_id": "tu-b1", "status": ""},
            {"type": "bg_commands_complete", "count": 1, "review": True},
        ]
        assert not [f for f in frames if f["type"] == "queue_sent"]
        # The frames come after the turn before it and ahead of the review's.
        texts = [i for i, f in enumerate(frames) if f["type"] == "text"]
        at = [i for i, f in enumerate(frames) if f.get("review")]
        assert texts[0] < at[0] and at[-1] < texts[-1]
    finally:
        _cleanup(pump)


async def test_review_turn_raises_the_history_cut_over_its_rows(temp_db, seams):
    """The nudge rows are final and no live block holds them: the history's
    cut rises over them, so a reconnect mid-review reads them (only a
    person's row is exempt from the in-flight cut). A review with no kind
    writes nothing."""
    sid = "sid-pr-review-cut"
    cid = "chat-pr-review-cut"
    task_store.create_chat(cid, "user-admin", "a1", source_type="task")
    pump = _mk_pump(cid, sid=sid, source_type="task")
    _active_pumps[cid] = pump
    q = pump.attach(bounded=True)
    try:
        from core.events import chat_writer
        await pump._on_review_turn(CommonEvent(type=BG_REVIEW_TURN, data={"nudges": []}))
        await chat_writer.drain(cid, timeout=5.0)
        assert _rows(cid) == []
        assert not [f for f in _drain(q) if f.get("pump_type") == "ws_event"]
        pump._db_msg_cutoff_id = 0
        await pump._on_review_turn(CommonEvent(type=BG_REVIEW_TURN, data={"nudges": [
            {"event_type": "bg_command_nudge", "count": 3, "labels": []}]}))
        # The handler returns with the row written and the cut raised, so
        # its frame never reaches a viewer before the row it announces.
        [row] = task_store.get_chat_messages(cid)
        assert row["event_type"] == "bg_command_nudge"
        assert pump._db_msg_cutoff_id == int(row["id"])
    finally:
        _cleanup(pump)


async def test_review_turn_counts_its_rows_in_flight(temp_db, seams):
    """The review's rows are an accept job like a queued turn's: counted in
    ``accepts_in_flight`` while the lane writes them, so a resume at the
    boundary drains the lane before its cut. Both kinds land in one write."""
    import threading

    sid = "sid-pr-review-flight"
    cid = "chat-pr-review-flight"
    task_store.create_chat(cid, "user-admin", "a1", source_type="task")
    pump = _mk_pump(cid, sid=sid, source_type="task")
    _active_pumps[cid] = pump
    pump.attach(bounded=True)
    held = threading.Event()
    try:
        from core.events import chat_writer
        lane = chat_writer.submit(cid, lambda: held.wait(10), label="held")
        review = asyncio.ensure_future(pump._on_review_turn(CommonEvent(
            type=BG_REVIEW_TURN, data={"nudges": [
                {"event_type": "bg_nudge", "count": 1, "labels": []},
                {"event_type": "bg_command_nudge", "count": 2, "labels": []}]})))
        for _ in range(100):
            if pump.accepts_in_flight:
                break
            await asyncio.sleep(0.01)
        assert pump.accepts_in_flight == 1 and not review.done()
        assert _rows(cid) == []
        held.set()
        await lane
        await asyncio.wait_for(review, timeout=5)
        assert pump.accepts_in_flight == 0
        assert [r["event_type"] for r in _rows(cid)] == ["bg_nudge", "bg_command_nudge"]
    finally:
        held.set()
        _cleanup(pump)


async def test_run_deleting(temp_db, seams):
    sid = "sid-pr-deleting"
    events = [CommonEvent(type=TEXT, data={"content": "Going. "}), CommonEvent(type=DONE, data={}),
              CommonEvent(type=PRODUCER_DONE, data={})]

    def _before(pump):
        pump.deleting = True
    await _run("deleting", "chat-pr-deleting", sid, events, seams, before=_before)


async def test_run_stopped_with_stored_wake_and_plan(temp_db, seams):
    sid = "sid-pr-stopped"
    cid = "chat-pr-stopped"
    events = [CommonEvent(type=TEXT, data={"content": "Half. "}), CommonEvent(type=PRODUCER_DONE, data={})]

    def _before(pump):
        pump.stopped = True
        stream_pump.note_stored_wake(cid)
    await _run("stopped-wake-plan", cid, sid, events, seams, before=_before,
               implementing_plan="plan-1.md")


async def test_run_background_pending_residual(temp_db, seams):
    """A background subagent still running at the turn's end: the live
    state keeps the badges, the alert waits for the review turn."""
    sid = "sid-pr-bg"
    events = [CommonEvent(type=TEXT, data={"content": "Launched. "}), CommonEvent(type=DONE, data={}),
              CommonEvent(type=PRODUCER_DONE, data={})]

    def _before(pump):
        get_subagent_registry(sid).register_spawn("task-bg-1", "toolu_bg_1")
    await _run("background-pending", "chat-pr-bg", sid, events, seams, before=_before)


async def test_run_superseded(temp_db, seams):
    """A newer pump owns the chat when this one ends: no end stamp, no
    ready broadcast, the live state left to the owner."""
    sid = "sid-pr-sup"
    cid = "chat-pr-sup"
    events = [CommonEvent(type=TEXT, data={"content": "Old. "}), CommonEvent(type=PRODUCER_DONE, data={})]
    newer = None

    def _before(pump):
        nonlocal newer
        newer = _mk_pump(cid, sid=sid + "-new")
        _active_pumps[cid] = newer
    try:
        await _run("superseded", cid, sid, events, seams, before=_before)
    finally:
        if newer is not None:
            newer.producer.cancel()
            _active_pumps.pop(cid, None)


async def test_run_task_chat_no_alert(temp_db, seams):
    """A task-driven pump: no alert at its end, the task's title like any."""
    sid = "sid-pr-task"
    events = [CommonEvent(type=TEXT, data={"content": "Task output. "}), CommonEvent(type=DONE, data={}),
              CommonEvent(type=PRODUCER_DONE, data={})]
    await _run("task-chat", "task-run-pr-1", sid, events, seams, source_type=session_kind.TASK.source_type)


# ─── the audit's scenarios ──────────────────────────────────────────────────

async def test_trace_permission_gate(temp_db, seams):
    """Two prompts held while a tool runs: the first shows, the second
    buffers, the answer releases the next; a retire of the buffered one, a
    plan review behind a prompt, a resolve with nothing waiting."""
    sid = "sid-pc-gate"
    steps = [
        ("event", CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "toolu_gate_1"})),
        ("perm", _perm("permission_prompt")),
        ("perm", _perm("permission_prompt", request_id="req-perm-0010",
                       tool_input={"command": "git push"})),
        ("perm", _perm("plan_review_named", request_id="req-plan-0010")),
        ("resolve", None),
        ("perm", _perm("prompt_retired", request_id="req-plan-0010")),
        ("resolve", None),
        ("resolve", None),
        ("perm", _perm("permission_prompt", request_id="req-perm-0011")),
        ("perm", _perm("prompt_retired_gone", request_id="req-perm-0011")),
        ("perm", _perm("permission_prompt", request_id="req-perm-0012",
                       tool_input={"command": "rm -r build"})),
        ("event", CommonEvent(type="tool_result", data={"name": "Bash", "tool_id": "toolu_gate_1"})),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    await _trace("permission-gate", "chat-pc-gate", sid, steps, seams)


async def test_trace_result_files_and_costs(temp_db, seams):
    """A manifest that declares outputs and costs: the tool start recorded,
    the result's blobs kept, the outputs relocated, the fee charged (and
    not for a failed call); a Codex result with pictures."""
    from types import SimpleNamespace
    sid = "sid-pc-costs"
    seams.manifests["image-gen"] = SimpleNamespace(name="image-gen", server_name="image-gen",
                                                   outputs=[SimpleNamespace(path="out")])
    seams.costs["mcp__image-gen__generate"] = (
        "image-gen", "generate",
        SimpleNamespace(amount=0.04, provider="google", model="nano-banana", currency="USD"))
    blocks = [{"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]
    steps = [
        ("event", CommonEvent(type=TOOL_USE, data={"name": "mcp__image-gen__generate", "tool_id": "toolu_ig_1"})),
        ("event", CommonEvent(type=TOOL_INPUT, data={"name": "mcp__image-gen__generate", "summary": "a cat",
                                                     "tool_input": {"prompt": "a cat"}})),
        ("event", CommonEvent(type="tool_result", data={"name": "mcp__image-gen__generate", "tool_id": "toolu_ig_1",
                                                        "result_content": "Saved out/cat.png", "is_error": False,
                                                        "result_blocks": blocks, "result_session_id": "sid-owner"})),
        ("event", CommonEvent(type=TOOL_USE, data={"name": "mcp__image-gen__generate", "tool_id": "toolu_ig_2"})),
        ("event", CommonEvent(type=TOOL_INPUT, data={"name": "mcp__image-gen__generate", "summary": "a dog",
                                                     "tool_input": {"prompt": "a dog"}})),
        ("perm", _perm("tool_result_error", tool_name="mcp__image-gen__generate", tool_use_id="toolu_ig_2")),
        ("event", CommonEvent(type="tool_result", data={"name": "mcp__image-gen__generate", "tool_id": "toolu_ig_2"})),
        ("event", CommonEvent(type="context_compact", data={"phase": "failed", "trigger": "auto"})),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    await _trace("result-files-and-costs", "chat-pc-costs", sid, steps, seams)


async def test_trace_viewer_cap_and_late_attach(temp_db, seams, monkeypatch):
    """A bounded viewer three frames behind is cut and told to resync; a
    viewer attached later gets what follows."""
    monkeypatch.setattr(stream_pump, "_VIEWER_QUEUE_MAX", 3)
    sid = "sid-pc-cap"
    steps = [
        ("burst", [CommonEvent(type=TEXT, data={"content": f"w{i} "}) for i in range(5)]),
        ("event", CommonEvent(type=TEXT, data={"content": "after the cut "})),
        ("attach", None),
        ("event", CommonEvent(type=TEXT, data={"content": "to both "})),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    await _trace("viewer-cap", "chat-pc-cap", sid, steps, seams)


async def test_trace_clock(temp_db, seams, monkeypatch):
    """The two clock windows with a counter for a clock: the text deltas of
    one burst merge into the pending frame the timer flushes, a thinking
    delta flushes the text first, the workflow progress inside its
    interval is not forwarded."""
    import time as _time
    from types import SimpleNamespace
    ticks = {"now": 1000.0}

    def _monotonic() -> float:
        ticks["now"] += 0.01
        return ticks["now"]
    monkeypatch.setattr(stream_pump, "time", SimpleNamespace(monotonic=_monotonic, time=_time.time))
    monkeypatch.setattr(stream_pump, "_DELTA_FLUSH_S", 0.04)
    monkeypatch.setattr(stream_pump, "_WORKFLOW_PROGRESS_MIN_INTERVAL", 0.3)
    sid = "sid-pc-clock"
    steps = [
        ("burst", [CommonEvent(type=TEXT, data={"content": "a "}), CommonEvent(type=TEXT, data={"content": "b "}),
                   CommonEvent(type=TEXT, data={"content": "c "})]),
        ("burst", [CommonEvent(type=TEXT, data={"content": "d "}),
                   CommonEvent(type="thinking", data={"phase": "start"}),
                   CommonEvent(type="thinking", data={"text": "t1"}),
                   CommonEvent(type="thinking", data={"text": "t2"}),
                   CommonEvent(type="thinking", data={"phase": "progress", "estimated_tokens": 10}),
                   CommonEvent(type="thinking", data={"phase": "progress", "estimated_tokens": 20}),
                   CommonEvent(type="thinking", data={"phase": "end", "text": ""})]),
        ("burst", [_injected("workflow_start"), _injected("workflow_progress"), _injected("workflow_progress"),
                   _injected("workflow_end")]),
        ("event", CommonEvent(type=DONE, data={})),
    ]
    await _trace("clock", "chat-pc-clock", sid, steps, seams, settle_s=0.12)


async def test_run_abort(temp_db, seams):
    """A Stop before the loop read its events: the producer gone, the live
    work flipped inactive, the queued events still read, the run status
    closed from the abort."""
    sid = "sid-pr-abort"
    events = [CommonEvent(type=TEXT, data={"content": "cut "}), _injected("delegate_spawn"),
              CommonEvent(type=PRODUCER_DONE, data={})]

    def _before(pump):
        pump.abort()
    await _run("abort", "chat-pr-abort", sid, events, seams, before=_before)


async def test_run_recovery_flush_suppressed(temp_db, seams):
    """A satellite turn marked for re-adoption: the end saves no blocks and
    no cost (the replay writes the whole turn)."""
    sid = "sid-pr-recover"
    cid = "chat-pr-recover"
    events = [CommonEvent(type=TEXT, data={"content": "kept by the replay "}), _injected("metadata_cli_1"),
              CommonEvent(type=PRODUCER_DONE, data={})]

    def _before(pump):
        stream_pump.suppress_recovery_flush(cid)
    try:
        await _run("recovery-suppressed", cid, sid, events, seams, before=_before)
    finally:
        stream_pump._recovery_suppress_flush.discard(cid)


async def test_run_recovery_eligible_steer_ranges(temp_db, seams, monkeypatch):
    """A recovery-eligible turn: the steer's early save notes its row
    range, in the stream and after the loop's last read."""
    from services.scheduler import run_recovery
    monkeypatch.setattr(run_recovery, "is_recovery_eligible", lambda cid: True)
    sid = "sid-pr-ranges"
    cid = "chat-pr-ranges"
    steer = input_queue.QueuedInput(queue_id="", chat_id=cid, author_sub="user-admin",
                                    item=TurnInput("a steer"))
    late = input_queue.QueuedInput(queue_id="", chat_id=cid, author_sub="user-admin",
                                   item=TurnInput("a late steer"))
    events = [
        CommonEvent(type=TEXT, data={"content": "before "}),
        CommonEvent(type=STEER_ACCEPTED, data={"item": steer, "extra_meta": None, "frame_extra": None}),
        CommonEvent(type=TEXT, data={"content": "after "}),
        CommonEvent(type=DONE, data={}),
        CommonEvent(type=PRODUCER_DONE, data={}),
    ]

    def _before(pump):
        pump.accepts_in_flight += 1      # what record_steer counts for the event above
        pump.record_steer(late)          # lands after PRODUCER_DONE: the finally's drain
    await _run("recovery-steer-ranges", cid, sid, events, seams, before=_before)
