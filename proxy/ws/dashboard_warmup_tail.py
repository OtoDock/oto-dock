"""The backgrounded spawn tail's phases and the two objects they share:
``_TailArgs`` (what ``_do_warmup`` snapshotted as it announced the warmup)
and ``_TailRun`` (the state the phases, the except arms, the finally and
the first turn share). ``WarmupController._spawn_tail`` (ws/dashboard_warmup.py)
keeps the try/except/finally around them, the first-turn hold and the
unregister; ``WarmupTail`` is its base, so the phases run as methods of the
connection. Behavior is pinned by tests/session/test_ws_dashboard_*.

Import ``ws.dashboard`` before this module (as the warmup module needs): the
two helpers it takes from there are defined above the point where
``ws/dashboard.py`` assembles its mixins. The names the tests patch on
``ws.dashboard_warmup`` (``run_db``, ``run_db_fast``,
``RESUME_PROBE_BUDGET_WARMUP_S``) are read through that module at call
time, never bound here.
"""

import asyncio
import contextlib
import functools
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import psycopg

from core import execution_mode, placement
from core.events import chat_writer, input_queue
from core.execution_layer import ExecutionLayer, MachineReach, ResumeVerdict
from core.sandbox.session_config_dir import AgentStateRefused
from core.session import interactive_session, warmup_registry
from core.session.history_seed import consume_pending_seed_digest
from core.session.resume_probe import ResumeUndecided, settle_resume
from core.session.session_state import clear_session_liveness, get_session_mode
from services.engines import subscription_pool
from services.remote import remote_status
from storage import database as task_store, remote_store

# ws/dashboard.py assembles the connection from the mixins at its end, so
# its helpers are imported inside the functions that call them: this
# module imports first too (tests/core/test_import_layers.py).
from ws import wire_events as wire
from ws.dashboard_chat_text import MACHINE_BUSY_NOT_SENT, SESSION_FILES_UNREAD_NOT_SENT

if TYPE_CHECKING:
    from ws.dashboard import _SpawnResult
    from ws.dashboard_host import DashboardHost as _Host
else:
    _Host = object

logger = logging.getLogger("otodock")


@dataclass
class _TailArgs:
    """What the backgrounded spawn tail reads, snapshotted when the warmup
    was announced: the connection's chat_id/agent_name/layer attributes may
    change underneath the spawn if the user switches chats during the (slow)
    start_session, so the tail never reads them back."""
    wcid: str
    w_agent: str
    w_layer: ExecutionLayer | None
    w_pinned: str  # the chat's pinned target (an existing chat); "" otherwise
    w_thread: str  # the chat's codex thread id (an existing chat); "" otherwise
    # New-chat pre-warm match key; ""/empty for existing-chat warmups.
    w_role: str
    w_target_model: str
    w_exec_mode: str  # per-chat interactive override
    w_theme: str  # dashboard light/dark
    msg: dict
    permission_mode: str
    chat_model: str
    effective_exec_path: str
    old_session_id: str | None
    pw_task: asyncio.Task | None
    heartbeat_stop: asyncio.Event
    hb_task: asyncio.Task
    # A minted chat's place, picked by its page (``local`` or a machine id,
    # checked; ``""``: the person's default) and when it was minted.
    w_place: str = ""
    w_minted_at: float = 0.0


@dataclass
class _TailRun:
    """The spawn tail's state, shared by its phases, its except arms, its
    finally and the first turn after it; each field is assigned at the
    statement that settles it (a raise after ``res`` is set still finds it
    set; ``aborted`` is set before the abort's await)."""
    res: "_SpawnResult | None" = None
    aborted: bool = False
    still_viewing: bool = False
    wants_interactive: bool = False
    seed_digest: str = ""
    seed_notice: str = ""
    # The reason we claimed, for restore-on-failure: the claim happens
    # BEFORE the spawn, so a failed spawn (offline satellite,
    # missing CLI after an engine switch, config error) would otherwise
    # BURN the seed — the eventual successful warmup then starts the
    # fresh session with no history digest at all. Captured pre-claim
    # (the claim clears the column and doesn't return the raw reason).
    seed_reason_claimed: str = ""
    argv_first_prompt: str = ""


def _override_machine_name(user_sub: str, agent: str) -> str:
    """The name of the person's own machine for this agent ("" when none):
    the banner of a session that fell back from it."""
    target = remote_store.get_user_remote_target(user_sub, agent)
    if not target:
        return ""
    return str((remote_store.get_remote_machine(target["machine_id"]) or {}).get("name") or "")


def _persist_spawn(chat_id: str, pin: str, theme: str) -> None:
    """A spawn's chat-row writes, one chat-writer job of
    ``_create_or_resume_session``: the resolved target a later resume pins
    to (a failure fails the spawn) and the baked theme (best-effort)."""
    if pin:
        task_store.update_chat(chat_id, execution_target=pin)
    if theme:
        try:
            task_store.update_chat(chat_id, tui_theme=theme)
        except psycopg.Error:
            logger.debug("tui_theme persist failed", exc_info=True)


def _stamp_abort(chat_id: str) -> None:
    """The abort-during-spawn stamp. A stamp that fails changes nothing: the
    stop stands."""
    try:
        task_store.update_chat(chat_id, last_turn_aborted=True)
    except psycopg.Error:
        logger.warning("abort-during-spawn: the chat=%s row was not stamped", chat_id)


async def _restore_seed(chat_id: str, reason: str, *, best_effort: bool = False) -> None:
    """A failed spawn gives the chat back the history seed it claimed."""
    def _job() -> None:
        try:
            task_store.update_chat(chat_id, pending_history_seed=reason)
        except Exception:  # a best-effort restore loses one digest, the failure card stands
            if not best_effort:
                raise
            logger.debug("seed restore failed for chat=%s", chat_id, exc_info=True)

    await chat_writer.submit(chat_id, _job, label="seed_restore")


class WarmupTail(_Host):
    """The spawn tail's phases, in the order ``_spawn_tail`` runs them: the
    seed claim, the spawn (a resume or a fresh start for an existing chat,
    a pre-warm or a fresh start for a minted one), the abort check, the
    adoption, the failure card, the first turn."""

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
    layer: "ExecutionLayer | None"
    session_id: "str | None"

    async def _release_prewarm(self) -> None:
        """Close this connection's pre-warmed session and forget it: ours to
        close only while the registry still holds it (another tab's warmup
        may have claimed it by key and run its chat on it, or the reaper has
        it and closes it itself)."""
        old_sid = self._pre_warmed_sid
        old_exec_path = self._pre_warmed_exec_path
        self._pre_warmed_sid = None
        self._pre_warmed_agent = None
        self._pre_warmed_exec_path = ""
        self._pre_warmed_model = ""
        self._pre_warmed_role = ""
        self._pre_warmed_place = ""
        self._pre_warmed_at = 0.0
        if not old_sid:
            return
        from core.session import prewarm_session_registry as _prewarm
        if await _prewarm.claim(old_sid):
            try:
                from core.session.session_manager import layer_for_prewarm
                old_layer = layer_for_prewarm(old_sid, old_exec_path)
                if old_layer:
                    await old_layer.close_session(old_sid)
            except Exception:  # noqa: BLE001  # closing a pre-warmed session nobody will use
                logger.debug("closing pre-warm %s failed", old_sid[:8], exc_info=True)
            from core.concurrency import release_chat_slot
            release_chat_slot(old_sid)
            logger.info("WS dashboard: pre-warmed session %s closed", old_sid[:8])

    async def _tail_claim_seed(self, w: _TailArgs, run: _TailRun) -> None:
        """An interactive chat's DB-digest reseed claimed before the spawn; the
        argv prompt stays empty when a digest will be pasted."""
        # An interactive chat must not adopt a headless pre-warmed session
        # (wrong spawn mode), so skip the pre-warm reuse path when interactive
        # is resolved-on. Layer/target gating happens in
        # _create_or_resume_session (falls back to -p if unsupported).
        from ws import dashboard_warmup
        run.wants_interactive = await dashboard_warmup.run_db_fast(
            functools.partial(execution_mode.is_interactive, chat_override=w.w_exec_mode or None))
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
        if run.wants_interactive:
            def _seed_claim_job():
                # Pre-claim read + the destructive claim, one lane job.
                reason = (task_store.get_chat(w.wcid) or {}).get("pending_history_seed") or ""
                # Smaller cap than the -p path (48k): the digest is
                # bracketed-pasted into the live TUI, so keep it light to
                # render fast (well under the Enter backstop) —
                # build_history_seed is newest-biased, so this keeps the
                # most recent context.
                digest, notice = consume_pending_seed_digest(w.wcid, max_chars=16_000)
                return reason, digest, notice

            _pre_claim_reason, run.seed_digest, run.seed_notice = await chat_writer.submit(
                w.wcid, _seed_claim_job, label="seed_claim",
            )
            if run.seed_notice:
                run.seed_reason_claimed = _pre_claim_reason
        # A reseeded prompt's digest is multi-line → keep it off Codex's launch
        # argv (Windows cmdline limit + structure loss); it's pasted via the PTY.
        run.argv_first_prompt = "" if run.seed_digest else w.msg.get("text", "")

    async def _tail_start(self, w: _TailArgs, run: _TailRun) -> None:
        """The spawn: an existing chat resumes its session or starts fresh, a
        minted chat takes a pre-warm or starts fresh."""
        if w.old_session_id:
            await self._tail_resume_or_fresh(w, run)
        else:
            await self._tail_new_chat(w, run)

    async def _tail_resume_or_fresh(self, w: _TailArgs, run: _TailRun) -> None:
        """An existing chat: the one-shot delivery awaited, the resume probe
        settled, the session resumed or a fresh one started with the reseed
        flagged."""
        # A one-shot delivery (--resume echo to this dead chat) may
        # still be writing the same JSONL: resuming NOW renders the
        # transcript as-of-open (the delivered turn stays invisible
        # until the next toggle) and dual-writes the file. Await it;
        # the heartbeat loop keeps the user informed meanwhile. Past
        # the cap, proceed — degrades to the pre-guard behavior.
        from core.session import session_delivery
        from ws import dashboard_warmup
        from ws.dashboard import _resume_username_for_chat
        inflight = session_delivery.oneshot_inflight(w.wcid)
        if inflight is not None:
            logger.info(
                "WS dashboard warmup: awaiting in-flight one-shot "
                "delivery on chat=%s before resume", w.wcid
            )
            try:
                await asyncio.wait_for(inflight.wait(), timeout=120)
            except asyncio.TimeoutError:
                logger.warning(
                    "WS dashboard warmup: one-shot still running after "
                    "120s on chat=%s — resuming anyway", w.wcid
                )
        # Does the CLI session file still have conversation data on
        # disk? If the proxy restarted / the session was reaped the
        # file may be gone — create a fresh session in that case.
        # Only a definite "no" takes that branch: a machine away
        # or not answering is waited on within the budget and
        # then refused (ResumeUndecided, caught below), since the
        # fresh branch replaces the chat's context for good.
        verdict = ResumeVerdict.FRESH
        resume_username = ""
        if w.w_layer:
            resume_username = await dashboard_warmup.run_db(
                _resume_username_for_chat, w.wcid, w.w_agent, self.user.get("username", ""),
            )
            verdict = await settle_resume(
                w.w_layer, cast("str", w.old_session_id), agent_name=w.w_agent,
                username=resume_username,
                budget_s=dashboard_warmup.RESUME_PROBE_BUDGET_WARMUP_S,
            )
        if verdict is ResumeVerdict.UNKNOWN:
            # UNKNOWN comes only from the probe above, which ran on a layer.
            reach = cast("ExecutionLayer", w.w_layer).resume_machine_reach(
                cast("str", w.old_session_id), agent_name=w.w_agent,
                username=resume_username)
            raise ResumeUndecided(cast("str", w.old_session_id), local=reach is MachineReach.LOCAL)
        if verdict is ResumeVerdict.RESUMABLE:
            run.res = await self._create_or_resume_session(
                cast("str", w.old_session_id), w.w_agent, w.permission_mode, resume=True,
                model=w.chat_model, exec_path=w.effective_exec_path,
                resume_handle=w.w_thread, chat_id=w.wcid,
                pinned_target=w.w_pinned, adopt=False,
                chat_exec_mode=w.w_exec_mode, chat_theme=w.w_theme,
            )
            logger.info(
                "WS dashboard warmup (resume): session=%s, "
                "chat=%s, agent=%s, model=%s",
                w.old_session_id, w.wcid, w.w_agent, w.chat_model
            )
        else:
            # The dead session's binding would outlive it: a
            # resume hands it over at bind time, a fresh id never
            # meets it, and nothing else releases a seat under an
            # id no session runs.
            subscription_pool.release_subscription(cast("str", w.old_session_id))
            new_sid = str(uuid.uuid4())
            run.res = await self._create_or_resume_session(
                new_sid, w.w_agent, w.permission_mode, resume=False,
                model=w.chat_model, exec_path=w.effective_exec_path,
                resume_handle=w.w_thread, chat_id=w.wcid,
                pinned_target=w.w_pinned, adopt=False,
                chat_exec_mode=w.w_exec_mode, chat_theme=w.w_theme,
                first_prompt=run.argv_first_prompt,
                focus_line=w.msg.get("_focus_line", ""),
            )
            # The resume gate legitimately refused (missing JSONL,
            # RPC timeout, satellite mid-reconnect) — this fresh
            # session starts with no context, so flag the DB-digest
            # reseed; and the dead session's background work died
            # with it, so clear any stuck liveness badges.
            # The interactive digest claim above ran BEFORE this
            # branch decided fresh — claim again so the digest
            # still rides the PTY paste (the -p path claims at
            # the _start_new_stream chokepoint). The flag and the
            # claim are one lane job.
            reclaim = run.wants_interactive and not run.seed_digest
            wcid = w.wcid

            def _fresh_job() -> tuple[str, str] | None:
                task_store.update_chat(wcid, session_id=new_sid,
                                       pending_history_seed="resume_failed")
                return consume_pending_seed_digest(wcid, max_chars=16_000) if reclaim else None

            claimed = await chat_writer.submit(wcid, _fresh_job, label="resume_failed")
            clear_session_liveness(
                cast("str", w.old_session_id), reason="resume_failed",
            )
            if claimed is not None:
                run.seed_digest, run.seed_notice = claimed
            logger.info(
                "WS dashboard warmup (fresh, old session gone): session=%s, "
                "chat=%s, agent=%s, model=%s",
                new_sid, w.wcid, w.w_agent, w.chat_model
            )

    async def _tail_new_chat(self, w: _TailArgs, run: _TailRun) -> None:
        """A minted chat: the eager pre-warm awaited and reused when it still
        matches, else a detached pre-warm claimed by key, else a fresh spawn."""
        from ws.dashboard import _SpawnResult
        # NEW CHAT. Await the in-flight eager pre-warm HERE (off the WS
        # loop, so warmup_started already fired), then reuse it if it
        # still matches this chat's agent/path/model/role + is alive, else
        # spawn fresh. We never CLOSE a mismatched pre-warm from the tail
        # (a concurrent new-chat pre-warm may own _pre_warmed_sid by now);
        # the next pre_warm/warmup or the idle reaper reclaims a stale one.
        if w.pw_task is not None and not w.pw_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await w.pw_task
        # A pre-warm is local by construction: a chat its page placed on a
        # machine reuses none, and the one this page made before the mint
        # (for the platform) is closed now rather than left to the reaper.
        on_machine = bool(w.w_place) and not placement.is_local(w.w_place)
        if on_machine and self._pre_warmed_at and self._pre_warmed_at <= w.w_minted_at:
            await self._release_prewarm()
        from ws.dashboard_chat_move import picked_place
        place_key = picked_place(w.msg)
        pw_match = (
            not on_machine
            and self._pre_warmed_sid is not None
            and self._pre_warmed_agent == w.w_agent
            and self._pre_warmed_exec_path == w.effective_exec_path
            and self._pre_warmed_model == w.w_target_model
            and self._pre_warmed_role == w.w_role
            and self._pre_warmed_place == place_key
        )
        # Claim the pre-warm out of the TTL reaper's reach as the FINAL
        # gate — if the reaper already took it (claim False), it's being
        # closed, so fall through to a fresh spawn.
        from core.session import prewarm_session_registry as _prewarm
        from core.session.session_manager import layer_for_prewarm
        pw_layer = (layer_for_prewarm(cast("str", self._pre_warmed_sid), self._pre_warmed_exec_path)
                    if pw_match else None)
        pw_reuse_ok = (
            pw_match and not run.wants_interactive and pw_layer is not None
            and await pw_layer.is_session_alive(cast("str", self._pre_warmed_sid))
            and await _prewarm.claim(cast("str", self._pre_warmed_sid))
        )
        reuse_sid = None
        reuse_layer = None
        reuse_note = ""
        if pw_reuse_ok:
            reuse_sid = self._pre_warmed_sid
            reuse_layer = pw_layer
            if self._pre_warmed_at:
                reuse_note = (
                    f", pre-warmed "
                    f"{time.monotonic() - self._pre_warmed_at:.1f}s ago"
                )
            self._pre_warmed_sid = None
            self._pre_warmed_agent = None
            self._pre_warmed_exec_path = ""
            self._pre_warmed_model = ""
            self._pre_warmed_role = ""
            self._pre_warmed_place = ""
            self._pre_warmed_at = 0.0
        elif not run.wants_interactive and w.w_layer is not None and not w.w_place:
            # DETACHED pre-warm fallback: wake-word detection fires
            # an HTTP pre-warm with no WS connection to remember it
            # on — claim by full key from the global registry, one made for
            # no pick (another tab's pick is its own chat's); a chat placed
            # elsewhere takes none.
            claimed = await _prewarm.claim_by_key(
                user_sub=self.user_sub, agent=w.w_agent,
                model=w.w_target_model, role=w.w_role,
                exec_path=w.effective_exec_path,
            )
            if claimed is not None:
                c_sid, c_age = claimed
                c_layer = layer_for_prewarm(c_sid, w.effective_exec_path)
                if c_layer is not None and await c_layer.is_session_alive(c_sid):
                    reuse_sid, reuse_layer = c_sid, c_layer
                    reuse_note = f", detached pre-warm {c_age:.1f}s old"
                elif c_layer is not None:
                    # Claimed a dead one — close it on its own (local)
                    # layer so its slot/subscription accounting frees.
                    with contextlib.suppress(Exception):
                        await c_layer.close_session(c_sid)
        if reuse_sid is not None:
            # A pre-warm runs on the platform: the chat is pinned there. The
            # resolver's reason rides along when it named the platform too
            # (a session below manager on an admin-targeted agent runs
            # locally, and the badge says so only when the frame carries it).
            from ws import dashboard_warmup
            try:
                resolved, reuse_reason = await dashboard_warmup.run_db(
                    remote_status.resolve_execution_target,
                    w.w_agent, self.user_sub, w.w_role,
                )
            except psycopg.Error:
                resolved, reuse_reason = placement.LOCAL, None
            if w.w_place or not placement.is_local(resolved):
                reuse_reason = None
            reuse_target = placement.LOCAL
            run.res = _SpawnResult(
                session_id=reuse_sid, layer=cast("ExecutionLayer", reuse_layer),
                execution_target=reuse_target, fallback_reason=reuse_reason,
            )
            await chat_writer.submit(
                w.wcid,
                functools.partial(task_store.update_chat, w.wcid, session_id=reuse_sid,
                                  execution_target=reuse_target),
                label="session_bind",
            )
            logger.info(
                "WS dashboard warmup (pre-warmed reuse): session=%s, "
                "chat=%s, agent=%s, model=%s"
                "%s", reuse_sid[:8], w.wcid, w.w_agent, w.chat_model, reuse_note
            )
            if not (self.deferred_mode and self.deferred_for in ("", w.wcid)):
                await self._give_reused_session_the_chats_mode(
                    reuse_layer, reuse_sid, w.permission_mode,
                )
        else:
            new_sid = str(uuid.uuid4())
            run.res = await self._create_or_resume_session(
                new_sid, w.w_agent, w.permission_mode, resume=False,
                model=w.chat_model, exec_path=w.effective_exec_path,
                chat_id=w.wcid, pinned_target=w.w_place, adopt=False,
                chat_exec_mode=w.w_exec_mode, chat_theme=w.w_theme,
                first_prompt=run.argv_first_prompt,
                focus_line=w.msg.get("_focus_line", ""),
            )
            await chat_writer.submit(
                w.wcid,
                functools.partial(task_store.update_chat, w.wcid, session_id=new_sid),
                label="session_bind",
            )
            logger.info(
                "WS dashboard warmup (new): session=%s, "
                "chat=%s, agent=%s, model=%s",
                new_sid, w.wcid, w.w_agent, w.chat_model
            )

    async def _tail_abort_check(self, w: _TailArgs, run: _TailRun) -> None:
        """A Stop that landed during the spawn: the session torn down and
        ``aborted`` set, so no ready frame and no first-turn kick follow."""
        # Abort-during-spawn: the user hit Stop while this chat was
        # warming. Cancelling the spawn task can't reliably stop a
        # half-started CLI/satellite process, so we let the spawn finish
        # then tear it down here: kill the just-spawned session (kept
        # resumable) and suppress BOTH warmup_ready and the first-turn
        # kick — the client already received `aborted`. The `finally`
        # below still unregisters the warmup.
        if self._warmup_abort_chat == w.wcid and run.res is not None:
            self._warmup_abort_chat = None
            run.aborted = True
            self._drop_warmup_mark(w.wcid)
            try:
                await run.res.layer.abort(run.res.session_id)
            except Exception:  # noqa: BLE001  # an abort during teardown: the chat is being released anyway
                logger.warning("abort-during-spawn teardown failed for chat=%s", w.wcid)
            # Deliberately NOT stamping last_abort_graceful: this abort
            # loses no turn (the first-turn kick was suppressed), so the
            # flag-pair keeps describing the PREVIOUS abort — a graceful
            # one kept its partial turn in the CLI history and must
            # still skip the cancelled-context injection. The stamp lands
            # before the queue is released below.
            await chat_writer.submit(w.wcid, functools.partial(_stamp_abort, w.wcid),
                                     label="abort_flags")
            # No first turn: what a teammate sent meanwhile goes out on
            # its own once the entry below is dropped.
            input_queue.first_turn_not_run(w.wcid)
            return

    async def _tail_adopt(self, w: _TailArgs, run: _TailRun) -> None:
        """The spawned session: the seed stashed, the viewed state adopted when
        the socket is still on the chat, the mode reconciled, ``warmup_ready``
        emitted."""
        res = cast("_SpawnResult", run.res)  # the spawn phase set it
        from ws import dashboard_warmup
        # stash the restored-history digest on the interactive session
        # BEFORE its readiness flush (this runs synchronously, ahead of the
        # ≥0.8s settle), so the first prompt is prepended with the prior
        # conversation (set_pending_seed → _emit_to_pty bracketed paste). A
        # -p FALLBACK (not res.interactive) prepends to its kick text below.
        if res.interactive and run.seed_digest:
            isess = interactive_session.get(res.session_id)
            if isess is not None:
                isess.set_pending_seed(run.seed_digest)

        # Adopt into the connection's VIEWED state ONLY if the socket is
        # still on the warmed chat. A chat-switch during the spawn means
        # the attributes now describe a DIFFERENT chat — adopting would
        # clobber it. The DB + registry already hold the truth either way.
        run.still_viewing = (self.chat_id == w.wcid)
        if run.still_viewing:
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
        eff_mode = get_session_mode(res.session_id) or w.permission_mode
        if eff_mode != w.permission_mode:
            await chat_writer.submit(
                w.wcid,
                functools.partial(task_store.update_chat, w.wcid, permission_mode=eff_mode),
                label="mode_reconcile",
            )

        # When a user per-agent override is offline and we soft-fell-back,
        # surface the offline machine's name for the "machine X offline —
        # using default" banner.
        offline_machine_name = ""
        if res.fallback_reason == "user-override-offline":
            offline_machine_name = await dashboard_warmup.run_db(
                _override_machine_name, self.user_sub, w.w_agent)
        _mismatch = await dashboard_warmup.run_db(self._target_mismatch_fields, w.wcid)
        await warmup_registry.emit(w.wcid, {
            "type": wire.WARMUP_READY,
            "session_id": res.session_id,
            "chat_id": w.wcid,
            "mode": eff_mode,
            "model": w.chat_model,
            "execution_path": w.effective_exec_path,
            "execution_target": res.execution_target,
            "fallback_reason": res.fallback_reason,
            "offline_machine_name": offline_machine_name,
            "interactive": res.interactive,
            **_mismatch,
        })
        # surface the "restored from history" card live (also persisted
        # to chat_messages by the digest claim, so it renders on reload).
        if run.seed_notice and run.still_viewing:
            await self._send({
                "type": wire.SYSTEM,
                "subtype": wire.SUBTYPE_SESSION_RESEEDED,
                "message": run.seed_notice,
                "chat_id": w.wcid,
            })
        # Interactive: the CLIENT attaches the PTY viewer itself via a
        # `pty_attach` frame once its terminal has mounted + subscribed —
        # so the scrollback replay can't race the subscribe (blank-terminal
        # bug). warmup_ready{interactive:true} above is the signal; the
        # connection's session_id is already adopted. No server-kick (the
        # human drives the PTY) — handled below.

    async def _tail_failed(self, w: _TailArgs, run: _TailRun, kind: str, e: Exception) -> None:
        """A spawn that raised: the claimed seed restored and ``warmup_failed``
        emitted with the reason for ``kind`` ("subscription": no usable
        credentials; "undecided": the resume probe got no answer; "spawn")."""
        if kind == "subscription":
            # Expected + actionable: this user has no usable credentials for the
            # layer (no own sub, and either Platform Auth off or only un-borrowable
            # admin OAuth in the pool). Surface the friendly message + a
            # machine-readable reason — it's a configuration state, not a crash.
            reason = cast("subscription_pool.NoSubscriptionError", e).reason
            logger.info("WS dashboard warmup blocked (no subscription, reason=%s) chat=%s", reason, w.wcid)
            if run.seed_reason_claimed:
                await _restore_seed(w.wcid, run.seed_reason_claimed)
            await warmup_registry.emit(w.wcid, {
                "type": wire.WARMUP_FAILED,
                "chat_id": w.wcid,
                "error": str(e),
                "reason": reason,
            })
        elif kind == "undecided":
            # The chat's machine did not answer the resume probe within
            # its budget (or this server's disk could not read the
            # session files twice): nothing of the chat changes (its
            # session id and pin stand, no digest was claimed), the
            # person's message stays above undelivered and the card says
            # so. The next send asks again.
            logger.info("WS dashboard warmup undecided chat=%s: %s", w.wcid, e)
            if run.seed_reason_claimed:
                await _restore_seed(w.wcid, run.seed_reason_claimed, best_effort=True)
            await warmup_registry.emit(w.wcid, {
                "type": wire.WARMUP_FAILED,
                "chat_id": w.wcid,
                "error": (SESSION_FILES_UNREAD_NOT_SENT if cast("ResumeUndecided", e).local
                          else MACHINE_BUSY_NOT_SENT),
                "reason": wire.REASON_TARGET_UNAVAILABLE,
            })
        else:
            # Failed spawn — un-burn the claimed history seed so the next
            # (successful) warmup still restores context. The claim already
            # persisted its "Continued with a fresh session" card; a rare
            # second card on the retry is accepted (two restart attempts).
            if run.seed_reason_claimed:
                await _restore_seed(w.wcid, run.seed_reason_claimed, best_effort=True)
            # Classify so the UI distinguishes a genuinely-offline/unreachable
            # remote target ("Remote machine unavailable") from a session that
            # FAILED TO START on a reachable machine (a config/spawn error,
            # e.g. a bad config.toml) — the latter is not an availability
            # problem, so the "machine unavailable" title misled. The
            # connection layer raises "...not connected" / "...command timeout"
            # for true unavailability; a satellite that ran the command and
            # returned an error surfaces as "Satellite command error: ...".
            emsg = str(e)
            if isinstance(e, AgentStateRefused):
                fail_reason = wire.REASON_BELOW_EDITOR
            else:
                fail_reason = (
                    "target_unavailable"
                    if ("not connected" in emsg or "command timeout" in emsg)
                    else "session_error"
                )
            await warmup_registry.emit(w.wcid, {
                "type": wire.WARMUP_FAILED,
                "chat_id": w.wcid,
                "error": emsg,
                "reason": fail_reason,
            })

    async def _tail_first_turn(self, w: _TailArgs, run: _TailRun) -> None:
        """After the spawn: the deferred mode/model re-applied, then the first
        turn, submitted into the PTY, kicked on the main loop, or run headless
        when the socket is gone."""
        res = cast("_SpawnResult", run.res)  # the spawn phase set it
        # Re-apply any deferred mode/model the user set during the spawn —
        # only if they're still on this chat (else the deferred value
        # belongs to the chat they switched to, handled by that chat's flow).
        if run.still_viewing:
            await self._reapply_deferred_after_warmup(self.chat_id or "", from_warmup=True)
        # Interactive: there is NO server-kicked pump turn — the human drives
        # the PTY. A cold send's text rides the warmup and is delivered HERE,
        # server-side (submit_prompt buffers until the TUI is ready and arms
        # the deferred Enter), so the prompt survives the client switching
        # chats / reloading mid-warmup. Codex fresh spawns delivered it via
        # the launch argv instead (auto-runs) — skip those (double-send).
        if res.interactive:
            first_text = w.msg.get("text", "")
            if first_text and not res.first_prompt_in_argv:
                isess = interactive_session.get(res.session_id)
                if isess is not None and isess.alive:
                    # Stamp the time and the viewer-focus line at delivery
                    # (parity with the -p pump's injection; the client
                    # sends the text RAW so a declined-to-headless kick
                    # can't double-stamp).
                    first_text = await self._with_pending_notes(w.wcid, first_text)
                    stamped = self._interactive_prelude(
                        first_text, session_id=res.session_id,
                        focus_line=w.msg.get("_focus_line", ""),
                    )
                    # Re-note the STAMPED bytes (replaces the raw note from
                    # _persist_first_prompt): the tailer's duplicate-skip
                    # matches the journaled user line and the CLI journals
                    # what the PTY received — without this the first user
                    # row lands twice. Any reseed digest rides the
                    # session's stashed seed (set_pending_seed above),
                    # prepended at the first submission — NOT here.
                    from core.session.transcript_tool_events import (
                        note_sent_prompt,
                    )
                    note_sent_prompt(w.wcid, stamped)
                    logger.info(
                        "WS dashboard: server-side first prompt submit "
                        "chat=%s session=%s",
                        w.wcid[:8], res.session_id[:8]
                    )
                    isess.submit_prompt(stamped)
            # What was sent while it got ready is typed after it.
            self._drop_warmup_mark(w.wcid)
            if input_queue.peek(w.wcid):
                input_queue.arm(w.wcid)
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
        kick_text = w.msg.get("text", "")
        if run.seed_digest and kick_text:
            kick_text = f"{run.seed_digest}\n\n{kick_text}"
        if not kick_text:
            # No first turn: what was sent while it got ready goes now.
            self._drop_warmup_mark(w.wcid)
            if input_queue.peek(w.wcid):
                input_queue.arm(w.wcid)
        elif self._ws_gone:
            self._drop_warmup_mark(w.wcid)
            await self._run_kick_headless(
                w.wcid, res.session_id, kick_text,
                w.msg.get("images", []), w.msg.get("files", []),
                force_headless=True,
            )
        else:
            # Breadcrumb: pairs with "server kick drained" /
            # "close-rescue" so a lost first turn is diagnosable. The
            # kick's drain clears ``_warmup_chat_id``.
            logger.info(
                "WS dashboard: server kick enqueued for chat=%s", w.wcid[:8]
            )
            self.notify_queue.put_nowait({
                "type": wire.NOTIFY_SERVER_KICK,
                "session_id": res.session_id,
                "chat_id": w.wcid,
                "text": kick_text,
                "images": w.msg.get("images", []),
                "files": w.msg.get("files", []),
                "focus_line": w.msg.get("_focus_line", ""),
            })
