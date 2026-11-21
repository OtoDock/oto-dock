"""The single create or resume funnel every spawn path goes through, the resume of
a dead session behind a chat, and what a reused session takes from its chat.
``WarmupController`` has this mixin as its base.

Import ``ws.dashboard`` before this module, as the warmup module needs.
"""

import functools
import logging
import uuid
from typing import TYPE_CHECKING

import config
from auth.providers import acting_role_of
from core import file_commit, placement
from core.config.config_builder import build_agent_config, release_config_seat
from core.config.task_config_builder import rewarm_task_identity
from core.engine import Engine
from core.events import chat_writer
from core.execution_layer import ASK_MODE, ExecutionLayer, MachineReach, ResumeVerdict
from core.session import interactive_session, session_kind
from core.session.resume_probe import (
    RESUME_PROBE_BUDGET_INLINE_S,
    ResumeUndecided,
    settle_resume,
)
from core.session.session_manager import (
    get_execution_layer,
    get_layer_by_path,
    get_layer_capabilities,
)
from core.session.session_state import (
    clear_session_liveness,
    get_session_mode,
    get_user_tz,
    set_session_mode,
    set_session_user_tz,
)
from services.engines import subscription_pool
from storage import database as task_store, remote_store
from storage.pg import run_db, run_db_fast
from ws import wire_events as wire
from ws.dashboard_chat_text import TurnDeferred
from ws.dashboard_warmup_tail import WarmupTail, _persist_spawn

if TYPE_CHECKING:
    from ws.dashboard import _SpawnResult

logger = logging.getLogger("otodock")


def _built_placement(cfg):
    """The compute placement a config was built with (its security
    context's), None when the platform's own sandbox serves it."""
    ctx = getattr(cfg, "security_context", None)
    return placement.compute_of(getattr(ctx, "placement", None) if ctx is not None else None)


class WarmupSpawn(WarmupTail):
    # The connection state this mixin assigns, declared where it assigns it,
    # as in ``WarmupController``. Annotations only (strings), no class attributes.
    deferred_for: "str"
    deferred_mode: "str"
    deferred_model: "str"

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

    async def _deferred_mode_stands(self, chat_id: str, bound_pick: bool) -> bool:
        """A chat's pick wrote its row when it was made: a row that holds
        another mode since (a terminal's Shift+Tab lowered the chat, a later
        pick) wins over this socket's copy, which would otherwise raise the
        chat again. A new-chat page's pick has no row of its own: it stands."""
        if not bound_pick:
            return True
        row = await run_db(task_store.get_chat, chat_id) or {}
        held = row.get("permission_mode") or ""
        if not row or held == self.deferred_mode:
            return True
        logger.info("WS dashboard post-warmup: deferred mode=%s dropped, chat=%s holds %s",
                    self.deferred_mode, chat_id[:8], held)
        return False

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
        bound_pick = self.deferred_for == chat_id
        self.deferred_for = ""
        if self.deferred_mode and not await self._deferred_mode_stands(chat_id, bound_pick):
            self.deferred_mode = ""
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
            from services.remote import machine_away
            if machine_away.held_out(offline_machine_id):
                # Away for a reason the platform knows: its version, or its update.
                raise machine_away.MachineAway(machine_away.away_sentence(offline_machine_id, machine_label, msg))
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
        built = _built_placement(agent_cfg)
        adm = await acquire_chat_slot(sid, target=placement.admission_target(agent_cfg.execution_target, built),
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
        # remote placement too — the answer is a function of the config. The
        # base answer reads the flag and is asked inline; an engine's own
        # answer may search its folder, so it runs on the probes' lane.
        resume_layer = get_layer_by_path(agent_cfg.execution_path or exec_path)
        if getattr(type(resume_layer), "resumes_interactive", None) is Engine.resumes_interactive:
            agent_cfg.resume = resume_layer.resumes_interactive(agent_cfg)
        else:
            agent_cfg.resume = await file_commit.run_in(
                "session-probes", resume_layer.resumes_interactive, agent_cfg)
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
            **({"placement_built": built} if built else {}),
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
        # The platform's chat with no compute machine to start it on: known
        # before anything of the session is touched. Its own machine
        # restarted a moment ago: the turn waits for the sign-in, as it does
        # for a machine that reconnects. Away longer: the builders' refusal.
        from core.session.session_manager import no_compute_for
        from services.remote import compute_bootstrap
        if await run_db(compute_bootstrap.chat_waits, chat_rec):
            logger.info("WS dashboard: the turn on session %s waits: the compute machine "
                        "restarted a moment ago", dead_sid[:8])
            raise TurnDeferred(dead_sid)
        if chat_rec and await run_db(no_compute_for, chat_rec):
            raise placement.NoComputeMachine()
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
