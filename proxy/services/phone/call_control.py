"""Call controls: the call tools relayed to the phone daemon.

An agent on a call ends it, transfers the caller or asks the agent that
placed the call through a TOOL (the call-control MCP on the CLI engines, a
Direct builtin) instead of a bracket marker. The tool reaches this registry
with the session's id; the registry sends a ``call_control`` frame on the
call's phone socket and answers with the daemon's ``call_control_result``.
The text markers stay accepted daemon-side for models that never call tools.

One binding per session, keyed by the session id a token names (the phone
socket binds at warmup, unbinds at teardown; a reuse warmup re-points an id
to its new socket, so the unbind is a compare-and-delete on the sender). The
frame carries the turn whose producer holds the session lock, so the daemon
can refuse an action that belongs to a turn the caller already interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger("otodock")

ACTION_END = "end"
ACTION_TRANSFER = "transfer"
ACTION_ASK = "ask"
ACTIONS: tuple[str, ...] = (ACTION_END, ACTION_TRANSFER, ACTION_ASK)

# The daemon answers an action at once (it records the action and acts at
# the turn's completion point), so a silent line is a dead one.
REQUEST_TIMEOUT_S = 10.0
NO_ANSWER_MESSAGE = "The line did not answer; the call goes on. Use the text marker if this repeats."

# The bundled MCP that carries the tools on the CLI engines (the Direct
# layer registers builtins of the same names instead), and the MCP whose
# presence makes a chat a caller (its companion on an in-session call).
CALL_CONTROL_MCP = "call-control-mcp"
PHONE_MCP = "phone-mcp"
# The env keys a phone session's config carries: the call tools apply (the
# daemon advertised ``call_control``; the Direct layer registers its builtins
# on this alone, so an older daemon keeps the markers) and the ask tool
# applies (an outbound call another agent placed).
CALL_TOOLS_ENV = "OTO_CALL_TOOLS"
ASK_BACK_ENV = "OTO_CALL_ASK_BACK"

Sender = Callable[[dict], Awaitable[None]]


@dataclass
class CallBinding:
    session_id: str
    sender: Sender
    ask_back: bool = False           # a daemon-driven outbound call: someone waits
    chat_id: str = ""                # an in-session call: the chat it runs on
    turn: int | None = None          # the turn whose producer holds the lock
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


_bindings: dict[str, CallBinding] = {}


def bind(session_id: str, sender: Sender, *, ask_back: bool = False,
         chat_id: str = "") -> CallBinding:
    """Bind a live call's socket to its session. A rebind (a reused
    pre-warmed session on a new socket) fails the old socket's waiters."""
    old = _bindings.get(session_id)
    if old is not None and old.sender is not sender:
        _fail_pending(old, "the call moved to another connection")
    binding = CallBinding(session_id=session_id, sender=sender, ask_back=ask_back, chat_id=chat_id)
    _bindings[session_id] = binding
    return binding


def unbind(session_id: str, sender: Sender | None = None) -> None:
    """Drop the binding; with ``sender`` only when it is still that socket's."""
    binding = _bindings.get(session_id)
    if binding is None or (sender is not None and binding.sender is not sender):
        return
    _bindings.pop(session_id, None)
    _fail_pending(binding, "the call ended")


def binding_for(session_id: str) -> CallBinding | None:
    return _bindings.get(session_id) if session_id else None


def set_turn(session_id: str, turn: int | None) -> None:
    binding = _bindings.get(session_id)
    if binding is not None:
        binding.turn = turn


async def request(session_id: str, action: str, args: dict, *,
                  timeout: float = REQUEST_TIMEOUT_S) -> dict:
    """Relay one action to the daemon and return its ``{ok, message}``.

    ``ok: False`` with a message for every failure: no binding, a refused
    action, a dead line (the timeout). The caller renders the message as the
    tool's result text.
    """
    from ws import wire_events as wire
    binding = _bindings.get(session_id)
    if binding is None:
        return {"ok": False, "message": (
            "No live call is bound to this session. On a phone call, end it with "
            "the [CALL_COMPLETE] text marker instead.")}
    if action not in ACTIONS:
        return {"ok": False, "message": f"Unknown call action {action!r}."}
    if action == ACTION_ASK and not binding.ask_back:
        return {"ok": False, "message": (
            "No agent is waiting on this call: it was not placed by another agent. "
            "Ask the person on the line instead.")}
    request_id = uuid.uuid4().hex
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    binding.pending[request_id] = fut
    frame = {
        "type": wire.PHONE_CALL_CONTROL,
        "turn": binding.turn,
        "data": {"request_id": request_id, "action": action, **args},
    }
    try:
        await binding.sender(frame)
        data = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("call control: %s on session %s got no answer", action, session_id[:8])
        return {"ok": False, "message": NO_ANSWER_MESSAGE}
    except Exception as e:  # noqa: BLE001  # the tool is told the call did not answer whatever the send raised
        logger.warning("call control: %s on session %s failed: %s", action, session_id[:8], e)
        return {"ok": False, "message": NO_ANSWER_MESSAGE}
    finally:
        binding.pending.pop(request_id, None)
    message = str(data.get("message") or "")
    return {"ok": bool(data.get("ok")), "message": message}


def resolve(session_id: str, request_id: str, data: dict) -> bool:
    """The daemon's answer to one request; False when nobody waits for it."""
    binding = _bindings.get(session_id)
    if binding is None:
        return False
    fut = binding.pending.get(request_id or "")
    if fut is None or fut.done():
        return False
    fut.set_result(dict(data or {}))
    return True


def _fail_pending(binding: CallBinding, reason: str) -> None:
    for fut in binding.pending.values():
        if not fut.done():
            fut.set_result({"ok": False, "message": f"The line did not answer: {reason}."})
    binding.pending.clear()
