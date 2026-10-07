"""The wire catalogue — every frame the proxy sends a client, named once.

The dashboard socket (``/ws/dashboard``, ``ws/dashboard.py``) speaks JSON
frames ``{"type": <name>, ...}`` in both directions. This module is the one
place their names live: the outbound frames (``TEXT`` … ``WARMUP_READY``,
with a payload ``TypedDict`` each and the ``FRAMES`` table that says
whether a frame belongs to one chat and how it is persisted), the inbound
messages (``IN_WARMUP`` … and the ``INBOUND`` dispatcher table both socket
loops walk), the tables the stream pump reads to turn a ``CommonEvent``
into a frame and a frame into a chat row, and the vocabularies that sit
beside the wire without being on it: the pump's item kinds (``PUMP_*``),
the hook queue's item kinds (``ITEM_*``), the notify queue's internal kinds
(``NOTIFY_*``), the live-block kinds (``LIVE_*``), the persisted row kinds
(``PERSISTED_*``), ``system.subtype`` and the reason codes the frames
carry. The dashboard's mirror is ``dashboard/src/api/wireEvents.ts``; a
proxy-side test reads it and keeps the two equal
(``tests/session/test_wire_events.py``); the acceptance test
(``tests/session/test_wire_surface.py``) refuses a frame name spelled as a
literal anywhere else.

Every string here is FROZEN: a frame name, a payload key and a persisted
``event_type`` are read by every dashboard bundle ever served, by the
phone daemon, by the share host and by every row in ``chat_messages``.
Naming a value here is the fix; renaming it is a wire change and needs
its own plan (a renamed frame took the phone pipeline down on
2026-08-14 — ``ws/phone.py``). A NEW frame is one constant, one
``TypedDict``, one ``FRAMES`` row, one mirror entry and one receiver case.

The sockets, and what each does with a type it does not know:

* **The dashboard socket** — this catalogue. The receiver
  (``hooks/useDashboardWs.ts``) switches on the name, hands the ``pty_*``,
  ``ui``, ``check_verdict``, ``artifact_*`` and ``app_action*`` frames to a
  subscription bus, and warns ONCE per unknown type; a bundle older than
  this phase drops an unknown type silently. ``execution_mode_changed``
  is sent and never acted on (catalogued so it does not warn).
* **The phone socket** (``/ws/phone``, ``ws/phone.py``) — seven frames in
  a ``{"type", "turn", "data": {...}}`` envelope (``PHONE_*``), built from
  the pump's items by ``ws.phone._pump_item_to_phone_ws``; the daemon
  (``phone/proxy/client.py``) ignores a type it does not know.
* **The duplex daemon socket** (``ws/duplex_attach.py``) — the phone
  frames again (the same translator) plus the interactive feed; the
  daemon's ``phone/duplex/connection.py`` routes six names and drops the
  rest.
* **The duplex browser socket** (``/ws/duplex``, ``ws/duplex.py``) — its
  own ten frames (``ready``, ``state``, ``interim``, ``interim_final``,
  ``final``, ``flush``, ``pause``, ``resume``, ``end``, ``error``), listed
  in that module's docstring; ``hooks/useDuplexVoice.ts`` ignores an
  unknown type. Not catalogued here.
* **The share host** (``dashboard/src/sharehost/main.ts``, a separately
  built bundle, and ``pages/SharedChatPage.tsx``) reads no frame: it reads
  the snapshot document ``services/sharing/chat_snapshot.build`` writes
  (``version: 1``), keyed by the persisted ``event_type`` — an unknown
  kind renders as a collapsed card with the raw row.
* **The audio socket** (``ws/audio.py``), **the app socket**
  (``api/apps/app_proxy.py``: ``catalog`` / ``catalog_ack``) and **the
  satellite protocol** (``ws/satellite.py``, ``core/remote/*``) are their
  own protocols and are not catalogued here.

The ``error`` frame is ONE string with THREE shapes — the dashboard's
``ErrorFrame`` (``message``), the phone and duplex daemon sockets'
``PhoneErrorFrame`` (``turn``, ``data.message``) and the duplex browser
socket's ``DuplexErrorFrame`` (``reason``). Unifying them is a wire
change and stays deferred; the three names document the three readers.

A stdlib leaf: ``ws/__init__.py`` is empty, so the pump imports this at
module level without pulling the socket modules.
"""

from __future__ import annotations

from typing import Any, NamedTuple, TypedDict

from core.events import common_events as _ce


# ---------------------------------------------------------------------------
# The outbound frames of the dashboard socket
# ---------------------------------------------------------------------------

# The turn stream (the pump forwards these; every one belongs to one chat).
TEXT = "text"
THINKING = "thinking"
TOOL_START = "tool_start"
TOOL_INFO = "tool_info"
TOOL_END = "tool_end"
TOOL_RESULT = "tool_result"
TASK_SPAWN = "task_spawn"
BG_AGENT_DONE = "bg_agent_done"
BG_COMMAND_SPAWN = "bg_command_spawn"
BG_COMMAND_DONE = "bg_command_done"
BG_AGENTS_COMPLETE = "bg_agents_complete"
BG_COMMANDS_COMPLETE = "bg_commands_complete"
FG_AGENTS_COMPLETE = "fg_agents_complete"
WORKFLOW_START = "workflow_start"
WORKFLOW_PROGRESS = "workflow_progress"
WORKFLOW_END = "workflow_end"
DELEGATE_SPAWN = "delegate_spawn"
DELEGATE_RESULT = "delegate_result"
CHECK_VERDICT = "check_verdict"
PERMISSION_PROMPT = "permission_prompt"
PLAN_REVIEW = "plan_review"
QUESTION = "question"
PROMPT_RETIRED = "prompt_retired"   # a prompt card whose wait ended with no answer
LOCATION_REQUEST = "location_request"
PLAN_MODE = "plan_mode"
SYSTEM = "system"
METADATA = "metadata"
MCP_COST = "mcp_cost"
TODO_UPDATE = "todo_update"
GOAL_UPDATE = "goal_update"
CONTEXT_COMPACT = "context_compact"
DONE = "done"
ERROR = "error"
ABORTED = "aborted"
LIVE_STATE = "live_state"
LIMIT_WARNING = "limit_warning"
LIMIT_REACHED = "limit_reached"

# The artifacts (the hook queue's renderables; ``core/events/artifact_events.py``
# builds them, the pump and the PTY drainer forward them).
IMAGES = "images"
IMAGE_GENERATING = "image_generating"
IMAGE_GEN_FAILED = "image_gen_failed"
URL = "url"
FILE = "file"
VIDEO = "video"
AUDIO = "audio"
MEDIA_PROCESSING = "media_processing"
MEDIA_FAILED = "media_failed"
DOCUMENT_PREVIEW = "document_preview"
UI = "ui"
ARTIFACT_INTERACTION = "artifact_interaction"
ARTIFACT_ACK = "artifact_ack"
APP_ACTION = "app_action"
APP_ACTION_ACK = "app_action_ack"

# The queue and the turn's control frames.
QUEUED = "queued"
QUEUE_REMOVED = "queue_removed"
QUEUE_SENT = "queue_sent"
QUEUE_CLEARED = "queue_cleared"
QUEUE_SNAPSHOT = "queue_snapshot"
STEERED = "steered"
MODE_CHANGED = "mode_changed"
MODEL_CHANGED = "model_changed"
EXECUTION_MODE_CHANGED = "execution_mode_changed"
PLAN_STATUS = "plan_status"
SERVER_TURN_START = "server_turn_start"

# The chat and session lifecycle.
WARMUP_STARTED = "warmup_started"
WARMUP_HEARTBEAT = "warmup_heartbeat"
WARMUP_READY = "warmup_ready"
WARMUP_FAILED = "warmup_failed"
PRE_WARMUP_READY = "pre_warmup_ready"
CHAT_HISTORY = "chat_history"
CHAT_HISTORY_DELTA = "chat_history_delta"
CHAT_STATUS = "chat_status"
CHAT_STATUS_SNAPSHOT = "chat_status_snapshot"
CHAT_READ = "chat_read"
CHAT_ROWS = "chat_rows"
CHAT_META = "chat_meta"
CHAT_MOVED = "chat_moved"
ENGINE_SWITCHED = "engine_switched"
SWITCH_ENGINE_DENIED = "switch_engine_denied"
LIVENESS = "liveness"
TITLE_UPDATED = "title_updated"
TURN_COMPLETE = "turn_complete"

# The interactive terminal (a PTY has no pump; the bus carries these).
PTY_OUTPUT = "pty_output"
PTY_EXIT = "pty_exit"
PTY_PERMISSION = "pty_permission"
PTY_ARTIFACT = "pty_artifact"
PTY_STATUS = "pty_status"

# The user's other screens: notifications, files, apps, machines, transfers.
NOTIFICATION = "notification"
NOTIFICATION_SILENT = "notification_silent"
NOTIFICATION_COUNT = "notification_count"
FILE_UPDATED = "file_updated"
APP_PUSH = "app_push"
APP_STATE = "app_state"
OPEN_APP = "open_app"
APP_DEPLOYED = "app_deployed"
CATALOG = "catalog"
SHARE_INBOX = "share_inbox"
INSTALL_STARTED = "install_started"
INSTALL_MCP_PLAN = "install_mcp_plan"
INSTALL_PROGRESS = "install_progress"
INSTALL_HEARTBEAT = "install_heartbeat"
INSTALL_VERIFYING = "install_verifying"
INSTALL_DONE = "install_done"
INSTALL_FAILED = "install_failed"
MCP_INSTALL_FAILED = "mcp_install_failed"
TRANSFER_STARTED = "transfer_started"
TRANSFER_MACHINE_STATE = "transfer_machine_state"
TRANSFER_PROGRESS = "transfer_progress"
TRANSFER_DONE = "transfer_done"
TRANSFER_STATE = "transfer_state"
SATELLITE_UPDATING = "satellite_updating"
SATELLITE_UPDATED = "satellite_updated"
SATELLITE_UPDATE_FAILED = "satellite_update_failed"
SATELLITE_UPDATE_SYNC = "satellite_update_sync"

# The connection itself.
SERVER_INFO = "server_info"
PONG = "pong"


class Frame(NamedTuple):
    """What the receiver needs to know about a frame besides its name.

    ``persisted`` is true of the pump's path; two frames have a second
    minter that persists nothing — a held Codex ``question`` (re-presented
    from the pending permissions) and the hook queue's ``permission_prompt``
    item (the ``PERMISSION_REQUEST`` event persists the block). A row minter
    (the reseed card, the undelivered-input card, the stale-rejected
    permission, the delegate-spawn fallback) spells the same constant: for
    those the row kind and the frame name coincide.
    """

    per_chat: bool
    """The frame belongs to ONE chat's stream: the receiver drops it when
    its ``chat_id`` names a chat the view is not showing (an untagged frame
    passes). False = a global frame, never filtered."""
    persisted: str | None
    """The ``chat_messages.event_type`` the frame's block is stored under,
    or None when the frame is live-only. ``TEXT`` is the exception in the
    other direction: it is stored as an ``assistant`` row (no event row)."""


# The persisted row kinds (``chat_messages.event_type``): a frame's block is
# stored under its own name except where the row kind differs.
PERSISTED_TOOL = "tool"           # tool_start + tool_info + tool_end, one row
PERSISTED_BG_NUDGE = "bg_nudge"   # the review nudge after background agents
PERSISTED_BG_COMMAND_NUDGE = "bg_command_nudge"
PERSISTED_SCHEDULE_WAKE = "schedule_wake"

FRAMES: dict[str, Frame] = {
    TEXT: Frame(True, None),
    THINKING: Frame(True, THINKING),
    TOOL_START: Frame(True, PERSISTED_TOOL),
    TOOL_INFO: Frame(True, PERSISTED_TOOL),
    TOOL_END: Frame(True, PERSISTED_TOOL),
    TOOL_RESULT: Frame(True, PERSISTED_TOOL),
    TASK_SPAWN: Frame(True, TASK_SPAWN),
    BG_AGENT_DONE: Frame(True, None),
    BG_COMMAND_SPAWN: Frame(True, BG_COMMAND_SPAWN),
    BG_COMMAND_DONE: Frame(True, None),
    BG_AGENTS_COMPLETE: Frame(True, None),
    BG_COMMANDS_COMPLETE: Frame(True, None),
    FG_AGENTS_COMPLETE: Frame(True, None),   # minted by the liveness clear alone; no layer mints the subtype
    WORKFLOW_START: Frame(True, None),
    WORKFLOW_PROGRESS: Frame(True, None),
    WORKFLOW_END: Frame(True, None),
    DELEGATE_SPAWN: Frame(True, DELEGATE_SPAWN),
    DELEGATE_RESULT: Frame(True, DELEGATE_RESULT),
    CHECK_VERDICT: Frame(False, CHECK_VERDICT),
    PERMISSION_PROMPT: Frame(True, PERMISSION_PROMPT),
    PLAN_REVIEW: Frame(True, PLAN_REVIEW),
    QUESTION: Frame(True, QUESTION),
    PROMPT_RETIRED: Frame(True, None),
    LOCATION_REQUEST: Frame(True, None),
    PLAN_MODE: Frame(True, PLAN_MODE),
    SYSTEM: Frame(True, SYSTEM),
    METADATA: Frame(True, METADATA),
    MCP_COST: Frame(True, None),
    TODO_UPDATE: Frame(True, None),
    GOAL_UPDATE: Frame(True, None),
    CONTEXT_COMPACT: Frame(True, CONTEXT_COMPACT),
    DONE: Frame(True, None),
    ERROR: Frame(True, None),
    ABORTED: Frame(True, None),
    LIVE_STATE: Frame(True, None),
    LIMIT_WARNING: Frame(True, None),
    LIMIT_REACHED: Frame(True, None),
    IMAGES: Frame(True, IMAGES),
    IMAGE_GENERATING: Frame(True, IMAGE_GENERATING),   # a turn ending mid-generation freezes the placeholder (phase 9 decides)
    IMAGE_GEN_FAILED: Frame(True, None),
    URL: Frame(True, URL),
    FILE: Frame(True, FILE),
    VIDEO: Frame(True, VIDEO),
    AUDIO: Frame(True, AUDIO),
    MEDIA_PROCESSING: Frame(True, None),
    MEDIA_FAILED: Frame(True, None),
    DOCUMENT_PREVIEW: Frame(True, DOCUMENT_PREVIEW),
    UI: Frame(False, UI),
    ARTIFACT_INTERACTION: Frame(False, ARTIFACT_INTERACTION),
    ARTIFACT_ACK: Frame(False, None),
    APP_ACTION: Frame(False, APP_ACTION),
    APP_ACTION_ACK: Frame(False, None),
    # The chat's queue frames are chat-routed: they reach every socket of the
    # chat's audience whatever it views, and name the chat whose queue they
    # describe (core/events/input_queue.py). A steer stays in its stream.
    QUEUED: Frame(False, None),
    QUEUE_REMOVED: Frame(False, None),
    QUEUE_SENT: Frame(False, None),
    QUEUE_CLEARED: Frame(False, None),
    QUEUE_SNAPSHOT: Frame(False, None),
    STEERED: Frame(True, None),
    MODE_CHANGED: Frame(True, None),
    MODEL_CHANGED: Frame(True, None),
    EXECUTION_MODE_CHANGED: Frame(False, None),
    PLAN_STATUS: Frame(False, None),
    SERVER_TURN_START: Frame(False, None),
    WARMUP_STARTED: Frame(False, None),
    WARMUP_HEARTBEAT: Frame(False, None),
    WARMUP_READY: Frame(False, None),
    WARMUP_FAILED: Frame(False, None),
    PRE_WARMUP_READY: Frame(False, None),
    CHAT_HISTORY: Frame(False, None),
    CHAT_HISTORY_DELTA: Frame(True, None),
    CHAT_STATUS: Frame(False, None),
    CHAT_STATUS_SNAPSHOT: Frame(False, None),
    CHAT_READ: Frame(False, None),
    CHAT_ROWS: Frame(True, None),
    CHAT_META: Frame(True, None),
    CHAT_MOVED: Frame(False, None),
    ENGINE_SWITCHED: Frame(False, None),
    SWITCH_ENGINE_DENIED: Frame(False, None),
    LIVENESS: Frame(False, None),
    TITLE_UPDATED: Frame(False, None),
    TURN_COMPLETE: Frame(False, None),   # origin-routed: it targets chats the view is not showing
    PTY_OUTPUT: Frame(True, None),
    PTY_EXIT: Frame(True, None),
    PTY_PERMISSION: Frame(True, None),
    PTY_ARTIFACT: Frame(True, None),
    PTY_STATUS: Frame(True, None),
    NOTIFICATION: Frame(False, None),
    NOTIFICATION_SILENT: Frame(False, None),
    NOTIFICATION_COUNT: Frame(False, None),
    FILE_UPDATED: Frame(False, None),
    APP_PUSH: Frame(False, None),
    APP_STATE: Frame(False, None),
    OPEN_APP: Frame(False, None),
    APP_DEPLOYED: Frame(False, None),
    CATALOG: Frame(False, None),
    SHARE_INBOX: Frame(False, None),
    INSTALL_STARTED: Frame(False, None),
    INSTALL_MCP_PLAN: Frame(False, None),
    INSTALL_PROGRESS: Frame(False, None),
    INSTALL_HEARTBEAT: Frame(False, None),
    INSTALL_VERIFYING: Frame(False, None),
    INSTALL_DONE: Frame(False, None),
    INSTALL_FAILED: Frame(False, None),
    MCP_INSTALL_FAILED: Frame(False, None),
    TRANSFER_STARTED: Frame(False, None),
    TRANSFER_MACHINE_STATE: Frame(False, None),
    TRANSFER_PROGRESS: Frame(False, None),
    TRANSFER_DONE: Frame(False, None),
    TRANSFER_STATE: Frame(False, None),
    SATELLITE_UPDATING: Frame(False, None),
    SATELLITE_UPDATED: Frame(False, None),
    SATELLITE_UPDATE_FAILED: Frame(False, None),
    SATELLITE_UPDATE_SYNC: Frame(False, None),
    SERVER_INFO: Frame(False, None),
    PONG: Frame(False, None),
}

PER_CHAT_FRAMES: frozenset[str] = frozenset(n for n, f in FRAMES.items() if f.per_chat)

# The ``event_type`` vocabulary of ``chat_messages``: every frame kind that
# is persisted under its own name, the three renamed kinds, and the rows
# the server writes without a frame (the two nudges, the schedule wake).
PERSISTED_EVENT_TYPES: frozenset[str] = frozenset(
    {f.persisted for f in FRAMES.values() if f.persisted}
    | {PERSISTED_BG_NUDGE, PERSISTED_BG_COMMAND_NUDGE, PERSISTED_SCHEDULE_WAKE}
)

# The live-block kinds (``live_state.live_blocks[].type``): what a reconnect
# renders before the rows land. Four differ from their frame and row names.
LIVE_TEXT = TEXT
LIVE_THINKING = THINKING
LIVE_TOOL = PERSISTED_TOOL
LIVE_AGENT = "agent"          # a task_spawn while it runs
LIVE_COMMAND = "command"      # a bg_command_spawn while it runs
LIVE_DELEGATE = "delegate"    # a delegate_spawn while it runs
LIVE_BLOCK_KINDS: frozenset[str] = frozenset({
    LIVE_TEXT, LIVE_THINKING, LIVE_TOOL, LIVE_AGENT, LIVE_COMMAND, LIVE_DELEGATE,
})


# ---------------------------------------------------------------------------
# The tables the pump reads: CommonEvent → wire, wire → block
# ---------------------------------------------------------------------------

# The frame a CommonEvent becomes (``core/events/stream_pump._process_event``
# forwards through ``_forward_event``; a type missing here is not forwarded
# as a frame — DONE is a turn boundary, QUEUE_TURN and ARTIFACT_TURN are
# queue drains handled in ``_run``; QUESTION and DELEGATE_RESULT are names
# the pump never receives: the hook queue mints the ``question`` frame and
# the delivery ladder mints ``delegate_result``). The exceptions the table
# cannot say: TOOL_RESULT also emits a content-carrying ``tool_result`` frame
# before its ``tool_end``; SYSTEM drops ``task_started`` / ``task_progress``;
# METADATA carrying only a Codex thread id emits nothing; a THINKING block
# persists at ``phase == end`` only, a CONTEXT_COMPACT block at
# ``completed`` only, and TODO_UPDATE persists a ``tool`` block named by
# ``tool_roles.TODO_SNAPSHOT``.
WIRE_OF_EVENT: dict[str, str] = {
    _ce.TEXT: TEXT,
    _ce.THINKING: THINKING,
    _ce.TOOL_USE: TOOL_START,
    _ce.TOOL_INPUT: TOOL_INFO,
    _ce.TOOL_RESULT: TOOL_END,
    _ce.PERMISSION_REQUEST: PERMISSION_PROMPT,
    _ce.SUBAGENT_START: TASK_SPAWN,
    _ce.SUBAGENT_END: BG_AGENT_DONE,
    _ce.BG_COMMAND_START: BG_COMMAND_SPAWN,
    _ce.BG_COMMAND_END: BG_COMMAND_DONE,
    _ce.WORKFLOW_START: WORKFLOW_START,
    _ce.WORKFLOW_PROGRESS: WORKFLOW_PROGRESS,
    _ce.WORKFLOW_END: WORKFLOW_END,
    _ce.DELEGATE_SPAWN: DELEGATE_SPAWN,
    _ce.CHECK_VERDICT: CHECK_VERDICT,
    _ce.PLAN_MODE: PLAN_MODE,
    _ce.SYSTEM: SYSTEM,
    _ce.METADATA: METADATA,
    _ce.TODO_UPDATE: TODO_UPDATE,
    _ce.GOAL_UPDATE: GOAL_UPDATE,
    _ce.CONTEXT_COMPACT: CONTEXT_COMPACT,
    _ce.ERROR: ERROR,
    _ce.QUEUE_TURN: QUEUE_SENT,
    _ce.PRODUCER_DONE: DONE,
}

# The live block a frame opens while its work runs (the same dict is the
# ``active_*`` entry and the ``live_blocks`` entry; ``active`` flips off on
# the matching *_done / *_end frame).
LIVE_BLOCK_OF_WIRE: dict[str, str] = {
    TEXT: LIVE_TEXT,
    THINKING: LIVE_THINKING,
    TOOL_START: LIVE_TOOL,
    TASK_SPAWN: LIVE_AGENT,
    BG_COMMAND_SPAWN: LIVE_COMMAND,
    DELEGATE_SPAWN: LIVE_DELEGATE,
}


def persisted_of(frame: str) -> str | None:
    """The row kind a frame's block is stored under (None: live-only; TEXT:
    an assistant row) — ``FRAMES`` asked by name."""
    f = FRAMES.get(frame)
    return f.persisted if f else None


# ---------------------------------------------------------------------------
# The pump's item kinds (``pump_type``): read by the stream loop
# (``ws/dashboard_chat_stream.py``), the phone translator (``ws/phone.py``)
# and the duplex forwarder (``ws/duplex_attach.py``). Never on the wire.
# ---------------------------------------------------------------------------

PUMP_WS_EVENT = "ws_event"                     # {"event": <a frame>}
PUMP_PERMISSION_PROMPT = "perm_permission_prompt"  # {"perm_data", "meeting_agent"?}
PUMP_PLAN_REVIEW = "perm_plan_review"          # {"perm_data", "filename"}
PUMP_QUESTION_PROMPT = "perm_question_prompt"  # {"perm_data"}
PUMP_MODE_RESTORED = "perm_mode_restored"      # {"mode"}
PUMP_ARTIFACT_INTERACTION = ARTIFACT_INTERACTION  # the drained chip, keys as the frame
PUMP_APP_ACTION = APP_ACTION
PUMP_IS_DONE = "is_done"                       # a turn boundary, rows saved
PUMP_ALL_DONE = "all_done"                     # every turn done, rows landed
PUMP_ERROR = "error"                           # {"message", "reason"?, "resets_at"?}
PUMP_DETACHED = "detached"                     # another socket took over
PUMP_ENDED = "pump_ended"                      # the pump finished or died
PUMP_TERMINAL: tuple[str, ...] = (PUMP_ALL_DONE, PUMP_ENDED, PUMP_ERROR)


# ---------------------------------------------------------------------------
# The hook queue's item kinds (``event_type`` of a permission-queue item):
# minted by ``api/hooks/*``, read by the pump's ``_handle_perm_event`` /
# ``_show_permission`` and the PTY drainer (``ws/dashboard_pty.py``). The
# artifact kinds and the three prompts share the frame's name; two do not.
# ---------------------------------------------------------------------------

ITEM_PERMISSION_PROMPT = PERMISSION_PROMPT
ITEM_PLAN_REVIEW = PLAN_REVIEW
ITEM_QUESTION = QUESTION                # Claude's fire-and-forget question card
ITEM_QUESTION_PROMPT = "question_prompt"  # Codex's held question (a request_id)
ITEM_MODE_RESTORED = "mode_restored"    # the plan-mode restore after a review
ITEM_TOOL_RESULT = TOOL_RESULT
# A prompt's wait ended with no answer: {"request_id", "caller_gone"?}; the
# pump frees its slot and forwards PROMPT_RETIRED.
ITEM_PROMPT_RETIRED = PROMPT_RETIRED
ITEM_KINDS: frozenset[str] = frozenset({
    ITEM_PERMISSION_PROMPT, ITEM_PLAN_REVIEW, ITEM_QUESTION, ITEM_QUESTION_PROMPT,
    ITEM_MODE_RESTORED, ITEM_TOOL_RESULT, ITEM_PROMPT_RETIRED,
    IMAGES, IMAGE_GENERATING, IMAGE_GEN_FAILED, URL, FILE, VIDEO, AUDIO,
    MEDIA_PROCESSING, MEDIA_FAILED, DOCUMENT_PREVIEW, UI,
})


# ---------------------------------------------------------------------------
# The notify queue's internal kinds: proxy → the connection's own loop
# (``ws/dashboard_server_events.py``). Never on the wire; every other item
# on that queue IS a frame and is forwarded verbatim.
# ---------------------------------------------------------------------------

NOTIFY_SERVER_KICK = "_server_kick"           # the backgrounded warmup's first turn
NOTIFY_BG_NUDGE = "bg_nudge"                  # background agents finished → a review turn
NOTIFY_BG_COMMAND_NUDGE = "bg_command_nudge"  # background commands finished → a review turn
NOTIFY_LIVENESS_CLEAR = "liveness_clear"      # a session died: clear the badges
NOTIFY_CHAT_UI_FRAME = "chat_ui_frame"        # {"chat_id", "frame"}: a frame for one chat's viewer
NOTIFY_TASK_RESULT_PROMPT = "task_result_prompt"  # a delegate's result → a synthesis turn
NOTIFY_CONTINUATION_PROMPT = "continuation_prompt"  # a scheduled wake → a wake turn
NOTIFY_LOCATION_REQUEST = LOCATION_REQUEST    # {"data": <the frame>} when no pump carries it


# ---------------------------------------------------------------------------
# system.subtype and the reason codes the frames carry
# ---------------------------------------------------------------------------

# The subtypes the PROXY mints or persists: the reseed card (a frame and a
# row), the undelivered-input card and the tailers' compaction separator
# (rows only), the self-wake marker and the meeting orchestrator's seven
# (frames and rows). Two open classes ride beside them: the CLI translator
# passes every raw subtype it does not consume straight through
# (``api_retry`` … — ``core/layers/cli/translator.py``) and the Direct
# layer forwards any unknown chunk kind as a subtype
# (``core/layers/direct/layer.py``); the dashboard renders the ones it
# knows and ignores the rest. The dashboard also synthesises six of its
# own (``warmup_failed.reason`` → a card; the two nudge rows → a label).
SUBTYPE_SESSION_RESEEDED = "session_reseeded"
SUBTYPE_UNDELIVERED_INPUT = "undelivered_input"
# A turn's typed ending (``core/events/turn_ending``), persisted by the pump
# after the turn's blocks so a reload shows the card where the live ``error``
# frame showed it: ``reason``, the ``message`` (the ending's line), its
# ``detail`` and ``resets_at``, an ``exit_code`` for an exited process.
SUBTYPE_TURN_ENDED = "turn_ended"
# ``undelivered_input.reason`` of a queued message nothing sent: a delivery
# the drive gate or the session refused (``queued``) or the turn it waited
# for ended other than cleanly (``turn_failed``), both written always (the
# live return can be lost), or a Stop's or an offboarding's return no socket
# of the author took (``stopped``); the terminal's abandoned cold flush
# carries none.
UNDELIVERED_QUEUED = "queued"
UNDELIVERED_TURN_FAILED = "turn_failed"
UNDELIVERED_STOPPED = "stopped"
SUBTYPE_BG_WAKE = "bg_wake"
SUBTYPE_CONTEXT_COMPRESSED = "context_compressed"   # the interactive tailers
SUBTYPE_MEETING_STARTED = "meeting_started"
SUBTYPE_MEETING_TURN_START = "meeting_turn_start"
SUBTYPE_MEETING_TURN_END = "meeting_turn_end"
SUBTYPE_MEETING_CONCLUDED = "meeting_concluded"
SUBTYPE_MEETING_AGENT_FAILED = "meeting_agent_failed"
SUBTYPE_MEETING_AGENT_LEFT = "meeting_agent_left"
SUBTYPE_MEETING_FAILED = "meeting_failed"
# Live only: a turn that could not wait in the chat's queue (its row was
# written: a server kick, an interaction) while the chat's machine is still
# reconnecting; ``message`` says to send it again.
SUBTYPE_MACHINE_RECONNECTING = "machine_reconnecting"
SYSTEM_SUBTYPES: frozenset[str] = frozenset({
    SUBTYPE_SESSION_RESEEDED, SUBTYPE_UNDELIVERED_INPUT, SUBTYPE_TURN_ENDED, SUBTYPE_BG_WAKE,
    SUBTYPE_CONTEXT_COMPRESSED, SUBTYPE_MACHINE_RECONNECTING,
    SUBTYPE_MEETING_STARTED, SUBTYPE_MEETING_TURN_START, SUBTYPE_MEETING_TURN_END,
    SUBTYPE_MEETING_CONCLUDED, SUBTYPE_MEETING_AGENT_FAILED,
    SUBTYPE_MEETING_AGENT_LEFT, SUBTYPE_MEETING_FAILED,
})
# The raw CLI subtypes the pump never persists (they drive registries).
SYSTEM_SUBTYPES_TRANSIENT: tuple[str, ...] = ("task_started", "task_progress")

# ``warmup_failed.reason``: the pool's classifier
# (``services/engines/subscription_pool.NoSubscriptionError``) plus the two
# spawn outcomes ``ws/dashboard_warmup.py`` names. The dashboard maps them
# onto its own system-card subtypes (``no_subscription``, ``pool_cap``,
# ``target_unavailable``, ``session_error``).
REASON_AUTH_OFF = "auth_off"
REASON_ADMIN_OAUTH_ONLY = "admin_oauth_only"
REASON_NO_POOL = "no_pool"
REASON_NONE = "none"
REASON_THROTTLED = "throttled"
REASON_OWN_SUB_EXPIRED = "own_sub_expired"
REASON_POOL_CAP = "pool_cap"
REASON_TARGET_UNAVAILABLE = "target_unavailable"
REASON_SESSION_ERROR = "session_error"
# A chat that runs as the agent, for a person below the editor tier
# (``core/sandbox/session_config_dir.AgentStateRefused``): a role matter.
REASON_BELOW_EDITOR = "below_editor"
WARMUP_FAILED_REASONS: frozenset[str] = frozenset({
    REASON_AUTH_OFF, REASON_ADMIN_OAUTH_ONLY, REASON_NO_POOL, REASON_NONE,
    REASON_THROTTLED, REASON_OWN_SUB_EXPIRED, REASON_POOL_CAP,
    REASON_TARGET_UNAVAILABLE, REASON_SESSION_ERROR, REASON_BELOW_EDITOR,
})

# ``session_reseeded.reason`` (``chats.pending_history_seed``'s kind prefix,
# ``core/session/history_seed.py``).
SEED_MACHINE_REMOVED = "machine_removed"
SEED_MOVED = "moved"
SEED_ENGINE_SWITCH = "engine_switch"
SEED_RESUME_FAILED = "resume_failed"
SEED_RETENTION = "retention"
SEED_REASONS: frozenset[str] = frozenset({
    SEED_MACHINE_REMOVED, SEED_MOVED, SEED_ENGINE_SWITCH, SEED_RESUME_FAILED,
    SEED_RETENTION,
})


# ---------------------------------------------------------------------------
# The payloads (one TypedDict per frame; ``_PerChat`` carries the optional
# ``chat_id`` tag the stream loop stamps). Keys are the wire's, frozen.
# ---------------------------------------------------------------------------

class _PerChat(TypedDict, total=False):
    chat_id: str


class TextFrame(_PerChat):
    content: str


class ThinkingFrame(_PerChat, total=False):
    phase: str            # start | delta | progress | end
    text: str
    estimated_tokens: int


class ToolStartFrame(_PerChat, total=False):
    name: str
    tool_id: str


class ToolInfoFrame(_PerChat, total=False):
    name: str
    summary: str
    tool_input: Any
    file_path: str


class ToolEndFrame(_PerChat, total=False):
    name: str
    tool_id: str


class ToolResultFrame(_PerChat, total=False):
    tool_name: str
    tool_use_id: str
    summary: str
    result_content: str


class TaskSpawnFrame(_PerChat, total=False):
    description: str
    subagent_type: str
    run_in_background: bool
    tool_use_id: str
    tool_input: Any


class BgAgentDoneFrame(_PerChat):
    tool_use_id: str


class BgCommandSpawnFrame(_PerChat, total=False):
    tool_use_id: str
    command: str
    description: str


class BgCommandDoneFrame(_PerChat, total=False):
    tool_use_id: str
    status: str


class CountFrame(_PerChat):
    """``bg_agents_complete`` / ``bg_commands_complete``."""
    count: int


class WorkflowStartFrame(_PerChat, total=False):
    tool_use_id: str
    workflow_name: str


class WorkflowProgressFrame(_PerChat, total=False):
    tool_use_id: str
    workflow_progress: list


class WorkflowEndFrame(_PerChat):
    tool_use_id: str


class DelegateSpawnFrame(_PerChat, total=False):
    """``chat_id`` on the persisted row is the WORKER chat; on the live
    frame the stream loop's tag overwrites it (a payload collision, deferred)."""
    task_id: str
    task_name: str
    agent: str
    surface: str
    prompt_preview: str
    prompt: str


class DelegateResultFrame(_PerChat, total=False):
    task_id: str
    task_name: str
    agent: str
    output_text: str
    status: str           # a storage/automation/run_status.py DELEGATE_RESULTS word
    # A worker turn's typed ending (any of turn_ending.REASONS) and its reset
    # time; the dashboard keeps its status icon.
    reason: str
    resets_at: str
    # A failed run's newest failing check verdict (services/checks, the
    # evaluator's row), when one landed since the run started.
    verdict: dict
    # The deliverables the worker attached, as they landed in the
    # delegator's tree (``{path, bytes}``, agent-tree-relative), and the
    # ones that did not land, each with its reason (``{path, reason}``).
    files: list
    files_skipped: list


class CheckVerdictFrame(_PerChat, total=False):
    """The card event of ``services/checks/render.card_event``, forwarded
    as-is (``check``, ``status``, ``summary``, ``findings``, ``round``…)."""
    check: str
    status: str
    summary: str
    findings: list
    round: int
    rounds: int
    ran_on: str


class PermissionPromptFrame(_PerChat, total=False):
    request_id: str
    tool_name: str
    tool_input: Any
    description: str
    meeting_agent: str


class PlanReviewFrame(_PerChat, total=False):
    request_id: str
    plan: str
    tool_input: Any
    filename: str


class QuestionFrame(_PerChat, total=False):
    request_id: str       # present for a held (Codex) question only
    tool_name: str
    tool_input: Any


class PromptRetiredFrame(_PerChat):
    request_id: str       # the permission, plan review or held question card


class LocationRequestFrame(_PerChat):
    request_id: str


class PlanModeFrame(_PerChat, total=False):
    action: str           # enter | exit
    plan: str
    filename: str
    tool_input: Any


class SystemFrame(_PerChat, total=False):
    subtype: str
    message: str
    machine_name: str
    reason: str
    agent: str
    agent_display_name: str
    agent_color: str
    round: int
    participants: list
    max_rounds: int
    max_turns: int
    meeting_id: str
    mcp: str
    pct: int
    phase: str


class MetadataFrame(_PerChat, total=False):
    cost_usd: float
    cost_billed: bool
    duration_ms: int
    duration_api_ms: int
    context_used: int
    context_max: int
    cache_read: int
    cache_write: int
    input_tokens: int
    output_tokens: int


class McpCostFrame(_PerChat):
    cost_usd: float
    cost_billed: bool
    provider: str
    model: str
    tool: str
    mcp: str


class TodoUpdateFrame(_PerChat):
    todos: list


class GoalUpdateFrame(_PerChat):
    goal: dict | None


class ContextCompactFrame(_PerChat, total=False):
    phase: str            # started | completed | failed | usage
    trigger: str          # auto | manual
    pre_tokens: int
    post_tokens: int
    context_max: int
    messages_summarized: int


class DoneFrame(_PerChat):
    pass


class _ErrorFrameBase(_PerChat):
    message: str


class ErrorFrame(_ErrorFrameBase, total=False):
    """The dashboard socket's error: a message, a chat tag when mid-turn, and
    for a turn that ended typed, its ending: ``reason`` (one of
    ``core/events/turn_ending.REASONS``: ``declined``, ``limit``, ``error``,
    ``exited``, ``silent``, ``lost``) and ``resets_at`` (ISO-8601 UTC, ""
    when unknown). A pump's frame always carries ``chat_id`` and a ``done``
    follows it once the turn's rows landed; a bare error (the dispatcher's
    refusal of one message) carries neither."""
    reason: str
    resets_at: str


class AbortedFrame(_PerChat):
    pass


class LiveStateFrame(_PerChat, total=False):
    streaming: bool
    session_id: str
    started_at: float
    live_blocks: list
    active_tools: list
    active_agents: list
    active_delegates: list
    active_commands: list
    pending_permission: dict | None
    thinking_active: bool
    thinking_text: str
    thinking_tokens: int
    todos: list
    goal: dict | None
    workflows: dict
    meeting_agent: str | None
    meeting_participants: list


class LimitFrame(_PerChat, total=False):
    """``limit_warning`` / ``limit_reached``: the pool cap, or the usage
    periods (``monthly``, ``weekly``, ``self``) spread in."""
    pool: dict
    monthly: dict | None
    weekly: dict | None
    self: dict


class ImagesFrame(_PerChat):
    images: list


class ImageGeneratingFrame(_PerChat, total=False):
    prompt_preview: str
    model: str


class ImageGenFailedFrame(_PerChat):
    pass


class UrlFrame(_PerChat, total=False):
    url: str
    title: str
    description: str


class FileFrame(_PerChat, total=False):
    filename: str
    download_url: str
    description: str


class MediaFrame(_PerChat, total=False):
    """``video`` / ``audio``."""
    src_kind: str         # url | token
    url: str
    token: str
    media_url: str
    mime: str
    caption: str
    title: str
    poster: str


class MediaProcessingFrame(_PerChat, total=False):
    media_kind: str       # video | audio
    caption: str


class MediaFailedFrame(_PerChat, total=False):
    error: str


class DocumentPreviewFrame(_PerChat, total=False):
    wopi_url: str
    # The WOPI token, on the live frame only (artifact_events.LIVE_ONLY_KEYS):
    # a stored row drops both, and a card from history mints its own.
    access_token: str
    access_token_ttl: int  # the token's expiry, epoch milliseconds
    filename: str
    file_id: str
    download_url: str
    snapshot_id: str
    generation: int
    version: int  # the push's number among the file's pushes in the chat


class UiFrame(_PerChat, total=False):
    token: str
    ui_url: str
    title: str
    height: int | None
    path: str


class ArtifactInteractionFrame(_PerChat, total=False):
    token: str
    title: str
    payload: Any


class ArtifactAckFrame(TypedDict, total=False):
    token: str
    status: str           # sent | queued | denied | unavailable
    reason: str


class AppActionFrame(_PerChat, total=False):
    app_id: str
    slug: str
    title: str
    action_id: str
    label: str
    prompt: str


class AppActionAckFrame(TypedDict, total=False):
    app_id: str
    action_id: str
    status: str
    reason: str


class _QueueAttachments(TypedDict, total=False):
    """The attachment meta a queued or steered message carries
    (``TurnInput.frame_fields``): the photos as saved (``{name, path}``) and
    the validated files (``{path, name}``); absent on a text-only message."""
    images: list
    files: list


class _QueueWaiting(TypedDict, total=False):
    """Why a message waits beyond the chat's turn: ``reconnect`` while the
    chat's machine is reconnecting (``input_queue.WAITING_RECONNECT``)."""
    waiting: str


class _QueueChip(_PerChat, _QueueAttachments, _QueueWaiting):
    queue_id: str         # the client-minted id of the waiting message
    text: str
    author_sub: str       # who typed it (a shared chat shows a teammate's chips)
    view_chat_id: str     # the chat its author viewed when not the queue's own


class QueuedFrame(_QueueChip):
    index: int            # its place in the chat's queue


class _Returned(_QueueAttachments, total=False):
    """On the author's sockets only: the message handed back to the
    composer (``returned: true``, the text and the attachments)."""
    returned: bool
    text: str


class QueueRemovedFrame(_PerChat, _Returned):
    queue_id: str
    index: int


class QueueSentFrame(_PerChat, _QueueAttachments):
    queue_ids: list       # the queued messages the turn took
    message_ids: list     # their chat_messages ids (one row per message)
    text: str             # the batch joined, as the engine got it


class QueueClearedFrame(_PerChat, _Returned):
    queue_ids: list
    reason: str           # cancel, stopped, turn_failed, drive_refused, revoked


class QueueSnapshotFrame(_PerChat):
    messages: list        # the chat's waiting messages, as ``_QueueChip`` entries


class _SteerMeta(TypedDict, total=False):
    event_data: dict      # a delegated steer: the delegating agent's identity (lane_steer)
    queue_id: str
    message_id: int       # the steer's row: its place in the chat


class SteeredFrame(_PerChat, _QueueAttachments, _SteerMeta):
    text: str


class ModeChangedFrame(_PerChat):
    mode: str


class ModelChangedFrame(_PerChat):
    model: str


class ExecutionModeChangedFrame(_PerChat):
    # ``chat_id``: the chat the ack or a refused pick's echo is about; absent
    # for a new chat's pick (nothing to apply it to yet). Not a per-chat
    # stream frame: the dashboard matches the id itself.
    execution_mode: str


class PlanStatusFrame(TypedDict):
    filename: str
    status: str


class ServerTurnStartFrame(_PerChat):
    pass


class WarmupStartedFrame(TypedDict, total=False):
    chat_id: str
    agent: str
    execution_path: str
    execution_target: str
    new_chat: bool


class WarmupHeartbeatFrame(TypedDict):
    chat_id: str


class WarmupReadyFrame(TypedDict, total=False):
    session_id: str | None
    chat_id: str
    mode: str
    model: str
    needs_warmup: bool
    execution_path: str
    execution_target: str
    execution_mode: str
    interactive: bool
    turn_open: bool
    fallback_reason: str | None
    offline_machine_name: str
    pinned_target: str
    pinned_label: str
    resolved_target: str
    resolved_label: str


class WarmupFailedFrame(TypedDict, total=False):
    chat_id: str
    error: str
    reason: str           # WARMUP_FAILED_REASONS


class PreWarmupReadyFrame(TypedDict):
    session_id: str


class ChatHistoryFrame(TypedDict, total=False):
    chat_id: str
    agent: str
    messages: list
    has_more: bool
    restore: dict
    plans: list
    total_cost: float
    context_used: int
    context_max: int
    cache_read: int
    cache_write: int
    output_tokens: int
    execution_path: str
    execution_mode: str
    model: str
    mode: str
    process_alive: bool
    # Present only on a chat that runs as the agent which this person may
    # not drive: the drive gate's sentence; the dashboard locks the mode,
    # model and terminal pickers with it as their tooltip.
    drive_refusal: str
    # True when the live turn's attach follows (its ``live_state``): the
    # dashboard holds these rows and paints them with the live blocks once.
    live_pending: bool


class _ChatHistoryDeltaKeys(TypedDict):
    # The keys a delta always carries (``Required[...]`` is invisible at run
    # time under this module's postponed annotations; the later base's
    # requiredness wins in a TypedDict's MRO).
    chat_id: str
    messages: list
    since_id: int


class ChatHistoryDeltaFrame(ChatHistoryFrame, _ChatHistoryDeltaKeys, total=False):
    """A history re-send of the viewed chat for a client that declared
    ``history_deltas``: a ``resume_chat`` with ``delta`` or one of the
    server's re-sends (a task or meeting chat's turn end, a task chat's next
    run, the idle poll's attach, a promised pump gone before its attach).
    ``ChatHistoryFrame``'s fields with ``messages`` holding the rows above
    the floors of the last history this connection got for the chat (the
    viewed chat and its sibling runs, ascending by id; a user row above a
    live pump's cutoff can come again, so the client dedupes by id and
    re-derives its view from every row it holds). Never the first frame of
    a view: a connection with no floor for the chat is sent the full
    history instead. The sender always sets
    ``chat_id``, ``messages`` and ``since_id`` (``_ChatHistoryDeltaKeys``);
    the rest stay optional."""


class ChatStatusFrame(TypedDict):
    chat_id: str
    status: str           # a ws/chat_phase.py WIRE_PHASES word: streaming | ready


class ChatStatusSnapshotFrame(TypedDict):
    chat_ids: list


class ChatReadFrame(TypedDict):
    chat_id: str


class ChatRowsFrame(TypedDict, total=False):
    chat_id: str
    agent: str


class ChatMetaFrame(TypedDict, total=False):
    chat_id: str
    delegate_role: str
    project_id: str


class ChatMovedFrame(TypedDict, total=False):
    chat_id: str
    new_target: str
    resolved_label: str


class EngineSwitchedFrame(TypedDict):
    chat_id: str
    execution_path: str
    model: str


class SwitchEngineDeniedFrame(TypedDict, total=False):
    chat_id: str
    message: str
    process_alive: bool


class LivenessFrame(TypedDict):
    chat_id: str
    process_alive: bool


class TitleUpdatedFrame(TypedDict):
    chat_id: str
    title: str


class TurnCompleteFrame(TypedDict, total=False):
    chat_id: str
    title: str
    body: str


class PtyOutputFrame(_PerChat, total=False):
    session_id: str
    data: str             # base64
    replay: bool
    reset: bool


class PtyExitFrame(_PerChat, total=False):
    session_id: str
    reason: str
    code: int


class PtyPermissionFrame(_PerChat, total=False):
    kind: str             # permission | plan_review | question
    session_id: str
    request_id: str
    tool_name: str
    tool_input: Any
    plan: str
    filename: str
    meeting_agent: str


class PtyArtifactFrame(_PerChat, total=False):
    session_id: str
    event: dict           # an artifact frame (IMAGES … UI), db_message_id when persisted


class PtyStatusFrame(_PerChat, total=False):
    session_id: str
    state: str            # attached | read_only | reconnecting | reconnected
    controller: str
    tui_theme: str


class NotificationFrame(TypedDict):
    """``notification`` / ``notification_silent``."""
    delivery: dict


class NotificationCountFrame(TypedDict):
    count: int


class ShareInboxFrame(TypedDict, total=False):
    """The recipient's "Shared with you" section changed (SHARING.md):
    ``pending`` is their count of decisions waiting, ``agents`` the agents
    whose Apps panel gained or lost a placed app for them."""
    pending: int
    agents: list


class FileUpdatedFrame(TypedDict, total=False):
    agent_slug: str
    rel_path: str
    file_id: str
    source: str           # collabora | disk
    pin: dict


class AppPushFrame(TypedDict):
    app_id: str
    payload: Any
    ts: int


class AppStateFrame(TypedDict):
    app_id: str
    doc: Any
    rev: int


class OpenAppFrame(TypedDict, total=False):
    app_id: str
    title: str
    agent: str
    opened_by: str
    scope_chat_id: str
    scope_project_id: str


class AppDeployedFrame(TypedDict):
    app_id: str
    release: Any
    actions_sig: str


class CatalogFrame(TypedDict, total=False):
    agent: str
    feed: str
    seq: int
    delta: Any
    snapshot: Any


class InstallFrame(TypedDict, total=False):
    """The ``install_*`` family and ``mcp_install_failed``, keyed by
    ``machine_id`` + ``agent``."""
    machine_id: str
    agent: str
    mcp: str
    phase: str
    pct: int
    message: str
    error: str
    mcps_to_install: list
    mcps_to_update: list
    warmup_failures: list


class TransferFrame(TypedDict, total=False):
    """The ``transfer_*`` family, keyed by ``transfer_id``."""
    transfer_id: str
    agent_slug: str
    rel_path: str
    filename: str
    kind: str
    bytes_total: int
    bytes_sent: int
    started_at: str
    machines: dict
    machine_id: str
    state: str
    error: str
    ok: bool


class SatelliteUpdateFrame(TypedDict, total=False):
    """``satellite_updating`` / ``satellite_updated`` / ``satellite_update_failed``."""
    machine_id: str
    machine_name: str
    from_version: str
    to_version: str
    version: str
    started_at: str
    error: str
    rolled_back_to: str
    attempts: int
    auto_update_paused: bool


class SatelliteUpdateSyncFrame(TypedDict):
    inflight: list


class ServerInfoFrame(TypedDict):
    build_id: str
    version: str


class PongFrame(TypedDict):
    build_id: str


# The other two shapes of ``error``.

class PhoneErrorFrame(TypedDict, total=False):
    """The phone and duplex daemon sockets: ``turn`` when turn-scoped."""
    turn: int
    data: dict            # {"message": str}


class DuplexErrorFrame(TypedDict):
    """The duplex browser socket (``ws/duplex.py``)."""
    reason: str           # bad_token | engine_offline | access_denied


# ---------------------------------------------------------------------------
# The phone socket (``ws/phone.py``): seven frames out, four messages in.
# ---------------------------------------------------------------------------

PHONE_WARMUP_READY = WARMUP_READY   # {"data": {"session_id", "llm_mode"}}
PHONE_SESSION = "session"           # {"turn", "data": {"session_id"}} — read by the daemon, minted by no live path (frozen)
PHONE_TEXT = TEXT                   # {"turn", "data": {"content"}}
PHONE_TOOL_START = TOOL_START       # {"turn", "data": {"name", "tool_use_id"}}
PHONE_TOOL_END = TOOL_END           # {"turn", "data": {"tool_use_id", "result_preview"}}
PHONE_DONE = DONE                   # {"turn", "data": {}}
PHONE_ERROR = ERROR                 # PhoneErrorFrame
PHONE_FRAMES: frozenset[str] = frozenset({
    PHONE_WARMUP_READY, PHONE_SESSION, PHONE_TEXT, PHONE_TOOL_START,
    PHONE_TOOL_END, PHONE_DONE, PHONE_ERROR,
})
PHONE_IN_WARMUP = "warmup"
PHONE_IN_CHAT = "chat"
PHONE_IN_ABORT = "abort"
PHONE_IN_CLOSE = "close"


# ---------------------------------------------------------------------------
# The inbound messages of the dashboard socket and the dispatcher table
# ---------------------------------------------------------------------------

IN_PRE_WARMUP = "pre_warmup"
IN_WARMUP = "warmup"
IN_CHAT = "chat"
IN_ARTIFACT_INTERACTION = ARTIFACT_INTERACTION
IN_APP_ACTION = APP_ACTION
IN_RESUME_CHAT = "resume_chat"
IN_CHAT_READ = CHAT_READ
IN_PERMISSION_RESPONSE = "permission_response"
IN_QUESTION_RESPONSE = "question_response"
IN_LOCATION_RESPONSE = "location_response"
IN_PLAN_REVIEW_RESPONSE = "plan_review_response"
IN_PTY_ATTACH = "pty_attach"
IN_PTY_TAKEOVER = "pty_takeover"
IN_PTY_INPUT = "pty_input"
IN_PTY_RESIZE = "pty_resize"
IN_PTY_ATTACHMENTS = "pty_attachments"
IN_MODE_CHANGE = "mode_change"
IN_MODEL_CHANGE = "model_change"
IN_EXECUTION_MODE_CHANGE = "execution_mode_change"
IN_EXECUTION_MODE_SWITCH = "execution_mode_switch"
IN_COMPACT_CONTEXT = "compact_context"
IN_MOVE_CHAT = "move_chat"
IN_SWITCH_ENGINE = "switch_engine"
IN_PROBE_LIVENESS = "probe_liveness"
IN_IMPLEMENT_PLAN = "implement_plan"
IN_ABORT = "abort"
IN_CANCEL_QUEUED = "cancel_queued"
IN_CANCEL_ALL_QUEUED = "cancel_all_queued"
IN_CLIENT_INFO = "client_info"
IN_USER_ACTIVE = "user_active"
IN_USER_IDLE = "user_idle"
IN_FOCUS = "focus"
IN_CATALOG_SUBSCRIBE = "catalog_subscribe"
IN_CATALOG_UNSUBSCRIBE = "catalog_unsubscribe"
IN_PING = "ping"
IN_CLOSE = "close"

# What the stream loop does with a message that arrives while a turn streams.
MID_STREAM_SAME = "same"      # the handler runs with the pump in hand
MID_STREAM_DETACH = "detach"  # the loop detaches and hands the message back
MID_STREAM_END = "end"        # the loop detaches and ends (the socket is closing)
MID_STREAM_DROP = "drop"      # logged and dropped until the turn ends


class Inbound(NamedTuple):
    handler: str      # a DashboardConnection method: async (msg, *, stream=None)
    mid_stream: str   # MID_STREAM_*


INBOUND: dict[str, Inbound] = {
    IN_PRE_WARMUP: Inbound("_on_pre_warmup", MID_STREAM_DETACH),
    IN_WARMUP: Inbound("_on_warmup", MID_STREAM_DETACH),
    IN_CHAT: Inbound("_on_chat", MID_STREAM_SAME),
    IN_ARTIFACT_INTERACTION: Inbound("_on_artifact_interaction", MID_STREAM_SAME),
    IN_APP_ACTION: Inbound("_on_app_action", MID_STREAM_SAME),
    IN_RESUME_CHAT: Inbound("_on_resume_chat", MID_STREAM_DETACH),
    IN_CHAT_READ: Inbound("_on_chat_read", MID_STREAM_SAME),
    IN_PERMISSION_RESPONSE: Inbound("_on_permission_response", MID_STREAM_SAME),
    IN_QUESTION_RESPONSE: Inbound("_on_question_response", MID_STREAM_SAME),
    IN_LOCATION_RESPONSE: Inbound("_on_location_response", MID_STREAM_SAME),
    IN_PLAN_REVIEW_RESPONSE: Inbound("_on_plan_review_response", MID_STREAM_SAME),
    IN_PTY_ATTACH: Inbound("_on_pty_attach", MID_STREAM_DROP),
    IN_PTY_TAKEOVER: Inbound("_on_pty_takeover", MID_STREAM_DROP),
    IN_PTY_INPUT: Inbound("_on_pty_input", MID_STREAM_DROP),
    IN_PTY_RESIZE: Inbound("_on_pty_resize", MID_STREAM_DROP),
    IN_PTY_ATTACHMENTS: Inbound("_on_pty_attachments", MID_STREAM_DROP),
    IN_MODE_CHANGE: Inbound("_on_mode_change", MID_STREAM_SAME),
    IN_MODEL_CHANGE: Inbound("_on_model_change", MID_STREAM_SAME),
    IN_EXECUTION_MODE_CHANGE: Inbound("_on_execution_mode_change", MID_STREAM_DROP),
    IN_EXECUTION_MODE_SWITCH: Inbound("_on_execution_mode_switch", MID_STREAM_DROP),
    IN_COMPACT_CONTEXT: Inbound("_on_compact_context", MID_STREAM_DROP),
    IN_MOVE_CHAT: Inbound("_on_move_chat", MID_STREAM_DROP),
    IN_SWITCH_ENGINE: Inbound("_on_switch_engine", MID_STREAM_DROP),
    IN_PROBE_LIVENESS: Inbound("_on_probe_liveness", MID_STREAM_DROP),
    IN_IMPLEMENT_PLAN: Inbound("_on_implement_plan", MID_STREAM_DROP),
    IN_ABORT: Inbound("_on_abort", MID_STREAM_SAME),
    IN_CANCEL_QUEUED: Inbound("_on_cancel_queued", MID_STREAM_SAME),
    IN_CANCEL_ALL_QUEUED: Inbound("_on_cancel_all_queued", MID_STREAM_SAME),
    IN_CLIENT_INFO: Inbound("_on_client_info", MID_STREAM_DROP),
    IN_USER_ACTIVE: Inbound("_on_user_active", MID_STREAM_SAME),
    IN_USER_IDLE: Inbound("_on_user_idle", MID_STREAM_SAME),
    IN_FOCUS: Inbound("_on_focus", MID_STREAM_SAME),
    IN_CATALOG_SUBSCRIBE: Inbound("_on_catalog_subscription", MID_STREAM_SAME),
    IN_CATALOG_UNSUBSCRIBE: Inbound("_on_catalog_subscription", MID_STREAM_SAME),
    IN_PING: Inbound("_on_ping", MID_STREAM_SAME),
    IN_CLOSE: Inbound("_on_close", MID_STREAM_END),
}

# A handler's return values (both loops read them).
HANDLED_CLOSE = "close"   # the between-turns loop: close the socket
HANDLED_BREAK = "break"   # the stream loop: the turn's loop ends (an abort)
