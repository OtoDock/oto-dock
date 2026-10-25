"""One admitted task run (``runner._run_task_body``): the run's state and
its phases, from the running stamp to the delivered result, and the two
handlers' and the ``finally``'s bodies.

The phases import the platform's modules at call time, as the run body did
(the tests patch the defining modules before a run), and reach the runner's
own functions through the runner module, the patch target the tests use.
The runner imports this module at its own import, so the names bound here
at module level are bound when the runner's were.
"""

import asyncio
import contextlib
import functools
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, cast

import psycopg

import config
from core import placement
from core.background import spawn
from core.events import turn_ending
from core.execution_layer import ResumeVerdict
from core.session import session_kind
from core.session.resume_probe import RESUME_PROBE_BUDGET_BACKGROUND_S, settle_resume
from services.scheduler import delivery, interactive, lanes, runner, shared, task_kinds
from storage import database as task_store
from storage.agents import agent_store
from storage.automation import run_status
from storage.chat import meeting_status

if TYPE_CHECKING:
    from core.events.stream_pump import ChatStreamPump
    from core.execution_layer import AgentConfig, ExecutionLayer

logger = logging.getLogger("otodock.scheduler")


class TaskRun:
    """One admitted task run: its identity, its chat and session, the
    engine session it drives, and the facts its end is written from. The
    phases run in the order of ``admitted``; the two handlers and the
    ``finally`` of ``runner._run_task_body`` call ``leave_for_recovery``,
    ``cancelled``, ``failed`` and ``release``."""

    def __init__(self, run_id: str, session_id: str, task: shared.TaskDefinition, prompt: str,
                 trigger_type: str, trigger_source: str | None, attempt: int,
                 trigger_payload: dict | None, _ident, _slot_target: str) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.task = task
        self.prompt = prompt
        self.trigger_type = trigger_type
        self.trigger_source = trigger_source
        self.attempt = attempt
        self.trigger_payload = trigger_payload
        self._ident = _ident
        self._slot_target = _slot_target
        self.start = time.monotonic()
        self.chat_id = self.task.target_chat_id or session_kind.task_chat_id(self.run_id)
        self.output_cursor = 0  # pre-run message cursor; set in step 1
        self.prompt_row_id = 0  # the driven prompt's own row; excluded from collection
        self.layer: ExecutionLayer | None = None  # bound in step 2; guards the except cleanup if config build fails
        self.pump: ChatStreamPump | None = None  # bound in step 6 (headless only); step 7 reads last_error
        # Set when a graceful abort leaves the session warm (step 7): the
        # finally must then keep its session-index entry so the idle reaper
        # + /v1/session/current still see the live session.
        self.session_kept_warm = False
        # Set when the step-1 lane gate had to reap a wedged prior pump: the
        # round must then respawn, never reuse (see _settle_prior_lane).
        self.lane_reaped = False
        # Seat accounting for the except path: the config build acquires a
        # pool seat that only start_session's bind lets a close release. A
        # round that failed after the build but before the bind (offline
        # target, spawn failure) must give the seat back itself. Step 2
        # binds it and the layer; the steps after it cast their reads.
        self.agent_cfg: AgentConfig | None = None
        self.reused_warm = False
        # The chat's line about the task's machine giving way is written
        # once per run (a fresh continue round binds the layer twice).
        self.machine_noted = False
        self.session_started = False
        self.seat_released = False  # the offline-target branch returns it itself
        # A continue round that drives the lane's LIVE interactive terminal
        # instead of spawning (see step 1): the terminal is the person's, so
        # no close, no session-index pop; the chat's hold is released at end.
        self.borrowed = None
        self.terminal_hold: interactive.TerminalHold | None = None
        # The verdict is written once (_end_run). A cancel or a failure that
        # lands after it — during the delivery or the session close — is
        # logged and never stamped over it, pages nobody and retries
        # nothing; a delivery that raised after it is retried with the same
        # status and output the success path was delivering.
        self.row_ended = False
        self.final_status = ""
        self.final_output = ""
        # The turn's typed ending when it ended short (any of
        # turn_ending.REASONS): the run's error, the page and the callback name it.
        self.ending = None
        # The run's own end before the worker's report, a turn_ending word ("":
        # the turn finished): the callback's "ended without a report" reads it
        # (lanes.report_missing), never the answer's length.
        self.cut_short = ""
        # The worker's turn returned (the pump or the terminal): a failure
        # after it is the platform's, not a turn that stopped half way.
        self.turn_done = False
        # The worker chat's last row when the turn ended: a person's message
        # after it continued the lane (the report flag reads it).
        self.turn_end_cursor = 0
        # The run's start: a reused worker chat's verdicts before it belong
        # to earlier runs.
        self.run_started_at = shared.now_iso()
        # The run owes its report from here (lanes.report_pending): the keys
        # are taken now, since the body may move to another session id.
        self.report_keys = (self.chat_id, self.session_id)
        # Exactly-once result delivery per run: the success path sets this right
        # after its _deliver_task_result; the cancel/exception handlers then skip
        # THEIR delivery (a failure after the result went out — e.g. session close
        # raising — used to deliver the same result a second time).
        self.result_delivered = False
        # The facts the phases hand each other.
        self.user_stop = False
        self.platform_reason: str | None = None
        self.first_prompt_in_argv = False
        self.stamp_first_prompt = False
        self.engine_prompt = prompt
        self.task_total_cost = 0
        self.engine_ending = None
        self.limit_line: str | None = None
        self.run_error: str | None = None
        self.background_pending = 0

    async def admitted(self) -> None:
        """The run from its running stamp to its delivered result."""
        from core.events import chat_writer
        from storage.pg import run_db
        # The running stamp sits INSIDE the handlers' reach: a cancel
        # during its thread hop is this body's to stamp, not the frame's.
        await run_db(
            task_store.update_run, self.run_id,
            status=run_status.RUNNING,
            started_at=self.run_started_at,
        )
        await self._open_round()
        if self.borrowed is not None:
            # 2-6, borrowed: the follow-up goes INTO the terminal (steered
            # when its turn is open, pasted at quiescence otherwise) and
            # the round waits for that turn's end; the tailer persists
            # the turn, so the collection and delivery below stand. The
            # prompt row is the tailer's too, as for a fresh interactive
            # round.
            await interactive._run_borrowed_terminal_turn(self.borrowed, self.prompt)
        else:
            await self._spawn_and_drive()

        completed_meeting_id = await self._await_meeting()
        self.turn_done = True
        self.turn_end_cursor = await chat_writer.submit(
            self.chat_id, functools.partial(task_store.get_last_chat_message_id, self.chat_id),
            label="turn_end")
        await self._read_verdict(completed_meeting_id)
        await self._stamp_end()
        self._notify_end()
        await delivery._deliver_task_result(
            self.task, self.final_status, self.final_output,
            worker_chat_id=self.chat_id, output_cursor=self.output_cursor,
            prompt_row_id=self.prompt_row_id, prompt_text=self.prompt,
            run_started_at=self.run_started_at, ending=self.ending,
            run_id=self.run_id, background_pending=self.background_pending,
            cut_short=self.cut_short, turn_end_cursor=self.turn_end_cursor,
        )
        self.result_delivered = True

        # Interactive task: close the PTY session now the run is terminal
        # (frees the slot; the transcript is already persisted). Headless tasks
        # let their pump-session idle-reap. No-op for a non-interactive run.
        if self.borrowed is None:
            await interactive._close_interactive_task_session(self.session_id)

    async def _open_round(self) -> None:
        """Step 1: the chat row (a worker chat's, or the backstop), the
        lane's live terminal when the round drives it, the abort flags and
        the output cursor, the run's chat id."""
        from core.events import chat_writer
        from core.session import session_state as _state
        from storage.pg import run_db
        # 1. Persist the chat row + the user's prompt FIRST — BEFORE the slow
        #    spawn (config build / start_session / offline-target hard-fail) — so
        #    a pre-turn failure still shows the prompt and is visible in
        #    the run's chat instead of an empty one. General task robustness:
        #    applies to every task type (scheduled / one-time / delegate).
        if self.task.target_chat_id:
            # Worker chat (surface="chat") — the spawn path created the row
            # with owner/origin/parent stamped. A missing row means the
            # user deleted the chat between spawn and fire.
            if not await run_db(task_store.get_chat, self.chat_id):
                raise RuntimeError("The worker chat for this run no longer exists")
            # BEFORE the output cursor below: a reap flushes the prior
            # round's blocks, and those rows must not be collected as
            # this run's output. Healthy live turns are WAITED out, not
            # reaped (a reap cancels the user's in-flight turn).
            self.lane_reaped = await lanes._settle_prior_lane(self.chat_id, self.run_id)
            # A live interactive terminal on the lane IS the lane: the
            # round drives it instead of spawning a second CLI on its
            # session (a `--resume` beside a live TUI dual-writes the
            # transcript, and the round's close took the person's
            # terminal with it). Rounds that drive a chat's terminal
            # serialize on the chat's hold, taken BEFORE the cursor so
            # the round collects only its own turn.
            if runner._is_valid_session_uuid(self.task.continue_session):
                self.terminal_hold = await interactive.hold_terminal(self.chat_id)
                self.borrowed = interactive.borrow_terminal(self.chat_id)
                if self.borrowed is not None and not self.borrowed.may_drive(self.task.created_by):
                    # The terminal runs as whoever warmed it (their
                    # token, role, subscription): a round for someone
                    # else never types into it, nor resumes beside it.
                    self.borrowed = None
                    self.terminal_hold.release()
                    self.terminal_hold = None
                    raise RuntimeError(
                        "This chat's live terminal belongs to another person; "
                        "a delegated follow-up cannot drive it."
                    )
                if self.borrowed is None:
                    self.terminal_hold.release()
                    self.terminal_hold = None
                else:
                    run_sid, self.session_id = self.session_id, self.borrowed.session_id
                    # The frame stamped the run's id a task session; the
                    # terminal's entry is the dashboard's and stays so.
                    if run_sid != self.session_id:
                        _state._sessions.pop(run_sid, None)
                    entry = _state._sessions.get(self.session_id)
                    if entry is not None:
                        entry.pop("is_task", None)
                        entry.pop("check", None)
                    _state._save_sessions()
                    logger.info(
                        "Task %s: driving the live terminal "
                        "%s on lane chat %s (no spawn)",
                        self.run_id, self.session_id[:8], self.chat_id[:8]
                    )
        elif not await run_db(task_store.get_chat, self.chat_id):
            # Backstop: _execute_task pre-creates the row before the slot
            # (queued visibility); this covers direct _run_task callers
            # and a chat deleted while the run was parked.
            await run_db(runner._create_task_chat_row, self.chat_id, self.run_id, self.task)
        # Clear stale abort flags from a PRIOR round on this chat —
        # scheduler-driven turns don't run the dashboard's turn-start clear,
        # and the post-run user_interrupted check keys on these flags (a
        # stale graceful=True from a prior round must not suppress a
        # close after THIS round's non-graceful abort).
        # The flag clear and the cursor are ONE job on the chat's lane:
        # the cursor must see the chat's queued rows, and nothing may land
        # between the two. Cursor BEFORE this round's prompt lands: output
        # collection (and the failure-path collection) reports only what
        # THIS run produced.
        _checks = (json.dumps(list(self.task.checks))
                   if self.task.target_chat_id and self.task.checks else None)

        def _start_round(_sid=self.session_id, _checks=_checks) -> int:
            task_store.update_chat(
                self.chat_id, session_id=_sid,
                last_turn_aborted=False, last_abort_graceful=False,
                # A worker chat that already existed takes this run's
                # checks (CHECKS.md): the evaluator reads the chat row
                # for every kind of session.
                **({"checks": _checks} if _checks is not None else {}),
            )
            return task_store.get_last_chat_message_id(self.chat_id)

        self.output_cursor = await chat_writer.submit(self.chat_id, _start_round, label="run_start")
        # The user-prompt bubble is persisted AFTER the config build below, so
        # we can skip it for an interactive run (there the prompt rides the
        # PTY/argv and the transcript tailer backfills it — pre-persisting
        # would duplicate it). Config build is fast relative to start_session,
        # so the prompt still lands before the slow spawn.
        # chat_id on the run immediately so the dashboard can load it while running.
        await run_db(task_store.update_run, self.run_id, chat_id=self.chat_id, session_id=self.session_id)

    async def _spawn_and_drive(self) -> None:
        """Steps 2 to 6 for a round that spawns: the config, the layer, a
        continue's warm or resumed session, the start, the turn."""
        await self._build_config()
        await self._bind_layer()
        if runner._is_valid_session_uuid(self.task.continue_session):
            await self._continue_round()
        await self._prepare_interactive()
        await self._persist_prompt_row()
        await self._start_session()
        await self._drive_turn()

    async def _build_config(self) -> None:
        """The task's agent config; an offline target ends the run."""
        from core.config.task_config_builder import build_task_agent_config
        # 2. Build config via task_config_builder (handles scope creds,
        #    security context, task suffix, env vars). trigger_payload
        #    threads through so manifest agent_context blocks resolve
        #    ${trigger.*} tokens for webhook-fired tasks.
        self.agent_cfg = await build_task_agent_config(
            self.task.agent, self.task, self.session_id,
            trigger_payload=self.trigger_payload,
        )

        # Hard-fail if the resolved target is the offline sentinel from
        # the resolver. Without this, agent-level remote targets silently
        # fall back to local CLI when the satellite is unreachable, which
        # masks misconfigurations and produces wrong results (different
        # MCPs, different filesystem). Mirrors ws/dashboard.py warmup.
        from core.config.config_builder import release_config_seat
        from storage import remote_store
        if placement.is_offline_sentinel(self.agent_cfg.execution_target):
            # Every branch below raises — the build's seat goes back now,
            # once: the except's release is skipped for it.
            release_config_seat(self.session_id, self.agent_cfg)
            self.seat_released = True
            offline_machine_id = placement.offline_machine_of(self.agent_cfg.execution_target)
            machine = remote_store.get_remote_machine(offline_machine_id)
            if not machine:
                # Deleted mid-flight (the delete's bulk chat transition
                # races this run). The row is already fixed — the next
                # run fresh-resolves and auto-continues.
                raise RuntimeError(
                    "This task's remote machine no longer exists — "
                    "the next run will use the agent's current target."
                )
            machine_label = machine.get("name") or offline_machine_id[:8]
            is_admin_target = placement.machine_is_admin_paired(machine)
            if is_admin_target:
                raise RuntimeError(
                    f"This agent's remote machine '{machine_label}' is currently offline. "
                    f"Please reconnect the remote machine or contact your admin."
                )
            raise RuntimeError(
                f"Your remote machine '{machine_label}' is offline. "
                f"Reconnect it from User Settings → Remote Machines, or remove the per-agent override."
            )

    async def _bind_layer(self) -> None:
        """The execution layer of the config's own target and path, the
        chat pinned to that target, the session's chat ids."""
        from core.session.session_manager import get_execution_layer
        from storage.pg import run_db
        # Resolve the execution layer from the config's already-resolved
        # target + path so the layer can never disagree with the config
        # (the divergence that silently ran user-scoped tasks on local).
        self.layer = get_execution_layer(
            self.task.agent,
            execution_path=cast("AgentConfig", self.agent_cfg).execution_path,
            user_sub=self._ident.creds_user_sub,
            role=self._ident.role,
            execution_target=cast("AgentConfig", self.agent_cfg).execution_target,
        )

        # Pin the task chat to the target it actually runs on
        # — same affinity as dashboard chats (ws/dashboard.py warmup pin).
        # Makes a task run's continues resume on the origin machine, and
        # lets delete_remote_machine transition task chats to
        # auto-continue like any other chat.
        await run_db(
            task_store.update_chat, self.chat_id,
            execution_target=cast("AgentConfig", self.agent_cfg).execution_target,
        )
        await self._record_target()

        # Pass chat_id to meetings-mcp so it uses the task's chat
        # (/v1/session/current excludes task sessions, returning the
        # wrong chat_id for meetings started from tasks).
        cast("AgentConfig", self.agent_cfg).extra_env["MEETINGS_MCP_CHAT_ID"] = self.chat_id
        cast("AgentConfig", self.agent_cfg).extra_env["MEETINGS_MCP_SESSION_ID"] = self.session_id
        cast("AgentConfig", self.agent_cfg).extra_env["NOTIF_MCP_CHAT_ID"] = self.chat_id

        # For continue_session (multi-turn delegation), prefer driving the
        # turn on the still-warm session; otherwise the session has prior
        # conversation on disk — use --resume to preserve context.
        # Same UUID guard as _execute_task: bad data falls back to fresh
        # session rather than passing "--resume false" to the CLI.
        self.reused_warm = False

    async def _record_target(self) -> None:
        """The run's record of where it ran (``task_runs.execution_target``)
        and, once per run, the chat's line when the task's own machine gave
        way to the default (``task_config_builder.task_target``)."""
        from core.config import task_config_builder
        from core.events import chat_writer
        from core.session.session_state import broadcast_chat_frame
        from storage.pg import run_db
        from ws import wire_events as wire
        cfg = cast("AgentConfig", self.agent_cfg)
        target = placement.machine_of(cfg.execution_target) or placement.LOCAL
        await run_db(task_store.update_run, self.run_id, execution_target=target)
        if (getattr(cfg, "fallback_reason", None) != task_config_builder.FALLBACK_TASK_MACHINE
                or self.machine_noted):
            return
        self.machine_noted = True
        note = await run_db(task_config_builder.task_machine_note, self.task, self._ident, target)
        # The amber machine card (SystemEvent.tsx), persisted so a reload
        # shows it where the run started.
        block = {"type": wire.SYSTEM, "subtype": wire.SUBTYPE_MACHINE_NOT_ANSWERING,
                 "message": note}
        await chat_writer.submit(self.chat_id, functools.partial(
            task_store.add_chat_message, self.chat_id, "event", "",
            event_type=wire.SYSTEM, event_data=json.dumps(block)), label="task_machine_fallback")
        broadcast_chat_frame(self.chat_id, {**block, "chat_id": self.chat_id})

    async def _continue_round(self) -> None:
        """A continue round: the lane quiet, a model change's close, the
        warm session reused, else a resume checked (or a fresh session)."""
        from core.config.config_builder import release_config_seat
        from core.events import chat_writer
        from core.events.stream_pump import _active_pumps
        from core.session import session_state as _state
        from storage.pg import run_db
        # Final lane gate at the decision point: the config build did
        # DB/thread work since the step-1 gate — a user turn may have
        # started on the lane meanwhile. A quiet observation here
        # means the respawn path can't hit the satellite stale-live
        # guard mid-turn and the pump registration won't clobber a
        # live dashboard pump (post-gate races on the reuse path are
        # serialized by layer.session_lock).
        _prior = _active_pumps.get(self.chat_id)
        if _prior is not None and not _prior.is_done:
            await lanes._await_lane_quiescence(self.chat_id)
            # The wait may have let a user turn land rows/flags —
            # re-take the output cursor + re-clear the abort flags so
            # that turn is neither collected as this run's output nor
            # mis-promoted to user_interrupted.
            def _reclear() -> int:
                task_store.update_chat(
                    self.chat_id,
                    last_turn_aborted=False, last_abort_graceful=False,
                )
                return task_store.get_last_chat_message_id(self.chat_id)

            self.output_cursor = await chat_writer.submit(
                self.chat_id, _reclear, label="run_reclear")
        if self.task.respawn_for_model:
            # The continue changed the worker's model: the live
            # session runs the old one, so it goes (under its
            # layer's session lock) and the round resumes the
            # conversation on agent_cfg.model — never warm.
            await lanes._close_for_model_change(
                self.session_id, self.run_id, slot_target=self._slot_target,
            )
        elif not self.lane_reaped:
            self.reused_warm = await lanes._try_reuse_warm_session(
                self.layer, self.agent_cfg, self.session_id, self.run_id,
            )
        if self.reused_warm:
            # No spawn: the turn rides the warm session (dashboard
            # turn-2+ semantics; spawn-time binding/context stand).
            # Release the seat this round's config build acquired —
            # nothing will bind it — and force headless: the
            # force-headless below keys on agent_cfg.resume, which
            # reuse never sets.
            cast("AgentConfig", self.agent_cfg).interactive = False
            # keep_binding: the id is the LIVE warm session's, bound
            # by the round that spawned it — only this round's seat
            # (and its acquire-window claim) goes back.
            release_config_seat(self.session_id, self.agent_cfg, keep_binding=True)
        else:
            # Resumability pre-check (same gate as dashboard warmups
            # and the one-shot delivery): a blind --resume on a
            # missing conversation makes the CLI's "No conversation
            # found with session ID: …" the run's entire output. Fall
            # back to a fresh session in the SAME chat instead — a
            # fresh id, so a half-present file under the old one
            # can't collide.
            resume_username = ""
            if runner._resume_username_needed(self.task):
                resume_username = await run_db(
                    task_store.get_username_by_sub, self.task.created_by) or ""
            verdict = ResumeVerdict.RESUMABLE
            if not runner._rebuilds_from_rows(self.agent_cfg):
                # (an engine that rebuilds its conversation from
                # its rounds' rows resumes under the same id; the
                # round's prompt row is skipped by id at the start
                # below)
                verdict = await settle_resume(
                    cast("ExecutionLayer", self.layer), self.session_id, agent_name=self.task.agent,
                    username=resume_username,
                    budget_s=RESUME_PROBE_BUDGET_BACKGROUND_S,
                )
            if verdict is ResumeVerdict.UNKNOWN:
                # The machine did not answer within the budget: a
                # fresh session here would drop the lane's
                # context for good, so the round fails and the
                # next continue asks again.
                raise RuntimeError(
                    "The machine running this chat did not answer in "
                    "time; the round was not started"
                )
            if verdict is ResumeVerdict.RESUMABLE:
                cast("AgentConfig", self.agent_cfg).resume = True
            else:
                old_sid = self.session_id
                self.session_id = str(uuid.uuid4())
                _state._sessions[self.session_id] = _state._sessions.pop(
                    old_sid, {"created": True, "message_count": 0},
                )
                # The dead session's binding would outlive it (nothing
                # else releases a seat under an id no session runs), and
                # the config built for it names it: its MCP config, its
                # OTO_SESSION_ID, the meetings MCP's session. The seat
                # goes back and the config is built again under the new
                # id; a full pool can give the seat away meanwhile, and
                # the round then fails as any start without one.
                release_config_seat(old_sid, self.agent_cfg)
                self.seat_released = True
                await run_db(task_store.update_chat, self.chat_id, session_id=self.session_id)
                await run_db(task_store.update_run, self.run_id, session_id=self.session_id)
                logger.warning(
                    "Delegate continue: session %s has no resumable "
                    "conversation on agent=%s — fresh session "
                    "%s in the same chat",
                    old_sid[:8], self.task.agent, self.session_id[:8]
                )
                await self._build_config()
                self.seat_released = False
                await self._bind_layer()

    async def _prepare_interactive(self) -> None:
        """The engine prompt and, for an interactive run, the terminal
        hold and the first prompt's way in."""
        from core.session import session_state as _state
        from storage.pg import run_db
        # Interactive task prep. Only FRESH interactive spawns run
        # interactive — a continue_session RESUME stays headless
        # (resuming would also make the tailer replay prior turn-end
        # signals). The frontend resolves the terminal on re-open from the
        # chat's stored execution_mode, so pin it.
        self.first_prompt_in_argv = False
        if cast("AgentConfig", self.agent_cfg).interactive and cast("AgentConfig", self.agent_cfg).resume:
            cast("AgentConfig", self.agent_cfg).interactive = False
        # The zone the run's [Current time] line speaks: the one the
        # task was made in, else its creator's browser, else the
        # platform's (every reader's fallback) — on the final id.
        run_tz = self.task.user_tz or _state.get_user_tz(self.task.created_by or "")
        if run_tz:
            _state.set_session_user_tz(self.session_id, run_tz)
        # A run's first prompt carries the time and parallel-activity
        # lines a dashboard turn carries: a headless engine stamps them
        # at its own turn site, an interactive one is handed them here.
        # A check's judge judges the work and gets neither.
        self.stamp_first_prompt = not task_kinds.of(self.task).judge
        self.engine_prompt = self.prompt
        if cast("AgentConfig", self.agent_cfg).interactive:
            # The chat's terminal hold, BEFORE the spawn: a continue
            # round arriving now waits for this round and its close
            # instead of borrowing the terminal being spawned.
            self.terminal_hold = await interactive.hold_terminal(self.chat_id)
            # The interactive session is keyed to the run chat (the codex/cli
            # layers pass chat_id=config.chat_id to interactive_session.register).
            # task_config_builder doesn't set it (the headless pump gets chat_id
            # directly), so set it here — without it the transcript tailer
            # short-circuits on an empty chat_id and the completion watcher never
            # fires (the run hangs) + nothing persists.
            cast("AgentConfig", self.agent_cfg).chat_id = self.chat_id
            await run_db(task_store.update_chat, self.chat_id, execution_mode="interactive")
            if self.stamp_first_prompt:
                self.engine_prompt = await runner._interactive_prelude(
                    self.prompt, run_tz, self.session_id)
            # An engine whose TUI takes the cold prompt as a launch argument
            # (Codex auto-runs it after MCP warm) gets it there; the others
            # get it via the PTY flush in the watcher.
            from core.session.session_manager import get_layer_capabilities
            _rc = get_layer_capabilities(cast("AgentConfig", self.agent_cfg).execution_path or "")
            if _rc is not None and _rc.runtime.interactive_first_prompt_via_argv:
                cast("AgentConfig", self.agent_cfg).interactive_first_prompt = self.engine_prompt
                self.first_prompt_in_argv = True

    async def _persist_prompt_row(self) -> None:
        """The prompt's row in the chat (an interactive run's comes from
        its transcript)."""
        from core.events import chat_writer
        # Persist the user-prompt bubble for the message-list view — but NOT
        # for interactive runs (the tailer backfills it from the transcript,
        # like an interactive chat; pre-persisting would duplicate it).
        if not cast("AgentConfig", self.agent_cfg).interactive:
            delegating_slug = self.task.on_complete_agent
            if delegating_slug:
                delegating_data = agent_store.get_agent(delegating_slug)
                prompt_agent_meta = {
                    "agent_slug": delegating_slug,
                    "agent_display_name": delegating_data["display_name"] if delegating_data else delegating_slug,
                    "agent_color": delegating_data.get("color", "") if delegating_data else "",
                    "badge": "delegated by",
                }
            else:
                executing_data = agent_store.get_agent(self.task.agent)
                prompt_agent_meta = {
                    "agent_slug": self.task.agent,
                    "agent_display_name": executing_data["display_name"] if executing_data else self.task.agent,
                    "agent_color": executing_data.get("color", "") if executing_data else "",
                    "badge": "task prompt",
                }
            # On the chat's lane, behind the round's start (its rows
            # must land in order; the search row rebuild runs off the
            # loop with it).
            self.prompt_row_id = await chat_writer.submit(
                self.chat_id,
                functools.partial(task_store.add_chat_message, self.chat_id, "user", self.prompt,
                                  event_data=json.dumps(prompt_agent_meta)),
                label="task_prompt",
            )

    async def _start_session(self) -> None:
        """Step 3 and 4: the session started (unless reused) and marked a
        task session."""
        from core.session import session_state as _state
        # 3. Start session via execution layer (skipped when the round
        #    rides an already-warm session — see _try_reuse_warm_session)
        if not self.reused_warm:
            if cast("AgentConfig", self.agent_cfg).resume and self.prompt_row_id and runner._rebuilds_from_rows(cast("AgentConfig", self.agent_cfg)):
                cast("AgentConfig", self.agent_cfg).extra_env["_RESUME_SKIP_ROW"] = str(self.prompt_row_id)
            await cast("ExecutionLayer", self.layer).start_session(self.session_id, cast("AgentConfig", self.agent_cfg))
        self.session_started = True

        # 4. Mark as task session so /v1/session/current excludes it.
        _state._sessions.setdefault(self.session_id, {"created": True, "message_count": 0})
        _state._sessions[self.session_id].update({
            "is_task": True,
            "client_type": session_kind.TASK.name,
            "agent": self.task.agent,
            "last_active": shared.now_iso(),
            **({"check": True} if task_kinds.of(self.task).judge else {}),
        })
        _state._save_sessions()

    async def _drive_turn(self) -> None:
        """Steps 5 and 6: the turn driven to its end."""
        from core.events.stream_pump import ChatStreamPump, _active_pumps
        from core.events.task_producer import task_produce
        from core.session.session_state import get_permission_queue
        # 5/6. Drive the turn to completion.
        if cast("AgentConfig", self.agent_cfg).interactive:
            # Interactive task: NO pump/producer — a PTY emits bytes, not
            # CommonEvents. Inject the first prompt (Claude via PTY;
            # Codex rode the launch argv) and wait for the turn-end signal:
            # the transcript/rollout tailer fires interactive_session's
            # on_turn_complete (gated bg-empty + min-turn-time). The tailer
            # also persists the turns to chat_messages, so the shared
            # output-collection + update_run + delivery below Just Work.
            await interactive._run_interactive_task(
                self.session_id, self.chat_id, self.engine_prompt, self.first_prompt_in_argv,
            )
        else:
            # 5. Create event queue + launch producer
            event_queue: asyncio.Queue = asyncio.Queue()
            # The task's timeout bounds how long the run waits for the
            # background work its turn left running (commands and
            # subagents alike), never below today's floors and never past
            # the per-turn ceiling the stall watchdog reaps at.
            bg_wait = float(min(max(self.task.timeout_seconds or 600, 600),
                                config.CLAUDE_TIMEOUT))
            producer = asyncio.create_task(
                task_produce(
                    cast("ExecutionLayer", self.layer), self.session_id, self.prompt, event_queue, self.run_id,
                    broadcast_fn=runner._broadcast, settle_timeout=30.0,
                    bg_cmd_ceiling=bg_wait, bg_sub_ceiling=max(bg_wait, 1800.0),
                    inject_time=self.stamp_first_prompt,
                )
            )

            # 6. Create pump, register in _active_pumps, and run to completion.
            # Registration allows dashboard WS to attach for live streaming.
            perm_queue = get_permission_queue(self.session_id)
            self.pump = ChatStreamPump(
                chat_id=self.chat_id,
                session_id=self.session_id,
                producer=producer,
                event_queue=event_queue,
                perm_queue=perm_queue,
                scope=self.task.scope,
                source_type=session_kind.TASK.source_type,
            )
            _active_pumps[self.chat_id] = self.pump
            self.pump.start()
            # Run to completion under the stall watchdog (reaps a wedged
            # turn instead of holding the run "generating" forever).
            await lanes._watch_task_pump(
                self.layer, self.pump, self.run_id, self.chat_id, self.session_id,
                on_parked=lambda: lanes.tell_run_parked(self.task, self.chat_id))

    async def _await_meeting(self) -> str | None:
        """Step 6b: a meeting the agent started, waited out (ten minutes
        at most). Answers its id."""
        # 6b. If the agent started a meeting, wait for it to conclude.
        # The meeting pump starts after the task pump clears (orchestrator
        # checks is_done). We poll the DB for the meeting status.
        active_meeting = await asyncio.to_thread(
            task_store.get_active_meeting_for_chat, self.chat_id
        )
        completed_meeting_id: str | None = None
        if active_meeting:
            completed_meeting_id = active_meeting["id"]
            logger.info("Task %s: waiting for meeting %s to conclude", self.run_id, completed_meeting_id)
            meeting_wait_start = time.monotonic()
            while True:
                await asyncio.sleep(2)
                m = await asyncio.to_thread(task_store.get_meeting, completed_meeting_id)
                if not m or meeting_status.is_terminal(m["status"]):
                    break
                if time.monotonic() - meeting_wait_start > 600:
                    logger.warning("Task %s: meeting %s wait timed out after 10min", self.run_id, completed_meeting_id)
                    break
            logger.info("Task %s: meeting %s finished", self.run_id, completed_meeting_id)
        return completed_meeting_id

    async def _read_verdict(self, completed_meeting_id: str | None) -> None:
        """Step 7 and 8: the chat's cost and abort state, the output, and
        the run's verdict from the turn's ending, a usage limit, an engine
        error or a failed check."""
        from storage.pg import run_db
        # 7. Read cost from chat record (pump saved it)
        chat_row = await run_db(task_store.get_chat, self.chat_id)
        self.task_total_cost = (chat_row or {}).get("total_cost", 0) or 0
        # A user watching the lane pressed Stop mid-turn (WS abort set the
        # flag; step 1 cleared any stale one). The run itself completed —
        # partial output is persisted — but the delegating agent must see
        # "user_interrupted", not a clean completion.
        self.final_status, self.session_kept_warm = lanes._post_run_session_action(chat_row)
        await self._close_after_interrupt()
        await self._collect_output(completed_meeting_id)
        self._engine_failure()
        await self._check_report()

    async def _close_after_interrupt(self) -> None:
        """A Stop mid-turn: the session closed unless it was kept warm."""
        if self.final_status == run_status.USER_INTERRUPTED and self.layer is not None:
            if self.session_kept_warm:
                logger.info(
                    "Task %s: graceful abort — keeping session "
                    "%s warm on lane chat %s",
                    self.run_id, self.session_id[:8], self.chat_id[:8]
                )
            else:
                try:
                    await self.layer.close_session(self.session_id)
                except Exception:  # noqa: BLE001  # closing an aborted run's session
                    logger.debug(
                        "Task %s: post-abort close_session failed", self.run_id,
                        exc_info=True,
                    )

    async def _collect_output(self, completed_meeting_id: str | None) -> None:
        """The output: a meeting's last summary, else the run's own rows."""
        # 8. Collect output text for the on-complete delivery payload.
        # For meetings: use the moderator's final summary (not the entire
        # transcript which would overwhelm the parent with redundant text).
        if completed_meeting_id:
            meeting_data = await asyncio.to_thread(task_store.get_meeting, completed_meeting_id)
            turns = await asyncio.to_thread(task_store.get_meeting_turns, completed_meeting_id)
            moderator = (meeting_data or {}).get("moderator", "")
            mod_turns = [t for t in (turns or []) if t.get("agent") == moderator and t.get("content")]
            self.final_output = mod_turns[-1]["content"] if mod_turns else lanes._collect_task_output(self.chat_id, self.output_cursor)
        else:
            self.final_output = lanes._collect_task_output(self.chat_id, self.output_cursor)

    def _engine_failure(self) -> None:
        """The turn's typed ending, a usage-limit notice and an engine error
        on the verdict."""
        # A turn that ended short (a decline, a limit, an error, an exit,
        # a silence, a loss) carries its typed ending on the pump: the run
        # failed for that reason, whatever an abort flag on the chat says.
        # A ``stopped`` ending is the person's Stop (a review turn or a
        # queued message met the process the abort ended): the run reads
        # as interrupted, never as failed, and the ending only reaches the
        # delegating agent's callback as its note.
        self.ending = getattr(self.pump, "last_ending", None)
        self.engine_ending = (self.ending if self.ending is not None
                         and self.ending.reason != turn_ending.STOPPED else None)
        if (self.ending is not None and self.engine_ending is None
                and self.final_status == run_status.COMPLETED):
            self.final_status = run_status.USER_INTERRUPTED
        if self.engine_ending is not None and self.final_status in (
                run_status.COMPLETED, run_status.USER_INTERRUPTED):
            self.final_status = run_status.FAILED
            logger.warning("Task run %s ended by the engine: %s", self.run_id, self.engine_ending.summary())

        # A run killed by a provider usage limit streams the limit notice
        # as a clean result text — without this check it lands as a
        # deceptive `completed` with the notice as output (an interactive
        # run has no pump, so no typed ending).
        self.limit_line = (
            lanes._limit_notice(self.final_output) if self.final_status == run_status.COMPLETED else None
        )
        if self.limit_line:
            self.final_status = run_status.FAILED
            logger.warning(
                "Task run %s stopped on a provider usage limit: %s",
                self.run_id, self.limit_line
            )

        # A run whose stream DIED on an engine/provider error (claude
        # is_error result — e.g. "Not logged in"; codex turn failure —
        # e.g. a cross-engine model 400) used to land as a deceptive
        # `completed` with empty (or error-text) output and $0 cost.
        # The pump breaks its loop on the ERROR event and records the
        # message — terminal by construction, so consult it exactly like
        # the limit notice. Empty output alone NEVER fails a run.
        self.run_error = None
        if self.final_status == run_status.COMPLETED and not self.limit_line:
            self.run_error = (getattr(self.pump, "last_error", "") or "").strip() or None
            if self.run_error:
                self.final_status = run_status.FAILED
                logger.warning(
                    "Task run %s failed on an engine error: "
                    "%s", self.run_id, self.run_error[:300]
                )
        # The engine's own stop, before a failed check reuses run_error
        # for its report (a verdict, not a turn cut short).
        self.cut_short = (self.engine_ending.reason if self.engine_ending is not None
                     else turn_ending.LIMIT if self.limit_line
                     else turn_ending.ERROR if self.run_error else "")

    async def _check_report(self) -> None:
        """A check that did not pass fails the run with its report."""
        # A check that did not pass after its rounds (CHECKS.md): the
        # run fails with the report, and the person gets the usual
        # failure notification below.
        if self.final_status == run_status.COMPLETED and not self.limit_line and not self.run_error:
            try:
                from services.checks import evaluator as _checks
                run_row = await asyncio.to_thread(task_store.get_run, self.run_id)
                report = await asyncio.to_thread(
                    _checks.final_failure, self.chat_id, (run_row or {}).get("started_at") or "")
            except psycopg.Error:
                logger.debug("Task %s: the check report could not be read", self.run_id, exc_info=True)
                report = None
            if report:
                self.final_status = run_status.FAILED
                self.run_error = report
                logger.info("Task run %s failed a check: %s", self.run_id, report[:200])

    async def _stamp_end(self) -> None:
        """The run row stamped terminal, then the run's own side effects."""
        duration_ms = int((time.monotonic() - self.start) * 1000)
        # Work the run left running: the session stays warm (the idle
        # reaper spares it while the job runs) and the run row says so.
        from core.session import background_leash
        self.background_pending = background_leash.background_pending_count(self.session_id)
        if self.background_pending:
            logger.warning(
                "Task %s finishing with "
                "%s still running",
                self.run_id[:8], background_leash.pending_summary(self.session_id)
            )
        row_status = (run_status.FAILED if (self.engine_ending or self.limit_line or self.run_error)
                      else run_status.COMPLETED)
        await runner._end_run(
            self.run_id, row_status,
            background_pending=self.background_pending,
            error_message=(
                self.engine_ending.summary()[:2000] if self.engine_ending
                else f"Provider usage limit: {self.limit_line}" if self.limit_line
                else (self.run_error[:2000] if self.run_error else None)
            ),
            output_text=self.final_output[:10000] if self.final_output else "",
            completed_at=shared.now_iso(),
            duration_ms=duration_ms,
            session_id=self.session_id,
            cost_usd=self.task_total_cost if self.task_total_cost > 0 else None,
            chat_id=self.chat_id,
        )
        self.row_ended = True
        logger.info(
            "Task completed: run=%s, task=%s, duration=%sms",
            self.run_id, self.task.id, duration_ms
        )
        # Shared-library turn-end reconcile (see ChatStreamPump): a worker
        # or scheduled run that deleted/wrote inside a library in its
        # sandbox gets it propagated now, not at the next 5-min sweep.
        try:
            from services.knowledge import library_projector
            library_projector.schedule_reconcile_for_agent(self.task.agent)
        except Exception:  # noqa: BLE001  # the library reconcile is scheduled best effort, the run is done
            pass
        if self.engine_ending and not self.final_output:
            self.final_output = self.engine_ending.line()
        elif self.run_error and not self.final_output:
            # Pre-output engine failure: deliver a clearly-marked
            # NON-EMPTY terminal so a delegating agent sees the failure
            # (not an empty bubble) and doesn't re-delegate — twin of
            # the exception path's fail_output synthesis.
            self.final_output = (
                f'⚠ Task "{self.task.name}" failed on an engine error: '
                f'{self.run_error[:300]}'
            )

    def _notify_end(self) -> None:
        """The end's page: a failure warns in every mode but none, a clean
        end pages in auto."""
        # Fire completion notification ONLY for 'auto' mode. 'manual' agents
        # fire their own (system-injected prompt tells them to); 'none' is
        # fully silent. A usage-limited run warns instead for every mode but
        # 'none' — the agent that would have self-notified in 'manual' died
        # with the limit. BEFORE the delivery: a delivery that raises must
        # not lose the page (the handler pages only a run that had no
        # verdict yet).
        if self.engine_ending or self.limit_line or self.run_error:
            # Engine/limit failure: the agent that would have
            # self-notified in 'manual' mode died with the error, so warn
            # for every mode but 'none'.
            if self.task.notification_mode != "none":
                from services.notifications import notification_manager
                stopped = ("declined" if self.engine_ending and self.engine_ending.reason == "declined"
                           else "usage limit" if (self.engine_ending or self.limit_line) else "")
                spawn(notification_manager.fire_notification(
                    title=(f"Task stopped: {stopped} — {self.task.name}"
                           if stopped else f"Task failed: {self.task.name}"),
                    body=((self.engine_ending.summary() if self.engine_ending else "")
                          or self.limit_line or self.run_error or "")[:200],
                    severity="warning",
                    scope=self.task.scope,
                    target=runner._notify_target(self.task),
                    source="task",
                    source_id=self.task.id,
                    agent_slug=self.task.agent,
                    chat_id=self.chat_id,
                ), name="fire_notification", owner="runner")
        elif self.task.notification_mode == "auto":
            from services.notifications import notification_manager
            spawn(notification_manager.fire_notification(
                title=f"Task Complete: {self.task.name}",
                body=self.final_output[:200] if self.final_output else "Task finished.",
                severity=self.task.notify_severity,
                scope=self.task.scope,
                target=runner._notify_target(self.task),
                source="task",
                source_id=self.task.id,
                agent_slug=self.task.agent,
                chat_id=self.chat_id,
            ), name="fire_notification", owner="runner")

    def leave_for_recovery(self) -> bool:
        """A shutdown that leaves a re-adoptable remote turn running:
        True when the cancel is re-raised with the run left as it is."""
        self.user_stop = self.run_id in shared._user_cancelled_runs
        self.platform_reason = shared._platform_interrupts.pop(self.run_id, None)
        # Graceful shutdown of a re-adoptable remote turn: LEAVE the run
        # running and DON'T close the satellite session — the CLI keeps
        # working, and after restart defer_orphaned_runs parks it +
        # sessions_alive re-adopts it (Mode C). Suppress the pump's
        # durable flush so the recovery replay doesn't duplicate content.
        from services.scheduler import run_recovery
        if (shared._shutting_down and not self.user_stop and self.platform_reason is None
                and run_recovery.is_recovery_eligible(self.chat_id)):
            from core.events.stream_pump import suppress_recovery_flush
            suppress_recovery_flush(self.chat_id)
            logger.info(
                "Shutdown: leaving recoverable run %s running "
                "for satellite re-adopt (chat=%s)", self.run_id, self.chat_id
            )
            return True
        return False

    async def cancelled(self) -> None:
        """The cancel's end: the verdict, the session closed, the seat back,
        the delegating agent told."""
        if self.row_ended:
            logger.info(
                "Task %s: cancelled after the run ended — the verdict stands", self.run_id
            )
        elif self.user_stop:
            await runner._end_run(
                self.run_id, run_status.CANCELLED,
                error_message="Interrupted by user",
                completed_at=shared.now_iso(),
                duration_ms=int((time.monotonic() - self.start) * 1000),
            )
        else:
            # A cancellation the user never asked for (stall-reap,
            # shutdown, a cancelled pump task) is a platform failure —
            # never stamp it "cancelled", the runs page must be able to
            # tell the two apart.
            await runner._end_run(
                self.run_id, run_status.FAILED,
                error_message=self.platform_reason or (
                    "Proxy shutting down" if shared._shutting_down
                    else "Interrupted by platform"
                ),
                completed_at=shared.now_iso(),
                duration_ms=int((time.monotonic() - self.start) * 1000),
            )
        if self.borrowed is None:
            await interactive._close_interactive_task_session(self.session_id)
        if self.layer is not None:
            await self.layer.close_session(self.session_id)
        if (self.agent_cfg is not None and not self.session_started and not self.reused_warm
                and not self.seat_released):
            # Built, never bound — as on the failure path below.
            from core.config.config_builder import release_config_seat
            release_config_seat(self.session_id, self.agent_cfg)
        logger.info(
            "Task cancelled: run=%s (user=%s, "
            "platform_reason=%s)",
            self.run_id, self.user_stop, self.platform_reason or 'none'
        )
        # Delegate-only: tell the delegating agent the run was canceled, else its
        # delegate badge spins "running" forever and — getting no result — it may
        # re-delegate. Skipped during graceful shutdown (the delegating session is
        # being torn down too). _deliver_task_result self-gates on on_complete_agent,
        # so this no-ops for non-delegate tasks. A user-initiated Stop reports
        # "user_interrupted" with lane finalization (30s grace collects the
        # partial output + the user's redirect message, if any).
        if self.task.on_complete_agent and not shared._shutting_down and not self.result_delivered:
            if self.user_stop:
                await delivery._deliver_task_result(
                    self.task, run_status.USER_INTERRUPTED,
                    f'⚠ Delegated task "{self.task.name}" was stopped by the user '
                    f'before it finished.',
                    worker_chat_id=self.chat_id, output_cursor=self.output_cursor,
                    prompt_row_id=self.prompt_row_id, prompt_text=self.prompt,
                    run_started_at=self.run_started_at,
                    run_id=self.run_id,
                )
            else:
                await delivery._deliver_task_result(
                    self.task, run_status.CANCELLED,
                    f'⚠ Delegated task "{self.task.name}" was canceled before it finished.',
                    run_id=self.run_id,
                )

    async def failed(self, e: Exception) -> None:
        """A failure's end: the verdict, the session closed, the seat back, the
        delegating agent told, the page, the retry."""
        from storage.pg import run_db
        # A dead PTY on a watched interactive worker = the user closed or
        # killed the CLI deliberately — report user_interrupted, don't retry,
        # don't page anyone about a "failure".
        self.user_stop = isinstance(e, interactive._InteractiveSessionDied) and e.had_viewer
        logger.exception("Task failed: run=%s, task=%s: %s", self.run_id, self.task.id, e)
        if self.row_ended:
            logger.info(
                "Task %s: failure after the run ended — the verdict stands", self.run_id
            )
        else:
            await runner._end_run(
                self.run_id, run_status.CANCELLED if self.user_stop else run_status.FAILED,
                error_message="Interrupted by user" if self.user_stop else str(e),
                completed_at=shared.now_iso(),
                duration_ms=int((time.monotonic() - self.start) * 1000),
                chat_id=self.chat_id,
            )
        if self.borrowed is None:
            await interactive._close_interactive_task_session(self.session_id)
        if self.layer is not None:
            await self.layer.close_session(self.session_id)
        if (self.agent_cfg is not None and not self.session_started and not self.reused_warm
                and not self.seat_released):
            # Built, never bound: close_session above found no binding to
            # release, so the seat would stay taken until a restart.
            from core.config.config_builder import release_config_seat
            release_config_seat(self.session_id, self.agent_cfg)

        if self.result_delivered:
            logger.info(
                "Task %s: result already delivered — skipping the "
                "failure re-delivery (post-delivery cleanup error)", self.task.id
            )
        elif self.row_ended:
            # The delivery itself raised after the verdict: deliver again
            # what the success path was delivering — the run did not
            # fail, and a synthesized "failed" would make the delegating
            # agent re-delegate a completed task.
            await delivery._deliver_task_result(
                self.task, self.final_status, self.final_output,
                worker_chat_id=self.chat_id, output_cursor=self.output_cursor,
                prompt_row_id=self.prompt_row_id, prompt_text=self.prompt,
                run_started_at=self.run_started_at, ending=self.ending,
                run_id=self.run_id, cut_short=self.cut_short, turn_end_cursor=self.turn_end_cursor,
            )
        else:
            # Collect whatever output was saved before the error; if none
            # (a pre-turn failure — bad config / dead process / offline
            # target), deliver a clearly-marked NON-EMPTY terminal so the
            # delegating agent sees the failure (not an empty bubble) and
            # doesn't re-delegate. Delegate-only via _deliver_task_result's
            # on_complete gate; harmless for plain tasks.
            collected = lanes._collect_task_output(self.chat_id, self.output_cursor)
            if collected and not self.turn_done and not self.turn_end_cursor:
                with contextlib.suppress(Exception):
                    self.turn_end_cursor = await run_db(task_store.get_last_chat_message_id, self.chat_id)
            fail_output = collected or (
                f'⚠ Delegated task "{self.task.name}" was stopped by the user.'
                if self.user_stop else f'⚠ Delegated task "{self.task.name}" failed: {e}'
            )
            await delivery._deliver_task_result(
                self.task, run_status.USER_INTERRUPTED if self.user_stop else run_status.FAILED, fail_output,
                worker_chat_id=self.chat_id, output_cursor=self.output_cursor,
                prompt_row_id=self.prompt_row_id, prompt_text=self.prompt,
                run_started_at=self.run_started_at,
                run_id=self.run_id,
                # The turn died half way after writing something: what it
                # wrote is not its report.
                cut_short=(turn_ending.EXITED
                           if collected and not self.turn_done and not self.user_stop else ""),
                turn_end_cursor=self.turn_end_cursor,
            )

        # Failure safety net: fire warning notification for 'auto' and
        # 'manual' modes (a crashed agent can't notify itself). 'none' is
        # fully silent — user explicitly opted out of all notifications. A
        # deliberate user stop is not a failure — no page; nor is a
        # failure after the verdict (the run's own page went out first).
        if self.task.notification_mode != "none" and not self.user_stop and not self.row_ended:
            from services.notifications import notification_manager
            spawn(notification_manager.fire_notification(
                title=f"Task Failed: {self.task.name}",
                body=str(e)[:200],
                severity="warning",
                scope=self.task.scope,
                target=runner._notify_target(self.task),
                source="task",
                source_id=self.task.id,
                agent_slug=self.task.agent,
                chat_id=self.chat_id,
            ), name="fire_notification", owner="runner")

        # A retry is for a run that failed — never for a cleanup failure
        # after the verdict, which would run a completed task again.
        if self.attempt < self.task.retry.max_attempts and not self.user_stop and not self.row_ended:
            logger.info(
                "Retrying task %s "
                "(attempt %s/%s)",
                self.task.id, self.attempt + 1, self.task.retry.max_attempts
            )
            await asyncio.sleep(self.task.retry.delay_seconds)
            await runner._execute_task(
                self.task, trigger_type=self.trigger_type,
                trigger_source=self.trigger_source, attempt=self.attempt + 1,
                trigger_payload=self.trigger_payload,
            )

    def release(self) -> None:
        """The ``finally``: the session's index entry, the terminal hold, the
        report window."""
        from core.session import session_state as _state
        # Reap the task's in-memory session-index entry (added at the top of
        # runner._execute_task and again by _start_session). Task sessions
        # were never removed, so _sessions leaked one is_task entry per run — growing
        # index.json unboundedly and (pre-fix) poisoning the recency lookups
        # that /v1/session/current + /v1/location/request used to do. The live
        # process is already closed by the layer, and any callback re-binds via
        # the persistent chat_id (not this entry), so dropping it here is safe.
        # Idempotent under retries (the retry's _run_task already popped it).
        # EXCEPT after a graceful abort: the session was deliberately left
        # warm, so its entry must survive for the idle reaper +
        # /v1/session/current until the layer actually closes it.
        if (not self.session_kept_warm and self.borrowed is None
                and _state._sessions.pop(self.session_id, None) is not None):
            _state._save_sessions()
        if self.terminal_hold is not None:
            self.terminal_hold.release()
        lanes.release_report(*self.report_keys)
        # The one-time task's row is the frame's to retire (_run_task):
        # this finally never runs for a run cancelled before admission.
