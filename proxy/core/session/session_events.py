"""Named session events — the one place every engine reports to.

Two CLIs (Claude Code, Codex) and one in-process loop (Direct LLM) each have
their own way of saying "a tool is about to run", "a tool ran", "a subagent
finished" and "the turn ended". The platform cares about a fixed set of
events and wants ONE answer to each wherever the session runs
(``docs/architecture/HOOKS.md``):

- ``pre_tool``        may this tool run? → ``decide_tool_permission`` (the
                      permission authority stays where it is; this is the
                      named door to it)
- ``post_tool``       what ran, did it fail, which paths did it touch — the
                      turn's record, kept per session in memory
- ``subagent_stop``   which subagent finished → the ``SubagentRegistry``
- ``turn_end``        the turn is over: may it end, or does the platform have
                      something to say first? A ``TurnEndVerdict`` — stop, or
                      continue with a reason the agent reads as a new
                      message. Nothing registers a handler in this module
                      itself; the checks of lane 3 do.
- ``session_start`` / ``session_end`` / ``prompt_submit``   declared, unwired

Who calls what (one source per placement, so nothing is counted twice):

- the hook receivers (``api/hooks/lifecycle.py``) for the CLI hook scripts;
- the execution layers' ``send_message`` (``core/execution_layer.py``
  wraps each layer's turn in ``drive_turns``) for ``turn_end`` on every
  proxy-driven session, headless Claude, Codex app-server and Direct alike;
- the ``/v1/hooks/stop`` receiver for ``turn_end`` on the two terminals,
  where the CLI drives the turn and the ``Stop`` hook is the only point that
  can hold it open;
- the Claude forwarder (headless), the Codex translator (app-server), the
  two transcript tailers (the terminals) and the direct loop for
  ``post_tool``.

A verdict is computed for exactly one source per placement: ``hook`` on a
terminal session, ``loop`` everywhere else; every other call is an
observation (``STOP``). The reason of a continue verdict is capped, and
rounds are counted here because Codex has no cap of its own (Claude
overrides after eight consecutive blocks — a backstop, not a policy).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
from core.events import tool_roles
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

logger = logging.getLogger("session-events")

# A continue reason larger than this is truncated: it becomes a message in
# the agent's context and a hook's stdout.
MAX_REASON_BYTES = 8192
# Automatic re-prompting is bounded, like everything unattended on the
# platform: after this many continue verdicts in a row the turn ends.
MAX_ROUNDS = 3
# Tool records kept per session (a ring; the oldest fall off).
MAX_TOOL_RECORDS = 500
# A shell command kept on a record (the checks classify a commit, a push, a
# build from it); the rest is cut.
MAX_COMMAND_BYTES = 2048
# The turn's result text a proxy-driven loop hands turn_end (the tail is
# kept: the answer ends with what matters); the Stop hook caps its own.
MAX_LAST_MESSAGE_BYTES = 8192
# A terminal's Stop hook is reachable from the agent's own shell with the
# same token (HOOKS.md "Residuals"): a second hook evaluation of one session
# within this interval is an observation, so a curl cannot burn rounds.
HOOK_MIN_INTERVAL_S = 1.0


# ---------------------------------------------------------------------------
# Verdicts and records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TurnEndVerdict:
    """What the platform says when a turn ends. ``continue_reason`` empty =
    let it end; non-empty = hold the turn open and hand the agent this text
    (a ``Stop`` hook block on a terminal, a follow-up turn elsewhere)."""
    continue_reason: str = ""

    @property
    def should_continue(self) -> bool:
        return bool(self.continue_reason)


STOP = TurnEndVerdict()


def continue_with(reason: str) -> TurnEndVerdict:
    """A continue verdict with the reason capped to ``MAX_REASON_BYTES``."""
    text = (reason or "").strip()
    if not text:
        return STOP
    raw = text.encode("utf-8")
    if len(raw) > MAX_REASON_BYTES:
        text = raw[:MAX_REASON_BYTES].decode("utf-8", errors="ignore") + "\n… (truncated)"
    return TurnEndVerdict(continue_reason=text)


@dataclass(frozen=True)
class TurnEndContext:
    session_id: str
    #: ``hook`` (a Stop hook), ``loop`` (a proxy-driven turn loop),
    #: ``transcript`` (a terminal's transcript fold — observation only).
    source: str
    #: ``claude`` | ``codex`` | ``direct`` | ``""`` when unknown.
    engine: str = ""
    #: ``cli`` (a terminal; the CLI drives the turn) | ``proxy`` (the proxy
    #: drives the turn and can start another).
    driven_by: str = "proxy"
    transcript_path: str = ""
    last_message: str = ""
    #: The hook's ``stop_hook_active`` (a continue verdict was already
    #: delivered this turn) or the loop's round count.
    rounds: int = 0


@dataclass(frozen=True)
class ToolRecord:
    tool_name: str
    tool_use_id: str = ""
    #: Paths named by the call's input (``file_path`` / ``path`` /
    #: ``notebook_path``), as the tool saw them.
    paths: tuple[str, ...] = ()
    is_error: bool = False
    #: Which adapter recorded it (``forwarder`` / ``codex-stream`` /
    #: ``tailer`` / ``direct``) — for the tests and the logs.
    source: str = ""
    #: A shell tool's command text (``Bash`` / ``PowerShell`` / a Codex
    #: ``commandExecution``), capped; empty for every other tool.
    command: str = ""
    at: float = field(default_factory=time.monotonic)


TurnEndHandler = Callable[[TurnEndContext], Awaitable["TurnEndVerdict | None"]]

_turn_end_handlers: list[TurnEndHandler] = []
_tool_records: dict[str, collections.deque[ToolRecord]] = {}
_rounds: dict[str, int] = {}
_last_turn_end: dict[str, float] = {}
_last_hook_eval: dict[str, float] = {}
# When the person last cut into the session (a message queued behind the
# turn, a Stop): a check round started before it is cancelled, and a
# proxy-driven turn it cut is neither judged nor continued.
_user_message_at: dict[str, float] = {}
# Coalesce concurrent evaluations per session: a hook and a loop racing, or
# an agent curling its own Stop endpoint, never evaluate twice at once.
_turn_end_locks: dict[str, asyncio.Lock] = {}


def register_turn_end_handler(handler: TurnEndHandler) -> None:
    """Add a handler consulted at ``turn_end``; the first continue verdict
    wins. Handlers are platform code only (a check, lane 3)."""
    if handler not in _turn_end_handlers:
        _turn_end_handlers.append(handler)


def unregister_turn_end_handler(handler: TurnEndHandler) -> None:
    with contextlib.suppress(ValueError):
        _turn_end_handlers.remove(handler)


def cleanup_session(session_id: str) -> None:
    """Forget a session's records (called with the rest of its state)."""
    _tool_records.pop(session_id, None)
    _rounds.pop(session_id, None)
    _last_turn_end.pop(session_id, None)
    _last_hook_eval.pop(session_id, None)
    _user_message_at.pop(session_id, None)
    _turn_end_locks.pop(session_id, None)


def note_user_message(session_id: str) -> None:
    """The person cut into the session: a message queued behind the turn,
    or a Stop. A check round that started before it is cancelled by the
    evaluator (``user_message_since``), and ``drive_turns`` neither judges
    nor continues the turn it cut. A message steered INTO the running turn
    is part of that turn and is not noted."""
    _user_message_at[session_id] = time.monotonic()


def user_message_since(session_id: str, since: float) -> bool:
    """Whether a person's message landed after the monotonic instant."""
    return _user_message_at.get(session_id, 0.0) > since


# ---------------------------------------------------------------------------
# pre_tool
# ---------------------------------------------------------------------------

async def pre_tool(
    session_id: str, tool_name: str, tool_input: dict | None = None,
    *, live_permission_mode: str = "",
) -> dict:
    """The named door to the permission authority. Returns the authority's
    ``{"decision": allow|deny|ask|defer, "reason"?, "updated_input"?}``."""
    from api.hooks.permission import decide_tool_permission
    return await decide_tool_permission(
        session_id, tool_name, tool_input or {},
        live_permission_mode=live_permission_mode,
    )


# ---------------------------------------------------------------------------
# post_tool
# ---------------------------------------------------------------------------

_PATH_KEYS = ("file_path", "path", "notebook_path")
# Lists of paths an adapter already extracted: the Codex translator's patch
# paths, the headless forwarder's ``tool_paths``.
_PATH_LIST_KEYS = ("_codex_paths", "_paths")
# Where the adapters put a shell command: the CLIs' ``command``, the Codex
# rollout's ``cmd`` (``exec_command``) and ``input`` (``custom_tool_call``).
_COMMAND_KEYS = ("command", "cmd", "input")


def _paths_of(tool_input: dict | None) -> tuple[str, ...]:
    if not isinstance(tool_input, dict):
        return ()
    out: list[str] = []
    for key in _PATH_KEYS:
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            out.append(v)
    for key in _PATH_LIST_KEYS:
        extra = tool_input.get(key)
        if isinstance(extra, list):
            out.extend(p for p in extra if isinstance(p, str) and p)
    return tuple(dict.fromkeys(out))


def _command_of(tool_name: str, tool_input: dict | None) -> str:
    """The command text of a shell tool call, capped; ``""`` otherwise."""
    if tool_roles.role_of(tool_name) != tool_roles.SHELL or not isinstance(tool_input, dict):
        return ""
    for key in _COMMAND_KEYS:
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            raw = v.encode("utf-8")
            if len(raw) > MAX_COMMAND_BYTES:
                return raw[:MAX_COMMAND_BYTES].decode("utf-8", errors="ignore")
            return v
    return ""


def post_tool(
    session_id: str, tool_name: str, *, tool_use_id: str = "",
    tool_input: dict | None = None, is_error: bool = False, source: str = "",
    command: str = "",
) -> ToolRecord:
    """Record one finished tool call on the session's ring. Cheap and
    synchronous; the rendering and the cost engine stay with the caller.
    ``command`` is for an adapter that carries the shell text outside the
    input (the headless forwarder); the input's own is read otherwise."""
    rec = ToolRecord(
        tool_name=tool_name or "", tool_use_id=tool_use_id or "",
        paths=_paths_of(tool_input), is_error=bool(is_error), source=source,
        command=_command_of(tool_name or "", {"command": command} if command
                            else tool_input),
    )
    ring = _tool_records.get(session_id)
    if ring is None:
        ring = collections.deque(maxlen=MAX_TOOL_RECORDS)
        _tool_records[session_id] = ring
    ring.append(rec)
    return rec


def tool_records(session_id: str) -> list[ToolRecord]:
    """The session's tool calls since the turn began (oldest first)."""
    return list(_tool_records.get(session_id) or ())


def clear_tool_records(session_id: str) -> None:
    ring = _tool_records.get(session_id)
    if ring is not None:
        ring.clear()


# ---------------------------------------------------------------------------
# subagent_stop
# ---------------------------------------------------------------------------

def subagent_stop(session_id: str, agent_id: str, agent_type: str = "") -> bool:
    """A subagent finished. Marks the registry (``buffer=True`` parks a stop
    that raced its spawn). Returns True on the transition to completed, False
    for a duplicate — the callers dedup on it."""
    from core.session.session_state import get_subagent_registry
    reg = get_subagent_registry(session_id)
    done = reg.mark_done(agent_id, buffer=True)
    logger.debug(
        "subagent_stop session=%s agent=%s type=%s transition=%s",
        session_id[:8], agent_id, agent_type, done,
    )
    return done


# ---------------------------------------------------------------------------
# turn_end
# ---------------------------------------------------------------------------

def _verdict_source_for(driven_by: str) -> str:
    return "hook" if driven_by == "cli" else "loop"


def rounds(session_id: str) -> int:
    return _rounds.get(session_id, 0)


async def turn_end(
    session_id: str, *, source: str, engine: str = "", driven_by: str = "proxy",
    transcript_path: str = "", last_message: str = "", stop_hook_active: bool = False,
) -> TurnEndVerdict:
    """The turn ended. Computes a verdict for the ONE source that can act on
    it for this placement (``hook`` when the CLI drives, ``loop`` when the
    proxy drives); any other source is an observation and gets ``STOP``.
    Rounds are counted per session and reset when a turn is allowed to end;
    the tool records are cleared then too (the turn is over)."""
    now = time.monotonic()
    _last_turn_end[session_id] = now
    if source != _verdict_source_for(driven_by):
        return STOP
    if source == "hook":
        # The hook path only: a loop calls once per turn by construction.
        if now - _last_hook_eval.get(session_id, -HOOK_MIN_INTERVAL_S) < HOOK_MIN_INTERVAL_S:
            logger.info("turn_end session=%s: a second hook evaluation within %.0fs — observation",
                        session_id[:8], HOOK_MIN_INTERVAL_S)
            return STOP
        _last_hook_eval[session_id] = now
    lock = _turn_end_locks.get(session_id)
    if lock is None:
        lock = _turn_end_locks[session_id] = asyncio.Lock()
    async with lock:
        done = _rounds.get(session_id, 0)
        if stop_hook_active and done == 0:
            # A hook reports a continue we delivered before this proxy
            # process saw it (a restart mid-loop): count it.
            done = 1
        ctx = TurnEndContext(
            session_id=session_id, source=source, engine=engine,
            driven_by=driven_by, transcript_path=transcript_path,
            last_message=last_message, rounds=done,
        )
        verdict = STOP
        # The last fix round is judged too (a check is evaluated up to its
        # rounds + 1 times); only a continue from it is refused.
        if done <= MAX_ROUNDS:
            for handler in list(_turn_end_handlers):
                try:
                    v = await handler(ctx)
                except Exception:
                    logger.exception(
                        "turn_end handler failed (session %s); the turn ends",
                        session_id[:8],
                    )
                    continue
                if v is not None and v.should_continue and done < MAX_ROUNDS:
                    verdict = continue_with(v.continue_reason)
                    break
        if done >= MAX_ROUNDS and _turn_end_handlers:
            logger.info(
                "turn_end session=%s: %d rounds reached — the turn ends",
                session_id[:8], done,
            )
        if verdict.should_continue:
            _rounds[session_id] = done + 1
        else:
            _rounds.pop(session_id, None)
            clear_tool_records(session_id)
        return verdict


# ---------------------------------------------------------------------------
# Declared, unwired
# ---------------------------------------------------------------------------

def session_start(session_id: str, *, engine: str = "", source: str = "startup") -> None:
    """A session started or resumed. No consumer yet; the CLIs' hooks for it
    are documented in HOOKS.md "Available and unused"."""
    logger.debug("session_start session=%s engine=%s source=%s", session_id[:8], engine, source)


def session_end(session_id: str, *, engine: str = "", reason: str = "") -> None:
    logger.debug("session_end session=%s engine=%s reason=%s", session_id[:8], engine, reason)


def prompt_submit(session_id: str, *, engine: str = "", prompt: str = "") -> None:
    logger.debug("prompt_submit session=%s engine=%s chars=%d", session_id[:8], engine, len(prompt or ""))


# ---------------------------------------------------------------------------
# drive_turns — the proxy-driven delivery of a continue verdict
# ---------------------------------------------------------------------------

class _TextTail:
    """The turn's text, tail-kept at ``MAX_LAST_MESSAGE_BYTES`` — what the
    loop hands ``turn_end`` as the result (a judge reads the answer, not
    only the files)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._cut = False

    def add(self, chunk) -> None:
        if not isinstance(chunk, str) or not chunk:
            return
        self._buf.extend(chunk.encode("utf-8"))
        if len(self._buf) > MAX_LAST_MESSAGE_BYTES:
            del self._buf[:len(self._buf) - MAX_LAST_MESSAGE_BYTES]
            self._cut = True

    def text(self) -> str:
        out = bytes(self._buf).decode("utf-8", errors="ignore")
        return ("… " + out) if self._cut else out


async def drive_turns(
    session_id: str, engine: str, first_message: str,
    run_turn: Callable[[str], AsyncIterator],
    *, max_rounds: int = MAX_ROUNDS,
) -> AsyncIterator:
    """Run one user turn through ``run_turn`` and, while ``turn_end`` says
    continue, run the reason as a follow-up turn on the same session — the
    proxy-driven twin of a ``Stop`` hook block. Yields every event of every
    turn except the ``DONE`` of a turn that is being continued (the consumer
    must see one turn end, not several).

    ``run_turn(message)`` is the layer's own async generator for one turn.

    A turn that ended in an engine error (the pump stops reading at the
    first one), never finished, or that the person cut into (a queued
    message, a Stop: ``note_user_message``) is neither judged nor
    continued; its tool records stay on the ring, so the next turn's
    evaluation covers what it changed.
    """
    from core.events.common_events import DONE, ERROR, TEXT
    # A new user turn starts at round 0: a fix round that never reached its
    # turn_end (a hard Stop, a lost layer) leaves no count behind.
    _rounds.pop(session_id, None)
    message = first_message
    round_no = 0
    while True:
        held_done = None
        failed = False
        started = time.monotonic()
        text = _TextTail()
        async for ev in run_turn(message):
            ev_type = getattr(ev, "type", None)
            if ev_type == DONE:
                held_done = ev      # decide after the verdict
                continue
            if ev_type == ERROR:
                failed = True
            elif ev_type == TEXT:
                text.add((getattr(ev, "data", None) or {}).get("content"))
            yield ev
        if failed or held_done is None or user_message_since(session_id, started):
            _rounds.pop(session_id, None)
            logger.info(
                "turn_end session=%s engine=%s: not judged (%s)", session_id[:8], engine,
                "engine error" if failed else "no end" if held_done is None
                else "the person cut in",
            )
            if held_done is not None:
                yield held_done
            return
        verdict = await turn_end(
            session_id, source="loop", engine=engine, driven_by="proxy",
            last_message=text.text(),
        )
        if (not verdict.should_continue or round_no >= max_rounds
                or user_message_since(session_id, started)):
            if verdict.should_continue:
                _rounds.pop(session_id, None)
            if held_done is not None:
                yield held_done
            return
        round_no += 1
        logger.info(
            "turn_end session=%s engine=%s: continuing (round %d)",
            session_id[:8], engine, round_no,
        )
        message = verdict.continue_reason
