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
from core import drain, placement
from core.config.config_builder import (
    build_agent_config,
    release_config_seat,
)
from core.events import chat_writer, input_queue
from core.events.common_events import carries_prompt
from core.events.stream_pump import _active_pumps
from core.execution_layer import ExecutionLayer
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
    RESUME_PROBE_BUDGET_WARMUP_S as RESUME_PROBE_BUDGET_WARMUP_S,  # read by the spawn tail through this module
    ResumeUndecided,
)
from core.session.session_manager import (
    get_execution_layer,
    get_layer_capabilities,
    layer_for_chat,
    resolve_execution_path,
)
from core.session.session_state import (
    get_session_mode,
    get_user_tz,
    set_session_user_tz,
)
from services.engines.subscription_pool import NoSubscriptionError
from services.remote import remote_status
from storage import database as task_store
from storage.agents import agent_store
from storage.pg import run_db, run_db_fast
from ws import wire_events as wire

# ws/dashboard.py assembles the connection from the mixins at its end, so
# its helpers are imported inside the functions that call them: this
# module imports first too (tests/core/test_import_layers.py).
from ws.dashboard_chat_support import chat_mode_for_path, coerce_chat_mode
from ws.dashboard_chat_text import (
    NO_COMPUTE_NOT_SENT,
    grace_layer,
    live_model,
)
from ws.dashboard_prewarm import (
    _model_foreign_to_engine,
    _pre_warmup_rollback,
    _schedule_pre_warmup_rollback,
)
from ws.dashboard_warmup_spawn import (
    WarmupSpawn,
)
from ws.dashboard_warmup_tail import _TailArgs, _TailRun, machine_dropped_off

if TYPE_CHECKING:
    from ws.dashboard_dispatch import StreamCtx

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


def _built_placement(cfg):
    """The compute placement a config was built with (its security
    context's), None when the platform's own sandbox serves it."""
    ctx = getattr(cfg, "security_context", None)
    return placement.compute_of(getattr(ctx, "placement", None) if ctx is not None else None)


class WarmupController(WarmupSpawn):
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
        for the chat owner or a platform admin.
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

    async def _on_target_check(self, msg: dict, *, stream: "StreamCtx | None" = None):
        """The page asks again whether the chat this socket is on runs where
        the agent's new chats would (a machine came or went): answered with
        the banner's fields of ``warmup_ready``, none when there is no
        mismatch. The request names no chat, so no other one is asked about."""
        cid = self.chat_id or ""
        if not cid:
            return None
        fields = await run_db(self._target_mismatch_fields, cid)
        await self._send({"type": wire.TARGET_MISMATCH, "chat_id": cid, **fields})
        return None

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

        # Already pre-warmed for same agent+model+role+place, still where the
        # new chat would start (its agent's machine may have come back), and
        # alive -> reuse.
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
                and self._pre_warmed_place == place_key
                and await self._prewarm_on_its_place(agent, pw_effective_role,
                                                     self._pre_warmed_exec_path, place_key)):
            # Asked on the layer that holds the pre-warm, never the resolver's.
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
            def _prewarm_path() -> tuple[str, bool, bool]:
                from services.remote import remote_status
                path = resolve_execution_path(agent, requested_exec_path)
                return path, _model_foreign_to_engine(requested_model, path), remote_status.keeps_beside(agent)

            resolved_exec_path, foreign_model, beside = await run_db(_prewarm_path)
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
            if not concurrency.prewarm_allowed(resolved_exec_path, user_sub=self.user_sub, shared_folder=beside):
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
            built = _built_placement(agent_cfg)
            adm = await acquire_chat_slot(new_sid, execution_path=agent_cfg.execution_path,
                                          user_sub=self.user_sub, speculative=True,
                                          mcp_config_path=agent_cfg.mcp_config_path or "",
                                          **({"target": built.machine_id} if built else {}))
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
                **({"placement_built": built} if built else {}),
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
            if isinstance(e, NoSubscriptionError):
                # The warm-up's own refusal, told to the new-chat page before
                # a send: the same builder a send runs, so never a second
                # check of who may chat.
                await self._send({"type": wire.PRE_WARMUP_REFUSED, "agent": agent,
                                  "execution_path": requested_exec_path,
                                  "reason": e.reason, "error": str(e)})
            elif not below_editor:
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
        if carries_prompt(msg) and "_focus_line" not in msg:
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
        # progress, never a second warmup or a second first prompt. The same
        # while the platform holds new work (``core.drain``): the text waits
        # in the chat's queue and no session is started for it.
        if (_rebind_cid and carries_prompt(msg) and not input_queue.deleting(_rebind_cid)
                and (self._chat_in_progress(_rebind_cid) or drain.holding())):
            await self._join_chat_in_progress(_rebind_cid, msg)
            return
        # A new chat's first message cannot wait anywhere: its chat does not
        # exist yet. Refused before any row is written.
        if carries_prompt(msg) and drain.holding():
            await self._refuse_input(drain.refusal(wire.PLATFORM_UPDATING), str(msg.get("queue_id") or ""))
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
        waits_for_compute = False
        try:
            # _do_warmup returns "inline" (session ready
            # synchronously — alive-session reuse / pre-warmed match), "bg" (the
            # slow spawn was backgrounded as _warmup_task), or "skip" (denied /
            # nothing to do). The backgrounded spawn re-applies deferred state +
            # enqueues its own _server_kick from _spawn_tail; inline paths do
            # both here.
            outcome = await self._do_warmup(agent, msg)
        except placement.NoComputeMachine as e:
            # No compute machine can take the chat, known before anything is
            # built: a new chat is refused with the sentence and no row is
            # made (the bare error below resets the page's warmup), an
            # existing chat gets the card under the message it could not send.
            # While the install's own machine restarts, an existing chat's
            # message waits for it instead (below, once this warmup is over).
            # An existing chat is one the warmup put this socket on: an id
            # the person may not open is refused as a new chat is.
            cid = msg.get("chat_id") or ""
            if not cid or self.chat_id != cid:
                raise
            from services.remote import compute_bootstrap
            waits_for_compute = (carries_prompt(msg) and not input_queue.deleting(cid)
                                 and await run_db(compute_bootstrap.chat_waits,
                                                  await run_db(task_store.get_chat, cid)))
            if not waits_for_compute:
                logger.info("WS dashboard warmup refused (no compute machine) chat=%s", cid)
                # The message's row first: the card under it says it was not sent.
                await self._persist_first_prompt(cid, msg)
                # Only the message this warmup carries was not sent. One
                # that waits in the chat's queue goes out at the machine's
                # sign-in, and its chip says so.
                said = NO_COMPUTE_NOT_SENT if carries_prompt(msg) else str(e)
                if drain.holding():
                    # The start still waits for the compute machine, or an
                    # update or a backup stopped it: the hold's own line (a
                    # terminal's start, a banner's button, a voice start).
                    said = drain.refusal(wire.PLATFORM_UPDATING)
                await self._send({"type": wire.WARMUP_FAILED, "chat_id": cid, "error": said,
                                  "reason": wire.REASON_TARGET_UNAVAILABLE})
        finally:
            self._warmup_in_flight = False
            if early and (outcome != "bg" or self.chat_id != early):
                if outcome == "inline" and carries_prompt(msg) and self.chat_id == early:
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

        if waits_for_compute:
            # The message waits in the chat's queue, its chip saying the
            # machine is reconnecting, and goes out at the machine's sign-in
            # (``compute_bootstrap``): the page joins the chat as it does
            # while the platform holds new work.
            logger.info("WS dashboard warmup: chat=%s waits for the compute machine, "
                        "which restarted a moment ago", msg.get("chat_id"))
            await self._join_chat_in_progress(msg["chat_id"], msg,
                                              waiting=input_queue.WAITING_RECONNECT)
            return

        # Auto-warmup callers (from _handle_chat on a dead/missing session) need
        # the session synchronously to continue the same turn — await the
        # backgrounded spawn so session_id is set before we return. The spawn
        # carried no text, so it enqueues no kick.
        if not background and outcome == "bg" and self._warmup_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._warmup_task

        # Server-owned first turn for the INLINE-ready paths: the session is
        # live, so kick the turn; its row was written at send-time
        # (_persist_first_prompt). A backgrounded spawn enqueues its own kick.
        if (outcome == "inline" and not carries_prompt(msg) and self.chat_id
                and input_queue.peek(self.chat_id)):
            input_queue.arm(self.chat_id)
        if outcome == "inline" and carries_prompt(msg) and self.session_id and self.chat_id:
            holder = grace_layer(self.session_id, self.layer) if self.layer else None
            await self._handle_chat({
                "text": msg.get("text", ""),
                "images": msg.get("images", []),
                "files": msg.get("files", []),
                "chat_id": self.chat_id,
                "_server_kick": True,
                "_focus_line": msg.get("_focus_line", ""),
                "_photos_dropped": msg.get("_photos_dropped", 0),
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

    async def _join_chat_in_progress(self, cid: str, msg: dict, *, waiting: str = "") -> None:
        """A cold send on a chat in progress: the connection joins the chat
        the way a resume does (the access check, the in-flight warmup's
        listener and history replay or the live pump's promised attach, or
        ``warmup_ready`` with the live session), becomes the session's
        notification sink as a resume does, then queues the text behind the
        turn in progress as a ``chat`` frame would, attaches to a pump that
        drives the chat, and arms the background monitors after it. A resume
        that refused, or a view-only connection, ends the page's warmup with
        a card and queues nothing. ``waiting`` says on the chip why the text
        waits when no turn is in progress."""
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
        await self._queue_for_next_turn(cid, item, msg.get("queue_id"), waiting=waiting)
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
            path = resolve_execution_path(agent, requested_exec_path)
            # Before the config exists: the layer the builders would build now.
            # None when no compute machine can take a session of the
            # platform's place: an existing chat is still opened (its own
            # layer is read from its row, and a machine that reconnects may
            # hold its session), a new chat is refused before its row.
            # An engine that runs in the proxy (Direct LLM) has a layer either
            # way, and its tools still need the machine: ``no_machine`` says
            # that nothing can take a new session of the platform's place.
            from core.session.session_manager import compute_placement_for
            built = compute_placement_for(path, agent=agent)
            try:
                layer = get_execution_layer(
                    agent, execution_path=requested_exec_path, user_sub=self.user_sub, role=role,
                    **({"placement_built": built} if built else {}),
                )
            except placement.NoComputeMachine:
                return role, None, path, True
            in_proxy = not layer.capabilities.runtime.supports_remote_execution
            return role, layer, path, in_proxy and built is None

        warmup_role, self.layer, effective_exec_path, no_machine = await run_db(_warm_role_layer)
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
        if no_machine and not self.chat_id:
            # A new chat (or a chat id this person may not open) with no
            # compute machine: refused before anything is minted.
            raise placement.NoComputeMachine()
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
                # A chat the socket was not on: it lets go of the one it was
                # on, and holds no session until this chat's own is adopted
                # (a start that fails or is stopped adopts none).
                if p.cid != self.chat_id:
                    self._detach_pty_viewer()
                    self.session_id = None
                self.chat_id = p.cid
                # Nothing holds the chat's session and no compute machine can
                # start one: known before its message is written. The
                # warmup's caller keeps the message waiting while the
                # machine restarts, else shows the refusal under it.
                from core.session.session_manager import no_compute_for
                if await run_db(no_compute_for, p.chat):
                    raise placement.NoComputeMachine()
                # Persist the prompt at send-time for an existing-chat cold send
                # (resumed dead session): the frontend warmup carries the text
                # when its session is cold, and the server-kick skips re-persist.
                await self._persist_first_prompt(self.chat_id, p.msg)
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
                # The platform's place reads through ``layer_for_chat``: the
                # holder of a session a compute machine keeps, else the
                # machine the chat's engine record lives on.
                chat_pin = p.chat.get("execution_target") or ""
                if p.chat.get("execution_path") or chat_pin or self.layer is None:
                    self.layer = await run_db(functools.partial(
                        layer_for_chat, p.agent, {**p.chat, "execution_path": p.effective_exec_path},
                        user_sub=self.user_sub, role=p.warmup_role,
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
            self.session_id = None   # a new chat's session is the one its start adopts
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
            await self._persist_first_prompt(self.chat_id, p.msg)
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
            "execution_target": (p.chat or {}).get("execution_target") or p.placed[0],
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
            elif isinstance(e, placement.NoComputeMachine):
                logger.info("WS dashboard warmup refused (no compute machine) chat=%s", w.wcid)
            elif isinstance(e, placement.PlacementRefused):
                # The place cannot take it now for a known reason (its
                # machine is in its update, the guard): the card says why.
                logger.info("WS dashboard warmup refused chat=%s: %s", w.wcid, e)
            elif not machine_dropped_off(e):
                # A machine that dropped off is logged by the card's own line.
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
            if run.res is not None and not run.res.interactive and carries_prompt(w.msg) and not run.aborted:
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
