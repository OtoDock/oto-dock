"""WebSocket dashboard endpoint — the persistent connection behind the dashboard UI.

Registered by app.py as @app.websocket("/ws/dashboard").

One WebSocket = one ``DashboardConnection`` instance: the connection's shared
state (socket, user, viewed session/chat ids, queues, ``_send``) lives as
attributes, and the handlers (warmup, chat turns, resume, permission
responses, PTY viewers, server notifications, ...) are its methods. The
socket allows exactly one reader, so ``run()`` remains the single dispatch
coroutine, multiplexing client messages / server notifications / the pump
poll. Behavior is pinned by the characterization suites
(``tests/session/test_ws_dashboard_*``): frame sequences must not change
without an explicit, justified assertion update. Closure-free helpers stay
at module level (several are imported by tests from this path).
"""

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import NamedTuple

from fastapi import WebSocket, WebSocketDisconnect

from storage import database as task_store
from storage import notification_store
from services.notifications import notification_manager
from auth.providers import validate_session_jwt
from core.session.session_state import (
    _dashboard_notify_queues,
)
from core.execution_layer import ExecutionLayer
from core.session.session_manager import get_execution_layer
from core.config.task_config_builder import resolve_task_identity
from core.events.stream_pump import (
    _active_pumps,
)
from core.remote import install_registry
from core.session import warmup_registry
from core.session import visibility as _vis
from core import execution_mode

logger = logging.getLogger("claude-proxy")

# A remote turn whose satellite event stream was severed (e.g. a reconnect
# orphaned the session queue) leaves its producer parked on q.get() with no
# activity; its pump never finishes, so a resume would re-attach to a zombie
# that re-shows "streaming" forever. If a pump claims to be streaming but its
# remote session has been idle longer than this, treat it as wedged and reap it
# (well below the 300s producer ceiling; a healthy turn advances last_activity
# on every event so it never trips this).
STALE_TURN_SECS = 90.0

# Source types whose pump is consumed OUT-OF-BAND by a non-dashboard driver.
# Today: the phone pipeline (ws/phone.py) drains the pump to play the agent's
# reply as TTS and to watch for the [CALL_COMPLETE] hangup sentinel. A dashboard
# viewer must NEVER attach() to these — ChatStreamPump.attach() is single-
# consumer, so attaching swaps the live stream away from the phone, silently
# killing the call (no TTS, no hangup). They are viewed READ-ONLY via
# chat_history. Future external inputs (e.g. an agent invoked from a website /
# webhook that awaits its reply on its own connection) join this set. NOTE:
# "task" and "meeting" pumps are deliberately NOT here — the dashboard IS their
# live viewer, so it correctly attaches to stream them.
_EXTERNAL_DRIVEN_SOURCES = frozenset({"phone"})

# Chat history loads the newest page; older turns lazy-load on scroll-up via
# GET /v1/chats/{id}?before_id= (see api/agents/chats.py, which mirrors this size).
_CHAT_PAGE = 50


def _build_chat_restore(chat_id: str) -> dict:
    """Panel-restore state computed from FULL history (window-independent), so the
    TODO panel + meeting state survive the lazy-load message window on an idle
    reload — the frontend no longer scans the loaded messages for them. For an
    ACTIVE session the pump's live_state overrides this (sent after chat_history)."""
    meeting = None
    m = task_store.get_active_meeting_for_chat(chat_id)
    if m:
        try:
            participants = json.loads(m.get("participants") or "[]")
        except (ValueError, TypeError):
            participants = []
        if not isinstance(participants, list):
            participants = []
        # The DB column stores SLUG STRINGS; the live `meeting_started` event
        # sends {slug, display_name, color} objects and the frontend renders
        # that shape (MeetingIndicator crashed the whole app on the string
        # form). Enrich to the same object shape the orchestrator emits.
        from storage import agent_store as _agent_store
        enriched = []
        for p in participants:
            if isinstance(p, dict):
                enriched.append(p)
                continue
            slug = str(p)
            a = _agent_store.get_agent(slug) or {}
            enriched.append({
                "slug": slug,
                "display_name": a.get("display_name", slug),
                "color": a.get("color", ""),
            })
        meeting = {
            "active": True,
            "participants": enriched,
            "max_turns": m.get("max_turns") or 30,
        }
    # Codex thread goal — chats.thread_goal JSON (NULL/garbage → no goal).
    goal = None
    raw_goal = (task_store.get_chat(chat_id) or {}).get("thread_goal")
    if raw_goal:
        try:
            parsed = json.loads(raw_goal)
            if isinstance(parsed, dict):
                goal = parsed
        except (ValueError, TypeError):
            pass
    return {"todos": task_store.get_last_todo_snapshot(chat_id), "meeting": meeting,
            "goal": goal}


class _SpawnResult(NamedTuple):
    """Resolved session spawn outcome from ``_create_or_resume_session``.

    Returned regardless of ``adopt``. When ``adopt=False`` the connection's
    VIEWED attributes (``session_id``/``layer``/``session_execution_target``/
    ``session_fallback_reason``) are NOT written — the caller (a backgrounded
    warmup spawn) decides whether to adopt these, so a spawn for a chat the user
    has navigated away from never clobbers the now-viewed chat's state."""
    session_id: str
    layer: ExecutionLayer
    execution_target: str
    fallback_reason: str | None
    # True when this session was spawned as the native interactive TUI under a
    # PTY rather than headless ``-p``. The caller
    # attaches a PTY viewer + skips the server-kick when set.
    interactive: bool = False
    # True when the cold first prompt was delivered as the codex launch arg
    # (auto-runs) → the caller must NOT also type it into the PTY (would double).
    first_prompt_in_argv: bool = False


def _effective_agent_role(user_sub: str, agent: str, fallback_user: dict | None = None) -> str:
    """Effective per-agent role for a user (admin > per-agent assignment >
    viewer). Reads live DB state so mid-session role changes take effect.
    Used wherever a session's execution target is (re)resolved so the layer
    matches the role the session was created with — a viewer on an admin-remote
    agent otherwise resolves to a different layer than its config."""
    live_user = task_store.get_user(user_sub) or fallback_user or {}
    if (live_user.get("role") or "") == "admin":
        return "admin"
    return task_store.get_user_agent_roles(user_sub).get(agent, "viewer")


def _task_continue_allowed(run: dict, *, effective_role: str, user_sub: str) -> bool:
    """Whether a user may CONTINUE (drive a new session for) a task run.

    A continued task is an interactive chat in the task's stored scope, so
    the gate matches the CREATE gate:

    - **Agent-scoped** task → editor+ on the agent (``can_edit_agent``: the
      same tier that may create agent-scope tasks). Continuing drives an
      agent-scope ``/workspace``-RW session, which a viewer must never do —
      even the original creator, if since demoted to viewer.
    - **User-scoped** task → the creator or a platform admin.

    ``effective_role`` is the live per-agent role from
    ``_effective_agent_role`` ("admin" for platform admins, which is why the
    user-scope branch needs no separate admin check beyond it).

    NOTE: this gates CONTINUING. Read-only VIEWING of an agent-scoped run
    (history load) follows the broader REST ``_check_run_access`` rule.
    """
    if (run.get("scope") or "agent") == "user":
        return run.get("created_by") == user_sub or effective_role == "admin"
    return effective_role in ("admin", "manager", "editor")


def _save_base64_image(data_url: str, save_dir: Path) -> dict | None:
    """Decode a base64 data URL, resize if needed, and save to disk.

    Args:
        data_url: Base64 data URL (data:image/...;base64,...)
        save_dir: Directory to save to. Required — caller picks the
            scope-correct location (user-scoped vs agent-scoped workspace).

    Returns:
        ``None`` on failure, otherwise a dict::

            {
                "path": str,         # absolute host path on disk
                "base64": str,       # base64 of the (resized/recompressed) saved bytes
                "media_type": str,   # "image/jpeg" or "image/png"
            }

        ``base64`` is the base64 of what we actually persisted (after resize +
        compression), not the original input — sending this through to a
        Direct-LLM agent's vision content block keeps payload small. CLI/Codex
        agents read from disk via their built-in Read tool and use ``path``.
    """
    try:
        import base64
        from PIL import Image
        import io

        _MAX_IMAGE_DIM = 1568

        # Parse: data:image/png;base64,iVBOR...
        header, b64data = data_url.split(",", 1)
        raw_bytes = base64.b64decode(b64data)

        # Open with Pillow to validate and optionally resize
        img = Image.open(io.BytesIO(raw_bytes))
        w, h = img.size
        original_size = len(raw_bytes)

        # Resize if either dimension exceeds the limit
        if max(w, h) > _MAX_IMAGE_DIM:
            img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM), Image.LANCZOS)
            logger.info(f"Resized image from {w}x{h} to {img.size[0]}x{img.size[1]}")

        # Save as JPEG for large photos (much smaller file size), PNG otherwise
        use_jpeg = original_size > 500_000 and img.mode in ("RGB", "RGBA", "L")
        if use_jpeg:
            if img.mode == "RGBA":
                img = img.convert("RGB")
            ext = "jpg"
            media_type = "image/jpeg"
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True)
            out_bytes = buf.getvalue()
        else:
            ext = "png"
            media_type = "image/png"
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            out_bytes = buf.getvalue()

        save_dir.mkdir(parents=True, exist_ok=True)
        filename = f"img_{uuid.uuid4().hex[:8]}.{ext}"
        filepath = save_dir / filename
        filepath.write_bytes(out_bytes)
        logger.info(
            f"Saved image: {filepath} ({w}x{h} -> {img.size[0]}x{img.size[1]}, "
            f"{original_size} -> {len(out_bytes)} bytes)"
        )
        return {
            "path": str(filepath),
            "base64": base64.b64encode(out_bytes).decode("ascii"),
            "media_type": media_type,
        }
    except Exception as e:
        logger.error(f"Failed to save image: {e}")
        return None


def _host_to_sandbox_path(host_path: str, agent_dir: Path) -> str:
    """Translate a host-absolute path under ``agent_dir`` to a sandbox-virtual path.

    Sandbox-virtual paths are what the agent sees in its prompt and tool calls:
    they start with ``/`` and are relative to the agent's bwrap-mount root
    (local) or get translated to satellite-absolute on remote (see
    ``satellite/path_translator.translate_paths_in_text``).

    Examples:
        ``<agent_dir>/users/alice/workspace/uploads/photos/img.jpg``
            → ``/users/alice/workspace/uploads/photos/img.jpg``
        ``<agent_dir>/workspace/uploads/photos/img.jpg``
            → ``/workspace/uploads/photos/img.jpg``
    """
    p = Path(host_path).resolve()
    rel = p.relative_to(agent_dir.resolve())
    return "/" + str(rel)


def _extract_server_kicks(queue: asyncio.Queue) -> list[dict]:
    """Drain a dying connection's notify queue, keeping only `_server_kick`
    items (the close-rescue path). A kick is a freshly-spawned chat's durable FIRST TURN:
    it waits in the per-connection queue while the viewed chat's turn occupies
    the main loop, and the queue dies with the connection — a refresh or
    network blip mid-turn silently dropped it ("my first message never got
    answered"). Everything else in the queue is live-push-only state that a
    reconnect re-derives; it is dropped exactly as a dead queue always did.
    """
    kicks: list[dict] = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if isinstance(item, dict) and item.get("type") == "_server_kick":
            kicks.append(item)
    return kicks


def _resolve_session_interactive(agent_cfg, chat_exec_mode: str = "") -> bool:
    """The interactive resolution for a built AgentConfig.

    The global kill-switch + per-chat override / per-agent default, gated to a CLI
    layer and a supported target: Claude AND Codex can run
    interactive LOCAL or on a REMOTE satellite that advertises the
    ``interactive_pty`` capability (else -p); any other remote layer is -p.
    Single source of truth for BOTH the spawn gate (``_create_or_resume_session``)
    and the pre-warm skip (``_handle_pre_warmup``).
    """
    exec_target = agent_cfg.execution_target or "local"
    remote_ok = False
    if exec_target != "local" and (agent_cfg.execution_path or "") in (
        "claude-code-cli", "codex-cli",
    ):
        from core.remote.satellite_connection import get_connection_manager
        remote_ok = get_connection_manager().satellite_supports_pty(exec_target)
    return (
        execution_mode.is_interactive(
            chat_override=chat_exec_mode or None,
            agent_default=agent_cfg.default_execution_mode or "",
        )
        and (agent_cfg.execution_path or "") in ("claude-code-cli", "codex-cli")
        and (exec_target == "local" or remote_ok)
    )


def _model_allowed_for_path(model: str, exec_path: str) -> bool:
    """True iff the execution layer serves this model (execution_layer_models —
    builtins + admin customs). A chat-switch race can deliver the PREVIOUS
    chat's model to this chat (the dashboard's selector fires model_change
    while the connection still points at the old chat) — a Claude model landing
    on a codex thread 400s every turn until the user flips the selector back,
    so a model foreign to the chat's layer must be refused, never applied.
    Empty model (agent/layer default) and unknown paths pass; a validation
    error passes too (this is a guard against cross-layer poison, not the
    model registry's gatekeeper)."""
    if not model or not exec_path:
        return True
    try:
        from storage import subscription_store
        return any(
            (m.get("model_id") or "") == model
            for m in subscription_store.list_models(exec_path)
        )
    except Exception:
        return True


def _resume_username_for_chat(
    cid_for_resume: str, agent_for_resume: str, viewer_username: str,
) -> str:
    """The username whose persistent dir (``.claude``/``.codex``) holds this
    chat's session file, for ``can_resume_session``. A ``task-{run}`` chat
    wrote its JSONL/rollout in the TASK's scope dir (agent-scope → ``""``;
    user-scope → the creator), NOT the viewer's — mirrors the
    ``resolve_task_identity`` rebuild ``_create_or_resume_session`` uses. A
    Shared-only agent's HUMAN chats also mount the agent scope
    (``mount_username=""`` → the JSONL lives under ``workspace/.claude``).
    Without either branch the resume CHECK looks in the viewer's dir and always
    returns False → a fresh, context-less spawn even though the conversation is
    resumable on disk."""
    if cid_for_resume.startswith("task-"):
        trun = task_store.get_run(cid_for_resume.removeprefix("task-"))
        if trun:
            tident = resolve_task_identity(
                agent_for_resume, trun.get("scope") or "agent", trun.get("created_by"),
            )
            return tident.username or ""
    if _vis.is_shared_only(agent_for_resume):
        return ""
    return viewer_username


def _rewarm_chat_allowed(
    chat: dict, cid: str, viewer_sub: str, user_agents: list[str],
) -> bool:
    """May this viewer re-warm EXISTING chat row ``chat`` (id ``cid``)?

    Own chats and a Shared-only agent's synthetic ``agent::`` chats (for
    assigned users) reuse directly. A ``task-{run}`` chat is owned by the
    synthetic ``task::<agent>`` sub (agent-scope) or the creator
    (user-scope) — never the viewer — and the continue-gate
    (``_deny_task_continue``) already vetted the viewer at every warmup/chat
    entry point, so ownership is granted here. Without the task clause the
    warmup treats a task continue as a NEW chat: the stored session_id is
    never read, the resume gate is skipped, and a fresh context-less session
    silently overwrites the chat's session binding."""
    if chat["user_sub"] == viewer_sub:
        return True
    if cid.startswith("task-"):
        return True
    return (
        _vis.is_shared_chat_owner(chat["user_sub"])
        and chat.get("agent", "") in user_agents
    )


async def ws_dashboard_handler(websocket: WebSocket):
    """Persistent WebSocket for dashboard chat interface.

    Auth: JWT from session cookie (same-origin) — validated here, before
    anything is accepted or allocated. Everything after auth lives on
    ``DashboardConnection``: one instance == one socket == one invocation.
    """
    session_cookie = websocket.cookies.get("session")
    if not session_cookie:
        await websocket.close(code=4001, reason="No session cookie")
        return

    payload = validate_session_jwt(session_cookie)
    if not payload:
        await websocket.close(code=4001, reason="Invalid or expired session")
        return

    user_sub = payload["sub"]
    user = task_store.get_user(user_sub)
    if not user:
        await websocket.close(code=4001, reason="User not found")
        return

    conn = DashboardConnection(websocket, user_sub=user_sub, user=user)
    await conn.run()


# The controller mixins import helpers back from this module, so they are
# imported AFTER those helpers are defined (safe intra-unit cycle).
from ws.dashboard_warmup import WarmupController  # noqa: E402
from ws.dashboard_chat import ChatController  # noqa: E402
from ws.dashboard_pty import PtyViewerController  # noqa: E402
from ws.dashboard_server_events import ServerNotificationController  # noqa: E402
from ws.dashboard_dispatch import ClientMessageDispatcher  # noqa: E402


class DashboardConnection(
    WarmupController, ChatController, PtyViewerController,
    ServerNotificationController, ClientMessageDispatcher,
):
    """One dashboard WebSocket connection: state + handlers.

    The former ``ws_dashboard_handler`` closures are methods; the shared
    connection state they captured (socket, user, viewed session/chat ids,
    queues, ``_send``) lives as attributes. ``run()`` is the single socket
    reader: it accepts, registers the notify queue, then multiplexes client
    messages / server notifications / the pump poll exactly as before.

    Client -> Server:
      {"type": "warmup", "agent": "...", "chat_id": "...", "permission_mode": "default"}
      {"type": "chat", "text": "...", "chat_id": "..."}
      {"type": "resume_chat", "chat_id": "..."}
      {"type": "permission_response", "request_id": "...", "approved": true}
      {"type": "mode_change", "mode": "..."}
      {"type": "model_change", "model": "..."}
      {"type": "thinking_change", "max_tokens": 16000}
      {"type": "implement_plan", "plan_path": "...", "mode": "acceptEdits"}
      {"type": "close"}

    Server -> Client:
      {"type": "warmup_ready", ...}
      {"type": "chat_history", "messages": [...]}
      {"type": "text", "content": "..."}
      {"type": "thinking", "phase": "...", "text": "..."}
      {"type": "tool_start", ...} / {"type": "tool_info", ...} / {"type": "tool_end", ...}
      {"type": "task_spawn", ...}
      {"type": "permission_prompt", ...}
      {"type": "plan_mode", ...}
      {"type": "system", ...}
      {"type": "image", ...} / {"type": "url", ...} / {"type": "file", ...}
      {"type": "metadata", ...}
      {"type": "done"}
      {"type": "error", "message": "..."}
      {"type": "mode_changed", "mode": "..."}
      {"type": "model_changed", "model": "..."}
      {"type": "queued", "index": N, "text": "..."}
      {"type": "queue_removed", "index": N}
      {"type": "queue_sent", "text": "..."}
      {"type": "user_message", "content": "..."}
    """

    def __init__(self, websocket: WebSocket, *, user_sub: str, user: dict):
        self.websocket = websocket
        self.user_sub = user_sub
        self.user = user

    async def run(self) -> None:

        self.agent_roles = task_store.get_user_agent_roles(self.user_sub)
        self.user_agents = list(self.agent_roles.keys())
        self.user_role = self.user["role"]

        await self.websocket.accept()
        logger.info(f"WebSocket dashboard connection accepted for user={self.user_sub}")

        self.session_id: str | None = None
        self.chat_id: str | None = None
        self.agent_name: str = ""
        self.message_queue: list[str] = []  # queued user messages during streaming
        self.artifact_queue: list[dict] = []  # queued display_ui backchannel interactions
        self.streaming = False
        self.deferred_model: str = ""  # model change before session exists
        self.deferred_mode: str = ""   # mode change before session exists
        self.pre_plan_mode_holder = ["default"]  # mutable container to track mode before plan mode
        self.implementing_plan: str = ""  # filename of plan being implemented (set on accept, cleared on done)
        self.chat_plan_filename: str = ""  # reused across pumps so edits update same plan
        # Chat for which _handle_resume_chat promised a pump attach ("active pump
        # found … will attach") and therefore sent TRUNCATED history (rows with id past
        # the pump's _db_msg_cutoff_id withheld as in-flight). If the pump finishes in the
        # gap before _enter_pump_loop attaches, the client would keep that
        # truncated view forever (no live_state/done ever arrives) — the loop
        # re-sends fresh history instead. Cleared on attach (promise kept) and at
        # the start of every resume.
        self.promised_pump_chat: str | None = None
        self._pre_warmed_sid: str | None = None   # session_id of pre-warmed session (no chat yet)
        self._pre_warmed_agent: str | None = None  # agent name of pre-warmed session
        self._pre_warmed_exec_path: str = ""      # execution path of pre-warmed session
        self._pre_warmed_model: str = ""          # model the pre-warmed session was spawned with
        self._pre_warmed_role: str = ""           # per-agent role at pre-warm time — invalidates if user reassigns role
        # Background task handle for an in-flight pre_warmup. Kept so resume_chat
        # / warmup / future pre_warmups can interact correctly even though
        # pre_warmup itself runs off the WS dispatcher loop (so a 5–10s remote
        # session start doesn't stall the dashboard's next click).
        self._pre_warmup_task: "asyncio.Task | None" = None
        # Handle for the BACKGROUNDED session spawn from a
        # warmup-on-send. The slow start_session runs off the WS loop so the
        # connection stays responsive (chat-switch / abort during the spawn).
        # Cancelled on abort-during-spawn (user hit stop). NOT cancelled on WS
        # close — instead `_ws_gone` flips and the spawn finishes + runs its first
        # turn HEADLESS so it still lands in the DB (preserves the
        # refresh-during-spawn durability).
        self._warmup_task: "asyncio.Task | None" = None
        self._ws_gone: bool = False
        # Abort-during-spawn. Cancelling a half-started CLI/satellite
        # process can't reliably stop it (codex/claude keep running and then answer
        # the server-kicked first turn), so instead we record the chat whose
        # in-flight warmup the user aborted. _spawn_tail (and the _server_kick
        # handler, for the just-finished-spawn race) then kill the spawned session +
        # suppress the first turn. Reset at the start of each _do_warmup.
        self._warmup_abort_chat: "str | None" = None
        self.layer: ExecutionLayer | None = None   # resolved on warmup, used by producer + handlers
        self.session_execution_target: str = "local"    # surfaced in warmup_ready for badge rendering
        self.session_fallback_reason: str | None = None  # non-None iff target differs from intent
        self._attached_warmups: set[str] = set()  # chat_ids this WS is attached to in warmup_registry
        # Tracks whether _handle_warmup is currently mid-flight. When True, a
        # mode_change / model_change that arrives still goes through the existing
        # "no session yet → set deferred" path; the _handle_warmup finally block
        # then re-applies the deferred value to the freshly-created session if
        # it's alive. Closes the bug where a mode_change consumed after warmup
        # started but before session_id assignment was silently dropped.
        self._warmup_in_flight: bool = False

        # Serialize all sends on this socket. Interactive PTY output fans out as many
        # concurrent tasks (one per output chunk) that each call _send; without this
        # lock those would interleave/clobber websocket.send_json. Sends are fast, so
        # this is cheap and also hardens the existing pump/heartbeat senders.
        self._send_lock = asyncio.Lock()

        # --- Interactive (PTY) viewer attach -------------------------------------
        # A connection views at most ONE interactive session at a time; attaching a
        # new one (or a chat-switch) detaches the prior. The PtyProcess OUTLIVES the
        # WS — detach removes this socket's output listener, it does NOT kill the
        # session (reconnect replays the scrollback ring).
        self._pty_viewer_sid: str | None = None
        self._pty_listener = None  # the bound bytes->WS listener registered on the session

        # Pending control requests to send after streaming turn completes
        self.pending_control_requests: list[tuple[str, dict]] = []  # [(subtype, kwargs), ...]

        # Notification queue for this WS connection — register immediately
        # so notifications can be delivered even before warmup/chat selection.
        # Each WS connection gets its own UUID so the multi-connection routing in
        # notification_manager can track per-tab/device visibility + platform.
        self.notify_queue: asyncio.Queue = asyncio.Queue()
        self.notify_connection_id = str(uuid.uuid4())
        notification_manager.register_user_connection(
            self.user_sub, self.notify_connection_id, self.notify_queue, platform="web",
        )
        # Send initial unread count so bell badge shows immediately
        _initial_count = notification_store.get_unread_count(self.user_sub)
        await self.websocket.send_json({"type": "notification_count", "count": _initial_count})

        # Replay in-flight MCP-install progress this user participates in, so a tab
        # opened (or transparently reconnected) mid-install renders the bar at the
        # current state instead of waiting for the next live event. Live events
        # arrive via the per-user broadcaster (push_install_event); this catches up
        # on what already fired. History is pushed into notify_queue so the drain
        # loop forwards each event like any other install event (idempotent on the
        # frontend if a live event overlaps). Scope to installs this user is a
        # participant of — exactly the broadcaster's recipient set, so a viewer on
        # an admin-shared machine catches up while unrelated users never do.
        try:
            for _inst in install_registry.snapshot_inflight():
                if self.user_sub not in _inst.participants:
                    continue
                for _ev in list(_inst.event_history):
                    try:
                        self.notify_queue.put_nowait(_ev)
                    except asyncio.QueueFull:
                        break
        except Exception:
            logger.exception("install replay-on-connect failed")

        # Reconcile satellite-update banners on (re)connect. `satellite_updating` /
        # `satellite_updated` are transient per-connection broadcasts, so a dashboard
        # briefly disconnected while its satellite restarts (common on a proxy restart:
        # both drop, the satellite reconnects on the new version while the dashboard is
        # still reconnecting) misses the `satellite_updated` → a stale "updating" banner
        # sticks until a page refresh. Send the authoritative in-flight set so the
        # frontend dismisses banners whose update already finished + shows any it missed.
        try:
            from ws import satellite as _satellite_ws
            await self.websocket.send_json({
                "type": "satellite_update_sync",
                "inflight": _satellite_ws.inflight_pushed_updates_for_user(self.user_sub),
            })
        except Exception:
            logger.exception("satellite-update reconcile-on-connect failed")

        # Authoritative "these chats are streaming right now" snapshot, so the
        # sidebar live-dots are RE-DERIVABLE instead of purely event-driven: a
        # chat that started or finished while this client was disconnected has
        # no chat_status frame to correct it — the frontend reconciles its
        # per-chat store against this set (clears stale dots, lights missed
        # ones). Pump turns + interactive PTY turns, filtered to what this
        # viewer may see (own chats + shared-only chats of accessible agents).
        try:
            from core.session.session_state import streaming_chat_ids as _pump_streaming
            from core.session import interactive_session as _isess
            from core.session.visibility import is_shared_chat_owner as _is_shared_owner
            _live_ids: list[str] = []
            _seen: set[str] = set()
            for _cid in list(_pump_streaming()) + list(_isess.streaming_chat_ids()):
                if not _cid or _cid in _seen:
                    continue
                _seen.add(_cid)
                _row = task_store.get_chat(_cid) or {}
                _owner = _row.get("user_sub") or ""
                # task:: owners mirror chat_status_targets: scheduled
                # agent-scope runs are visible to every user of the agent
                # (the Task history view is the reader).
                if _owner == self.user_sub or (
                    (_is_shared_owner(_owner) or _owner.startswith("task::"))
                    and self._can_access_agent(_row.get("agent") or "")
                ):
                    _live_ids.append(_cid)
            await self.websocket.send_json({
                "type": "chat_status_snapshot", "chat_ids": _live_ids,
            })
        except Exception:
            logger.exception("chat-status snapshot on connect failed")

        try:
            ws_closing = False
            while not ws_closing:
                # Multiplex: wait for client message, server notification, or task pump poll
                recv_task = asyncio.create_task(self.websocket.receive_text())
                notify_task = asyncio.create_task(self.notify_queue.get())

                wait_tasks: set[asyncio.Task] = {recv_task, notify_task}
                # Periodic poll for a pump that APPEARS on the viewed chat while the
                # socket is idle between turns — so resume mid-turn shows live
                # generation. Covers task/meeting turns AND a regular
                # chat's server-kicked first turn or a bg-nudge review turn that
                # starts after the user reconnected. _task_pump_poll re-attaches a
                # healthy pump and (via _handle_resume_chat) reaps a wedged one.
                poll_task: asyncio.Task | None = None
                if self.chat_id and not self.streaming:
                    poll_task = asyncio.create_task(asyncio.sleep(3))
                    wait_tasks.add(poll_task)

                done, pending = await asyncio.wait(
                    wait_tasks, return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t

                for t in done:
                    if t is recv_task:
                        try:
                            raw = t.result()
                        except RuntimeError:
                            # Starlette raises 'Cannot call "receive" once a
                            # disconnect message has been received' when the
                            # disconnect landed on a receive task that a previous
                            # iteration cancelled/consumed (browser refresh
                            # mid-multiplex). Same close as a clean disconnect —
                            # without the ERROR traceback noise.
                            raise WebSocketDisconnect(code=1006) from None
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            await self._send_error("Invalid JSON")
                            continue
                        try:
                            result = await self._dispatch_client_message(msg)
                        except RuntimeError as e:
                            # `config.resolve_agent_model` raises RuntimeError when
                            # no enabled model exists for the agent (fresh install
                            # before admin configures a model). Surface as a WS
                            # error so the user sees the actionable message instead
                            # of a silent WS close.
                            logger.warning(f"WS dashboard model resolution failed: {e}")
                            await self._send_error(str(e))
                            result = None
                        if result == "close":
                            ws_closing = True
                    elif t is notify_task:
                        notification = t.result()
                        await self._handle_server_notification(notification)
                    elif t is poll_task:
                        await self._task_pump_poll()

        except WebSocketDisconnect:
            logger.info(f"WebSocket dashboard disconnected: session={self.session_id}, chat={self.chat_id}")
        except RuntimeError as e:
            # Starlette throws RuntimeError("WebSocket is not connected. Need
            # to call 'accept' first.") when ``receive_text`` (or send_json)
            # runs on a socket whose state already flipped to DISCONNECTED.
            # This happens when the browser closes the WS in the small window
            # between ``websocket.accept()`` returning and the next receive —
            # common during page reload, multi-tab fanout, or our own
            # pre_warmup-then-warmup back-to-back dance. It is semantically
            # the same as ``WebSocketDisconnect`` (the connection is gone)
            # and must not crash the handler: an unhandled exception here
            # leaks the satellite-side session that warmup already spawned,
            # which then orphans any subsequent message the user sends on
            # what their browser still thinks is a live socket.
            msg = str(e)
            if "not connected" in msg or "accept" in msg or "disconnect message" in msg:
                logger.info(
                    f"WebSocket dashboard disconnected (mid-receive): "
                    f"session={self.session_id}, chat={self.chat_id}"
                )
            else:
                logger.error(f"WebSocket dashboard error: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"WebSocket dashboard error: {e}", exc_info=True)
        finally:
            # Mark the connection gone so a still-running
            # backgrounded warmup spawn (_warmup_task) drives its first turn HEADLESS
            # instead of enqueuing a _server_kick to this (now-dead) notify queue —
            # the spawn + first turn stay durable across a refresh/navigate during
            # the spawn window. We deliberately do NOT cancel _warmup_task here
            # (unlike _pre_warmup_task): it owns a real chat's first turn.
            self._ws_gone = True
            # Close-rescue: rescue kicks that were ALREADY enqueued — they waited behind the
            # viewed chat's turn (the main loop drains only between turns) and
            # would die with this per-connection queue. Flag-set + drain happen in
            # one synchronous step, and _spawn_tail's check+enqueue is likewise
            # atomic on the loop, so a kick either lands here or sees _ws_gone and
            # goes headless in _spawn_tail — no gap. Honors the same
            # abort-during-spawn guard as the main-loop drain; turns run as
            # fire-and-forget tasks so connection cleanup isn't delayed.
            for _kick in _extract_server_kicks(self.notify_queue):
                _kcid = _kick.get("chat_id", "")
                _ksid = _kick.get("session_id")
                if self._warmup_abort_chat == _kcid:
                    self._warmup_abort_chat = None
                    _k_layer = self._resolve_layer_for_chat(_kcid)
                    if _k_layer and _ksid:
                        try:
                            await _k_layer.abort(_ksid)
                        except Exception:
                            logger.warning(
                                f"close-rescue: abort teardown failed for chat={_kcid[:8]}"
                            )
                    continue
                logger.info(
                    f"close-rescue: WS died with a queued server kick — running "
                    f"first turn headless for chat={_kcid[:8]}"
                )
                asyncio.create_task(self._run_kick_headless(
                    _kcid, _ksid, _kick.get("text", ""),
                    _kick.get("images", []), _kick.get("files", []),
                    force_headless=True,
                ))
            # Cancel any background pre_warmup task so it doesn't keep mutating
            # this WS handler's closure (and writing to _pre_warmed_* slots) after
            # the connection is gone. We await briefly so an in-progress
            # start_session can roll back its concurrency-slot acquisition.
            if self._pre_warmup_task and not self._pre_warmup_task.done():
                self._pre_warmup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._pre_warmup_task
            # Detach from any in-flight warmups this WS was listening to so the
            # registry stops fanning future events to a closed socket. The
            # warmup itself keeps running and will emit through any other
            # listener attached (e.g. a reconnected WS via resume_chat).
            for _wcid in list(self._attached_warmups):
                await warmup_registry.detach_listener(_wcid, self._send)
            self._attached_warmups.clear()
            # Detach any interactive PTY viewer: removes this socket's output
            # listener only — the PtyProcess keeps running (reconnect replays the
            # scrollback ring; the idle reaper reclaims it if never re-viewed).
            self._detach_pty_viewer()
            # Install events need no per-connection detach: they are delivered via
            # the per-user broadcaster, and notification_manager.unregister_user_connection
            # below removes this connection from that fan-out cleanly.
            self._unregister_notify_queue()
            notification_manager.unregister_user_connection(self.user_sub, self.notify_connection_id)
            # Clean up orphaned pre-warmed session (user left without sending a message)
            if self._pre_warmed_sid:
                orphan_sid = self._pre_warmed_sid
                orphan_agent = self._pre_warmed_agent
                self._pre_warmed_sid = None
                self._pre_warmed_agent = None
                self._pre_warmed_exec_path = ""
                self._pre_warmed_model = ""
                orphan_role = self._pre_warmed_role or "manager"
                self._pre_warmed_role = ""
                try:
                    orphan_layer = get_execution_layer(orphan_agent, user_sub=self.user_sub, role=orphan_role) if orphan_agent else self.layer
                    if orphan_layer:
                        await orphan_layer.close_session(orphan_sid)
                    logger.info(f"WS dashboard: closed orphaned pre-warmed session {orphan_sid[:8]}")
                except Exception:
                    pass
                from core.concurrency import release_chat_slot
                release_chat_slot(orphan_sid)
                from core.session import prewarm_session_registry as _prewarm
                await _prewarm.discard(orphan_sid)
            # Release session_id's concurrency slot on WS disconnect.
            # The session PROCESS stays alive for reconnection (reaper handles
            # eventual cleanup), but the concurrency SLOT is freed immediately.
            # On reconnect, acquire_chat_slot() re-acquires the slot.
            if self.session_id:
                from core.concurrency import release_chat_slot
                release_chat_slot(self.session_id)
            logger.info(f"WS dashboard cleanup: session={self.session_id}, chat={self.chat_id}")


    async def _send(self, data: dict):
        try:
            async with self._send_lock:
                await self.websocket.send_json(data)
        except Exception:
            pass

    async def _send_error(self, msg: str):
        await self._send({"type": "error", "message": msg})

    def _can_access_agent(self, name: str) -> bool:
        return self.user_role == "admin" or name in self.user_agents

    def _register_notify_queue(self, *, replaces: str = ""):
        """Register/update the notification queue for the current session_id.

        ``replaces`` names the session id this registration supersedes — a
        re-warm minted a fresh id for the SAME chat. Its entry is dropped
        (identity-guarded to this connection's queue): dead-session keys
        otherwise accumulate and can misroute the server-kick session lookup
        (session_delivery / hooks pick the stale sid as the "active" one).
        Chat-SWITCH re-registers must NOT pass it — the connection's entries
        for its other, backgrounded chats are load-bearing."""
        if (replaces and replaces != self.session_id
                and _dashboard_notify_queues.get(replaces) is self.notify_queue):
            del _dashboard_notify_queues[replaces]
        if self.session_id:
            _dashboard_notify_queues[self.session_id] = self.notify_queue
            logger.info(f"WS dashboard: registered notify queue for session={self.session_id[:8]} (dict_id={id(_dashboard_notify_queues)}, len={len(_dashboard_notify_queues)})")

    def _unregister_notify_queue(self):
        """Remove notification queue for all session_ids pointing to our queue."""
        for sid in list(_dashboard_notify_queues.keys()):
            if _dashboard_notify_queues.get(sid) is self.notify_queue:
                del _dashboard_notify_queues[sid]

    def _resolve_layer_for_chat(self, cid: str) -> "ExecutionLayer | None":
        """Build the execution layer for a chat from its STORED row (agent +
        execution_path + pinned execution_target) — never the connection's
        viewed attributes. Used to drive a server turn on a chat this socket
        isn't viewing. Returns None if the chat is gone or its pinned remote is
        offline (get_execution_layer raises) so the caller defers gracefully."""
        rec = task_store.get_chat(cid) if cid else None
        if not rec:
            return None
        ag = rec.get("agent") or ""
        role = _effective_agent_role(self.user_sub, ag, fallback_user=self.user)
        try:
            return get_execution_layer(
                ag,
                execution_path=rec.get("execution_path") or "",
                user_sub=self.user_sub,
                role=role,
                execution_target=rec.get("execution_target") or "local",
            )
        except Exception as e:
            logger.warning(f"WS dashboard: cannot resolve layer for chat {cid}: {e}")
            return None

    async def _task_pump_poll(self) -> bool:
        """Check if a new pump appeared (meeting pump or task turn).

        Returns True if a pump was found and the pump loop was entered.
        Called periodically from the main message loop when viewing a
        chat with no active streaming.
        """
        if not self.chat_id or self.streaming:
            return False
        pump = _active_pumps.get(self.chat_id)
        if pump and not pump.is_done:
            # Chat has active pump (meeting or task turn) — re-send history + attach
            await self._handle_resume_chat({"chat_id": self.chat_id})
            await self._enter_pump_loop()
            return True
        if not self.chat_id.startswith("task-"):
            return False
        pump = self._find_task_pump()
        if pump and not pump.is_done:
            # Related turn has active pump — re-send history + attach
            await self._handle_resume_chat({"chat_id": self.chat_id})
            await self._enter_pump_loop()
            return True
        return False
