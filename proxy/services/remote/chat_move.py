"""Moving a chat to another place, with its memory.

A chat runs on the platform (``local``) or a paired machine; the person
picks another place from the chat's "Runs on" chip, the agent moves it on
request, or the platform moves it when another person drives a shared chat
whose place they may not use (the hand-over). The move takes the engine's
session files with it (``core/remote/session_files.py``) so the chat
resumes the SAME session there; when the files cannot travel (the source
offline, an older satellite, a failed transfer) it continues there from a
summary of the conversation (the reseed, ``core/session/history_seed.py``).

The sequence (``_run``): the chat is held (its input-queue claim and the
warmup registry, so no send, warmup or one-shot starts a turn meanwhile),
its live session closed, the files copied and committed on the target, the
chat rebound with a compare-and-set, a history row written, the viewers
told (``chat_moved``). The source keeps its copy until the TARGET proves it
resumed: the session file there grew past its copied size (``sweep``);
nothing is ever deleted on a weaker sign. Every refusal is one sentence.

The lock (``lock_sentence``): a chat whose place is a person's own machine,
with a live session there (or a move by that person that has not proved
yet, or a summarised one still waiting for its first turn), is read-only
for everyone else. The hand-over (``hand_over``) moves a Shared-only chat,
or a person's own chat on an admin machine their role cannot use, to the
sender's default before their message; any other chat whose session may
not run where it is answers with a sentence and stays.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from auth import roles
from core import placement
from services.remote import chat_targets
from storage import move_schema

logger = logging.getLogger("otodock.remote")

FINISH_TURN = "Finish or stop the current turn before moving the chat."
KIND_STAYS = "This kind of chat stays where it runs."
NOT_MOVER = "Only someone who can continue this chat can move it."
CANT_THERE = "You can't move this chat there."
ALREADY_THERE = "This chat already runs there."
DIRECT_LOCAL = f"Direct LLM chats run on {chat_targets.place_phrase(placement.LOCAL)}."
NOT_FOUND = "Chat not found."
CONTINUES_WORK = "This chat continues delegated work, so it stays where it runs."
VOICE_IN_USE = "A voice conversation is using this chat. End it before moving the chat."

#: How long a move row waits for the target's proof before it settles.
PROOF_WINDOW = timedelta(days=14)
#: How long the agent's scheduled move waits for the chat's turn to end.
TOOL_WAIT_S = 1800.0
#: The proof sweep's per-row back-off: the first wait and the longest one.
PROOF_BACKOFF_S = (60.0, 900.0)


class MoveRefused(Exception):
    """A move refused; the message is the sentence the person sees."""


@dataclass(frozen=True)
class Asker:
    """Who asks for a move: their sub, their effective role on the chat's
    agent and whether they are a platform admin."""

    sub: str
    role: str
    platform_admin: bool


#: The progress frames' pace: a frame when the copy moved this many points of
#: its total AND this long passed since the last one; a phase change and the
#: copy's end always go.
PROGRESS_STEP_PCT = 2.0
PROGRESS_MIN_INTERVAL_S = 0.5


@dataclass
class _Job:
    move_id: str
    chat_id: str
    to_label: str
    target: str
    #: The latest progress (``{done, total, phase}``), read by the chip's
    #: answer so a refetch mid-copy shows where the copy is.
    progress: dict | None = None

    @property
    def to_phrase(self) -> str:
        return chat_targets.place_phrase(self.target, self.to_label)


class _Progress:
    """A running move's ``chat_move_progress`` frames: every report kept on
    the job, a frame only at the pace above (a pull reports from the
    receive loop: this stays synchronous and quick)."""

    def __init__(self, job: _Job, chat: dict, actor: str, readers: set[str]) -> None:
        self.job, self.chat, self.actor, self.readers = job, chat, actor, readers
        self.last_pct: float | None = None
        self.last_at = 0.0
        self.last_phase = ""
        self.sent = 0

    def __call__(self, done: int, total: int, phase: str) -> None:
        self.job.progress = {"done": done, "total": total, "phase": phase}
        now = time.monotonic()
        pct = done * 100.0 / total if total else 0.0
        reached_end = total > 0 and pct >= 100.0 and (self.last_pct or 0.0) < 100.0
        if phase == self.last_phase and not reached_end and self.last_pct is not None and (
                pct - self.last_pct < PROGRESS_STEP_PCT
                or now - self.last_at < PROGRESS_MIN_INTERVAL_S):
            return
        self.last_pct, self.last_at, self.last_phase = pct, now, phase
        self.sent += 1
        from services.notifications import notification_manager
        notification_manager.broadcast_chat_move_progress(
            self.chat.get("user_sub") or "", self.chat.get("agent") or "", self.actor,
            {"chat_id": self.job.chat_id, "move_id": self.job.move_id,
             "to_label": self.job.to_label, "done": done, "total": total, "phase": phase},
            readers=self.readers)


# The moves running in this process, by chat (one per chat; the proxy is one
# process).
_running: dict[str, _Job] = {}


def moving(chat_id: str) -> bool:
    """A move of the chat runs in this process."""
    return chat_id in _running


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- what a chat is --------------------------------------------------------------

def home_user_of(chat: dict, person: chat_targets.RunPerson) -> str:
    """The scope home's user of the chat's session files (``""``: the
    workspace home): the session's mount identity."""
    from core.session import session_kind
    from core.session.visibility import resolve_visibility
    from storage import database as task_store
    cid, agent = chat.get("id") or "", chat.get("agent") or ""
    if session_kind.is_task_chat_id(cid):
        from core.config.task_config_builder import rewarm_task_identity
        ident = rewarm_task_identity(cid, agent)
        return ident.username if ident else ""
    username = task_store.get_username_by_sub(person.sub) or "" if person.sub else ""
    return resolve_visibility(agent, username=username, user_sub=person.sub or "").mount_username


def _movable_kind(chat: dict) -> bool:
    from core.session import session_kind
    kind = session_kind.of_chat(chat)
    return kind in (session_kind.DASHBOARD, session_kind.TASK) or session_kind.is_task_chat_id(
        chat.get("id") or "")


def _continues_delegated_work(chat: dict) -> bool:
    """A delegated worker chat (its delegator continues it with
    ``continue_id``) or a chat a task continues: the next round runs where
    the task runs, not where the chat is pinned, and resumes the session
    there. Synchronous."""
    if chat.get("parent_chat_id"):
        return True
    from storage import remote_store
    return remote_store.chat_continued_by_task(chat)


def _server_only(chat: dict) -> bool:
    """A chat whose engine never runs on a machine (Direct LLM)."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(chat_targets.chat_engine(chat))
    return caps is not None and not caps.runtime.supports_remote_execution


def may_drive(chat: dict, asker: Asker) -> bool:
    """Whether the asker may drive (and so move) the chat."""
    from core.session import session_kind, visibility
    cid = chat.get("id") or ""
    if session_kind.is_task_chat_id(cid):
        from storage import database as task_store
        run = task_store.get_run(session_kind.run_id_of_chat(cid))
        if not run:
            return False
        return visibility._task_continue_allowed(run, effective_role=asker.role, user_sub=asker.sub)
    owner = chat.get("user_sub") or ""
    if visibility.is_shared_chat_owner(owner):
        return roles.can_edit(asker.role)
    return owner == asker.sub or asker.platform_admin


def _session_live(chat: dict) -> bool:
    """A live session of the chat right now: a terminal, or a headless
    session (a remote one held through its reconnect grace counts)."""
    from core.session import interactive_session
    sid = chat.get("session_id") or ""
    if interactive_session.find_live_for_chat(chat.get("id") or "") is not None:
        return True
    if not sid:
        return False
    pin = chat.get("execution_target") or ""
    if placement.is_local(pin):
        from core.session.session_manager import find_layer_for_session
        return find_layer_for_session(sid) is not None
    from core.remote.satellite_connection import get_connection_manager
    from core.session.session_manager import _get_remote_layer
    layer = _get_remote_layer()
    machine = placement.machine_of(pin)
    held = sid in (layer.session_ids_on(machine) if hasattr(layer, "session_ids_on") else ())
    return ((held and get_connection_manager().is_connected(machine))
            or bool(getattr(layer, "is_session_grace_held", lambda _s: False)(sid)))


def lock_sentence(chat: dict, person_sub: str) -> str:
    """The lock: "This chat is running on <name>'s computer." when the chat's
    place is another person's own machine and either its session is live
    there or that person's move to it has not proved yet; ``""`` otherwise.
    Synchronous (reads the machine row)."""
    pin = chat.get("execution_target") or ""
    if placement.is_local(pin):
        return ""
    from storage import remote_store
    machine = remote_store.get_remote_machine(placement.machine_of(pin))
    if not machine or placement.machine_is_admin_paired(machine):
        return ""
    owner = machine.get("registered_by") or ""
    if not owner or owner == person_sub:
        return ""
    locked = _session_live(chat)
    if not locked:
        last = remote_store.latest_chat_move(chat.get("id") or "") or {}
        state = last.get("state") or ""
        waiting = (state in (move_schema.MOVING, move_schema.AWAITING_RESUME)
                   # A summarised move has no session there until its first turn.
                   or (state == move_schema.SETTLED
                       and last.get("memory") == move_schema.MEMORY_SUMMARY))
        if waiting and last.get("target") == pin and last.get("actor") == owner:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"])
            except (KeyError, ValueError):
                age = timedelta(0)
            locked = age.total_seconds() < _idle_timeout_s()
    if not locked:
        return ""
    return f"This chat is running on {chat_targets.owner_name(owner)}'s computer."


def _idle_timeout_s() -> float:
    """The admin's session idle timeout (the idle reaper's knob). Sync: the
    caller runs on the DB executor."""
    import config
    try:
        return float(config.get_idle_timeout())
    except Exception:  # noqa: BLE001  # the lock window falls back to the default
        return float(getattr(config, "PERSISTENT_SESSION_TIMEOUT", 900) or 900)


def open_turn(chat: dict) -> bool:
    """A turn open on the chat: a pump running, a warmup in flight, a prompt
    waiting, a one-shot delivery running, a terminal turn, an active run."""
    from core.events.stream_pump import _active_pumps
    from core.session import (
        interactive_session,
        session_delivery,
        session_kind,
        warmup_registry,
    )
    from core.session.session_state import has_pending_prompt
    cid = chat.get("id") or ""
    sid = chat.get("session_id") or ""
    pump = _active_pumps.get(cid)
    if pump is not None and not pump.is_done:
        return True
    if warmup_registry.get(cid) is not None or session_delivery.oneshot_inflight(cid) is not None:
        return True
    if sid and has_pending_prompt(sid):
        return True
    isess = interactive_session.find_live_for_chat(cid)
    if isess is not None and isess.turn_open:
        return True
    if session_kind.is_task_chat_id(cid):
        from storage import database as task_store
        from storage.automation import run_status
        run = task_store.get_run(session_kind.run_id_of_chat(cid)) or {}
        if run.get("status") in run_status.LIVE:
            return True
    return False


# -- the chip's answer -----------------------------------------------------------

def targets_view(chat: dict, asker: Asker, *, blocked: str = "") -> dict:
    """The chip's answer for ``chat`` as ``asker`` sees it; ``blocked`` is a
    socket's own refusal (a voice conversation on the chat). Synchronous."""
    from storage import remote_store
    cid = chat.get("id") or ""
    engine = chat_targets.chat_engine(chat)
    person = chat_targets.run_person_of_chat(chat, asker.sub, asker.role)
    default, targets = chat_targets.offered(chat.get("agent") or "", person, engine_path=engine,
                                            admin_asker=asker.platform_admin)
    current = chat_targets.current_target(chat)
    entries = [t.view() for t in targets]
    known = {t.target for t in targets}
    cur_machine = (remote_store.get_remote_machine(current)
                   if not placement.is_local(current) else None)
    from services.remote import remote_status
    cur_state = (remote_status.get_live_machine_status(current, machine=cur_machine)["state"]
                 if cur_machine else "")
    cur = {"target": current, "label": chat_targets.label_of(current, cur_machine or {}),
           "pairing": ("" if cur_machine is None else
                       placement.PAIRING_ADMIN if placement.machine_is_admin_paired(cur_machine)
                       else placement.PAIRING_USER),
           "state": cur_state,
           "owner": (chat_targets.owner_name(cur_machine.get("registered_by") or "")
                     if cur_machine and not placement.machine_is_admin_paired(cur_machine) else "")}
    for e in entries:
        e["is_current"] = e["target"] == current
    if current not in known:
        entries.insert(0, {"target": current, "label": cur["label"], "pairing": cur["pairing"],
                           "state": cur_state, "reachable": cur_state in remote_status.REACHABLE,
                           "is_default": False, "pickable": False, "why_not": "",
                           "allowed": True, "moves_files": chat_targets.moves_files(current),
                           "is_current": True})
    refusal = ""
    if not _movable_kind(chat):
        refusal = KIND_STAYS
    elif _server_only(chat):
        refusal = DIRECT_LOCAL
    elif not may_drive(chat, asker):
        refusal = NOT_MOVER
    elif _continues_delegated_work(chat):
        refusal = CONTINUES_WORK
    elif blocked:
        refusal = blocked
    lock = lock_sentence(chat, asker.sub)
    job = _running.get(cid)
    last = remote_store.latest_chat_move(cid)
    chosen = bool(last and last.get("target") == current
                  and last.get("state") not in (move_schema.FAILED,))
    return {"current": cur, "default_target": default, "targets": entries,
            "can_move": not refusal and not lock, "refusal": refusal or lock,
            "lock": {"sentence": lock} if lock else None,
            "moving": {"to_label": job.to_label, "progress": job.progress} if job else None,
            "place_chosen": chosen}


# -- the move ----------------------------------------------------------------------

def _validate(chat: dict, wanted: str, asker: Asker, *, expect_from: str | None,
              check_turn: bool) -> tuple[chat_targets.Target, chat_targets.RunPerson]:
    """The synchronous checks; ``MoveRefused`` with the sentence."""
    if not _movable_kind(chat):
        raise MoveRefused(KIND_STAYS)
    if not may_drive(chat, asker):
        raise MoveRefused(NOT_MOVER)
    if _continues_delegated_work(chat):
        raise MoveRefused(CONTINUES_WORK)
    lock = lock_sentence(chat, asker.sub)
    if lock:
        raise MoveRefused(lock)
    cid = chat.get("id") or ""
    job = _running.get(cid)
    if job is not None:
        raise MoveRefused(f"This chat is moving to {job.to_phrase}.")
    current = chat_targets.current_target(chat)
    if expect_from is not None and (placement.machine_of(expect_from) or placement.LOCAL) != current:
        raise MoveRefused(f"This chat just moved to {chat_targets.place_phrase(current)}.")
    engine = chat_targets.chat_engine(chat)
    person = chat_targets.run_person_of_chat(chat, asker.sub, asker.role)
    if _server_only(chat):
        raise MoveRefused(DIRECT_LOCAL)
    _default, targets = chat_targets.offered(chat.get("agent") or "", person, engine_path=engine,
                                             admin_asker=asker.platform_admin)
    hit = chat_targets.find(targets, wanted)
    if hit is None:
        raise MoveRefused(CANT_THERE)
    if hit.target == current:
        raise MoveRefused(ALREADY_THERE)
    if not hit.pickable:
        raise MoveRefused(hit.why_not or CANT_THERE)
    if check_turn and open_turn(chat):
        raise MoveRefused(FINISH_TURN)
    return hit, person


def _expected_memory(chat: dict, target: str | None = None) -> str:
    """What the move should carry, as the chip promises it: the files when
    the chat has a session there, the current place is reachable (or the
    platform) and, given the ``target``, both places' satellites move them,
    else a summary."""
    if not (chat.get("session_id") or chat.get("codex_thread_id")):
        return move_schema.MEMORY_SUMMARY
    current = chat_targets.current_target(chat)
    if target is not None and _old_satellite(current, target):
        return move_schema.MEMORY_SUMMARY
    if placement.is_local(current):
        return move_schema.MEMORY_FILES
    from services.remote.remote_status import is_reachable
    return move_schema.MEMORY_FILES if is_reachable(current) else move_schema.MEMORY_SUMMARY


async def request_move(chat_id: str, wanted: str, *, asker: Asker,
                       expect_from: str | None = None, origin: str = move_schema.ORIGIN_CHIP,
                       blocked: Callable[[str], str] | None = None) -> dict:
    """Validate a move and start it. Returns ``{move_id, to_label, memory}``;
    ``MoveRefused`` with the sentence. ``blocked`` lets a socket add a reason
    of its own (a voice bridge attached to the chat)."""
    from storage import database as task_store
    from storage.pg import run_db
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat:
        raise MoveRefused(NOT_FOUND)
    if blocked is not None and (why := blocked(chat_id)):
        raise MoveRefused(why)
    hit, person = await run_db(lambda: _validate(chat, wanted, asker, expect_from=expect_from,
                                                 check_turn=True))
    return await _start(chat, hit, person, asker=asker, origin=origin)


async def _start(chat: dict, hit: chat_targets.Target, person: chat_targets.RunPerson, *,
                 asker: Asker, origin: str, claim: object | None = None,
                 resume: bool = False) -> dict:
    """Hold the chat, write the row, start the job (``resume``: the agent's
    move of its own chat, which continues there by itself)."""
    from core.events import input_queue
    from core.session import warmup_registry
    from storage import remote_store
    from storage.pg import run_db
    cid = chat["id"]
    if claim is None:
        q = await input_queue.loaded(cid)
        claim = q.try_claim()
        if claim is None:
            raise MoveRefused(f"This chat is moving to {_running[cid].to_phrase}."
                              if cid in _running else FINISH_TURN)
    else:
        q = await input_queue.loaded(cid)
    move_id = str(uuid.uuid4())
    job = _Job(move_id, cid, hit.label, hit.target)
    if cid in _running:
        q.release(claim)
        raise MoveRefused(f"This chat is moving to {_running[cid].to_phrase}.")
    if warmup_registry.get(cid) is not None:
        # A person's warmup holds no claim: registering would share its entry
        # and the move's end would remove it.
        q.release(claim)
        raise MoveRefused(FINISH_TURN)
    _running[cid] = job
    try:
        await warmup_registry.register(cid, asker.sub, chat.get("agent") or "")
        fresh = await run_db(_get_chat, cid)
        if not fresh or _turn_open_while_held(fresh):
            raise MoveRefused(FINISH_TURN)
        memory = _expected_memory(fresh)
        home_user = await run_db(home_user_of, fresh, person)
        current = chat_targets.current_target(fresh)
        if _old_satellite(current, hit.target):
            memory = move_schema.MEMORY_SUMMARY
        await run_db(remote_store.create_chat_move, {
            "id": move_id, "chat_id": cid, "agent": fresh.get("agent") or "",
            "engine": chat_targets.chat_engine(fresh),
            "session_id": fresh.get("session_id") or "",
            "resume_handle": fresh.get("codex_thread_id") or "",
            "home_user": home_user, "source": current, "target": hit.target,
            "from_label": chat_targets.label_of(current), "to_label": hit.label,
            "state": move_schema.MOVING, "actor": asker.sub, "origin": origin,
            "created_at": _now(),
        })
    except BaseException:
        _running.pop(cid, None)
        await warmup_registry.unregister(cid)
        q.release(claim)
        raise
    from core import background
    background.spawn(_run(job, fresh, hit, person, claim, origin=origin, asker=asker,
                          resume=resume),
                     name=f"chat-move-{move_id[:8]}", owner="chat_move")
    return {"move_id": move_id, "to_label": hit.label, "memory": memory,
            "to_phrase": chat_targets.place_phrase(hit.target, hit.label)}


def _get_chat(chat_id: str) -> dict | None:
    from storage import database as task_store
    return task_store.get_chat(chat_id)


def _turn_open_while_held(chat: dict) -> bool:
    """``open_turn`` minus the warmup registry (the move itself holds it)."""
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session, session_delivery
    from core.session.session_state import has_pending_prompt
    cid, sid = chat.get("id") or "", chat.get("session_id") or ""
    pump = _active_pumps.get(cid)
    if pump is not None and not pump.is_done:
        return True
    if session_delivery.oneshot_inflight(cid) is not None or (sid and has_pending_prompt(sid)):
        return True
    isess = interactive_session.find_live_for_chat(cid)
    return bool(isess is not None and isess.turn_open)


async def _close_live(chat: dict) -> None:
    """Close the chat's live session where it runs (idle by the gates); a
    machine that is not connected is sent nothing."""
    from core.session import interactive_session
    cid, sid = chat["id"], chat.get("session_id") or ""
    if interactive_session.find_live_for_chat(cid) is not None:
        await interactive_session.close_for_chat(cid, reason="moved")
        return
    if not sid:
        return
    pin = chat.get("execution_target") or ""
    try:
        if placement.is_local(pin):
            from core.session.session_manager import find_layer_for_session
            layer = find_layer_for_session(sid)
            if layer is not None:
                await layer.close_session(sid)
            return
        from core.remote.satellite_connection import get_connection_manager
        from core.session.session_manager import _get_remote_layer
        machine = placement.machine_of(pin)
        if get_connection_manager().is_connected(machine):
            # A machine that is not connected is sent nothing: its record
            # goes with the reaper's next pass.
            await _get_remote_layer().close_session(sid)
    except Exception:  # noqa: BLE001  # a session that will not close is replaced either way
        logger.warning("chat move: closing session %s failed", sid[:8], exc_info=True)


def _old_satellite(source: str, target: str) -> str:
    """The ``why`` of a move whose files a place's satellite cannot move
    (``chat_targets.moves_files``), naming that place; ``""`` when both can."""
    old_source = not chat_targets.moves_files(source)
    old_target = not chat_targets.moves_files(target)
    if old_source and old_target:
        return move_schema.WHY_OLD_SATELLITE
    if old_source:
        return move_schema.WHY_OLD_SOURCE
    return move_schema.WHY_OLD_TARGET if old_target else ""


def _reachable(place: str) -> bool:
    if placement.is_local(place):
        return True
    from services.remote.remote_status import is_reachable
    return is_reachable(place)


async def _run(job: _Job, chat: dict, hit: chat_targets.Target, person: chat_targets.RunPerson,
               claim: object, *, origin: str, asker: Asker | None = None,
               resume: bool = False) -> None:
    """The move job: close, copy (or summarise), rebind, record, tell; then,
    for the agent's move of its own chat (``resume``), the chat continues on
    the new place by itself (``_resume_after_tool_move``)."""
    from core.events import input_queue
    from core.remote import session_files
    from core.session import warmup_registry
    from storage import remote_store
    from storage.pg import run_db
    cid, move_id, target = job.chat_id, job.move_id, hit.target
    row = await run_db(remote_store.get_chat_move, move_id) or {}
    source = row.get("source") or placement.LOCAL
    from_label, to_label = row.get("from_label") or "", hit.label
    fp = chat_targets.place_phrase(source, from_label)
    tp = chat_targets.place_phrase(target, to_label)
    scope = session_files.SessionScope(
        agent=chat.get("agent") or "", engine=row.get("engine") or "",
        home_user=row.get("home_user") or "", session_id=row.get("session_id") or "",
        resume_handle=row.get("resume_handle") or "")
    was_target = str(chat.get("execution_target") or "")
    was_sid, was_thread = chat.get("session_id"), chat.get("codex_thread_id")
    ok, memory, why, message = False, "", "", ""
    busy_at = ""
    try:
        await _close_live(chat)
        result = None
        if not scope.refs:
            why = move_schema.WHY_NO_FILES
        elif not _reachable(source):
            why = move_schema.WHY_OFFLINE
        elif old := _old_satellite(source, target):
            why = old
        else:
            progress = _Progress(job, chat, row.get("actor") or "", await _readers(chat))
            try:
                result = await session_files.copy_session(scope, source=source, target=target,
                                                          move_id=move_id, progress=progress)
            except Exception as e:  # noqa: BLE001  # every failure cleans up, then falls back
                why = getattr(e, "why", move_schema.WHY_TRANSFER_FAILED)
                if why == move_schema.WHY_BUSY:
                    busy_at = getattr(e, "place", "") or source
                logger.warning("chat move %s: the files did not move (%s): %s", move_id[:8], why, e)
                # The platform's own staging (a pull from a machine lands
                # there whatever the target), then each machine's.
                await asyncio.to_thread(session_files.local_discard, scope, move_id)
                for place in (source, target):
                    if placement.is_local(place) or not _reachable(place):
                        continue
                    await session_files.discard(place, scope, move_id)
                if getattr(e, "committed", False) and _reachable(target):
                    # A copy that failed at or after its commit leaves the
                    # session on the target under ids no chat resumes there.
                    try:
                        await session_files.delete_files(target, scope)
                    except Exception as de:  # noqa: BLE001  # the target keeps an unused copy
                        logger.info("chat move %s: nothing removed on the target: %s",
                                    move_id[:8], de)
        if result is not None:
            if await run_db(lambda: remote_store.rebind_chat_with_files(
                    cid, target, expect_target=was_target, expect_session_id=was_sid,
                    expect_thread=was_thread)):
                ok, memory = True, move_schema.MEMORY_FILES
                await run_db(lambda: remote_store.update_chat_move(
                    move_id, state=move_schema.AWAITING_RESUME, memory=memory,
                    files=len(result.files), bytes=result.bytes, main_rel=result.main_rel,
                    main_size=result.main_size))
                message = f"This chat moved from {fp} to {tp}."
                await run_db(_history_row, cid, message, "")
                logger.info("chat move %s: chat=%s %s -> %s by %s (%s): memory travelled "
                            "(%d files, %d bytes)", move_id[:8], cid[:8], from_label, to_label,
                            row.get("actor", "")[:16], origin, len(result.files), result.bytes)
            else:
                await session_files.delete_files(target, scope)
                message = "This chat changed while it moved, so it stays where it was."
        elif busy_at:
            # The files are intact: a session still holds them, so the chat
            # stays with its memory instead of moving on a summary.
            message = (f"This chat is busy on {fp}, so it stays there. Finish or stop "
                       "its turn there, then move it again." if busy_at == source else
                       f"A session is still open on {tp}, so the chat stays on "
                       f"{fp}. Close it there, then move the chat again.")
        elif not _reachable(target):
            message = (f"The move to {tp} did not finish: {tp} went offline. "
                       f"The chat stays on {fp}.")
        elif await run_db(lambda: remote_store.rebind_chat_to_target(
                cid, target, move_id, expect_target=was_target, expect_session_id=was_sid,
                expect_thread=was_thread)):
            ok, memory = True, move_schema.MEMORY_SUMMARY
            await run_db(lambda: remote_store.update_chat_move(
                move_id, state=move_schema.SETTLED, memory=memory, why=why))
            message = summary_notice(from_label, to_label, why, source=source, target=target)
            logger.info("chat move %s: chat=%s %s -> %s by %s (%s): memory summarised (%s)",
                        move_id[:8], cid[:8], from_label, to_label, row.get("actor", "")[:16],
                        origin, why)
        else:
            message = "This chat changed while it moved, so it stays where it was."
    except Exception:
        logger.exception("chat move %s failed", move_id[:8])
        message = f"The move to {tp} did not finish. The chat stays on {fp}."
    finally:
        if not ok:
            await run_db(lambda: remote_store.update_chat_move(move_id, state=move_schema.FAILED,
                                                                why=why))
            await run_db(_history_row, cid, message, "failed")
            logger.info("chat move %s: chat=%s did not move: %s", move_id[:8], cid[:8], message)
        await _forget_resume_probe(chat)
        _running.pop(cid, None)
        await warmup_registry.unregister(cid)
        q = await input_queue.loaded(cid)
        q.release(claim, rearm=True)
        await _broadcast(chat, row.get("actor") or "", {
            "chat_id": cid, "move_id": move_id, "ok": ok, "memory": memory,
            "new_target": target if ok else (chat.get("execution_target") or placement.LOCAL),
            "label": to_label, "from_label": from_label, "message": message})
    if ok and resume and asker is not None:
        from core import background
        background.spawn(_resume_after_tool_move(job, asker), name=f"chat-resume-{move_id[:8]}",
                         owner="chat_move")


def _phrases(from_label: str, to_label: str, source: str | None,
             target: str | None) -> tuple[str, str, str]:
    """The two places as they read in a sentence, and the first one opening
    one; a label with no target (an unreadable row) is used as it is."""
    if source is None:
        fp = fp_start = from_label
    else:
        fp = chat_targets.place_phrase(source, from_label)
        fp_start = chat_targets.place_phrase(source, from_label, start=True)
    tp = to_label if target is None else chat_targets.place_phrase(target, to_label)
    return fp, fp_start, tp


def returned_notice(from_label: str, to_label: str, *, source: str | None = None,
                    target: str | None = None) -> str:
    """The card's words for a chat the removal of its machine sent back."""
    fp, _start, tp = _phrases(from_label, to_label, source, target)
    return (f"This chat came back to {fp} because {tp} was removed. Its turns "
            f"on {tp} were restored from chat history. Files from {tp} are not "
            "available.")


def summary_notice(from_label: str, to_label: str, why: str, *, source: str | None = None,
                   target: str | None = None) -> str:
    """The reseed card's words for a move that could not take the files."""
    fp, fp_start, tp = _phrases(from_label, to_label, source, target)
    tp_start = (to_label if target is None
                else chat_targets.place_phrase(target, to_label, start=True))
    reason = {
        move_schema.WHY_OFFLINE: f"{fp_start} was offline",
        move_schema.WHY_OLD_SOURCE: f"{fp_start} runs an older satellite",
        move_schema.WHY_OLD_TARGET: f"{tp_start} runs an older satellite",
        move_schema.WHY_OLD_SATELLITE: f"{fp_start} and {tp} run an older satellite",
        move_schema.WHY_NO_FILES: "The previous session left no file to take",
    }.get(why, "Its files could not be copied")
    return (f"This chat moved from {fp} to {tp}. {reason}, so it continues from a summary of "
            "the conversation.")


def _history_row(chat_id: str, message: str, reason: str) -> None:
    from storage import database as task_store
    from ws import wire_events as wire
    task_store.add_chat_message(
        chat_id, "event", "", event_type=wire.SYSTEM,
        event_data=json.dumps({"type": wire.SYSTEM, "subtype": wire.SUBTYPE_CHAT_MOVED,
                               "message": message, "reason": reason}),
    )


async def _forget_resume_probe(chat: dict) -> None:
    sid = chat.get("session_id") or ""
    if not sid:
        return
    try:
        from core.remote import remote_resume
        from core.session.session_manager import _get_remote_layer
        _get_remote_layer().forget_resume_probe(sid, agent_name=chat.get("agent") or "")
        remote_resume.forget_resume_target(sid)
    except Exception:  # noqa: BLE001  # the cache only speeds a probe up
        logger.debug("chat move: resume probe cache not cleared", exc_info=True)


async def _readers(chat: dict) -> set[str]:
    """The people with a tab focused on the chat who may read it: a focus is
    the client's word, so each one's access is checked before a move frame
    reaches them outside the chat's own audience."""
    from services.notifications import notification_manager
    focused = notification_manager.chat_focused_subs(chat.get("id") or "")
    if not focused:
        return set()

    def _job() -> set[str]:
        from auth.providers import user_context_for_sub
        from core.session.visibility import can_access_chat
        out = set()
        for sub in focused:
            u = user_context_for_sub(sub)
            if u is not None and can_access_chat(u, chat):
                out.add(sub)
        return out

    from storage.pg import run_db
    return await run_db(_job)


async def _broadcast(chat: dict, actor: str, frame: dict) -> None:
    from services.notifications import notification_manager
    notification_manager.broadcast_chat_moved(
        chat.get("user_sub") or "", chat.get("agent") or "", actor, frame,
        readers=await _readers(chat))


# -- the agent's move ------------------------------------------------------------

async def schedule_after_turn(chat_id: str, wanted: str, *, asker: Asker,
                              blocked: Callable[[str], str] | None = None) -> dict:
    """The agent's tool, called inside the chat's own turn: validate now
    (the turn aside), then wait for the chat's queue claim (the turn's end)
    and move. Returns ``{to_label, memory}``."""
    from storage import database as task_store
    from storage.pg import run_db
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat:
        raise MoveRefused(NOT_FOUND)
    if blocked is not None and (why := blocked(chat_id)):
        raise MoveRefused(why)
    hit, person = await run_db(lambda: _validate(chat, wanted, asker, expect_from=None,
                                                 check_turn=False))
    memory = await run_db(_expected_memory, chat, hit.target)
    from core import background
    earlier = _waiters.pop(chat_id, None)
    if earlier is not None and not earlier.done():
        earlier.cancel()
        logger.info("chat move: chat=%s: the agent's later move replaces its earlier one",
                    chat_id[:8])
    waiter = background.spawn(_wait_and_move(chat_id, hit.target, asker, blocked, resume=True),
                              name="chat-move-wait", owner="chat_move")
    _waiters[chat_id] = waiter
    waiter.add_done_callback(lambda t: _waiters.pop(chat_id, None) if _waiters.get(chat_id) is t
                             else None)
    return {"to_label": hit.label, "memory": memory,
            "to_phrase": chat_targets.place_phrase(hit.target, hit.label)}


async def _wait_and_move(chat_id: str, target: str, asker: Asker,
                         blocked: Callable[[str], str] | None = None, *,
                         resume: bool = False) -> None:
    """Wait for the chat's turn to end, then move it; ``resume``: the chat
    then continues there by itself. The claim alone is not the end: a
    terminal turn, a task run and a person's warmup hold no claim, so the
    claim is given back while one of them is open (``open_turn``) and asked
    for again."""
    from core.events import input_queue
    from storage import database as task_store
    from storage.pg import run_db
    deadline = time.monotonic() + TOOL_WAIT_S
    while time.monotonic() < deadline:
        try:
            claim = await input_queue.claim_when_free(
                chat_id, timeout=max(0.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            break
        q = await input_queue.loaded(chat_id)
        chat = await run_db(task_store.get_chat, chat_id)
        if chat and await run_db(open_turn, chat):
            q.release(claim, rearm=True)
            await asyncio.sleep(TURN_POLL_S)
            continue
        try:
            if not chat:
                raise MoveRefused(NOT_FOUND)
            if blocked is not None and (why := blocked(chat_id)):
                raise MoveRefused(why)
            hit, person = await run_db(functools.partial(
                _validate, chat, target, asker, expect_from=None, check_turn=False))
            await _start(chat, hit, person, asker=asker, origin=move_schema.ORIGIN_TOOL,
                         claim=claim, resume=resume)
            return
        except MoveRefused as e:
            q.release(claim, rearm=True)
            if str(e) == FINISH_TURN:
                await asyncio.sleep(TURN_POLL_S)
                continue
            await run_db(_history_row, chat_id, f"The chat did not move: {e}", "failed")
            return
    label = await run_db(chat_targets.label_of, target)
    await run_db(_history_row, chat_id,
                 f"The move to {chat_targets.place_phrase(target, label)} did not happen: "
                 "the turn did not end within 30 minutes.", "failed")


# -- the resume after the agent's own move ---------------------------------------

#: The agent's own moves of a chat in a row, with no message from a person,
#: after which the platform stops resuming it by itself; the same number per
#: chat in one hour, counted in this process, whatever the rows say.
RESUME_CHAIN_MAX = 10
RESUME_WINDOW_S = 3600.0
#: How often the agent's move asks again whether the chat's turn ended (a
#: terminal turn, a task run, a person's warmup: none of them holds a claim).
TURN_POLL_S = 2.0

# Chat id -> the monotonic times of its recent resumes.
_recent_resumes: dict[str, list[float]] = {}
# Chat id -> the agent's move waiting for the chat's turn to end.
_waiters: dict[str, asyncio.Task] = {}

_WHY_LOCKED = "the chat is read-only for its person there"
_WHY_BLOCKED = "its person may not run there"


def resume_nudge(target: str, label: str) -> str:
    """The resumed turn's prompt: the platform's words, one line."""
    return f"You now run on {chat_targets.place_phrase(target, label)}. Continue the work."


def _turn_holds_chat(chat_id: str) -> bool:
    """A turn holds the chat: a pump, a claim, a warmup or a live terminal."""
    from core.events import input_queue
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session, warmup_registry
    pump = _active_pumps.get(chat_id)
    return bool((pump is not None and not pump.is_done) or input_queue.is_claimed(chat_id)
                or warmup_registry.get(chat_id) is not None
                or interactive_session.find_live_for_chat(chat_id) is not None)


def _person_took_chat(chat_id: str) -> bool:
    """A person's turn holds the chat, or their messages wait for it."""
    from core.events import input_queue
    return _turn_holds_chat(chat_id) or bool(input_queue.peek(chat_id))


def _resume_bounds(chat: dict, job: _Job, asker: Asker) -> str:
    """Why the chat does not continue by itself now (``""``: it does), as
    the log names it. Synchronous."""
    from storage import remote_store
    cid = job.chat_id
    if chat_targets.current_target(chat) != job.target:
        return "the chat is elsewhere now"
    latest = remote_store.latest_chat_move(cid) or {}
    if latest.get("id") != job.move_id or moving(cid):
        return "a newer move of the chat"
    run_sub, _run_role = _builds_as(chat, asker.sub, asker.role)
    if lock_sentence(chat, run_sub or ""):
        return _WHY_LOCKED
    if placement_blocked(chat, asker.sub, asker.role):
        return _WHY_BLOCKED
    return ""


def _waiting_message_goes_first(chat: dict) -> bool:
    """A person's message waits in the chat's queue and will go out: its
    author may drive the chat where it runs now (one the lock sends back to
    its author holds nothing). Synchronous."""
    from core.events import input_queue
    return any(not lock_sentence(chat, qi.author_sub or chat.get("user_sub") or "")
               for qi in input_queue.peek(chat.get("id") or ""))


def _skipped_sentence(job: _Job) -> str:
    return (f"The agent did not continue on {chat_targets.place_phrase(job.target, job.to_label)} "
            "on its own. Send a message to continue there.")


async def _resume_after_tool_move(job: _Job, asker: Asker) -> None:
    """The agent moved its own chat: the chat continues on the new place by
    itself, one turn of the platform's words (``resume_nudge``), as the
    session that called the tool and in the chat's own mode, through the
    wake ladder's rungs that start a session there (never a viewer's socket).
    A person's message waiting in the queue goes first instead, and after
    ``RESUME_CHAIN_MAX`` moves in a row the chain stops with a card."""
    from storage import database as task_store, remote_store
    from storage.pg import run_db
    from ws import wire_events as wire
    cid = job.chat_id
    try:
        chat = await run_db(task_store.get_chat, cid)
        if not chat:
            return
        why = await run_db(_resume_bounds, chat, job, asker)
        if not why and await run_db(_waiting_message_goes_first, chat):
            why = "a person's message waits: it goes first"
        if why:
            logger.info("chat move %s: chat=%s does not continue by itself: %s",
                        job.move_id[:8], cid[:8], why)
            if why in (_WHY_LOCKED, _WHY_BLOCKED):
                await run_db(_history_row, cid, _skipped_sentence(job), wire.MOVED_RESUME_SKIPPED)
            return
        now = time.monotonic()
        for stale in [k for k, v in _recent_resumes.items()
                      if not v or now - v[-1] >= RESUME_WINDOW_S]:
            _recent_resumes.pop(stale, None)
        recent = [t for t in _recent_resumes.get(cid, []) if now - t < RESUME_WINDOW_S]
        chain = await run_db(remote_store.tool_moves_since_person, cid)
        if chain >= RESUME_CHAIN_MAX or len(recent) >= RESUME_CHAIN_MAX:
            sentence = (
                f"The agent moved this chat {RESUME_CHAIN_MAX} times in a row with no message "
                "from you, so it stops here. Send a message to continue."
                if chain >= RESUME_CHAIN_MAX else
                f"The agent moved this chat {RESUME_CHAIN_MAX} times in the last hour, so it "
                "stops here. Send a message to continue.")
            await run_db(_history_row, cid, sentence, wire.MOVED_RESUME_STOPPED)
            logger.info("chat move %s: chat=%s stops: %d moves in a row, %d resumes this hour",
                        job.move_id[:8], cid[:8], chain, len(recent))
            return
        recent.append(now)
        _recent_resumes[cid] = recent
        await _deliver_resume(chat, job, asker)
    except Exception:
        logger.exception("chat move %s: the chat did not continue by itself", job.move_id[:8])


async def _deliver_resume(chat: dict, job: _Job, asker: Asker) -> None:
    from core.events import chat_writer
    from core.permission_modes import offered_mode
    from core.session import session_kind
    from core.session.session_delivery import deliver_prompt
    from core.session.session_manager import get_layer_capabilities
    from services.scheduler import delivery
    from storage import database as task_store
    from storage.pg import run_db
    from ws import wire_events as wire
    cid = job.chat_id
    run_sub, run_role = _builds_as(chat, asker.sub, asker.role)
    caps = get_layer_capabilities(chat_targets.chat_engine(chat))
    stored = chat.get("permission_mode") or ""
    mode = (offered_mode(stored, caps.permission_modes) if caps and caps.permission_modes
            else offered_mode(stored))
    kind = (session_kind.TASK.name if session_kind.is_task_chat_id(cid)
            else session_kind.DASHBOARD.name)
    text = resume_nudge(job.target, job.to_label)
    wake = json.dumps({"prompt": text, "source": wire.WAKE_SOURCE_MOVE})

    # The ladder writes the row before its rungs, so it precedes the turn's
    # answer; a nudge no rung delivered takes it back.
    written: list[asyncio.Future] = []

    def _persist(target_chat_id: str) -> None:
        if target_chat_id:
            written.append(chat_writer.submit(
                target_chat_id,
                functools.partial(task_store.add_chat_message, target_chat_id, "event", "",
                                  event_type=wire.PERSISTED_SCHEDULE_WAKE, event_data=wake),
                label="move_resume"))

    async def _outcome(outcome) -> None:
        if outcome.path != "none":
            return
        for fut in written:
            try:
                row = await fut
                await chat_writer.submit(cid, functools.partial(
                    task_store.delete_chat_message_ranges, cid, [(row - 1, row)]),
                    label="move_resume_undone")
            except Exception:  # noqa: BLE001  # the row stays: a stale line, not a lost turn
                logger.debug("chat move: the resume row was not taken back", exc_info=True)
        if _person_took_chat(cid):
            return
        await run_db(_history_row, cid, _skipped_sentence(job), wire.MOVED_RESUME_SKIPPED)

    if await run_db(_waiting_message_goes_first, chat):
        logger.info("chat move %s: chat=%s does not continue by itself: a person's message "
                    "waits: it goes first", job.move_id[:8], cid[:8])
        return
    if _turn_holds_chat(cid):
        # The queue's own drain (or a tab that reopened the chat) started a
        # person's turn after the move: the nudge would steer into it or run
        # straight after it. Read with no await before the delivery starts;
        # the queue itself was judged just above (a message the lock sends
        # back holds nothing).
        logger.info("chat move %s: chat=%s does not continue by itself: a person's turn "
                    "has the chat", job.move_id[:8], cid[:8])
        return
    logger.info("chat move %s: chat=%s continues on %s by itself (%s, %s)", job.move_id[:8],
                cid[:8], job.to_label, mode, kind)
    try:
        await deliver_prompt(
            cid, text, source="move_resume", session_id=chat.get("session_id") or "",
            agent=chat.get("agent") or "", user_sub=run_sub, role=run_role,
            persist_event=_persist, allow_ws=False, allow_pty=False,
            persistent_fn=delivery._deliver_via_persistent,
            oneshot_fn=functools.partial(delivery._deliver_via_oneshot, permission_mode=mode,
                                         client_type=kind, fresh_without_session=True,
                                         note_prompt=True),
            on_outcome=_outcome,
        )
    except Exception:  # noqa: BLE001  # a rung that raises (the spawn's guard, a broken start) is undone like an undelivered nudge
        logger.warning("chat move %s: the resume on %s raised", job.move_id[:8], job.to_label,
                       exc_info=True)
        from core.session.session_delivery import DeliveryOutcome
        await _outcome(DeliveryOutcome("none", chat_id=cid))


# -- the hand-over ---------------------------------------------------------------

def _builds_as(chat: dict, sub: str, role: str) -> tuple[str | None, str]:
    """``(sub, role)`` a session of the chat builds with when this person
    drives it: the task's identity on a task-run chat, else the sender."""
    from core.session import session_kind
    cid = chat.get("id") or ""
    if session_kind.is_task_chat_id(cid):
        from core.config.task_config_builder import rewarm_task_identity
        ident = rewarm_task_identity(cid, chat.get("agent") or "")
        return (ident.creds_user_sub, ident.role) if ident else (None, roles.MANAGER)
    return sub, role


def placement_blocked(chat: dict, person_sub: str, person_role: str) -> bool:
    """Whether a session this person drives may not run where the chat is
    (the guard, ``placement.may_run_on``, for the identity it builds
    with). Synchronous."""
    pin = chat.get("execution_target") or ""
    if placement.is_local(pin) or not chat.get("agent"):
        return False
    from storage import remote_store
    machine = remote_store.get_remote_machine(placement.machine_of(pin))
    if machine is None:
        return False
    run_sub, run_role = _builds_as(chat, person_sub, person_role)
    return not placement.may_run_on(machine, run_sub=run_sub,
                                    run_can_manage=roles.can_manage(run_role),
                                    user_paired_allowed=chat_targets.user_machines_allowed())


def _handover_refusal(chat: dict, asker: Asker) -> str:
    """``""`` when the chat hands over to the asker's default; else the
    sentence for a chat whose place their session may not use and that
    does not hand over (an admin's send into a colleague's chat, a task's
    run). Called when ``placement_blocked``. Synchronous."""
    from core.session import session_kind, visibility
    from storage import remote_store
    machine = remote_store.get_remote_machine(
        placement.machine_of(chat.get("execution_target") or "")) or {}
    owner = chat.get("user_sub") or ""
    own_chat = owner == asker.sub and not session_kind.is_task_chat_id(chat.get("id") or "")
    if placement.machine_is_admin_paired(machine):
        if visibility.is_shared_chat_owner(owner) or own_chat:
            return ""
        name = (chat_targets.label_of(placement.machine_of(chat.get("execution_target") or ""),
                                      machine) if machine else "this machine")
        return f"You can't run this chat on {name}. Move it from the Runs on menu."
    if visibility.is_shared_chat_owner(owner):
        return ""
    machine_owner = machine.get("registered_by") or ""
    if machine_owner == asker.sub:
        return ("Personal computers are turned off on this platform. Move this chat from the "
                "Runs on menu.")
    return (f"This chat runs on {chat_targets.owner_name(machine_owner)}'s computer, so only "
            "they can continue it there. Move it from the Runs on menu.")


async def hand_over(chat_id: str, *, asker: Asker) -> dict | None:
    """When ``asker`` drives a chat whose place their session may not use:
    a Shared-only chat, or their own chat on an admin machine their role
    cannot use, moves to their default first (the lock refuses while it
    holds); any other such chat answers with its sentence. Returns the
    started move (``{move_id, to_label, memory}``), or ``None`` when
    nothing had to move; ``MoveRefused`` with the sentence."""
    from storage import database as task_store
    from storage.pg import run_db
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat:
        return None
    if not await run_db(placement_blocked, chat, asker.sub, asker.role):
        return None
    lock = await run_db(lock_sentence, chat, asker.sub)
    if lock:
        raise MoveRefused(lock)
    refusal = await run_db(_handover_refusal, chat, asker)
    if refusal:
        raise MoveRefused(refusal)
    view = await run_db(chat_targets.agent_view, chat.get("agent") or "", asker.sub, asker.role,
                        admin_asker=asker.platform_admin, engine_path=chat_targets.chat_engine(chat))
    default = view["default_target"]
    entry = next((t for t in view["targets"] if t["target"] == default), None)
    if entry is None or not entry["pickable"]:
        label = entry["label"] if entry else chat_targets.label_of(default)
        raise MoveRefused(f"{chat_targets.place_phrase(default, label, start=True)} is offline, "
                          "so this chat can't run for you right now.")
    try:
        return await request_move(chat_id, default, asker=asker, origin=move_schema.ORIGIN_HANDOVER)
    except MoveRefused as e:
        if str(e) != FINISH_TURN:
            raise
        pin = chat.get("execution_target") or ""
        place = await run_db(chat_targets.label_of, pin)
        raise MoveRefused(f"This chat is busy on {chat_targets.place_phrase(pin, place)}. Send "
                          "your message again when its turn ends.") from None


# -- the proof sweep ---------------------------------------------------------------

async def _settle(move_id: str, state: str) -> None:
    from storage import remote_store
    from storage.pg import run_db
    await run_db(functools.partial(remote_store.update_chat_move, move_id, state=state))


# Move id -> (monotonic time of its next proof check, the wait after it).
_proof_next: dict[str, tuple[float, float]] = {}


def _not_yet(move_id: str) -> None:
    """Back the row's next proof check off (doubling, to the longest wait)."""
    first, longest = PROOF_BACKOFF_S
    wait = _proof_next.get(move_id, (0.0, first / 2))[1]
    wait = min(wait * 2, longest)
    _proof_next[move_id] = (time.monotonic() + wait, wait)


async def sweep() -> None:
    """Settle the moves waiting for proof (the reaper's 60 s loop): the
    source's copy goes once the target's session file grew past its copied
    size with the chat's ids unchanged; nothing is deleted on a changed id,
    a newer move, a gone machine, a gone chat, or a session something else
    still names (another chat's ids, a task that continues it). A row that
    is not proved yet is asked again later each time (``_not_yet``)."""
    from core.remote import session_files
    from storage import database as task_store, remote_store
    from storage.pg import run_db
    rows = await run_db(remote_store.chat_moves_in, (move_schema.AWAITING_RESUME,))
    now = datetime.now(timezone.utc)
    try:
        # A staging folder a move never finished (the platform's own side).
        await run_db(session_files.sweep_staging)
    except OSError:
        logger.warning("chat move sweep: staging not swept", exc_info=True)
    waiting = {row["id"] for row in rows}
    for mid in [m for m in _proof_next if m not in waiting]:
        _proof_next.pop(mid, None)
    for row in rows:
        mid = row["id"]
        try:
            chat = await run_db(task_store.get_chat, row["chat_id"])
            if not chat:
                await _settle(mid, move_schema.CHAT_GONE)
                continue
            latest = await run_db(remote_store.latest_chat_move, row["chat_id"])
            if latest and latest["id"] != mid:
                await _settle(mid, move_schema.SUPERSEDED)
                continue
            if ((chat.get("session_id") or "") != row["session_id"]
                    or (chat.get("codex_thread_id") or "") != row["resume_handle"]):
                await _settle(mid, move_schema.REF_CHANGED)
                continue
            if now - datetime.fromisoformat(row["created_at"]) > PROOF_WINDOW:
                await _settle(mid, move_schema.EXPIRED)
                continue
            target, source = row["target"], row["source"]
            if not _reachable(target) or not _reachable(source):
                continue
            if time.monotonic() < _proof_next.get(mid, (0.0, 0.0))[0]:
                continue
            scope = session_files.SessionScope(
                agent=row["agent"], engine=row["engine"], home_user=row["home_user"],
                session_id=row["session_id"], resume_handle=row["resume_handle"])
            size = await session_files.main_size(target, scope, row["main_rel"])
            if size is None or size <= int(row["main_size"] or 0):
                _not_yet(mid)
                continue
            if source != chat_targets.current_target(chat):
                # The target's answer took a while: a move of the chat that
                # began or ended since the reads above may have put its files
                # back on the source.
                fresh = await run_db(task_store.get_chat, row["chat_id"]) or {}
                again = await run_db(remote_store.latest_chat_move, row["chat_id"])
                if (moving(row["chat_id"]) or not again or again["id"] != mid
                        or source == chat_targets.current_target(fresh)):
                    _not_yet(mid)
                    continue
                if await run_db(remote_store.session_refs_shared, row["chat_id"], scope.refs):
                    logger.info("chat move %s: resumed on %s; kept on %s: the session is shared",
                                mid[:8], row["to_label"], row["from_label"])
                else:
                    gone = await session_files.delete_files(source, scope)
                    logger.info("chat move %s: resumed on %s; %d files removed from %s",
                                mid[:8], row["to_label"], gone, row["from_label"])
            await _settle(mid, move_schema.SETTLED)
            _proof_next.pop(mid, None)
        except session_files.TransferError as e:
            # A machine that answered busy or refused: asked again later.
            logger.info("chat move %s: not proved yet: %s", mid[:8], e)
            _not_yet(mid)
        except Exception:  # noqa: BLE001  # one row's failure never stops the sweep
            logger.warning("chat move sweep: row %s", mid[:8], exc_info=True)
            _not_yet(mid)


def settle_at_start() -> int:
    """A move a restart cut short failed; its staging is cleaned. At boot."""
    from core.remote import session_files
    from storage import remote_store
    n = remote_store.fail_running_moves()
    try:
        session_files.sweep_staging(max_age_s=0.0)
    except OSError:
        logger.warning("chat move: staging sweep at start failed", exc_info=True)
    return n

