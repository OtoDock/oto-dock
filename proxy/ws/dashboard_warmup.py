"""Session warmup + spawn: pre-warm, warmup (inline reuse or backgrounded spawn
tail), deferred mode/model re-apply, and the single create/resume funnel every
spawn path goes through. The DETACHED pre-warm (no connection), its rollback
and the model-foreign check live in ws/dashboard_prewarm.py.

WarmupController is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
Behavior is pinned by tests/session/test_ws_dashboard_*.
"""

import asyncio
import contextlib
import functools
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import config
from auth import roles
from auth.providers import acting_role_of
from core import placement
from core.config.config_builder import (
    build_agent_config,
    release_config_seat,
)
from core.config.task_config_builder import rewarm_task_identity
from core.events import chat_writer, input_queue
from core.events.stream_pump import _active_pumps
from core.execution_layer import ASK_MODE, ExecutionLayer, MachineReach, ResumeVerdict
from core.sandbox.session_config_dir import (
    AgentStateRefused,
    refuse_agent_state_below_editor,
)
from core.session import (
    interactive_session,
    session_kind,
    visibility as _vis,
    warmup_registry,
)
from core.session.resume_probe import (
    RESUME_PROBE_BUDGET_INLINE_S,
    RESUME_PROBE_BUDGET_WARMUP_S as RESUME_PROBE_BUDGET_WARMUP_S,  # read by the spawn tail through this module
    ResumeUndecided,
    settle_resume,
)
from core.session.session_manager import (
    get_execution_layer,
    get_layer_by_path,
    get_layer_capabilities,
    resolve_execution_path,
)
from core.session.session_state import (
    clear_session_liveness,
    get_session_mode,
    get_user_tz,
    set_session_mode,
    set_session_user_tz,
)
from services.engines import subscription_pool
from services.engines.subscription_pool import NoSubscriptionError
from services.remote import remote_status
from storage import database as task_store, remote_store
from storage.agents import agent_store
from storage.pg import run_db, run_db_fast
from ws import wire_events as wire

# ws/dashboard.py assembles the connection from the mixins at its end, so
# its helpers are imported inside the functions that call them: this
# module imports first too (tests/core/test_import_layers.py).
from ws.dashboard_chat_support import chat_mode_for_path, coerce_chat_mode
from ws.dashboard_chat_text import (
    TurnDeferred,
    grace_layer,
    live_model,
)
from ws.dashboard_prewarm import (
    _model_foreign_to_engine,
    _pre_warmup_rollback,
    _schedule_pre_warmup_rollback,
)
from ws.dashboard_warmup_tail import WarmupTail, _persist_spawn, _TailArgs, _TailRun

if TYPE_CHECKING:
    from ws.dashboard import _SpawnResult

logger = logging.getLogger("otodock")


def _engine_default_model(execution_path: str) -> str:
    """What an unpinned agent runs on ``execution_path`` (its declared
    default, or the tier below it), or "" when nothing is enabled there."""
    try:
        return config.resolve_layer_default_model(execution_path)
    except RuntimeError:
        return ""


@dataclass
class _WarmupPlan:
    """One warmup's request and what its phases settle before the tail is
    snapshotted: the head's role and path, then the chat row's values (an
    existing chat) or the minted chat's."""
    agent: str
    msg: dict
    requested_model: str
    requested_exec_mode: str
    requested_theme: str
    warmup_role: str
    effective_exec_path: str
    permission_mode: str
    cid: str | None
    pw_task: asyncio.Task | None  # the in-flight eager pre-warm, snapshotted before the chat is read
    old_session_id: str | None = None
    chat: dict | None = None  # the existing chat's row; None for a new chat
    chat_model: str = ""  # loaded from the chat row, else the agent default
    chat_exec_mode: str = ""  # per-chat interactive override (loaded/persisted by the phases)
    # New-chat-only: the tail's pre-warm match key; "" on the existing-chat
    # path, which skips the mint and still snapshots them.
    target_model: str = ""
    consume_effective_role: str = ""
    # Whether THIS warmup minted the chat row: the dashboard moves the
    # new-chat page's draft onto a chat id only when the frame says so
    # (a resumed chat's frame must never take it). Derived from the mint
    # branch, not the request: a denied or vanished chat_id falls through
    # to a mint too.
    minted: bool = False
    # The checked place the page picked (``dashboard_chat_move.place_new_chat``:
    # place, label, default; place "" for the default); when it was minted.
    placed: tuple[str, str, str] = ("", "", "")
    minted_at: float = 0.0


class WarmupController(WarmupTail):
    """Session warmup + spawn: pre-warm, warmup (inline reuse or backgrounded
    spawn tail), deferred mode/model re-apply, and the single create/resume
    funnel every spawn path goes through."""

    # The connection state this mixin assigns, declared where it assigns it:
    # the type checker reads an attribute a mixin assigns from the mixin
    # first. Annotations only (strings), no class attributes.
    _pre_warmed_agent: "str | None"
    _pre_warmed_at: "float"
    _pre_warmed_exec_path: "str"
    _pre_warmed_model: "str"
    _pre_warmed_role: "str"
    _pre_warmed_place: "str"
    _pre_warmed_sid: "str | None"
    _warmup_abort_chat: "str | None"
    _warmup_chat_id: "str | None"
    _warmup_task: "asyncio.Task | None"
    agent_name: "str"
    chat_id: "str | None"
    deferred_for: "str"
    deferred_mode: "str"
    deferred_model: "str"
    layer: "ExecutionLayer | None"
    session_id: "str | None"

    def _target_mismatch_fields(self, chat_id: str) -> dict:
        """Locality-mismatch fields for warmup frames, or {}.

        A pinned chat keeps running on its pin (config_builder honors it
        verbatim), so the session's own resolved target can never disagree
        with the pin — the mismatch is computed against an INDEPENDENT
        resolve of the agent's CURRENT target for this user. Attached only
        for the chat owner / a platform admin: a non-owner's resolve is
        forced 'local', which would advertise a move the move op refuses.
        Best-effort — a lookup failure just means no banner."""
        try:
            chat = task_store.get_chat(chat_id) or {}
            pin = chat.get("execution_target") or ""
            if not pin:
                return {}
            agent = chat.get("agent") or ""
            # A Direct LLM chat always runs on the server (config_builder
            # pins it local whatever the agent's target says), so there is
            # nothing to move it to — no banner, even when the agent's own
            # layer default resolves to a machine.
            _wc = get_layer_capabilities(
                resolve_execution_path(agent, chat.get("execution_path") or ""))
            if _wc is not None and not _wc.runtime.supports_remote_execution:
                return {}
            role = acting_role_of(self.user_sub, agent)
            if chat.get("user_sub") != self.user_sub and not roles.is_admin(role):
                return {}
            resolved, _ = remote_status.resolve_execution_target(
                agent, self.user_sub, role,
            )
            if not resolved or resolved == pin or placement.is_offline_sentinel(resolved):
                return {}

            from services.remote.chat_targets import place_phrase
            return {
                "pinned_target": pin,
                "pinned_label": place_phrase(pin),
                "resolved_target": resolved,
                "resolved_label": place_phrase(resolved),
            }
        except Exception:  # noqa: BLE001  # the target-mismatch banner is optional, the warmup goes on
            logger.debug("target-mismatch fields failed for %s", chat_id,
                         exc_info=True)
            return {}

    async def _handle_pre_warmup(self, msg: dict):
        """Pre-warm a session for a new chat (MCP init in background).

        Creates a session via the execution layer but does NOT create a DB
        chat.  The session is stored in _pre_warmed_sid and reused by
        _handle_warmup when the user sends their first message.
        """
        from ws.dashboard import _resolve_session_interactive

        agent = msg.get("agent", "")
        if not agent:
            await self._send_error("Agent name required")
            return
        if not self._can_access_agent(agent):
            await self._send_error(f"Access denied for agent '{agent}'")
            return
        if not await run_db(agent_store.agent_exists, agent):
            await self._send_error(f"Agent '{agent}' no longer exists")
            return

        permission_mode = chat_mode_for_path(msg.get("permission_mode", ""), "")
        requested_model = await live_model(msg.get("model", ""))
        requested_exec_path = msg.get("execution_path", "")

        # Resolve current per-agent role — needed for both the match check
        # below and the new-pre-warm path (so the spawned session's
        # SecurityContext gets the per-agent role, not the platform role).
        # Role is part of the match key so per-agent role
        # reassignments (e.g. user demoted from editor to viewer) invalidate
        # the pre-warm and force a fresh session with the right role.
        pw_effective_role = await run_db(
            acting_role_of, self.user_sub, agent, fallback_user=self.user,
        )

        # Already pre-warmed for same agent+model+role+place and alive -> reuse.
        # Model is part of the match key because CLI sessions are spawned with
        # --model <name> and we can't swap models on a running process.
        # Role is part because the session's SecurityContext is baked at start,
        # and the new chat's picked place: a pick elsewhere closes it below.
        from ws import dashboard_chat_move
        place_key = dashboard_chat_move.picked_place(msg)
        target_model = requested_model or await run_db(config.get_cli_model, agent)
        if (self._pre_warmed_sid and self._pre_warmed_agent == agent
                and self._pre_warmed_model == target_model
                and self._pre_warmed_role == pw_effective_role
                and self._pre_warmed_place == place_key):
            # A pre-warm is local: asked on its engine's local layer, never the resolver's.
            from core.session.session_manager import layer_for_prewarm
            pw_layer = layer_for_prewarm(self._pre_warmed_sid, self._pre_warmed_exec_path)
            if pw_layer is not None and await pw_layer.is_session_alive(self._pre_warmed_sid):
                logger.info(
                    "WS dashboard pre_warmup: reusing existing "
                    "session=%s, agent=%s, model=%s, role=%s",
                    self._pre_warmed_sid[:8], agent, self._pre_warmed_model, self._pre_warmed_role
                )
                await self._send({"type": wire.PRE_WARMUP_READY, "session_id": self._pre_warmed_sid})
                return

        # Pre-warmed for a different agent/model/role/place (or dead) -> close old
        if self._pre_warmed_sid:
            await self._release_prewarm()

        agent_cfg = None
        started = False
        build = None
        new_sid = str(uuid.uuid4())
        try:
            def _prewarm_path() -> tuple[str, bool]:
                path = resolve_execution_path(agent, requested_exec_path)
                return path, _model_foreign_to_engine(requested_model, path)

            resolved_exec_path, foreign_model = await run_db(_prewarm_path)
            # The frame's model belongs to the engine the page showed; when
            # that is not the engine this pre-warm resolved, building would
            # acquire (and refuse) credentials for the wrong provider. A
            # pre-warm is an optimisation — skip, the real warmup decides.
            if foreign_model:
                logger.info(
                    "WS dashboard pre_warmup: skipped (model %s "
                    "is not a %s model) agent=%s",
                    requested_model, resolved_exec_path, agent
                )
                return
            # A pre-warm needs room for itself and one more session, nobody
            # waiting for a slot, and the person below their cap; otherwise it
            # is skipped without a word (nothing was asked for yet).
            from core import concurrency
            if not concurrency.prewarm_allowed(resolved_exec_path, user_sub=self.user_sub):
                logger.info("WS dashboard pre_warmup: skipped (no room for two) agent=%s", agent)
                return
            # A refused place or a machine pre-warms nothing; the platform pins the build.
            pinned = await dashboard_chat_move.prewarm_place(self, agent, place_key,
                                                             resolved_exec_path)
            if pinned is None:
                return

            # Build the config FIRST so the resolved execution target + the
            # interactive decision are known BEFORE we acquire a slot or spawn.
            # From here on every early exit returns the pool seat the build
            # acquired (release_config_seat) — a skipped pre-warm used to
            # leak one seat per chat opened on a remote or interactive agent.
            # Shielded: the seat is taken inside the build's thread, so a
            # cancel landing here must not lose the config that holds it —
            # the rollback waits for the build and returns the seat.
            build = asyncio.ensure_future(build_agent_config(
                agent_name=agent, user=self.user, user_sub=self.user_sub,
                user_role=pw_effective_role, permission_mode=permission_mode,
                client_type=session_kind.DASHBOARD.name, resume=False,
                model=requested_model,
                execution_path=resolved_exec_path,
                session_id=new_sid,
                pinned_target=pinned,
            ))
            agent_cfg = await asyncio.shield(build)
            # Skip pre-warm for a REMOTE target: it would spawn a real session on
            # the satellite (counting against THAT satellite's budget) for a chat
            # the user may never send. The first real send warms it on demand.
            if not placement.is_local(agent_cfg.execution_target):
                logger.info("WS dashboard pre_warmup: skipped (remote target) agent=%s", agent)
                release_config_seat(new_sid, agent_cfg)
                return
            # Interactive agents are NOT eagerly pre-warmed: the interactive
            # cold-start spawns a FRESH session (`_spawn_tail` skips reusing a
            # pre-warm when interactive), so a -p pre-warm here is a throwaway
            # session that ALSO runs a redundant background MCP install. Skip it;
            # the first send warms interactively and waits for the install.
            if await run_db_fast(_resolve_session_interactive, agent_cfg):
                logger.info("WS dashboard pre_warmup: skipped (interactive) agent=%s", agent)
                release_config_seat(new_sid, agent_cfg)
                return
            # Local target → acquire a unit of the local ceiling G before
            # spawning. Speculative: a refusal for lack of room is silent.
            from core.concurrency import acquire_chat_slot
            adm = await acquire_chat_slot(new_sid, execution_path=agent_cfg.execution_path,
                                          user_sub=self.user_sub, speculative=True)
            if not adm:
                release_config_seat(new_sid, agent_cfg)
                if adm.reason == "speculative":
                    logger.info("WS dashboard pre_warmup: skipped (no room) agent=%s", agent)
                    return
                await self._send_error(cast("str", adm.user_message))
                return
            # pw_effective_role (resolved at the top) → the spawned session's
            # SecurityContext gets the per-agent role. Install progress is
            # delivered out-of-band via the per-user broadcaster (install_registry
            # → push_install_event), so a detached pre-warm still reaches the
            # user's dashboard tabs.
            # The layer must match the target the build resolved (local here,
            # the remote case returned above): resolving again without it
            # could hand back the remote layer for an agent whose machine the
            # build had already decided to fall back from.
            self.layer = get_execution_layer(
                agent, execution_path=requested_exec_path, user_sub=self.user_sub,
                role=pw_effective_role, execution_target=agent_cfg.execution_target,
            )
            await self.layer.start_session(new_sid, agent_cfg)
            started = True

            # Propagate the user's browser-detected TZ onto the pre-warmed
            # session (client_info fires on WS open, before pre_warmup).
            _user_default_tz = get_user_tz(self.user_sub)
            if _user_default_tz:
                set_session_user_tz(new_sid, _user_default_tz)

            self._pre_warmed_sid = new_sid
            self._pre_warmed_agent = agent
            self._pre_warmed_exec_path = resolved_exec_path
            self._pre_warmed_model = agent_cfg.model
            self._pre_warmed_role = pw_effective_role
            self._pre_warmed_place = place_key
            self._pre_warmed_at = time.monotonic()
            # Track as a reapable pre-warm: if the user never sends, the fast TTL
            # reaper frees its slot + subscription (vs. holding the full idle window).
            from core.session import prewarm_session_registry as _prewarm
            await _prewarm.register(new_sid, agent=agent, user_sub=self.user_sub,
                                    role=pw_effective_role, exec_path=self._pre_warmed_exec_path,
                                    model=agent_cfg.model, place=place_key)

            await self._send({"type": wire.PRE_WARMUP_READY, "session_id": new_sid})
            logger.info(
                "WS dashboard pre_warmup: created session=%s, "
                "agent=%s, exec_path=%s, model=%s",
                new_sid[:8], agent, self._pre_warmed_exec_path, agent_cfg.model
            )
        except asyncio.CancelledError:
            # A newer pre_warmup frame (the dispatcher) or the closing socket
            # cancelled this spawn. Not an Exception, so the rollback below
            # never ran for it: the seat, the chat slot and a half-started
            # session stayed taken until a restart. The rollback is its own
            # task — a second cancel (the socket closing right after an
            # agent switch) must not cut it short too.
            _schedule_pre_warmup_rollback(
                new_sid, build, agent_cfg, started, getattr(self, "layer", None))
            raise
        except Exception as e:
            below_editor = isinstance(e, AgentStateRefused)
            if isinstance(e, NoSubscriptionError):
                # A configuration state (no usable account, a pool cap), not
                # a crash: the first real send surfaces the reason to the user.
                logger.info("WS dashboard pre_warmup skipped (reason=%s): %s", e.reason, e)
            elif below_editor:
                # A chat that runs as the agent, below the editor tier: the
                # first send's warmup card explains it, once.
                logger.info("WS dashboard pre_warmup skipped (below the editor tier) agent=%s", agent)
            else:
                logger.exception("WS dashboard pre_warmup failed: %s", e)
            await _pre_warmup_rollback(
                new_sid, None, agent_cfg, started, getattr(self, "layer", None))
            self._pre_warmed_sid = None
            self._pre_warmed_agent = None
            self._pre_warmed_exec_path = ""
            self._pre_warmed_model = ""
            self._pre_warmed_role = ""
            self._pre_warmed_place = ""
            if not below_editor:
                await self._send_error(f"Pre-warmup failed: {e}")

    async def _handle_warmup(self, msg: dict, *, background: bool = True):

        agent = msg.get("agent", "")
        if not agent:
            await self._send_error("Agent name required")
            return
        if not self._can_access_agent(agent):
            await self._send_error(f"Access denied for agent '{agent}'")
            return
        # A deleted agent must be fully unusable: a stale session can still carry
        # the slug in ``user_agents`` (so _can_access_agent passes) until the JWT
        # refreshes, so gate on live existence too. Its chats are already gone
        # (delete_agent), this stops a NEW chat from spawning on the dead slug.
        if not await run_db(agent_store.agent_exists, agent):
            await self._send_error(f"Agent '{agent}' no longer exists")
            return

        # The app on this screen at SEND time (APPS.md "Live apps"): the
        # first prompt runs as a server kick seconds later, after the overlay
        # closed on send, so the kick carries the line captured now.
        if msg.get("text") and "_focus_line" not in msg:
            try:
                from services.apps.focus_context import focus_line
                msg["_focus_line"] = await run_db(
                    focus_line, self.user_sub,
                    connection_id=getattr(self, "notify_connection_id", "") or None,
                )
            except Exception:  # noqa: BLE001  # the focus line is optional, a failure must never hold the warmup
                msg["_focus_line"] = ""

        # For an EXISTING chat the chat row is the agent of record — the frame's
        # agent can be stale (observed 2026-07-09: a post-restart redirect opened
        # agent A's chat under agent B's URL slug, and the /chat/:agent/:chatId
        # route trusts the slug). Re-warming under the frame's agent would spawn
        # a B session and overwrite A's session binding on the chat row, so
        # rebind to the chat's stored agent and re-run the SAME access gates
        # against it, fail-closed. New chats (no row yet) keep the frame agent.
        _rebind_cid = msg.get("chat_id")
        if _rebind_cid:
            _chat_agent = ((await run_db_fast(task_store.get_chat, _rebind_cid)) or {}).get("agent") or ""
            if _chat_agent and _chat_agent != agent:
                if not self._can_access_agent(_chat_agent):
                    await self._send_error(f"Access denied for agent '{_chat_agent}'")
                    return
                if not await run_db(agent_store.agent_exists, _chat_agent):
                    await self._send_error(f"Agent '{_chat_agent}' no longer exists")
                    return
                logger.warning(
                    "WS dashboard warmup: agent mismatch for chat=%s — "
                    "frame='%s', chat row='%s'; using the chat's agent",
                    _rebind_cid, agent, _chat_agent
                )
                agent = _chat_agent

        # A cold send on a chat another page is warming, or whose turn runs:
        # this page joins the chat and its text waits behind the turn in
        # progress, never a second warmup or a second first prompt.
        carries = bool(msg.get("text") or msg.get("images") or msg.get("files"))
        if (_rebind_cid and carries and not input_queue.deleting(_rebind_cid)
                and self._chat_in_progress(_rebind_cid)):
            await self._join_chat_in_progress(_rebind_cid, msg)
            return

        # The eager pre_warmup (if still in flight) is NOT awaited here anymore — a
        # slow remote pre-warm would block warmup_started (the chat row + sidebar +
        # URL) for its whole duration. The await + the pre-warmed-reuse-vs-fresh
        # decision moved INTO the backgrounded spawn (_do_warmup → _spawn_tail), so
        # the new chat appears in <1s and the reuse still happens.

        # An existing chat with no warmup in flight is registered as warming
        # before _do_warmup runs (the check and the register yield only
        # while the registry's lock is held), so a second cold send
        # meanwhile joins instead of spawning too; another page's in-flight
        # entry is never taken over. The backgrounded tail owns the entry
        # from then on; every other outcome (an inline reuse, a refusal, a
        # mint of another chat) drops it below and arms the queue for what a
        # joined page sent meanwhile.
        early = ""
        if (_rebind_cid and not input_queue.deleting(_rebind_cid)
                and warmup_registry.get(_rebind_cid) is None):
            await warmup_registry.register(_rebind_cid, self.user_sub, agent)
            early = _rebind_cid

        # Tracks the warmup-in-progress window across ALL return paths
        # (alive-session reuse, pre-warmed-reuse, new-chat) so mode_change /
        # model_change arriving during any await yields know to defer +
        # rely on the post-warmup re-apply.
        self._warmup_in_flight = True
        outcome = "skip"
        try:
            # _do_warmup returns "inline" (session ready
            # synchronously — alive-session reuse / pre-warmed match), "bg" (the
            # slow spawn was backgrounded as _warmup_task), or "skip" (denied /
            # nothing to do). The backgrounded spawn re-applies deferred state +
            # enqueues its own _server_kick from _spawn_tail; inline paths do
            # both here.
            outcome = await self._do_warmup(agent, msg)
        finally:
            self._warmup_in_flight = False
            if early and (outcome != "bg" or self.chat_id != early):
                if outcome == "inline" and msg.get("text") and self.chat_id == early:
                    # The inline kick below registers its pump a few awaits
                    # from here: the hold keeps a joined page's delivery
                    # from starting its text first (the kick's end ends it).
                    input_queue.hold_first_turn(early)
                await warmup_registry.unregister(early)
                if outcome != "inline":
                    input_queue.first_turn_not_run(early)
            if outcome == "inline":
                # Post-warmup re-apply of deferred mode/model: a mode/model
                # change that raced the synchronous warmup is applied to the new
                # session + echoed to the client so the UI stays in sync.
                await self._reapply_deferred_after_warmup(self.chat_id or "", from_warmup=True)

        # Auto-warmup callers (from _handle_chat on a dead/missing session) need
        # the session synchronously to continue the same turn — await the
        # backgrounded spawn so session_id is set before we return. The spawn
        # carried no text, so it enqueues no kick.
        if not background and outcome == "bg" and self._warmup_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._warmup_task

        # Server-owned first turn for the INLINE-ready paths: the
        # session is live now, so kick the turn. The prompt was persisted at
        # send-time in _do_warmup (server_kick skips re-persist). The
        # backgrounded path enqueues its own _server_kick after warmup_ready.
        if (outcome == "inline" and not msg.get("text") and self.chat_id
                and input_queue.peek(self.chat_id)):
            input_queue.arm(self.chat_id)
        if outcome == "inline" and msg.get("text") and self.session_id and self.chat_id:
            holder = grace_layer(self.session_id, self.layer) if self.layer else None
            await self._handle_chat({
                "text": msg.get("text", ""),
                "images": msg.get("images", []),
                "files": msg.get("files", []),
                "chat_id": self.chat_id,
                "_server_kick": True,
                "_focus_line": msg.get("_focus_line", ""),
                # The reuse above already waited out the machine's reconnect.
                "_reconnect_waited": bool(holder and holder.is_session_grace_held(self.session_id)),
            })
            input_queue.first_turn_not_run(self.chat_id)

    @staticmethod
    def _chat_in_progress(cid: str) -> bool:
        """Whether a cold send on ``cid`` joins it instead of warming it: the
        chat is getting ready (another page's warmup, its first turn still
        enqueued), a pump is registered for it (done or not: its end hands
        the queue on), or a starter holds its claim."""
        return (input_queue.getting_ready(cid) or _active_pumps.get(cid) is not None
                or input_queue.is_claimed(cid))

    async def _join_chat_in_progress(self, cid: str, msg: dict) -> None:
        """A cold send on a chat in progress: the connection joins the chat
        the way a resume does (the access check, the in-flight warmup's
        listener and history replay or the live pump's promised attach, or
        ``warmup_ready`` with the live session), becomes the session's
        notification sink as a resume does, then queues the text behind the
        turn in progress as a ``chat`` frame would, attaches to a pump that
        drives the chat, and arms the background monitors after it. A resume
        that refused, or a view-only connection, ends the page's warmup with
        a card and queues nothing."""
        await self._handle_resume_chat({"chat_id": cid})
        if self.chat_id != cid:
            await self._send({"type": wire.WARMUP_FAILED, "chat_id": cid,
                              "error": "This chat could not be opened.",
                              "reason": "session_error"})
            return
        self._register_notify_queue()
        refusal = ""
        if self._view_only:
            refusal = await self._deny_task_continue(cid, quiet=True) or (
                "This chat is one you can read but not drive.")
        if refusal:
            await self._send({"type": wire.WARMUP_FAILED, "chat_id": cid,
                              "error": refusal, "reason": wire.REASON_BELOW_EDITOR})
            return
        item = await self._prepare_turn_input(
            msg.get("text", ""), msg.get("images") or [], msg.get("files") or [])
        if item is None:
            # The refusal went out bare: the page resets its warmup as it
            # does for any cold send's refusal.
            return
        await self._queue_for_next_turn(cid, item, msg.get("queue_id"))
        live = _active_pumps.get(cid)
        if live is not None and not live.is_done:
            await self._enter_pump_loop()
        self._ensure_bg_monitor()

    async def _do_warmup(self, agent: str, msg: dict) -> str:
        self.agent_name = agent
        # Fresh warmup intent — clear any stale abort flag from a prior spawn.
        self._warmup_abort_chat = None
        # Resolve execution layer — use frontend override if provided, else agent default
        requested_exec_path = msg.get("execution_path", "")
        requested_model = msg.get("model", "")
        # Per-chat interactive override — the frontend's
        # interactive toggle. '' = unset (use stored/resolver default).
        requested_exec_mode = msg.get("execution_mode", "")
        logger.info("WS dashboard warmup: agent=%s, requested_exec_path='%s', model='%s', execution_mode='%s'", agent, requested_exec_path, requested_model, requested_exec_mode)
        def _warm_role_layer():
            # Role + layer + path in ONE executor job (the layer reads
            # remote_store for a remote target; the path is the actual one for
            # storage, never "remote").
            role = acting_role_of(self.user_sub, agent, fallback_user=self.user)
            return role, get_execution_layer(
                agent, execution_path=requested_exec_path, user_sub=self.user_sub, role=role,
            ), resolve_execution_path(agent, requested_exec_path)

        warmup_role, self.layer, effective_exec_path = await run_db(_warm_role_layer)
        permission_mode = chat_mode_for_path(msg.get("permission_mode", ""), effective_exec_path)
        requested_model = await live_model(msg.get("model", ""))  # frontend sends current selection
        # Dashboard light/dark mode → seeds Claude's TUI theme (interactive only).
        requested_theme = msg.get("theme", "")
        cid = msg.get("chat_id")
        # Task continue-gate: reject re-warm of a task this user can't continue
        # (agent-scoped → editor+; user-scoped → creator/admin) before spawning.
        if await self._deny_task_continue(cid):
            return "skip"
        p = _WarmupPlan(
            agent=agent, msg=msg, requested_model=requested_model,
            requested_exec_mode=requested_exec_mode, requested_theme=requested_theme,
            warmup_role=warmup_role, effective_exec_path=effective_exec_path,
            permission_mode=permission_mode, cid=cid,
            pw_task=self._pre_warmup_task,  # snapshot the in-flight eager pre-warm task
        )
        if verdict := await self._warm_existing_chat(p):
            return verdict
        if verdict := await self._warm_mint_chat(p):
            return verdict
        await self._warm_announce(p)

        # Snapshot everything the backgrounded spawn needs as LOCALS — the
        # connection's chat_id/agent_name/layer attributes may change underneath
        # the spawn if the user switches chats during the (slow) start_session.
        wcid = cast("str", self.chat_id)  # the announce registered it
        w_agent = self.agent_name
        w_layer = self.layer
        w_pinned = (p.chat.get("execution_target") if (p.old_session_id and p.chat) else "") or ""
        w_thread = (p.chat.get("codex_thread_id", "") if (p.old_session_id and p.chat) else "")
        # New-chat pre-warm match key (snapshotted — the attributes may change if the user
        # switches chats during the spawn). Empty/"" for existing-chat warmups.
        w_role = p.consume_effective_role
        w_target_model = p.target_model
        w_exec_mode = p.chat_exec_mode  # per-chat interactive override (snapshot)
        w_theme = p.requested_theme  # dashboard light/dark (snapshot)

        # Heartbeat fires every 15s of registry silence so reverse proxies +
        # the dashboard's 30s ping window don't time out the WS during slow
        # MCP installs. emit() updates last_emit_ts, so a noisy install
        # naturally suppresses heartbeats. Keyed by wcid (the viewed chat_id
        # may change mid-spawn).
        heartbeat_stop = asyncio.Event()

        async def _heartbeat_loop() -> None:
            try:
                while not heartbeat_stop.is_set():
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), timeout=15)
                        return
                    except asyncio.TimeoutError:
                        rec = warmup_registry.get(wcid)
                        if rec is None:
                            return
                        if time.monotonic() - rec.last_emit_ts >= 15:
                            await warmup_registry.emit(wcid, {
                                "type": wire.WARMUP_HEARTBEAT,
                                "chat_id": wcid,
                            })
            except Exception:
                logger.exception("warmup heartbeat loop crashed")

        hb_task = asyncio.create_task(_heartbeat_loop())
        w = _TailArgs(
            wcid=wcid, w_agent=w_agent, w_layer=w_layer, w_pinned=w_pinned, w_thread=w_thread,
            w_role=w_role, w_target_model=w_target_model, w_exec_mode=w_exec_mode, w_theme=w_theme,
            msg=msg, permission_mode=p.permission_mode, chat_model=p.chat_model,
            effective_exec_path=p.effective_exec_path, old_session_id=p.old_session_id,
            pw_task=p.pw_task, heartbeat_stop=heartbeat_stop, hb_task=hb_task,
            w_place=p.placed[0], w_minted_at=p.minted_at,
        )
        self._warmup_chat_id = wcid
        self._warmup_task = asyncio.create_task(self._spawn_tail(w, _TailRun()))
        return "bg"

    async def _warm_existing_chat(self, p: _WarmupPlan) -> str | None:
        """A chat_id in the request: its row's mode, model, path and pin; a
        live interactive or a grace-held session is re-attached here ("inline"),
        a deleted chat or a refused slot ends the warmup ("skip"); None when a
        spawn follows (a denied chat_id falls through to a mint)."""
        from ws.dashboard import _rewarm_chat_allowed
        if p.cid:
            # Reuse existing chat
            p.chat = await run_db_fast(task_store.get_chat, p.cid)
            if p.chat and input_queue.deleting(p.cid):
                await self._send({"type": wire.WARMUP_FAILED, "chat_id": p.cid,
                                  "error": "This chat was deleted.",
                                  "reason": "session_error"})
                return "skip"
            if p.chat and _rewarm_chat_allowed(
                    p.chat, p.cid, self.user_sub, self.user_agents):
                self.chat_id = p.cid
                # Persist the prompt at send-time for an existing-chat cold send
                # (resumed dead session): the frontend warmup carries the text
                # when its session is cold, and the server-kick skips re-persist.
                await self._persist_first_prompt(self.chat_id, p.msg.get("text", ""))
                p.old_session_id = p.chat.get("session_id")
                p.permission_mode = p.chat.get("permission_mode", p.permission_mode)
                p.chat_model = p.chat.get("model", "")
                # Per-chat execution mode: a fresh toggle in this warmup wins and
                # is persisted; else the chat's stored mode (so resume re-spawns
                # in the same mode).
                p.chat_exec_mode = p.requested_exec_mode or p.chat.get("execution_mode", "") or ""
                if p.requested_exec_mode and p.requested_exec_mode != (p.chat.get("execution_mode") or ""):
                    await chat_writer.submit(
                        p.cid,
                        functools.partial(task_store.update_chat, p.cid,
                                          execution_mode=p.requested_exec_mode),
                        label="exec_mode",
                    )
                # Use chat's stored execution_path if available
                if p.chat.get("execution_path"):
                    p.effective_exec_path = p.chat["execution_path"]
                p.permission_mode = await coerce_chat_mode(p.cid, p.permission_mode, p.effective_exec_path)
                # Resume affinity: the alive-check and the resume gate below
                # must consult the layer of the chat's PINNED target, never the
                # agent's current default — an agent retargeted between
                # sessions would otherwise ask the wrong machine about the old
                # session (a live local session reads dead, an on-disk resume
                # is refused) and the chat loses its context to a fresh spawn.
                chat_pin = p.chat.get("execution_target") or ""
                if p.chat.get("execution_path") or chat_pin:
                    self.layer = await run_db(functools.partial(
                        get_execution_layer, p.agent, execution_path=p.effective_exec_path,
                        user_sub=self.user_sub, role=p.warmup_role,
                        execution_target=chat_pin,
                    ))

                # If the process is still alive, reuse directly
                if p.old_session_id:
                    # Live INTERACTIVE session (PTY) — re-attach the viewer rather
                    # than re-spawn. The headless is_session_alive check below
                    # never sees PTY sessions (they live in interactive_session,
                    # not the layer's registry), so this must come first.
                    isess = interactive_session.get(p.old_session_id)
                    if isess is not None and isess.alive:
                        from core.concurrency import acquire_chat_slot
                        # Re-acquire with the live session's target so a REMOTE
                        # interactive re-attach never takes a local-G slot.
                        # A LIVE session is already tracked, so this is the
                        # idempotent path today — but check the Admission
                        # anyway: silently attaching an unadmitted session is
                        # the failure mode a future reordering would hit. The
                        # owner is recorded only for a connection that drives
                        # the chat; a re-attach is never capped.
                        adm = await acquire_chat_slot(
                            p.old_session_id, target=isess.target,
                            user_sub=None if self._view_only else self.user_sub,
                            per_user_cap=False)
                        if not adm:
                            await self._send_error(cast("str", adm.user_message))
                            return "inline"
                        self.session_id = p.old_session_id
                        await self._send({
                            "type": wire.WARMUP_READY,
                            "session_id": self.session_id,
                            "chat_id": self.chat_id,
                            "mode": get_session_mode(p.old_session_id),
                            "model": p.chat_model or await run_db(config.get_cli_model, p.agent),
                            "execution_path": p.effective_exec_path,
                            "interactive": True,
                            # Live turn state at attach: visiting a mid-turn
                            # interactive chat re-fires warmup_started/ready,
                            # and status broadcasts are transition-only — this
                            # field is what keeps the sidebar dot truthful
                            # (finishWarmup maps True → streaming).
                            "turn_open": isess.turn_open,
                        })
                        # Client attaches via pty_attach (see _dispatch).
                        return "inline"
                    # A machine in its reconnect grace is coming back: its live
                    # session is reused, never resumed over (still reconnecting
                    # after the wait too: the turn then waits for it).
                    holder = grace_layer(p.old_session_id, self.layer) if self.layer else None
                    if holder and (
                            await cast("ExecutionLayer", self.layer).is_session_alive(p.old_session_id)
                            or await holder.wait_session_reconnect(p.old_session_id)
                            or holder.is_session_grace_held(p.old_session_id)):
                        # Confirm the concurrency slot (idempotent for a tracked
                        # session), using the chat's pinned target so a REMOTE
                        # session is a no-op (and a local-full proxy doesn't
                        # reject reconnecting to a remote chat).
                        from core.concurrency import acquire_chat_slot
                        adm = await acquire_chat_slot(
                            p.old_session_id, target=p.chat.get("execution_target") or placement.LOCAL,
                            execution_path=p.chat.get("execution_path"),
                            user_sub=None if self._view_only else self.user_sub,
                            per_user_cap=False,
                        )
                        if not adm:
                            await self._send_error(cast("str", adm.user_message))
                            return "skip"
                        self.session_id = p.old_session_id
                        await self._send({
                            "type": wire.WARMUP_READY,
                            "session_id": self.session_id,
                            "chat_id": self.chat_id,
                            "mode": get_session_mode(p.old_session_id),
                            "model": p.chat_model or await run_db(config.get_cli_model, p.agent),
                            "execution_path": p.effective_exec_path,
                            "interactive": False,
                        })
                        return "inline"
            else:
                # Invalid/denied chat_id — will create new. Also drop any
                # chat this connection was viewing (resume_chat may have set
                # it from the SAME denied/deleted row): keeping it would make
                # the fresh spawn below adopt that chat id and overwrite its
                # session binding instead of creating a genuinely new chat.
                self.chat_id = None
                self.session_id = None
        else:
            # No chat_id — new chat. Reset viewed connection state from any previous chat
            # on this WebSocket connection.
            self._detach_pty_viewer()  # leaving any interactive chat we were viewing
            self.chat_id = None
            self.session_id = None
        return None

    async def _warm_mint_chat(self, p: _WarmupPlan) -> str | None:
        """No chat to reuse: the chat row minted with its model, role and mode
        settled (the tail's pre-warm match key) and the first prompt persisted;
        "skip" when the caller is below the editor tier of a Shared-only agent."""
        from ws.dashboard import _model_allowed_for_path
        if not self.chat_id:
            # A Shared-only agent's chats run as the agent, which takes the
            # editor tier: a caller below it is refused before the chat is
            # minted, so the agent's one history never holds their prompt
            # (an existing pool chat is held by the drive gate above).
            chat_owner = await run_db(_vis.chat_history_owner, p.agent, self.user_sub)
            if _vis.is_shared_chat_owner(chat_owner) and not roles.can_edit(p.warmup_role):
                refusal = ""
                try:
                    refuse_agent_state_below_editor(_vis.SCOPE_AGENT, p.warmup_role)
                except AgentStateRefused as e:
                    refusal = str(e)
                logger.info("WS dashboard warmup refused before the mint (below the editor tier) agent=%s", p.agent)
                await self._send({
                    "type": wire.WARMUP_FAILED,
                    "error": refusal,
                    "reason": wire.REASON_BELOW_EDITOR,
                })
                return "skip"
            # The place the page picked: a refusal mints nothing.
            from ws import dashboard_chat_move
            placed = await dashboard_chat_move.place_new_chat(self, p.agent, p.msg,
                                                             p.effective_exec_path, p.warmup_role)
            if placed is None:
                return "skip"
            p.placed = placed
            # Resolve the model + role that form the pre-warm match key (the tail
            # checks them after awaiting the eager pre-warm). Model precedence:
            # deferred (user changed during warmup) > requested (sent with warmup
            # msg) > agent default — it must match the session's spawned model
            # because CLI processes bake --model at start and can't swap live.
            # A deferral counts only when it is the new-chat page's (one made
            # for another chat while its session was away stays that chat's).
            new_chat_pick = "" if self.deferred_for else self.deferred_model

            def _pick_model(candidate: str, agent: str, path: str) -> tuple[str, bool]:
                model = candidate or config.get_cli_model(agent)
                return model, _model_allowed_for_path(model, path)

            p.target_model, model_allowed = await run_db(
                _pick_model, new_chat_pick or p.requested_model, p.agent, p.effective_exec_path)
            # A chat-switch race can leave the OTHER chat's model in the
            # selector when a new chat spawns on a different engine, and a
            # stale page can name a model the engine no longer serves — a
            # foreign model would bake into the chat row and 400 every turn
            # (see _model_allowed_for_path). Fall back to the engine's own
            # default (the agent default may belong to the agent's PRIMARY
            # layer, which is exactly what a cross-layer chat isn't on), never
            # its first row: the catalog is tier-ordered, so that is the
            # engine's most expensive model.
            if not model_allowed:
                fallback_model = await run_db(_engine_default_model, p.effective_exec_path)
                logger.warning(
                    "WS dashboard warmup: model=%s is not a "
                    "%s model — using %s",
                    p.target_model, p.effective_exec_path, fallback_model or 'the layer default'
                )
                p.target_model = fallback_model
            # Per-agent effective role at consume time: if the user's role changed
            # since the pre-warm spawned (e.g. manager→editor via admin UI/SQL), the
            # pre-warm's SecurityContext is stale → the tail's match fails and it
            # spawns a fresh session with the right role baked into its mounts/policy.
            p.consume_effective_role = await run_db(
                acting_role_of, self.user_sub, p.agent, fallback_user=self.user,
            )

            # The pre-warmed-reuse-vs-fresh decision moved into _spawn_tail: it must
            # first await the eager pre-warm (no longer done on the WS loop), so the
            # chat below is allocated + announced immediately and the reuse happens in
            # the background.
            self.chat_id = str(uuid.uuid4())
            p.minted = True
            p.chat_model = p.target_model
            p.chat_exec_mode = p.requested_exec_mode
            if not self.deferred_for:
                self.deferred_model = ""  # consumed
                if self.deferred_mode:
                    p.permission_mode = self.deferred_mode
                    self.deferred_mode = ""
            # The chat row (pinned where placed) first, then the prompt — both on the chat's lane.
            p.minted_at = time.monotonic()
            await chat_writer.submit(self.chat_id, functools.partial(
                dashboard_chat_move.mint_new_chat, self.chat_id, chat_owner, self.agent_name,
                p.permission_mode, model=p.chat_model, execution_path=p.effective_exec_path,
                execution_mode=p.chat_exec_mode, placed=p.placed, actor=self.user_sub or "",
            ), label="create_chat")
            await self._persist_first_prompt(self.chat_id, p.msg.get("text", ""))
        return None

    async def _warm_announce(self, p: _WarmupPlan) -> None:
        """The in-flight warmup registered and ``warmup_started`` emitted inline,
        so the dashboard learns the chat id before the slow spawn."""
        # For existing chats with no model stored, fill from agent default
        if not p.chat_model:
            p.chat_model = await run_db(config.get_cli_model, p.agent)

        # Register an in-flight warmup so a reconnecting WS can re-attach via
        # resume_chat and the eventual warmup_ready reaches whichever socket
        # is currently attached. Also emit warmup_started right away so the
        # dashboard learns the chat_id (load-bearing for reconnect recovery).
        # This runs INLINE (fast) so the frontend navigates immediately; the
        # slow session spawn is backgrounded below so the WS loop stays free
        # to handle chat-switch / abort during the spawn.
        # The plan settled the chat (existing or minted) before the announce.
        await warmup_registry.register(cast("str", self.chat_id), self.user_sub, self.agent_name)
        await warmup_registry.attach_listener(cast("str", self.chat_id), self._send)
        self._attached_warmups.add(cast("str", self.chat_id))
        await warmup_registry.emit(cast("str", self.chat_id), {
            "type": wire.WARMUP_STARTED,
            "chat_id": self.chat_id,
            "agent": self.agent_name,
            "execution_path": p.effective_exec_path,
            "execution_target": self.session_execution_target,
            "new_chat": p.minted,
        })

    async def _spawn_tail(self, w: _TailArgs, run: _TailRun) -> None:
        """Backgrounded session spawn. Runs the slow
        start_session OFF the WS loop so the connection stays responsive
        (chat-switch / abort during the spawn). Persists to the DB +
        warmup_registry headless, adopts the connection's VIEWED attributes
        ONLY if the user is still on the warmed chat (else it would clobber
        the chat they switched to), then enqueues a _server_kick
        so the MAIN loop drives the first turn — the tail never enters the
        pump loop itself, preserving the single-socket-reader invariant."""
        try:
            # The seed claim inside the try: a failed claim is a failed
            # warmup, and the finally below still stops the heartbeat and
            # unregisters it.
            await self._tail_claim_seed(w, run)
            await self._tail_start(w, run)
            await self._tail_abort_check(w, run)
            if run.aborted:
                return
            await self._tail_adopt(w, run)
        except NoSubscriptionError as e:
            await self._tail_failed(w, run, "subscription", e)
        except ResumeUndecided as e:
            await self._tail_failed(w, run, "undecided", e)
        except Exception as e:
            if isinstance(e, AgentStateRefused):
                # A person below the editor tier on a chat that runs as the
                # agent: the card below carries the refusal, no crash.
                logger.info("WS dashboard warmup refused (below the editor tier) chat=%s", w.wcid)
            else:
                logger.exception("WS dashboard warmup failed: %s", e)
            await self._tail_failed(w, run, "spawn", e)
        finally:
            w.heartbeat_stop.set()
            try:
                await asyncio.wait_for(w.hb_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                w.hb_task.cancel()
            # A -p first turn is about to be kicked: what is sent before it
            # starts keeps waiting behind it (core/events/input_queue.py).
            # A stopped spawn kicks nothing and holds nothing.
            if run.res is not None and not run.res.interactive and w.msg.get("text") and not run.aborted:
                input_queue.hold_first_turn(w.wcid)
            # Terminal event emitted; drop the registry entry so the next
            # chat reusing this WS doesn't see stale state.
            await warmup_registry.unregister(w.wcid)
            self._attached_warmups.discard(w.wcid)

        if run.res is None:
            # Spawn failed — warmup_failed already emitted, no kick: what
            # was sent while it got ready goes back to its authors.
            self._drop_warmup_mark(w.wcid)
            await self._return_warmup_sends(w.wcid, "turn_failed")
            return
        if run.aborted:
            # Stopped during the spawn: whatever failed after the Stop, the
            # stopped turn is never kicked, and what a teammate sent while it
            # warmed is released (idempotent: the abort check may have).
            input_queue.first_turn_not_run(w.wcid)
            return
        await self._tail_first_turn(w, run)

    def _drop_warmup_mark(self, chat_id: str) -> None:
        """A spawn of ``chat_id`` ends with no kick to take: the socket's own
        warmup mark goes only when it is this spawn's (a later spawn of the
        same socket holds the mark for its chat)."""
        if self._warmup_chat_id == chat_id:
            self._warmup_chat_id = None

    async def _return_warmup_sends(self, chat_id: str, reason: str) -> None:
        """A warmup that ends with no first turn (failed, stopped): every
        author's messages queued while it got ready go back to their
        composers with the card (the turn that would have drained them is
        not coming)."""
        input_queue.first_turn_started(chat_id)
        await input_queue.return_all(chat_id, reason)

    async def _give_reused_session_the_chats_mode(self, layer, sid: str, mode: str) -> None:
        """A pre-warm spawned with the page's mode of that moment; a pick made
        while it spawned was consumed into the chat's row at the mint. The
        chat's mode wins and reaches the reused session the way a live pick
        does (``_handle_mode_change``): the stored mode the hooks judge by,
        and the engine's own control command where it has one. The caller
        skips this while a later pick waits (``deferred_mode``): that one
        lands after ``warmup_ready`` and wins."""
        old = get_session_mode(sid) or ASK_MODE
        if old == mode:
            return
        set_session_mode(sid, mode)
        caps = layer.capabilities_for(sid)
        if "set_permission_mode" in (caps.control_commands if caps.supports_control_commands else []):
            try:
                await layer.change_mode(sid, mode)
            except Exception as e:  # noqa: BLE001  # a change the engine refuses is logged, the chat keeps its session
                logger.warning("WS dashboard warmup: mode change on the reused session failed: %s", e)
        logger.info(
            "WS dashboard warmup: reused session=%s takes the chat's mode=%s "
            "(pre-warmed as %s)", sid[:8], mode, old
        )

    async def _reapply_deferred_after_warmup(self, chat_id: str, *,
                                             from_warmup: bool = False) -> None:
        """A mode or model picked with no live session to take it, applied to
        ``chat_id``'s session now that it is live. A pick belongs to one chat
        (``deferred_for``: the chat the frame named, or "" on the new-chat
        page, for the chat the warmup mints): a warmup takes its own chat's
        pick or the new-chat page's, a turn start on a live session only a
        pick made for that chat, so a new-chat pick never reaches the chat
        the socket is still bound to."""
        from ws.dashboard import _model_allowed_for_path
        if not self.session_id or not self.layer or chat_id != (self.chat_id or ""):
            return
        if self.deferred_for != chat_id and not (from_warmup and not self.deferred_for):
            return
        try:
            alive = await self.layer.is_session_alive(self.session_id)
        except Exception:  # noqa: BLE001  # a failed liveness probe reads as not alive
            alive = False
        if not alive:
            return
        self.deferred_for = ""
        if self.deferred_mode:
            pending = self.deferred_mode
            self.deferred_mode = ""
            from ws.dashboard_chat_support import take_owed_engine_mode
            take_owed_engine_mode(self.session_id)  # this deferral is that pick
            set_session_mode(self.session_id, pending)
            if self.chat_id:
                await chat_writer.submit(
                    self.chat_id,
                    functools.partial(task_store.update_chat, self.chat_id, permission_mode=pending),
                    label="mode_change",
                )
            try:
                await self.layer.change_mode(self.session_id, pending)
            except Exception as e:  # noqa: BLE001  # a change the engine refuses is logged, the chat keeps its session
                logger.warning("Post-warmup mode re-apply failed: %s", e)
            await self._send({"type": wire.MODE_CHANGED, "mode": pending})
            logger.info(
                "WS dashboard post-warmup re-applied deferred mode=%s "
                "to session=%s", pending, self.session_id[:8]
            )
        if self.deferred_model:
            pending = self.deferred_model
            self.deferred_model = ""
            # The deferral may predate the chat binding (no path known at
            # _handle_model_change time) — validate against the layer that
            # actually spawned before applying (see _model_allowed_for_path).
            def _deferred_check(cid: str, model: str) -> tuple[str, bool]:
                path = (task_store.get_chat(cid) or {}).get("execution_path", "") if cid else ""
                return path, _model_allowed_for_path(model, path)

            chat_path, model_allowed = await run_db(_deferred_check, self.chat_id or "", pending)
            if not model_allowed:
                logger.warning(
                    "WS dashboard deferred model DROPPED: model=%s is not a "
                    "%s model (chat=%s)", pending, chat_path, self.chat_id
                )
                return
            if self.chat_id:
                await chat_writer.submit(
                    self.chat_id,
                    functools.partial(task_store.update_chat, self.chat_id, model=pending),
                    label="model_change",
                )
            try:
                await self.layer.change_model(self.session_id, pending)
            except Exception as e:  # noqa: BLE001  # a change the engine refuses is logged, the chat keeps its session
                logger.warning("Post-warmup model re-apply failed: %s", e)
            await self._send({"type": wire.MODEL_CHANGED, "model": pending})
            logger.info(
                "WS dashboard post-warmup re-applied deferred model=%s "
                "to session=%s", pending, self.session_id[:8]
            )

    async def _create_or_resume_session(self,
        sid: str, agent: str, perm_mode: str, *, resume: bool = False,
        model: str = "", exec_path: str = "", resume_handle: str = "",
        chat_id: str = "", pinned_target: str = "", adopt: bool = True,
        chat_exec_mode: str = "", chat_theme: str = "", first_prompt: str = "",
        focus_line: str = "",
    ) -> "_SpawnResult":
        """Create a session via the execution layer.

        When resume=True, reuses the old session_id with --resume so CLI
        loads conversation history from disk.

        ``first_prompt`` is a cold interactive send's RAW text; an engine
        whose TUI takes it as a launch argument gets it stamped here (time
        and ``focus_line``, the viewer-focus line captured at send time),
        the others get it stamped at the PTY submit after spawn.

        ``adopt`` (default True): write the resolved session into the
        connection's VIEWED attributes (``session_id``/``layer``/
        ``session_execution_target``/``session_fallback_reason``). A
        backgrounded warmup spawn passes ``adopt=False`` so a chat-switch
        mid-spawn can't clobber the now-viewed chat — it adopts the returned
        ``_SpawnResult`` itself only if the user is still viewing the warmed
        chat. Always returns the ``_SpawnResult``.
        """
        from ws.dashboard import _resolve_session_interactive, _SpawnResult

        # NB: the concurrency slot is acquired AFTER build_agent_config below,
        # once the execution target is known — a REMOTE (satellite) session must
        # NOT consume a local-G slot (it's bounded by its satellite's budget).
        # See the acquire just before the interactive-mode resolution.

        # Resolve per-agent role for config builder. Re-fetch from DB on
        # every session start — `user_role` + `agent_roles` are captured
        # ONCE at WS connect time and don't propagate
        # admin role changes / per-agent role re-assignments mid-WS. New
        # sessions must see the current state so editor/viewer demotions
        # take effect without forcing the user to reconnect.
        effective_role = await run_db(
            acting_role_of, self.user_sub, agent, fallback_user=self.user,
        )

        # Spawn-collision guard (incident 2026-08-06): a LIVE interactive
        # session already owns this session id — a second runtime on the same
        # id (the observed headless pump twin) forks the transcript, and the
        # stale twin's eventual close() tears down the SHARED permission +
        # satellite secret state under the survivor. Reuse the live session
        # instead of spawning. The mode-switch flow is unaffected:
        # _handle_switch_execution_mode closes the old session BEFORE
        # re-warming, so nothing live is registered under the sid there.
        live_isess = interactive_session.get(sid)
        if live_isess is not None and live_isess.alive:
            logger.warning(
                "refusing headless spawn: live interactive session exists for %s", sid
            )
            reuse_layer = get_execution_layer(
                agent, execution_path=exec_path, user_sub=self.user_sub,
                role=effective_role, execution_target=live_isess.target,
            )
            if adopt:
                self.session_id = sid
                self.layer = reuse_layer
                self.session_execution_target = live_isess.target
                self.session_fallback_reason = None
            return _SpawnResult(
                session_id=sid, layer=reuse_layer,
                execution_target=live_isess.target, fallback_reason=None,
                interactive=True,
            )
        # Task re-warm: a `task-{run_id}` chat ALWAYS rebuilds in the task's
        # stored scope/identity (agent-scope → no user, agent role; user-scope
        # → the creator), never the viewer's. Resolved from the run row here
        # so EVERY (re)build path that funnels through this helper is covered.
        # The continue-gate (_deny_task_continue) is enforced separately at the
        # warmup / chat entry points.
        task_identity = (await run_db(rewarm_task_identity, chat_id, agent)
                         if session_kind.is_task_chat_id(chat_id) else None)
        agent_cfg = await build_agent_config(
            agent_name=agent, user=self.user, user_sub=self.user_sub,
            user_role=effective_role, permission_mode=perm_mode,
            client_type=session_kind.DASHBOARD.name, resume=resume, model=model,
            execution_path=exec_path,
            resume_handle=resume_handle,
            chat_id=chat_id,
            session_id=sid,
            task_identity=task_identity,
            pinned_target=pinned_target,
        )
        # Hard-fail if the resolved target is offline and fallback is disabled.
        # The resolver encoded this as the offline sentinel (core/placement.py).
        # Checked BEFORE get_execution_layer so the user gets the tailored
        # message below instead of the resolver's generic offline RuntimeError.
        if placement.is_offline_sentinel(agent_cfg.execution_target):
            # No slot acquired yet (acquire happens after this check); the
            # pool seat the build took IS, and every raise below abandons
            # the spawn.
            release_config_seat(sid, agent_cfg)
            offline_machine_id = placement.offline_machine_of(agent_cfg.execution_target)
            machine = await run_db_fast(remote_store.get_remote_machine, offline_machine_id)
            if not machine:
                # Deleted mid-flight: this warmup read the pin before the
                # delete's bulk chat transition cleared it. The row is fixed
                # by now — the retry fresh-resolves and auto-continues with
                # the history seed.
                raise RuntimeError(
                    "This chat's remote machine no longer exists — send "
                    "again to continue with a fresh session on the agent's "
                    "current target."
                )
            machine_label = machine.get("name") or offline_machine_id[:8]
            is_admin_target = placement.machine_is_admin_paired(machine)
            if is_admin_target:
                # Admin remote target — blocks everyone using this agent.
                msg = (
                    f"This agent's remote machine '{machine_label}' is currently offline. "
                    f"Please reconnect the remote machine or contact your admin."
                )
            else:
                # User-paired target — only this user is affected.
                msg = (
                    f"Your computer '{machine_label}' is offline. Reconnect it, or pick "
                    "another place from the Runs on menu."
                )
            raise RuntimeError(msg)

        # Acquire the concurrency slot now that the target is known. A LOCAL
        # session takes a unit of the local ceiling G; a denial carries the
        # real reason (busy vs host-memory pressure) so the user never sees
        # "too many sessions" while the admin page shows zero. A REMOTE session
        # is budgeted on its satellite, so this short-circuits to admitted (no
        # local slot). Idempotent for a reused pre-warm (already tracked) and
        # for an interactive task (already "task"). A denied new chat waits
        # its turn in the admission queue; a Direct-LLM session reserves for
        # its stdio MCPs.
        from core.concurrency import acquire_chat_slot
        adm = await acquire_chat_slot(sid, target=agent_cfg.execution_target,
                                      execution_path=agent_cfg.execution_path,
                                      user_sub=self.user_sub,
                                      queue_wait_s=config.ADMISSION_QUEUE_WAIT_S,
                                      mcp_config_path=agent_cfg.mcp_config_path or "")
        if not adm:
            release_config_seat(sid, agent_cfg)
            raise RuntimeError(adm.user_message)

        # Interactive mode: resolved once here — this
        # helper is the single funnel for build_agent_config → start_session, so
        # the per-chat override + global kill-switch are honoured for every spawn
        # path. The CLI layer's start_session reads this flag (interactive=True →
        # PTY register, no -p; else the normal pump path).
        #
        # Claude (2a/2b) AND Codex (2c) interactive can run LOCAL or on a
        # REMOTE satellite (the native TUI under a PTY, streamed over the WS) — the
        # latter only when that satellite advertises the interactive_pty capability,
        # else it falls back to headless -p instead of hanging on a pty_open the
        # satellite never answers. Any other remote layer is -p. Local interactive
        # (Claude + Codex) is unchanged.
        agent_cfg.interactive = await run_db_fast(_resolve_session_interactive, agent_cfg, chat_exec_mode)
        # Dashboard light/dark at spawn → seeds Claude's TUI theme to match.
        # Server-side re-warms (dead-session auto-resume, headless server turns)
        # carry no dashboard snapshot (chat_theme='') — fall back to the chat's
        # last BAKED theme so a light terminal never flips dark across a
        # respawn. 'dark' only for chats that never baked one (otodock-opened
        # sessions seed dark by design).
        chat_baked_theme = ""
        if not chat_theme and chat_id:
            chat_baked_theme = ((await run_db_fast(task_store.get_chat, chat_id)) or {}).get("tui_theme") or ""
        agent_cfg.interactive_theme = chat_theme or chat_baked_theme or "dark"
        # Whether an interactive spawn resumes is the ENGINE's answer: the
        # warmup's `resume` flag is keyed on the just-closed -p session (False
        # right after a -p→terminal switch), which is right for an engine that
        # resumes its session id (Claude, --resume) and wrong for one that
        # resumes by a handle whose state outlives the session (Codex: the
        # thread id and its rollout). The engine's local layer answers for a
        # remote placement too — the answer is a function of the config.
        agent_cfg.resume = get_layer_by_path(
            agent_cfg.execution_path or exec_path,
        ).resumes_interactive(agent_cfg)
        # Codex interactive cold first prompt: a FRESH spawn delivers it as the
        # `codex` launch arg → the TUI auto-runs it after MCP warm (deterministic
        # first-turn submit; the PTY type-then-Enter race is unreliable during
        # Codex's warm). On RESUME the prompt instead rides the server-side
        # submit in `_spawn_tail` (so `codex resume <tid>` continues the thread
        # and any new turn still runs). An engine whose TUI takes no launch
        # argument — and every resume — uses that server-side submit too.
        _first_prompt_in_argv = False
        _fc = get_layer_capabilities(agent_cfg.execution_path or "")
        if (agent_cfg.interactive and first_prompt and not agent_cfg.resume
                and _fc is not None and _fc.runtime.interactive_first_prompt_via_argv):
            # Stamped here, the one place the argv delivery is decided (the
            # remote start path reads the same field). No session exists
            # yet, so the user's last-known zone is the one — the same value
            # the spawn stamps onto the session below. Re-noted STAMPED so
            # the rollout tailer's duplicate-skip meets what the TUI journals
            # (the send-time note holds the raw text).
            agent_cfg.interactive_first_prompt = self._interactive_prelude(
                first_prompt, focus_line=focus_line,
            )
            from core.session.transcript_tool_events import note_sent_prompt
            note_sent_prompt(chat_id, agent_cfg.interactive_first_prompt)
            _first_prompt_in_argv = True
        # Resolve the layer from the config's already-resolved target so the
        # layer can never disagree with the config. LOCAL,
        # not the attribute yet — only adopted into the viewed `layer` below.
        resolved_layer = get_execution_layer(
            agent, execution_path=agent_cfg.execution_path or exec_path,
            user_sub=self.user_sub, role=effective_role,
            execution_target=agent_cfg.execution_target,
        )
        # Install progress is delivered out-of-band through the per-user
        # broadcaster (install_registry → ws/satellite.py::push_install_event
        # → this user's dashboard notify queues), so there is nothing to
        # attach here. A tab opened mid-install replays history on connect.
        try:
            await resolved_layer.start_session(sid, agent_cfg)
        except Exception:
            from core.concurrency import release_chat_slot
            release_chat_slot(sid)
            release_config_seat(sid, agent_cfg)
            raise
        # Propagate browser-detected TZ from the user's last client_info onto
        # this session. Required when client_info arrived before warmup (the
        # normal frontend ordering — client_info fires on WS open, warmup
        # later). Without this, the per-turn time injection would have no
        # session.user_tz and fall back to platform.
        _user_default_tz = get_user_tz(self.user_sub)
        if _user_default_tz:
            set_session_user_tz(sid, _user_default_tz)
        # Persist the resolved target so a future resume pins to it.
        # Only on a FRESH resolve (no pin) and only a real target (not the
        # offline sentinel) — a pinned resume already has it stored. Task
        # chats (task-{run}) get their pin from the scheduler at run start;
        # this site covers them too when their session is re-warmed here.
        # Keyed by the chat PARAM (not the viewed attribute) so it's correct even
        # for a backgrounded spawn on a non-viewed chat.
        # Remember the baked TUI theme for future server-side re-warms (see the
        # resolution above). Only when a REAL dashboard snapshot arrived — a
        # fallback-resolved re-warm must never overwrite the baked value with
        # 'dark'. Best-effort: a pre-migration DB lacks the column (one-time
        # manual ALTER) and a cosmetic write must not fail the spawn.
        pin = (agent_cfg.execution_target if chat_id and not pinned_target
               and not placement.is_offline_sentinel(agent_cfg.execution_target) else "")
        theme = agent_cfg.interactive_theme if chat_id and chat_theme and agent_cfg.interactive else ""
        if pin or theme:
            await chat_writer.submit(chat_id, functools.partial(_persist_spawn, chat_id, pin, theme),
                                     label="spawn_pin")
        # Adopt into the connection's VIEWED state only when asked (the common
        # inline path). A backgrounded spawn passes adopt=False and adopts the
        # returned result itself, guarded by "still viewing the warmed chat".
        if adopt:
            self.session_id = sid
            self.layer = resolved_layer
            self.session_execution_target = agent_cfg.execution_target
            self.session_fallback_reason = agent_cfg.fallback_reason
        return _SpawnResult(
            session_id=sid, layer=resolved_layer,
            execution_target=agent_cfg.execution_target,
            fallback_reason=agent_cfg.fallback_reason,
            interactive=agent_cfg.interactive,
            first_prompt_in_argv=_first_prompt_in_argv,
        )

    async def _resume_dead_session_for_chat(self,
        dead_sid: str, t_chat_id: str, layer_for_chat: ExecutionLayer, *,
        adopt: bool,
    ) -> "_SpawnResult":
        """Re-warm a dead session for ``t_chat_id``, resolving its agent / model /
        exec-path / pinned target from the CHAT ROW (never the viewed attributes).

        ``adopt=True`` is the VIEWED auto-resume — ``_create_or_resume_session``
        writes the connection's ``session_id``/``layer`` attributes. ``adopt=False``
        is the Step-6 path: a server turn (bg-nudge / delegate result) whose
        originating chat was reaped while the user views a DIFFERENT chat — warm
        it WITHOUT clobbering the viewed attributes and run headless on the
        returned ``_SpawnResult``. Raises on spawn failure (the caller handles)."""
        from ws.dashboard import _resume_username_for_chat
        chat_rec = await run_db_fast(task_store.get_chat, t_chat_id) if t_chat_id else None
        chat_perm_mode = (chat_rec or {}).get("permission_mode", ASK_MODE)
        chat_model = (chat_rec or {}).get("model", "")
        chat_agent = (chat_rec or {}).get("agent") or self.agent_name
        chat_exec_path = (chat_rec or {}).get("execution_path", "")
        chat_pinned = (chat_rec or {}).get("execution_target") or ""
        # ORDER IS LOAD-BEARING: can_resume_session BEFORE prepare_resume — the
        # remote resumability check needs the session info's machine_id, which
        # prepare_resume pops; once popped the check returns False unconditionally
        # (forcing a fresh session + dropping chat memory). Local derives its path
        # from agent+username, so it's order-independent — this order keeps both.
        # This runs on the socket's own dispatch, so the probe waits the
        # inline budget only; a machine that did not answer defers the turn
        # (every caller of _start_new_stream handles TurnDeferred) with the
        # record and the grace queue untouched. A connected machine's probe
        # stays out and a watcher arms the chat again when it answers
        # (ws/resume_wait); an away machine's turn waits for the reconnect
        # pass, as a grace-held one does.
        resume_username = await run_db(
            _resume_username_for_chat, t_chat_id or "", chat_agent, self.user.get("username", ""),
        )
        verdict = await settle_resume(
            layer_for_chat, dead_sid, agent_name=chat_agent, username=resume_username,
            budget_s=RESUME_PROBE_BUDGET_INLINE_S, keep_probe=True,
        )
        if verdict is ResumeVerdict.UNKNOWN:
            reach = layer_for_chat.resume_machine_reach(
                dead_sid, agent_name=chat_agent, username=resume_username)
            if reach is MachineReach.LOCAL:
                # This server's disk could not read the files twice: no
                # machine to wait for, the start fails as any other failure.
                raise ResumeUndecided(dead_sid, local=True)
            connected = reach is MachineReach.CONNECTED
            if connected:
                from ws import resume_wait
                resume_wait.watch(layer_for_chat, dead_sid, t_chat_id or "", chat_pinned,
                                  agent_name=chat_agent, username=resume_username)
            logger.info("WS dashboard: the turn on session %s waits — its machine %s",
                        dead_sid[:8], "did not answer the resume probe" if connected
                        else "is away")
            raise TurnDeferred(dead_sid, unanswered=connected)
        await layer_for_chat.prepare_resume(dead_sid)
        if verdict is ResumeVerdict.RESUMABLE:
            return await self._create_or_resume_session(
                dead_sid, chat_agent, chat_perm_mode, resume=True, model=chat_model,
                exec_path=chat_exec_path, chat_id=t_chat_id, pinned_target=chat_pinned,
                adopt=adopt,
            )
        # No conversation data → fresh session. Release the old slot and the
        # old binding first (prepare_resume removed the session from the
        # registry; a fresh id never meets the binding a resume would have
        # handed over at bind time).
        from core.concurrency import release_chat_slot
        release_chat_slot(dead_sid)
        subscription_pool.release_subscription(dead_sid)
        # The dead session's background work died with it — clear any stuck
        # liveness badges before the fresh session takes the chat over.
        clear_session_liveness(dead_sid, reason="resume_failed")
        new_sid = str(uuid.uuid4())
        res = await self._create_or_resume_session(
            new_sid, chat_agent, chat_perm_mode, resume=False, model=chat_model,
            exec_path=chat_exec_path, chat_id=t_chat_id, pinned_target=chat_pinned,
            adopt=adopt,
        )
        if t_chat_id:
            # Flag the DB-digest reseed: the resume gate refused, so this fresh
            # session has no context. The turn that triggered this auto-resume
            # claims the flag at the _start_new_stream chokepoint right after
            # this returns and prepends the digest — awaited so the claim
            # can never run ahead of the flag.
            await chat_writer.submit(
                t_chat_id,
                functools.partial(
                    task_store.update_chat, t_chat_id, session_id=new_sid,
                    pending_history_seed="resume_failed",
                ),
                label="resume_failed_flag",
            )
        return res
