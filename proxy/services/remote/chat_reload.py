"""Reloading a chat's session at its agent's request.

The agent calls a tool inside the chat's own turn (after a tool was enabled
for it or an account was connected, or to switch the chat to another of its
person's accounts). The turn ends by itself. The platform then rebuilds the
chat's session from what is stored at that moment (the agent's tools, the
person's accounts and role, the chat's mode and place) and continues the
chat with one line of its own words. Nothing reloads by itself, and a
reload widens nothing: the rebuild is ``core/session/chat_rebuild.py``, with
the builder a fresh chat uses.

The sequence (``_wait_and_reload``), in the shape of the agent's own move
(``chat_move.py``, whose gates it shares): wait for the chat's queue claim
and an idle chat, read the refusals again, hold the chat and close its live
session (``chat_rebuild.closed``), write the chat's account choices and the
note under the hold, give the hold back, then run the resume turn through
the delivery ladder. A message that waited in the chat's queue goes first
and starts the session itself. A reload never ends a turn and never answers
a card: it waits for both. A refusal is a sentence or two, read as written.

A switch of account rides the reload: the chat's choice is written under the
hold, and the note says in the platform's own words which account the chat
uses from then on, whatever reason the agent gave. A person's own chat
chooses among the accounts that person lets the agent use; a chat whose
person's session runs in the agent's space chooses among the agent's own
accounts, for everyone in it, and only an editor or a manager switches it
(read again when the choice is written). A choice is refused where a
session could not honour it (``services/mcp/account_context.py``): on an
engine that builds its own tool config. When one of the agent's own
accounts leaves its set (``remove_service_account``, the one way out), its
event subscriptions go first, then every live session built on it, whatever
the default is now, has its running reply stopped and is closed, and its
chat is told which account it uses now; a chat that chose it or ran on it as
the default is told too, and a token file no row names any more goes. So
none runs on a removed account and none moves to another person's mailbox
with no word. A task, a call, a meeting and a delegated worker's running
round keep the account to their end.

The waiter is a task of this process, and a row says it waits
(``storage/chat/pending_rebuilds.py``). After a restart that row is settled
once (``settle_after_restart``): a chat the platform continues is built from
the stored state by that start, a reload whose session lives on is armed
again, and anything else is told the reload did not happen. A waiting move
is armed again the same way (``chat_move.settle_after_restart``).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable

from core import drain, placement
from core.session.interactive_queue import ACCOUNT_LIMIT_SOURCE, person_sent_hooks
from core.session.visibility import SCOPE_AGENT, SCOPE_USER
from services.remote import chat_move, chat_targets
from storage.chat import pending_rebuilds

logger = logging.getLogger("otodock.remote")

ONLY_A_CHAT = "Only a chat can be reloaded. A task's next run starts with today's tools."
A_CALL = "A call cannot be reloaded. The next call starts with today's tools."
DELEGATED_WORK = ("This chat continues delegated work. Its next round starts with "
                  "today's tools.")
NOT_DRIVER = "Only someone who can continue this chat can reload it."
VOICE_IN_USE = "A voice conversation is using this chat. End it before reloading."
MOVING = "This chat is moving. It starts there with today's tools."
NOT_EDITOR = "Only an editor or a manager of this agent can switch its account."
NOBODY = "A reload needs the person the chat runs as. This session has none."
NO_CHOICE = "This session cannot switch accounts."
RUN_TERMINAL = "This run drives a terminal, which cannot change its account while it runs."
NO_RUN = "This session is not a run's."
#: What a chat is told when its agent's team account switch changes
#: (``team_account_changed``): on, off, and the floor moved.
TEAM_ON = ("This agent now uses its own accounts in every chat. From its next start "
           "this chat uses them too.")
TEAM_OFF = ("This agent's own accounts are no longer used in people's chats. From its "
            "next start this chat uses your own accounts.")
TEAM_FLOOR_MOVED = ("Who may use the agent's own accounts in a chat changed. From its next "
                    "start this chat follows the new setting.")

#: How long a scheduled reload waits for the chat's turn to end.
WAIT_S = 1800.0
#: Reloads of one chat in a row with no message from a person (a limit's
#: continuations: ten, an unattended run meets one real limit per account),
#: and in one hour (counted in this process), after which the platform stops.
CHAIN_MAX = 3
LIMIT_CHAIN_MAX = 10
HOUR_MAX = 6
WINDOW_S = 3600.0
#: How long the watcher looks for a terminal chat's new terminal.
TERMINAL_WATCH_S = 180.0
#: The word that clears a chat's choice of account for a tool.
DEFAULT_ACCOUNT = "default"

# Chat id -> its reload waiting for the turn to end, and its recent reloads.
_waiters: dict[str, asyncio.Task] = {}
#: The waiters that carry a continuation after a limit (``continuation_waits``),
#: each with whether it holds the chat's queue; those that started the chat
#: again (a Stop no longer cancels them); the chats a Stop reached since.
_limit_waiters: dict[asyncio.Task, bool] = {}
_limit_started: set[asyncio.Task | None] = set()
_limit_stopped: set[str] = set()
_recent: dict[str, list[float]] = {}
# Chat id -> the close waiting for its turn to end (``close_when_idle``), and
# the reason it was asked for.
_idle_closes: dict[str, asyncio.Task] = {}
_idle_close_reasons: dict[str, str] = {}
# Session id -> the round a run's own call asked for (``schedule_for_run``),
# taken by the run after its turn (``take_task_round``): the reason and the
# sentences of the accounts it switched.
_task_rounds: dict[str, tuple[str, list[str]]] = {}
# Chat id -> when its continuation after a limit went out (monotonic): the
# turn it starts gets no second one (``limit_marked``); when it started fresh
# from its history after a refusal of its conversation (``fresh_marked``).
_limit_marks: dict[str, float] = {}
_fresh_marks: dict[str, float] = {}
#: A spoken conversation that holds a chat, read by the continuation's wait as
#: the reload's ``blocked`` (the socket layer appends its reader).
voice_holds: list[Callable[[str], str]] = []

#: The source of the continuation after a limit. A terminal's queue never
#: types it over text waiting in its input box nor into a turn
#: (``interactive_queue``).
LIMIT_SOURCE = ACCOUNT_LIMIT_SOURCE
#: The chat's line when a limit cut its turn and another account takes it on.
LIMIT_LINE = ("This chat's AI account reached its usage limit during a reply. "
              "The chat goes on with another account.")
#: The words of the continuation turn: the platform's, never the person's.
LIMIT_NUDGE = ("The account this chat ran on reached its usage limit while you were working. The chat "
               "continues on another account. A tool call that had not answered may or may not have "
               "finished, so check what it did before you repeat it. Continue from where you stopped.")
#: The chat's line when another account cannot read the conversation it took on.
LIMIT_FRESH_LINE = ("The new account could not read this chat's earlier reasoning. "
                    "The chat continues from its history.")
#: The words of the turn that starts the chat again from its history.
FRESH_NUDGE = ("This chat now runs on another account, which starts it again from its history. A tool "
               "call that had not answered may or may not have finished, so check what it did before "
               "you repeat it. Continue from where you stopped.")
#: The chat's lines when the continuation does not happen.
LIMIT_FAILED = "The chat did not continue on another account. Send a message to continue."
LIMIT_STOPPED = "Stopped. This chat does not continue on another account by itself. Send a message to continue."
LIMIT_NOT_MOVED = "The chat could not continue on another account. Send a message to continue."
LIMIT_RESTARTED = ("The chat did not continue on another account: the platform restarted. "
                   "Send a message to continue.")
#: The chat's one line when its terminal holds unsent text past ``LIMIT_HOLD_S``.
LIMIT_HELD = "The chat continues on another account when the text in its terminal is sent or cleared."
LIMIT_HOLD_S = 15 * 60.0
#: How long a continuation waits for an in-place session's sign-in to move.
LIMIT_MOVE_WAIT_S = 120.0
#: How long a continuation's mark lasts with no turn end to clear it.
LIMIT_MARK_S = 3600.0


class ReloadRefused(Exception):
    """A reload refused; the message is the sentence the agent reads."""


def waiting(chat_id: str) -> bool:
    """A reload of the chat waits for its turn to end."""
    task = _waiters.get(chat_id)
    return task is not None and not task.done()


def cancel(chat_id: str) -> None:
    """Drop the chat's waiting reload: a move rebuilds the session itself."""
    task = _waiters.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()
        logger.info("chat reload: chat=%s: its waiting reload gives way to a move", chat_id[:8])


def reloaded_sentence(reason: str, switched: list[str]) -> str:
    """The chat's line for a reload that happened: the agent's reason, then
    the platform's own sentence for each account the chat changed to."""
    return " ".join([f"The session was reloaded: {reason}.", *switched])


def resume_nudge(reason: str, switched: list[str] | None = None) -> str:
    """The resumed turn's prompt: the platform's words, one line."""
    return f"{reloaded_sentence(reason, switched or [])} Continue where you were."


def _why(reason: str) -> str:
    """The agent's reason as the tail of a sentence: one line, no full stop."""
    return " ".join((reason or "").split()).rstrip(".!? ")[:200] or "its tools or accounts changed"


def _refusal(chat: dict, asker: chat_move.Asker) -> str:
    """Why this chat is not reloaded for this asker (``""``: it is). Read
    at the call and again when the reload fires. Synchronous."""
    from core.session import session_kind, visibility
    cid, owner = chat.get("id") or "", chat.get("user_sub") or ""
    kind = session_kind.of_chat(chat)
    if kind is session_kind.PHONE or visibility.is_phone_chat_owner(owner):
        return A_CALL
    if kind is not session_kind.DASHBOARD:
        return ONLY_A_CHAT
    if chat_move._continues_delegated_work(chat):
        return DELEGATED_WORK
    if not chat_move.may_drive(chat, asker):
        return NOT_DRIVER
    if chat_move.moving(cid) or cid in chat_move._waiters:
        return MOVING
    return ""


def _choices(chat: dict, asker: chat_move.Asker, accounts: dict[str, str] | None, *,
             scope: str | None = None) -> dict[str, str]:
    """The account choices of a reload, checked: ``{tool: label}``, the label
    ``""`` for a choice to clear. Each tool is one of the agent's, each label
    one of the accounts the person lets this agent use for it: a label the
    set holds is that account, whatever its word (a person's first account
    of a tool is labelled ``default``), and only else does ``DEFAULT_ACCOUNT``
    clear the choice. Refused where a session of the chat could not honour a
    choice (``account_context``). ``scope``: a run's choice scope
    (``account_context.choice_scope`` with ``task``), whose set is the run's
    identity's, with no editor check for a run with no person; None for a
    person's chat. Synchronous."""
    if not accounts:
        return {}
    from auth import roles
    from services.mcp import account_context, mcp_registry
    from storage.identity import credential_store, service_account_set
    word = account_context.word
    agent = chat.get("agent") or ""
    floor = account_context.team_floor_refusal(agent, asker.role)
    if floor:
        raise ReloadRefused(floor)
    if scope is not None:
        if not scope:
            raise ReloadRefused(NO_CHOICE)
        agents_own = scope == SCOPE_AGENT
        if agents_own and asker.sub and not roles.can_edit(asker.role):
            raise ReloadRefused(NOT_EDITOR)
    else:
        agents_own = _agents_own(chat, role=asker.role)
        if agents_own and not roles.can_edit(asker.role):
            raise ReloadRefused(NOT_EDITOR)
    tools = {m.name: m for m in mcp_registry.get_agent_mcps_all_placements(agent)}
    out: dict[str, str] = {}
    for tool, label in accounts.items():
        manifest = tools.get(tool)
        if manifest is None:
            raise ReloadRefused(f"{word(tool)} is not one of this agent's tools.")
        name = word(manifest.label or tool)
        held = [row.account_label for row in (
            service_account_set.list_service_agent_set(tool, agent) if agents_own
            else credential_store.list_agent_account_set(asker.sub, tool, agent))]
        # The label as stored, else the one label the prompt shows by that
        # name (a label from before the one-line rule reads there as one line).
        shown = [h for h in held if word(h) == label]
        account = label if label in held else shown[0] if len(shown) == 1 else None
        if account is not None:
            out[tool] = account
        elif label == DEFAULT_ACCOUNT:
            out[tool] = ""
        elif agents_own:
            raise ReloadRefused(f"{word(label)} is not one of this agent's accounts for {name}.")
        else:
            raise ReloadRefused(
                f"{word(label)} is not one of the accounts the person lets this agent use "
                f"for {name}.")
    return out


def _agents_own(chat: dict, *, role: str = "") -> bool:
    """Whether the chat chooses among the agent's own accounts
    (``account_context.chooses_agents_accounts``). Synchronous."""
    from services.mcp import account_context
    return account_context.chooses_agents_accounts(chat, role=role)


def _mark(chat: dict) -> tuple[str, int]:
    """The life of the chat's session now: its id and the floor of its token
    (a session started again under the same id has a later floor)."""
    from core.session import session_state
    sid = chat.get("session_id") or ""
    return sid, (session_state.live_state(sid)[1] if sid else 0)


async def schedule_after_turn(chat_id: str, *, asker: chat_move.Asker, reason: str,
                              accounts: dict[str, str] | None = None,
                              blocked: Callable[[str], str] | None = None) -> dict:
    """The agent's tool, called inside the chat's own turn: check now, then
    wait for the turn's end and reload. ``accounts`` is the chat's choice of
    account per tool for its person (``DEFAULT_ACCOUNT`` clears one);
    ``blocked`` a socket-side reason of the caller's (a voice bridge on the
    chat). ``ReloadRefused`` with the sentence."""
    from storage import database as task_store
    from storage.pg import run_db
    if not asker.sub:
        raise ReloadRefused(NOBODY)
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat:
        raise ReloadRefused(chat_move.NOT_FOUND)
    if blocked is not None and blocked(chat_id):
        raise ReloadRefused(VOICE_IN_USE)
    why = await run_db(_refusal, chat, asker)
    if why:
        raise ReloadRefused(why)
    choices = await run_db(_choices, chat, asker, accounts)
    earlier = _waiters.pop(chat_id, None)
    if earlier is not None and not earlier.done():
        earlier.cancel()
        logger.info("chat reload: chat=%s: the agent's later reload replaces its earlier one",
                    chat_id[:8])
    await run_db(functools.partial(
        pending_rebuilds.save, chat_id, pending_rebuilds.RELOAD, person=asker.sub,
        wait_s=WAIT_S, reason=_why(reason), choices=choices))
    _arm(chat_id, asker, _why(reason), choices, _mark(chat), blocked, WAIT_S)
    return {"scheduled": True, "accounts": choices}


def _arm(chat_id: str, asker: chat_move.Asker, reason: str, choices: dict[str, str],
         mark: tuple[str, int], blocked: Callable[[str], str] | None, wait_s: float, *,
         limit: _Limit | None = None) -> None:
    """Start the chat's waiter. Its row goes when it ends, unless a later
    request replaced it (that one's row is the chat's) or the platform's own
    stop cut the wait: the next boot settles the row, as after a crash. A
    continuation after a limit (``limit``) begins first (its line, the newest
    person row) and writes its own row with it, never over a reload's or a
    move's; its end lets go of the chat's queue, whatever way it ends."""
    from core import background
    from core.events import input_queue
    what = pending_rebuilds.LIMIT if limit is not None else pending_rebuilds.RELOAD

    async def _waiter() -> None:
        from storage.pg import run_db
        cut_by_the_stop = False
        begun, saved = limit, None
        try:
            if begun is not None:
                begun = await _limit_begins(chat_id, begun)
                saved = asyncio.ensure_future(run_db(functools.partial(
                    pending_rebuilds.save, chat_id, what, person=asker.sub, wait_s=wait_s,
                    target=begun.target(), replace=False)))
                await asyncio.shield(saved)   # a Stop now clears the row after it is written
            # A reload's call keeps the shape it always had.
            await _wait_and_reload(chat_id, asker, reason, choices, mark, blocked, wait_s,
                                   **({"limit": begun} if begun is not None else {}))
        except asyncio.CancelledError:
            cut_by_the_stop = drain.stopping()
            raise
        finally:
            if limit is not None and limit.holds_queue:
                input_queue.first_turn_not_run(chat_id)
            if not cut_by_the_stop and _waiters.get(chat_id) is asyncio.current_task():
                with contextlib.suppress(Exception):
                    if saved is not None:
                        await asyncio.shield(saved)
                    await asyncio.shield(run_db(pending_rebuilds.clear, chat_id, what))

    waiter = background.spawn(_waiter(), name="chat-reload-wait", owner="chat_reload")
    _waiters[chat_id] = waiter
    if limit is not None:
        _limit_waiters[waiter] = limit.holds_queue
        # A Stop is about the continuation of its moment. One left behind (a
        # Stop while the terminal already typed the continuation) is not this one's.
        _limit_stopped.discard(chat_id)

    def _done(t: asyncio.Task) -> None:
        _limit_waiters.pop(t, None)
        _limit_started.discard(t)
        if _waiters.get(chat_id) is t:
            _waiters.pop(chat_id, None)
            _limit_stopped.discard(chat_id)
    waiter.add_done_callback(_done)


async def settle_after_restart(row: dict, *, continued: bool, person: str = "",
                               lives_on: bool = False) -> list[str]:
    """The row of a reload or a move that waited when the platform stopped
    (taken by the caller, so this runs once per row). A row past its
    deadline is told so. ``continued``: the platform continues the chat, and
    that start builds from the stored state: a reload's account choices are
    written now and the chat is told it was reloaded (the sentences are
    returned). ``lives_on``: the chat's session is still there (its machine
    kept the turn, or no turn was cut), so a reload is armed again with what
    is left of its wait. Otherwise the chat is told it did not happen. A
    move's row is ``chat_move.settle_after_restart``'s."""
    from auth import roles
    from auth.providers import effective_role_of
    from storage import database as task_store
    from storage.pg import run_db
    from ws import wire_events as wire
    chat_id = row["chat_id"]
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat:
        return []
    if row["what"] == pending_rebuilds.MOVE:
        # A chat the platform continues lives on too: the move waits for the
        # continued turn to end.
        await chat_move.settle_after_restart(row, lives_on=lives_on or continued)
        return []
    if row["what"] == pending_rebuilds.LIMIT:
        await _settle_limit(row, chat, continued=continued, lives_on=lives_on)
        return []
    left = pending_rebuilds.seconds_left(row)
    if left <= 0:
        await _note(chat_id, "The session was not reloaded: the turn did not end within "
                    "30 minutes.", wire.MOVED_FAILED)
        return []
    who = row.get("person") or person
    if continued:
        switched = await run_db(_write_choices, chat, who, row["choices"]) if who else []
        await _note(chat_id, reloaded_sentence(row.get("reason") or _why(""), switched))
        return switched
    standing = await run_db(effective_role_of, who, chat.get("agent") or "") if who else ""
    if lives_on and who and standing != roles.NO_ACCESS:
        asker = chat_move.Asker(sub=who, role=standing, platform_admin=roles.is_admin(standing))
        await run_db(functools.partial(
            pending_rebuilds.save, chat_id, pending_rebuilds.RELOAD, person=who, wait_s=left,
            reason=row.get("reason") or "", choices=row["choices"]))
        _arm(chat_id, asker, row.get("reason") or _why(""), row["choices"], _mark(chat), None, left)
        logger.info("chat reload: chat=%s: its reload waits again after the restart", chat_id[:8])
        return []
    await _note(chat_id, "The session was not reloaded: the platform restarted. Ask again.",
                wire.MOVED_FAILED)
    return []


async def _settle_limit(row: dict, chat: dict, *, continued: bool, lives_on: bool) -> None:
    """A continuation after a limit that waited when the platform stopped. A
    chat the platform continues needs none (its turn starts on the pool's
    pick, the old account rests). One whose session lives on, its person's
    standing intact, waits again for what is left of its wait. Otherwise the
    chat is told it did not continue."""
    from auth import roles
    from auth.providers import effective_role_of
    from storage.pg import run_db
    from ws import wire_events as wire
    if continued:
        return
    chat_id, who = row["chat_id"], row.get("person") or ""
    limit = _Limit.from_target(row.get("target") or "")
    left = pending_rebuilds.seconds_left(row)
    standing = await run_db(effective_role_of, who, chat.get("agent") or "") if who else ""
    if (limit is not None and lives_on and left > 0 and who and standing != roles.NO_ACCESS
            and schedule_for_limit(chat_id, person=who, session_id=limit.session_id,
                                   old_sub=limit.old_sub, in_place=limit.in_place, fresh=limit.fresh,
                                   told=True, since=limit.since, wait_s=left,
                                   holds_queue=not await run_db(_is_terminal_chat, chat))):
        logger.info("chat reload: chat=%s: its continuation waits again after the restart", chat_id[:8])
        return
    await _note(chat_id, LIMIT_RESTARTED, wire.MOVED_FAILED)


def _note_block(message: str, reason: str) -> dict:
    from ws import wire_events as wire
    return {"type": wire.SYSTEM, "subtype": wire.SUBTYPE_SESSION_RELOADED,
            "message": message, "reason": reason}


async def _note(chat_id: str, message: str, reason: str = "", *, touch_chat: bool = True) -> None:
    """The chat's line about its reload: a history row on the chat's lane,
    and the same block to whoever views the chat."""
    from core.events import chat_writer
    from core.session.session_state import broadcast_chat_frame
    from storage import database as task_store
    from ws import wire_events as wire
    block = _note_block(message, reason)
    await chat_writer.submit(
        chat_id,
        functools.partial(task_store.add_chat_message, chat_id, "event", "",
                          event_type=wire.SYSTEM, event_data=json.dumps(block),
                          touch_chat=touch_chat),
        label="session_reloaded")
    broadcast_chat_frame(chat_id, {**block, "chat_id": chat_id})


def _away_machine(chat: dict) -> str:
    """The name of the chat's machine when it is not connected (``""``: the
    chat runs on the platform, or its machine is here). Synchronous."""
    pin = chat.get("execution_target") or ""
    if placement.is_local(pin):
        return ""
    from core.remote.satellite_connection import get_connection_manager
    if get_connection_manager().is_connected(placement.machine_of(pin)):
        return ""
    return chat_targets.label_of(pin)


def _recent_reloads(chat_id: str) -> list[float]:
    """The times of the chat's reloads in the last hour, in this process."""
    now = time.monotonic()
    for stale in [k for k, v in _recent.items() if not v or now - v[-1] >= WINDOW_S]:
        _recent.pop(stale, None)
    return [t for t in _recent.get(chat_id, []) if now - t < WINDOW_S]


def _bound(chat_id: str, chain: int, *, run: bool = False, limit: bool = False) -> str:
    """The sentence when the agent reloaded this chat too often (``""``: it
    may). It counts nothing: a reload is counted when it happened (``_count``).
    ``run``: a run's own switch. ``limit``: a continuation after a limit."""
    chain_max = LIMIT_CHAIN_MAX if limit else CHAIN_MAX
    if chain >= chain_max:
        times, when = chain_max, "" if run else " in a row with no message from you"
    elif len(_recent_reloads(chat_id)) >= HOUR_MAX:
        times, when = HOUR_MAX, " in the last hour"
    else:
        return ""
    if run:
        return f"This run switched its account {times} times{when}, so it stops here."
    what = "This chat changed account" if limit else "The agent reloaded this chat"
    return f"{what} {times} times{when}, so it stops here. Send a message to continue."


def _count(chat_id: str) -> None:
    """Count a reload that happened into the hour's window."""
    _recent[chat_id] = [*_recent_reloads(chat_id), time.monotonic()]


def _write_choices(chat: dict, person: str, choices: dict[str, str], *,
                   scope: str | None = None) -> list[str]:
    """Write the chat's choices of account, under the reload's hold, and
    answer the platform's sentence for each one written: the account the
    chat uses from now on. A label that left the set since the call is
    skipped with no sentence (the set's default takes the tool). A chat in
    the agent's space writes its own choice among the agent's accounts,
    only while the person is an editor or a manager of the agent and the
    account's lender still manages it (read now: the call may be long gone,
    or a restart settles it), and its line names who switched. ``scope``: a
    run's choice scope (``schedule_for_run``): the run's identity's set,
    written at the call, the line naming the run. Synchronous."""
    from auth import roles
    from auth.providers import effective_role_of
    from services.mcp import account_context, mcp_registry
    from storage.identity import credential_store, service_account_set
    word = account_context.word
    cid, agent = chat["id"], chat.get("agent") or ""
    by_run = scope is not None
    role = effective_role_of(person, agent) if person else roles.NO_ACCESS
    agents_own = scope == SCOPE_AGENT if by_run else _agents_own(chat, role=role)
    if agents_own and (person or not by_run) and not roles.can_edit(role):
        logger.info("chat reload: chat=%s: its switch is not written, the person is no "
                    "longer an editor", cid[:8])
        return []
    # A run's line is person-less ("This run now uses…"); a chat on the
    # agent's accounts names who switched.
    who = "" if by_run else (account_context.person_name(person) if agents_own else "")
    this = "this run" if by_run else "this chat"
    said: list[str] = []
    for tool, label in choices.items():
        manifest = mcp_registry.get_manifest(tool)
        name = word((manifest.label if manifest is not None else "") or tool)
        if not label:
            if agents_own:
                service_account_set.clear_chat_service_choice(cid, tool)
            else:
                credential_store.clear_chat_account_choice(cid, person, tool)
            said.append(f"{who} put {this} back on the agent's default account for {name}." if who
                        else f"{this.capitalize()} is back on the agent's default account for {name}.")
        elif agents_own:
            if (credential_store.get_service_agent_binding(tool, agent, account_label=label) is not None
                    and service_account_set.set_chat_service_choice(cid, agent, tool, label,
                                                                    set_by=person or f"run:{agent}")):
                said.append(f"{who} switched {this} to {word(label)} for {name}." if who
                            else f"{this.capitalize()} now uses {word(label)} for {name}.")
            else:
                logger.info("chat reload: chat=%s: %s is no longer one of the agent's for %s",
                            cid[:8], label, tool)
        elif credential_store.set_chat_account_choice(cid, person, agent, tool, label):
            said.append(f"{this.capitalize()} now uses {word(label)} for {name}.")
        else:
            logger.info("chat reload: chat=%s: %s is no longer in the set for %s",
                        cid[:8], label, tool)
    return said


async def schedule_for_run(chat_id: str, *, asker: chat_move.Asker, session_id: str, reason: str,
                           accounts: dict[str, str] | None = None) -> dict:
    """The agent's tool, called inside a run's own turn (a task's, a
    delegated worker's): the choices checked against the run's identity's
    set (``account_context.choice_scope`` with ``task``: the agent's own for a
    run with no person or on an agent whose people use its accounts, the
    creator's for their own run) and written now (nothing reads a run's
    choice before its next build), and the run's next round recorded
    (``take_task_round``): the run closes its session after this turn and
    goes on with the account. A run driving a live terminal is refused, and
    so is one past the chain bound. ``ReloadRefused`` with the sentence."""
    from core.session import interactive_session
    from services.mcp import account_context
    from storage import database as task_store, remote_store
    from storage.pg import run_db
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat:
        raise ReloadRefused(chat_move.NOT_FOUND)
    if interactive_session.get(session_id) is not None:
        raise ReloadRefused(RUN_TERMINAL)
    stopped = _bound(chat_id, await run_db(remote_store.reloads_since_person, chat_id), run=True)
    if stopped:
        raise ReloadRefused(stopped)
    scope = await run_db(functools.partial(
        account_context.choice_scope, chat_id, mount_scope=SCOPE_USER if asker.sub else SCOPE_AGENT,
        role=asker.role, task=True))
    choices = await run_db(functools.partial(_choices, chat, asker, accounts, scope=scope))
    said = await run_db(functools.partial(_write_choices, chat, asker.sub, choices, scope=scope))
    _task_rounds[session_id] = (_why(reason), said)
    return {"scheduled": True, "accounts": choices}


def take_task_round(session_id: str) -> tuple[str, list[str]] | None:
    """The round a run's own call asked for, taken once (``TaskRun``): the
    reason and the sentences of the switch; None when none waits."""
    return _task_rounds.pop(session_id, None)


def drop_task_round(session_id: str) -> None:
    """A run that ends without its round (cancelled, failed, cut) drops it."""
    _task_rounds.pop(session_id, None)


async def note_limit_round(chat_id: str) -> None:
    """The run's chat line when a limit cut its turn and its next round goes
    on another account, counted into the chain's window."""
    _count(chat_id)
    await _account_note(chat_id, LIMIT_LINE)


async def note_round(chat_id: str, reason: str, said: list[str]) -> None:
    """The chat's line when a run's round starts (``reloaded_sentence``),
    counted into the chain's window."""
    _count(chat_id)
    await _note(chat_id, reloaded_sentence(reason, said))


def _chats_on_default(mcp_name: str, agent_name: str) -> list[str]:
    """The dashboard chats that run on the agent's default account of the
    tool and chose none: the agent's pool (a Shared-only history, work the
    agent delegated) and, on an agent whose people use its accounts, every
    chat. Synchronous."""
    from core.session.visibility import shared_chat_owner
    from services.mcp import account_context
    from storage.identity import service_account_set
    return service_account_set.chats_without_a_choice(
        mcp_name, agent_name, chat_owner=shared_chat_owner(agent_name),
        any_owner=account_context.people_use_agents_accounts(agent_name))


@dataclasses.dataclass(frozen=True)
class Leaving:
    """What a removal from the agent's own set of a tool takes, read before
    it: the accounts (``(lender's sub, label)`` pairs), the installations
    they act as, and the chats whose next start moves to another account,
    the chats that chose one (``chose``) and, when the default is one, the
    chats on it (``on_default``)."""
    accounts: frozenset = frozenset()
    chose: tuple = ()
    on_default: tuple = ()
    installations: frozenset = frozenset()


def chats_losing_account(mcp_name: str, agent_name: str, goes: Callable) -> Leaving:
    """What the removal of the rows of the agent's own set that ``goes``
    picks takes: read before the removal. Synchronous."""
    from storage.identity import service_account_set
    going = [row for row in service_account_set.list_service_agent_set(
        mcp_name, agent_name, standing_only=False) if goes(row)]
    chose = service_account_set.chats_choosing(
        mcp_name, agent_name, [row.account_label for row in going])
    on_default = (_chats_on_default(mcp_name, agent_name)
                  if any(row.is_default for row in going) else [])
    return Leaving(frozenset((row.account_owner_sub, row.account_label) for row in going),
                   tuple(chose), tuple(on_default),
                   frozenset(row.installation_id for row in going if row.installation_id))


def _account_now(mcp_name: str, agent_name: str) -> tuple[str, str]:
    """The agent's default account of the tool that stands, as ``(lender's
    sub, how a line names it)``, or ``("", "")``. Synchronous."""
    from services.mcp import account_context
    from storage.identity import credential_store
    binding = credential_store.get_service_agent_binding(mcp_name, agent_name)
    if binding is None:
        return "", ""
    return binding.account_owner_sub, account_context.named_account(
        binding.account_owner_sub, mcp_name, binding.account_label)


def _computer_owner(chat: dict) -> str | None:
    """The owner of the person's own computer the chat is placed on, or None
    when it runs anywhere else: there a Shared-only chat takes only that
    person's accounts (``config_builder.personal_owner_of``). Synchronous."""
    pin = chat.get("execution_target") or ""
    if placement.is_local(pin):
        return None
    from storage import remote_store
    machine = remote_store.get_remote_machine(placement.machine_of(pin)) or {}
    if (machine.get("pairing_scope") or "") != placement.PAIRING_USER:
        return None
    return machine.get("registered_by") or ""


def _lines(mcp_name: str, agent_name: str, chat_ids: list[str], *,
           removed: set[str], default_went: bool) -> dict[str, str]:
    """``{chat id: its line}`` about the agent's own account of the tool, each
    true for that chat afterwards: a chat whose account went (``removed``)
    reads that and what it uses now; any other reads the default it moves to
    at its next start, or, when the default was removed (``default_went``)
    and none is left, that it has none. A chat on a person's own computer is
    never told of an account it cannot use there. A chat gone meanwhile is
    left out. Synchronous."""
    from storage import database as task_store
    name = _tool_word(mcp_name)
    owner, now = _account_now(mcp_name, agent_name)
    out: dict[str, str] = {}
    for cid in chat_ids:
        chat = task_store.get_chat(cid)
        if not chat:
            continue
        computer = _computer_owner(chat)
        usable = bool(now) and (computer is None or computer == owner)
        if cid in removed:
            out[cid] = (f"The account this chat used for {name} was removed from the agent. "
                        + (f"The chat now uses {now}." if usable
                           else f"It has no other account for {name} on this computer."
                           if now else f"It has no other account for {name} now."))
        elif now:
            out[cid] = (f"The default account for {name} is now {now}. "
                        + ("This chat moves to it at its next start." if usable
                           else "This chat cannot use it on this computer."))
        elif default_went:
            out[cid] = (f"The default account for {name} was removed from the agent. "
                        f"It has no other account for {name} now.")
    return out


def _tool_word(mcp_name: str) -> str:
    from services.mcp import account_context, mcp_registry
    manifest = mcp_registry.get_manifest(mcp_name)
    return account_context.word((manifest.label if manifest is not None else "") or mcp_name)


async def _account_note(chat_id: str, message: str) -> None:
    """A chat's line about the agent's own account: no reload of the chain,
    and the chat keeps its place in the list."""
    from ws import wire_events as wire
    await _note(chat_id, message, wire.MOVED_ACCOUNT_CHANGED, touch_chat=False)


async def _tell(lines: dict[str, str]) -> None:
    """Each chat's line, on its own: one that cannot be told stops none of
    the others."""
    for cid, line in lines.items():
        try:
            await _account_note(cid, line)
        except Exception:  # noqa: BLE001  # the rest of the chats are still told
            logger.warning("chat reload: chat=%s was not told about the agent's account",
                           cid[:8], exc_info=True)


async def remove_service_account(mcp_name: str, agent_name: str, *, owner_sub: str | None = None,
                                 account_label: str | None = None,
                                 installation_id: str | None = None) -> bool:
    """Take rows of the agent's own set of a tool out (the filters of
    ``credential_store.remove_service_agent_binding``): each leaving
    account's event subscriptions first, through its lender's own token
    while it still resolves; then the rows; then every live session built on
    one is closed and the chats told (``account_left``); then a token file
    of an installation no row names any more goes. Returns whether a row
    went."""
    from storage.identity import credential_store
    from storage.pg import run_db
    filters = {k: v for k, v in (("owner_sub", owner_sub), ("account_label", account_label),
                                 ("installation_id", installation_id)) if v is not None}

    def goes(row) -> bool:
        return ((owner_sub is None or row.account_owner_sub == owner_sub)
                and (account_label is None or row.account_label == account_label)
                and (installation_id is None or row.installation_id == installation_id))

    leaving = await run_db(chats_losing_account, mcp_name, agent_name, goes)
    await _drop_subscriptions(mcp_name, agent_name, leaving)
    gone = await run_db(functools.partial(
        credential_store.remove_service_agent_binding, mcp_name, agent_name, **filters))
    if not gone:
        return False
    await account_left(mcp_name, agent_name, leaving)
    await _reap(mcp_name, leaving)
    return True


async def _drop_subscriptions(mcp_name: str, agent_name: str, leaving: Leaving) -> None:
    """The leaving accounts' service subscriptions, unregistered through each
    lender's own token. One that fails is logged; the removal goes on."""
    from services.webhooks import subscription_manager
    for owner, label in sorted(leaving.accounts):
        try:
            await subscription_manager.cleanup_account_subscriptions(
                scope="service", owner=owner, mcp_name=mcp_name, account_label=label,
                agent=agent_name, token_owner_sub=owner)
        except Exception:  # the account still leaves; the sweep retries the vendor
            logger.exception("chat reload: the subscriptions of %s on %s were not removed",
                             mcp_name, agent_name)


async def _reap(mcp_name: str, leaving: Leaving) -> None:
    """The token files of the leaving installations no standing row names."""
    if not leaving.installations:
        return
    from services.mcp import mcp_registry
    from services.oauth import github_apps
    from storage.pg import run_db
    manifest = mcp_registry.get_manifest(mcp_name)
    oauth = (manifest.credentials.oauth or {}) if manifest is not None and manifest.credentials else {}
    provider_id = str(oauth.get("provider_id") or "")
    for installation in sorted(leaving.installations):
        await run_db(github_apps.reap_if_orphan, provider_id, installation)


async def default_moved(mcp_name: str, agent_name: str, *, already_told: set[str] | None = None) -> None:
    """The agent's default account of a tool moved and the old one stays in
    the set: each chat on the default is told. Its session goes on, on an
    account the agent still has, so no running turn stops for a change of
    default; the chat moves when its session next starts."""
    from storage.pg import run_db
    chats = [cid for cid in await run_db(_chats_on_default, mcp_name, agent_name)
             if cid not in (already_told or set())]
    await _tell(await run_db(functools.partial(
        _lines, mcp_name, agent_name, chats, removed=set(), default_went=False)))


async def team_account_changed(agent_name: str, *, lends: bool | None) -> None:
    """The agent's team account changed: the switch went on or off
    (``lends``: what people's chats run on now) or its floor moved (None).
    Every live session of a dashboard chat on the agent with a person behind
    it (the chats whose accounts the switch decides: ``offboarding_sessions.live_sessions``)
    is closed, kept to its transcript, once its turn is over: now when it is
    idle, else when the open turn ends (``close_when_idle``), never stopping
    a turn; and each such chat is told one line. A pre-warm is closed
    outright. A task, a call, a meeting and a worker's round keep their
    accounts to their end."""
    from core.session import chat_rebuild
    from services.agents import offboarding_sessions as closer
    line = TEAM_FLOOR_MOVED if lends is None else (TEAM_ON if lends else TEAM_OFF)
    live = [v.session_id for v in closer.live_sessions()
            if v.agent == agent_name and v.kind != closer.WORKER]
    closing = await _sessions_to_close(live, agent_name)
    told: dict[str, str] = {}
    for victim, chat in closing.values():
        if chat is None:
            await closer.close_sessions([victim], f"team account of {agent_name} changed",
                                        reason="team-account", revoked=False)
            continue
        cid = chat["id"]
        if chat_rebuild.turn_open_while_held(chat):
            close_when_idle(cid)
        else:
            try:
                await chat_rebuild.close_chat_session(
                    chat, reason="team-account", require_idle=True, lock=True)
            except chat_rebuild.ChatBusy:
                close_when_idle(cid)
            except Exception:  # noqa: BLE001  # the chat is told either way; its next start resolves anew
                logger.warning("chat reload: chat=%s was not closed for the team account",
                               cid[:8], exc_info=True)
        told[cid] = line
    await _tell(told)
    # A warm app manager keeps the accounts it was built with: the idle ones
    # go now, a busy one when its press ends (``headless_exec.drop_idle``).
    from services.apps import headless_exec
    await headless_exec.drop_idle(agent_name)


def close_when_idle(chat_id: str, *, reason: str = "team-account",
                    hold: Callable[[], bool] | None = None,
                    on_close: Callable[[], Awaitable[None]] | None = None) -> None:
    """Close the chat's live session once the turn open on it ends
    (``chat_move.open_turn``, polled), as long as that session lives; a later call
    replaces an earlier one. The turn is never stopped. On the loop.

    ``hold``: read at each poll with the turn ended; True keeps the session
    open for now (a terminal whose input box holds the person's unsent
    text). ``on_close``: awaited once the session was closed."""
    old = _idle_closes.pop(chat_id, None)
    if old is not None:
        old.cancel()
    _idle_close_reasons[chat_id] = reason
    _idle_closes[chat_id] = asyncio.ensure_future(
        _close_when_idle(chat_id, reason=reason, hold=hold, on_close=on_close))


def close_waiting(chat_id: str, reason: str) -> bool:
    """Whether a close asked for ``reason`` already waits for the chat's turn
    to end: a caller that asks again keeps it, and with it its ``hold``."""
    task = _idle_closes.get(chat_id)
    return task is not None and not task.done() and _idle_close_reasons.get(chat_id) == reason


async def note_account_change(chat_id: str, message: str, *, waits: bool = False) -> None:
    """The chat's line about the AI account it runs on: "Account changed", or
    with ``waits`` "Account change waiting" (the text typed in its terminal
    holds the change)."""
    if waits:
        from ws import wire_events as wire
        await _note(chat_id, message, wire.MOVED_ACCOUNT_CHANGE_WAITS, touch_chat=False)
        return
    await _account_note(chat_id, message)


async def _close_when_idle(chat_id: str, *, reason: str = "team-account",
                           hold: Callable[[], bool] | None = None,
                           on_close: Callable[[], Awaitable[None]] | None = None) -> None:
    from core.session import chat_rebuild, session_state
    from storage import database as task_store
    from storage.pg import run_db
    try:
        chat = await run_db(task_store.get_chat, chat_id)
        sid = (chat or {}).get("session_id") or ""
        # While the session the change found lives: a turn open for hours
        # still ends on the old identity, never a later one. A session that
        # ended or was built again (a new start resolved anew) needs no close.
        while chat and sid and chat.get("session_id") == sid and session_state.session_is_held(sid):
            if not await run_db(chat_move.open_turn, chat) and not (hold and hold()):
                try:
                    await chat_rebuild.close_chat_session(
                        chat, reason=reason, require_idle=True, lock=True)
                    if on_close is not None:
                        await on_close()
                    return
                except chat_rebuild.ChatBusy:
                    pass
            await asyncio.sleep(chat_move.TURN_POLL_S)
            chat = await run_db(task_store.get_chat, chat_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001  # the chat was told; its next start resolves anew
        logger.warning("chat reload: chat=%s: the close that waited failed", chat_id[:8],
                       exc_info=True)
    finally:
        if _idle_closes.get(chat_id) is asyncio.current_task():
            _idle_closes.pop(chat_id, None)
            _idle_close_reasons.pop(chat_id, None)


async def _sessions_to_close(session_ids: list[str], agent_name: str) -> dict[str, tuple]:
    """``{session id: (offboarding_sessions.Live, its chat row or None)}`` of
    the sessions built on an account that left (only a session that ran on
    the agent's own accounts recorded one): a dashboard chat's session or
    terminal, whatever its owner or the agent's mode, and a pre-warm. A
    task, a call, a meeting and an app keep the account to their end, and so
    does a delegated worker whose round is running."""
    from core.session import (
        chat_rebuild,
        interactive_session,
        prewarm_session_registry as prewarm,
        session_kind,
    )
    from services.agents import offboarding_sessions as closer
    from storage import database as task_store
    from storage.pg import run_db
    out: dict[str, tuple] = {}
    for sid in session_ids:
        if sid in prewarm._entries:
            out[sid] = (closer.Live(sid, agent_name, closer.PREWARM, "", "", ""), None)
            continue
        terminal = interactive_session.get(sid)
        chat = await run_db(task_store.get_chat, terminal.chat_id) if terminal is not None \
            else await run_db(task_store.get_chat_by_session, sid)
        if not chat or session_kind.of_chat(chat) is not session_kind.DASHBOARD:
            continue
        if chat_move._continues_delegated_work(chat) and chat_rebuild.turn_open_while_held(chat):
            continue
        kind = closer.INTERACTIVE if terminal is not None else closer.POOL
        out[sid] = (closer.Live(sid, agent_name, kind, "", "", ""), chat)
    return out


async def account_left(mcp_name: str, agent_name: str, leaving: Leaving) -> set[str]:
    """The accounts of ``leaving`` just left the agent's own set of the tool.
    Every live session built on one (``session_state.sessions_built_on``,
    whatever the default is now) has its running turn stopped and is closed,
    in offboarding's bounded batch, and its chat is told once its own close
    is done; a session on a machine that is away is ended there too. Every
    other chat of ``leaving`` is told now. Returns the chats told."""
    from core.session import session_state
    from services.agents import offboarding_sessions as closer
    from storage.pg import run_db
    if not leaving.accounts:
        return set()
    closing = await _sessions_to_close(
        session_state.sessions_built_on(agent_name, mcp_name, set(leaving.accounts)), agent_name)
    closed = {chat["id"] for _, chat in closing.values() if chat}
    others = [cid for cid in dict.fromkeys(leaving.chose + leaving.on_default) if cid not in closed]
    lines = await run_db(functools.partial(
        _lines, mcp_name, agent_name, sorted(closed) + others,
        removed=closed | set(leaving.chose), default_went=bool(leaving.on_default)))

    async def after(v) -> None:
        await run_db(_forget_on_away_machine, v.session_id)
        await session_state.security_index_written()   # on disk before anyone is told
        chat = closing[v.session_id][1]
        line = lines.pop(chat["id"], None) if chat else None
        if line is not None:
            await _tell({chat["id"]: line})
            logger.info("chat reload: chat=%s: its %s account left the agent", chat["id"][:8], mcp_name)

    await closer.close_sessions([v for v, _ in closing.values()],
                                f"{mcp_name} account left {agent_name}",
                                reason="account-removed", revoked=False, after=after)
    await _tell({cid: lines[cid] for cid in others if cid in lines})
    return closed | set(others)


def _forget_on_away_machine(session_id: str) -> None:
    """A session no process holds whose context names a machine that is away
    (the platform restarted meanwhile): without its context the machine's
    report at its return is refused and the process there ends
    (``run_recovery._readopt_state``). Synchronous."""
    from core.remote.satellite_connection import get_connection_manager
    from core.session.session_state import get_session_security
    from services.scheduler import run_recovery
    context = get_session_security(session_id) if session_id else None
    machine = getattr(getattr(context, "placement", None), "machine_id", "") or ""
    if machine and not get_connection_manager().is_connected(machine):
        run_recovery._drop_local_state(session_id)


async def _wait_and_reload(chat_id: str, asker: chat_move.Asker, reason: str,
                           choices: dict[str, str], mark: tuple[str, int],
                           blocked: Callable[[str], str] | None, wait_s: float = WAIT_S, *,
                           limit: _Limit | None = None) -> None:
    """Wait for the chat's turn to end, then reload it. The claim alone is
    not the end: a terminal turn, a waiting card and a person's warm-up hold
    no claim, so the claim is given back while one is open
    (``chat_move.open_turn``) and asked for again.

    ``limit``: a turn a usage limit cut, continued under the same guards. In
    place the session waits for the move under the claim; else it is closed
    and its account let go (a Codex terminal never over unsent text). Then a
    waiting message is the turn, with no continuation."""
    from auth import roles
    from auth.providers import effective_role_of
    from core.events import input_queue
    from core.session import chat_rebuild
    from storage import database as task_store, remote_store
    from storage.pg import run_db
    from ws import wire_events as wire
    deadline = time.monotonic() + wait_s
    q = await input_queue.loaded(chat_id)
    hold: Callable[[], bool] | None = None
    while time.monotonic() < deadline:
        claim = await input_queue.claim_when_free(
            chat_id, timeout=max(0.0, deadline - time.monotonic()))
        if claim is None:
            # No claim: the wait ran out, or the chat is being deleted (the
            # queue answers at once then), and a deleted chat gets no note.
            if input_queue.deleting(chat_id) or not await run_db(task_store.get_chat, chat_id):
                return
            break
        held = True
        try:
            chat = await run_db(task_store.get_chat, chat_id)
            # While new work is held it waits too, its row kept: a close now
            # would leave a session the hold starts nothing on. The resume, or
            # the boot's settle of the row, takes it up.
            if drain.holding() or (chat and await run_db(chat_move.open_turn, chat)):
                if limit is not None and limit.holds_queue:
                    input_queue.hold_first_turn(chat_id)   # held while a turn is open
                q.release(claim, rearm=True)
                held = False
                await asyncio.sleep(chat_move.TURN_POLL_S)
                continue
            if limit is not None:
                # The claim holds the chat from here; a person's waiting
                # message is judged under it, never started past it.
                input_queue.first_turn_started(chat_id)
            if not chat:
                return
            # Who may continue the chat is judged on the person's standing
            # now: the turn that asked may have ended long after the call.
            standing = await run_db(effective_role_of, asker.sub, chat.get("agent") or "")
            if standing == roles.NO_ACCESS:
                await _not_done(chat_id, limit, f"The session was not reloaded: {NOT_DRIVER}")
                return
            asker = chat_move.Asker(sub=asker.sub, role=standing,
                                    platform_admin=roles.is_admin(standing))
            why = (VOICE_IN_USE if blocked is not None and blocked(chat_id)
                   else await run_db(_refusal, chat, asker))
            if why:
                await _not_done(chat_id, limit, f"The session was not reloaded: {why}")
                return
            if limit is None and not choices and _mark(chat) != mark and _mark(chat)[1]:
                # A turn since the call started the session again (a message
                # that waited, another person's warm-up): it was built from
                # the stored state, which is all a reload does.
                await _note(chat_id, reloaded_sentence(reason, []))
                return
            stopped = _bound(chat_id, await run_db(
                remote_store.reloads_since_person if limit is None else remote_store.resumes_since_person,
                chat_id), limit=limit is not None)
            if stopped:
                await _note(chat_id, stopped, wire.MOVED_RESUME_STOPPED)
                return
            away = await run_db(_away_machine, chat)
            if away:
                from services.remote import machine_away
                await _not_done(chat_id, limit, machine_away.sentence_for_chat(
                    chat, f"The session was not reloaded: {away} is offline. Ask again when it is back.",
                    label=away), away=away)
                return
            terminal = await run_db(_is_terminal_chat, chat)
            if limit is not None and limit.in_place:
                # The rebalance moves the sign-in under the live session. The
                # claim stays through the wait: a message that waits starts
                # only on the moved session, and nothing follows it.
                from services.engines import pool_limits
                if not await pool_limits.moved_off(limit.session_id, limit.old_sub, LIMIT_MOVE_WAIT_S):
                    await _note(chat_id, LIMIT_NOT_MOVED, wire.MOVED_RESUME_SKIPPED)
                    return
                if await run_db(chat_move._waiting_message_goes_first, chat):
                    return
                q.release(claim, rearm=True)
                held = False
                _count(chat_id)
                _limit_started.add(asyncio.current_task())   # from here a Stop is the resume's to read
                await _resume(chat, asker, reason, limit=limit, terminal=terminal)
                return
            if limit is not None and terminal and hold is None:
                from services.engines import pool_moves
                hold = pool_moves.terminal_hold(chat_id, LIMIT_HELD)
            if limit is not None:
                _limit_started.add(asyncio.current_task())   # the close starts the chat again
            try:
                held = False   # the hold gives the claim back, whatever happens in it
                async with chat_rebuild.closed(chat_id, reason="account" if limit else "reload",
                                               by=asker.sub, claim=claim,
                                               **({"hold": hold} if hold is not None else {})) as rebuilt:
                    switched: list[str] = []
                    if limit is None:
                        switched = await run_db(_write_choices, rebuilt.chat, asker.sub, choices)
                        await _note(chat_id, reloaded_sentence(reason, switched))
                    else:
                        from services.engines import pool_limits
                        await run_db(functools.partial(pool_limits.leave_account, chat_id, limit.session_id,
                                                       limit.old_sub, fresh=limit.fresh))
                    chat = rebuilt.chat
            except chat_rebuild.ChatBusy:
                _limit_started.discard(asyncio.current_task())
                await asyncio.sleep(chat_move.TURN_POLL_S)
                continue
            except LookupError:
                return
            if limit is not None and await run_db(chat_move._waiting_message_goes_first, chat):
                # The person's message starts the closed chat on the pool's
                # pick: it is the turn, and no continuation follows it.
                logger.info("chat reload: chat=%s: a person's message goes first after the limit",
                            chat_id[:8])
                return
            _count(chat_id)
            logger.info("chat reload: chat=%s %s (%s)", chat_id[:8],
                        "continues after a limit" if limit else "reloaded", reason)
            if terminal:
                _watch_terminal(chat_id)
            await _resume(chat, asker, reason, switched, limit=limit)
            return
        except Exception:
            logger.exception("chat reload: chat=%s: the reload failed", chat_id[:8])
            await _not_done(chat_id, limit, "The session was not reloaded. Send a message to continue.")
            return
        finally:
            if held:
                q.release(claim, rearm=True)
    await _not_done(chat_id, limit, "The session was not reloaded: the turn did not end within "
                    "30 minutes.")


async def _not_done(chat_id: str, limit: _Limit | None, line: str, *, away: str = "") -> None:
    """The chat's line when its reload, or its continuation after a limit,
    does not happen."""
    from ws import wire_events as wire
    if limit is None:
        await _note(chat_id, line, wire.MOVED_FAILED)
    elif away:
        await _note(chat_id, f"The chat did not continue on another account: {away} is offline. "
                    "Send a message to continue.", wire.MOVED_FAILED)
    else:
        await _note(chat_id, LIMIT_FAILED, wire.MOVED_FAILED)


def _is_terminal_chat(chat: dict) -> bool:
    from core import execution_mode
    return execution_mode.is_interactive(chat_override=chat.get("execution_mode") or None)


def _watch_terminal(chat_id: str) -> None:
    """Tell the chat's viewers when its new terminal is up, so an open page
    attaches to it (live only, no card)."""
    from core import background

    async def _watch() -> None:
        from core.session import interactive_session
        from core.session.session_state import broadcast_chat_frame
        from ws import wire_events as wire
        deadline = time.monotonic() + TERMINAL_WATCH_S
        while time.monotonic() < deadline:
            isess = interactive_session.find_live_for_chat(chat_id)
            if isess is not None and isess.alive:
                broadcast_chat_frame(chat_id, {
                    "type": wire.SYSTEM, "subtype": wire.SUBTYPE_SESSION_RELOADED,
                    "terminal": True, "chat_id": chat_id})
                return
            await asyncio.sleep(0.5)

    background.spawn(_watch(), name="chat-reload-terminal", owner="chat_reload")


async def _resume(chat: dict, asker: chat_move.Asker, reason: str,
                  switched: list[str] | None = None, *, limit: _Limit | None = None,
                  terminal: bool = False) -> None:
    """The chat continues by itself after its reload: one turn of the
    platform's words, as the person the session runs as and in the chat's
    own mode, on a session built as their own warm-up builds it. A person's
    message that waits goes first instead, a turn that holds the chat is
    left alone, and a turn the person stopped stays stopped.

    ``limit``: the continuation after a limit, in its own words and source
    (``LIMIT_SOURCE``); a person's message since the limit wins too, and the
    turn is marked. ``terminal``: queued on the chat's live terminal."""
    from core.events import chat_writer
    from core.session import chat_rebuild, session_kind
    from core.session.session_delivery import DeliveryOutcome
    from storage import database as task_store, remote_store
    from storage.pg import run_db
    from ws import wire_events as wire
    cid = chat["id"]
    fresh = await run_db(task_store.get_chat, cid)
    if not fresh:
        return
    if fresh.get("last_turn_aborted"):
        logger.info("chat reload: chat=%s does not continue by itself: its person stopped "
                    "the turn", cid[:8])
        return
    if (limit is not None and limit.since is not None
            and await run_db(remote_store.newest_person_row, cid) > limit.since):
        logger.info("chat reload: chat=%s does not continue by itself: a person wrote since the "
                    "limit", cid[:8])
        return
    mode = await run_db(chat_rebuild.chat_mode, fresh)
    # Read last, with no await before the turn: a person's message or turn
    # that took the chat meanwhile goes first.
    if await run_db(chat_move._waiting_message_goes_first, fresh) or _turn_holds(cid, terminal):
        logger.info("chat reload: chat=%s does not continue by itself: a person's message "
                    "or turn has it", cid[:8])
        return
    if limit is not None and cid in _limit_stopped:   # a person's Stop since the chat started again
        _limit_stopped.discard(cid)
        await _note(cid, LIMIT_STOPPED, wire.MOVED_RESUME_SKIPPED)
        return
    if limit is None:
        text, source = resume_nudge(reason, switched), "reload"
    else:
        text, source = (FRESH_NUDGE if limit.fresh else LIMIT_NUDGE), LIMIT_SOURCE
    wake = json.dumps({"prompt": text, "source": wire.WAKE_SOURCE_RELOAD})
    written: list[asyncio.Future] = []

    def _persist(target_chat_id: str) -> None:
        if target_chat_id:
            written.append(chat_writer.submit(
                target_chat_id,
                functools.partial(task_store.add_chat_message, target_chat_id, "event", "",
                                  event_type=wire.PERSISTED_SCHEDULE_WAKE, event_data=wake),
                label="reload_resume"))

    async def _outcome(outcome: DeliveryOutcome) -> None:
        if limit is not None and outcome.path == "pty":
            _after_terminal_queue(cid, outcome.session_id)
        if outcome.path != "none":
            if limit is not None and limit.unattended:   # it went out with nobody there
                from services.scheduler import continue_pass
                await continue_pass.count_limit_continuation(cid, limit.session_id, asker.sub)
            return
        if limit is not None:
            _limit_marks.pop(cid, None)
            _fresh_marks.pop(cid, None)
        for fut in written:
            try:
                row = await fut
                await chat_writer.submit(cid, functools.partial(
                    task_store.delete_chat_message_ranges, cid, [(row - 1, row)]),
                    label="reload_resume_undone")
            except Exception:  # noqa: BLE001  # the row stays: a stale line, not a lost turn
                logger.debug("chat reload: the resume row was not taken back", exc_info=True)
        stopped = cid in _limit_stopped
        _limit_stopped.discard(cid)
        if chat_move._person_took_chat(cid) and not stopped:
            return
        await _note(cid, LIMIT_STOPPED if stopped else LIMIT_FAILED if limit is not None
                    else "The chat did not continue on its own. Send a message to continue.",
                    wire.MOVED_RESUME_SKIPPED)

    if limit is not None:
        # Before the turn: a limit in it shows the card (``limit_marked``),
        # and a refusal of the conversation in it starts nothing fresh.
        _limit_marks[cid] = time.monotonic()
        if limit.fresh:
            _fresh_marks[cid] = time.monotonic()
    logger.info("chat reload: chat=%s continues by itself (%s)", cid[:8], mode)
    try:
        await chat_rebuild.rebuild_and_resume(
            cid, text, source=source, session_id=fresh.get("session_id") or "",
            agent=fresh.get("agent") or "", person=asker.sub, role=asker.role,
            mode=mode, kind=session_kind.DASHBOARD.name, build=chat_rebuild.AS_CHAT,
            persist_event=_persist, allow_ws=False, allow_pty=terminal,
            fresh_without_session=True, on_outcome=_outcome,
        )
    except Exception:  # noqa: BLE001  # a rung that raises (the spawn's guard, a broken start) is undone like an undelivered nudge
        logger.warning("chat reload: chat=%s: the resume raised", cid[:8], exc_info=True)
        await _outcome(DeliveryOutcome("none", chat_id=cid))


def _turn_holds(chat_id: str, terminal: bool) -> bool:
    """A turn holds the chat now: the live terminal's own turn or an Enter
    whose turn is not seen yet (``terminal``), else a pump, a claim, a
    warm-up or a live terminal. Synchronous."""
    if terminal:
        from core.session import interactive_session
        isess = interactive_session.find_live_for_chat(chat_id)
        # An Enter empties the box before its turn is seen: it holds too.
        return isess is not None and bool(isess.turn_open or isess.submit_settling)
    return chat_move._turn_holds_chat(chat_id)


# ---------------------------------------------------------------------------
# The continuation after a limit
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Limit:
    """A continuation after a usage limit through the reload's wait: the cut
    session, the account it rests, a sign-in taken in place, a fresh start,
    the newest person row when the limit came (``since``), whether the chat
    was told, and whether its queue is held for the waiter (not stored)."""
    session_id: str
    old_sub: str
    in_place: bool
    fresh: bool = False
    since: int | None = None
    told: bool = False
    holds_queue: bool = False
    unattended: bool = False    # nobody watched the cut turn: counted when it goes out

    def target(self) -> str:
        """Its ``chat_pending_rebuilds`` row's ``target``: ids and flags only."""
        return json.dumps({"session_id": self.session_id, "old_sub": self.old_sub,
                           "in_place": self.in_place, "fresh": self.fresh, "since": self.since,
                           "unattended": self.unattended})

    @classmethod
    def from_target(cls, target: str) -> _Limit | None:
        try:
            raw = json.loads(target or "{}")
            return cls(session_id=str(raw["session_id"]), old_sub=str(raw["old_sub"]),
                       in_place=bool(raw["in_place"]), fresh=bool(raw.get("fresh")),
                       since=raw.get("since"), told=True, unattended=bool(raw.get("unattended")))
        except (KeyError, TypeError, ValueError):
            return None


def limit_marked(chat_id: str, *, take: bool = True) -> bool:
    """A continuation went out and no turn ended clean since: a limit now is
    its own, which shows the card. ``take``: the pump's read takes the mark; a
    terminal's line (a file the session could write) only reads it."""
    at = _limit_marks.pop(chat_id, None) if take else _limit_marks.get(chat_id)
    return at is not None and time.monotonic() - at < LIMIT_MARK_S


def fresh_marked(chat_id: str) -> bool:
    """The chat already starts fresh from its history after a refusal of its
    conversation: a refusal in that turn starts nothing again."""
    at = _fresh_marks.get(chat_id)
    return at is not None and time.monotonic() - at < LIMIT_MARK_S


def limit_turn_ended(chat_id: str) -> None:
    """A turn of the chat ended clean, or its person sent a message into its
    terminal: the continuation (if any) is done, and the next limit may
    continue again."""
    _limit_marks.pop(chat_id, None)
    _fresh_marks.pop(chat_id, None)


if limit_turn_ended not in person_sent_hooks:
    person_sent_hooks.append(limit_turn_ended)


def may_continue_after_limit(chat_id: str, person: str) -> bool:
    """The reload's own rule for a continuation (``_refusal``): a person's own
    dashboard chat they may drive, never a call's, never delegated work, never
    a task's. Synchronous."""
    from auth import roles
    from auth.providers import effective_role_of
    from core.session import session_kind
    from storage import database as task_store
    if not person or session_kind.is_task_chat_id(chat_id):
        return False
    chat = task_store.get_chat(chat_id)
    if not chat:
        return False
    role = effective_role_of(person, chat.get("agent") or "")
    if role == roles.NO_ACCESS:
        return False
    return not _refusal(chat, chat_move.Asker(sub=person, role=role, platform_admin=roles.is_admin(role)))


def schedule_for_limit(chat_id: str, *, person: str, session_id: str, old_sub: str,
                       in_place: bool, fresh: bool = False, told: bool = False,
                       since: int | None = None, wait_s: float = WAIT_S,
                       holds_queue: bool = False, unattended: bool = False) -> bool:
    """One continuation after a limit, through the reload's own wait and a
    ``LIMIT`` row. ``in_place``: the session takes the new sign-in under it;
    else it is closed and starts on the pool's pick. ``fresh``: it starts
    from the chat's history. ``since``: the newest person row at the limit.
    ``holds_queue``: the chat's queue is held for the waiter until its claim.
    Nothing when a reload or a continuation already waits; an account close
    waiting for the chat gives way. On the loop. Whether it was scheduled."""
    if waiting(chat_id):
        logger.info("chat reload: chat=%s: a %s already waits", chat_id[:8],
                    "continuation" if continuation_waits(chat_id) else "reload")
        return False
    if _idle_close_reasons.get(chat_id) == "account":
        old = _idle_closes.pop(chat_id, None)
        _idle_close_reasons.pop(chat_id, None)
        if old is not None:
            old.cancel()
    if holds_queue:
        from core.events import input_queue
        input_queue.hold_first_turn(chat_id)
    _arm(chat_id, chat_move.Asker(sub=person, role="", platform_admin=False), "", {}, ("", 0),
         _voice_holds, wait_s,
         limit=_Limit(session_id, old_sub, in_place, fresh, since=since, told=told,
                      holds_queue=holds_queue, unattended=unattended))
    return True


def continuation_waits(chat_id: str) -> bool:
    """A continuation after a limit waits for the chat (not the agent's
    reload): a limit now in the chat keeps its card."""
    task = _waiters.get(chat_id)
    return task is not None and not task.done() and task in _limit_waiters


def _voice_holds(chat_id: str) -> str:
    """A spoken conversation holds the chat (the reload's ``blocked``): the
    socket's own reader, registered by the socket layer (``voice_holds``)."""
    return next((why for check in voice_holds if (why := check(chat_id))), "")


async def _limit_begins(chat_id: str, limit: _Limit) -> _Limit:
    """The waiter's start: the chat's line (the move writes its own for an
    engine that takes the new account in place), and the newest message of a
    person, which a newer one beats."""
    from storage import remote_store
    from storage.pg import run_db
    if not limit.told:
        if limit.fresh:
            await _account_note(chat_id, LIMIT_FRESH_LINE)
        elif not limit.in_place:
            await _account_note(chat_id, LIMIT_LINE)
    since = limit.since
    if since is None:
        since = await run_db(remote_store.newest_person_row, chat_id)
    return dataclasses.replace(limit, since=since, told=True)


def _after_terminal_queue(chat_id: str, session_id: str) -> None:
    """The continuation waits in the live terminal's queue: the terminal's own
    turn end after it clears the chat's mark, and a draft that holds it for
    ``LIMIT_HOLD_S`` tells the chat once."""
    from core import background
    from core.session import interactive_session
    isess = interactive_session.get(session_id)
    if isess is not None:
        isess.add_turn_end_waiter(lambda *_a: limit_turn_ended(chat_id))
    background.spawn(_tell_if_held(chat_id, session_id), name="chat-limit-held", owner="chat_reload")


async def _tell_if_held(chat_id: str, session_id: str) -> None:
    """A continuation queued on a live terminal waits while text sits in its
    input box (the queue's gate for ``LIMIT_SOURCE``). After ``LIMIT_HOLD_S``
    still held, the chat is told once."""
    from core.session import interactive_session
    await asyncio.sleep(LIMIT_HOLD_S)
    isess = interactive_session.get(session_id)
    if isess is None or not isess.holds_unsent_text:
        return
    if any(item.get("source") == LIMIT_SOURCE for item in list(isess._prompt_queue)):
        await note_account_change(chat_id, LIMIT_HELD, waits=True)
