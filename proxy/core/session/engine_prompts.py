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

The PreToolUse hook's own cards share the between-turns half (``offer_hook_card``,
``end_hook_card``): the ask the hook raises in Ask (and for a critical-tier tool
or a call no engine reviewer judges), a plan review, a question, a restored
mode. Each goes onto its session's permission queue while a live pump reads
that queue (the pump shows it in its turn), and is shown here otherwise, as a
background sub-agent's call between turns needs: the frame to the chat's
viewers, the "needs your input" notice, the card found again by a viewer who
opens the chat while it waits (``pending_for_chat``), and its row written
when it ends, with its outcome. The hook answers its own call through its
HTTP response, never through the layer. The notice keeps no memory of an
earlier card: unlike the pump's (``_retried``, ``_RETRY_QUIET_S``), a hook
call retried after its connection dropped between turns notifies again.

A request for a call the platform's gate already allowed is not a card
either where the gate is the only authority. The gate records each such call
(``note_gate_allow``: the call's id, its tool, the digest of the input it
judged and the record's kind, used once, ``GATE_ALLOW_S``); ``note_frame``
answers the CLI's ask for that same call at once. Two kinds:

* ``PLAN_RECORD``: Claude Code 2.1.299+ asks for a call its plan mode does not
  read as read-only even after the PreToolUse hook allowed it, so a headless
  Plan spawn passes the prompt tool and the asks the plan floor raises are
  answered from the record (``_PLAN_FLOOR_REASONS``, and ExitPlanMode's own
  question after the person's approval, whose answer also moves the CLI to
  the mode the person chose: ``setMode``).
* ``HOOK_RECORD``: under bypass (Ask, Don't Ask, the unattended word) the CLI
  still asks for an ``rm`` its safety check cannot clear
  (``_HOOK_CHECK_REASONS``). Under Don't Ask and the unattended word the hook
  is the only authority, and in Ask the person already allowed the call
  through the hook's card, so the gate records those allows and the ask is
  answered from the record. Auto keeps its cards (the CLI's reviewer asks).

A rule, another hook, a reason outside the kind's set or a changed input
stays a card.

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
import hashlib
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
    # A hook card's frame type (``card_frame``; the permission prompt when
    # empty) and, for a plan review, the action the person chose.
    frame_type: str = ""
    action: str = ""


# request id -> the pending prompt
_pending: dict[str, EnginePrompt] = {}
# Request ids the engine withdrew while their wait ran.
_withdrawn: set[str] = set()
# The hook's own cards shown between turns (``offer_hook_card``): request id
# -> the card, until its wait ends (``end_hook_card``).
_hook_cards: dict[str, EnginePrompt] = {}
# Request ids withdrawn before anything registered them (the request and its
# cancel read in one burst): the late registration skips them. Pruned past
# ``_EARLY_WITHDRAWAL_S``.
_withdrawn_early: dict[str, float] = {}
_EARLY_WITHDRAWAL_S = 60.0

#: How long the gate's record of a call it allowed waits for the CLI's ask
#: (which follows the hook's answer at once).
GATE_ALLOW_S = 120.0
#: The record's kinds: a call the Plan stages allowed, and a call the hook
#: alone decided (Don't Ask, the unattended word, a person's Allow in Ask).
PLAN_RECORD = "plan"
HOOK_RECORD = "hook"
# session id -> {tool_use_id: (tool name, input digest, expiry, kind)}
_gate_allowed: dict[str, dict[str, tuple[str, str, float, str]]] = {}
#: The ``decision_reason_type`` values Claude Code's plan floor gives the ask
#: for a call the hook allowed (2.1.312: ``mode`` for a write or a tool its
#: plan mode refuses, ``other`` for a command it does not read as read-only,
#: ``workingDir`` for a read outside the working folders).
_PLAN_FLOOR_REASONS = frozenset({"mode", "other", "workingDir"})
#: The ``decision_reason_type`` values of the asks Claude Code raises under
#: bypass for a call the hook allowed (2.1.296 and 2.1.312: ``safetyCheck``
#: for an ``rm`` of the working folder or of a target it cannot resolve,
#: ``subcommandResults`` for that check inside a compound command; both with
#: ``classifier_approvable`` false).
_HOOK_CHECK_REASONS = frozenset({"safetyCheck", "subcommandResults"})
# request id -> the CLI's own mode word an allow moves it to (``answer_frame``).
_set_modes: dict[str, str] = {}


def _mark_withdrawn_early(request_id: str) -> None:
    now = time.monotonic()
    for rid, at in list(_withdrawn_early.items()):
        if now - at > _EARLY_WITHDRAWAL_S:
            del _withdrawn_early[rid]
    _withdrawn_early[request_id] = now


def input_digest(tool_input) -> str:
    """The digest of a call's input as the gate judged it (canonical JSON)."""
    text = json.dumps(tool_input if isinstance(tool_input, dict) else {},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def note_gate_allow(session_id: str, tool_use_id: str, tool_name: str, digest: str,
                    *, kind: str = PLAN_RECORD) -> None:
    """The gate allowed ``tool_use_id`` (``kind``: in Plan, or as the only
    authority): the CLI's ask for that call is answered at once, once, within
    ``GATE_ALLOW_S``. Expired records of every session go on each write."""
    if not session_id or not tool_use_id:
        return
    now = time.monotonic()
    for sid in list(_gate_allowed):
        live = {k: v for k, v in _gate_allowed[sid].items() if v[2] > now}
        if live:
            _gate_allowed[sid] = live
        else:
            del _gate_allowed[sid]
    _gate_allowed.setdefault(session_id, {})[tool_use_id] = (
        tool_name, digest, now + GATE_ALLOW_S, kind)


def _take_gate_allow(session_id: str, request: dict) -> bool:
    """Whether ``request`` asks for a call the gate allowed, for a reason its
    record's kind answers (the record is used up when it matches)."""
    tool_use_id = str(request.get("tool_use_id") or "")
    calls = _gate_allowed.get(session_id) or {}
    entry = calls.get(tool_use_id)
    if entry is None:
        return False
    tool_name, digest, expiry, kind = entry
    if expiry <= time.monotonic() or request.get("tool_name") != tool_name:
        return False
    if request.get("decision_reason_code"):
        return False
    reason = request.get("decision_reason_type")
    if kind == HOOK_RECORD:
        # Never an ask the CLI's reviewer could take (Auto keeps its cards).
        if (request.get("classifier_approvable") is not False
                or reason not in _HOOK_CHECK_REASONS
                or input_digest(request.get("input")) != digest):
            return False
    else:
        if "classifier_approvable" in request:
            return False
        # The plan-exit tool's own question after the hook allowed it carries
        # no reason and an empty input (the plan is in the plan file).
        from core.events import tool_roles
        if tool_roles.role_of(tool_name) == tool_roles.PLAN_EXIT and reason is None:
            if not request.get("requires_user_interaction"):
                return False
        elif reason not in _PLAN_FLOOR_REASONS or input_digest(request.get("input")) != digest:
            return False
    del calls[tool_use_id]
    if not calls:
        _gate_allowed.pop(session_id, None)
    return True


def withdrawn_before_registered(request_id: str) -> bool:
    """The engine withdrew ``request_id`` before it was registered: its
    card is not shown and nothing waits on it."""
    return request_id in _withdrawn_early


def answer_frame(request_id: str, approved: bool, tool_input: dict | None = None) -> dict:
    """The ``control_response`` the CLI parses for a ``can_use_tool``
    request: the decision under ``response.response``, an allow carrying the
    call's input unchanged when it is known (an empty one would replace it),
    a deny its message. An allow of ExitPlanMode's own question moves the
    CLI to the mode the person chose (``_set_modes``)."""
    mode = _set_modes.pop(request_id, "")
    if approved:
        decision: dict = {"behavior": "allow"}
        if tool_input is not None:
            decision["updatedInput"] = dict(tool_input)
        if mode:
            decision["updatedPermissions"] = [
                {"type": "setMode", "mode": mode, "destination": "session"}]
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
    """The cards still waiting on the chat with no pump to show them: the
    engine's own requests and the hook's cards shown between turns."""
    return [p for p in (*_pending.values(), *_hook_cards.values())
            if chat_id and p.chat_id == chat_id]


def card_frame(p: EnginePrompt) -> dict:
    """The frame a viewer renders as the card (``permission_prompt``, or a
    hook card's ``plan_review``)."""
    from ws import wire_events as wire
    return {"type": p.frame_type or wire.PERMISSION_PROMPT, "chat_id": p.chat_id, **p.frame}


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
    if _answer_gate_allow(session_id, str(data.get("request_id") or ""), request):
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
    _mark_withdrawn_early(rid)
    _deny_messages[rid] = _REVIEW_LIMIT_MESSAGE
    spawn(_refuse_and_reenter(session_id, rid, dict(tool_input)),
          name="review_limit", owner="engine_prompts")


def _answer_gate_allow(session_id: str, rid: str, request: dict) -> bool:
    """The CLI's ask for a call the gate allowed: allowed at once with the
    request's own input, never shown (the pump's card for it is skipped:
    ``withdrawn_before_registered``). A remote session's answer waits on a
    satellite that writes the CLI's shape."""
    if not rid or not session_id or rid in _pending:
        return False
    if not _take_gate_allow(session_id, request):
        return False
    if not _satellite_answers(session_id):
        return False
    _mark_withdrawn_early(rid)
    _note_implement_mode(session_id, rid, str(request.get("tool_name") or ""))
    spawn(_answer_allowed(session_id, rid, dict(request.get("input") or {}),
                          str(request.get("tool_name") or "")),
          name="plan_allow", owner="engine_prompts")
    return True


def _note_implement_mode(session_id: str, rid: str, tool_name: str) -> None:
    """ExitPlanMode's own question after the person chose Implement in Auto
    or in Ask: the answer moves the CLI to that mode (Claude Code otherwise
    goes back to the mode it entered Plan from, ``default`` for a chat that
    started in Plan, so its own reviewer would not run in Auto). A
    satellite drops the field (it passes the decision's own keys only)."""
    from core.events import tool_roles
    if tool_roles.role_of(tool_name) != tool_roles.PLAN_EXIT:
        return
    from core.permission_modes import ASK_MODE, AUTO_REVIEW_MODE, cli_mode
    from core.session.session_state import get_session_mode
    mode = get_session_mode(session_id)
    if mode in (AUTO_REVIEW_MODE, ASK_MODE):
        _set_modes[rid] = cli_mode(mode)


async def _answer_allowed(session_id: str, rid: str, tool_input: dict, tool_name: str) -> None:
    from core.session.session_manager import find_layer_for_session
    layer = find_layer_for_session(session_id)
    if layer is None:
        return
    try:
        await layer.answer_engine_prompt(session_id, rid, True, tool_input)
        logger.info("engine prompt %s on session %s allowed: the gate allowed %s",
                    rid, session_id[:8], tool_name)
    except Exception:
        logger.exception("engine prompt %s on session %s: the answer from the record failed",
                         rid, session_id[:8])


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
            _mark_withdrawn_early(request_id)
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


# ---------------------------------------------------------------------------
# The hook's own cards between turns
# ---------------------------------------------------------------------------

def queue_has_reader(queue) -> bool:
    """Whether a live pump reads ``queue``, a session's permission queue: a
    registered pump that holds this queue and has not ended (a wake
    capture's pump holds none)."""
    from core.events.stream_pump import _active_pumps
    return any(getattr(p, "perm_queue", None) is queue and not p.is_done
               for p in list(_active_pumps.values()))


def offer_hook_card(queue, item: dict, *, session_id: str, chat_id: str) -> bool:
    """A card the PreToolUse hook raises (an ask, a plan review, a question,
    a restored mode): onto ``queue`` while a live pump reads it, the pump
    showing it in its turn; shown here at once otherwise. The reader test
    and the put are one synchronous step, so no pump ends between them.
    True when shown here."""
    if queue_has_reader(queue) or not chat_id:
        queue.put_nowait(item)
        return False
    _show_hook_card(item, session_id=session_id, chat_id=chat_id)
    return True


def hand_off_hook_cards(queue, *, session_id: str, chat_id: str) -> None:
    """A pump just ended (``is_done``): a hook card put on its queue after its
    final drain, while it still looked live, is shown here, an ask or a plan
    review only while something waits on it; every other item stays queued,
    in order, for the next pump."""
    if queue is None or not chat_id or queue_has_reader(queue):
        return
    from core.session.session_state import prompt_waits
    from ws import wire_events as wire
    kept = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        kind = item.get("event_type")
        waited = kind in (wire.ITEM_PERMISSION_PROMPT, wire.ITEM_PLAN_REVIEW)
        if kind in (wire.ITEM_QUESTION, wire.ITEM_MODE_RESTORED) or (
                waited and prompt_waits(str(item.get("request_id") or ""))):
            _show_hook_card(item, session_id=session_id, chat_id=chat_id)
        else:
            # Nobody waits on it now (a hook released before the pump
            # ended, a plan the turn ends with): the next pump's, as before.
            kept.append(item)
    for item in kept:
        queue.put_nowait(item)


def note_card_action(request_id: str, action: str) -> None:
    """The plan review action the person chose, for the row a hook card
    shown here writes when it ends."""
    p = _hook_cards.get(request_id)
    if p is not None:
        p.action = action


def end_hook_card(queue, request_id: str, *, answered: bool, approved: bool,
                  caller_gone: bool, chat_id: str = "") -> None:
    """The hook's wait on a card ended. A card shown here writes its row now,
    with its outcome (none for a caller that went away: its retried call
    raises its own card), and leaves its viewers when nobody answered it. An
    unanswered card a pump showed is retired through that pump while one
    reads the queue (it frees its slot), else on the chat's viewers."""
    from core.session.session_state import broadcast_chat_frame
    from ws import wire_events as wire
    p = _hook_cards.pop(request_id, None)
    if p is not None:
        if answered or not caller_gone:
            _hook_card_row(p, approved=answered and approved)
        if not answered:
            broadcast_chat_frame(p.chat_id, {"type": wire.PROMPT_RETIRED,
                                             "chat_id": p.chat_id, "request_id": request_id})
        return
    if answered:
        return
    item: dict[str, object] = {"event_type": wire.ITEM_PROMPT_RETIRED, "request_id": request_id}
    if caller_gone:
        item["caller_gone"] = True
    if queue_has_reader(queue) or not chat_id:
        queue.put_nowait(item)
        return
    # The pump that showed it has ended: the resume's copy of its card goes.
    from core.events.stream_pump import _pending_permissions
    for sid, held in list(_pending_permissions.items()):
        if held.get("request_id") == request_id:
            del _pending_permissions[sid]
    broadcast_chat_frame(chat_id, {"type": wire.PROMPT_RETIRED, "chat_id": chat_id,
                                   "request_id": request_id})


def _show_hook_card(item: dict, *, session_id: str, chat_id: str) -> None:
    """One hook card on the chat with no pump: the frame to its viewers and
    the notice; a question's row now (nothing waits on it), an ask's or a
    plan review's when it ends."""
    from core.events import chat_writer
    from core.session.session_state import broadcast_chat_frame
    from storage import database as task_store
    from ws import wire_events as wire
    kind = item.get("event_type")
    rid = str(item.get("request_id") or "")
    meeting = bool(item.get("meeting_agent"))
    if kind == wire.ITEM_MODE_RESTORED:
        mode = str(item.get("mode") or "")
        chat_writer.submit(chat_id, functools.partial(task_store.update_chat, chat_id,
                                                      permission_mode=mode),
                           label="mode_restored")
        broadcast_chat_frame(chat_id, {"type": wire.MODE_CHANGED, "mode": mode, "chat_id": chat_id})
        return
    tool_input = dict(item.get("tool_input") or {})
    if kind == wire.ITEM_QUESTION:
        block = {"type": wire.QUESTION, "tool_name": item.get("tool_name") or "",
                 "tool_input": tool_input}
        chat_writer.submit(chat_id, functools.partial(
            task_store.add_chat_message, chat_id, "event", "",
            event_type=wire.QUESTION, event_data=json.dumps(block)), label="hook_question")
        broadcast_chat_frame(chat_id, {**block, "chat_id": chat_id})
        _ping_hook_card(session_id, chat_id, "", "Waiting for your answer", meeting=meeting)
        return
    p = EnginePrompt(request_id=rid, session_id=session_id, chat_id=chat_id,
                     tool_name=str(item.get("tool_name") or ""), tool_input=tool_input)
    if kind == wire.ITEM_PLAN_REVIEW:
        filename = _plan_filename(tool_input, rid)
        plan = str(item.get("plan") or "")
        if plan:
            chat_writer.submit(chat_id, functools.partial(
                task_store.add_chat_plan, chat_id, filename, plan), label="chat_plan")
        p.frame_type = wire.PLAN_REVIEW
        p.frame = {"request_id": rid, "plan": plan, "tool_input": tool_input, "filename": filename}
        body = "Review the plan"
    else:
        p.frame = {k: item[k] for k in ("request_id", "tool_name", "tool_input", "description",
                                        "meeting_agent") if k in item}
        # The tool's input stays out of the notice (it rides the push to the
        # phone, and a command or a URL can carry a secret).
        body = f"Allow {p.tool_name or 'a tool'}?"
    _hook_cards[rid] = p
    broadcast_chat_frame(chat_id, card_frame(p))
    _ping_hook_card(session_id, chat_id, rid, body, meeting=meeting)
    logger.info("hook card %s on session %s shown between turns (chat=%s)",
                rid[:8], session_id[:8], chat_id[:8])


def _plan_filename(tool_input: dict, request_id: str) -> str:
    """The plan file's name as the pump names it: the plan path's last part,
    else one made from the request."""
    for key in ("planFilePath", "plan_file_path", "file_path"):
        val = tool_input.get(key) or ""
        if isinstance(val, str) and val:
            return val.rsplit("/", 1)[-1]
    return f"plan-{request_id[:8]}.md"


def _hook_card_row(p: EnginePrompt, *, approved: bool) -> None:
    """The ended card's row, as the chat's history draws it."""
    from core.events import chat_writer
    from storage import database as task_store
    from ws import wire_events as wire
    if p.frame_type == wire.PLAN_REVIEW:
        block = {"type": wire.PLAN_REVIEW, **p.frame}
        if p.action:
            block["action"] = p.action
    else:
        block = {"type": wire.PERMISSION_PROMPT, **p.frame, "resolved": True,
                 "approved": approved}
    chat_writer.submit(p.chat_id, functools.partial(
        task_store.add_chat_message, p.chat_id, "event", "",
        event_type=block["type"], event_data=json.dumps(block)), label="hook_card")


def _ping_hook_card(session_id: str, chat_id: str, request_id: str, body: str,
                    *, meeting: bool) -> None:
    """The "needs your input" notice of a card shown between turns: never
    for a session nobody attends or a meeting participant's card."""
    from core.session import session_kind
    from core.session.session_state import get_session_client_type
    if meeting or not session_kind.attended(get_session_client_type(session_id)):
        return
    spawn(_fire_hook_ping(chat_id, request_id, body), name="hook_card_ping",
          owner="engine_prompts")


async def _fire_hook_ping(chat_id: str, request_id: str, body: str) -> None:
    from core.session.session_state import prompt_waits
    from services.notifications import notification_manager
    from storage import database as task_store
    from storage.pg import run_db

    def _read() -> tuple[dict | None, bool, str]:
        row = task_store.get_chat(chat_id)
        if not row:
            return None, False, ""
        return (row, bool(task_store.get_active_meeting_for_chat(chat_id)),
                notification_manager.agent_label(row["agent"]) if row.get("agent") else "")

    try:
        chat, in_meeting, label = await run_db(_read)
        # Answered or released while the row was read: nothing waits.
        if not chat or in_meeting or (request_id and not prompt_waits(request_id)):
            return
        await notification_manager.fire_ephemeral(
            chat["user_sub"], title=f"{label or chat.get('agent') or ''} needs your input",
            body=body, chat_id=chat_id)
    except Exception:
        logger.exception("hook card %s: the notice was not sent", request_id[:8])
