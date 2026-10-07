"""The wire catalogue (``ws/wire_events.py``) and its dashboard mirror
(``dashboard/src/api/wireEvents.ts``), bound in lock-step, plus the tables
the pump, the socket loops, the phone translator and the share snapshot
read from it. A frame name is spelled once on each side and this test is
what keeps the two spellings equal.
"""

from __future__ import annotations

import re
import subprocess
import sys

from tests._paths import PROXY_DIR, REPO_ROOT

from core.events import common_events as ce
from ws import wire_events as wire

MIRROR = REPO_ROOT / "dashboard" / "src" / "api" / "wireEvents.ts"


# ---------------------------------------------------------------------------
# The catalogue itself
# ---------------------------------------------------------------------------

def _constants(module, prefix: str = "") -> dict[str, str]:
    return {k: v for k, v in vars(module).items()
            if k.isupper() and isinstance(v, str) and k.startswith(prefix)}


# The constants that alias a frame name for another vocabulary (the pump
# item, hook item, notify, phone, inbound and live-block spellings).
_ALIAS_PREFIXES = ("PUMP_", "ITEM_", "NOTIFY_", "PHONE_", "IN_", "PERSISTED_", "SUBTYPE_")
# UNDELIVERED_QUEUED: a card's ``reason`` value that shares the word "queued".
_ALIASES = {"LIVE_TEXT", "LIVE_THINKING", "LIVE_TOOL", "UNDELIVERED_QUEUED"}


def _frame_constants() -> dict[str, str]:
    return {k: v for k, v in _constants(wire).items()
            if v in wire.FRAMES and k not in _ALIASES and not k.startswith(_ALIAS_PREFIXES)}


def test_every_frame_is_a_constant_and_every_constant_a_frame():
    names = set(wire.FRAMES)
    # Every FRAMES key is the value of an upper-case module constant.
    values = set(_constants(wire).values())
    assert names <= values, sorted(names - values)
    assert len(names) == 115
    # No frame name is spelled twice.
    assert len(_frame_constants()) == len(names)


def test_per_chat_is_derived_from_the_table():
    assert wire.PER_CHAT_FRAMES == frozenset(n for n, f in wire.FRAMES.items() if f.per_chat)
    assert wire.TEXT in wire.PER_CHAT_FRAMES
    assert wire.WARMUP_READY not in wire.PER_CHAT_FRAMES
    # The receiver's hand list had 61 names; thinking_changed (a dead
    # listener) is gone and turn_complete stays origin-routed, never gated.
    assert wire.TURN_COMPLETE not in wire.PER_CHAT_FRAMES
    # The chat's queue frames are chat-routed: they name the chat whose
    # queue they describe, which the view may not be showing. A steer stays
    # in its stream.
    for frame in (wire.QUEUED, wire.QUEUE_REMOVED, wire.QUEUE_SENT, wire.QUEUE_CLEARED,
                  wire.QUEUE_SNAPSHOT):
        assert frame not in wire.PER_CHAT_FRAMES
    assert wire.STEERED in wire.PER_CHAT_FRAMES
    # A retired prompt's card belongs to the viewed chat's stream.
    assert wire.FRAMES[wire.PROMPT_RETIRED] == wire.Frame(True, None)
    assert len(wire.PER_CHAT_FRAMES) == 57


def test_the_persisted_spellings():
    assert wire.persisted_of(wire.TOOL_START) == wire.PERSISTED_TOOL
    assert wire.persisted_of(wire.TOOL_INFO) == wire.PERSISTED_TOOL
    assert wire.persisted_of(wire.TOOL_END) == wire.PERSISTED_TOOL
    assert wire.persisted_of(wire.TEXT) is None          # an assistant row
    assert wire.persisted_of(wire.MEDIA_PROCESSING) is None
    assert wire.persisted_of(wire.TASK_SPAWN) == "task_spawn"
    assert wire.persisted_of("no-such-frame") is None
    assert {f.persisted for f in wire.FRAMES.values() if f.persisted} <= wire.PERSISTED_EVENT_TYPES
    assert {"tool", "bg_nudge", "bg_command_nudge", "schedule_wake", "thinking", "system",
            "metadata", "context_compact", "check_verdict", "ui"} <= wire.PERSISTED_EVENT_TYPES
    assert len(wire.PERSISTED_EVENT_TYPES) == 27


def test_the_rename_table_covers_every_forwarded_common_event():
    forwarded = {
        ce.TEXT, ce.THINKING, ce.TOOL_USE, ce.TOOL_INPUT, ce.TOOL_RESULT,
        ce.PERMISSION_REQUEST, ce.SUBAGENT_START, ce.SUBAGENT_END,
        ce.BG_COMMAND_START, ce.BG_COMMAND_END, ce.WORKFLOW_START, ce.WORKFLOW_PROGRESS,
        ce.WORKFLOW_END, ce.DELEGATE_SPAWN, ce.CHECK_VERDICT,
        ce.PLAN_MODE, ce.SYSTEM, ce.METADATA, ce.TODO_UPDATE, ce.GOAL_UPDATE,
        ce.CONTEXT_COMPACT, ce.ERROR, ce.QUEUE_TURN, ce.PRODUCER_DONE,
    }
    assert set(wire.WIRE_OF_EVENT) == forwarded
    assert set(wire.WIRE_OF_EVENT.values()) <= set(wire.FRAMES)
    # The boundaries that are not frames, and the two names the pump never
    # receives (the hook queue mints `question`, the delivery ladder
    # `delegate_result`).
    assert ce.DONE not in wire.WIRE_OF_EVENT
    assert ce.ARTIFACT_TURN not in wire.WIRE_OF_EVENT
    assert ce.QUESTION not in wire.WIRE_OF_EVENT
    assert ce.DELEGATE_RESULT not in wire.WIRE_OF_EVENT
    # The renames the finding named.
    assert wire.WIRE_OF_EVENT[ce.TOOL_USE] == wire.TOOL_START
    assert wire.WIRE_OF_EVENT[ce.TOOL_RESULT] == wire.TOOL_END
    assert wire.WIRE_OF_EVENT[ce.PERMISSION_REQUEST] == wire.PERMISSION_PROMPT
    assert wire.WIRE_OF_EVENT[ce.SUBAGENT_START] == wire.TASK_SPAWN
    assert wire.WIRE_OF_EVENT[ce.SUBAGENT_END] == wire.BG_AGENT_DONE
    assert wire.WIRE_OF_EVENT[ce.QUEUE_TURN] == wire.QUEUE_SENT
    assert wire.WIRE_OF_EVENT[ce.PRODUCER_DONE] == wire.DONE
    assert set(wire.LIVE_BLOCK_OF_WIRE.values()) <= wire.LIVE_BLOCK_KINDS


def test_the_other_vocabularies_beside_the_wire():
    assert wire.PUMP_TERMINAL == (wire.PUMP_ALL_DONE, wire.PUMP_ENDED, wire.PUMP_ERROR)
    assert wire.ITEM_QUESTION_PROMPT not in wire.FRAMES and wire.ITEM_MODE_RESTORED not in wire.FRAMES
    assert wire.ITEM_KINDS >= {wire.IMAGES, wire.UI, wire.DOCUMENT_PREVIEW, wire.PERMISSION_PROMPT}
    assert wire.NOTIFY_SERVER_KICK.startswith("_")
    assert wire.PHONE_FRAMES == {"warmup_ready", "session", "text", "tool_start", "tool_end", "done", "error"}
    assert wire.SYSTEM_SUBTYPES >= {"session_reseeded", "meeting_started", "bg_wake", "context_compressed"}
    assert wire.REASON_POOL_CAP in wire.WARMUP_FAILED_REASONS
    assert wire.REASON_BELOW_EDITOR in wire.WARMUP_FAILED_REASONS
    assert wire.SEED_REASONS == {"machine_removed", "moved", "engine_switch", "resume_failed",
                                 "retention"}


def test_the_three_error_shapes_share_one_string():
    assert wire.ERROR == wire.PHONE_ERROR == "error"
    assert set(wire.ErrorFrame.__annotations__) == {"chat_id", "message", "reason", "resets_at"}
    assert set(wire.PhoneErrorFrame.__annotations__) == {"turn", "data"}
    assert set(wire.DuplexErrorFrame.__annotations__) == {"reason"}


# ---------------------------------------------------------------------------
# The dispatcher table
# ---------------------------------------------------------------------------

BETWEEN_TURNS_NAMES = {
    "pre_warmup", "warmup", "chat", "artifact_interaction", "app_action", "resume_chat",
    "chat_read", "permission_response", "question_response", "pty_attach", "pty_takeover",
    "pty_input", "pty_resize", "pty_attachments", "location_response", "plan_review_response",
    "mode_change", "model_change", "execution_mode_change", "execution_mode_switch",
    "compact_context", "move_chat", "switch_engine", "probe_liveness", "implement_plan",
    "abort", "cancel_queued", "cancel_all_queued", "client_info", "user_active", "user_idle",
    "focus", "catalog_subscribe", "catalog_unsubscribe", "ping", "close",
}
MID_STREAM_NAMES = {
    "permission_response", "question_response", "location_response", "plan_review_response",
    "chat", "artifact_interaction", "app_action", "cancel_queued", "cancel_all_queued", "abort",
    "resume_chat", "mode_change", "model_change", "ping", "close", "warmup", "pre_warmup",
    "user_active", "user_idle", "focus", "catalog_subscribe", "catalog_unsubscribe", "chat_read",
}


def test_the_inbound_table_names_every_message_the_two_chains_handled():
    assert set(wire.INBOUND) == BETWEEN_TURNS_NAMES
    live = {n for n, spec in wire.INBOUND.items() if spec.mid_stream != wire.MID_STREAM_DROP}
    assert live == MID_STREAM_NAMES
    assert {spec.mid_stream for spec in wire.INBOUND.values()} == {
        wire.MID_STREAM_SAME, wire.MID_STREAM_DETACH, wire.MID_STREAM_END, wire.MID_STREAM_DROP}
    assert wire.INBOUND["close"].mid_stream == wire.MID_STREAM_END
    assert {n for n, s in wire.INBOUND.items() if s.mid_stream == wire.MID_STREAM_DETACH} == {
        "resume_chat", "warmup", "pre_warmup"}


def test_every_handler_in_the_table_is_a_connection_method():
    import ws.dashboard  # noqa: F401  (the mixins assemble at its end)
    from ws.dashboard import DashboardConnection
    import inspect
    for name, spec in wire.INBOUND.items():
        fn = getattr(DashboardConnection, spec.handler, None)
        assert fn is not None, f"{name}: {spec.handler} is not a DashboardConnection method"
        assert inspect.iscoroutinefunction(fn), spec.handler
        params = inspect.signature(fn).parameters
        assert "stream" in params and params["stream"].kind is inspect.Parameter.KEYWORD_ONLY, spec.handler


# ---------------------------------------------------------------------------
# The mirror, in lock-step
# ---------------------------------------------------------------------------

def _ts_const_object(text: str, name: str) -> dict[str, str]:
    m = re.search(rf"export const {name} = \{{(.*?)\}} as const", text, re.S)
    assert m, name
    body = re.sub(r"//[^\n]*", "", m.group(1))
    # A quoted value, or None for an entry that aliases another constant.
    return {k: (v or None) for k, v in
            re.findall(r"([A-Z_][A-Z0-9_]*):\s*(?:'([a-z_]+)'|[A-Z_.]+)", body)}


def _ts_frames(text: str) -> dict[str, tuple[bool, str | None]]:
    m = re.search(r"export const FRAMES: Record<WireType, FrameMeta> = \{(.*?)\n\}", text, re.S)
    assert m
    out = {}
    for name, per_chat, persisted in re.findall(
            r"^\s*([a-z_]+): \{ perChat: (true|false), persisted: (?:'([a-z_]+)'|null) \}", m.group(1), re.M):
        out[name] = (per_chat == "true", persisted or None)
    return out


def test_the_history_delta_declares_what_it_always_carries():
    # The sender always sets these three and the dashboard's
    # ChatHistoryDeltaFrame requires them; the rest stay optional.
    assert {"chat_id", "messages", "since_id"} <= wire.ChatHistoryDeltaFrame.__required_keys__
    assert "has_more" in wire.ChatHistoryDeltaFrame.__optional_keys__
    text = MIRROR.read_text(encoding="utf-8")
    body = re.search(r"export interface ChatHistoryDeltaFrame[^{]*\{(.*?)\n\}", text, re.S).group(1)
    for key in ("chat_id", "since_id"):
        assert re.search(rf"^\s*{key}: ", body, re.M), key


def test_the_dashboard_mirror_equals_the_authority():
    text = MIRROR.read_text(encoding="utf-8")
    ts_wire = _ts_const_object(text, "WIRE")
    assert ts_wire == _frame_constants()
    ts_frames = _ts_frames(text)
    assert ts_frames == {n: (f.per_chat, f.persisted) for n, f in wire.FRAMES.items()}
    ts_persisted = _ts_const_object(text, "PERSISTED")
    assert ts_persisted == {"TOOL": wire.PERSISTED_TOOL, "BG_NUDGE": wire.PERSISTED_BG_NUDGE,
                            "BG_COMMAND_NUDGE": wire.PERSISTED_BG_COMMAND_NUDGE,
                            "SCHEDULE_WAKE": wire.PERSISTED_SCHEDULE_WAKE}
    ts_live = _ts_const_object(text, "LIVE_BLOCK")
    assert {k: v for k, v in ts_live.items() if v} == {"AGENT": wire.LIVE_AGENT, "COMMAND": wire.LIVE_COMMAND,
                                                       "DELEGATE": wire.LIVE_DELEGATE}
    ts_out = _ts_const_object(text, "OUT")
    assert set(ts_out.values()) == set(wire.INBOUND)
    ts_sub = _ts_const_object(text, "SYSTEM_SUBTYPE")
    assert set(ts_sub.values()) >= wire.SYSTEM_SUBTYPES
    synthesised = set(ts_sub.values()) - wire.SYSTEM_SUBTYPES
    assert synthesised == {"no_subscription", "pool_cap", "target_unavailable", "session_error",
                           "below_editor", "bg_agents_completed", "bg_commands_completed"}
    ts_reasons = _ts_const_object(text, "WARMUP_FAILED_REASON")
    assert set(ts_reasons.values()) == wire.WARMUP_FAILED_REASONS
    assert _ts_const_object(text, "UNDELIVERED_REASON") == {
        "QUEUED": wire.UNDELIVERED_QUEUED, "TURN_FAILED": wire.UNDELIVERED_TURN_FAILED,
        "STOPPED": wire.UNDELIVERED_STOPPED}
    from core.events import input_queue
    assert _ts_const_object(text, "QUEUE_WAITING") == {"RECONNECT": input_queue.WAITING_RECONNECT}
    # The artifact kinds' replayable set derives from the facts table in
    # lib/kinds/artifact.ts — bound by tests/core/test_kinds.py (phase 9).
    # Every frame has a payload interface in the union.
    union = re.search(r"export type WireFrame =(.*?)\n\n", text, re.S).group(1)
    members = set(re.findall(r"\b([A-Z][A-Za-z]+Frame)\b", union))
    declared = set(re.findall(r"export interface ([A-Z][A-Za-z]+Frame)\b", text))
    assert members <= declared, sorted(members - declared)
    assert len(re.findall(r"type: typeof WIRE\.[A-Z_]+", text)) == len(wire.FRAMES)


def _ts_interfaces(text: str) -> dict[str, tuple[set[str], list[str]]]:
    """Each TS interface's own keys and the interfaces it extends (an
    ``Omit<X, …>`` parent counts as ``X``)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    out: dict[str, tuple[set[str], list[str]]] = {}
    for m in re.finditer(r"(?:export\s+)?interface\s+(\w+)\s*(?:extends\s+([^{]+?))?\s*\{", text):
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        keys: set[str] = set()
        seg, level = "", 0
        for c in text[m.end():j - 1] + ";":
            level += {"{": 1, "[": 1, "(": 1, "}": -1, "]": -1, ")": -1}.get(c, 0)
            if level == 0 and c in ";\n":
                k = re.match(r"\s*(\w+)\??\s*:", seg)
                if k:
                    keys.add(k.group(1))
                seg = ""
            else:
                seg += c
        parents = [a or b for a, b in re.findall(r"Omit<\s*(\w+)|(\w+)", m.group(2) or "")]
        out[m.group(1)] = (keys, [p for p in parents if p != "Omit"])
    return out


def _ts_keys(name: str, table: dict[str, tuple[set[str], list[str]]]) -> set[str]:
    keys, parents = table[name]
    found = set(keys)
    for parent in parents:
        if parent in table:
            found |= _ts_keys(parent, table)
    return found - {"type"}


# The frames whose keys the 2026-10 release wave changed. A comparison over
# every frame finds older drifts (AbortedFrame.session_id,
# OpenAppFrame.handled, a page-only flag) that predate it; a frame joins this
# set when its keys are reconciled. The queue and prompt frames joined with
# the queue frames' attachments and the steer's event_data declared.
_KEY_BOUND_FRAMES = ("DocumentPreviewFrame", "PromptRetiredFrame", "QueueClearedFrame",
                     "SystemFrame", "QueuedFrame", "QueueRemovedFrame", "QueueSentFrame",
                     "SteeredFrame", "QueueSnapshotFrame", "LiveStateFrame", "ErrorFrame",
                     "PermissionPromptFrame", "PlanReviewFrame", "QuestionFrame",
                     "DelegateResultFrame", "ShareInboxFrame")


def test_the_mirror_declares_the_same_keys_per_frame():
    table = _ts_interfaces(MIRROR.read_text(encoding="utf-8"))
    for name in _KEY_BOUND_FRAMES:
        cls = getattr(wire, name)
        py_keys = set(cls.__required_keys__) | set(cls.__optional_keys__)
        assert py_keys == _ts_keys(name, table), name
    # The parser reads what the frames carry (a guard against a vacuous pass).
    assert {"access_token", "access_token_ttl", "wopi_url", "chat_id"} <= _ts_keys(
        "DocumentPreviewFrame", table)


# ---------------------------------------------------------------------------
# The readers beside the wire
# ---------------------------------------------------------------------------

def test_the_phone_translator_speaks_only_phone_frames():
    from ws.phone import _pump_item_to_phone_ws
    items = [
        {"pump_type": wire.PUMP_WS_EVENT, "event": {"type": wire.TEXT, "content": "hi"}},
        {"pump_type": wire.PUMP_WS_EVENT, "event": {"type": wire.TOOL_START, "name": "Read", "tool_id": "t1"}},
        {"pump_type": wire.PUMP_WS_EVENT, "event": {"type": ce.TOOL_USE, "name": "Read", "tool_id": "t1"}},
        {"pump_type": wire.PUMP_WS_EVENT, "event": {"type": wire.TOOL_END, "tool_id": "t1"}},
        {"pump_type": wire.PUMP_WS_EVENT, "event": {"type": ce.TOOL_RESULT, "tool_id": "t1"}},
        {"pump_type": wire.PUMP_WS_EVENT, "event": {"type": wire.TOOL_RESULT, "tool_use_id": "t1"}},
        {"pump_type": wire.PUMP_WS_EVENT, "event": {"type": "session", "session_id": "s"}},
        {"pump_type": wire.PUMP_WS_EVENT, "event": {"type": wire.THINKING, "phase": "start"}},
        {"pump_type": wire.PUMP_ALL_DONE},
        {"pump_type": wire.PUMP_ENDED},
        {"pump_type": wire.PUMP_ERROR, "message": "boom"},
        {"pump_type": wire.PUMP_IS_DONE},
    ]
    out = [_pump_item_to_phone_ws(i, 7) for i in items]
    assert [o["type"] if o else None for o in out] == [
        "text", "tool_start", "tool_start", "tool_end", "tool_end", "tool_end", "session",
        None, "done", "done", "error", None]
    assert all(o["turn"] == 7 for o in out if o)
    assert {o["type"] for o in out if o} <= wire.PHONE_FRAMES


def test_the_share_snapshot_keeps_the_persisted_tool_kinds():
    from services.sharing import chat_snapshot
    assert chat_snapshot.TOOL_EVENTS == {wire.PERSISTED_TOOL, wire.THINKING, wire.TASK_SPAWN,
                                         wire.BG_COMMAND_SPAWN}
    assert chat_snapshot.TOOL_EVENTS <= wire.PERSISTED_EVENT_TYPES
    assert chat_snapshot.ARTIFACT_EVENTS == {wire.UI, wire.IMAGES, wire.VIDEO, wire.AUDIO,
                                             wire.FILE, wire.URL}
    from core.events import artifact_events
    assert chat_snapshot.ARTIFACT_EVENTS is artifact_events.SHAREABLE  # the table's shareable rows (phase 9)


# ---------------------------------------------------------------------------
# The leaf, and the CLI layer's inverse table
# ---------------------------------------------------------------------------

def test_the_catalogue_is_a_leaf():
    # A fresh interpreter importing the catalogue loads nothing under core.
    # but the CommonEvent names it maps (the execution-layer leaf's closure
    # stays what tests/execution/test_import_direction.py pins).
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, ws.wire_events; print(sorted(m for m in sys.modules if m.startswith(('core', 'ws', 'services', 'storage', 'api'))))"],
        cwd=PROXY_DIR, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "['core', 'core.events', 'core.events.common_events', 'ws', 'ws.wire_events']", out


def test_the_cli_layer_spells_the_inverse_of_the_rename_table():
    # The CLI layer's chunk kinds are a byte twin of the wire names and hold
    # the inverse table by literal (``et == "tool_start" → TOOL_USE`` …); the
    # composition through the catalogue must be the identity for every chunk
    # kind that is a wire name.
    src = (PROXY_DIR / "core" / "layers" / "cli" / "layer.py").read_text(encoding="utf-8")
    pairs = re.findall(r'el?if et == "([a-z_]+)"[^\n]*:\n\s*events\.append\(CommonEvent\(type=([A-Z_]+)', src)
    assert len(pairs) >= 12, pairs
    checked = 0
    for chunk, event_name in pairs:
        if chunk in wire.FRAMES:
            assert wire.WIRE_OF_EVENT[getattr(ce, event_name)] == chunk, (chunk, event_name)
            checked += 1
    assert checked >= 8, checked
