"""The turns a restart cut: their records at boot, and the chat's card.

Every pump turn, and every terminal's turn, has an open-turn row from its
start to its end (``storage/chat/open_turns.py``). A process that ends
without its shutdown, or a graceful one that cut a turn, leaves the row, so a
row the boot finds is a turn nobody finished. ``record_at_boot`` turns each
into a record (``storage/chat/cut_records.py``) that the continue pass works
from (``continue_pass.py``): a turn the platform ran itself is ``pending`` at
once, a turn on a machine is ``waiting`` until that machine says whether it
still holds the session. Unless the turn's engine had answered and no run
drives it (the pump only waited), or it was a meeting's (the failed meeting's
own row says what happened): those rows go and nothing is recorded.

A turn on a machine keeps its open-turn row while it waits: the replay of a
turn the machine still holds takes that row (``run_recovery``), early ranges
and all. Never a timer: a machine that has not reported keeps its records
waiting. After ``WAITING_LINE_S`` the chat is told once that the reply was
cut, and whether it can still continue by itself.

The card (``turn_ending.RESTARTED``: the reply was cut short, send a message
to continue) is written for a turn nothing continues or replays, with the
chat's abort flags, once.

Whoever holds a record decides what it becomes at the end of its turn: an
adoption's replay (``claim`` to ``replay_settled``) or the continue pass
(``hold`` to ``let_go``). A held turn its machine cuts goes back to the pass
from the holder's end (``hand_back``), read from the holder's own pump.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from datetime import datetime, timezone

from core.background import spawn
from storage import database as task_store
from storage.chat import cut_records, open_turns, pending_rebuilds

logger = logging.getLogger("otodock.recovery")

#: A terminal's turn, as the row and the record name its kind (a terminal is
#: no session kind: ``core/session/session_kind.py``).
KIND_TERMINAL = "terminal"

#: How long a turn waits for its machine before its chat is told so.
WAITING_LINE_S = 180.0
#: True for a machine that is away: nothing is decided for it until its
#: complete report.
WAITING_LINE = ("The reply was cut short when the platform restarted. This chat runs on a machine, "
                "and it continues by itself if that machine reports back within a day. "
                "Send a message to continue now.")
#: A turn a boot did not take up: the machine may still hold it, so it waits
#: too, and it never continues by itself.
WAITING_LINE_NOT_TAKEN_UP = ("The reply was cut short when the platform restarted. This chat runs "
                             "on a machine, and after this restart it does not continue by itself. "
                             "Send a message to continue now.")

# session id -> {"chat_id", "token", "machine_id", "record_id", "since",
# "told"}: the turns on a machine waiting for its report.
_parked: dict[str, dict] = {}

# record id -> its holder's entry: the cut turns a holder of this process has
# taken up again. An adoption's replay holds its record from ``claim`` to
# ``replay_settled`` (the parked entry, with ``by`` and ``pump``), the
# continue pass from before its claim to the end of its verdict (``hold``,
# ``let_go``). ``pump``: the pump that runs the held turn, once known. A
# turn its machine cuts goes back to the pass from the holder's end, never
# from the report's reader (``holder_of``).
_held: dict[int, dict] = {}
_BY_REPLAY = "replay"
_BY_PASS = "pass"

# This process's boot: a person's message after it means the chat moved on,
# and a closing row would land after that message.
_booted_at = datetime.now(timezone.utc).isoformat()


def _closing(cause: str = "") -> tuple[str, str, bool | None]:
    """The closing row's event type and payload, and the abort flag.
    ``cause``: the record's. A turn its machine's restart cut says so."""
    from core.events import turn_ending
    from core.events.stream_pump import turn_ended_block
    from ws import wire_events as wire
    ending = turn_ending.TurnEnding(
        reason=turn_ending.RESTARTED,
        detail=turn_ending.MACHINE if cause == cut_records.CAUSE_MACHINE else "")
    stamps = ending.abort_stamps()
    return (wire.SYSTEM, json.dumps(turn_ended_block(ending)),
            stamps["last_abort_graceful"] if stamps else None)


def close(chat_id: str, token: str) -> bool:
    """Write the closing row of the cut turn ``token`` and delete its
    open-turn row, unless a newer turn took the chat (``open_turns.close_cut``).
    Synchronous. True when the row was written."""
    event_type, event_data, graceful = _closing()
    written = open_turns.close_cut(chat_id, token, event_type, event_data,
                                   abort_graceful=graceful, since=_booted_at)
    if written:
        logger.info("chat=%s: the turn the restart cut got its closing row", chat_id[:8])
    return written


async def _on_lane(chat_id: str, job, label: str) -> None:
    from core.events import chat_writer
    try:
        await chat_writer.submit(chat_id, job, label=label)
    except Exception:  # one chat's close must not stop the others
        logger.exception("chat=%s: the closing row of the cut turn failed", chat_id[:8])


async def _write_waiting(record: dict) -> None:
    """The line of a turn that waits for its machine: no abort flags, since
    nothing has been decided, and a subtype of its own, so the card that may
    follow is still written."""
    from ws import wire_events as wire
    line = (WAITING_LINE_NOT_TAKEN_UP if record.get("cause") == cut_records.CAUSE_NOT_TAKEN_UP
            else WAITING_LINE)
    await _on_lane(record["chat_id"], functools.partial(
        open_turns.write_closing, record["chat_id"], wire.SYSTEM,
        json.dumps({"type": wire.SYSTEM, "subtype": wire.SUBTYPE_TURN_WAITING, "message": line}),
        abort_graceful=None, since=record.get("cut_at") or _booted_at,
        turn_started=record.get("started_at") or ""), "turn_waiting")


async def write_card(record: dict) -> None:
    """The card of a record nothing continues: the closing row on the chat's
    lane, unless a person wrote since the cut or the turn's own card is the
    chat's newest row."""
    event_type, event_data, graceful = _closing(record.get("cause") or "")
    await _on_lane(record["chat_id"], functools.partial(
        open_turns.write_closing, record["chat_id"], event_type, event_data,
        abort_graceful=graceful, since=record.get("cut_at") or _booted_at,
        turn_started=record.get("started_at") or ""), "turn_cut_close")


async def close_taken(chat_id: str, turn_started: str) -> None:
    """The closing row of a cut turn whose record the replay took and whose
    replay never began (``turn_started``: the record's start)."""
    event_type, event_data, graceful = _closing()
    await _on_lane(chat_id, functools.partial(
        open_turns.write_closing, chat_id, event_type, event_data,
        abort_graceful=graceful, since=_booted_at, turn_started=turn_started), "turn_cut_close")


def _park(record: dict, token: str) -> None:
    _parked[record["session_id"]] = {
        "chat_id": record["chat_id"], "token": token, "machine_id": record["machine_id"],
        "record_id": record["id"], "since": time.monotonic(), "told": False,
    }


def record_at_boot(*, clean_stop: bool, catches_up: bool) -> dict[str, int]:
    """Boot, before any machine can report: the last process's records are
    tidied (``cut_records.tidy_at_boot``) and every open-turn row becomes a
    record. A row whose engine turn had answered and that no run drives, or
    with no session (a meeting's), is deleted and nothing is recorded. A
    turn on a machine is copied and waits for that machine's report, its row
    left for the replay. Every other row is moved: the pass takes it.
    ``clean_stop``: the last process stopped by its own shutdown.
    ``catches_up`` (``platform_marks.Boot.catches_up``): false, and every
    unsettled record says it was not taken up, whichever boot reaches it.
    Returns the counts ``{"waiting", "pending", "ended"}``."""
    global _booted_at
    _booted_at = datetime.now(timezone.utc).isoformat()
    cut_records.tidy_at_boot()
    _parked.clear()
    kept: set[tuple[str, str]] = set()
    for record in cut_records.in_state(cut_records.WAITING):
        if record["session_id"]:
            _park(record, record["turn_token"])
            kept.add((record["chat_id"], record["turn_token"]))
    cause = cut_records.CAUSE_STOP if clean_stop else cut_records.CAUSE_CRASH
    counts = {"waiting": 0, "pending": 0, "ended": 0}
    for row in open_turns.list_open():
        chat_id, token, sid = row["chat_id"], row["turn_token"], row["session_id"] or ""
        if (chat_id, token) in kept:
            continue
        if not sid or not (row["engine_open"] or row.get("run_id")):
            open_turns.end_turn(chat_id, token)
            counts["ended"] += 1
            continue
        from services.scheduler import run_recovery
        chat = task_store.get_chat(chat_id) or {}
        machine = row.get("machine_id") or ""
        if not machine:
            # The one answer recovery parks a run on: the session's own record,
            # else the chat's place (a row an older release left, a session
            # with no context), so a chat on a machine waits for it rather than
            # be continued beside it.
            machine = run_recovery._session_machine(chat, sid)
        if machine and run_recovery.chat_runs_no_process(chat):
            # A turn of an engine with no process on the machine (Direct LLM)
            # has nothing there to wait for: it continues like the platform's.
            machine = ""
        if machine:
            copied = cut_records.copy_open({**row, "machine_id": machine}, cause=cause,
                                           state=cut_records.WAITING)
            if copied is not None:
                _park(copied, token)
                counts["waiting"] += 1
        elif cut_records.move_open(chat_id, token, cause=cause, state=cut_records.PENDING):
            counts["pending"] += 1
    # A chat cut again while the pass held it has its older record too: the
    # newest stays, or the chat would be continued twice.
    if cut_records.supersede_older():
        for sid, entry in list(_parked.items()):
            parked = cut_records.get(entry["record_id"])
            if parked is None or parked["state"] != cut_records.WAITING:
                _parked.pop(sid, None)
    if not catches_up:
        cut_records.mark_not_taken_up()
    return counts


def waiting(session_id: str) -> bool:
    """A cut turn of this session waits for its machine's report."""
    return session_id in _parked


async def stopped_by_person(record: dict) -> bool:
    """A person stopped the run this unsettled record names: the record is
    settled as dropped, in the one statement a reopen of the run also needs
    (``taken_for_reopen``), so one of the two wins. Then it waits for no
    machine any more, and the cut turn's open-turn row goes with no card:
    the Stop is the turn's end. False when the record was no longer
    unsettled (the reopen took it)."""
    from storage.pg import run_db
    if not await run_db(cut_records.settle, record["id"], cut_records.DROPPED,
                        cut_records.STOPPED_BY_PERSON):
        return False
    sid = record.get("session_id") or ""
    entry = _parked.pop(sid, None) if sid else None
    token = (entry or {}).get("token") or record.get("turn_token") or ""
    if token:
        await _on_lane(record["chat_id"], functools.partial(
            open_turns.end_turn, record["chat_id"], token), "turn_close")
    # Nothing continues the chat: its waiting reload or move is told (once:
    # a holder's later settle finds the row taken).
    await _settle_waiter(record["chat_id"], lives_on=False)
    return True


async def taken_for_reopen(record: dict) -> bool:
    """The continue pass reopens the run this record names: the record is
    settled as continued before the run is launched. False when a person's
    Stop settled it first (``stopped_by_person``): nothing is launched."""
    from storage.pg import run_db
    return bool(await run_db(cut_records.settle, record["id"], cut_records.CONTINUED,
                             "its run was reopened"))


def claim(session_id: str) -> dict | None:
    """An adoption replays this session's turn: its waiting entry, taken,
    and held for the replay until ``replay_settled``. Called before the
    adoption's first await, so no settle sees it."""
    entry = _parked.pop(session_id, None)
    if entry is not None:
        entry.update(by=_BY_REPLAY, pump=None)
        _held[entry["record_id"]] = entry
    return entry


def drives(entry: dict | None, pump) -> None:
    """The replay's pump, named before it starts: a machine that cuts it
    finds its holder by the pump itself (``holder_of``)."""
    if entry is not None:
        entry["pump"] = pump


def hold(record: dict) -> dict:
    """The continue pass takes this record (before its claim, so the record
    is never ``running`` with nobody held). Returns the entry ``let_go``
    takes back."""
    entry = {"record_id": record["id"], "chat_id": record["chat_id"], "by": _BY_PASS,
             "pump": None}
    _held[record["id"]] = entry
    return entry


def let_go(entry: dict | None) -> None:
    """A holder ends its hold: only its own entry, never a later holder's of
    the same record."""
    if entry is not None and holds(entry):
        del _held[entry["record_id"]]


def holds(entry: dict | None) -> bool:
    """This holder's entry still holds its record (not a later holder's of
    the same record)."""
    return entry is not None and _held.get(entry["record_id"]) is entry


def held(record_id: int) -> bool:
    """A holder of this process has the record: it moves it on at its end.
    Read by the lane gate on the store's executor (a dict lookup)."""
    return record_id in _held


def holder_of(pump) -> int:
    """The record whose holder runs the turn of ``pump``, 0 for none: a
    replay's own pump, or the turn the continue pass started for the chat
    whose record it holds (a continue's pump, by what started it). Asked by
    the report's reader while the pump still runs, before it queues the lost
    marker: the holder's end then decides from how the pump ended."""
    from ws import wire_events as wire
    for record_id, entry in _held.items():
        if entry["pump"] is pump:
            return record_id
    if getattr(pump, "driven_by", "") != wire.WAKE_SOURCE_CONTINUE:
        return 0
    for record_id, entry in _held.items():
        if (entry["by"] == _BY_PASS and entry["pump"] is None
                and entry["chat_id"] == getattr(pump, "chat_id", "")):
            entry["pump"] = pump
            return record_id
    return 0


async def ended(entry: dict | None, *, timeout: float = 30.0) -> None:
    """Wait for the held turn's pump to end, once the report's reader named
    it (``holder_of``): its ``cut_by_machine`` is final only then."""
    task = getattr((entry or {}).get("pump"), "_task", None)
    if task is not None and not task.done():
        await asyncio.wait({task}, timeout=timeout)


def cut_by_its_machine(entry: dict | None) -> bool:
    """The held turn ended lost because its machine came back without the
    session (the pump's own reading, never a person's Stop or an end)."""
    return entry is not None and bool(getattr(entry.get("pump"), "cut_by_machine", False))


async def hand_back(record_id: int, chat_id: str) -> bool:
    """A held turn its machine cut goes back to the continue pass: the record
    is ``pending`` again with the machine as its cause, and the pass is
    asked. The one move a holder makes back. False when the record was
    settled meanwhile (a person's Stop took it)."""
    from storage.pg import run_db
    if not await run_db(functools.partial(cut_records.make_pending, record_id, taken=True,
                                          cause=cut_records.CAUSE_MACHINE)):
        return False
    logger.info("chat=%s: its machine cut the turn that took record %s up again, back to the "
                "pass", chat_id[:8], record_id)
    from services.scheduler import continue_pass
    try:
        continue_pass.kick()
    except Exception:  # noqa: BLE001  # the record is the pass's now: the reaper's sweep takes it within its tick
        logger.warning("chat=%s: the pass was not asked for record %s", chat_id[:8], record_id,
                       exc_info=True)
    return True


async def replay_settled(entry: dict | None, *, began: bool) -> None:
    """A claimed entry's replay decided: it began (the machine still held
    the turn, which the replay writes), or it never did (the turn got its
    closing row). A replay its machine cut (its pump's own ending) settles
    nothing: the record goes back to the pass, which settles the chat's
    waiting reload with its verdict. When a person's Stop settled the record
    first, nothing goes back and the waiting reload is told here."""
    if entry is None:
        return
    from storage.pg import run_db
    try:
        if cut_by_its_machine(entry):
            if await hand_back(entry["record_id"], entry["chat_id"]):
                # The pass owns the run the record names from here.
                entry["handed_back"] = True
            else:
                await _settle_waiter(entry["chat_id"], lives_on=False)
            return
        if began:
            await run_db(cut_records.settle, entry["record_id"], cut_records.LEFT,
                         "its machine still held the turn")
        else:
            await run_db(cut_records.settle, entry["record_id"], cut_records.DROPPED,
                         "the replay of the turn never began")
        await _settle_waiter(entry["chat_id"], lives_on=began)
    finally:
        let_go(entry)


async def _settle_waiter(chat_id: str, *, lives_on: bool) -> None:
    """The chat's waiting reload or move, settled with its record: the turn
    its machine kept lets a reload wait again, anything else is told it did
    not happen."""
    from services.remote import chat_reload
    from storage.pg import run_db
    row = await run_db(pending_rebuilds.take, chat_id)
    if row is not None:
        await chat_reload.settle_after_restart(row, continued=False, lives_on=lives_on)


async def close_claimed(entry: dict | None) -> None:
    """A claimed entry whose replay did not start after all: its card."""
    if entry is not None:
        await _on_lane(entry["chat_id"], functools.partial(close, entry["chat_id"], entry["token"]),
                       "turn_cut_close")
        await replay_settled(entry, began=False)


async def machine_reported(report) -> list[int]:
    """A machine's complete report (``satellite_link.AliveReport``), after
    the adoption took what it takes: each turn of it still waiting gets its
    outcome. A session the report lists was kept, taken back idle or closed
    there by the adoption, and its turn is not replayed: the card. A
    terminal's turn: the card (its terminal is closed at the sign-in). A
    session the report does not list is gone: its record is the pass's
    (returned, now ``pending``), and its open-turn row goes. Only what was
    parked before this sign-in is judged, and a report that stopped being
    current decides nothing more."""
    from storage.pg import run_db
    gone: list[int] = []
    for sid in [s for s, e in _parked.items() if e["machine_id"] == report.machine_id]:
        entry = _parked[sid]
        if entry["since"] > report.signed_in_at:
            continue
        record = await run_db(cut_records.get, entry["record_id"]) or {}
        if not report.is_current():
            break
        if _parked.pop(sid, None) is None:
            continue
        if record.get("kind") == KIND_TERMINAL and _terminal_taken_back(sid, report.machine_id):
            # A claim took the terminal back with this record's row: its own
            # turn end closes the row, nothing is replayed and no card.
            await run_db(cut_records.settle, entry["record_id"], cut_records.LEFT,
                         "its machine still held the terminal")
            await _settle_waiter(entry["chat_id"], lives_on=True)
            continue
        if sid in report.headless or record.get("kind") == KIND_TERMINAL:
            await _on_lane(entry["chat_id"], functools.partial(
                close, entry["chat_id"], entry["token"]), "turn_cut_close")
            await run_db(cut_records.settle, entry["record_id"], cut_records.DROPPED,
                         "its machine kept or closed the session")
            await _settle_waiter(entry["chat_id"], lives_on=sid in report.headless)
            if record.get("run_id"):
                # An engine with no replay: its lane ended here, and its
                # delegator hears it beside this report (the machine's next
                # one waits for this hook).
                from services.scheduler import run_recovery
                spawn(run_recovery._fail_and_report(record["run_id"], entry["chat_id"],
                                                    run_recovery.RUN_CUT),
                      name="run_failed_report", owner="cut_turns")
        else:
            await _on_lane(entry["chat_id"], functools.partial(
                open_turns.end_turn, entry["chat_id"], entry["token"]), "turn_close")
            if await run_db(cut_records.make_pending, entry["record_id"]):
                gone.append(entry["record_id"])
    return gone


def _terminal_taken_back(session_id: str, machine_id: str) -> bool:
    """The record's terminal lives on the platform again, on its machine (a
    claim at this sign-in, ``services/remote/machine_sessions.py``)."""
    from core.session import interactive_session
    sess = interactive_session.get(session_id)
    return sess is not None and sess.alive and sess.target == machine_id


async def tell_waiting() -> None:
    """The chats whose turn has waited ``WAITING_LINE_S`` for its machine
    are told once. Nothing is decided: the record keeps waiting."""
    from storage.pg import run_db
    now = time.monotonic()
    for entry in [e for e in _parked.values()
                  if not e["told"] and now - e["since"] >= WAITING_LINE_S]:
        entry["told"] = True
        record = await run_db(cut_records.get, entry["record_id"])
        if record is not None:
            await _write_waiting(record)
