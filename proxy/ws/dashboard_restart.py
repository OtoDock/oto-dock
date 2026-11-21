"""A chat's session closed and started again from the dashboard's socket.

Two starts carry no turn and answer the socket with frames: the switch
between a terminal and a headless chat (``_handle_switch_execution_mode``)
and a mode pick on a live terminal (``_handle_mode_change``). Both end in
``history_and_rewarm``: the chat's history from its rows, then the warm-up
that starts the session again in what the row holds, resuming the
conversation.

A mode pick on a live terminal starts it again in the picked mode, the
three clicks a person made before (the terminal off, the pick, the terminal
on) in one. It runs under the chat's hold (``chat_rebuild.closed``), so a
start, a move or a reload of the chat already under way refuses it, and an
open turn, a message just sent, a waiting card or question, background work
the terminal started, a reconnecting machine, a local terminal holding the
session or text typed in it and not sent refuse it before anything is closed
or written: read before the hold, and again with its close. So does a pick
while the chat's terminal is still starting. A refusal answers with the
chat's stored mode and one sentence (``mode_changed``'s ``refusal``).
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

from core.events import chat_writer
from core.execution_layer import ASK_MODE
from core.session import interactive_session, session_kind, warmup_registry
from core.session.background_frames import running_fields
from core.session.session_state import recorded_session_mode
from storage import database as task_store
from storage.pg import run_db
from ws import wire_events as wire

if TYPE_CHECKING:
    from ws.dashboard_host import DashboardHost

logger = logging.getLogger("otodock")

# Why a pick on a live terminal is refused, the person's words. The page
# disables the mode button during a turn; these answer what still reaches
# the server.
WORKING = "The agent is working. Press Esc to stop it, or pick the mode again when it is done."
CARD = "The agent is waiting for your answer. Answer it, then pick again."
ASKING = "The agent is asking you something in the terminal. Answer it, or press Esc, then pick again."
BACKGROUND = "The agent's background work is still running. Pick the mode again when it ends."
BUSY = "The chat is busy. Pick the mode again in a moment."
RECONNECTING = "The terminal is reconnecting. Pick the mode again when it is back."
LOCAL = "The chat is open in the otodock command on a computer. Leave it there, then pick again."
UNSENT = "You have text typed in the terminal. Send it or clear it, then pick the mode again."
DRIVEN = "Someone else drives this terminal. Take it over to change its mode."
STARTING = "The terminal is starting. Pick the mode again when it is up."

_SPARE_NOTES = {
    "turn-open": WORKING,
    "prompt-pending": CARD,
    "question-parked": ASKING,
    "background-pending": BACKGROUND,
}


def busy_note(isess) -> str:
    """Why the live terminal cannot start again now, or "". The reaper's own
    reasons (an open turn, a card or a question waiting on the person,
    background work it started), a message just sent whose turn is not open
    yet, a machine in its reconnect grace, a local terminal holding the
    session, and text typed in the terminal and not sent (a restart would
    drop it). No await: read just before the hold."""
    from core.remote.satellite_connection import get_connection_manager
    from core.session.interactive_registry import reap_spare_reason
    note = _SPARE_NOTES.get(reap_spare_reason(isess, 0.0), "")
    if note:
        return note
    if isess.submit_settling:
        return WORKING
    if isess.is_remote and get_connection_manager().is_pty_in_grace(isess.target):
        return RECONNECTING
    if isess.otodock_attached:
        return LOCAL
    if isess.holds_unsent_text:
        return UNSENT
    return ""


async def pick_on_live_terminal(host: DashboardHost, cid: str, new_mode: str, theme: str) -> bool:
    """A mode pick on a chat whose terminal is live or starting: refused, a
    no-op or the terminal started again in the pick. False when the chat has
    no terminal live or starting (the caller's own branches answer). Called
    after the drive gate and the mode's validation."""
    isess = interactive_session.find_live_for_chat(cid)
    if isess is None or not isess.alive:
        return await _refused_while_starting(host, cid)
    # A task run's terminal keeps the run's mode, and a terminal built for
    # nobody or driven by someone else takes no pick (``may_drive``).
    if session_kind.is_task_chat_id(cid):
        await host._echo_pick(cid, "mode")
        return True
    if not await host._may_drive_terminal(isess):
        await host._echo_pick(cid, "mode", refusal=DRIVEN)
        return True
    if host.streaming:
        # Another chat's headless turn runs on this socket: the warm-up below
        # must not run with its pump in hand.
        await host._echo_pick(cid, "mode", refusal=BUSY)
        return True
    chat = await run_db(task_store.get_chat, cid) or {}
    # Done already only when the terminal runs the mode too: its own record
    # ("" before it has one) can differ from the row, and the button shows
    # the terminal's.
    live_mode = recorded_session_mode(isess.session_id)
    if new_mode == (chat.get("permission_mode") or ASK_MODE) and live_mode in ("", new_mode):
        await host._send({"type": wire.MODE_CHANGED, "mode": new_mode, "chat_id": cid})
        return True
    note = busy_note(isess)
    if note:
        await host._echo_pick(cid, "mode", chat, refusal=note)
        return True
    await _restart_in_mode(host, cid, new_mode, theme)
    return True


async def _refused_while_starting(host: DashboardHost, cid: str) -> bool:
    """A chat with no live terminal whose terminal is being started (a
    warm-up, a move, a reload, a wake holds the chat): the start has read
    the row already, so a pick written now would leave the terminal in the
    old mode while the row says the new one. Refused, nothing written. Any
    other chat with no live terminal: False, the caller writes the row and
    the next start reads it."""
    if warmup_registry.get(cid) is None:
        return False
    chat = await run_db(task_store.get_chat, cid) or {}
    if not chat or not await run_db(_starts_a_terminal, chat):
        return False
    await host._echo_pick(cid, "mode", chat, refusal=STARTING)
    return True


def _starts_a_terminal(chat: dict) -> bool:
    """Whether the chat's next start is a terminal: its own choice, else its
    agent's default, as a warm-up resolves it. Synchronous: call it on the
    DB executor."""
    from core import execution_mode
    from storage.agents import agent_store
    agent = agent_store.get_agent(chat.get("agent") or "") or {}
    return execution_mode.is_interactive(
        agent_default=agent.get("default_execution_mode") or "",
        chat_override=chat.get("execution_mode") or None)


async def _restart_in_mode(host: DashboardHost, cid: str, new_mode: str, theme: str) -> None:
    """Close the chat's terminal under the chat's hold, write the pick, and
    start the terminal again in it."""
    from core.session import chat_rebuild
    from ws.dashboard_chat_support import take_owed_engine_mode
    # ``busy_note`` read again with the close: text typed, a reconnect grace
    # or a local terminal that came during the hold's awaits refuse the pick
    # with its own sentence (the idle check re-reads only the turn's).
    held = {"note": ""}

    def _busy() -> bool:
        isess = interactive_session.find_live_for_chat(cid)
        held["note"] = busy_note(isess) if isess is not None else ""
        return bool(held["note"])
    try:
        async with chat_rebuild.closed(cid, reason="mode pick", by=host.user_sub or "",
                                       hold=_busy) as hold:
            # A close that failed leaves the terminal in its old mode: the
            # pick is not written.
            if interactive_session.find_live_for_chat(cid) is not None:
                raise chat_rebuild.ChatBusy(cid)
            await chat_writer.submit(
                cid, functools.partial(task_store.update_chat, cid, permission_mode=new_mode),
                label="mode_change")
            # The row holds the pick: nothing owed to the closed life may
            # come back at a later start.
            take_owed_engine_mode(hold.chat.get("session_id") or "")
            if host.deferred_for == cid:
                host.deferred_mode = ""
                if not host.deferred_model:
                    host.deferred_for = ""
            chat = {**hold.chat, "permission_mode": new_mode}
    except chat_rebuild.ChatBusy:
        await host._echo_pick(cid, "mode", refusal=held["note"] or BUSY)
        return
    except LookupError:
        return
    logger.info("WS dashboard: chat=%s terminal starts again in mode=%s (a pick)", cid[:8], new_mode)
    # The chat's other viewers saw the old terminal end: they attach to the
    # new one once it is up, as after the agent's own reload.
    from services.remote import chat_reload
    chat_reload._watch_terminal(cid)
    if cid == host.chat_id:
        host.session_id = None
    host._detach_pty_viewer()
    await history_and_rewarm(host, cid, chat, execution_mode=chat.get("execution_mode") or "",
                             theme=theme)


async def history_and_rewarm(host: DashboardHost, cid: str, chat: dict, *,
                             execution_mode: str, theme: str) -> None:
    """The chat's history page into the client (a terminal's close tailed
    its transcript into the rows), then the warm-up that starts the chat
    again, resuming the conversation, in what the row holds."""
    from core.session.session_manager import resolve_execution_path
    from ws.dashboard import _CHAT_PAGE, _build_chat_restore

    def _history_job():
        if session_kind.is_task_chat_id(cid):
            return task_store.get_chat_messages(cid), False, _build_chat_restore(cid)
        return (*task_store.get_chat_messages_page(cid, _CHAT_PAGE),
                _build_chat_restore(cid))

    # Lane job: behind the closed session's last rows.
    msgs, msgs_has_more, restore = await chat_writer.submit(cid, _history_job, label="switch_history")
    agent = chat.get("agent", "")
    # The page rebuilds from this page of rows: the delegations still
    # running (the closed session ends none of them) keep their badges.
    running = await running_fields((cid,))
    await host._send({
        "type": wire.CHAT_HISTORY, "chat_id": cid, "agent": agent,
        "messages": msgs, "plans": [],
        "has_more": msgs_has_more,
        "restore": restore,
        "total_cost": chat.get("total_cost") or 0,
        "context_used": chat.get("context_used") or 0,
        "context_max": chat.get("context_max") or 0,
        "execution_path": resolve_execution_path(agent, chat.get("execution_path", "")),
        "execution_mode": execution_mode,
        "model": chat.get("model", ""),
        "mode": chat.get("permission_mode", ASK_MODE),
        **running,
    })
    host._history_floors = None  # a page with no siblings: no base for a delta
    # The old session is closed, so the warm-up takes the resume path and
    # starts the session in the chat's execution mode, resuming its
    # transcript. background=False: warmup_ready (the page's swap) is sent.
    await host._handle_warmup({
        "agent": agent, "chat_id": cid,
        "permission_mode": chat.get("permission_mode", ASK_MODE),
        "model": chat.get("model", ""),
        "execution_path": chat.get("execution_path", ""),
        "execution_mode": execution_mode,
        "theme": theme,
    }, background=False)
