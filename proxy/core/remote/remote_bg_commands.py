"""Between-turns reads of a remote session's event queue (mixin).

``drain_bg_commands`` resolves background-command completions the way the
session's ENGINE surfaces them (``RemoteEngineAdapter.drain_bg_commands``:
the CLI engine reads the frames the satellite forwards between turns, the
Codex engine reconciles the registry against the satellite's terminal
list). ``queue_frame_reader`` is the generic queue reader the wake-bracket
capture uses here and in ``send_message``'s stale drain. Mixed into
RemoteExecutionLayer; split out of remote_execution.py.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.remote.remote_session_info import RemoteSessionInfo  # noqa: F401 (quoted annotations)

logger = logging.getLogger("remote-layer")


def queue_frame_reader(info: "RemoteSessionInfo"):
    """Frame reader over ``info.event_queue`` for a wake-bracket capture:
    skips synthetic markers, bumps ``last_activity`` (idle reaper), None
    on timeout. The caller holds ``info.lock`` for the whole capture."""
    async def _read(timeout: float) -> dict | None:
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
            if isinstance(raw, dict):
                info.last_activity = time.monotonic()
                return raw
    return _read


class RemoteBgCommandsMixin:
    # --- Background commands between turns ---

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0) -> bool:
        """Resolve background-command completions for a REMOTE session between
        turns, the engine's way. Returns True if any resolved. The post-turn
        bg-command monitor (stream_pump.py) polls this — bg bash has no
        completion hook, so this active read is the only signal."""
        info = self._sessions.get(session_id)
        if info is None:
            return False
        return await self._adapter(info).drain_bg_commands(info, self._cm, budget=budget)
