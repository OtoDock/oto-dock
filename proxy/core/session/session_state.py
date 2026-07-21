"""Shared module-level state and accessor functions for the CLI session system.

This file is THE HUB — all module-level dicts/sets/vars live here.
Other core/ modules import from here; this file only imports config (+ stdlib).
"""

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("claude-proxy")

# ---------------------------------------------------------------------------
# Module-level state variables
# ---------------------------------------------------------------------------

# In-memory session tracking: {session_id: metadata}
# Persisted to sessions/index.json for crash recovery
_sessions: dict[str, dict] = {}
_SESSION_INDEX = config.SESSIONS_DIR / "index.json"
# index.json is append-only (entries are never removed during normal use), so it
# grows unbounded over a long-lived install. This dict is lightweight TRACKING
# metadata (message_count / last_active / tz / agent) — NOT the resume mechanism
# (that's the on-disk .claude/.codex session files), so dropping a stale entry is
# safe: a session reused after the cutoff just re-registers. We prune entries
# whose last activity is older than this TTL (chosen to match the retention
# default for session-file aging, so an index entry never outlives its content).
_SESSION_INDEX_TTL_DAYS = 180
# Task ("is_task") sessions are reaped far more aggressively than the 180-day
# index TTL: their entry is normally popped the instant the run finishes (in
# scheduler._run_task's finally). This is the defense-in-depth backstop for
# entries that leaked anyway — a pre-launch failure or a process restart
# mid-run — so the TTL need only exceed the longest plausible single run.
_TASK_SESSION_REAP_TTL_SECONDS = 6 * 3600  # 6h

# Per-session locks: prevent concurrent processes on the same session.
# Without this, a client retry can spawn a second Claude process on a session
# that already has one running.
_session_locks: dict[str, asyncio.Lock] = {}

# Active running processes by session_id (for abort support)
_active_processes: dict[str, asyncio.subprocess.Process] = {}
# Sessions explicitly aborted via abort endpoint (don't save pending results)
_aborted_sessions: set[str] = set()

# Permission gate state (for hook-based permission system)
_session_modes: dict[str, str] = {}  # session_id -> permission_mode (auto|default|plan)
# session_id -> MCP tool names the user already clicked Allow for this session.
# One Allow per tool per session: without this, default mode re-prompts on
# EVERY call (N cards for N calls of the same tool). MCP tools only — Bash
# risk varies per command, so shell approvals are never remembered.
_session_tool_allows: dict[str, set[str]] = {}
_permission_events: dict[str, asyncio.Event] = {}  # request_id -> event (unblocks hook)
_permission_decisions: dict[str, bool] = {}  # request_id -> approved
# session_id -> set of its pending request_ids. The waiters above are keyed by
# request_id only, so without this index there is no way to release a session's
# pending prompts on abort/close. A warm app-server daemon (Codex) + its MCP
# subprocesses + the stdio interceptor all SURVIVE turn/interrupt, so a pending
# MCP-permission wait would otherwise hang to its 7-day timeout and hold the
# interceptor pipe. resolve_session_permissions() walks this to deny them.
_session_permission_requests: dict[str, set[str]] = {}  # session_id -> {request_id}
_permission_request_sessions: dict[str, str] = {}  # request_id -> session_id (reverse)
# Codex request_user_input questions block the SAME way (the held
# item/tool/requestUserInput server-request), but resolve to an ANSWERS MAP, not
# a bool. Separate registries; question request_ids are ALSO indexed in
# _session_permission_requests above, so resolve_session_permissions() releases
# them on abort/close (empty answers → the held request unwinds cleanly).
_question_events: dict[str, asyncio.Event] = {}  # request_id -> event
_question_answers: dict[str, dict] = {}  # request_id -> answers map
# SSE emitters: when a hook requests permission in "default" mode, this callback
# emits the permission_request/question event to the downstream SSE stream
_permission_emitters: dict[str, asyncio.Queue] = {}  # session_id -> queue of permission requests

# Per-session security context for path-based access control.
# Set when session is created. Used by hook_permission() to enforce file restrictions.
# PERSISTED to sessions/security_index.json: a remote session (and its
# background sub-agents) survives a proxy crash on the satellite, so without
# reloading its context the post-restart permission gate would see None and
# either fail OPEN (the old hole) or brick the surviving turn. The context
# carries NO secrets (role/username/agent/target-meta/device-grants —
# auth/path_policy.SecurityContext), so persisting it is not a credential leak.
# Cleared on session close, so a closed session's self-contained 24h JWT
# (auth/session_token.py — no server-side liveness check) replayed in its window
# finds no context and is denied.
_session_security: dict = {}  # session_id -> path_policy.SecurityContext
_session_security_ts: dict = {}  # session_id -> float (warmup epoch; for the load-time TTL prune)
_SECURITY_INDEX = config.SESSIONS_DIR / "security_index.json"
# Drop persisted contexts older than the session-JWT lifetime — one that old
# can't be replayed anyway, so this bounds the file across non-graceful crashes.
_SECURITY_TTL_S = 24 * 3600

# Per-session persistent .claude/ directory path (host path).
# Used by plan API and can_resume_session to find session-specific data.
_session_claude_dirs: dict[str, str] = {}  # session_id -> host path to .claude/

# Per-session Codex interactive context for the rollout tailer (CODEX_HOME is
# per-(user, agent) scope — shared across that scope's chats — so the tailer needs
# the sandbox CWD + spawn time to pick THIS session's rollout). Parallel to
# _session_claude_dirs. session_id -> {"home", "cwd", "started_at"}.
_session_codex_dirs: dict[str, dict] = {}

# The location bridge (wait_for_location / resolve_location + its event/result
# maps) lives in location_bridge.py; re-exported so `from core.session.session_state
# import wait_for_location` (api/hooks) and `resolve_location` (ws/dashboard) work.
from core.session.location_bridge import wait_for_location, resolve_location  # noqa: F401
import contextlib

# Hook activity tracking: updated every time a permission hook fires for a session.
# Used by settle mode to detect that internal background agents are still working
# (they fire hooks but don't produce stdout stream events).
_session_hook_activity: dict[str, float] = {}  # session_id -> monotonic timestamp

# Per-session notification queue for server-initiated events (bg agent completion,
# task delegation results) to reach the dashboard WebSocket between turns.
# Stored in this shared module to avoid circular imports — the WS layer
# (ws/dashboard.py), api/tasks and the scheduler all push/read these queues.
_dashboard_notify_queues: dict[str, asyncio.Queue] = {}  # session_id → queue

# Meeting session mapping: links meeting agent sessions back to parent + pump.
# Used by hook_permission() to inherit the parent chat's permission mode and
# route permission prompts to the meeting pump's perm_queue.
_meeting_session_info: dict[str, dict] = {}  # agent_session_id → {parent_session_id, pump_session_id, agent_slug}

# Per-chat live streaming state — enables reconnecting clients to see in-progress work.
# Updated by _stream_chat_turn(), read by _handle_resume_chat() on reconnect
# (both in ws/dashboard.py).
_chat_streaming_state: dict[str, dict] = {}  # chat_id → state dict


def streaming_chat_ids() -> list[str]:
    """Chat ids with an ACTIVE pump turn (bg residuals excluded — their turn
    ended). The pump half of the dashboard connect-time ``chat_status_snapshot``;
    the interactive half is ``interactive_session.streaming_chat_ids``."""
    return [cid for cid, st in _chat_streaming_state.items() if st.get("streaming")]

# Per-user IANA timezone (browser-detected). In-memory only; reseeded by the next
# `client_info` WS message after a restart. Session-scoped TZ lives on
# _sessions[sid]["user_tz"] and persists to sessions/index.json via _save_sessions().
_user_tz: dict[str, str] = {}  # user_sub → IANA tz name


# ---------------------------------------------------------------------------
# Hook activity tracking
# ---------------------------------------------------------------------------


def record_hook_activity(session_id: str) -> None:
    """Record that a permission hook fired for this session (tool call in progress)."""
    _session_hook_activity[session_id] = time.monotonic()


def get_hook_activity(session_id: str) -> float:
    """Get the last hook activity monotonic timestamp for a session."""
    return _session_hook_activity.get(session_id, 0)


# ---------------------------------------------------------------------------
# Subagent registry — deterministic spawn/finish tracking (BOTH layers)
# ---------------------------------------------------------------------------

# Per-session subagent registry, fed by BOTH execution layers:
#  - Claude CLI: the translator on `task_started` + the SubagentStop hook on
#    completion.
#  - Codex: the per-thread background-sub-agent supervisor (`register_spawn` /
#    `mark_done` in core/layers/codex/session.py).
# `reg.has_pending` / `reg.wait_all_done()` then drive the shared
# `_bg_agent_monitor` (chat nudge) + `ExecutionLayer.wait_for_bg_subagents`
# (tasks/delegation) for either layer. Replaces the old FIFO `task_notification`
# counting + 10s hook-silence heuristic with id-keyed, order-independent tracking.
_subagent_registries: dict[str, "SubagentRegistry"] = {}  # session_id → registry


class SubagentRegistry:
    """Deterministic per-session subagent tracking, fed by BOTH layers.

    **Claude CLI:** emits, for every subagent (foreground OR background), a
    ``task_started`` event carrying a stable ``task_id`` plus the spawning
    ``tool_use_id``; when the subagent finishes it fires a ``SubagentStop``
    hook whose ``agent_id`` equals that ``task_id`` (idle-safe, out-of-band
    HTTP) and a ``task_notification`` on stdout (a backup that stalls while
    the ``-p`` process is idle). This registry binds the two so completion is
    KNOWN, not guessed. Only ``task_type == "local_agent"`` agents are tracked —
    backgrounded ``local_bash`` work emits ``task_started`` + ``task_notification``
    but NO ``SubagentStop``, so gating on it would hang the completion wait.
    Workflow-internal agents live only inside ``task_progress`` and never emit a
    top-level ``task_started``, so they can't pollute the gate either.

    **Codex:** the session's per-thread background-sub-agent supervisor calls
    ``register_spawn(agent_id, agent_id)`` at main-turn end for each still-active
    (background) sub-agent and ``mark_done(agent_id)`` when its thread reaches its
    terminal — keyed by the sub-agent's thread id (which is also the dashboard
    ``tool_use_id``). Foreground/waited Codex subs aren't registered here (they
    complete within the turn).

    Keyed internally by ``task_id`` (CLI ``agent_id`` / Codex thread id). The
    dashboard correlates by ``tool_use_id``.
    """

    __slots__ = (
        "spawned", "completed", "pending_stops",
        "task_to_tuid", "workflow_tuids", "chat_id", "_all_done_event",
    )

    def __init__(self) -> None:
        self.spawned: set[str] = set()        # task_ids registered via task_started (whitelist)
        self.completed: set[str] = set()       # task_ids marked done (⊆ spawned)
        self.pending_stops: set[str] = set()   # SubagentStop that raced ahead of task_started
        self.task_to_tuid: dict[str, str] = {}  # task_id → spawning tool_use_id
        self.workflow_tuids: set[str] = set()  # tool_use_ids of active Workflow tools
        self.chat_id: str = ""                 # set by the pump for hook-endpoint routing
        self._all_done_event = asyncio.Event()

    def register_spawn(self, task_id: str, tool_use_id: str) -> None:
        """Record a subagent spawn (CLI ``task_started``, local_agent only)."""
        if not task_id:
            return
        self.spawned.add(task_id)
        if tool_use_id:
            self.task_to_tuid[task_id] = tool_use_id
        # Reconcile a SubagentStop that arrived before its task_started.
        if task_id in self.pending_stops:
            self.pending_stops.discard(task_id)
            self.completed.add(task_id)
        self._refresh()

    def mark_done(self, task_id: str, *, buffer: bool = False) -> bool:
        """Mark a subagent finished. Idempotent.

        Returns True only on the transition to completed (so callers emit the
        WS completion exactly once across the SubagentStop + task_notification
        paths). ``buffer=True`` (SubagentStop hook) parks an id whose
        ``task_started`` hasn't been parsed yet; ``buffer=False``
        (task_notification backup) ignores unknown ids — they're local_bash
        or noise, not tracked agents.
        """
        if not task_id or task_id in self.completed:
            return False
        if task_id in self.spawned:
            self.completed.add(task_id)
            self._refresh()
            return True
        if buffer:
            self.pending_stops.add(task_id)
        return False

    def tuid_for(self, task_id: str) -> str:
        """Resolve a task_id to its spawning tool_use_id (dashboard key)."""
        return self.task_to_tuid.get(task_id, "")

    @property
    def has_pending(self) -> bool:
        """True if any tracked subagent hasn't finished yet."""
        return bool(self.spawned - self.completed)

    @property
    def pending_count(self) -> int:
        return len(self.spawned - self.completed)

    def _refresh(self) -> None:
        # Fire only when agents WERE spawned and all of them have finished
        # (never on the vacuous empty-set case — the monitor waits on this).
        if self.spawned and self.spawned <= self.completed:
            self._all_done_event.set()
        else:
            self._all_done_event.clear()

    async def wait_all_done(self) -> None:
        """Block until every spawned subagent has finished."""
        await self._all_done_event.wait()

    def reset(self) -> None:
        """Reset per-turn state, but PRESERVE still-pending subagents.

        A background subagent can outlive the turn that spawned it (CLI
        ``run_in_background``; Codex background ``spawn_agent``). The CLI resets
        the registry at the start of every turn — so without preserving pending
        entries, a follow-up user turn fired *while a bg agent is still running*
        would wipe it, and the _bg_agent_monitor awaiting it would never see it
        finish (the "can't talk / lost nudge while bg runs" bug). Keep only the
        still-running agents; drop fully-resolved entries + this-turn foreground
        bookkeeping. Preserves the Event object so a monitor awaiting it across
        the reset isn't orphaned. (CLI never had cross-turn pending before, so
        preserving-pending is a no-op for the common case — only the talk-while-bg
        case changes.)"""
        pending = self.spawned - self.completed
        self.spawned = set(pending)         # keep only still-running agents
        self.completed = set()              # none of `pending` is completed by definition
        self.task_to_tuid = {
            t: u for t, u in self.task_to_tuid.items() if t in pending
        }
        self.pending_stops.clear()          # a Stop with no task_started is stale across turns
        self.workflow_tuids.clear()
        self.chat_id = ""
        self._refresh()                     # recompute all-done for the carried-over set


def get_subagent_registry(session_id: str) -> SubagentRegistry:
    """Get or create the subagent registry for a session."""
    reg = _subagent_registries.get(session_id)
    if reg is None:
        reg = SubagentRegistry()
        _subagent_registries[session_id] = reg
    return reg


def reset_subagent_registry(session_id: str) -> None:
    """Reset a session's subagent registry at the start of a new turn."""
    reg = _subagent_registries.get(session_id)
    if reg is not None:
        reg.reset()


# ---------------------------------------------------------------------------
# Pump callbacks (set by app.py at startup to avoid circular imports)
# ---------------------------------------------------------------------------

_push_pump_event_fn = None   # (chat_id, event) -> bool
_queue_pump_message_fn = None  # (chat_id, text, system) -> bool
_inject_pump_event_fn = None  # (chat_id, common_event) -> bool


def set_pump_callbacks(push_event_fn, queue_message_fn, inject_event_fn=None):
    """Register pump access callbacks (called by app.py at startup)."""
    global _push_pump_event_fn, _queue_pump_message_fn, _inject_pump_event_fn
    _push_pump_event_fn = push_event_fn
    _queue_pump_message_fn = queue_message_fn
    _inject_pump_event_fn = inject_event_fn


def push_pump_event(chat_id: str, event: dict) -> bool:
    """Push a WS event to the active pump for immediate frontend delivery. Best-effort."""
    if _push_pump_event_fn:
        return _push_pump_event_fn(chat_id, event)
    return False


def inject_pump_event(chat_id: str, event) -> bool:
    """Inject a CommonEvent into the active pump's stream for in-order processing
    (persisted in the turn's blocks + live-state, then forwarded to the
    frontend). Best-effort; returns False if there's no live pump for chat_id.
    Used by the proxy to emit a delegate_spawn at task-create time so the badge
    appears only once the delegate actually starts.
    """
    if _inject_pump_event_fn:
        return _inject_pump_event_fn(chat_id, event)
    return False


def queue_pump_prompt(chat_id: str, text: str, system: bool = False) -> bool:
    """Queue a prompt on the active pump for in-context delivery after current turn.
    system=True suppresses user bubble (used for notification prompts).
    """
    if _queue_pump_message_fn:
        return _queue_pump_message_fn(chat_id, text, system)
    return False


def mark_delegate_completed(chat_id: str, task_name: str, task_id: str = "",
                            status: str = "completed"):
    """Update live state when a delegate task reaches a terminal. Matches by task_id
    (the stable correlation key) when present, else falls back to task_name. Records
    the terminal ``status`` (completed/failed/cancelled) so the badge resolves to the
    right icon instead of a misleading green check. Mutates in-place so the shared
    dict ref in live_blocks also updates.
    """
    live = _chat_streaming_state.get(chat_id)
    if live:
        for d in live["active_delegates"]:
            if (task_id and d.get("task_id") == task_id) or \
                    (not task_id and d.get("task_name") == task_name):
                d["active"] = False
                d["status"] = status
                break


def mark_bg_agents_completed(chat_id: str):
    """Update live state when all background agents have settled (nudge)."""
    live = _chat_streaming_state.get(chat_id)
    if live:
        for a in live["active_agents"]:
            a["active"] = False


def mark_subagent_done(chat_id: str, tool_use_id: str) -> None:
    """Update live state when one subagent finishes — keyed by tool_use_id.

    Order-independent (replaces the old FIFO oldest-active match) so parallel
    agents that finish out of order each clear their own widget. Mutates in
    place so the shared dict ref in live_blocks updates too.
    """
    if not tool_use_id:
        return
    live = _chat_streaming_state.get(chat_id)
    if live:
        for a in live["active_agents"]:
            if a.get("tool_use_id") == tool_use_id:
                a["active"] = False
                break


def resolve_bg_subagent(session_id: str, sub_tid: str, translator=None) -> bool:
    """Resolve a finished background sub-agent's completion side-effects.

    Marks the per-session ``SubagentRegistry`` done, clears the sub-agent's
    dashboard badge (live-state + WS ``bg_agent_done``), and tombstones it in the
    translator so a later ``collabAgentToolCall`` snapshot can't re-open a phantom
    badge. The single source of truth shared by BOTH Codex bg supervisors — the
    local session's ``notif_queue`` supervisor (``core/layers/codex/session.py``)
    and the remote layer's WS-forwarded supervisor (``core/remote/remote_execution.py``)
    — so completion behaves identically on local and satellite agents.

    Idempotent: ``reg.mark_done`` returns True only on the resolving transition,
    so the badge/WS clear fires exactly once across a supervisor's ``finally`` and
    the teardown backstop. The caller pops its own per-thread buffer/supervisor
    bookkeeping before calling this (that state is layer-specific)."""
    reg = get_subagent_registry(session_id)
    if not reg.mark_done(sub_tid):
        return False
    chat_id = reg.chat_id
    if chat_id:
        mark_subagent_done(chat_id, sub_tid)
        push_pump_event(chat_id, {"type": "bg_agent_done", "tool_use_id": sub_tid})
    if translator is not None:
        with contextlib.suppress(Exception):
            translator.subagent_end_event(sub_tid)
    return True


def mark_command_done(chat_id: str, tool_use_id: str) -> None:
    """Update live state when one background bash command finishes — keyed by
    tool_use_id. Mirror of mark_subagent_done: mutates the shared active_commands
    dict ref in place so the ordered live_blocks reconstruction updates too."""
    if not tool_use_id:
        return
    live = _chat_streaming_state.get(chat_id)
    if live:
        for c in live.get("active_commands", []):
            if c.get("tool_use_id") == tool_use_id:
                c["active"] = False
                break


def resolve_bg_command(session_id: str, task_id: str, status: str = "completed") -> bool:
    """Resolve a finished background bash command's completion side-effects: mark
    the BackgroundCommandRegistry done, clear its dashboard badge (live-state
    active_commands + WS ``bg_command_done``). Mirror of resolve_bg_subagent for
    the paths that have no pump event loop in scope — the bg-command monitor and
    the stale-output drain (background bash has NO completion hook, so completion
    is only observed by actively reading stdout).

    Idempotent: ``mark_done`` returns True only on the resolving transition, so
    the badge clear fires exactly once across the drain + monitor paths."""
    from core.events.bg_command_state import get_bg_command_registry
    reg = get_bg_command_registry(session_id)
    # Every caller here observes the completion AFTER the turn's generation
    # (idle/stale stdout drains + the post-turn monitor) — the model never saw
    # it, so it must count toward the task producer's review-turn decision.
    if not reg.mark_done(task_id, surfaced=False):
        return False
    tuid = reg.tuid_for(task_id)
    chat_id = reg.chat_id
    if chat_id:
        mark_command_done(chat_id, tuid)
        push_pump_event(chat_id, {
            "type": "bg_command_done", "tool_use_id": tuid, "status": status,
        })
    return True


def resolve_bg_command_frame(session_id: str, data: dict) -> bool:
    """If a raw stream-json ``system`` frame is a background-command completion
    (``task_updated{patch.status in TERMINAL}`` or ``task_notification``) for a
    tracked command, resolve it. Returns True only on the resolving transition.
    Lets the idle/stale stdout drains clear a bg command WITHOUT the per-turn
    translator (which is reset between turns)."""
    if data.get("type") != "system":
        return False
    subtype = data.get("subtype", "")
    task_id = data.get("task_id", "")
    if not task_id:
        return False
    if subtype == "task_updated":
        from core.events.bg_command_state import TERMINAL_STATUSES
        status = (data.get("patch") or {}).get("status", "")
        if status in TERMINAL_STATUSES:
            return resolve_bg_command(session_id, task_id, status)
        return False
    if subtype == "task_notification":
        return resolve_bg_command(session_id, task_id, "completed")
    return False


def clear_session_liveness(session_id: str, *, reason: str = "") -> None:
    """Clear a DEAD session's dashboard liveness — agent badges and
    background-command spinners — everywhere they could outlive it.

    The normal clears ride the session's own lifecycle signals (SubagentStop
    hooks, stdout ``task_updated`` frames, the ``*_complete`` WS frames), which
    a dead CLI can no longer emit — so every place the proxy declares a session
    dead calls this instead: the layer close paths (``close_persistent_session``
    / ``close_codex_session`` locally; ``cleanup_session_permission_state``
    covers the remote/interactive/meeting closes), the explicit dashboard Stop
    (CLI only — the abort kills the whole process group), and the warmup fresh
    branch that replaces a dead session. Death-only by design: background work
    legitimately outlives turn ends, so this must NEVER run on a mere turn end
    — and it is NOT called on a satellite reconnect blip (grace-held sessions
    are alive; only the post-grace reaper close lands here).

    Three effects, all idempotent:
      * pops the session's chats from ``_chat_streaming_state`` (matched by the
        entry's ``session_id``, so a chat already re-warmed onto a NEW session
        is never touched) — a reconnect can't resurrect badges via
        ``live_state``;
      * broadcasts a ``liveness_clear`` item to every dashboard notify queue —
        the WS consumer emits the ``*_complete`` frames to sockets viewing an
        affected chat, clearing live badges without a reload;
      * drops both per-session registries so stale pending entries can't leak
        (a monitor still watching them exits on its own ``is_session_alive``
        poll; ``_subagent_registries`` was already popped on cleanup, the
        bg-command registry previously leaked).
    """
    from core.events.bg_command_state import _bg_command_registries

    chats: set[str] = set()
    reg = _subagent_registries.pop(session_id, None)
    if reg is not None and reg.chat_id:
        chats.add(reg.chat_id)
    bgreg = _bg_command_registries.pop(session_id, None)
    if bgreg is not None and bgreg.chat_id:
        chats.add(bgreg.chat_id)
    for cid, live in list(_chat_streaming_state.items()):
        if live.get("session_id") == session_id:
            chats.add(cid)
            _chat_streaming_state.pop(cid, None)
    if not chats:
        return

    logger.info(
        f"clear_session_liveness: session={session_id[:8]} reason={reason or '-'} "
        f"chats={sorted(chats)}"
    )
    seen_queues: set[int] = set()
    for queue in list(_dashboard_notify_queues.values()):
        # One WS can be registered under several session ids — dedupe by queue.
        if id(queue) in seen_queues:
            continue
        seen_queues.add(id(queue))
        for cid in chats:
            with contextlib.suppress(Exception):
                queue.put_nowait({
                    "type": "liveness_clear",
                    "chat_id": cid,
                    "session_id": session_id,
                    "reason": reason,
                })


def broadcast_chat_frame(chat_id: str, frame: dict) -> None:
    """Push a chat-scoped UI frame to every dashboard notify queue.

    Each socket's server-events handler forwards ``frame`` verbatim only when
    it is viewing ``chat_id`` — a chat-targeted sibling of the
    ``liveness_clear`` broadcast above. Used for UI-state frames that must
    reach a live viewer INDEPENDENT of any delivery path (e.g. the terminal
    ``delegate_result`` badge frame when the wake itself lands on the
    pty/persistent/one-shot/none rungs, which carry no socket). Fire-and-
    forget; sockets not viewing the chat drop it.
    """
    if not chat_id:
        return
    seen: set[int] = set()
    for queue in list(_dashboard_notify_queues.values()):
        if id(queue) in seen:
            continue
        seen.add(id(queue))
        with contextlib.suppress(Exception):
            queue.put_nowait({
                "type": "chat_ui_frame",
                "chat_id": chat_id,
                "frame": frame,
            })


# ---------------------------------------------------------------------------
# Session lock helpers
# ---------------------------------------------------------------------------


def _get_session_lock(session_id: str) -> asyncio.Lock:
    """Get or create a lock for a specific session."""
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


# ---------------------------------------------------------------------------
# Session persistence (load / save)
# ---------------------------------------------------------------------------


def prune_dead_sessions(now: datetime | None = None) -> int:
    """Drop tracking entries for sessions inactive longer than the TTL so
    ``index.json`` can't grow unbounded. Returns the number removed.

    Only entries with a parseable ``last_active`` older than the cutoff are
    removed — a created-but-never-messaged stub (no ``last_active``) is left
    alone (it's tiny and indistinguishable from a brand-new registration).
    Safe to call any time: ``_sessions`` is tracking metadata, not the resume
    mechanism, so a later reuse just re-registers the entry.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=_SESSION_INDEX_TTL_DAYS)
    removed = 0
    for sid in list(_sessions.keys()):
        last_active = _sessions[sid].get("last_active")
        if not last_active:
            continue
        try:
            seen = datetime.fromisoformat(last_active)
        except (ValueError, TypeError):
            continue  # unparseable timestamp — leave it rather than guess
        if seen < cutoff:
            del _sessions[sid]
            removed += 1
    return removed


def reap_task_sessions(now: datetime | None = None) -> int:
    """Drop leaked ``is_task`` session-index entries (defense-in-depth).

    Task sessions are normally removed the moment their run finishes
    (``scheduler._run_task``'s finally). This catches the ones that leaked — a
    pre-launch failure, or a process restart mid-run — so the index can't grow
    unbounded with stale task entries (the leak that previously poisoned the
    recency lookups and bloated ``index.json``). Only ``is_task`` entries older
    than ``_TASK_SESSION_REAP_TTL_SECONDS`` (or with no/unparseable timestamp —
    legacy stubs) are removed, so a genuinely long-running task is never reaped
    out from under itself. Returns the number removed.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=_TASK_SESSION_REAP_TTL_SECONDS)
    removed = 0
    for sid in list(_sessions.keys()):
        meta = _sessions[sid]
        if not meta.get("is_task"):
            continue
        last_active = meta.get("last_active")
        if last_active:
            # Unparseable timestamp → treat as stale (fall through to reap).
            with contextlib.suppress(ValueError, TypeError):
                if datetime.fromisoformat(last_active) >= cutoff:
                    continue  # still recent — may be running; keep
        del _sessions[sid]
        removed += 1
    if removed:
        _save_sessions()
    return removed


async def reap_idle_task_sessions(interval_seconds: int = 300) -> None:
    """Periodic backstop loop for ``reap_task_sessions`` (mounted in app.py).

    Runs the sweep immediately on start (clears any backlog left by older code
    that never reaped task sessions), then every ``interval_seconds``.
    """
    while True:
        try:
            n = reap_task_sessions()
            if n:
                logger.info(f"Session index: reaped {n} stale task session(s)")
        except Exception:
            logger.exception("reap_idle_task_sessions sweep failed")
        await asyncio.sleep(interval_seconds)


def _load_sessions() -> None:
    """Load session index from disk, pruning long-dead entries on the way in."""
    global _sessions
    if _SESSION_INDEX.exists():
        try:
            _sessions = json.loads(_SESSION_INDEX.read_text())
        except (json.JSONDecodeError, OSError):
            _sessions = {}
            return
        removed = prune_dead_sessions()
        if removed:
            logger.info(f"Session index: pruned {removed} entries inactive >{_SESSION_INDEX_TTL_DAYS}d")
            _save_sessions()


def _save_sessions() -> None:
    """Persist session index to disk."""
    try:
        _SESSION_INDEX.write_text(json.dumps(_sessions, indent=2))
    except OSError as e:
        logger.error(f"Failed to save session index: {e}")


# Load on import
_load_sessions()


# ---------------------------------------------------------------------------
# Session creation / existence
# ---------------------------------------------------------------------------


def create_session() -> str:
    """Create a new session UUID and register it."""
    sid = str(uuid.uuid4())
    _sessions[sid] = {"created": True, "message_count": 0}
    _save_sessions()
    return sid


def session_exists(session_id: str) -> bool:
    """Check if a session has been used before."""
    return session_id in _sessions and _sessions[session_id].get("message_count", 0) > 0


# ---------------------------------------------------------------------------
# Session use recording / client type
# ---------------------------------------------------------------------------


def _record_session_use(session_id: str, client_type: str = "", agent: str = "") -> None:
    """Record that a session was used, optionally storing its client_type and agent."""
    if session_id not in _sessions:
        _sessions[session_id] = {"created": True, "message_count": 0}
    _sessions[session_id]["message_count"] = _sessions[session_id].get("message_count", 0) + 1
    _sessions[session_id]["last_active"] = datetime.now(timezone.utc).isoformat()
    if client_type:
        _sessions[session_id]["client_type"] = client_type
    if agent:
        _sessions[session_id]["agent"] = agent
    _save_sessions()


def get_session_client_type(session_id: str) -> str:
    """Get the client_type stored for a session (e.g. 'dashboard', 'phone')."""
    return _sessions.get(session_id, {}).get("client_type", "")


# ---------------------------------------------------------------------------
# Per-user / per-session timezone (browser-detected via client_info WS message)
# ---------------------------------------------------------------------------


def set_user_tz(user_sub: str, tz: str) -> None:
    """Record the user's browser-detected IANA timezone."""
    if user_sub and tz:
        _user_tz[user_sub] = tz


def get_user_tz(user_sub: str) -> str | None:
    """Get the most recently reported IANA timezone for a user, or None."""
    return _user_tz.get(user_sub) if user_sub else None


def set_session_user_tz(session_id: str, tz: str) -> None:
    """Snapshot the user's TZ onto a specific session (persisted to disk)."""
    if not session_id or not tz:
        return
    if session_id not in _sessions:
        _sessions[session_id] = {"created": True, "message_count": 0}
    _sessions[session_id]["user_tz"] = tz
    _save_sessions()


def get_session_user_tz(session_id: str) -> str | None:
    """Get the IANA timezone stored on a session, or None."""
    if not session_id:
        return None
    return _sessions.get(session_id, {}).get("user_tz")


def _save_pending_result(
    session_id: str,
    text: str,
    cost_usd: float = 0.0,
    duration_ms: int = 0,
    prompt: str = "",
) -> None:
    """Save a pending result for later retrieval (client disconnected mid-stream)."""
    pending_path = config.SESSIONS_DIR / f"{session_id}_pending.json"
    data = {
        "session_id": session_id,
        "text": text,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "prompt": prompt,
        "timestamp": time.time(),
    }
    try:
        pending_path.write_text(json.dumps(data, indent=2))
        logger.info(f"Saved pending result for session {session_id} ({len(text)} chars)")
    except OSError as e:
        logger.error(f"Failed to save pending result for {session_id}: {e}")


# ---------------------------------------------------------------------------
# Permission mode / security context / plan files
# ---------------------------------------------------------------------------


def get_session_mode(session_id: str) -> str:
    """Get the permission mode for a session."""
    return _session_modes.get(session_id, "auto")


def set_session_mode(session_id: str, mode: str) -> None:
    """Set the permission mode for a session.

    Used by dashboard, pump, execution layers, hooks, and session API.
    Replaces direct _session_modes[sid] = mode writes.
    """
    _session_modes[session_id] = mode


def remember_session_tool_allow(session_id: str, tool_name: str) -> None:
    """Record a user-approved MCP tool so the same tool doesn't re-prompt for
    the rest of this session. Never called for high-risk device tools — those
    re-prompt per call by design."""
    _session_tool_allows.setdefault(session_id, set()).add(tool_name)


def is_session_tool_allowed(session_id: str, tool_name: str) -> bool:
    """True if the user already clicked Allow for this tool this session."""
    return tool_name in _session_tool_allows.get(session_id, ())


def _serialize_security_ctx(ctx) -> dict:
    """Serialize a SecurityContext to a JSON-safe dict. No secrets in it (see
    _session_security). ``target_device_grants`` is a set → emit a sorted list."""
    import dataclasses
    d = dataclasses.asdict(ctx)
    d["target_device_grants"] = sorted(ctx.target_device_grants or ())
    return d


def _deserialize_security_ctx(d: dict):
    """Rebuild a SecurityContext from a persisted dict, tolerant of field drift
    (unknown keys dropped, missing keys default)."""
    import dataclasses
    from auth.path_policy import SecurityContext
    fields = {f.name for f in dataclasses.fields(SecurityContext)}
    kw = {k: v for k, v in d.items() if k in fields}
    kw["target_device_grants"] = set(kw.get("target_device_grants") or ())
    if kw.get("target_user_dirs") is None:
        kw["target_user_dirs"] = {}
    # JSON has no tuple type — restore the tuple-typed fields so a reloaded
    # context compares equal to a freshly-built one.
    for _tup in ("session_allowed_roots", "available_scopes"):
        if isinstance(kw.get(_tup), list):
            kw[_tup] = tuple(kw[_tup])
    return SecurityContext(**kw)


def _save_session_security() -> None:
    """Persist the session security index for crash recovery. Whole-file
    rewrite from memory (mirrors ``_save_sessions``); the live set is small
    (idle-reaped at 15min). Best-effort — a write failure must never break a
    warmup/close, so it only logs."""
    now = time.time()
    data = {}
    for sid, ctx in _session_security.items():
        try:
            data[sid] = {
                **_serialize_security_ctx(ctx),
                "_saved_at": _session_security_ts.get(sid, now),
            }
        except Exception:
            continue  # a non-serializable entry must not block the rest
    try:
        _SECURITY_INDEX.write_text(json.dumps(data, indent=2))
    except OSError as e:
        logger.error(f"Failed to save session security index: {e}")


def load_session_security() -> None:
    """Reload the persisted security index into memory on startup. Called
    from the app lifespan (after imports settle — it constructs SecurityContext).
    Drops entries older than the session-JWT TTL and rewrites the pruned file."""
    if not _SECURITY_INDEX.exists():
        return
    try:
        raw = json.loads(_SECURITY_INDEX.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load session security index: {e}")
        return
    now = time.time()
    loaded = 0
    for sid, d in (raw or {}).items():
        try:
            saved_at = float(d.get("_saved_at", 0) or 0)
            if now - saved_at > _SECURITY_TTL_S:
                continue  # stale crash orphan — its JWT would be expired
            _session_security[sid] = _deserialize_security_ctx(d)
            _session_security_ts[sid] = saved_at or now
            loaded += 1
        except Exception:
            logger.warning("Skipping unreadable security index entry %s", sid[:8])
    if loaded:
        logger.info("Reloaded %d session security context(s) from disk", loaded)
        _save_session_security()  # drop the pruned/unreadable entries from disk


def set_session_security(session_id: str, ctx) -> None:
    """Store the security context for a session (path-based access control).

    Persisted so the permission gate stays ENFORCING for a session that
    survives a proxy crash; re-persisted on every (re)warmup, cleared on close.

    Stamps ``cli_session_id`` here — the ONE registration choke point every
    builder funnels through — so the Claude runtime-tree carve
    (``path_policy_v2``) is scoped to this session without threading the id
    through each SecurityContext constructor. Only a UUID-shaped key stamps
    (the warmup accepts client-supplied ids — junk fails closed to no carve);
    a context that already carries an id is left untouched.
    """
    if not getattr(ctx, "cli_session_id", ""):
        # Junk ids fail the UUID parse and fall closed to no carve; TypeError
        # also covers duck-typed test contexts without the field.
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            uuid.UUID(session_id)
            ctx = dataclasses.replace(ctx, cli_session_id=session_id.lower())
    _session_security[session_id] = ctx
    _session_security_ts[session_id] = time.time()
    _save_session_security()


def get_session_security(session_id: str):
    """Get the security context for a session, or None if not set."""
    return _session_security.get(session_id)


def refresh_target_allow_full_fs(machine_id: str, allow_full_fs: bool) -> int:
    """Live-update cached ``allow_full_fs`` on every warm session targeting
    ``machine_id``. Returns the count updated.

    ``SecurityContext`` is frozen and baked at warmup, so a dashboard toggle
    of ``remote_machines.allow_full_fs`` would otherwise not take effect until
    the session re-warms. The satellite's own policy cache refreshes live via
    the ``policy_update`` WS message, but the PROXY-side gate (the primary one
    for Read/Write/Edit/Bash/MCP) reads this cached context. Swapping in an
    updated context makes the NEXT tool call on a live session honor the new
    policy — no re-warm needed. Disabling already fails safe via the
    satellite re-check; this closes the enabling case.
    """
    import dataclasses
    updated = 0
    for sid, ctx in list(_session_security.items()):
        if getattr(ctx, "target_machine_id", "") != machine_id:
            continue
        if bool(getattr(ctx, "target_allow_full_fs", False)) == bool(allow_full_fs):
            continue
        try:
            _session_security[sid] = dataclasses.replace(
                ctx, target_allow_full_fs=bool(allow_full_fs),
            )
            updated += 1
        except Exception:
            logger.exception(
                "refresh_target_allow_full_fs failed for session %s", sid[:8],
            )
    if updated:
        _save_session_security()  # keep the persisted copy in sync with the toggle
    return updated


def refresh_target_device_grants(machine_id: str, device_grants: set) -> int:
    """Live-update cached ``target_device_grants`` on every warm session
    targeting ``machine_id``. Returns the count updated.

    Mirrors ``refresh_target_allow_full_fs``: ``SecurityContext`` is frozen and
    baked at warmup, so a dashboard toggle of ``remote_machines.device_grants``
    would otherwise not take effect until re-warm. The MCP is ALREADY attached
    for the session (the build-time gate ran at warmup), so a mid-session GRANT
    can't retroactively attach it; but a mid-session REVOKE swaps the context so
    the per-tool device-control gate (the auto-approve hook) stops honoring the
    capability on the next call. Only dashboard-chat sessions carry
    ``target_machine_id`` (task/meeting/phone contexts don't), so this matches
    ``allow_full_fs``'s live-refresh reach.
    """
    import dataclasses
    grants = set(device_grants or set())
    updated = 0
    for sid, ctx in list(_session_security.items()):
        if getattr(ctx, "target_machine_id", "") != machine_id:
            continue
        if set(getattr(ctx, "target_device_grants", set())) == grants:
            continue
        try:
            _session_security[sid] = dataclasses.replace(
                ctx, target_device_grants=set(grants),
            )
            updated += 1
        except Exception:
            logger.exception(
                "refresh_target_device_grants failed for session %s", sid[:8],
            )
    if updated:
        _save_session_security()  # keep the persisted copy in sync with the toggle
    return updated


def set_session_claude_dir(session_id: str, host_path: str) -> None:
    """Store the host path to this session's persistent .claude/ directory."""
    _session_claude_dirs[session_id] = host_path


def get_session_claude_dir(session_id: str) -> str | None:
    """Get the host path to this session's .claude/ directory, or None."""
    return _session_claude_dirs.get(session_id)


def set_session_codex_dir(session_id: str, host_codex_home: str, sandbox_cwd: str) -> None:
    """Record the host CODEX_HOME + sandbox CWD for a Codex interactive session so the
    rollout tailer can locate this session's rollout JSONL (stamps the spawn time used
    as the fresh-rollout lower bound)."""
    _session_codex_dirs[session_id] = {
        "home": host_codex_home, "cwd": sandbox_cwd, "started_at": time.time(),
    }


def get_session_codex_dir(session_id: str) -> dict | None:
    """Get the Codex interactive context ({home, cwd, started_at}) for a session."""
    return _session_codex_dirs.get(session_id)


def get_permission_queue(session_id: str) -> asyncio.Queue:
    """Get or create a permission request queue for a session."""
    if session_id not in _permission_emitters:
        _permission_emitters[session_id] = asyncio.Queue()
    return _permission_emitters[session_id]


async def wait_for_permission(
    request_id: str, session_id: str = "", timeout: float = 120.0,
) -> bool:
    """Block until the user responds to a permission request. Returns True if approved.

    ``session_id`` indexes the request so an abort/close can release it;
    pass it for every gate that a warm daemon could leave pending.
    """
    event = asyncio.Event()
    _permission_events[request_id] = event
    if session_id:
        _session_permission_requests.setdefault(session_id, set()).add(request_id)
        _permission_request_sessions[request_id] = session_id
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return _permission_decisions.pop(request_id, True)
    except asyncio.TimeoutError:
        # Fail CLOSED: an unanswered gate must never turn into an approval
        # (matches the hook gate's transport-failure posture).
        return False
    finally:
        _permission_events.pop(request_id, None)
        if session_id:
            _permission_request_sessions.pop(request_id, None)
            reqs = _session_permission_requests.get(session_id)
            if reqs is not None:
                reqs.discard(request_id)
                if not reqs:
                    _session_permission_requests.pop(session_id, None)


def resolve_permission(request_id: str, approved: bool) -> bool:
    """Resolve a pending permission request. Returns True if the request existed."""
    event = _permission_events.get(request_id)
    if event is None:
        return False
    _permission_decisions[request_id] = approved
    event.set()
    return True


def get_permission_request_session(request_id: str) -> str | None:
    """The session a pending permission request is bound to.

    ``None`` for an unknown request or one whose ``wait_for_permission``
    call didn't pass a session_id. Responder paths use this to check that
    whoever answers a permission/plan prompt is actually driving the
    session it belongs to.
    """
    return _permission_request_sessions.get(request_id)


async def wait_for_question(
    request_id: str, session_id: str = "", timeout: float = 604800.0,
) -> dict:
    """Block until the user answers a Codex ``request_user_input`` question.

    Returns the answers MAP ``{<id>: {"answers": [...]}}`` (keyed by the verbatim
    codex question id). On timeout / abort release, returns ``{}`` (empty answers)
    so the held request unwinds and the turn continues rather than hanging.
    ``session_id`` indexes it (shared with permissions) for abort/close release.
    """
    event = asyncio.Event()
    _question_events[request_id] = event
    if session_id:
        _session_permission_requests.setdefault(session_id, set()).add(request_id)
        _permission_request_sessions[request_id] = session_id
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return _question_answers.pop(request_id, {})
    except asyncio.TimeoutError:
        return {}
    finally:
        _question_events.pop(request_id, None)
        _question_answers.pop(request_id, None)
        if session_id:
            _permission_request_sessions.pop(request_id, None)
            reqs = _session_permission_requests.get(session_id)
            if reqs is not None:
                reqs.discard(request_id)
                if not reqs:
                    _session_permission_requests.pop(session_id, None)


def resolve_question(request_id: str, answers: dict) -> bool:
    """Resolve a pending question with the user's answers. True if it existed."""
    event = _question_events.get(request_id)
    if event is None:
        return False
    _question_answers[request_id] = answers if isinstance(answers, dict) else {}
    event.set()
    return True


def resolve_session_permissions(session_id: str, approved: bool = False) -> int:
    """Release every pending permission/question request for a session (deny/empty).

    Called on abort / session close / daemon death. Under the warm app-server
    model the daemon, its MCP subprocesses and the stdio interceptor all survive
    ``turn/interrupt`` — nothing drops the in-flight hook connection — so without
    an explicit release a pending wait hangs to its 7-day timeout and holds the
    interceptor pipe. A held ``request_user_input`` question hangs the same way,
    so release those too (empty answers). Returns the number of waiters released.
    """
    rids = list(_session_permission_requests.get(session_id, ()))
    released = 0
    for rid in rids:
        if rid in _question_events:
            if resolve_question(rid, {}):
                released += 1
        elif resolve_permission(rid, approved):
            released += 1
    _session_permission_requests.pop(session_id, None)
    if released:
        logger.info(
            f"resolve_session_permissions: released {released} pending "
            f"permission(s) for session={session_id[:8]} (approved={approved})"
        )
    return released


def set_meeting_session_info(
    agent_session_id: str,
    parent_session_id: str,
    pump_session_id: str,
    agent_slug: str,
    parent_chat_id: str,
) -> None:
    """Register a meeting agent session's parent and pump context.

    Called by the meeting orchestrator after creating agent sessions.
    The hook route resolver (api/hooks) uses this to inherit the parent
    chat's permission mode and rebind every out-of-band hook event —
    permission prompts, tool results, media artifacts, subagent stops —
    to the meeting pump's queue and the meeting's parent chat.
    """
    _meeting_session_info[agent_session_id] = {
        "parent_session_id": parent_session_id,
        "pump_session_id": pump_session_id,
        "agent_slug": agent_slug,
        "parent_chat_id": parent_chat_id,
    }


def get_meeting_session_info(agent_session_id: str) -> dict | None:
    """Get meeting context for an agent session, or None if not a meeting session."""
    return _meeting_session_info.get(agent_session_id)


def cleanup_meeting_session_info(agent_session_id: str) -> None:
    """Remove meeting session mapping (agent left or meeting ended)."""
    _meeting_session_info.pop(agent_session_id, None)


def cleanup_session_permission_state(session_id: str) -> None:
    """Clean up permission state when a session's stream ends."""
    # Release any waiter still blocked on this session FIRST (deny) so a close
    # that races an in-flight prompt doesn't strand it (and its MCP pipe).
    resolve_session_permissions(session_id, approved=False)
    # The session is being forgotten for good — clear any liveness badges it
    # left behind (also pops both registries, incl. the bg-command one).
    clear_session_liveness(session_id, reason="session_closed")
    _session_modes.pop(session_id, None)
    _session_tool_allows.pop(session_id, None)
    _permission_emitters.pop(session_id, None)
    _had_security = _session_security.pop(session_id, None) is not None
    _session_security_ts.pop(session_id, None)
    if _had_security:
        _save_session_security()  # drop from disk so a replayed JWT is denied
    _session_claude_dirs.pop(session_id, None)
    _session_codex_dirs.pop(session_id, None)
    _meeting_session_info.pop(session_id, None)
    _subagent_registries.pop(session_id, None)
    # Drop the session's brokered MCP secrets (in-memory only) so a
    # replayed capability token finds nothing after close. Late import keeps this
    # hub module free of core-package deps at load time.
    from core.credentials import mcp_broker
    mcp_broker.purge_session(session_id)
    # Tear down any browser (camoufox) context bound to this session so a killed
    # proxy session frees its browser tab immediately — the router's idle-GC is
    # the backstop. Best-effort, fire-and-forget; never blocks teardown.
    with contextlib.suppress(Exception):
        from services.infra import browser_session
        browser_session.schedule_close(session_id)


# ---------------------------------------------------------------------------
# Location bridge (MCP → proxy → WS → dashboard → WS → proxy → MCP)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pending result retrieval / adapter context
# ---------------------------------------------------------------------------


def get_pending_result(session_id: str) -> dict | None:
    """Read and delete a pending result file (one-time retrieval)."""
    pending_path = config.SESSIONS_DIR / f"{session_id}_pending.json"
    if not pending_path.exists():
        return None
    try:
        data = json.loads(pending_path.read_text())
        pending_path.unlink()
        logger.info(f"Retrieved pending result for session {session_id}")
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to read pending result for {session_id}: {e}")
        return None
