"""A session on a machine survives the platform and the link, and a machine
that lost one hands it to the continue pass.

Three entries, all registered at boot (``register``):

* the terminal claim (``satellite_link.add_terminal_claim``): a terminal the
  machine still runs and the platform does not hold (the platform restarted,
  or the machine was away longer than the reconnect window) is taken back as
  the chat's terminal, with the context the security index kept for it and
  the open-turn row it was in;
* the terminals the machine lost (``satellite_link.add_terminals_lost_hook``):
  a terminal the platform held with a turn open that the machine's report no
  longer lists gets its record once its registry entry is gone;
* the alive report hook (``satellite_link.add_alive_report_hook``), after
  ``cut_turns``' by registration order: a headless session the platform held
  before the sign-in that a complete report does not list is gone. Its open
  turn is ended for whoever reads it, with or without a chat (the queue gets
  the lost marker, the reader's end is awaited), the session is closed, and a
  chat gets its record through
  ``continue_pass.session_gone``, which the pass continues with the
  machine's words.

A record is written once per chat (``cut_records.unsettled_of``), and a
session whose kind was never recorded (a re-adopted one) is recorded under
its chat's kind first, so the pass's judge reads one. A turn a holder runs
again (an adoption's replay, the pass's continue: ``cut_turns.holder_of``)
gets no record here: its holder hands its own record back to the pass. A turn a live run
drove is recorded with the run's name: the run is left as it is (its own
task reads the pump's ``cut_by_machine``) and the pass reopens it. A task
chat's lost session with no such run is left to its run's own ending.

The reaper's entry (``end_away_turn``): a held turn whose machine stayed away
past the bound is ended for its reader before the session is closed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import NamedTuple

from core import placement
from core.remote import remote_pty, satellite_link
from core.remote.satellite_grace import lost_frame
from core.session import (
    interactive_session,
    session_kind,
    session_state,
)
from core.session.transcript_tool_events import note_persisted_prompt
from storage import database as task_store
from storage.chat import cut_records, open_turns
from storage.pg import run_db

logger = logging.getLogger("otodock.machine-sessions")

# How long a lost terminal's registry entry is waited for before its record
# is written (its close tails the transcript in a thread first).
_TERMINAL_GONE_WAIT_S = 300.0
# How long a lost session's open turn is waited for to read its end, a chat's
# pump or the turn's own stream, before the session is closed.
_TURN_END_WAIT_S = 30.0
# The lost marker the pump reads as the ``lost`` ending (the one the window's
# expiry wrote before 2.0).
LOST_LINE = ("⚠ stream interrupted — the machine running this chat came back "
             "without this session; output may be incomplete")
# The marker of a held turn whose machine stayed away past the bound.
AWAY_LINE = ("⚠ stream interrupted — the machine running this chat stayed away "
             "too long; output may be incomplete")


class TurnEnded(NamedTuple):
    """What ``_end_turn`` found: the run whose turn the pass takes, and the
    record whose holder runs the turn (0 for none: an adoption's replay or
    the continue pass's continue, ``cut_turns.holder_of``). A held turn is
    its holder's to hand back, never the report's to record."""
    run_id: str = ""
    held: int = 0


def register() -> None:
    """Wire the three entries once (``startup._boot_services``)."""
    if all(fn is not on_alive_report for _, fn in satellite_link._alive_hooks):
        satellite_link.add_alive_report_hook(on_alive_report, name="machine_sessions")
    if all(fn is not claim_terminals for _, fn in satellite_link._terminal_claims):
        satellite_link.add_terminal_claim(claim_terminals, name="machine_sessions")
    if all(fn is not terminals_lost for _, fn in satellite_link._terminals_lost_hooks):
        satellite_link.add_terminals_lost_hook(terminals_lost, name="machine_sessions")


# -- the claim ----------------------------------------------------------------

def _claim_facts(machine_id: str, session_id: str) -> dict | None:
    """What a claim needs from the store, or None with the reason logged:
    the chat whose terminal this is, on this machine, its context placed on
    this machine exactly, no outside person's, its person's standing, for a
    user-paired machine the machine's owner and for a compute machine a
    compute placement and a person. Synchronous, on the store's executor."""
    from services.remote import chat_targets
    from storage import remote_store
    ctx = session_state.get_session_security(session_id)
    pl = getattr(ctx, "placement", None)
    if ctx is None or (getattr(pl, "machine_id", "") or "") != machine_id:
        logger.info("terminal claim: %s on %s refused: no context placed on this machine",
                    session_id[:8], machine_id[:8])
        return None
    if session_state.security_context_expired(session_id):
        logger.info("terminal claim: %s on %s refused: its token expired", session_id[:8], machine_id[:8])
        return None
    chat = task_store.get_chat_by_session(session_id)
    if not chat or session_kind.is_task_chat_id(chat.get("id")):
        logger.info("terminal claim: %s on %s refused: no chat of its own", session_id[:8], machine_id[:8])
        return None
    machine = remote_store.get_remote_machine(machine_id)
    compute = placement.machine_is_compute(machine)
    if not placement.runs_on(chat.get("execution_target"), machine_id, platform_machine=compute):
        logger.info("terminal claim: %s on %s refused: the chat runs elsewhere", session_id[:8], machine_id[:8])
        return None
    if getattr(ctx, "is_external", False):
        logger.info("terminal claim: %s on %s refused: an outside person's", session_id[:8], machine_id[:8])
        return None
    user_sub = task_store.get_user_sub_by_username(ctx.username) or "" if ctx.username else ""
    # An admin's machine takes back the terminal of any person its sessions
    # ran for; a person's own takes back its owner's only (an empty sub owns
    # nothing); a missing row takes back nothing. A compute machine has no
    # owner: it takes back a person's terminal the platform placed on it (a
    # compute placement in the security index, which no machine can write),
    # never one with no person, whose standing nothing could read.
    if compute:
        refused = placement.compute_of(pl) is None or not user_sub
    else:
        refused = not machine or (not placement.machine_is_admin_paired(machine) and (
            not user_sub or (machine.get("registered_by") or "") != user_sub))
    if refused:
        logger.info("terminal claim: %s on %s refused: %s", session_id[:8], machine_id[:8],
                    "no person, or not placed there as compute" if compute else "not the machine's owner")
        return None
    from services.scheduler import run_recovery
    if session_id in run_recovery.lost_standing(machine_id, {session_id: {}}, stops=False):
        logger.info("terminal claim: %s on %s refused: the person's standing changed",
                    session_id[:8], machine_id[:8])
        return None
    mode, floor = session_state.reloaded_state(session_id)
    if not mode and not floor:
        mode, floor = session_state.live_state(session_id)
    if not mode:
        from core.permission_modes import ASK_MODE
        mode = chat.get("permission_mode") or ASK_MODE
    row = open_turns.get_open(chat["id"]) or {}
    token = row.get("turn_token") if row.get("session_id") == session_id else ""
    # The open turn's prompt, whose row the earlier process wrote.
    prompt = task_store.get_last_user_message_text(chat["id"]) if token else ""
    return {
        "chat": chat, "ctx": ctx, "user_sub": user_sub, "mode": mode, "floor": floor,
        "turn_token": token or "", "prompt": prompt,
        "execution_path": chat_targets.chat_engine(chat),
    }


async def claim_terminals(machine_id: str, unheld: frozenset[str]) -> set[str]:
    """The terminals a machine reports that the platform does not hold:
    each one whose facts hold is taken back as its chat's terminal. Runs in
    the machine's read loop: the store reads run in a thread, nothing waits
    on a person or another machine."""
    from services.remote import chat_reload
    taken: set[str] = set()
    for session_id in sorted(unheld):
        if interactive_session.get(session_id) is not None:
            continue
        try:
            facts = await run_db(_claim_facts, machine_id, session_id)
        except Exception:
            logger.exception("terminal claim: %s on %s: the store read failed", session_id[:8], machine_id[:8])
            continue
        if facts is None:
            continue
        chat, ctx = facts["chat"], facts["ctx"]
        if interactive_session.find_live_for_chat(chat["id"]) is not None:
            continue   # a start of this chat took it meanwhile
        try:
            interactive_session.adopt_remote(
                session_id=session_id, chat_id=chat["id"], agent_name=chat.get("agent") or ctx.agent,
                machine_id=machine_id, execution_path=facts["execution_path"],
                user_sub=facts["user_sub"], role=ctx.role or "", username=ctx.username or "",
                mode=facts["mode"], token_floor=facts["floor"], turn_token=facts["turn_token"],
                chat_row=chat,
            )
        except Exception:
            logger.exception("terminal claim: %s on %s: the adoption failed", session_id[:8], machine_id[:8])
            continue
        if facts["prompt"]:
            note_persisted_prompt(chat["id"], facts["prompt"], owner=session_id)
        # A page whose socket came back before the machine showed the chat's
        # history: it reopens the chat on the terminal, as after a reload.
        chat_reload._watch_terminal(chat["id"])
        taken.add(session_id)
        logger.info("terminal claim: %s on %s taken back for chat %s", session_id[:8], machine_id[:8],
                    chat["id"][:8])
    return taken


# -- a session the machine lost ----------------------------------------------

async def machine_lost_session(chat_id: str, session_id: str, machine_id: str,
                               facts: session_state.SessionFacts | None = None, *,
                               run_id: str = "") -> bool:
    """A chat's session its machine no longer has: one record for the chat
    (an unsettled one of a boot or an earlier loss stands) through
    ``continue_pass.session_gone``, made of ``facts`` (read before the
    session's close, which forgets them; read now when none are given). A
    session with no recorded kind (one re-adopted before 2.0) takes its
    chat's, so ``continue_pass.judge`` reads one. ``run_id``: the run whose
    turn the pass takes (``_end_turn``'s answer: its pump named it and was
    told so). The record names it and the pass reopens it, in a task's own
    chat too. Such a run was left live by its own task, so it never stays
    behind with nothing to end it: when no record is written and the one
    that stands is not this run's, it fails here with its one report. True
    when a record was written."""
    from services.scheduler import continue_pass, run_recovery
    held = facts if facts is not None else session_state.session_facts(session_id)

    async def _end_the_left_run() -> None:
        if run_id:
            await run_recovery._fail_and_report(run_id, chat_id, run_recovery.RUN_CUT_MACHINE,
                                                cause=cut_records.CAUSE_MACHINE)

    if session_kind.is_task_chat_id(chat_id) and not run_id:
        return False
    chat, unsettled = await run_db(lambda: (task_store.get_chat(chat_id), cut_records.unsettled_of(chat_id)))
    if not chat:
        await _end_the_left_run()
        return False
    if unsettled is not None:
        logger.info("machine %s lost session %s of chat %s: its record %s stands",
                    machine_id[:8], session_id[:8], chat_id[:8], unsettled["id"])
        if (unsettled.get("run_id") or "") != run_id:
            # Another turn's record: it will not reopen this run.
            await _end_the_left_run()
        return False
    if not held.kind:
        held = dataclasses.replace(held, kind=session_kind.of_chat(chat).name)
    written = await continue_pass.session_gone(chat_id, session_id, machine_id, facts=held,
                                               run_id=run_id)
    if not written:
        await _end_the_left_run()
    return written


async def _end_left_run(run_id: str, chat_id: str) -> None:
    """Best effort: a run left for the pass whose record could not be
    written fails with its one report."""
    from services.scheduler import run_recovery
    try:
        await run_recovery._fail_and_report(run_id, chat_id, run_recovery.RUN_CUT_MACHINE,
                                            cause=cut_records.CAUSE_MACHINE)
    except Exception:
        logger.exception("run %s was left for the continue pass and could not be ended", run_id)


async def terminals_lost(machine_id: str, lost: list[satellite_link.LostTerminal]) -> None:
    """Terminals the platform held with a turn open that the machine's
    report no longer lists, with the facts the reconcile read before it
    fired their exits: each gets its record once that session's close is
    done (it left the registry: the close tailed the transcript, ended the
    turn's row and cleared the id's state in one block). The record kicks
    the pass, whose continue starts the chat's session again under the
    same id, so no record is written while that close can still reach its
    cleanup: past ``_TERMINAL_GONE_WAIT_S`` the chat gets none, and a
    warning says so. A terminal started again under the id meanwhile does
    not hold the record back."""
    for item in lost:
        for _ in range(int(_TERMINAL_GONE_WAIT_S / 0.2)):
            if interactive_session.get(item.session_id) is not item.session:
                break
            await asyncio.sleep(0.2)
        if interactive_session.get(item.session_id) is item.session:
            logger.warning("machine %s lost terminal %s of chat %s: its close is still running, no record",
                           machine_id[:8], item.session_id[:8], item.chat_id[:8])
            continue
        await machine_lost_session(item.chat_id, item.session_id, machine_id, item.facts)


def _held_before(remote, machine_id: str, signed_in_at: float) -> list[tuple[str, object]]:
    """The headless sessions the layer held on the machine before the sign-in."""
    out = []
    for sid in remote.session_ids_on(machine_id):
        info = remote._sessions.get(sid)
        if info is None or remote.is_spawning(sid):
            continue
        if getattr(info, "created_at", 0.0) >= signed_in_at:
            continue   # started on this connection: the report predates it
        out.append((sid, info))
    return out


async def _end_turn(remote, info, chat_id: str, *, to_the_pass: bool = True) -> TurnEnded:
    """End a lost session's open turn for whoever reads it, before the close
    and before anything records it: the queue gets the lost marker and the
    end, and the reader's end is awaited. A chat's pump, so the record never
    meets a live pump (``chat_rebuild.turn_open_while_held``); the pump writes
    no card, the pass carries the turn on. With no pump (a meeting
    participant, a chat's turn with none) the turn's own stream, since the
    close takes the engine's state and a turn still reading would wait on it
    for ever. Each wait is bounded by ``_TURN_END_WAIT_S``. ``to_the_pass``
    False (a machine still away): the turn ends lost with its card and the
    ``away`` detail, nothing carries it on. Returns the run whose turn the
    pass takes (the one the chat's running pump of this session names, ""
    when it names none or the pass takes nothing) and the record whose
    holder runs the turn, found from the pump while it still runs. A running
    pump of another session of the chat is not this turn's reader: it gets no
    mark, and its end is not waited for."""
    from core.events import turn_ending
    from core.events.stream_pump import _active_pumps, the_pass_takes_it
    pump = _active_pumps.get(chat_id) if chat_id else None
    task = getattr(pump, "_task", None) if pump is not None else None
    running = task is not None and not task.done()
    run_id, held = "", 0
    if running and to_the_pass and getattr(pump, "session_id", "") == info.session_id:
        # Only a running pump discards the mark at its end, and only the
        # pump of this session reads this turn's end: a pump that drives
        # another session of the chat keeps its own endings and its card.
        from services.scheduler import cut_turns
        the_pass_takes_it(chat_id)
        run_id = getattr(pump, "run_id", "") or ""
        held = cut_turns.holder_of(pump)
    queue = info.event_queue
    try:
        queue.put_nowait(lost_frame(LOST_LINE) if to_the_pass
                         else lost_frame(AWAY_LINE, turn_ending.AWAY))
        queue.put_nowait(None)
    except asyncio.QueueFull:
        logger.warning("session %s: its queue is full, the turn ends at the close", info.session_id[:8])
    if task is not None and running and getattr(pump, "session_id", "") == info.session_id:
        await asyncio.wait({task}, timeout=_TURN_END_WAIT_S)
        return TurnEnded(run_id, held)
    deadline = time.monotonic() + _TURN_END_WAIT_S
    while getattr(info, "turn_active", False) and time.monotonic() < deadline:  # noqa: ASYNC110  # the turn's flag is the layer's, cleared in its stream's finally: no event to wait on
        await asyncio.sleep(0.05)
    return TurnEnded(run_id, held)


async def end_away_turn(remote, info) -> None:
    """The reaper closes a held session whose machine is still away with its
    turn open (no line from it for the turn ceiling): the turn is ended for
    its reader first, lost with the ``away`` detail. Without this the close
    told the reader nothing, and a turn with a tool open waited for ever."""
    chat = await run_db(task_store.get_chat_by_session, info.session_id)
    if remote._sessions.get(info.session_id) is not info or not getattr(info, "turn_active", False):
        return
    logger.info("machine %s stayed away too long: ending the open turn of session %s (chat %s)",
                info.machine_id[:8], info.session_id[:8], ((chat or {}).get("id") or "none")[:8])
    await _end_turn(remote, info, (chat or {}).get("id") or "", to_the_pass=False)


async def on_alive_report(report) -> None:
    """A machine's complete report, after the adoption and after ``cut_turns``
    took the records of a restart: every headless session the platform held
    on the machine before the sign-in that the report does not list is gone.
    A turn that was open is ended for whoever reads it, a chat's turn is
    recorded for the pass; the session is closed either way, so the next send
    resumes a chat by its own means."""
    if not report.complete or not report.is_current():
        return
    from core.session.session_manager import _get_remote_layer
    remote = _get_remote_layer()
    if remote is None:
        return
    for sid, info in _held_before(remote, report.machine_id, report.signed_in_at):
        if sid in report.headless or not report.is_current():
            continue
        chat = await run_db(task_store.get_chat_by_session, sid)
        # Read after the store's answer, with no await between them and the
        # facts: a start that replaced the session or a close elsewhere during
        # the read is seen here.
        if not report.is_current() or remote._sessions.get(sid) is not info:
            continue   # a start replaced it meanwhile
        chat_id = (chat or {}).get("id") or ""
        turn_open = bool(getattr(info, "turn_active", False))
        logger.info("machine %s came back without session %s (chat %s, turn %s): closing it",
                    report.machine_id[:8], sid[:8], chat_id[:8] or "none", "open" if turn_open else "idle")
        # The record's facts first (the close forgets them), then the turn's
        # end, the record, the close. The record comes before the close so a
        # follow-up at the lane gate never finds the chat quiet with no
        # record. The pass's start waits for the close (``session_closing``),
        # and the close takes the record out of the registry before the pass
        # can run: nothing awaits between the record's write and that.
        facts = session_state.session_facts(sid)
        ended = await _end_turn(remote, info, chat_id) if turn_open else TurnEnded()
        run_id = ended.run_id
        # Its machine said the session is gone: no round rides it as a warm one.
        if info is not None:
            info.cli_dead = True
        try:
            if turn_open and chat_id and ended.held:
                # A holder ran this turn again (a replay, the pass's continue):
                # its end reads how the pump ended and hands the record back.
                # Nothing awaits from here to the close, which holds the id
                # before that hand-back can be written.
                logger.info("machine %s lost session %s of chat %s: record %s goes back to the "
                            "pass when the turn that held it ends", report.machine_id[:8],
                            sid[:8], chat_id[:8], ended.held)
            elif turn_open and chat_id:
                await machine_lost_session(chat_id, sid, report.machine_id, facts, run_id=run_id)
        except Exception:
            # The store failed under the record. The run's own task already
            # left it for the pass: with no record nothing would end it.
            if run_id:
                await _end_left_run(run_id, chat_id)
            raise
        finally:
            # The turn's end and the record's store reads sit between the
            # last look at the registry and here. A start may have put its
            # own record under the id meanwhile (the turn is over, so a
            # message that waited can start the session again): the close is
            # of the record this report found, never of the id.
            if remote._sessions.get(sid) is info:
                try:
                    await remote.close_session(sid)
                except Exception:
                    logger.exception("machine %s: closing the lost session %s failed",
                                     report.machine_id[:8], sid[:8])


# -- what a machine's terminals wait for when it is away ----------------------

def sweep_away_terminals() -> int:
    """Terminals held for a machine away longer than
    ``remote_pty.TERMINAL_AWAY_MAX_S`` are ended, as the window ended them
    at 90 s before 2.0 (the reaper's tick)."""
    return remote_pty.sweep_away()
