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
import contextlib
import functools
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from auth import roles
from core import drain, placement
from services.remote import chat_targets, machine_away
from storage import move_schema

if TYPE_CHECKING:
    from core.remote import session_files

logger = logging.getLogger("otodock.remote")

FINISH_TURN = "Finish or stop the current turn before moving the chat."
KIND_STAYS = "This kind of chat stays where it runs."
NOT_MOVER = "Only someone who can continue this chat can move it."
CANT_THERE = "You can't move this chat there."
ALREADY_THERE = "This chat already runs there."
DIRECT_LOCAL = f"Direct LLM chats run on {chat_targets.place_phrase(placement.LOCAL)}."
NOT_FOUND = "Chat not found."
UPDATING = "OtoDock is updating. Move the chat again in a few minutes."
CONTINUES_WORK = "This chat continues delegated work, so it stays where it runs."
LANE_BUSY = "This chat is busy with delegated work. Move it when that work ends."
LANE_HANDOVER = ("This chat continues delegated work. Move it from the Runs on menu, then send "
                 "your message.")
#: The refusals of a chat whose turn is open: a waiting move asks again.
_BUSY = (FINISH_TURN, LANE_BUSY)
VOICE_IN_USE = "A voice conversation is using this chat. End it before moving the chat."

#: How long a move row waits for the target's proof before it settles.
PROOF_WINDOW = timedelta(days=14)
#: How long the agent's scheduled move waits for the chat's turn to end.
TOOL_WAIT_S = 1800.0
#: How long, inside that wait, it holds for a machine of the move that is
#: away (a boot's moves wait for the machines to come back).
MACHINE_WAIT_S = 120.0
#: The proof sweep's per-row back-off: the first wait and the longest one.
PROOF_BACKOFF_S = (60.0, 900.0)


class MoveRefused(Exception):
    """A move refused; the message is the sentence the person sees."""


@dataclass(frozen=True)
class Asker:
    """Who asks for a move: their sub, their effective role on the chat's
    agent, whether they are a platform admin, and the session that asks
    when no person does (a task run moving its own chat)."""

    sub: str
    role: str
    platform_admin: bool
    session_id: str = ""


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

    def again(self) -> None:
        """A second try of the copy starts its phases over: its first report goes."""
        self.last_pct = None

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


def session_place(chat: dict, person: chat_targets.RunPerson) -> str:
    """Where the chat's engine session lives, for a round that resumes it:
    the machine its pin names; the platform when the pin says so and the
    platform holds the session's file (``local`` is also the column's
    default, which a chat whose round never ran keeps); else ``""``, the
    agent's default. A run pins its chat to where it ran
    (``task_run._bind_layer``) and a move to where it went. Synchronous."""
    from core.remote import session_files
    pin = chat.get("execution_target") or ""
    if not pin:
        return ""
    if not placement.is_local(pin):
        return placement.machine_of(pin)
    scope = session_files.SessionScope(
        agent=chat.get("agent") or "", engine=chat_targets.chat_engine(chat),
        home_user=home_user_of(chat, person), session_id=chat.get("session_id") or "",
        resume_handle=chat.get("codex_thread_id") or "")
    return placement.LOCAL if scope.refs and session_files.local_stat(scope) else ""


def _movable_kind(chat: dict) -> bool:
    from core.session import session_kind
    kind = session_kind.of_chat(chat)
    return kind in (session_kind.DASHBOARD, session_kind.TASK) or session_kind.is_task_chat_id(
        chat.get("id") or "")


def _continues_delegated_work(chat: dict) -> bool:
    """A delegated worker chat (its delegator continues it with
    ``continue_id``), a chat a task can still continue by itself, or one a
    delegate's round works in now (queued or running): the reload, the
    account choice and the account's removal leave such a chat to its
    rounds. A task's row that stayed behind after its run does not count.
    Synchronous."""
    if chat.get("parent_chat_id"):
        return True
    from storage import database as task_store, remote_store
    return (remote_store.task_can_continue(chat)
            or bool(task_store.live_delegate_run_ids_for_chat(chat.get("id") or "")))


def _server_only(chat: dict) -> bool:
    """A chat whose engine never runs on a machine (Direct LLM)."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(chat_targets.chat_engine(chat))
    return caps is not None and not caps.runtime.supports_remote_execution


def task_asker(chat: dict, session_id: str) -> Asker | None:
    """A task run's own chat moves as the task's identity, in every scope: a
    user-scope run as its creator at the creator's role on the agent, a run
    with no person behind it (an agent-scope task, every task on a
    Shared-only agent) as the agent at the run's role. The run identity is
    the authority here, not the token's principal: a token minted before the
    task builder named the run's person (a session started by an earlier
    release, or one re-adopted after a restart) still names nobody. A run
    with no person asks with its own session, which the rule reads for the
    knowledge write. ``None`` for any other chat. Synchronous."""
    from core.config.task_config_builder import rewarm_task_identity
    from core.session import session_kind
    cid = chat.get("id") or ""
    if not session_kind.is_task_chat_id(cid):
        return None
    ident = rewarm_task_identity(cid, chat.get("agent") or "")
    if ident is None:
        return None
    sub = ident.creds_user_sub or ""
    return Asker(sub=sub, role=ident.role, platform_admin=bool(sub) and roles.is_admin(ident.role),
                 session_id=session_id)


def run_asker(chat: dict, session_id: str) -> Asker | None:
    """The identity of the run whose session this is, for a task session's
    own call (``chat_reload.schedule_for_run``): a task's own chat or a
    worker chat, the run found by its session; the creator at their role
    for a user-scope run, the agent at the run's role with no person
    otherwise. ``None`` when no run has the session, or it is a call's
    wrap-up. Synchronous."""
    from core.config.task_config_builder import resolve_task_identity
    from services.scheduler import task_kinds
    from storage import database as task_store
    run = task_store.get_run_by_session(session_id) if session_id else None
    if not run or run.get("task_type") == task_kinds.RUN_WRAPUP:
        return None
    ident = resolve_task_identity(
        chat.get("agent") or "", run.get("scope") or "agent", run.get("created_by"),
        allow_knowledge_rw=task_kinds.run_allows_knowledge_rw(run.get("task_type")))
    sub = ident.creds_user_sub or ""
    return Asker(sub=sub, role=ident.role, platform_admin=bool(sub) and roles.is_admin(ident.role),
                 session_id=session_id)


def _asker_now(chat: dict, asker: Asker) -> Asker:
    """The asker as they stand when the agent's move fires (the turn that
    asked may have ended long before, or a restart came between): a task-run
    chat's identity at its session, a person's role on the agent now.
    ``MoveRefused`` for a person with no access any more. Synchronous."""
    from auth.providers import effective_role_of
    task = task_asker(chat, asker.session_id or chat.get("session_id") or "")
    if task is not None:
        return task
    standing = effective_role_of(asker.sub, chat.get("agent") or "") if asker.sub else roles.NO_ACCESS
    if standing == roles.NO_ACCESS:
        raise MoveRefused(NOT_MOVER)
    return Asker(sub=asker.sub, role=standing, platform_admin=roles.is_admin(standing),
                 session_id=asker.session_id)


def may_drive(chat: dict, asker: Asker) -> bool:
    """Whether the asker may drive (and so move) the chat: the rule every
    door asks (``visibility.drive_refusal``)."""
    from core.session import visibility
    return not visibility.drive_refusal(chat, asker.sub, asker.role, admin=asker.platform_admin,
                                        session_id=asker.session_id)


def _session_live(chat: dict) -> bool:
    """A live session of the chat on the chat's own place: a terminal, or a
    headless session held there (one on a machine counts while its machine
    answers or its reconnect grace holds it). The lock reads this, so a
    session held elsewhere behind the pin, as a round that fell back to the
    platform leaves one, is not running on the pinned machine."""
    from core.session import interactive_session, session_manager
    sid = chat.get("session_id") or ""
    if interactive_session.find_live_for_chat(chat.get("id") or "") is not None:
        return True
    live = session_manager.live_session(sid) if sid else None
    return (live is not None and live.reachable
            and live.machine_id == placement.machine_of(chat.get("execution_target")))


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
    if not placement.machine_is_personal(machine):
        return ""
    owner = placement.owner_of(machine)
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
    waiting, a one-shot delivery running, a terminal turn, an active run,
    a delegate's round queued or running in it."""
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
    return _round_live(chat)


def _round_live(chat: dict) -> bool:
    """A delegate's round works in the chat now, queued or running (a
    continue of a lane, or of a chat a delegation names). Synchronous."""
    from storage import database as task_store
    return bool(task_store.live_delegate_run_ids_for_chat(chat.get("id") or ""))


def moving_sentence(chat_id: str) -> str:
    """The sentence that a move of the chat runs in this process ("This
    chat is moving to <place>."), else ``""``."""
    job = _running.get(chat_id)
    return f"This chat is moving to {job.to_phrase}." if job is not None else ""


def _lane_without_memory(chat: dict) -> str:
    """The refusal of a lane (a delegated worker chat) whose files cannot
    travel now: its delegator's continue resumes its session by id and
    never reads a summary, so it moves only with them. ``""`` otherwise
    (a lane with no session has nothing to lose). Synchronous."""
    if not chat.get("parent_chat_id") or not (chat.get("session_id")
                                              or chat.get("codex_thread_id")):
        return ""
    if chat.get("compute_machine"):
        # Its record is in a compute machine's own copy, which no move
        # carries (that machine refuses ``session_move``).
        return CONTINUES_WORK
    current = chat_targets.current_target(chat)
    if _reachable(current):
        return ""
    return ("This chat continues delegated work, so it moves only with its memory. Move it when "
            f"{chat_targets.place_phrase(current)} is back.")


# -- the chip's answer -----------------------------------------------------------

def targets_view(chat: dict, asker: Asker, *, blocked: str = "", own_move: bool = False) -> dict:
    """The chip's answer for ``chat`` as ``asker`` sees it; ``blocked`` is a
    socket's own refusal (a voice conversation on the chat), ``own_move``
    the agent's tool listing the places of its own chat. Synchronous."""
    from core.session import visibility
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
                       else placement.PAIRING_COMPUTE if placement.machine_is_compute(cur_machine)
                       else placement.PAIRING_USER),
           "state": cur_state,
           "owner": (chat_targets.owner_name(placement.owner_of(cur_machine))
                     if placement.machine_is_personal(cur_machine) else "")}
    for e in entries:
        e["is_current"] = e["target"] == current
    if current not in known:
        entries.insert(0, {"target": current, "label": cur["label"], "pairing": cur["pairing"],
                           "state": cur_state, "reachable": cur_state in remote_status.REACHABLE,
                           "is_default": False, "pickable": False, "why_not": "",
                           "allowed": True, "is_current": True})
    refusal = ""
    if not _movable_kind(chat):
        refusal = KIND_STAYS
    elif _server_only(chat):
        refusal = DIRECT_LOCAL
    elif not may_drive(chat, asker):
        refusal = NOT_MOVER
    elif remote_store.task_can_continue(chat) or (own_move and chat.get("parent_chat_id")):
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
            "place_chosen": chosen,
            # A Shared-only chat runs with the agent's accounts: the move
            # dialog says which of them a personal computer gets.
            "shared": visibility.is_shared_chat_owner(chat.get("user_sub"))}


# -- the move ----------------------------------------------------------------------

def _validate(chat: dict, wanted: str, asker: Asker, *, expect_from: str | None,
              check_turn: bool, own_move: bool = False,
              ) -> tuple[chat_targets.Target, chat_targets.RunPerson]:
    """The synchronous checks; ``MoveRefused`` with the sentence. A task
    that can still continue the chat keeps it where that task runs; a lane
    moves when a person moves it, never by its agent's own move
    (``own_move``: its resume there would run a turn its delegator never
    hears), and only with its files."""
    from storage import remote_store
    if not _movable_kind(chat):
        raise MoveRefused(KIND_STAYS)
    if not may_drive(chat, asker):
        raise MoveRefused(NOT_MOVER)
    if remote_store.task_can_continue(chat) or (own_move and chat.get("parent_chat_id")):
        raise MoveRefused(CONTINUES_WORK)
    lock = lock_sentence(chat, asker.sub)
    if lock:
        raise MoveRefused(lock)
    cid = chat.get("id") or ""
    moving_now = moving_sentence(cid)
    if moving_now:
        raise MoveRefused(moving_now)
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
    lane = _lane_without_memory(chat)
    if lane:
        raise MoveRefused(lane)
    if check_turn and open_turn(chat):
        raise MoveRefused(LANE_BUSY if _round_live(chat) else FINISH_TURN)
    return hit, person


def _expected_memory(chat: dict) -> str:
    """What the move should carry, as the chip promises it: the files when
    the chat has a session there and the current place is reachable (or the
    platform), else a summary. A record in a compute machine's own copy
    (``chats.compute_machine``) never travels: a summary."""
    if not (chat.get("session_id") or chat.get("codex_thread_id")) or chat.get("compute_machine"):
        return move_schema.MEMORY_SUMMARY
    current = chat_targets.current_target(chat)
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
    if drain.holding():
        raise MoveRefused(drain.refusal(UPDATING))
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
            raise MoveRefused(moving_sentence(cid) or FINISH_TURN)
    else:
        q = await input_queue.loaded(cid)
    move_id = str(uuid.uuid4())
    job = _Job(move_id, cid, hit.label, hit.target)
    if cid in _running:
        q.release(claim)
        raise MoveRefused(moving_sentence(cid))
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
        if await run_db(_round_live, fresh):
            # A delegate's round queued after the check above.
            raise MoveRefused(LANE_BUSY)
        memory = _expected_memory(fresh)
        home_user = await run_db(home_user_of, fresh, person)
        current = chat_targets.current_target(fresh)
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
    from core.session import chat_rebuild
    return chat_rebuild.turn_open_while_held(chat)


async def _close_live(chat: dict) -> bool:
    """Close the chat's live session where it runs (idle by the gates, read
    under the move's own hold); a machine that is not connected is sent
    nothing. True when a session was closed, or may have been."""
    from core.session import chat_rebuild
    try:
        return await chat_rebuild.close_chat_session(chat, reason="moved", require_idle=False,
                                                     lock=False)
    except Exception:  # noqa: BLE001  # a session that will not close is replaced either way
        logger.warning("chat move: closing session %s failed",
                       (chat.get("session_id") or "")[:8], exc_info=True)
        return True


async def _copy(job: _Job, scope: session_files.SessionScope, source: str, target: str,
                progress: _Progress, *,
                closed_at_source: bool) -> tuple[session_files.CopyResult | None, str, str]:
    """The copy, after the source's files stopped growing, tried once more
    when the engine wrote under the first try (``changed``) or the source
    answered busy right after this move closed its session there. Every
    failed try's stagings go. ``(result, why, busy_at)``: the copy's
    result, else the summary's reason and, for a busy refusal, its place."""
    from core.remote import session_files
    stage = job.move_id
    for attempt in (1, 2):
        await session_files.wait_quiet(source, scope)
        try:
            return await session_files.copy_session(scope, source=source, target=target,
                                                    move_id=stage, progress=progress), "", ""
        except Exception as e:  # noqa: BLE001  # every failure cleans up, then falls back
            why = getattr(e, "why", move_schema.WHY_TRANSFER_FAILED)
            place = getattr(e, "place", "") or source
            # The platform's own staging (a pull from a machine lands
            # there whatever the target), then each machine's.
            await asyncio.to_thread(session_files.local_discard, scope, stage)
            for p in (source, target):
                if placement.is_local(p) or not _reachable(p):
                    continue
                await session_files.discard(p, scope, stage)
            if getattr(e, "committed", False):
                if _reachable(target):
                    # A copy that failed at or after its commit leaves the
                    # session on the target under ids no chat resumes there.
                    try:
                        await session_files.delete_files(target, scope)
                    except Exception as de:  # noqa: BLE001  # the target keeps an unused copy
                        logger.info("chat move %s: nothing removed on the target: %s",
                                    job.move_id[:8], de)
            elif attempt == 1 and _reachable(source) and _reachable(target) and (
                    getattr(e, "changed", False)
                    or (why == move_schema.WHY_BUSY and place == source and closed_at_source)):
                logger.info("chat move %s: the source changed during the copy (%s); trying "
                            "once more", job.move_id[:8], e)
                stage = str(uuid.uuid4())
                progress.again()
                continue
            logger.warning("chat move %s: the files did not move (%s): %s", job.move_id[:8], why, e)
            return None, why, (place if why == move_schema.WHY_BUSY else "")
    return None, move_schema.WHY_TRANSFER_FAILED, ""


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
        closed = await _close_live(chat)
        result = None
        if not scope.refs:
            why = move_schema.WHY_NO_FILES
        elif not _reachable(source):
            why = move_schema.WHY_OFFLINE
        else:
            progress = _Progress(job, chat, row.get("actor") or "", await _readers(chat))
            result, why, busy_at = await _copy(job, scope, source, target, progress,
                                               closed_at_source=closed)
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
        elif chat.get("parent_chat_id") and scope.refs:
            # A lane's delegator resumes its session by id and never reads
            # a summary: it stays where its files are.
            message = f"The move to {tp} did not finish. The chat stays on {fp}."
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
                                                 check_turn=False, own_move=True))
    memory = await run_db(_expected_memory, chat)
    from services.remote import chat_reload
    # The move rebuilds the session on the new place: a reload that waits
    # for the same turn's end would only close it again.
    chat_reload.cancel(chat_id)
    from storage.chat import pending_rebuilds
    await run_db(functools.partial(
        pending_rebuilds.save, chat_id, pending_rebuilds.MOVE, person=asker.sub,
        wait_s=TOOL_WAIT_S, target=hit.target))
    _arm(chat_id, hit.target, asker, blocked, TOOL_WAIT_S)
    return {"to_label": hit.label, "memory": memory,
            "to_phrase": chat_targets.place_phrase(hit.target, hit.label)}


def _arm(chat_id: str, target: str, asker: Asker, blocked: Callable[[str], str] | None,
         wait_s: float) -> None:
    """Start the chat's waiting move, replacing an earlier one. Its row (what
    a restart finds) goes when the wait ends, unless a later request
    replaced it or the platform's own stop cut the wait: the next boot
    settles the row (``settle_after_restart``)."""
    from core import background
    from storage.chat import pending_rebuilds
    from storage.pg import run_db
    earlier = _waiters.pop(chat_id, None)
    if earlier is not None and not earlier.done():
        earlier.cancel()
        logger.info("chat move: chat=%s: the agent's later move replaces its earlier one",
                    chat_id[:8])

    async def _waiter() -> None:
        cut_by_the_stop = False
        try:
            await _wait_and_move(chat_id, target, asker, blocked, resume=True, wait_s=wait_s)
        except asyncio.CancelledError:
            cut_by_the_stop = drain.stopping()
            raise
        finally:
            if not cut_by_the_stop and _waiters.get(chat_id) is asyncio.current_task():
                with contextlib.suppress(Exception):
                    await asyncio.shield(run_db(pending_rebuilds.clear, chat_id,
                                                pending_rebuilds.MOVE))

    waiter = background.spawn(_waiter(), name="chat-move-wait", owner="chat_move")
    _waiters[chat_id] = waiter
    waiter.add_done_callback(lambda t: _waiters.pop(chat_id, None) if _waiters.get(chat_id) is t
                             else None)


async def settle_after_restart(row: dict, *, lives_on: bool) -> None:
    """The agent's move that waited when the platform stopped (its row taken
    by the caller, so this runs once per row): armed again with what is left
    of its wait when the chat's session lives on (its machine kept the turn,
    the platform continues the chat, or no turn was cut); the asker is read
    again when it fires and the target validated then. Otherwise, past its
    deadline, or with nobody to ask for it, the chat is told it did not
    move. ``blocked`` is none: a voice conversation does not outlive a
    restart."""
    from core.session import session_kind
    from storage.chat import pending_rebuilds
    from storage.pg import run_db
    from ws import wire_events as wire
    chat_id, target = row["chat_id"], row.get("target") or ""
    who = row.get("person") or ""
    left = pending_rebuilds.seconds_left(row)
    if lives_on and left > 0 and target and (who or session_kind.is_task_chat_id(chat_id)):
        await run_db(functools.partial(
            pending_rebuilds.save, chat_id, pending_rebuilds.MOVE, person=who, wait_s=left,
            target=target))
        _arm(chat_id, target, Asker(sub=who, role="", platform_admin=False), None, left)
        logger.info("chat move: chat=%s: its move waits again after the restart", chat_id[:8])
        return
    earlier = _waiters.pop(chat_id, None)
    if earlier is not None and not earlier.done():
        earlier.cancel()
    await run_db(_history_row, chat_id, "The chat did not move: the platform restarted. Ask again.",
                 wire.MOVED_FAILED)


def _machine_away(chat: dict, target: str) -> bool:
    """A machine of the move (where the chat is, where it goes) is away.
    Synchronous."""
    return not (_reachable(chat_targets.current_target(chat)) and _reachable(target))


async def _wait_and_move(chat_id: str, target: str, asker: Asker,
                         blocked: Callable[[str], str] | None = None, *,
                         resume: bool = False, wait_s: float = TOOL_WAIT_S) -> None:
    """Wait for the chat's turn to end, then move it; ``resume``: the chat
    then continues there by itself. The claim alone is not the end: a
    terminal turn, a task run and a person's warmup hold no claim, so the
    claim is given back while one of them is open (``open_turn``) and asked
    for again. No claim is asked for while the continue pass holds a cut
    turn of the chat (its start would be refused), and a machine of the move
    that is away is waited for up to ``MACHINE_WAIT_S``. The asker is read
    again when it fires."""
    from core.events import input_queue
    from storage import database as task_store
    from storage.chat import cut_records
    from storage.pg import run_db
    deadline = time.monotonic() + wait_s
    q = await input_queue.loaded(chat_id)
    away_since: float | None = None
    while time.monotonic() < deadline:
        # While new work is held it waits too, its row kept: the boot settles it.
        if drain.holding() or await run_db(cut_records.unsettled_of, chat_id) is not None:
            await asyncio.sleep(TURN_POLL_S)
            continue
        claim = await input_queue.claim_when_free(
            chat_id, timeout=max(0.0, deadline - time.monotonic()))
        if claim is None:
            # The wait ran out, or the chat is being deleted (the queue
            # answers at once then): a deleted chat gets no note.
            if input_queue.deleting(chat_id) or not await run_db(task_store.get_chat, chat_id):
                return
            break
        try:
            chat = await run_db(task_store.get_chat, chat_id)
            if not chat:
                q.release(claim, rearm=True)
                return
            if drain.holding() or await run_db(open_turn, chat):
                q.release(claim, rearm=True)
                await asyncio.sleep(TURN_POLL_S)
                continue
            if await run_db(_machine_away, chat, target):
                away_since = away_since if away_since is not None else time.monotonic()
                if time.monotonic() - away_since < MACHINE_WAIT_S:
                    q.release(claim, rearm=True)
                    await asyncio.sleep(TURN_POLL_S)
                    continue
            if blocked is not None and (why := blocked(chat_id)):
                raise MoveRefused(why)
            asker = await run_db(_asker_now, chat, asker)
            hit, person = await run_db(functools.partial(
                _validate, chat, target, asker, expect_from=None, check_turn=False,
                own_move=resume))
            await _start(chat, hit, person, asker=asker, origin=move_schema.ORIGIN_TOOL,
                         claim=claim, resume=resume)
            return
        except BaseException as e:
            # A cancel (a later move, the stop) or a failure while the claim
            # is held gives it back; a second release, or one after _start
            # gave it back, changes nothing.
            q.release(claim, rearm=True)
            if not isinstance(e, MoveRefused):
                raise
            if str(e) in _BUSY:
                await asyncio.sleep(TURN_POLL_S)
                continue
            await run_db(_history_row, chat_id, f"The chat did not move. {e}", "failed")
            return
    label = await run_db(chat_targets.label_of, target)
    await run_db(_history_row, chat_id,
                 f"The move to {chat_targets.place_phrase(target, label)} did not happen: "
                 "the turn did not end in time.", "failed")


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
    from core.session import chat_rebuild, session_kind
    from storage import database as task_store
    from storage.pg import run_db
    from ws import wire_events as wire
    cid = job.chat_id
    run_sub, run_role = _builds_as(chat, asker.sub, asker.role)
    mode = chat_rebuild.chat_mode(chat)
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
    # A person's own chat, and a typist's turn in a lane's chat, resume as their own warm-up builds.
    from core.config import spender
    build = (chat_rebuild.AS_CHAT if run_sub and await run_db(spender.typist_of_lane, chat, run_sub)
             else (await run_db(chat_rebuild.wake_build, chat, run_sub, run_role)).build)
    try:
        await chat_rebuild.rebuild_and_resume(
            cid, text, source="move_resume", session_id=chat.get("session_id") or "",
            agent=chat.get("agent") or "", person=run_sub, role=run_role,
            mode=mode, kind=kind, build=build,
            persist_event=_persist, allow_ws=False, allow_pty=False,
            fresh_without_session=True, on_outcome=_outcome,
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
    from storage.agents import agent_store
    machine = remote_store.get_remote_machine(placement.machine_of(pin))
    if machine is None:
        return False
    run_sub, run_role = _builds_as(chat, person_sub, person_role)
    agent_default = (agent_store.get_agent(chat["agent"]) or {}).get("execution_target")
    return not placement.may_run_on(machine, run_sub=run_sub,
                                    run_can_manage=roles.can_manage(run_role),
                                    user_paired_allowed=chat_targets.user_machines_allowed(),
                                    agent_default=agent_default)


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
    if placement.machine_is_compute(machine):
        name = chat_targets.label_of(placement.machine_of(chat.get("execution_target") or ""), machine)
        return f"This chat is on {name}, a compute machine. Move it from the Runs on menu."
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
    if chat.get("parent_chat_id"):
        # A lane moves only when a person picks its place: a message must
        # not send its delegator's work to the sender's own default.
        raise MoveRefused(LANE_HANDOVER)
    view = await run_db(chat_targets.agent_view, chat.get("agent") or "", asker.sub, asker.role,
                        admin_asker=asker.platform_admin, engine_path=chat_targets.chat_engine(chat))
    default = view["default_target"]
    entry = next((t for t in view["targets"] if t["target"] == default), None)
    if entry is None or not entry["pickable"]:
        label = entry["label"] if entry else chat_targets.label_of(default)
        offline = f"{chat_targets.place_phrase(default, label, start=True)} is offline, so this chat can't run for you right now."
        raise MoveRefused(machine_away.away_sentence(placement.machine_of(default), label, offline))
    try:
        return await request_move(chat_id, default, asker=asker, origin=move_schema.ORIGIN_HANDOVER)
    except MoveRefused as e:
        if str(e) not in _BUSY:
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
    """Settle the moves waiting for proof (a task of its own that the
    reaper's 60 s tick starts and never waits for): the source's copy goes
    once the target's session file grew past its copied size with the
    chat's ids unchanged; nothing is deleted on a changed id,
    a newer move, a gone machine, a gone chat, or a session something else
    still names (another chat's ids, a task that continues it). A row that
    is not proved yet is asked again later each time (``_not_yet``), and so
    is one whose machine does not answer a read or a delete within the
    leftovers sweep's bound: one silent machine holds no other row."""
    from core.remote import session_files
    from services.remote import switch_leftovers
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
            size = await asyncio.wait_for(session_files.main_size(target, scope, row["main_rel"]),
                                          timeout=switch_leftovers.DELETE_WAIT_S)
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
                    gone = await asyncio.wait_for(session_files.delete_files(source, scope),
                                                  timeout=switch_leftovers.DELETE_WAIT_S)
                    logger.info("chat move %s: resumed on %s; %d files removed from %s",
                                mid[:8], row["to_label"], gone, row["from_label"])
            await _settle(mid, move_schema.SETTLED)
            _proof_next.pop(mid, None)
        except session_files.TransferError as e:
            # A machine that answered busy or refused: asked again later.
            logger.info("chat move %s: not proved yet: %s", mid[:8], e)
            _not_yet(mid)
        except asyncio.TimeoutError:
            logger.info("chat move %s: a machine did not answer within %.0f s; asked again later",
                        mid[:8], switch_leftovers.DELETE_WAIT_S)
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

