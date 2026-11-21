"""The live terminal sessions: the table, its lookups, closing them, and the idle
reaper. ``interactive_session`` registers a session here and re-exports this.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import config
from core.session.interactive_common import _TOKEN_MARGIN_S

if TYPE_CHECKING:
    from core.session.interactive_session import InteractiveSession

logger = logging.getLogger("otodock.interactive")

# Idle reaping is unified across all session kinds via config.get_idle_timeout()
# (the admin `session_idle_timeout` setting); reap_idle() reads it per-sweep. A
# viewer attached keeps an interactive session alive regardless of the timeout;
# its idle age moves on input, prompts, interrupts, turn signals and PTY output
# inside an open turn (see _fanout_output and reap_idle).
_REAPER_PERIOD_S = 60

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_sessions: dict[str, InteractiveSession] = {}
_reaper_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Registry API
# ---------------------------------------------------------------------------

def get(session_id: str) -> InteractiveSession | None:
    return _sessions.get(session_id)


def alive_on(session_id: str, machine_id: str) -> bool:
    """A terminal of this id runs, alive, on that machine."""
    session = _sessions.get(session_id)
    return bool(session is not None and getattr(session, "target", "") == machine_id and session.alive)


def find_live_for_chat(
    chat_id: str, *, target: str | None = None
) -> InteractiveSession | None:
    """dual-control: the NEWEST live session for ``chat_id`` (optionally pinned
    to the ``target`` machine). Used by ``otodock --resume`` to ATTACH to a running
    PTY instead of killing + re-spawning it (the in-flight turn survives). Returns
    the NEWEST-alive, not first-found: the spawn lease is per-session_id (not
    per-chat), so a brief fresh-respawn race can transiently leave two live
    sessions for one chat_id."""
    if not chat_id:
        return None
    matches = [
        s for s in _sessions.values()
        if s.chat_id == chat_id and s.alive and (target is None or s.target == target)
    ]
    if not matches:
        return None
    return max(matches, key=lambda s: s.created_at)


async def close_for_chat(chat_id: str, *, reason: str = "superseded") -> int:
    """otodock-CLI take-over: close any LIVE session(s) for ``chat_id`` so a new
    spawn/resume of the same chat is the single writer. The close fires each
    session's ``_on_exit`` → for an otodock session that feeds an EXIT to the old
    local terminal (it restores its tty + exits); a dashboard viewer sees the PTY
    close. Returns how many were closed."""
    if not chat_id:
        return 0
    victims = [s for s in list(_sessions.values()) if s.chat_id == chat_id and s.alive]
    for s in victims:
        try:
            await s.close(reason=reason)
        except Exception:
            logger.exception("close_for_chat: failed to close %s", s.session_id[:8])
    return len(victims)


def live_session_ids(local_only: bool = False) -> set[str]:
    """Session ids with a live PTY — consumed by concurrency.reconcile_chat_slots
    so interactive slots aren't released as orphans. With ``local_only``,
    excludes remote (satellite) sessions: those belong to a satellite budget, not
    the local ceiling G, so they must not appear in the local reconciler's live set."""
    return {sid for sid, s in _sessions.items()
            if s.alive and (not local_only or not s.is_remote)}


def live_sessions() -> list[InteractiveSession]:
    """The live terminals (``services.delegation.lane_status`` reads which
    of them work now)."""
    return [s for s in list(_sessions.values()) if s.alive]


async def close_session(session_id: str, *, reason: str = "closed",
                        drop_queued: bool = False) -> bool:
    """Close a registered interactive session by id. Returns True if it existed.

    ``drop_queued`` discards the session's queued server prompts instead of
    handing them back to the delivery ladder (a closer acting on a person who
    lost access must not re-deliver what was queued under their role)."""
    session = _sessions.get(session_id)
    if session is None:
        return False
    if drop_queued:
        session._prompt_queue.clear()
    await session.close(reason=reason)
    return True


async def close_all(reason: str = "shutdown", *, local_only: bool = False,
                    timeout: float | None = None) -> None:
    """Close every terminal: one after another, or with a ``timeout`` side
    by side within it (the platform's stop, which cannot wait out each
    close in turn). ``local_only``: the terminals this host runs (a machine
    keeps its own). Each close tails its transcript into its chat's rows
    first."""
    async def _one(session: InteractiveSession) -> None:
        try:
            await session.close(reason=reason)
        except Exception:
            logger.exception("interactive %s: close_all failed", session.session_id[:8])

    sessions = [s for s in list(_sessions.values()) if not (local_only and s.is_remote)]
    if timeout is None:
        for session in sessions:
            await _one(session)
    elif sessions:
        _done, late = await asyncio.wait([asyncio.ensure_future(_one(s)) for s in sessions],
                                         timeout=timeout)
        if late:
            logger.warning("interactive: %d terminal(s) did not close in %.0fs", len(late), timeout)


# ---------------------------------------------------------------------------
# Idle reaper
# ---------------------------------------------------------------------------

def reap_spare_reason(session: InteractiveSession, timeout_s: float) -> str:
    """Why an over-timeout session must NOT be reaped, or "".

    * ``question-parked`` — the turn is parked on an unanswered
      AskUserQuestion: idle by every byte measure, but waiting on a person.
      Spared like a held prompt: until the session's token ends less
      ``_TOKEN_MARGIN_S``, at most ``session_state.PROMPT_WAIT_S`` idle (see
      the constant).
    * ``prompt-pending``: a prompt the platform holds for this session
      waits on a person (``session_state.has_pending_prompt``), as the
      headless reapers spare it; the prompt's own wait bounds it.
    * ``turn-open`` — mid-turn work with no viewer: a byte-quiet stretch of a
      long tool call can outlast the idle window. Capped at the per-TURN
      ceiling (``config.get_session_timeout()``): a turn cannot legitimately
      outlive it, so past that the flag is stuck and must not confer immortality.
    * ``hook-activity`` — tool hooks are still firing for this session id even
      though the PTY output stalled (satellite WS-down frames are dropped
      silently), so the CLI is demonstrably working. Session-id-keyed, same
      signal the headless reapers consult. Self-limiting: it needs hooks to keep
      firing inside the window.
    * ``background-pending``: the terminal's last Stop listed background work
      the agent started (``note_background_report``): a command or a
      sub-agent still running wakes the terminal when it ends. Counted from
      the last hook (the Stop itself, or the work's own tool calls), up to
      the ceiling the headless path uses (``BACKGROUND_WORK_CEILING_S``):
      a job that never ends, or a wake that never came, holds it no longer.
    """
    from core.session import background_leash, session_state
    if (session.question_parked
            and session.idle_seconds < session_state.PROMPT_WAIT_S
            and time.time() < session.token_expires_at - _TOKEN_MARGIN_S):
        return "question-parked"
    if session_state.has_pending_prompt(session.session_id):
        return "prompt-pending"
    if session.turn_open and session.idle_seconds < config.get_session_timeout():
        return "turn-open"
    last_hook = session_state.get_hook_activity(session.session_id)
    if last_hook and time.monotonic() - last_hook < timeout_s:
        return "hook-activity"
    if (session._background_reported and last_hook
            and time.monotonic() - last_hook <= background_leash.background_work_ceiling()):
        return "background-pending"
    return ""


def background_reported(session: InteractiveSession) -> bool:
    """The terminal's last Stop listed background work, inside its ceiling."""
    return reap_spare_reason(session, 0.0) == "background-pending"


def note_background_report(session_id: str, kinds: list[str]) -> None:
    """A terminal's Stop listed the background work still in flight
    (``/v1/hooks/stop``, the kinds only; Claude's Stop input carries the
    list, Codex's none, so a Codex terminal never reports): keep or clear
    its ``background-pending`` spare. A headless session is left alone (its
    registries hold its work)."""
    session = _sessions.get(session_id)
    if session is None:
        return
    from core.session import background_leash
    if kinds:
        logger.info("interactive %s: its Stop lists background work %r",
                    session_id[:8], kinds)
    session._background_reported = background_leash.names_terminal_work(kinds)


def held_on_machine(machine_id: str, timeout_s: float) -> InteractiveSession | None:
    """The most idle terminal on this machine that only its background report
    keeps: nobody has it open, and without the report the reaper would have
    closed it (``timeout_s`` is the idle timeout, not the eviction floor).
    A machine at its session ceiling takes it back (the remote layer's
    ``_evict_idle_on_machine``): a machine has no evictor for terminals, and a
    stale report must not refuse someone's new chat for longer than an idle
    timeout. Under that timeout it stays, as a plain idle terminal does."""
    held = [s for s in _sessions.values()
            if s.target == machine_id and s.alive and not s.closing
            and not s.has_viewer and not s.otodock_attached
            and s.idle_seconds > timeout_s
            and reap_spare_reason(s, timeout_s) == "background-pending"]
    return max(held, key=lambda s: s.idle_seconds, default=None)


async def reap_idle(timeout_s: float | None = None) -> int:
    """Close sessions with no viewer and no activity for ``timeout_s``.

    ``timeout_s`` defaults to the platform-wide admin idle timeout (the
    cached off-loop read): ONE knob shared with the headless reapers.
    A viewer attached keeps a session alive; input, prompts, turn signals
    and output inside an open turn reset its age, so a long unviewed agent
    turn is never killed mid-flight while a redraw between turns is no
    activity. An on-screen or reconnect-grace terminal is also spared below
    (don't kill visible state — that is NOT a longer timeout). A parked question / open turn / recent hook
    activity / reported background work also spares (``reap_spare_reason``),
    logged once per stretch over the timeout. Returns the count reaped.
    """
    if timeout_s is None:
        from core.session import session_state as _state
        timeout_s = await _state.cached_idle_timeout()
    reaped = 0
    for session in list(_sessions.values()):
        if session.has_viewer or session.otodock_attached or not session.alive:
            session._last_reap_spare = None
            continue
        # A remote session whose satellite is mid-reconnect is held in
        # PTY-grace — still `alive` but transiently unviewable. Don't reap it; the
        # grace timer (or its reconcile on reconnect) decides its fate.
        if session.is_remote:
            from core.remote.satellite_connection import get_connection_manager
            if get_connection_manager().is_pty_in_grace(session.target):
                continue
        if session.idle_seconds > timeout_s:
            spare = reap_spare_reason(session, timeout_s)
            if spare:
                if spare != session._last_reap_spare:
                    session._last_reap_spare = spare
                    logger.info(
                        "interactive %s: sparing idle session (%s, %.0fs idle)",
                        session.session_id[:8], spare, session.idle_seconds,
                    )
                continue
            logger.info(
                "interactive %s: reaping idle (%.0fs, no viewer%s)",
                session.session_id[:8], session.idle_seconds,
                ", its background work outlived the ceiling" if session._background_reported else "",
            )
            await session.close(reason="idle")
            reaped += 1
        else:
            session._last_reap_spare = None
    return reaped


async def tail_live_sessions() -> None:
    """Persist each live session's transcript incrementally (idempotent via the
    tailer's line cursor) so a proxy crash between turns can't lose history. The
    close()-time tail covers normal teardown; this covers the crash gap."""
    for session in list(_sessions.values()):
        if not session.alive:
            continue
        try:
            tailer = session._tailer()
            _r = await asyncio.to_thread(
                tailer.resolve_and_tail, session.session_id, session.chat_id
            )
            # A turn end the post-output debounce missed (busy loop) still
            # completes, and an engine's ending still acts.
            session._fold_tail_result(_r, effects=False)
        except Exception:
            logger.exception(
                "interactive %s: periodic transcript tail failed", session.session_id[:8]
            )


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(_REAPER_PERIOD_S)
        try:
            await tail_live_sessions()
            await reap_idle()
        except Exception:
            logger.exception("interactive idle reaper error")


def start_idle_reaper() -> None:
    """Start the idle-reaper background task (call once at app startup)."""
    global _reaper_task
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = asyncio.get_event_loop().create_task(_reaper_loop())
