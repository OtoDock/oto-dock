"""Session warmup + spawn: pre-warm, warmup (inline reuse or backgrounded spawn
tail), deferred mode/model re-apply, and the single create/resume funnel every
spawn path goes through.

WarmupController is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
Behavior is pinned by tests/session/test_ws_dashboard_*.
"""

import asyncio
import contextlib
import logging
import time
import uuid
import config
from storage import database as task_store, agent_store, remote_store
from core.session.session_state import (
    set_session_mode,
    get_session_mode,
    get_session_user_tz,
    get_user_tz,
    set_session_user_tz,
    clear_session_liveness,
)
from core.execution_layer import ExecutionLayer
from core.session.session_manager import get_execution_layer, resolve_execution_path
from core.config.config_builder import (
    build_agent_config,
    is_hard_fail_target,
    extract_offline_machine,
)
from services.engines.subscription_pool import NoSubscriptionError
from core.session.history_seed import consume_pending_seed_digest
from core.config.task_config_builder import resolve_task_identity
from core.session import warmup_registry, visibility as _vis
from core import execution_mode
from core.session import interactive_session
# Imported by ws/dashboard.py AFTER its helpers are defined —
# safe intra-unit circularity (see the class assembly there).
from ws.dashboard import (
    _SpawnResult,
    _effective_agent_role,
    _model_allowed_for_path,
    _resolve_session_interactive,
    _resume_username_for_chat,
    _rewarm_chat_allowed,
)

logger = logging.getLogger("claude-proxy")


class WarmupController:
    """Session warmup + spawn: pre-warm, warmup (inline reuse or backgrounded
    spawn tail), deferred mode/model re-apply, and the single create/resume
    funnel every spawn path goes through."""

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
            role = _effective_agent_role(self.user_sub, agent)
            if chat.get("user_sub") != self.user_sub and role != "admin":
                return {}
            resolved, _ = remote_store.resolve_execution_target(
                agent, self.user_sub, role,
            )
            if not resolved or resolved == pin or resolved.startswith("__offline__:"):
                return {}

            def _label(target: str) -> str:
                if target == "local":
                    return "local sandbox"
                m = remote_store.get_remote_machine(target) or {}
                return str(m.get("name") or "") or target[:8]

            return {
                "pinned_target": pin,
                "pinned_label": _label(pin),
                "resolved_target": resolved,
                "resolved_label": _label(resolved),
            }
        except Exception:
            logger.debug("target-mismatch fields failed for %s", chat_id,
                         exc_info=True)
            return {}

    async def _handle_pre_warmup(self, msg: dict):
        """Pre-warm a session for a new chat (MCP init in background).

        Creates a session via the execution layer but does NOT create a DB
        chat.  The session is stored in _pre_warmed_sid and reused by
        _handle_warmup when the user sends their first message.
        """

        agent = msg.get("agent", "")
        if not agent:
            await self._send_error("Agent name required")
            return
        if not self._can_access_agent(agent):
            await self._send_error(f"Access denied for agent '{agent}'")
            return
        if not agent_store.agent_exists(agent):
            await self._send_error(f"Agent '{agent}' no longer exists")
            return

        permission_mode = msg.get("permission_mode", "default")
        requested_model = msg.get("model", "")
        requested_exec_path = msg.get("execution_path", "")

        # Resolve current per-agent role — needed for both the match check
        # below and the new-pre-warm path (so the spawned session's
        # SecurityContext gets the per-agent role, not the platform role).
        # Role is part of the match key so per-agent role
        # reassignments (e.g. user demoted from editor to viewer) invalidate
        # the pre-warm and force a fresh session with the right role.
        pw_effective_role = _effective_agent_role(self.user_sub, agent, fallback_user=self.user)

        # Already pre-warmed for same agent+model+role and alive -> reuse.
        # Model is part of the match key because CLI sessions are spawned with
        # --model <name> and we can't swap models on a running process.
        # Role is part because the session's SecurityContext is baked at start.
        target_model = requested_model or config.get_cli_model(agent)
        if (self._pre_warmed_sid and self._pre_warmed_agent == agent
                and self._pre_warmed_model == target_model
                and self._pre_warmed_role == pw_effective_role):
            pw_layer = get_execution_layer(agent, user_sub=self.user_sub, role=pw_effective_role)
            if await pw_layer.is_session_alive(self._pre_warmed_sid):
                logger.info(
                    f"WS dashboard pre_warmup: reusing existing "
                    f"session={self._pre_warmed_sid[:8]}, agent={agent}, model={self._pre_warmed_model}, role={self._pre_warmed_role}"
                )
                await self._send({"type": "pre_warmup_ready", "session_id": self._pre_warmed_sid})
                return

        # Pre-warmed for different agent/model/role (or dead) -> close old
        if self._pre_warmed_sid:
            old_sid = self._pre_warmed_sid
            self._pre_warmed_sid = None
            old_agent = self._pre_warmed_agent
            self._pre_warmed_agent = None
            self._pre_warmed_exec_path = ""
            self._pre_warmed_model = ""
            old_role = self._pre_warmed_role or "manager"
            self._pre_warmed_role = ""
            try:
                old_layer = get_execution_layer(old_agent, user_sub=self.user_sub, role=old_role) if old_agent else None
                if old_layer:
                    await old_layer.close_session(old_sid)
            except Exception:
                pass
            from core.concurrency import release_chat_slot
            release_chat_slot(old_sid)
            from core.session import prewarm_session_registry as _prewarm
            await _prewarm.discard(old_sid)

        try:
            new_sid = str(uuid.uuid4())

            # Build the config FIRST so the resolved execution target + the
            # interactive decision are known BEFORE we acquire a slot or spawn.
            agent_cfg = await build_agent_config(
                agent_name=agent, user=self.user, user_sub=self.user_sub,
                user_role=pw_effective_role, permission_mode=permission_mode,
                client_type="dashboard", resume=False,
                model=requested_model,
                execution_path=resolve_execution_path(agent, requested_exec_path),
                session_id=new_sid,
            )
            # Skip pre-warm for a REMOTE target: it would spawn a real session on
            # the satellite (counting against THAT satellite's budget) for a chat
            # the user may never send. The first real send warms it on demand.
            if (agent_cfg.execution_target or "local") != "local":
                logger.info(f"WS dashboard pre_warmup: skipped (remote target) agent={agent}")
                return
            # Interactive agents are NOT eagerly pre-warmed: the interactive
            # cold-start spawns a FRESH session (`_spawn_tail` skips reusing a
            # pre-warm when interactive), so a -p pre-warm here is a throwaway
            # session that ALSO runs a redundant background MCP install. Skip it;
            # the first send warms interactively and waits for the install.
            if _resolve_session_interactive(agent_cfg):
                logger.info(f"WS dashboard pre_warmup: skipped (interactive) agent={agent}")
                return
            # Local target → acquire a unit of the local ceiling G before spawning.
            from core.concurrency import acquire_chat_slot
            adm = await acquire_chat_slot(new_sid, execution_path=agent_cfg.execution_path,
                                          user_sub=self.user_sub)
            if not adm:
                await self._send_error(adm.user_message)
                return
            # pw_effective_role (resolved at the top) → the spawned session's
            # SecurityContext gets the per-agent role. Install progress is
            # delivered out-of-band via the per-user broadcaster (install_registry
            # → push_install_event), so a detached pre-warm still reaches the
            # user's dashboard tabs.
            self.layer = get_execution_layer(
                agent, execution_path=requested_exec_path, user_sub=self.user_sub,
                role=pw_effective_role,
            )
            await self.layer.start_session(new_sid, agent_cfg)

            # Propagate the user's browser-detected TZ onto the pre-warmed
            # session (client_info fires on WS open, before pre_warmup).
            _user_default_tz = get_user_tz(self.user_sub)
            if _user_default_tz:
                set_session_user_tz(new_sid, _user_default_tz)

            self._pre_warmed_sid = new_sid
            self._pre_warmed_agent = agent
            self._pre_warmed_exec_path = resolve_execution_path(agent, requested_exec_path)
            self._pre_warmed_model = agent_cfg.model
            self._pre_warmed_role = pw_effective_role
            # Track as a reapable pre-warm: if the user never sends, the fast TTL
            # reaper frees its slot + subscription (vs. holding the full idle window).
            from core.session import prewarm_session_registry as _prewarm
            await _prewarm.register(new_sid, agent=agent, user_sub=self.user_sub,
                                    role=pw_effective_role, exec_path=self._pre_warmed_exec_path)

            await self._send({"type": "pre_warmup_ready", "session_id": new_sid})
            logger.info(
                f"WS dashboard pre_warmup: created session={new_sid[:8]}, "
                f"agent={agent}, exec_path={self._pre_warmed_exec_path}, model={agent_cfg.model}"
            )
        except Exception as e:
            logger.error(f"WS dashboard pre_warmup failed: {e}", exc_info=True)
            from core.concurrency import release_chat_slot
            release_chat_slot(new_sid)
            self._pre_warmed_sid = None
            self._pre_warmed_agent = None
            self._pre_warmed_exec_path = ""
            self._pre_warmed_model = ""
            self._pre_warmed_role = ""
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
        if not agent_store.agent_exists(agent):
            await self._send_error(f"Agent '{agent}' no longer exists")
            return

        # For an EXISTING chat the chat row is the agent of record — the frame's
        # agent can be stale (observed 2026-07-09: a post-restart redirect opened
        # agent A's chat under agent B's URL slug, and the /chat/:agent/:chatId
        # route trusts the slug). Re-warming under the frame's agent would spawn
        # a B session and overwrite A's session binding on the chat row, so
        # rebind to the chat's stored agent and re-run the SAME access gates
        # against it, fail-closed. New chats (no row yet) keep the frame agent.
        _rebind_cid = msg.get("chat_id")
        if _rebind_cid:
            _chat_agent = (task_store.get_chat(_rebind_cid) or {}).get("agent") or ""
            if _chat_agent and _chat_agent != agent:
                if not self._can_access_agent(_chat_agent):
                    await self._send_error(f"Access denied for agent '{_chat_agent}'")
                    return
                if not agent_store.agent_exists(_chat_agent):
                    await self._send_error(f"Agent '{_chat_agent}' no longer exists")
                    return
                logger.warning(
                    f"WS dashboard warmup: agent mismatch for chat={_rebind_cid} — "
                    f"frame='{agent}', chat row='{_chat_agent}'; using the chat's agent"
                )
                agent = _chat_agent

        # The eager pre_warmup (if still in flight) is NOT awaited here anymore — a
        # slow remote pre-warm would block warmup_started (the chat row + sidebar +
        # URL) for its whole duration. The await + the pre-warmed-reuse-vs-fresh
        # decision moved INTO the backgrounded spawn (_do_warmup → _spawn_tail), so
        # the new chat appears in <1s and the reuse still happens.

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
            if outcome == "inline":
                # Post-warmup re-apply of deferred mode/model: a mode/model
                # change that raced the synchronous warmup is applied to the new
                # session + echoed to the client so the UI stays in sync.
                await self._reapply_deferred_after_warmup()

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
        if outcome == "inline" and msg.get("text") and self.session_id and self.chat_id:
            await self._handle_chat({
                "text": msg.get("text", ""),
                "images": msg.get("images", []),
                "files": msg.get("files", []),
                "chat_id": self.chat_id,
                "_server_kick": True,
            })

    async def _do_warmup(self, agent: str, msg: dict) -> str:
        self.agent_name = agent
        # Fresh warmup intent — clear any stale abort flag from a prior spawn.
        self._warmup_abort_chat = None
        # Resolve execution layer — use frontend override if provided, else agent default
        requested_exec_path = msg.get("execution_path", "")
        requested_model = msg.get("model", "")
        logger.info(f"WS dashboard warmup: agent={agent}, requested_exec_path='{requested_exec_path}', model='{requested_model}'")
        warmup_role = _effective_agent_role(self.user_sub, agent, fallback_user=self.user)
        self.layer = get_execution_layer(agent, execution_path=requested_exec_path, user_sub=self.user_sub, role=warmup_role)
        effective_exec_path = resolve_execution_path(agent, requested_exec_path)  # actual path for storage (never "remote")
        permission_mode = msg.get("permission_mode", "default")
        requested_model = msg.get("model", "")  # frontend sends current selection
        # Per-chat interactive override — the frontend's
        # interactive toggle. '' = unset (use stored/resolver default).
        requested_exec_mode = msg.get("execution_mode", "")
        # Dashboard light/dark mode → seeds Claude's TUI theme (interactive only).
        requested_theme = msg.get("theme", "")
        cid = msg.get("chat_id")
        # Task continue-gate: reject re-warm of a task this user can't continue
        # (agent-scoped → editor+; user-scoped → creator/admin) before spawning.
        if await self._deny_task_continue(cid):
            return "skip"
        old_session_id: str | None = None
        chat: dict | None = None  # set below for an existing chat; None for new

        chat_model = ""  # will be loaded from DB or left empty for new chats
        chat_exec_mode = ""  # per-chat interactive override (loaded/persisted below)
        # New-chat-only locals the backgrounded tail reuses for the pre-warm match;
        # defaulted so the existing-chat path (which skips the block below) still
        # leaves them defined for the snapshot.
        target_model = ""
        consume_effective_role = ""
        pw_task = self._pre_warmup_task  # snapshot the in-flight eager pre-warm task

        if cid:
            # Reuse existing chat
            chat = task_store.get_chat(cid)
            if chat and _rewarm_chat_allowed(
                    chat, cid, self.user_sub, self.user_agents):
                self.chat_id = cid
                # Persist the prompt at send-time for an existing-chat cold send
                # (resumed dead session): the frontend warmup carries the text
                # when its session is cold, and the server-kick skips re-persist.
                self._persist_first_prompt(self.chat_id, msg.get("text", ""))
                old_session_id = chat.get("session_id")
                permission_mode = chat.get("permission_mode", permission_mode)
                chat_model = chat.get("model", "")
                # Per-chat execution mode: a fresh toggle in this warmup wins and
                # is persisted; else the chat's stored mode (so resume re-spawns
                # in the same mode).
                chat_exec_mode = requested_exec_mode or chat.get("execution_mode", "") or ""
                if requested_exec_mode and requested_exec_mode != (chat.get("execution_mode") or ""):
                    task_store.update_chat(cid, execution_mode=requested_exec_mode)
                # Use chat's stored execution_path if available
                if chat.get("execution_path"):
                    effective_exec_path = chat["execution_path"]
                # Resume affinity: the alive-check and the resume gate below
                # must consult the layer of the chat's PINNED target, never the
                # agent's current default — an agent retargeted between
                # sessions would otherwise ask the wrong machine about the old
                # session (a live local session reads dead, an on-disk resume
                # is refused) and the chat loses its context to a fresh spawn.
                chat_pin = chat.get("execution_target") or ""
                if chat.get("execution_path") or chat_pin:
                    self.layer = get_execution_layer(
                        agent, execution_path=effective_exec_path,
                        user_sub=self.user_sub, role=warmup_role,
                        execution_target=chat_pin,
                    )

                # If the process is still alive, reuse directly
                if old_session_id:
                    # Live INTERACTIVE session (PTY) — re-attach the viewer rather
                    # than re-spawn. The headless is_session_alive check below
                    # never sees PTY sessions (they live in interactive_session,
                    # not the layer's registry), so this must come first.
                    isess = interactive_session.get(old_session_id)
                    if isess is not None and isess.alive:
                        from core.concurrency import acquire_chat_slot
                        # Re-acquire with the live session's target so a REMOTE
                        # interactive re-attach never takes a local-G slot.
                        # A LIVE session is already tracked, so this is the
                        # idempotent path today — but check the Admission
                        # anyway: silently attaching an unadmitted session is
                        # the failure mode a future reordering would hit.
                        adm = await acquire_chat_slot(old_session_id, target=isess.target)
                        if not adm:
                            await self._send_error(adm.user_message)
                            return "inline"
                        self.session_id = old_session_id
                        await self._send({
                            "type": "warmup_ready",
                            "session_id": self.session_id,
                            "chat_id": self.chat_id,
                            "mode": get_session_mode(old_session_id),
                            "model": chat_model or config.get_cli_model(agent),
                            "execution_path": effective_exec_path,
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
                    if self.layer and await self.layer.is_session_alive(old_session_id):
                        # Re-acquire concurrency slot (released on WS disconnect),
                        # using the chat's pinned target so a REMOTE session is a
                        # no-op (and a local-full proxy doesn't reject reconnecting
                        # to a remote chat).
                        from core.concurrency import acquire_chat_slot
                        adm = await acquire_chat_slot(old_session_id, target=chat.get("execution_target") or "local", execution_path=chat.get("execution_path"))
                        if not adm:
                            await self._send_error(adm.user_message)
                            return "skip"
                        self.session_id = old_session_id
                        await self._send({
                            "type": "warmup_ready",
                            "session_id": self.session_id,
                            "chat_id": self.chat_id,
                            "mode": get_session_mode(old_session_id),
                            "model": chat_model or config.get_cli_model(agent),
                            "execution_path": effective_exec_path,
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

        if not self.chat_id:
            # Resolve the model + role that form the pre-warm match key (the tail
            # checks them after awaiting the eager pre-warm). Model precedence:
            # deferred (user changed during warmup) > requested (sent with warmup
            # msg) > agent default — it must match the session's spawned model
            # because CLI processes bake --model at start and can't swap live.
            target_model = self.deferred_model or requested_model or config.get_cli_model(agent)
            # A chat-switch race can leave the OTHER chat's model in the
            # selector when a new chat spawns on a different engine — a
            # foreign model would bake into the chat row and 400 every turn
            # (see _model_allowed_for_path). Fall back to the layer's first
            # model (the agent default may belong to the agent's PRIMARY
            # layer, which is exactly what a cross-layer chat isn't on).
            if not _model_allowed_for_path(target_model, effective_exec_path):
                fallback_model = next(
                    (m["value"] for m in config.get_layer_models(effective_exec_path) if m.get("value")),
                    "",
                )
                logger.warning(
                    f"WS dashboard warmup: model={target_model} is not a "
                    f"{effective_exec_path} model — using {fallback_model or 'the layer default'}"
                )
                target_model = fallback_model
            # Per-agent effective role at consume time: if the user's role changed
            # since the pre-warm spawned (e.g. manager→editor via admin UI/SQL), the
            # pre-warm's SecurityContext is stale → the tail's match fails and it
            # spawns a fresh session with the right role baked into its mounts/policy.
            consume_effective_role = _effective_agent_role(self.user_sub, agent, fallback_user=self.user)

            # The pre-warmed-reuse-vs-fresh decision moved into _spawn_tail: it must
            # first await the eager pre-warm (no longer done on the WS loop), so the
            # chat below is allocated + announced immediately and the reuse happens in
            # the background.
            self.chat_id = str(uuid.uuid4())
            chat_model = target_model
            chat_exec_mode = requested_exec_mode
            self.deferred_model = ""  # consumed
            if self.deferred_mode:
                permission_mode = self.deferred_mode
                self.deferred_mode = ""
            task_store.create_chat(self.chat_id, _vis.chat_history_owner(self.agent_name, self.user_sub), self.agent_name, permission_mode, model=chat_model, execution_path=effective_exec_path, execution_mode=chat_exec_mode)
            self._persist_first_prompt(self.chat_id, msg.get("text", ""))

        # For existing chats with no model stored, fill from agent default
        if not chat_model:
            chat_model = config.get_cli_model(agent)

        # Register an in-flight warmup so a reconnecting WS can re-attach via
        # resume_chat and the eventual warmup_ready reaches whichever socket
        # is currently attached. Also emit warmup_started right away so the
        # dashboard learns the chat_id (load-bearing for reconnect recovery).
        # This runs INLINE (fast) so the frontend navigates immediately; the
        # slow session spawn is backgrounded below so the WS loop stays free
        # to handle chat-switch / abort during the spawn.
        await warmup_registry.register(self.chat_id, self.user_sub, self.agent_name)
        await warmup_registry.attach_listener(self.chat_id, self._send)
        self._attached_warmups.add(self.chat_id)
        await warmup_registry.emit(self.chat_id, {
            "type": "warmup_started",
            "chat_id": self.chat_id,
            "agent": self.agent_name,
            "execution_path": effective_exec_path,
            "execution_target": self.session_execution_target,
        })

        # Snapshot everything the backgrounded spawn needs as LOCALS — the
        # connection's chat_id/agent_name/layer attributes may change underneath
        # the spawn if the user switches chats during the (slow) start_session.
        wcid = self.chat_id
        w_agent = self.agent_name
        w_layer = self.layer
        w_pinned = (chat.get("execution_target") if (old_session_id and chat) else "") or ""
        w_thread = (chat.get("codex_thread_id", "") if (old_session_id and chat) else "")
        # New-chat pre-warm match key (snapshotted — the attributes may change if the user
        # switches chats during the spawn). Empty/"" for existing-chat warmups.
        w_role = consume_effective_role
        w_target_model = target_model
        w_exec_mode = chat_exec_mode  # per-chat interactive override (snapshot)
        w_theme = requested_theme  # dashboard light/dark (snapshot)

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
                                "type": "warmup_heartbeat",
                                "chat_id": wcid,
                            })
            except Exception:
                logger.exception("warmup heartbeat loop crashed")

        hb_task = asyncio.create_task(_heartbeat_loop())

        async def _spawn_tail() -> None:
            """Backgrounded session spawn. Runs the slow
            start_session OFF the WS loop so the connection stays responsive
            (chat-switch / abort during the spawn). Persists to the DB +
            warmup_registry headless, adopts the connection's VIEWED attributes
            ONLY if the user is still on the warmed chat (else it would clobber
            the chat they switched to), then enqueues a _server_kick
            so the MAIN loop drives the first turn — the tail never enters the
            pump loop itself, preserving the single-socket-reader invariant."""
            res: _SpawnResult | None = None
            still_viewing = False
            # An interactive chat must not adopt a headless pre-warmed session
            # (wrong spawn mode), so skip the pre-warm reuse path when interactive
            # is resolved-on. Layer/target gating happens in
            # _create_or_resume_session (falls back to -p if unsupported).
            wants_interactive = execution_mode.is_interactive(chat_override=w_exec_mode or None)
            # DB-fallback resume: a chat whose pinned machine was removed, or
            # whose on-disk session files were aged out by retention, lost its CLI
            # context (chats.pending_history_seed set + session_id cleared → a FRESH
            # spawn). For an INTERACTIVE chat, restore the conversation from the DB:
            # claim the digest here and STASH it on the session below (set_pending_seed
            # → prepended as a bracketed paste to the first prompt). Gate on
            # wants_interactive only — the prompt does NOT ride msg.text for Claude
            # (the frontend sends it as a pty_input paste after warmup_ready), so the
            # stash is delivery-path-independent. The -p path reseeds in
            # _start_new_stream; a -p FALLBACK (satellite without PTY) prepends the
            # digest to its kick text below. consume_pending_seed_digest no-ops
            # (returns '','') when nothing is pending.
            seed_digest = ""
            seed_notice = ""
            if wants_interactive:
                # Smaller cap than the -p path (48k): the digest is bracketed-pasted
                # into the live TUI, so keep it light to render fast (well under the
                # Enter backstop) — build_history_seed is newest-biased, so this
                # keeps the most recent context.
                seed_digest, seed_notice = consume_pending_seed_digest(wcid, max_chars=16_000)
            # A reseeded prompt's digest is multi-line → keep it off Codex's launch
            # argv (Windows cmdline limit + structure loss); it's pasted via the PTY.
            argv_first_prompt = "" if seed_digest else msg.get("text", "")
            try:
                if old_session_id:
                    # A one-shot delivery (--resume echo to this dead chat) may
                    # still be writing the same JSONL: resuming NOW renders the
                    # transcript as-of-open (the delivered turn stays invisible
                    # until the next toggle) and dual-writes the file. Await it;
                    # the heartbeat loop keeps the user informed meanwhile. Past
                    # the cap, proceed — degrades to the pre-guard behavior.
                    from core.session import session_delivery
                    inflight = session_delivery.oneshot_inflight(wcid)
                    if inflight is not None:
                        logger.info(
                            f"WS dashboard warmup: awaiting in-flight one-shot "
                            f"delivery on chat={wcid} before resume"
                        )
                        try:
                            await asyncio.wait_for(inflight.wait(), timeout=120)
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"WS dashboard warmup: one-shot still running after "
                                f"120s on chat={wcid} — resuming anyway"
                            )
                    # Does the CLI session file still have conversation data on
                    # disk? If the proxy restarted / the session was reaped the
                    # file may be gone — create a fresh session in that case.
                    can_resume = w_layer and await w_layer.can_resume_session(
                        old_session_id, agent_name=w_agent,
                        username=_resume_username_for_chat(
                            wcid, w_agent, self.user.get("username", ""),
                        ),
                    )
                    if can_resume:
                        res = await self._create_or_resume_session(
                            old_session_id, w_agent, permission_mode, resume=True,
                            model=chat_model, exec_path=effective_exec_path,
                            codex_thread_id=w_thread, chat_id=wcid,
                            pinned_target=w_pinned, adopt=False,
                            chat_exec_mode=w_exec_mode, chat_theme=w_theme,
                        )
                        logger.info(
                            f"WS dashboard warmup (resume): session={old_session_id}, "
                            f"chat={wcid}, agent={w_agent}, model={chat_model}"
                        )
                    else:
                        new_sid = str(uuid.uuid4())
                        res = await self._create_or_resume_session(
                            new_sid, w_agent, permission_mode, resume=False,
                            model=chat_model, exec_path=effective_exec_path,
                            codex_thread_id=w_thread, chat_id=wcid,
                            pinned_target=w_pinned, adopt=False,
                            chat_exec_mode=w_exec_mode, chat_theme=w_theme,
                            first_prompt=argv_first_prompt,
                        )
                        # The resume gate legitimately refused (missing JSONL,
                        # RPC timeout, satellite mid-reconnect) — this fresh
                        # session starts with no context, so flag the DB-digest
                        # reseed; and the dead session's background work died
                        # with it, so clear any stuck liveness badges.
                        task_store.update_chat(
                            wcid, session_id=new_sid,
                            pending_history_seed="resume_failed",
                        )
                        clear_session_liveness(
                            old_session_id, reason="resume_failed",
                        )
                        if wants_interactive and not seed_digest:
                            # The interactive digest claim above ran BEFORE this
                            # branch decided fresh — claim again so the digest
                            # still rides the PTY paste (the -p path claims at
                            # the _start_new_stream chokepoint).
                            seed_digest, seed_notice = consume_pending_seed_digest(
                                wcid, max_chars=16_000,
                            )
                        logger.info(
                            f"WS dashboard warmup (fresh, old session gone): session={new_sid}, "
                            f"chat={wcid}, agent={w_agent}, model={chat_model}"
                        )
                else:
                    # NEW CHAT. Await the in-flight eager pre-warm HERE (off the WS
                    # loop, so warmup_started already fired), then reuse it if it
                    # still matches this chat's agent/path/model/role + is alive, else
                    # spawn fresh. We never CLOSE a mismatched pre-warm from the tail
                    # (a concurrent new-chat pre-warm may own _pre_warmed_sid by now);
                    # the next pre_warm/warmup or the idle reaper reclaims a stale one.
                    if pw_task is not None and not pw_task.done():
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await pw_task
                    pw_match = (
                        self._pre_warmed_sid is not None
                        and self._pre_warmed_agent == w_agent
                        and self._pre_warmed_exec_path == effective_exec_path
                        and self._pre_warmed_model == w_target_model
                        and self._pre_warmed_role == w_role
                    )
                    # Claim the pre-warm out of the TTL reaper's reach as the FINAL
                    # gate — if the reaper already took it (claim False), it's being
                    # closed, so fall through to a fresh spawn.
                    from core.session import prewarm_session_registry as _prewarm
                    pw_reuse_ok = (
                        pw_match and not wants_interactive and w_layer
                        and await w_layer.is_session_alive(self._pre_warmed_sid)
                        and await _prewarm.claim(self._pre_warmed_sid)
                    )
                    if pw_reuse_ok:
                        reuse_sid = self._pre_warmed_sid
                        self._pre_warmed_sid = None
                        self._pre_warmed_agent = None
                        self._pre_warmed_exec_path = ""
                        self._pre_warmed_model = ""
                        self._pre_warmed_role = ""
                        # Fresh-resolve the target for the badge + pin (new
                        # chat → no existing pin); fall back to local on any error.
                        try:
                            reuse_target, _ = remote_store.resolve_execution_target(
                                w_agent, self.user_sub, w_role,
                            )
                        except Exception:
                            reuse_target = "local"
                        reuse_target = reuse_target or "local"
                        res = _SpawnResult(
                            session_id=reuse_sid, layer=w_layer,
                            execution_target=reuse_target, fallback_reason=None,
                        )
                        task_store.update_chat(
                            wcid, session_id=reuse_sid, execution_target=reuse_target,
                        )
                        logger.info(
                            f"WS dashboard warmup (pre-warmed reuse): session={reuse_sid[:8]}, "
                            f"chat={wcid}, agent={w_agent}, model={chat_model}"
                        )
                    else:
                        new_sid = str(uuid.uuid4())
                        res = await self._create_or_resume_session(
                            new_sid, w_agent, permission_mode, resume=False,
                            model=chat_model, exec_path=effective_exec_path,
                            chat_id=wcid, adopt=False,
                            chat_exec_mode=w_exec_mode, chat_theme=w_theme,
                            first_prompt=argv_first_prompt,
                        )
                        task_store.update_chat(wcid, session_id=new_sid)
                        logger.info(
                            f"WS dashboard warmup (new): session={new_sid}, "
                            f"chat={wcid}, agent={w_agent}, model={chat_model}"
                        )

                # Abort-during-spawn: the user hit Stop while this chat was
                # warming. Cancelling the spawn task can't reliably stop a
                # half-started CLI/satellite process, so we let the spawn finish
                # then tear it down here: kill the just-spawned session (kept
                # resumable) and suppress BOTH warmup_ready and the first-turn
                # kick — the client already received `aborted`. The `finally`
                # below still unregisters the warmup.
                if self._warmup_abort_chat == wcid and res is not None:
                    self._warmup_abort_chat = None
                    try:
                        await res.layer.abort(res.session_id)
                    except Exception:
                        logger.warning(f"abort-during-spawn teardown failed for chat={wcid}")
                    # Deliberately NOT stamping last_abort_graceful: this abort
                    # loses no turn (the first-turn kick was suppressed), so the
                    # flag-pair keeps describing the PREVIOUS abort — a graceful
                    # one kept its partial turn in the CLI history and must
                    # still skip the cancelled-context injection.
                    task_store.update_chat(wcid, last_turn_aborted=True)
                    return

                # stash the restored-history digest on the interactive session
                # BEFORE its readiness flush (this runs synchronously, ahead of the
                # ≥0.8s settle), so the first prompt is prepended with the prior
                # conversation (set_pending_seed → _emit_to_pty bracketed paste). A
                # -p FALLBACK (not res.interactive) prepends to its kick text below.
                if res.interactive and seed_digest:
                    isess = interactive_session.get(res.session_id)
                    if isess is not None:
                        isess.set_pending_seed(seed_digest)

                # Adopt into the connection's VIEWED state ONLY if the socket is
                # still on the warmed chat. A chat-switch during the spawn means
                # the attributes now describe a DIFFERENT chat — adopting would
                # clobber it. The DB + registry already hold the truth either way.
                still_viewing = (self.chat_id == wcid)
                if still_viewing:
                    prev_sid = self.session_id
                    self.session_id = res.session_id
                    self.layer = res.layer
                    self.session_execution_target = res.execution_target
                    self.session_fallback_reason = res.fallback_reason
                    # Register the dashboard notify queue under the NOW-adopted
                    # session id. The dispatch's `_register_notify_queue()` ran
                    # BEFORE this backgrounded spawn adopted `session_id`, so a
                    # freshly-spawned session (an interactive session, or a `-p`
                    # rewarm after a mode switch) would otherwise never land in
                    # `_dashboard_notify_queues` — which breaks location-mcp's
                    # "find the active dashboard session" lookup (api/hooks/hooks.py
                    # request_user_location → "No active dashboard session").
                    # Same chat, fresh sid — the replaced sid's entry goes too.
                    self._register_notify_queue(replaces=prev_sid)

                # Reconcile chat row + warmup_ready mode with the spawned
                # session's actual mode (a mode_change may have raced the spawn).
                eff_mode = get_session_mode(res.session_id) or permission_mode
                if eff_mode != permission_mode:
                    task_store.update_chat(wcid, permission_mode=eff_mode)

                # When a user per-agent override is offline and we soft-fell-back,
                # surface the offline machine's name for the "machine X offline —
                # using default" banner.
                offline_machine_name = ""
                if res.fallback_reason == "user-override-offline":
                    ut = remote_store.get_user_remote_target(self.user_sub, w_agent)
                    if ut:
                        om = remote_store.get_remote_machine(ut["machine_id"]) or {}
                        offline_machine_name = str(om.get("name") or "")
                await warmup_registry.emit(wcid, {
                    "type": "warmup_ready",
                    "session_id": res.session_id,
                    "chat_id": wcid,
                    "mode": eff_mode,
                    "model": chat_model,
                    "execution_path": effective_exec_path,
                    "execution_target": res.execution_target,
                    "fallback_reason": res.fallback_reason,
                    "offline_machine_name": offline_machine_name,
                    "interactive": res.interactive,
                    **self._target_mismatch_fields(wcid),
                })
                # surface the "restored from history" card live (also persisted
                # to chat_messages by the digest claim, so it renders on reload).
                if seed_notice and still_viewing:
                    await self._send({
                        "type": "system",
                        "subtype": "session_reseeded",
                        "message": seed_notice,
                        "chat_id": wcid,
                    })
                # Interactive: the CLIENT attaches the PTY viewer itself via a
                # `pty_attach` frame once its terminal has mounted + subscribed —
                # so the scrollback replay can't race the subscribe (blank-terminal
                # bug). warmup_ready{interactive:true} above is the signal; the
                # connection's session_id is already adopted. No server-kick (the
                # human drives the PTY) — handled below.
            except NoSubscriptionError as e:
                # Expected + actionable: this user has no usable credentials for the
                # layer (no own sub, and either Platform Auth off or only un-borrowable
                # admin OAuth in the pool). Surface the friendly message + a
                # machine-readable reason — it's a configuration state, not a crash.
                logger.info(f"WS dashboard warmup blocked (no subscription, reason={e.reason}) chat={wcid}")
                await warmup_registry.emit(wcid, {
                    "type": "warmup_failed",
                    "chat_id": wcid,
                    "error": str(e),
                    "reason": e.reason,
                })
            except Exception as e:
                logger.error(f"WS dashboard warmup failed: {e}", exc_info=True)
                # Classify so the UI distinguishes a genuinely-offline/unreachable
                # remote target ("Remote machine unavailable") from a session that
                # FAILED TO START on a reachable machine (a config/spawn error,
                # e.g. a bad config.toml) — the latter is not an availability
                # problem, so the "machine unavailable" title misled. The
                # connection layer raises "...not connected" / "...command timeout"
                # for true unavailability; a satellite that ran the command and
                # returned an error surfaces as "Satellite command error: ...".
                emsg = str(e)
                fail_reason = (
                    "target_unavailable"
                    if ("not connected" in emsg or "command timeout" in emsg)
                    else "session_error"
                )
                await warmup_registry.emit(wcid, {
                    "type": "warmup_failed",
                    "chat_id": wcid,
                    "error": emsg,
                    "reason": fail_reason,
                })
            finally:
                heartbeat_stop.set()
                try:
                    await asyncio.wait_for(hb_task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    hb_task.cancel()
                # Terminal event emitted; drop the registry entry so the next
                # chat reusing this WS doesn't see stale state.
                await warmup_registry.unregister(wcid)
                self._attached_warmups.discard(wcid)

            if res is None:
                return  # spawn failed — warmup_failed already emitted, no kick
            # Re-apply any deferred mode/model the user set during the spawn —
            # only if they're still on this chat (else the deferred value
            # belongs to the chat they switched to, handled by that chat's flow).
            if still_viewing:
                await self._reapply_deferred_after_warmup()
            # Interactive: there is NO server-kicked pump turn — the human drives
            # the PTY. A cold send's text rides the warmup and is delivered HERE,
            # server-side (submit_prompt buffers until the TUI is ready and arms
            # the deferred Enter), so the prompt survives the client switching
            # chats / reloading mid-warmup. Codex fresh spawns delivered it via
            # the launch argv instead (auto-runs) — skip those (double-send).
            if res.interactive:
                first_text = msg.get("text", "")
                if first_text and not res.first_prompt_in_argv:
                    isess = interactive_session.get(res.session_id)
                    if isess is not None and isess.alive:
                        # Stamp browser time/zone at delivery (parity with the
                        # -p pump's injection; the client sends the text RAW so
                        # a declined-to-headless kick can't double-stamp).
                        user_tz = (get_session_user_tz(res.session_id)
                                   or get_user_tz(self.user_sub))
                        stamped = (
                            f"[Current time: "
                            f"{config.format_current_time(user_tz)}]"
                            f"\n\n{first_text}"
                        )
                        # Re-note the STAMPED bytes (replaces the raw note from
                        # _persist_first_prompt): the tailer's duplicate-skip
                        # matches the journaled user line byte-for-byte and the
                        # CLI journals what the PTY received — without this the
                        # first user row lands twice. Any reseed digest rides
                        # the session's stashed seed (set_pending_seed above),
                        # prepended at the first submission — NOT here.
                        from core.session.transcript_tool_events import (
                            note_sent_prompt,
                        )
                        note_sent_prompt(wcid, stamped)
                        logger.info(
                            f"WS dashboard: server-side first prompt submit "
                            f"chat={wcid[:8]} session={res.session_id[:8]}"
                        )
                        isess.submit_prompt(stamped)
                return
            # Server-owned first turn. If the WS is gone (refresh/navigate during
            # the spawn), there is no main loop to drive the kick — run it
            # HEADLESS right here so the turn still lands in wcid's DB (preserves
            # refresh-during-spawn durability). Otherwise hand the kick to
            # the MAIN loop via the notify queue (the tail must NOT enter the pump
            # loop itself — that would be a 2nd socket reader); the main loop
            # routes it through _run_server_turn → streams if still viewing wcid,
            # else headless. Auto-warmup callers carry no text → no kick.
            # a wants-interactive chat that fell back to -p (no PTY on the
            # satellite) AND was reseeded carries the digest into the kick too, so
            # the -p turn restores context (_start_new_stream's own consume then
            # no-ops — the flag is already cleared). A genuine -p chat reseeds in
            # _start_new_stream (seed_digest is '' here).
            kick_text = msg.get("text", "")
            if seed_digest and kick_text:
                kick_text = f"{seed_digest}\n\n{kick_text}"
            if kick_text:
                if self._ws_gone:
                    await self._run_kick_headless(
                        wcid, res.session_id, kick_text,
                        msg.get("images", []), msg.get("files", []),
                        force_headless=True,
                    )
                else:
                    # Breadcrumb: pairs with "server kick drained" /
                    # "close-rescue" so a lost first turn is diagnosable.
                    logger.info(
                        f"WS dashboard: server kick enqueued for chat={wcid[:8]}"
                    )
                    self.notify_queue.put_nowait({
                        "type": "_server_kick",
                        "session_id": res.session_id,
                        "chat_id": wcid,
                        "text": kick_text,
                        "images": msg.get("images", []),
                        "files": msg.get("files", []),
                    })

        self._warmup_task = asyncio.create_task(_spawn_tail())
        return "bg"

    async def _reapply_deferred_after_warmup(self) -> None:
        if not self.session_id or not self.layer:
            return
        try:
            alive = await self.layer.is_session_alive(self.session_id)
        except Exception:
            alive = False
        if not alive:
            return
        if self.deferred_mode:
            pending = self.deferred_mode
            self.deferred_mode = ""
            set_session_mode(self.session_id, pending)
            if self.chat_id:
                task_store.update_chat(self.chat_id, permission_mode=pending)
            try:
                await self.layer.change_mode(self.session_id, pending)
            except Exception as e:
                logger.warning(f"Post-warmup mode re-apply failed: {e}")
            await self._send({"type": "mode_changed", "mode": pending})
            logger.info(
                f"WS dashboard post-warmup re-applied deferred mode={pending} "
                f"to session={self.session_id[:8]}"
            )
        if self.deferred_model:
            pending = self.deferred_model
            self.deferred_model = ""
            # The deferral may predate the chat binding (no path known at
            # _handle_model_change time) — validate against the layer that
            # actually spawned before applying (see _model_allowed_for_path).
            chat_path = (task_store.get_chat(self.chat_id) or {}).get("execution_path", "") if self.chat_id else ""
            if not _model_allowed_for_path(pending, chat_path):
                logger.warning(
                    f"WS dashboard deferred model DROPPED: model={pending} is not a "
                    f"{chat_path} model (chat={self.chat_id})"
                )
                return
            if self.chat_id:
                task_store.update_chat(self.chat_id, model=pending)
            try:
                await self.layer.change_model(self.session_id, pending)
            except Exception as e:
                logger.warning(f"Post-warmup model re-apply failed: {e}")
            await self._send({"type": "model_changed", "model": pending})
            logger.info(
                f"WS dashboard post-warmup re-applied deferred model={pending} "
                f"to session={self.session_id[:8]}"
            )

    async def _create_or_resume_session(self,
        sid: str, agent: str, perm_mode: str, *, resume: bool = False,
        model: str = "", exec_path: str = "", codex_thread_id: str = "",
        chat_id: str = "", pinned_target: str = "", adopt: bool = True,
        chat_exec_mode: str = "", chat_theme: str = "", first_prompt: str = "",
    ) -> _SpawnResult:
        """Create a session via the execution layer.

        When resume=True, reuses the old session_id with --resume so CLI
        loads conversation history from disk.

        ``adopt`` (default True): write the resolved session into the
        connection's VIEWED attributes (``session_id``/``layer``/
        ``session_execution_target``/``session_fallback_reason``). A
        backgrounded warmup spawn passes ``adopt=False`` so a chat-switch
        mid-spawn can't clobber the now-viewed chat — it adopts the returned
        ``_SpawnResult`` itself only if the user is still viewing the warmed
        chat. Always returns the ``_SpawnResult``.
        """

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
        effective_role = _effective_agent_role(self.user_sub, agent, fallback_user=self.user)
        # Task re-warm: a `task-{run_id}` chat ALWAYS rebuilds in the task's
        # stored scope/identity (agent-scope → no user, agent role; user-scope
        # → the creator), never the viewer's. Resolved from the run row here
        # so EVERY (re)build path that funnels through this helper is covered.
        # The continue-gate (_deny_task_continue) is enforced separately at the
        # warmup / chat entry points.
        task_identity = None
        if chat_id.startswith("task-"):
            run = task_store.get_run(chat_id.removeprefix("task-"))
            if run:
                task_identity = resolve_task_identity(
                    agent, run.get("scope") or "agent", run.get("created_by"),
                )
        agent_cfg = await build_agent_config(
            agent_name=agent, user=self.user, user_sub=self.user_sub,
            user_role=effective_role, permission_mode=perm_mode,
            client_type="dashboard", resume=resume, model=model,
            execution_path=exec_path,
            codex_thread_id=codex_thread_id,
            chat_id=chat_id,
            session_id=sid,
            task_identity=task_identity,
            pinned_target=pinned_target,
        )
        # Hard-fail if the resolved target is offline and fallback is disabled.
        # The resolver encoded this as a "__offline__:<machine_id>" sentinel.
        # Checked BEFORE get_execution_layer so the user gets the tailored
        # message below instead of the resolver's generic offline RuntimeError.
        if is_hard_fail_target(agent_cfg.execution_target):
            # No slot acquired yet (acquire happens after this check), so there
            # is nothing to release here.
            offline_machine_id = extract_offline_machine(agent_cfg.execution_target)
            machine = remote_store.get_remote_machine(offline_machine_id)
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
            is_admin_target = (machine.get("pairing_scope") or "") == "admin"
            if is_admin_target:
                # Admin remote target — blocks everyone using this agent.
                msg = (
                    f"This agent's remote machine '{machine_label}' is currently offline. "
                    f"Please reconnect the remote machine or contact your admin."
                )
            else:
                # User-paired target — only this user is affected.
                msg = (
                    f"Your remote machine '{machine_label}' is offline. "
                    f"Reconnect it from User Settings → Remote Machines, or remove the per-agent override to fall back to the default target."
                )
            raise RuntimeError(msg)

        # Acquire the concurrency slot now that the target is known. A LOCAL
        # session takes a unit of the local ceiling G; a denial carries the
        # real reason (busy vs host-memory pressure) so the user never sees
        # "too many sessions" while the admin page shows zero. A REMOTE session
        # is budgeted on its satellite, so this short-circuits to admitted (no
        # local slot). Idempotent for a reused pre-warm (already tracked) and
        # for an interactive task (already "task").
        from core.concurrency import acquire_chat_slot
        adm = await acquire_chat_slot(sid, target=agent_cfg.execution_target,
                                      execution_path=agent_cfg.execution_path,
                                      user_sub=self.user_sub)
        if not adm:
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
        agent_cfg.interactive = _resolve_session_interactive(agent_cfg, chat_exec_mode)
        # Dashboard light/dark at spawn → seeds Claude's TUI theme to match.
        # Server-side re-warms (dead-session auto-resume, headless server turns)
        # carry no dashboard snapshot (chat_theme='') — fall back to the chat's
        # last BAKED theme so a light terminal never flips dark across a
        # respawn. 'dark' only for chats that never baked one (otodock-opened
        # sessions seed dark by design).
        chat_baked_theme = ""
        if not chat_theme and chat_id:
            chat_baked_theme = (task_store.get_chat(chat_id) or {}).get("tui_theme") or ""
        agent_cfg.interactive_theme = chat_theme or chat_baked_theme or "dark"
        # Codex interactive resumes by THREAD id (chats.codex_thread_id), whose
        # rollout persists on disk independently of the in-memory app-server
        # session AND the warmup's `resume` flag (which is keyed on the just-closed
        # -p session, so it's False right after a -p→terminal switch). Re-derive
        # here: resume iff the thread's rollout is still on disk — otherwise a
        # switch back to the terminal (or a reopen) starts a FRESH codex with no
        # context. Claude resumes by session_id via --resume → unaffected; this
        # only touches codex interactive.
        if agent_cfg.interactive and (agent_cfg.execution_path or "") == "codex-cli":
            _tid = (agent_cfg.codex_thread_id or "").strip()
            if (agent_cfg.execution_target or "local") != "local":
                # Remote: the rollout lives on the satellite, not on local
                # disk, so rollout_exists (a local glob) can't see it. Trust the
                # thread id — the satellite's CodexPtySession locates
                # rollout-*-<tid>.jsonl and `codex resume <tid>` continues it. A
                # genuinely-missing remote rollout is the DB-fallback case, not
                # handled here (codex would just start a fresh thread).
                agent_cfg.resume = bool(_tid)
            else:
                from core.session import codex_rollout_tailer
                agent_cfg.resume = bool(_tid) and codex_rollout_tailer.rollout_exists(
                    agent_cfg.sandbox_host_claude_dir, _tid,
                )
        # Codex interactive cold first prompt: a FRESH spawn delivers it as the
        # `codex` launch arg → the TUI auto-runs it after MCP warm (deterministic
        # first-turn submit; the PTY type-then-Enter race is unreliable during
        # Codex's warm). On RESUME the prompt instead rides the server-side
        # submit in `_spawn_tail` (so `codex resume <tid>` continues the thread
        # and any new turn still runs). Claude — fresh and resume — always uses
        # that server-side submit too.
        _first_prompt_in_argv = False
        if (agent_cfg.interactive and first_prompt and not agent_cfg.resume
                and (agent_cfg.execution_path or "") == "codex-cli"):
            agent_cfg.interactive_first_prompt = first_prompt
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
        if chat_id and not pinned_target and not is_hard_fail_target(agent_cfg.execution_target):
            task_store.update_chat(chat_id, execution_target=agent_cfg.execution_target)
        # Remember the baked TUI theme for future server-side re-warms (see the
        # resolution above). Only when a REAL dashboard snapshot arrived — a
        # fallback-resolved re-warm must never overwrite the baked value with
        # 'dark'. Best-effort: a pre-migration DB lacks the column (one-time
        # manual ALTER) and a cosmetic write must not fail the spawn.
        if chat_id and chat_theme and agent_cfg.interactive:
            try:
                task_store.update_chat(chat_id, tui_theme=agent_cfg.interactive_theme)
            except Exception:
                logger.debug("tui_theme persist failed", exc_info=True)
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
    ) -> _SpawnResult:
        """Re-warm a dead session for ``t_chat_id``, resolving its agent / model /
        exec-path / pinned target from the CHAT ROW (never the viewed attributes).

        ``adopt=True`` is the VIEWED auto-resume — ``_create_or_resume_session``
        writes the connection's ``session_id``/``layer`` attributes. ``adopt=False``
        is the Step-6 path: a server turn (bg-nudge / delegate result) whose
        originating chat was reaped while the user views a DIFFERENT chat — warm
        it WITHOUT clobbering the viewed attributes and run headless on the
        returned ``_SpawnResult``. Raises on spawn failure (the caller handles)."""
        chat_rec = task_store.get_chat(t_chat_id) if t_chat_id else None
        chat_perm_mode = (chat_rec or {}).get("permission_mode", "default")
        chat_model = (chat_rec or {}).get("model", "")
        chat_agent = (chat_rec or {}).get("agent") or self.agent_name
        chat_exec_path = (chat_rec or {}).get("execution_path", "")
        chat_pinned = (chat_rec or {}).get("execution_target") or ""
        # ORDER IS LOAD-BEARING: can_resume_session BEFORE prepare_resume — the
        # remote resumability check needs the session info's machine_id, which
        # prepare_resume pops; once popped the check returns False unconditionally
        # (forcing a fresh session + dropping chat memory). Local derives its path
        # from agent+username, so it's order-independent — this order keeps both.
        can_resume = await layer_for_chat.can_resume_session(
            dead_sid, agent_name=chat_agent,
            username=_resume_username_for_chat(
                t_chat_id or "", chat_agent, self.user.get("username", ""),
            ),
        )
        await layer_for_chat.prepare_resume(dead_sid)
        if can_resume:
            return await self._create_or_resume_session(
                dead_sid, chat_agent, chat_perm_mode, resume=True, model=chat_model,
                exec_path=chat_exec_path, chat_id=t_chat_id, pinned_target=chat_pinned,
                adopt=adopt,
            )
        # No conversation data → fresh session. Release the old slot first
        # (prepare_resume removed the session from the registry).
        from core.concurrency import release_chat_slot
        release_chat_slot(dead_sid)
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
            # this returns and prepends the digest.
            task_store.update_chat(
                t_chat_id, session_id=new_sid,
                pending_history_seed="resume_failed",
            )
        return res
