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


# Per-chat terminal holds: the rounds that drive a chat's interactive
# terminal (a fresh interactive round, a borrowed round) serialize on one
# lock per chat, so a continue for another caller waits for the round in
# flight instead of steering into its turn and reporting its output twice.
_terminal_locks: dict[str, asyncio.Lock] = {}
_TERMINAL_HOLD_CEILING_S = 1800.0


class TerminalHold:
    """A held per-chat terminal lock; ``release`` is idempotent. The lock
    stays in the table: a waiter already holds a reference to it, so
    dropping the entry on release would let a third round take a fresh one."""

    def __init__(self, chat_id: str, lock: asyncio.Lock) -> None:
        self.chat_id = chat_id
        self._lock = lock

    def release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None and lock.locked():
            lock.release()


async def hold_terminal(chat_id: str) -> TerminalHold:
    """Take the chat's terminal hold, waiting out a round in flight (bounded
    like the lane quiescence wait: past the ceiling the round proceeds)."""
    lock = _terminal_locks.setdefault(chat_id, asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_TERMINAL_HOLD_CEILING_S)
    except asyncio.TimeoutError:
        logger.warning(
            f"Terminal hold ceiling reached for chat {chat_id[:8]} "
            f"({int(_TERMINAL_HOLD_CEILING_S)}s) — proceeding"
        )
        return TerminalHold(chat_id, asyncio.Lock())
    return TerminalHold(chat_id, lock)


def borrow_terminal(chat_id: str):
    """The live interactive session on ``chat_id`` a continue round drives
    instead of spawning — the person's terminal is the lane — or None."""
    from core.session import interactive_session
    live = interactive_session.find_live_for_chat(chat_id)
    if live is None or not live.alive:
        return None
    return live


async def _run_borrowed_terminal_turn(isess, prompt: str) -> None:
    """Drive a delegated follow-up through a LIVE interactive terminal the
    round did not spawn: queue the prompt for injection (steered into an
    open turn, pasted at quiescence otherwise) and wait for the first turn
    end AFTER it was injected — the person's own in-flight turn cannot
    satisfy it, since the waiter is armed by the injection hook, which the
    drain calls right after the paste with no yield in between. The tailer
    persists the turn, so the caller's output collection and delivery work
    unchanged. A cancel drops the prompt if it is still queued and leaves
    the terminal alone."""
    injected = asyncio.Event()
    done = asyncio.Event()

    def _on_turn_end(_last_message: str) -> None:
        done.set()

    def _on_injected() -> None:
        injected.set()
        isess.add_turn_end_waiter(_on_turn_end)

    item = isess.queue_prompt(
        prompt, "delegate_continue", steer=True, chat_id=isess.chat_id,
        on_injected=_on_injected,
    )
    if item is None:
        raise _InteractiveSessionDied(
            "the terminal closed before the follow-up could be queued",
            had_viewer=isess.had_viewer,
        )
    waited = 0.0
    _STEP = 5.0
    try:
        while True:
            try:
                await asyncio.wait_for(done.wait(), timeout=_STEP)
                return
            except asyncio.TimeoutError:
                if not isess.alive:
                    raise _InteractiveSessionDied(
                        "the terminal closed before the follow-up's turn ended",
                        had_viewer=isess.had_viewer,
                    )
                waited += _STEP
                if waited >= INTERACTIVE_TASK_MAX_S:
                    raise RuntimeError(
                        f"The follow-up's turn did not end within "
                        f"{int(INTERACTIVE_TASK_MAX_S)}s (no turn-end signal)"
                    )
    finally:
        if not injected.is_set():
            isess.cancel_prompt(item)
        isess.remove_turn_end_waiter(_on_turn_end)


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
