"""The continue pass: a chat whose turn the platform cut goes on by itself.

A stop, a crash or a lost machine cut a turn and left a record
(``storage/chat/cut_records.py``, written at boot by ``cut_turns`` and, for a
kept run with no record of its own, by ``run_recovery``). The pass
takes each ``pending`` record once and decides (``judge``): the chat is
continued through the one function that starts a session with no socket
(``core/session/chat_rebuild.rebuild_and_resume``), its task run is reopened
(``run_recovery.reopen_run``), it gives way to a person who wrote first, or
it gets the card that says the reply was cut. Nothing here trusts the record
beyond who and what kind: the person's standing and role are read when the
session starts, and the rung that starts it reads them again.

When it runs: at boot for the turns the platform ran itself, and for a turn
on a machine only when that machine's complete report no longer lists the
session (``cut_turns.machine_reported``), never on a timer. So two engines
never write one conversation.

The numbers: ``_PASS_CONCURRENCY`` continues are between their claim and
their turn's start at once, starts are ``_PASS_SPACING_S`` apart, and each
start then waits for admission like any wake. A chat is continued at most
``CHAIN_MAX`` times in a row with no message from a person and ``DAY_MAX``
times in a day, and a start that could not happen is tried ``ATTEMPTS_MAX``
times.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.background import spawn
from core.session import session_kind
from services.infra import platform_marks
from services.scheduler import cut_turns
from storage import database as task_store
from storage.automation import run_status
from storage.chat import cut_records, pending_rebuilds

if TYPE_CHECKING:
    from core.session.session_state import SessionFacts

logger = logging.getLogger("otodock.recovery")

#: A call can finish with its answer lost (a mail sent, a push landed), so the
#: engine is never told it did not. One sentence for every engine and every
#: path that tells of a cut.
_CUT_CALL = ("A tool call that had not answered may or may not have finished, so check what "
             "it did before you repeat it.")
#: What the engine is told, on every engine, and what the chat shows under
#: "Continued after a restart".
CONTINUE_WORDS = (
    "The platform restarted while you were working, and your last turn was cut off. "
    f"The platform runs nothing again for you. {_CUT_CALL} "
    "Look at what is already done, then continue from where you stopped."
)
MACHINE_WORDS = (
    "The machine this chat runs on restarted while you were working, and your last turn "
    f"was cut off. The platform runs nothing again for you. {_CUT_CALL} "
    "Look at what is already done, then continue from where you stopped."
)
#: What a person's own next message carries when their turn took the cut
#: turn's place.
GAVE_WAY_NOTE = (
    "The platform restarted while you were working on the turn before this message, and "
    f"that turn was cut off. The platform runs nothing of it again for you. {_CUT_CALL}"
)

CHAIN_MAX = 3
DAY_MAX = 6
ATTEMPTS_MAX = 3
_PASS_CONCURRENCY = 2
_PASS_SPACING_S = 5.0
#: How long a continue's start is watched for its turn before its place in
#: the gate goes to the next (admission may hold it for minutes).
_START_WATCH_S = 30.0
_START_POLL_S = 0.5

CONTINUE = "continue"
REOPEN = "reopen"
GIVE_WAY = "give_way"
CARD = "card"
DROP = "drop"

#: The kinds of session that have no resume: a call and a meeting end as
#: they do without the pass.
_NO_RESUME = frozenset({session_kind.PHONE.name, session_kind.MEETING.name,
                        session_kind.DUPLEX_TURN})

_running = False
_again = False


@dataclass(frozen=True)
class Verdict:
    """What the pass does with one record, and why."""

    action: str
    reason: str = ""
    chat: dict | None = None
    person: str = ""
    role: str = ""
    run: dict | None = None
    queued: bool = False


def judge(record: dict) -> Verdict:
    """The rules, in order, from the store alone. Synchronous: call it on
    the DB executor."""
    from auth import roles
    from auth.providers import acting_role_of, effective_role_of
    from core.session.visibility import drive_refusal, is_synthetic_owner
    from services.scheduler import run_recovery
    chat_id = record["chat_id"]
    chat = task_store.get_chat(chat_id)
    if not chat:
        return Verdict(DROP, "the chat is gone")
    kind = record.get("kind") or ""
    if kind in _NO_RESUME:
        return Verdict(DROP, f"a {kind} turn has no resume", chat)
    if not kind:
        return Verdict(CARD, "the turn of an older release", chat)
    # The product's rule: past a long downtime, or after any restore, nothing
    # is continued. The boot that found it wrote that on the record, so a boot
    # that follows reads it too. A machine away that long counts the same.
    if record.get("cause") == cut_records.CAUSE_NOT_TAKEN_UP:
        return Verdict(CARD, "the platform was down too long or was restored", chat)
    if platform_marks.past_max_downtime(record.get("cut_at") or ""):
        return Verdict(CARD, "its machine was away too long", chat)
    if cut_records.person_wrote_since(chat_id, record["cut_at"]):
        return Verdict(GIVE_WAY, "a person wrote first", chat, person=record.get("person") or "")
    if task_store.list_chat_input_queue(chat_id):
        return Verdict(GIVE_WAY, "a person's message waits", chat,
                       person=record.get("person") or "", queued=True)
    if (cut_records.continued_in_a_row(chat_id) >= CHAIN_MAX
            or cut_records.continued_since(chat_id, 24) >= DAY_MAX):
        return Verdict(CARD, "continued too often with no message from a person", chat)
    run_id = record.get("run_id") or ""
    if run_id:
        run = task_store.get_run(run_id)
        if run and run_status.is_live(run.get("status")) and run_recovery.reopens(run):
            return Verdict(REOPEN, "", chat, run=run)
    if kind == session_kind.TASK.name:
        return Verdict(CARD, "its run is over", chat)
    if session_kind.is_task_chat_id(chat_id):
        return Verdict(CARD, "a task's chat with no run of its own", chat)
    person = record.get("person") or ""
    owner = chat.get("user_sub") or ""
    if not person:
        return Verdict(CARD, "nobody to continue it as", chat)
    if not is_synthetic_owner(owner) and owner != person:
        return Verdict(CARD, "the chat is another person's", chat)
    agent = chat.get("agent") or ""
    if effective_role_of(person, agent) == roles.NO_ACCESS:
        return Verdict(CARD, "its person no longer holds the agent", chat)
    role = acting_role_of(person, agent)
    # The rule every door that drives a chat asks: the editor tier in the
    # agent's shared space, a run's own rule on a chat with a run.
    if drive_refusal(chat, person, role):
        return Verdict(CARD, "its person may not continue this chat", chat)
    return Verdict(CONTINUE, "", chat, person=person, role=role)


def words(record: dict, chat: dict) -> str:
    """The continue's prompt: the one text, a sentence for a card that
    waited, and the cut turn's tool calls for an engine that rebuilds its
    history from the chat's rows. Synchronous."""
    from core.session.session_manager import capabilities_for_path
    from services.remote import chat_targets
    from ws import wire_events as wire
    text = MACHINE_WORDS if record.get("cause") == cut_records.CAUSE_MACHINE else CONTINUE_WORDS
    try:
        card = json.loads(record.get("card") or "{}") or {}
    except ValueError:
        card = {}
    if card.get("event_type") == wire.ITEM_PLAN_REVIEW:
        text += " You were waiting for the person to review your plan. Present it again."
    elif card.get("tool"):
        text += (f" You were waiting for the person's answer about {card['tool']}. "
                 "Ask again if you still need it.")
    try:
        rebuilds = capabilities_for_path(
            chat_targets.chat_engine(chat)).behaviour.rebuilds_history_from_db
    except ValueError:
        rebuilds = False
    if rebuilds:
        calls = _tool_calls_since(chat["id"], record.get("started_at") or "")
        if calls:
            # An engine that rebuilds from rows has no other record of them,
            # and the rows are saved at checkpoints only.
            text += " Before the cut you had called: " + ", ".join(calls) + ". This list may be short."
    return text


def _tool_calls_since(chat_id: str, since: str) -> list[str]:
    """The tool calls of the chat's rows written since ``since``, each with
    whether it answered."""
    from ws import wire_events as wire
    out: list[str] = []
    for row in task_store.get_chat_messages(chat_id):
        if (row.get("event_type") != wire.PERSISTED_TOOL or (row.get("created_at") or "") < since):
            continue
        try:
            block = json.loads(row.get("event_data") or "{}")
        except ValueError:
            continue
        name = block.get("name") if isinstance(block, dict) else None
        if isinstance(name, str) and name:
            # A row says ``active: false`` once its tool ended. Direct LLM's end
            # carries a preview only, so its rows never hold a result.
            answered = block.get("active") is False or wire.TOOL_RESULT in block
            out.append(f"{name} ({'answered' if answered else 'no answer, it may have finished'})")
    return out[-20:]


def write_wake_row(chat_id: str, text: str) -> int:
    """The chat's row for a continue: the platform's words, drawn as
    "Continued after a restart". Synchronous: the chat's lane. Returns the
    row's id."""
    from ws import wire_events as wire
    return task_store.add_chat_message(
        chat_id, "event", "", event_type=wire.PERSISTED_SCHEDULE_WAKE,
        event_data=json.dumps({"prompt": text, "source": wire.WAKE_SOURCE_CONTINUE}))


def kick() -> None:
    """Run the pass soon (a machine reported sessions gone)."""
    spawn(run_pending(), name="continue_pass", owner="continue_pass")


async def sweep() -> None:
    """The reaper's tick: a record still ``pending`` (its start did not
    happen) is tried again."""
    from storage.pg import run_db
    if not _running and await run_db(cut_records.in_state, cut_records.PENDING):
        await run_pending()


async def run_pending() -> int:
    """One pass over the ``pending`` records, unless new work is held: the
    drain's resume or the boot passes them. A second call while one runs asks
    it to look again. Returns how many records it took, once each has settled
    or has its turn on the chat: a continue's turn goes on beside the caller
    and settles its record at its end, so what follows the pass at boot never
    waits for a turn that may wait on a person."""
    global _running, _again
    from core import drain
    from storage.pg import run_db
    if _running:
        _again = True
        return 0
    if drain.holding():
        return 0
    _running = True
    taken = 0
    try:
        while True:
            _again = False
            records = await run_db(cut_records.in_state, cut_records.PENDING)
            if not records:
                break
            gate = asyncio.Semaphore(_PASS_CONCURRENCY)
            handed = [asyncio.Event() for _ in records]
            for i, (rec, done) in enumerate(zip(records, handed, strict=True)):
                spawn(_take(rec, gate, i * _PASS_SPACING_S, done),
                      name="continue_pass_take", owner="continue_pass")
            await asyncio.gather(*(done.wait() for done in handed))
            taken += len(records)
            if not _again:
                break
    finally:
        _running = False
    return taken


async def _take(pending: dict, gate: asyncio.Semaphore, delay: float, handed: asyncio.Event) -> None:
    """One record: its spaced place in the gate, the claim, the verdict.
    ``handed`` is set when the pass need not wait for it any longer."""
    from core import drain
    from storage.pg import run_db
    record_id = pending["id"]
    held = [False]

    def _release() -> None:
        handed.set()
        if held[0]:
            held[0] = False
            gate.release()

    # Held from before the claim to the end of the verdict, so a record is
    # never ``running`` with no holder of this process (the lane gate reads
    # it), and a continue's turn its machine cuts is found from its pump.
    holding = None
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        await gate.acquire()
        held[0] = True
        if drain.holding():
            return
        holding = cut_turns.hold(pending)
        record = await run_db(cut_records.claim, record_id)
        if record is None:
            return
        try:
            if record["attempts"] > ATTEMPTS_MAX:
                # A process ended on its last attempt.
                await _card(record, "it could not be started", _release)
                return
            verdict = await run_db(judge, record)
            await _act(record, verdict, _release, holding=holding)
        except Exception:
            logger.exception("continue pass: chat=%s failed", pending["chat_id"][:8])
            if record["attempts"] >= ATTEMPTS_MAX:
                await _card(record, "it could not be started", _release)
            else:
                # Left for the next sweep, within its attempts.
                await run_db(cut_records.release, record_id)
    finally:
        cut_turns.let_go(holding)
        _release()


async def _settle(record: dict, state: str, reason: str = "") -> None:
    from storage.pg import run_db
    await run_db(cut_records.settle, record["id"], state, reason)
    logger.info("continue pass: chat=%s %s%s", record["chat_id"][:8], state,
                f" ({reason})" if reason else "")


async def _act(record: dict, verdict: Verdict, release, *, holding: dict | None = None) -> None:
    """``holding``: this take's hold (``cut_turns.hold``). A continue's turn
    its machine cut (its pump's own ending) goes back to the pass, and gets
    the card once its attempts are spent."""
    from storage.pg import run_db
    chat_id = record["chat_id"]
    if verdict.action == DROP:
        # No card (the chat is gone, or its kind has no resume), but a run the
        # record names never stays live behind it.
        await _end_the_run(record, release)
        await _settle(record, cut_records.DROPPED, verdict.reason)
        return
    if verdict.action == CARD:
        await _card(record, verdict.reason, release)
        return
    if verdict.action == GIVE_WAY:
        await _give_way(record, verdict, release)
        return
    chat = verdict.chat or {}
    if verdict.action == REOPEN:
        from core import drain
        from services.scheduler import run_recovery, runner
        # The cut run's own task may still be unwinding (its machine cut the
        # turn while the platform ran). New work may be held by then: the
        # drain's resume passes the record.
        await runner.unwound((verdict.run or {}).get("id") or "")
        if drain.holding():
            await run_db(cut_records.release, record["id"])
            return
        await _settle_waiter(chat, continued=True, person=record.get("person") or "")
        reopened = await run_recovery.reopen_run(verdict.run or {}, record)
        if reopened:
            await _settle(record, cut_records.CONTINUED, "its run was reopened")
        elif reopened is False:
            await _card(record, "its run could not be reopened", release)
        # (None: a person stopped the run meanwhile and took the record)
        return
    from services.remote import chat_move
    if chat_move._person_took_chat(chat_id):
        await _give_way(record, Verdict(GIVE_WAY, "a person's turn has the chat", chat,
                                        person=verdict.person), release)
        return
    task = asyncio.ensure_future(_continue(record, chat, verdict.person, verdict.role))
    await _watch_start(chat_id, task)
    if not task.done():
        release()
    state = await task
    # A turn the stall watch reaped returns before its pump has ended: the
    # pump's own ending is read once it has one.
    await cut_turns.ended(holding)
    if state != cut_records.GAVE_WAY and cut_turns.cut_by_its_machine(holding):
        if record["attempts"] >= ATTEMPTS_MAX:
            await _card(record, "its machine cut it again", release)
        else:
            await cut_turns.hand_back(record["id"], chat_id)
        return
    if state == cut_records.CONTINUED:
        await _settle(record, cut_records.CONTINUED)
    elif state == cut_records.GAVE_WAY:
        await _give_way(record, Verdict(GIVE_WAY, "a person took the chat", chat,
                                        person=verdict.person), release)
    elif record["attempts"] >= ATTEMPTS_MAX:
        await _card(record, "it could not be started", release)
    else:
        await run_db(cut_records.release, record["id"])
        logger.info("continue pass: chat=%s not started, tried again later (attempt %d)",
                    chat_id[:8], record["attempts"])


async def _watch_start(chat_id: str, task: asyncio.Future) -> None:
    """Until the continue's turn is on the chat, its call returned, or the
    watch ran out: then the next record may start."""
    from core.events.stream_pump import _active_pumps
    waited = 0.0
    while not task.done() and chat_id not in _active_pumps and waited < _START_WATCH_S:
        await asyncio.sleep(_START_POLL_S)
        waited += _START_POLL_S


async def _card(record: dict, reason: str, release=None) -> None:
    """The card of a record nothing continues, and the end of its run with
    the run's one report. ``release`` lets the gate and the boot go on beside
    the report. The record settles last, so a process that ends here does
    this again, and the card is written once. The report goes out only from
    the call that failed the run: a process that ends between the two leaves
    the run failed with no report."""
    await cut_turns.write_card(record)
    await _end_the_run(record, release)
    await _settle(record, cut_records.DROPPED, reason)
    await _settle_waiter(record, continued=False)


async def _end_the_run(record: dict, release) -> None:
    from services.scheduler import run_recovery
    if not record.get("run_id"):
        return
    if release is not None:
        release()
    await run_recovery._fail_and_report(record["run_id"], record["chat_id"],
                                        run_recovery.cut_reason(record),
                                        cause=record.get("cause") or "")


async def _give_way(record: dict, verdict: Verdict, release=None) -> None:
    """A person's own turn takes the cut turn's place: the cut rides their
    message as a note, and a message that waits is sent. A run the cut turn
    belonged to ends here, with its one report: the person's turn is not its
    round."""
    from core.events import input_queue
    from storage.pg import run_db
    chat_id = record["chat_id"]
    await run_db(functools.partial(
        task_store.append_pending_delegate_wake, chat_id, GAVE_WAY_NOTE,
        person=verdict.person, note=True))
    if verdict.queued:
        await input_queue.loaded(chat_id)
        input_queue.arm(chat_id)
    await _end_the_run(record, release)
    await _settle(record, cut_records.GAVE_WAY, verdict.reason)
    await _settle_waiter(record, continued=False)


async def _settle_waiter(chat_or_record: dict, *, continued: bool, person: str = "") -> list[str]:
    """The chat's waiting reload or move, settled with its record
    (``chat_pending_rebuilds``): a reload whose chat is continued is done by
    the start itself (its account choices written first), anything else is
    told it did not happen. Returns the reload's sentences for the chat."""
    from services.remote import chat_reload
    from storage.pg import run_db
    chat_id = chat_or_record.get("chat_id") or chat_or_record.get("id") or ""
    row = await run_db(pending_rebuilds.take, chat_id)
    if row is None:
        return []
    return await chat_reload.settle_after_restart(row, continued=continued, person=person)


async def _continue(record: dict, chat: dict, person: str, role: str) -> str:
    """Continue the chat: the wake row, the close of a session this process
    still holds, the one function. Returns the record's state: continued,
    gave way, or pending (nothing started)."""
    from core.events import chat_writer
    from core.session import chat_rebuild, session_manager
    from core.session.session_delivery import DeliveryOutcome
    from services.remote import chat_move
    from storage.pg import run_db
    from ws import wire_events as wire
    chat_id = chat["id"]
    text = await run_db(words, record, chat)
    written: list[asyncio.Future] = []

    def _persist(target_chat_id: str) -> None:
        if target_chat_id:
            written.append(chat_writer.submit(
                target_chat_id, functools.partial(write_wake_row, target_chat_id, text),
                label="continue_wake"))

    async def _undo() -> None:
        for fut in written:
            try:
                row = await fut
                await chat_writer.submit(chat_id, functools.partial(
                    task_store.delete_chat_message_ranges, chat_id, [(row - 1, row)]),
                    label="continue_wake_undone")
            except Exception:  # noqa: BLE001  # the row stays: a stale line, not a lost turn
                logger.debug("continue pass: the wake row was not taken back", exc_info=True)

    sid = chat.get("session_id") or ""
    if sid and session_manager.find_layer_for_session(sid) is not None:
        # A session this process still holds for the chat (a record whose
        # machine lost the stream): closed first, under the chat's hold.
        try:
            async with chat_rebuild.closed(chat_id, reason="continue", by=person):
                pass
        except chat_rebuild.ChatBusy:
            return cut_records.GAVE_WAY
        except LookupError:
            return cut_records.GAVE_WAY
        chat = await run_db(task_store.get_chat, chat_id) or chat
    await _settle_waiter(chat, continued=True, person=person)
    mode = await run_db(chat_rebuild.chat_mode, chat)
    logger.info("continue pass: chat=%s continues by itself (%s, %s)", chat_id[:8], mode,
                record.get("cause") or "")
    outcome = DeliveryOutcome("none", chat_id=chat_id)
    try:
        outcome = await chat_rebuild.rebuild_and_resume(
            chat_id, text, source=wire.WAKE_SOURCE_CONTINUE, session_id=chat.get("session_id") or "",
            agent=chat.get("agent") or "", person=person, role=role,
            mode=mode, kind=session_kind.DASHBOARD.name, build=chat_rebuild.AS_CHAT,
            persist_event=_persist, allow_ws=False, allow_pty=False, allow_live_turn=False,
            fresh_without_session=True,
        )
    except Exception:  # noqa: BLE001  # a rung that raises (the spawn's guard, a broken start) is undone like a start that did not happen
        logger.warning("continue pass: chat=%s: the start raised", chat_id[:8], exc_info=True)
    if outcome.path != "none":
        if record.get("plan"):
            await chat_writer.submit(chat_id, functools.partial(
                task_store.update_chat_plan_status, chat_id, record["plan"], "implemented"),
                label="plan_status")
        return cut_records.CONTINUED
    await _undo()
    return cut_records.GAVE_WAY if chat_move._person_took_chat(chat_id) else cut_records.PENDING


async def session_gone(chat_id: str, session_id: str, machine_id: str, *,
                       facts: SessionFacts | None = None, run_id: str = "") -> bool:
    """A session a machine lost while the platform stayed up, with its turn
    open: the chat gets a record and the pass takes it. The record is made
    of ``facts``, read by the caller before anything closed the session (a
    close forgets them); with none, of what the platform holds for the
    session now. The caller (``services/remote/machine_sessions``, the
    reader of a machine's complete report) says the session is gone.
    ``run_id``: the run that drove the turn, which the pass reopens. True
    when a record was written."""
    from core.session import session_state
    from storage.pg import run_db
    held = facts if facts is not None else session_state.session_facts(session_id)

    def _record() -> dict | None:
        return cut_records.add(
            chat_id, cause=cut_records.CAUSE_MACHINE, state=cut_records.PENDING,
            session_id=session_id, machine_id=machine_id, engine_open=True,
            person=(task_store.get_user_sub_by_username(held.username) or "") if held.username else "",
            kind=held.kind, mode=held.mode, run_id=run_id,
        )

    record = await run_db(_record)
    if record is None:
        return False
    kick()
    return True


async def count_limit_continuation(chat_id: str, session_id: str, person: str) -> None:
    """A continuation after a usage limit went out for a turn nobody watched
    (``chat_reload._resume``'s outcome): counted with this pass's own, as a
    settled record of the cause ``limit``, so restarts and limits share the
    bounds per chat (``CHAIN_MAX`` in a row, ``DAY_MAX`` a day). One that
    never went out (a person's message went first, the bound, a refusal)
    counts for nothing."""
    import psycopg

    from storage.pg import run_db
    try:
        await run_db(functools.partial(
            cut_records.add_settled, chat_id, cause=cut_records.CAUSE_LIMIT,
            state=cut_records.CONTINUED, reason="continued after a usage limit",
            session_id=session_id, person=person, kind=session_kind.DASHBOARD.name))
    except psycopg.Error:
        logger.warning("continue pass: chat=%s: its continuation after a limit was not counted",
                       chat_id[:8], exc_info=True)


async def arm_queues() -> int:
    """Boot: every chat with messages waiting in its queue is loaded and
    armed, so one whose author's page is back goes out with no chat opened.
    Returns how many chats were armed."""
    from core.events import input_queue
    from storage.pg import run_db
    chats = await run_db(task_store.chats_with_queued_input)
    for chat_id in chats:
        await input_queue.loaded(chat_id)
        input_queue.arm(chat_id)
    return len(chats)


async def settle_waiters() -> None:
    """Boot, after the records were passed: a waiting reload or move whose
    chat has no record left to settle it. One past its deadline is told so.
    A reload whose session lives on (no record, or a turn its machine still
    holds) is armed again. Anything else is told it did not happen."""
    from services.remote import chat_reload
    from storage.pg import run_db
    unsettled = await run_db(cut_records.unsettled_chats)
    for row in await run_db(pending_rebuilds.listed):
        if row["chat_id"] in unsettled:
            continue
        taken = await run_db(pending_rebuilds.take, row["chat_id"])
        if taken is not None:
            await chat_reload.settle_after_restart(taken, continued=False, lives_on=True)
