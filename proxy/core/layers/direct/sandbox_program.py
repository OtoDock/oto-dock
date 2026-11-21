"""A program of the platform's own that runs in a session's sandbox.

The Direct LLM loop stays in the proxy. What it must do inside a session's
own folders it hands to a small program that the compute machine runs in the
session's sandbox, where the kernel is the wall. This module is the general
half of that on the platform. It knows nothing of what a program does: the
file tools are its first user (``files.PROGRAM``).

What a program gets here, and what sets it apart from a tool's server
(``mcp.MCPServerConnection``, whose one-task ownership of the streams it
reuses):

- **Its text rides the open.** The program is the proxy's own text, joined
  behind the general loop (``sandbox_program_runtime.py``) and sent in one
  variable of the open's environment. No image, no folder and no satellite
  carries it, and no session can change it.
- **A sandbox of the role table and nothing else.** Its open sends a copy of
  the session's description with no tool folder, no manifest mount, no
  credential exit and no SSH socket. A manifest mount can sit inside a
  session folder, where the kernel would show a tool's folder: a program
  never sees one. Its environment holds no token and no secret.
- **Counted, never judged.** Its start is counted on the session's box. A
  restart and a start that failed give the count back, and the session's
  own release takes it at the session's end. It is never refused for
  memory: the session's loop already runs.
- **A cap of its own and a restart.** A call that passes the cap, and a
  program that ended, both close the program and arm a new start for the
  next call. A restart arms whatever raised on its way. A slow call never
  counts against the program. Starts that fail, programs that end and
  restarts that raise, several in a row, make it rest for a while, and
  then it is tried again. A lost link is the session's loss. Nothing else
  ends a program before its session does.
- **Its life is its own.** A start and a restart run as tasks of the
  program, one at a time, and a call only waits for them. So a call that is
  stopped (a Stop in a chat, a turn that ends) leaves no start and no
  restart half done, and ``close`` waits for what is in flight.
- **A call that changes something is one piece with what follows it.** Such
  a call takes a ``then``: what the platform does with the answer (a written
  file's record). From the moment the program is asked, the call and its
  ``then`` run as a task of the program's own that keeps the call's turn. A
  stop ends only the caller's wait, and the next call is asked only after
  the ``then`` has ended, so two calls' records land in the calls' order. A
  call stopped before that moment asks the program nothing. ``close`` gives
  a piece in flight a short while to end before it stops the program.

The interface is a ``ProgramSpec`` handed to the session's manager
(``AgentMCPManager(programs=...)``) and one call, ``call``. A second program
writes its operations, its spec and its handlers, and nothing here changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import math
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from core import placement
from core.layers.direct.mcp import MCPServerConnection, TimedOut, _run_on_mcp_loop
from core.remote import tool_relay

if TYPE_CHECKING:
    from core.layers.direct.mcp import AgentMCPManager
    from core.sandbox.sandbox import SandboxBuilder
    from core.sandbox.sandbox_render import SandboxDescription

logger = logging.getLogger("mcp-manager")

_T = TypeVar("_T")

#: What every program's name starts with: outside the manifest name grammar,
#: so no installed tool can take a program's name.
NAME_PREFIX = "oto:"
#: The variable of the open's environment that carries a program's text.
TEXT_ENV = "OTO_PROGRAM"
#: The start: it takes the text out of the environment and runs it.
_START = f'import os,sys;exec(compile(os.environ.pop("{TEXT_ENV}"),"<oto-program>","exec"))'
#: One environment value takes 128 KB on Linux. A program's whole text stays
#: well under it.
MAX_TEXT_BYTES = 64 * 1024

# What ``call`` answers when the program gave no answer of its own. Two
# families, and ``asked`` tells them apart. After the first four the program
# was asked nothing, so nothing changed:
#: There is no program to ask: the session ended, or its machine's link went.
NOT_RUNNING = "not_running"
#: The machine could not start the program (``detail`` says why).
NOT_STARTED = "not_started"
#: It ended or could not start several times in a row and is tried again in
#: ``seconds``.
RESTING = "resting"
#: The call could not be sent: it does not encode as text, or the relay
#: refused it as too large for a frame.
UNSENT = "unsent"
# After the next four the program HAD been asked, and what it did is not known:
#: The program ended before it answered this call. The machine is there:
#: the next call starts a new one.
ENDED = "ended"
#: The program did not answer inside its cap of ``seconds`` and was closed.
TIMED_OUT = "timed_out"
#: The program answered something that is no answer.
GARBLED = "garbled"
#: The session closed under the call.
CLOSED = "closed"
_ASKED = frozenset({ENDED, TIMED_OUT, GARBLED, CLOSED})

#: How long a session's close waits for a piece in flight (a call with a
#: ``then`` that the program was asked) before it stops the program. A write
#: takes milliseconds, and a hang-up closes a call's session in the moment it
#: stops the turn.
PIECE_CLOSE_WAIT_S = 3.0

#: What the platform does with a call's answer, on the platform's own loop.
#: Its contract, for the author of a program that takes one:
#:
#: - It runs once for every call that reached its piece: a call the program
#:   was asked (``asked`` says of the answer whether the outcome is known),
#:   and also one the relay refused to send (``unsent``: nothing changed).
#:   It never runs for a call that ended before its piece began.
#: - It must be short. The piece keeps the call's turn until it has ended,
#:   and every later call of that program waits behind it with no cap.
#: - It must never call a program of its own session: it would wait for
#:   its own turn for ever.
#: - It runs after a Stop of the caller too. One that must not begin after
#:   a Stop reads a mark that its caller sets on the platform's loop when
#:   it is cancelled (``builtins._delete``): both run on that loop, so the
#:   mark is set or the ``then`` has begun.
#: - One that raises is logged, and the answer stands.
Then = Callable[[dict], Coroutine[Any, Any, None]]


def asked(answer: dict) -> bool:
    """Whether the program was asked and gave no answer of its own: what the
    operation did is not known. Every other answer of this module's own says
    that the program was asked nothing, and an answer of the program's own
    says what it did."""
    return answer.get("ok") is False and answer.get("code") in _ASKED


def _encodes(value: object) -> bool:
    """Whether every string of a call's arguments can be sent as text. One
    that cannot (a lone surrogate) would fail inside the relay's writer,
    under the program's streams and not at the caller."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, dict):
        return all(_encodes(key) and _encodes(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_encodes(item) for item in value)
    return True

_RUNTIME = Path(__file__).with_name("sandbox_program_runtime.py").read_text(encoding="utf-8")


@dataclass(frozen=True)
class ProgramSpec:
    """One program of the platform's own. ``name`` starts with
    ``NAME_PREFIX``. ``source`` is the program's own text: its operations
    (functions of ``(facts, args)`` that answer a dict, standard library
    only, Python 3.10, no ``from __future__`` line) and a table
    ``OPERATIONS``. ``facts`` makes what the program is told at each start,
    from the description its sandbox is rendered from and the session's
    builder. ``call_timeout_s`` is the cap of one call. After
    ``max_failures`` starts that failed or programs that ended, in a row,
    the program rests for ``rest_s`` before it is tried again.
    ``starts_with_call``: a call's session starts it with the session,
    beside the model, so that no turn of a call waits for its start."""
    name: str
    source: str
    facts: Callable[[SandboxDescription, SandboxBuilder], dict]
    call_timeout_s: float = 30.0
    max_failures: int = 3
    rest_s: float = 60.0
    starts_with_call: bool = True

    def __post_init__(self) -> None:
        if not self.name.startswith(NAME_PREFIX):
            raise ValueError(f"a program's name starts with {NAME_PREFIX!r}: {self.name!r}")
        if len(program_text(self).encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError(f"the program {self.name!r} is too large to ride an open")


def program_text(spec: ProgramSpec) -> str:
    """The one text a sandbox runs: the general loop, the program's own
    text, and the line that starts the loop on its operations."""
    return f"{_RUNTIME}\n{spec.source}\n_serve(OPERATIONS)\n"


def program_description(builder: SandboxBuilder) -> SandboxDescription:
    """The sandbox a program runs in: the session's role table and nothing
    else. No tool folder, no manifest mount, no credential exit and no SSH
    socket (the render refuses a start while a machine's exit is not up,
    and a program needs none of them). Every other field is the session's:
    a mask and a refusal stay. ``test_direct_sandbox_program.py`` names each
    field with what a program gets, so a new field is a decision.

    A session that runs from an engine home of its own gets no program. The
    home holds the session's sign-in, so a program must not have it. But the
    render shows the store's own sign-in file empty only for a description
    that names a home (``sandbox_render.home_args``): with the home taken
    out, that file would show. No Direct session has a home today. The day
    one does, a program needs the render to keep that mask without the
    home, and until then it refuses to start."""
    description = builder.describe()
    if description.home is not None:
        raise RuntimeError("a session with a home of its own has no sandbox for a platform program yet")
    return dataclasses.replace(description, mcp_binds=(), extra_binds=(), exit_port=0, ssh_agent="")


class SandboxProgram(MCPServerConnection):
    """One program of one session, on the session's compute machine."""

    #: A program is no tool's server: it has no hints to keep.
    records_hints = False

    def __init__(self, spec: ProgramSpec, *, session_id: str, sandbox_builder: Any, agent_name: str,
                 machine_id: str, main_loop: asyncio.AbstractEventLoop | None) -> None:
        super().__init__(spec.name, {"type": "stdio"}, session_id=session_id,
                         sandbox_builder=sandbox_builder, agent_name=agent_name,
                         tool_timeout=max(1, int(spec.call_timeout_s)), machine_id=machine_id,
                         main_loop=main_loop)
        self.spec = spec
        # It starts at its first call (or with a call's session, the
        # manager's word), never with the session's tools.
        self.pending = True
        #: Starts that failed and programs that ended, in a row. A call the
        #: program answered clears it.
        self.failures = 0
        # No start is tried before this moment (``time.monotonic``).
        self._rest_until = 0.0
        self._counted = False
        self._description: SandboxDescription | None = None
        # The steps of its life in flight (a start, a restart): tasks of
        # the program's own, one at a time.
        self._steps: set[asyncio.Task] = set()
        self._turn: asyncio.Lock | None = None
        # The calls in flight that the program was asked with a ``then``:
        # each a task of the program's own, which keeps the call's turn.
        self._pieces: set[asyncio.Task] = set()
        # One call at a time: the program itself answers one at a time, and
        # no step of another call can then come between a call's start and
        # its operation.
        self._calls: asyncio.Lock | None = None

    # -- The seams of ``MCPServerConnection`` ------------------------------

    async def _admission(self):
        """Counted on the session's box, never judged."""
        admission = await self._admit(judged=False)
        self._counted = bool(admission)
        return admission

    def _relay_description(self) -> dict:
        description = self._description or program_description(self.sandbox_builder)
        return description.to_dict()

    def _relay_exit_block(self) -> dict | None:
        return None

    async def _give_back(self) -> None:
        """Its count given back, once: a start that failed and a restart
        both come through here."""
        if self._counted:
            self._counted = False
            await super()._give_back()

    async def _stop_owner(self) -> None:
        """The program stopped, as a tool's server is. An owner task that
        had ended as cancelled (the client's own task group can leave one
        so) has ended: the stop is done, and whoever asked for it was not
        stopped. Waiting for such a task raises the same error as a cancel
        of the stop itself, and the stop's own task tells the two apart:
        only a cancel of the stop itself goes on."""
        try:
            await super()._stop_owner()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is None or current.cancelling():
                raise

    async def _start_stdio(self) -> None:
        """The program on the session's compute machine: its text in the
        open's environment, its facts as its one argument."""
        if not self.machine_id:
            self.refused = placement.NO_COMPUTE_SENTENCE
            raise RuntimeError(self.refused)
        if self.sandbox_builder is None:
            raise RuntimeError(f"the program {self.name!r} has no sandbox to run in")
        from core.sandbox.session_config_dir import sandbox_python
        self._description = program_description(self.sandbox_builder)
        facts = json.dumps(self.spec.facts(self._description, self.sandbox_builder))
        await self._start_relayed(sandbox_python(), ["-I", "-c", _START, facts],
                                  {TEXT_ENV: program_text(self.spec)})

    # -- Its own life -------------------------------------------------------
    #
    # A start and a restart are steps of the program's life. Whoever asks
    # for one may be stopped at any await: a Stop in a chat cancels the
    # turn's task, a turn's time ends, a session closes. A step that ran in
    # that task would stop half done, with a program started that nobody
    # holds or a program closed that nobody armed again. So a step never
    # runs in the task that asked for it.

    def _begin(self, step: Callable[[], Coroutine[Any, Any, _T]]) -> asyncio.Task[_T]:
        """One step of the program's life begun as a task of the program's
        own. The steps run one at a time, in the order they were begun, and
        each runs to its end whatever happens to the one who asked.
        ``settled`` waits for those in flight."""
        task = asyncio.ensure_future(self._in_turn(step))
        self._steps.add(task)
        task.add_done_callback(self._step_ended)
        return task

    async def _in_turn(self, step: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        if self._turn is None:
            self._turn = asyncio.Lock()
        async with self._turn:
            return await step()

    def _step_ended(self, task: asyncio.Task) -> None:
        self._steps.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Read here, so that a step nobody waited for leaves no warning
            # of an exception nobody took.
            logger.error("program %s of %s: a step of its life failed", self.name,
                         (self.session_id or "")[:8], exc_info=task.exception())

    async def _own(self, step: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        """A step begun and waited for. The wait is all a stop can end."""
        return await asyncio.shield(self._begin(step))

    async def settled(self) -> None:
        """Every step of its life in flight has ended."""
        while self._steps:
            await asyncio.wait(list(self._steps))

    def start_soon(self) -> None:
        """Its start begun now, beside whatever comes next: a call's session
        starts its program with the session."""
        self._begin(self._make_ready)

    async def close(self) -> None:
        """The session ended. A piece in flight gets a short while to end
        first: a write that was asked then keeps its record. Nothing new is
        asked meanwhile (``closed`` is set before the wait). Then a start in
        flight ends and its program is stopped with the rest, and a restart
        in flight ends too: nothing of the program's own life outlives its
        session. A piece whose program has not answered by then ends with
        its program, as a call that was asked and not answered, and one that
        is already in its ``then`` keeps its answer. Either ``then`` still
        runs on the platform's loop, so a piece can outlive this close by
        its ``then``.

        Whoever asked for the close may be stopped while it waits for a
        piece. The program is stopped all the same: its session has left
        the platform's books by then, so nothing else would close it."""
        self.closed = True
        try:
            if self._pieces:
                await asyncio.wait(list(self._pieces), timeout=PIECE_CLOSE_WAIT_S)
        finally:
            await super().close()
            await self.settled()

    def _arm(self, *, failed: bool) -> None:
        """A new start armed for the next call, while the session lives and
        its link holds. ``failed`` counts a start that failed or a program
        that ended by itself. After ``max_failures`` of those in a row the
        program rests, and the count starts anew."""
        if failed:
            self.failures += 1
            if self.failures >= self.spec.max_failures:
                self.failures = 0
                self._rest_until = time.monotonic() + self.spec.rest_s
        self.pending = not self.closed and not self.link_lost

    async def _restart(self, *, failed: bool) -> None:
        """A step: the program is closed (its place on the machine is free,
        its count given back) and a new start is armed. It arms whatever
        raised on the way: a program closed and not armed would answer no
        call again. A restart that raised counts as a failure, so one that
        raises every time comes to rest like a program that cannot start."""
        raised = True
        try:
            try:
                await self._stop_owner()
            finally:
                await self._give_back()
            raised = False
        finally:
            self.dead = False
            self._arm(failed=failed or raised)

    async def _renew(self, *, failed: bool) -> None:
        """The restart after a call that asked the program, begun and waited
        for. A restart that raises is its step's own failure: ``_restart``
        arms whatever raised and ``_step_ended`` tells it. It never becomes
        the call's. The program was asked, so the call still answers what is
        known of it, and a piece still hands that to its ``then``."""
        with contextlib.suppress(Exception):
            await self._own(functools.partial(self._restart, failed=failed))

    async def _make_ready(self) -> dict | None:
        """A step: the program brought to where a call can run. None once
        it runs, else what a call answers."""
        if self.closed:
            return {"ok": False, "code": NOT_RUNNING}
        if self.dead and not self.pending:
            # It ended by itself since its last call.
            await self._restart(failed=True)
        if self.pending:
            rest = self._rest_until - time.monotonic()
            if rest > 0:
                return {"ok": False, "code": RESTING, "seconds": math.ceil(rest)}
            self.dead = False       # whatever an earlier program left
            refused = await self.ensure_started()
            if refused:
                logger.info("program %s of %s did not start: %s", self.name,
                            (self.session_id or "")[:8], refused)
                self._arm(failed=True)
                if self.link_lost:
                    # The session's loss: its next message builds a new one.
                    return {"ok": False, "code": NOT_RUNNING}
                return {"ok": False, "code": NOT_STARTED, "detail": refused}
        if self.session is None:
            return {"ok": False, "code": NOT_RUNNING}
        return None

    async def call(self, operation: str, args: dict, then: Then | None = None) -> dict:
        """One operation of the program: its own answer (a dict with
        ``ok``), or one of this module's codes when it gave none. Runs on
        the tool loop.

        Without ``then`` the operation runs in the caller's task: a call
        that is stopped drops its answer. With ``then`` (what the platform
        does with the answer, on the platform's loop) the call and its
        ``then`` are one piece from the moment the program is asked: a task
        of the program's own that the caller only waits for. Either way a
        call that is stopped before its turn, or while its program starts,
        asks the program nothing, and no ``then`` runs. ``Then`` holds the
        contract of a ``then``: short, no call of a program of its own
        session, and every later call waits for it with no cap."""
        if not _encodes(args):
            return {"ok": False, "code": UNSENT}
        if self._calls is None:
            self._calls = asyncio.Lock()
        await self._calls.acquire()
        kept = False            # the turn went to a piece, which gives it back
        try:
            cannot = await self._own(self._make_ready)
            if cannot is not None:
                return cannot
            if then is None:
                return await self._ask(operation, args)
            # The piece's begin is the line between "not asked" and "asked".
            # No await lies between the ready program and it, so a stop lands
            # before it (nothing is asked) or on the wait below (the piece
            # runs on). There is no moment in between.
            piece = asyncio.ensure_future(self._whole(operation, args, then))
            self._pieces.add(piece)
            piece.add_done_callback(self._pieces.discard)
            kept = True
        finally:
            if not kept:
                self._calls.release()
        return await asyncio.shield(piece)

    async def _whole(self, operation: str, args: dict, then: Then) -> dict:
        """A call and what the platform does with its answer, to their end
        whatever happens to the caller. It keeps the call's turn until the
        ``then`` has ended: the next call is asked only after this one's
        record is whole. A ``then`` that fails is told here and the answer
        stands: what the program did is what the caller hears."""
        try:
            answer = await self._ask(operation, args)
            try:
                await self._on_platform(then(answer))
            except Exception:  # the record's own failure never changes what the program answered
                logger.exception("program %s of %s: what follows its answer failed (%s)", self.name,
                                 (self.session_id or "")[:8], operation)
            return answer
        finally:
            assert self._calls is not None
            self._calls.release()

    async def _on_platform(self, work: Coroutine[Any, Any, None]) -> None:
        """``work`` run on the proxy's own loop and waited for, as the
        program's count is asked there (``_admit``). With no such loop it
        runs here."""
        if self.main_loop is None:
            await work
            return
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work, self.main_loop))

    async def _ask(self, operation: str, args: dict) -> dict:
        """The program asked, once: its answer, or why there is none. An
        answer in hand is the answer, whatever happened to the session
        meanwhile."""
        text, _blocks = await self.call_tool_blocks(operation, args)
        timed_out = isinstance(text, TimedOut)
        answer: dict | None = None
        if not timed_out:
            try:
                parsed = json.loads(text)
            except (ValueError, RecursionError):     # no JSON, or nested deeper than the parser goes
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("ok"), bool):
                answer = parsed
        if answer is not None:
            self.failures = 0
        if self.closed:
            # The session ended under the call. Its close stops the program,
            # and the session's release takes the count.
            return answer if answer is not None else {"ok": False, "code": CLOSED}
        if timed_out:
            # A slow call says nothing against the program: it is closed,
            # so that its work ends, and started again at the next call.
            logger.warning("program %s of %s did not answer in %s s: closed", self.name,
                           (self.session_id or "")[:8], self.tool_timeout)
            await self._renew(failed=False)
            return {"ok": False, "code": TIMED_OUT, "seconds": self.tool_timeout}
        if self.dead:
            # The program ended: the next call starts a new one.
            await self._renew(failed=True)
            if answer is None:
                return {"ok": False, "code": ENDED}
        if answer is None and tool_relay.TOO_LARGE in text:
            # The relay refused the call's line: the program got nothing.
            return {"ok": False, "code": UNSENT}
        return answer if answer is not None else {"ok": False, "code": GARBLED}


async def call(manager: AgentMCPManager | None, name: str, operation: str, args: dict, *,
               then: Then | None = None) -> dict:
    """One operation of the program ``name`` of a session's manager: the
    program's answer, or one of this module's codes. A call moves the
    manager's clock, which the pool's idle reaper reads.

    ``then`` is what the platform does with the answer of a call that
    changes something. It runs on this loop, once, for every call that
    reached its piece: a call the program was asked (``asked`` says of its
    answer whether the outcome is known), and one the relay refused to send
    (``unsent``). It never runs for a call that ended before its piece
    began. From the moment the piece begins, a stop of the caller ends only
    the wait: the call and ``then`` run to their end
    (``SandboxProgram.call``). The contract of a ``then`` is written at
    ``Then``."""
    if manager is None:
        return {"ok": False, "code": NOT_RUNNING}
    program = manager.programs.get(name)
    if program is None:
        return {"ok": False, "code": NOT_RUNNING}
    manager.last_activity = time.monotonic()
    return await _run_on_mcp_loop(program.call(operation, args, then))
