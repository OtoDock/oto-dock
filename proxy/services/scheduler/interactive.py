"""Interactive (PTY) task runs: the max-time backstop, ``_run_interactive_task``
and the session close.

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import logging


import config

logger = logging.getLogger("claude-proxy.scheduler")


# Hard max-time backstop for an interactive task run:
# if the turn-end signal never lands (CLI hung / crashed), the run is failed with
# a clear timeout rather than awaiting forever. Reuses the CLI turn ceiling (2h).
INTERACTIVE_TASK_MAX_S = float(getattr(config, "INTERACTIVE_TASK_MAX_S", config.CLAUDE_TIMEOUT))


class _InteractiveSessionDied(RuntimeError):
    """The interactive worker's PTY ended before the turn-end signal.

    ``had_viewer`` distinguishes a deliberate user stop (someone was watching
    the PTY and closed/killed the CLI → the run reports "user_interrupted")
    from an unattended crash (plain "failed")."""

    def __init__(self, message: str, *, had_viewer: bool = False):
        super().__init__(message)
        self.had_viewer = had_viewer


async def _run_interactive_task(
    session_id: str, chat_id: str, prompt: str, first_prompt_in_argv: bool,
) -> None:
    """Drive a FRESH interactive task to completion.

    There is no pump for an interactive session (the PTY emits raw bytes, not
    CommonEvents), so completion is detected from the turn-end signal: the
    transcript/rollout tailer (already running on the output-quiet debounce +
    close + reaper sweep) fires ``interactive_session.on_turn_complete`` once a
    turn ends with the bg ``SubagentRegistry`` empty + min-turn-time elapsed. We
    register that callback, inject the cold first prompt, and await it under a
    hard max-time backstop. The tailer has already persisted the turns to
    ``chat_messages`` by the time it fires, so the caller's ``lanes._collect_task_output``
    + ``update_run`` + delivery work unchanged."""
    from core.session import interactive_session

    isess = interactive_session.get(session_id)
    if isess is None:
        raise RuntimeError("interactive task session was not registered")

    done = asyncio.Event()

    def _on_complete(_last_message: str) -> None:
        done.set()

    # Register BEFORE injecting the prompt so no turn can complete unobserved.
    isess.on_turn_complete = _on_complete
    # Cold first prompt: Codex fresh delivered it via the launch argv (auto-runs
    # after MCP warm); Claude needs the PTY flush (buffered behind the readiness
    # gate until the TUI accepts input). A trailing CR submits.
    if not first_prompt_in_argv:
        isess.submit_prompt(prompt)

    # Wait for the turn-end signal — but FAIL FAST if the PTY dies (CLI crash /
    # idle reap) instead of hanging until the max-time backstop. Poll liveness
    # between short waits on the completion event. (Without this, a Codex exit-1
    # on a bad config left the run stuck "running" for the full timeout.)
    waited = 0.0
    _STEP = 5.0
    while True:
        try:
            await asyncio.wait_for(done.wait(), timeout=_STEP)
            return  # turn completed (on_turn_complete fired)
        except asyncio.TimeoutError:
            if not isess.alive:
                raise _InteractiveSessionDied(
                    "Interactive task session ended before completing "
                    "(the CLI exited or was reaped)",
                    had_viewer=isess.had_viewer,
                )
            waited += _STEP
            if waited >= INTERACTIVE_TASK_MAX_S:
                raise RuntimeError(
                    f"Interactive task did not complete within "
                    f"{int(INTERACTIVE_TASK_MAX_S)}s (no turn-end signal)"
                )


async def _close_interactive_task_session(session_id: str) -> None:
    """Tear down an interactive task's PTY session (no-op if it wasn't one).

    ``layer.close_session`` only closes pump/daemon sessions — an interactive
    session lives in ``core.session.interactive_session`` — so completion/cancel/failure
    cleanup must close it here too (its final tail persists any tail-end output,
    then the slot is released)."""
    try:
        from core.session import interactive_session
        await interactive_session.close_session(session_id, reason="task_end")
    except Exception:
        logger.exception("interactive task %s: session close failed", session_id[:8])
