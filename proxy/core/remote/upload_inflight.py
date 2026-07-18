"""In-flight dashboard-upload push registry — the turn-start barrier's
source of truth.

``POST /v1/upload`` returns as soon as the platform write lands and pushes
the file to remote satellites in the BACKGROUND (``api/media/uploads.py``),
so the dashboard's upload spinner and workspace listing never stall on a
slow satellite link. That opens a gap: a user can attach a file in chat and
send a prompt referencing it before the push reaches the satellite — the
CLI there would hit ENOENT. This module closes the gap: every backgrounded
upload push registers here, and ``RemoteExecutionLayer.send_message``
awaits the agent's in-flight pushes (bounded) BEFORE dispatching the
prompt to the satellite.

The barrier is deliberately bounded (``BARRIER_TIMEOUT_S``): a wedged push
must not hang turns. Pushes were always best-effort — a failed push falls
back to the periodic fingerprint sweep / the next session-start sync — so
proceeding after the cap matches the pre-existing failure mode exactly.

Keyed by agent slug, not machine id: the fan-out computes its machine
targets deep inside ``workspace_fanout.fan_out_write`` (after isolation
filtering), so the target machines aren't known when the task is created.
Worst case a turn waits on a push bound for a DIFFERENT machine of the
same agent — rare (two machines simultaneously active on one agent with an
upload racing a prompt) and bounded by the cap.

Out of scope by design:
  * Local sessions never touch this module — the barrier lives in the
    remote execution layer only, and local (bind-mounted) workspaces have
    nothing to push.
  * PTY/terminal sessions have no turn-dispatch chokepoint (keystrokes
    stream raw), so they get no barrier; by the time a human has typed a
    prompt the push has usually landed, and the same sweep/session-start
    backstops cover the rest.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Coroutine

logger = logging.getLogger("claude-proxy.upload-inflight")

# Hard ceiling on how long a turn waits for in-flight uploads. Generous
# enough for a multi-MB file on a slow WAN link (chunked pushes ack every
# window; a DEAD link fails its ack wait in ~30s inside ``push_file``),
# small enough that a wedged push can't hang the chat.
BARRIER_TIMEOUT_S = 60.0

_inflight: dict[str, set[asyncio.Task]] = {}


def track(agent_slug: str, coro: Coroutine) -> asyncio.Task:
    """Run ``coro`` (an upload push) as a background task, registered so
    ``wait_settled`` can barrier on it. The registry holds a strong ref
    until completion (a bare ``create_task`` is GC-collectable mid-flight).
    The caller is responsible for the coroutine never raising — push
    helpers are best-effort by contract and swallow their own errors."""
    task = asyncio.create_task(coro)
    _inflight.setdefault(agent_slug, set()).add(task)
    task.add_done_callback(lambda t: _discard(agent_slug, t))
    return task


def _discard(agent_slug: str, task: asyncio.Task) -> None:
    tasks = _inflight.get(agent_slug)
    if tasks is not None:
        tasks.discard(task)
        if not tasks:
            _inflight.pop(agent_slug, None)


def pending_count(agent_slug: str) -> int:
    """Number of in-flight pushes for ``agent_slug`` (tests / diagnostics)."""
    return sum(1 for t in _inflight.get(agent_slug, ()) if not t.done())


async def wait_settled(
    agent_slug: str, *, timeout: float = BARRIER_TIMEOUT_S,
) -> bool:
    """Wait (bounded) until every upload push in flight for ``agent_slug``
    AT CALL TIME has finished. Pushes that start DURING the wait are not
    awaited — the barrier protects the prompt that was just submitted, not
    future ones — so a steady stream of uploads can't extend the wait
    unboundedly.

    Returns True when everything settled, False on cap. Callers proceed
    either way — the barrier is best-effort, like the pushes themselves.
    Never raises.
    """
    tasks = {t for t in _inflight.get(agent_slug, ()) if not t.done()}
    if not tasks:
        return True
    started = time.monotonic()
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
    except Exception:
        logger.exception("upload barrier wait failed for %s", agent_slug)
        return False
    waited = time.monotonic() - started
    if pending:
        logger.warning(
            "upload barrier: %d push(es) for agent %s still in flight after "
            "%.0fs — dispatching the turn anyway (the push continues in the "
            "background; the satellite reconciles via the fingerprint sweep "
            "or its next session-start sync)",
            len(pending), agent_slug, waited,
        )
        return False
    if waited >= 0.05:
        logger.info(
            "upload barrier: turn for agent %s waited %.2fs for %d upload "
            "push(es)", agent_slug, waited, len(done),
        )
    return True
