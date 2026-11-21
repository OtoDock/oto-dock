"""Between-turns reads of a remote session's event queue (mixin).

``drain_bg_commands`` resolves background-command completions the way the
session's ENGINE surfaces them (``RemoteEngineAdapter.drain_bg_commands``:
the CLI engine reads the frames the satellite forwards between turns, the
Codex engine reconciles the registry against the satellite's terminal
list). ``read_between_turns`` is the one reader of a session whose queue the
platform reads between turns (an engine that owns its queue reads its own):
started when a line arrives with no turn reading (``wake_reader``), and the
body of the monitors' poll and of the stale drain before a send.
``queue_frame_reader`` is the queue reader the wake-bracket capture uses.
Mixed into RemoteExecutionLayer; split out of remote_execution.py.
"""

import asyncio
import contextlib
import logging
import math
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.remote.remote_session_info import (
        RemoteSessionInfo,
    )

logger = logging.getLogger("remote-layer")


def queue_frame_reader(info: "RemoteSessionInfo"):
    """Frame reader over ``info.event_queue`` for a wake-bracket capture:
    skips synthetic markers, bumps ``last_activity`` (idle reaper), None
    on timeout. The queue's end sentinel (the process exited on the
    machine) answers None at once and marks the CLI dead, as the turn
    stream does when it reads it, so ``stream_alive`` ends the bracket
    there. The caller holds ``info.lock`` for the whole capture."""
    async def _read(timeout: float) -> dict | None:
        kept = next_carried(info)
        if kept is not None:
            return kept
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(
                    info.event_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if raw is None:
                info.cli_dead = True
                return None
            if isinstance(raw, dict):
                info.last_activity = time.monotonic()
                return raw
    return _read


def next_carried(info: "RemoteSessionInfo") -> dict | None:
    """The next frame a turn's end kept for the readers between turns
    (``drain_until_turn_ended``): they read these before the queue."""
    return info.carried.pop(0) if info.carried else None


@dataclass
class EngineTurn:
    """The state of a turn the engine started by itself while it is the
    chat's live turn (``info.engine_turn``): whether it reached its result,
    and a steer written into it that the CLI may take after that result."""
    result_seen: bool = False
    steer_written: bool = False
    steer_pending: int = 0


def engine_turn_guard(info: "RemoteSessionInfo",
                      turn: EngineTurn) -> Callable[[], contextlib.AbstractContextManager[None]]:
    """The session's own turn, marked as a running turn while it is the
    chat's live turn: a Stop takes the graceful interrupt, a queued
    message's interrupt reaches it, a steer reaches it on a satellite that
    takes one (``info.engine_turn``) and the idle closes spare it, as for a
    turn the platform sent. Its own command id pins the interrupt's
    watchdog to it."""
    @contextlib.contextmanager
    def _guard() -> Iterator[None]:
        before = info.current_send_command_id
        info.turn_active = True
        info.current_send_command_id = f"engine-turn-{uuid.uuid4().hex[:12]}"
        info.engine_turn = turn
        try:
            yield
        finally:
            info.turn_active = False
            info.current_send_command_id = before
            info.engine_turn = None
    return _guard


async def capture_engine_turn(info: "RemoteSessionInfo", cm, first_frame: dict, *,
                              source: str) -> bool:
    """Record a turn the engine started by itself, read from the session's
    queue between turns: the one door for every reader between turns
    (``wake_capture.capture_wake_turn``)."""
    from core.events import wake_capture
    turn = EngineTurn()
    read = queue_frame_reader(info)

    async def _read(timeout: float) -> dict | None:
        frame = await read(timeout)
        if isinstance(frame, dict) and not frame.get("parent_tool_use_id"):
            if frame.get("type") == "result":
                turn.result_seen = True
            elif wake_capture.opens_turn(frame):
                turn.result_seen = False
        return frame

    def _steered() -> bool:
        taken = turn.steer_written or turn.steer_pending > 0
        turn.steer_written = False
        return taken

    return await wake_capture.capture_wake_turn(
        info.session_id, first_frame, _read, source=source,
        alive=stream_alive(info, cm), turn_guard=engine_turn_guard(info, turn),
        steered=_steered,
    )


# How long the reader keeps the session after its last line: a turn's lines
# come in bursts, and a new turn waits this long at most for the session.
READER_QUIET_S = 0.4


async def read_between_turns(info: "RemoteSessionInfo", cm, *, quiet_s: float,
                             source: str, budget_s: float = math.inf) -> tuple[bool, Counter]:
    """The one reader of a session's lines between turns; the caller holds
    the session's lock. Lines a turn's end kept come first, then the queue,
    until it stays empty for ``quiet_s`` (0: only what already waits) or the
    budget ends. A line that opens a turn of the main agent records that turn
    (``capture_engine_turn``), except after the platform's own hard stop
    (the dying turn's last lines are no turn). Any other line is background
    work or nobody's answer (``read_line_between_turns``). Returns whether
    background work ended, and the census of the lines read."""
    from core.events import wake_capture
    from core.session.background_frames import read_line_between_turns
    census: Counter[str] = Counter()
    progressed = False
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        raw = next_carried(info)
        if raw is None:
            try:
                raw = (info.event_queue.get_nowait() if quiet_s <= 0 else
                       await asyncio.wait_for(info.event_queue.get(), timeout=quiet_s))
            except (asyncio.QueueEmpty, asyncio.TimeoutError):
                break
        if not isinstance(raw, dict):
            census["?"] += 1
            continue
        info.last_activity = time.monotonic()
        if wake_capture.opens_turn(raw) and not info.proxy_killed:
            census["wake_captured"] += 1
            await capture_engine_turn(info, cm, raw, source=source)
            progressed = True
            continue
        kind, ended = read_line_between_turns(info.session_id, raw)
        census[kind] += 1
        progressed = progressed or ended
    return progressed, census


def lines_wait(info: "RemoteSessionInfo") -> bool:
    return bool(info.carried) or not info.event_queue.empty()


def wake_reader(info: "RemoteSessionInfo", cm) -> None:
    """A line arrived with no turn reading the session, or a turn ended with
    lines waiting: the one reader runs, once any turn or record holding the
    session lets it go. Never for a session a task run's producer reads
    itself (``reading_held``), and never a second reader."""
    from core.background import spawn
    from core.events.pump_bg_monitors import reading_held
    if (info.reader is not None and not info.reader.done()) or not info.alive or not lines_wait(info):
        return
    if reading_held(info.session_id):
        return
    info.reader = spawn(_reader(info, cm), name="between_turns_reader", owner="remote-layer")


async def _reader(info: "RemoteSessionInfo", cm) -> None:
    """Reads while lines wait; ends when the session is quiet. It bumps the
    session's activity only for a line it reads, so it keeps no idle session
    open."""
    while info.alive and lines_wait(info):
        async with info.lock:
            if not lines_wait(info):
                return  # a turn that held the session read them
            await read_between_turns(info, cm, quiet_s=READER_QUIET_S, source="remote_reader")


def stream_alive(info: "RemoteSessionInfo", cm) -> Callable[[], bool]:
    """Whether the session can still write a frame: it is open, its CLI
    runs and its queue is reachable (attached, or held in the reconnect
    grace). The wake capture's silent gap ends a bracket only once this
    answers False, the twin of the local session's pipe check."""
    def _alive() -> bool:
        return bool(info.alive and not info.cli_dead
                    and cm.is_session_stream_attached(info.machine_id, info.session_id))
    return _alive


if TYPE_CHECKING:
    from core.remote.remote_host import RemoteLayerHost as _Host
else:
    _Host = object


class RemoteBgCommandsMixin(_Host):
    # --- Background commands between turns ---

    def _line_arrived(self, session_id: str) -> None:
        """A line of the session reached its queue (the connection's line
        listener): with no turn and no record reading the session, the one
        reader between turns runs. An engine that owns its queue reads it
        itself."""
        info = self._sessions.get(session_id)
        if info is None or info.turn_active or self._adapter(info).owns_event_queue:
            return
        wake_reader(info, self._cm)

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0) -> bool:
        """Resolve background-command completions for a REMOTE session between
        turns, the engine's way. Returns True if any resolved. The post-turn
        bg-command monitor (stream_pump.py) polls this — bg bash has no
        completion hook, so this active read is the only signal."""
        info = self._sessions.get(session_id)
        if info is None:
            return False
        return await self._adapter(info).drain_bg_commands(info, self._cm, budget=budget)
