"""The engine's own permission requests: the prompts a Claude CLI raises over
its stdio channel (``control_request`` ``can_use_tool``), which a headless
session opens with ``--permission-prompt-tool stdio``. Claude Code still asks
for some calls under ``--dangerously-skip-permissions`` (a heredoc that
writes a file, an ``rm -rf`` of a wide glob), after the platform's own hook
answered.

Such a request is a prompt waiting on a person like a hook-raised one: it is
registered with the session's permission waiters (so ``has_pending_prompt``,
``resolve_permission``, the drive gate and a close's release all see it),
answered through the session's layer in the shape the CLI parses, withdrawn
when the CLI withdraws it, shown again to a viewer who opens the chat later,
and bounded by the surface it runs on: a person's chat waits like a hook
card, a session nobody drives waits ``UNATTENDED_WAIT_S`` and is refused (the
CLI refuses that one call and the turn goes on), a phone call's is refused at
once. A remote session's answer is written by its satellite, so it waits only
on a satellite that writes the CLI's shape (``ANSWER_SATELLITE_VERSION``);
on an older one the request is refused at once.

A request reaches here from the pump (the turn's own stream, ``watch``) or
from a between-turns reader (``note_frame``: the idle drains, the stale
drains, a run's background wait).

One request is never a card: the one Claude Code raises in its auto mode
once its reviewer declined several actions in a row (``decision_reason_type``
``classifier``). Every reader reaches ``note_frame`` first (the turn's
readers before the translator), which refuses it at once with a message the
model reads; the CLI then re-enters auto before the next turn, at most
``REVIEW_REENTRIES`` times per session, and after that Auto answers with the
platform's cards (``session_state.pause_review``).
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from dataclasses import dataclass, field

from core.background import spawn

logger = logging.getLogger("otodock")

#: How long a session nobody drives (a task run, a delegated worker, a meeting
#: participant) waits for an answer before the request is refused.
UNATTENDED_WAIT_S = 600.0
#: The first satellite version that writes an answer in the CLI's shape; an
#: older one's remote request is refused at once.
ANSWER_SATELLITE_VERSION = "0.5.140"

#: The ``decision_reason_type`` of a request Claude Code raises after its
#: reviewer's decline limit.
REVIEW_DECLINE_REASON = "classifier"
#: How many times per session the CLI re-enters auto after that limit.
REVIEW_REENTRIES = 2
_REVIEW_LIMIT_MESSAGE = (
    "The reviewer declined several actions in a row. Do not retry this one now: "
    "tell the person what you wanted to do and why, and wait for their reply."
)
# request id -> the message its deny carries (``answer_frame``).
_deny_messages: dict[str, str] = {}

# Why a request was refused without a person's answer (the chat line).
_NOBODY = "nobody answered in time"
_PHONE = "a phone call cannot answer it"
_SATELLITE = "this machine's satellite cannot pass an answer back yet"


@dataclass
class EnginePrompt:
    request_id: str
    session_id: str
    chat_id: str
    tool_name: str
    tool_input: dict
    frame: dict = field(default_factory=dict)
    task: asyncio.Task | None = None


# request id -> the pending prompt
_pending: dict[str, EnginePrompt] = {}
# Request ids the engine withdrew while their wait ran.
_withdrawn: set[str] = set()
# Request ids withdrawn before anything registered them (the request and its
# cancel read in one burst): the late registration skips them. Pruned past
# ``_EARLY_WITHDRAWAL_S``.
_withdrawn_early: dict[str, float] = {}
_EARLY_WITHDRAWAL_S = 60.0


def withdrawn_before_registered(request_id: str) -> bool:
    """The engine withdrew ``request_id`` before it was registered: its
    card is not shown and nothing waits on it."""
    return request_id in _withdrawn_early


def answer_frame(request_id: str, approved: bool, tool_input: dict | None = None) -> dict:
    """The ``control_response`` the CLI parses for a ``can_use_tool``
    request: the decision under ``response.response``, an allow carrying the
    call's input unchanged when it is known (an empty one would replace it),
    a deny its message."""
    if approved:
        decision: dict = {"behavior": "allow"}
        if tool_input is not None:
            decision["updatedInput"] = dict(tool_input)
    else:
        decision = {"behavior": "deny", "message": _deny_messages.pop(
            request_id, "The request was not approved.")}
    return {"type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": decision}}


def owns(request_id: str) -> bool:
    """Whether ``request_id`` is an engine-raised request still pending."""
    return request_id in _pending


def input_of(request_id: str) -> dict:
    p = _pending.get(request_id)
    return dict(p.tool_input) if p else {}


def pending_for_chat(chat_id: str) -> list[EnginePrompt]:
    return [p for p in _pending.values() if chat_id and p.chat_id == chat_id]


def card_frame(p: EnginePrompt) -> dict:
    """The ``permission_prompt`` frame a viewer renders as the card."""
    from ws import wire_events as wire
    return {"type": wire.PERMISSION_PROMPT, "chat_id": p.chat_id, **p.frame}


def _version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in (v or "").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _satellite_answers(session_id: str) -> bool:
    """A local session always; a remote one only on a satellite that writes
    the CLI's shape."""
    from core.remote import remote_file_flow
    machine = remote_file_flow.remote_machine_id(session_id)
    if not machine:
        return True
    if not ANSWER_SATELLITE_VERSION:
        return False
    from core.remote.satellite_connection import get_connection_manager
    have = get_connection_manager().satellite_version(machine)
    return _version_tuple(have) >= _version_tuple(ANSWER_SATELLITE_VERSION)


def _bound(session_id: str) -> tuple[float, str]:
    """How long the request may wait for a person, and why it is refused
    when the wait is nil."""
    from core.session import session_kind
    from core.session.session_state import PROMPT_WAIT_S, get_session_client_type
    client_type = get_session_client_type(session_id)
    if session_kind.of(client_type) is session_kind.PHONE:
        return 0.0, _PHONE
    if not _satellite_answers(session_id):
        return 0.0, _SATELLITE
    if session_kind.attended(client_type):
        return float(PROMPT_WAIT_S), _NOBODY
    return UNATTENDED_WAIT_S, _NOBODY


def watch(session_id: str, data: dict, *, chat_id: str = "") -> None:
    """A request the turn's pump shows as its card: registered now (the
    card's answer can only come after this), then waited on and answered."""
    rid = str(data.get("request_id") or "")
    if not rid or not session_id or rid in _pending or rid in _withdrawn_early:
        return
    from core.session.session_state import open_permission_wait, watch_prompt
    open_permission_wait(rid, session_id)
    # Before any await: a release that lands first still reads as no answer.
    watch_prompt(rid)
    p = EnginePrompt(
        request_id=rid, session_id=session_id, chat_id=chat_id,
        tool_name=str(data.get("tool_name") or ""),
        tool_input=dict(data.get("tool_input") or {}),
        frame={k: data.get(k) for k in ("request_id", "tool_name", "tool_input",
                                        "tool_use_id", "description") if k in data},
    )
    _pending[rid] = p
    p.task = asyncio.get_running_loop().create_task(_wait_and_answer(p))


def note_frame(session_id: str, data: dict, *, in_turn: bool = False) -> None:
    """A raw stream frame read off a session's stdout. A withdrawal retires
    its request wherever it is read; a request read between turns (the
    reader is a drain, not the turn's pump) is surfaced here."""
    if not isinstance(data, dict):
        return
    t = data.get("type")
    if t == "control_cancel_request":
        withdraw(str(data.get("request_id") or ""))
        return
    if t != "control_request":
        return
    request = data.get("request") or {}
    if request.get("subtype") != "can_use_tool":
        return
    if request.get("decision_reason_type") == REVIEW_DECLINE_REASON:
        _refuse_review_fallback(session_id, str(data.get("request_id") or ""),
                                request.get("input") or {})
        return
    if in_turn:
        return
    rid = str(data.get("request_id") or "")
    if not rid or rid in _pending or rid in _withdrawn_early:
        return
    surfaced = {
        "request_id": rid,
        "tool_name": request.get("tool_name", ""),
        "tool_input": request.get("input", {}),
        "tool_use_id": request.get("tool_use_id", ""),
        "description": request.get("description", ""),
        "session_id": session_id,
    }
    spawn(_surface(session_id, surfaced), name="surface", owner="engine_prompts")


def _refuse_review_fallback(session_id: str, rid: str, tool_input: dict) -> None:
    """Claude Code's ask after its reviewer's decline limit: refused at once
    with a message the model reads, never shown (the pump's card for it is
    skipped: ``withdrawn_before_registered``), then auto re-entered."""
    if not rid or not session_id:
        return
    _withdrawn_early[rid] = time.monotonic()
    _deny_messages[rid] = _REVIEW_LIMIT_MESSAGE
    spawn(_refuse_and_reenter(session_id, rid, dict(tool_input)),
          name="review_limit", owner="engine_prompts")


async def _refuse_and_reenter(session_id: str, rid: str, tool_input: dict) -> None:
    from core.permission_modes import AUTO_REVIEW_MODE
    from core.session.session_manager import find_layer_for_session
    from core.session.session_state import (
        get_session_mode,
        note_review_reentry,
        pause_review,
    )
    layer = find_layer_for_session(session_id)
    if layer is None:
        _deny_messages.pop(rid, None)
        return
    try:
        await layer.answer_engine_prompt(session_id, rid, False, tool_input)
        logger.info("engine prompt %s on session %s refused: the reviewer's decline limit",
                    rid, session_id[:8])
        if get_session_mode(session_id) != AUTO_REVIEW_MODE:
            return
        done = note_review_reentry(session_id)
        if done >= REVIEW_REENTRIES:
            pause_review(session_id)
            logger.info("engine review paused on session %s after %d re-entries",
                        session_id[:8], done)
            return
        # The engine re-enters its own review (a local session after the
        # turn's end); a mode the person changed meanwhile stands.
        await layer.reenter_review(session_id)
    except Exception:
        logger.exception("engine prompt %s on session %s: the decline-limit answer failed",
                         rid, session_id[:8])


async def _surface(session_id: str, data: dict) -> None:
    """A request read between turns: through the chat's live pump when one
    runs on this session (its card, its slot), else as a card of its own (the
    row, the frame to the chat's viewers) and waited on here."""
    from core.events import chat_writer
    from core.events.common_events import PERMISSION_REQUEST, CommonEvent
    from core.events.stream_pump import _active_pumps
    from core.session.session_state import broadcast_chat_frame
    from storage import database as task_store
    from storage.pg import run_db
    from ws import wire_events as wire
    try:
        chat = await run_db(task_store.get_chat_by_session, session_id)
        if data["request_id"] in _withdrawn_early:
            return   # withdrawn while the chat was read
        chat_id = (chat or {}).get("id") or ""
        pump = _active_pumps.get(chat_id) if chat_id else None
        if pump is not None and not pump.is_done and pump.session_id == session_id:
            pump.event_queue.put_nowait(CommonEvent(type=PERMISSION_REQUEST, data=data))
            return
        watch(session_id, data, chat_id=chat_id)
        p = _pending.get(data["request_id"])
        if p is None or not chat_id:
            return
        block = {"type": wire.PERMISSION_PROMPT, **p.frame}
        chat_writer.submit(chat_id, functools.partial(
            task_store.add_chat_message, chat_id, "event", "",
            event_type=wire.PERMISSION_PROMPT, event_data=json.dumps(block)),
            label="engine_prompt")
        broadcast_chat_frame(chat_id, card_frame(p))
    except Exception:
        logger.exception("engine prompt %s on session %s was not surfaced",
                         data.get("request_id"), session_id[:8])


def withdraw(request_id: str) -> None:
    """The engine withdrew its request (an interrupt, its own cancel): the
    wait ends with no answer written and the card is retired."""
    p = _pending.get(request_id)
    if p is None:
        if request_id:
            now = time.monotonic()
            for rid, at in list(_withdrawn_early.items()):
                if now - at > _EARLY_WITHDRAWAL_S:
                    del _withdrawn_early[rid]
            _withdrawn_early[request_id] = now
        return
    _withdrawn.add(request_id)
    from core.session.session_state import resolve_permission
    resolve_permission(request_id, False)


def _retire(p: EnginePrompt) -> None:
    """The card leaves the pump's slot (and its buffer) and every viewer."""
    from core.session.session_state import broadcast_chat_frame
    from ws import wire_events as wire
    queue = _pump_queue(p)
    if queue is not None:
        # The pump frees its slot and tells its viewers.
        queue.put_nowait({"event_type": wire.ITEM_PROMPT_RETIRED, "request_id": p.request_id})
    elif p.chat_id:
        broadcast_chat_frame(p.chat_id, {"type": wire.PROMPT_RETIRED, "chat_id": p.chat_id,
                                         "request_id": p.request_id})


def _pump_queue(p: EnginePrompt):
    from core.events.stream_pump import _active_pumps
    pump = _active_pumps.get(p.chat_id) if p.chat_id else None
    if pump is None or pump.is_done:
        return None
    return getattr(pump, "perm_queue", None)


async def _wait_and_answer(p: EnginePrompt) -> None:
    from core.session.session_state import took_no_answer, wait_for_permission
    bound, why = _bound(p.session_id)
    started = time.monotonic()
    answered = False
    approved = False
    try:
        # A nil bound refuses at once: its zero wait closes the registration
        # ``watch`` opened.
        approved = await wait_for_permission(p.request_id, p.session_id, timeout=bound)
        answered = not took_no_answer(p.request_id) and bound > 0
        if p.request_id in _withdrawn:
            _retire(p)
            return
        timed_out = not answered and (bound <= 0 or time.monotonic() - started >= bound - 1)
        if not answered and not timed_out:
            # A close, an abort or a stop-and-send released it: the engine's
            # own interrupt or exit ends the request; nothing to write.
            _retire(p)
            return
        await _answer(p, approved if answered else False)
        if not answered:
            _retire(p)
            _note_refusal(p, why)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("engine prompt %s on session %s was not answered",
                         p.request_id, p.session_id[:8])
    finally:
        _pending.pop(p.request_id, None)
        _withdrawn.discard(p.request_id)


async def _answer(p: EnginePrompt, approved: bool) -> None:
    from core.session.session_manager import find_layer_for_session
    layer = find_layer_for_session(p.session_id)
    if layer is None:
        logger.info("engine prompt %s: session %s is gone, nothing to answer",
                    p.request_id, p.session_id[:8])
        return
    await layer.answer_engine_prompt(p.session_id, p.request_id, approved, p.tool_input)
    logger.info("engine prompt %s on session %s answered: %s", p.request_id,
                p.session_id[:8], "allow" if approved else "deny")


def _note_refusal(p: EnginePrompt, why: str) -> None:
    """The chat line of a request refused without an answer."""
    if not p.chat_id:
        return
    from core.events import chat_writer
    from core.session.session_state import broadcast_chat_frame
    from storage import database as task_store
    from ws import wire_events as wire
    what = _call_label(p)
    block = {"type": wire.SYSTEM, "subtype": wire.SUBTYPE_ENGINE_PROMPT_REFUSED,
             "message": f"The engine asked to run {what}; {why}, so it was refused "
                        f"and the agent went on.",
             "request_id": p.request_id}
    chat_writer.submit(p.chat_id, functools.partial(
        task_store.add_chat_message, p.chat_id, "event", "",
        event_type=wire.SYSTEM, event_data=json.dumps(block)), label="engine_prompt_refused")
    broadcast_chat_frame(p.chat_id, {**block, "chat_id": p.chat_id})
    logger.info("engine prompt %s on session %s refused: %s", p.request_id,
                p.session_id[:8], why)


def _call_label(p: EnginePrompt) -> str:
    """``the command `…``` for a shell call (its first line, clipped), else the
    tool's name."""
    from core.events import tool_roles
    cmd = p.tool_input.get("command") if isinstance(p.tool_input, dict) else None
    if tool_roles.role_of(p.tool_name) == tool_roles.SHELL and isinstance(cmd, str) and cmd.strip():
        line = cmd.strip().splitlines()[0]
        if len(line) > 80:
            line = line[:77] + "..."
        return f"the command `{line}`"
    return f"`{p.tool_name or 'a tool'}`"
