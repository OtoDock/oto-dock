"""Reconnect-grace lifecycle for satellite connections (mixin).

When a satellite drops, its live sessions are held in a grace window so a quick
reconnect (restart / auto-update / network blip) resumes them instead of tearing
them down. Mixed into SatelliteConnectionManager; split out of
satellite_connection.py. `_GRACE_WINDOW_S` stays in satellite_connection (shared
with deregister + monkeypatched by tests) and is imported lazily where needed.
The lines a machine sends after a sign-in for what the platform does not hold
yet wait here too, bounded, for the report that takes them back (``HeldLines``).
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

logger = logging.getLogger("otodock.satellite")

# What a machine forwards for a terminal or a headless session the platform
# holds no entry for yet (after the platform's restart it sends what it read
# meanwhile, before its reports): kept per machine for the report that takes
# them back, until its report of headless sessions has been handled, or for
# ``HELD_LINES_S`` at most. The machine raises none of the bounds. A real
# restart brings a few sessions' lines of a few hundred characters: an entry
# keeps what a session's queue takes at its adoption (Claude Code's 1,000),
# a machine resends at most 10,000 frames after an outage and a frame may be
# 16 MiB, so 64 entries (the satellite's own ceiling of sessions), 10,000
# lines and 32 MiB a machine leave it room. A line past a bound is dropped.
HELD_LINES_S = 120.0
_HELD_ENTRIES = 64
_HELD_LINES_EACH = 1000
_HELD_LINES = 10_000
_HELD_SIZE = 32 * 1024 * 1024
TERMINAL = "terminal"
SESSION = "session"


@dataclass
class HeldLines:
    """One machine's held lines since its sign-in, by kind and id. What a
    take returns stays counted: the bounds hold for the whole window."""

    lines: dict[tuple[str, str], list] = field(default_factory=dict)
    count: int = 0
    size: int = 0
    ended: bool = False
    timer: asyncio.TimerHandle | None = None

    def hold(self, kind: str, entry_id: str, line: object, size: int) -> bool:
        """Keep ``line`` (``size`` characters); False when a bound refuses it
        or the window ended."""
        key = (kind, entry_id)
        mine = self.lines.get(key)
        if (self.ended or self.count >= _HELD_LINES or self.size + size > _HELD_SIZE
                or (len(self.lines) >= _HELD_ENTRIES if mine is None else len(mine) >= _HELD_LINES_EACH)):
            return False
        if mine is None:
            mine = self.lines[key] = []
        mine.append(line)
        self.count += 1
        self.size += size
        return True

    def take(self, kind: str, entry_id: str) -> list:
        return self.lines.pop((kind, entry_id), [])

    def end(self) -> None:
        self.ended = True
        self.lines.clear()
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


_GRACE_EXPIRED_MSG = (
    "⚠ stream interrupted — the remote machine disconnected and did not "
    "reconnect in time; output may be incomplete"
)



if TYPE_CHECKING:
    from core.remote.satellite_host import SatelliteManagerHost as _Host
else:
    _Host = object


def lost_frame(message: str, detail: str = "") -> dict:
    """The satellite protocol's ``error`` that ends a held turn as lost: the
    pump persists it as a visible block (``durable_marker``). ``detail``: the
    lost ending's own detail (``turn_ending.AWAY``), else the message is."""
    frame = {"type": "error", "message": message, "durable_marker": True}
    if detail:
        frame["detail"] = detail
    return frame


class SatelliteGraceMixin(_Host):
    async def _expire_grace(self, machine_id: str) -> None:
        """The reconnect window ran out with headless sessions held. The
        window ends only the waiting: the queues stay held, so a reconnect
        at any time re-adopts them (``register``), the turns' silence stays
        quiet (``is_session_in_grace``) and a send waits for the machine. An
        open turn ends at the turn ceiling by the stall watch, an idle session
        at the idle timeout by the reaper, both as on a connected machine.
        Before 2.0 this ended every held turn with the lost marker."""
        # _GRACE_WINDOW_S stays in satellite_connection (shared with deregister +
        # monkeypatched by tests) — read it live each call.
        from core.remote.satellite_connection import _GRACE_WINDOW_S
        try:
            await asyncio.sleep(_GRACE_WINDOW_S)
        except asyncio.CancelledError:
            return
        self._grace_timers.pop(machine_id, None)
        held = self._grace_sessions.get(machine_id)
        if held:
            logger.info(
                "Satellite %s: reconnect window ran out, %d held session(s) wait for it",
                machine_id[:8], len(held),
            )

    def end_held_sessions(self, machine_id: str) -> int:
        """End every session held for a machine with the lost marker (a
        machine removed): what the window's expiry did before 2.0."""
        held = self._grace_sessions.pop(machine_id, None)
        timer = self._grace_timers.pop(machine_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        for _session_id, (queue, _path) in (held or {}).items():
            try:
                queue.put_nowait(lost_frame(_GRACE_EXPIRED_MSG))
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        return len(held or {})

    async def _expire_pty_grace(self, machine_id: str) -> None:
        """The reconnect window ran out with terminals held: they wait for
        the machine (``remote_pty.machine_away``), their viewers keep the
        reconnecting banner, the idle reaper spares them, and the machine's
        report decides each at its return. The reaper's sweep ends them
        after a day. Before 2.0 this ended every held terminal."""
        from core.remote.satellite_connection import _GRACE_WINDOW_S
        try:
            await asyncio.sleep(_GRACE_WINDOW_S)
        except asyncio.CancelledError:
            return
        from core.remote import remote_pty
        self._pty_grace_timers.pop(machine_id, None)
        remote_pty.machine_away(machine_id)
        logger.info(
            "Satellite %s: reconnect window ran out, %d held terminal(s) wait for it",
            machine_id[:8], len(remote_pty.machine_pty_session_ids(machine_id)),
        )

    async def _reconcile_ptys(self, machine_id: str, alive_session_ids: list,
                              open_tokens: dict | None = None) -> None:
        """Reconcile our interactive PTY handles against the satellite's live set
        on reconnect, driven by the satellite's post-auth `pty_alive`.
        Re-adopts the still-alive ones (cancel the grace timer + repaint), exits
        the ones whose child died during the blip, and closes the orphans the
        satellite still runs but we no longer want (kills the reverse-orphan).
        A terminal the platform does not hold is first offered to the
        terminal claims (``satellite_link.add_terminal_claim``): one a claim
        took back is held by then and is no orphan."""
        from core.remote import remote_pty, satellite_link
        from core.session import interactive_session, session_state
        alive = {sid for sid in alive_session_ids if isinstance(sid, str)}
        # Each terminal's open as the machine names it (0.5.171): a terminal
        # taken back keeps it, an orphan's close names it.
        tokens = {sid: str(tok) for sid, tok in (open_tokens or {}).items()
                  if sid in alive and isinstance(tok, str)}
        remote_pty.note_reported_opens(machine_id, tokens)
        unheld = alive - remote_pty.machine_pty_session_ids(machine_id)
        claimed: set[str] = set()
        if unheld:
            claimed = await self._links.claim_terminals(machine_id, frozenset(unheld))
        # The lines the machine forwarded while the platform held no entry
        # for a terminal the report names reach it when a claim took it
        # back, and go otherwise.
        held = self._held_lines.get(machine_id)
        for sid in unheld | claimed:
            lines = held.take(TERMINAL, sid) if held is not None else []
            sess = interactive_session.get(sid) if sid in claimed else None
            if sess is not None and sess.target == machine_id and lines:
                sess.feed_transcript_lines(lines)
        timer = self._pty_grace_timers.pop(machine_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        remote_pty.machine_back(machine_id)
        # A held terminal the machine no longer runs, with a turn open: its
        # chat gets a record once its entry has closed (the hooks). Its facts
        # are read here, before the reconcile below fires its exit: the close
        # that exit starts forgets them.
        gone = remote_pty.machine_pty_session_ids(machine_id) - alive
        lost = []
        for sid in gone:
            sess = interactive_session.get(sid)
            if sess is not None and sess.chat_id and sess.turn_open:
                lost.append(satellite_link.LostTerminal(sid, sess.chat_id, session_state.session_facts(sid), sess))
        readopted, exited, orphans = remote_pty.reconcile_machine_ptys(
            machine_id, alive,
        )
        self._links.terminals_lost(machine_id, lost)
        # Side effects: viewer notifications + orphan reaping
        # (which sends a WS frame). `exited` already fired teardown inside.
        for sid in readopted:
            self._notify_pty_status(sid, "reconnected")
        for sid in orphans:
            frame = {"type": "pty_close", "session_id": sid}
            if tokens.get(sid):
                frame["open_token"] = tokens[sid]
            await self.send_fire_and_forget(machine_id, frame)
        if readopted or exited or orphans:
            logger.info(
                "Satellite %s PTY reconcile: re-adopted %d, exited %d, "
                "closed %d orphan(s)", machine_id[:8],
                len(readopted), len(exited), len(orphans),
            )

    def is_pty_in_grace(self, machine_id: str) -> bool:
        """True while a machine's terminals wait for it: inside the reconnect
        window, and past it until the machine reports or the sweep ends them.
        The idle reaper skips them (still `alive` but unviewable) and a
        (re)attaching viewer shows "reconnecting". Per machine."""
        from core.remote import remote_pty
        return machine_id in self._pty_grace_timers or remote_pty.is_machine_away(machine_id)

    # The lines of terminals and headless sessions the platform holds no
    # entry for yet, one store per machine (``HeldLines``).

    def start_held_lines(self, machine_id: str) -> HeldLines:
        """A sign-in's window for the machine's held lines. What a replaced
        connection held for a report it never had stays for this one."""
        held = self._held_lines.get(machine_id)
        if held is None or held.ended:
            held = self._held_lines[machine_id] = HeldLines()
        elif held.timer is not None:
            held.timer.cancel()
        held.timer = asyncio.get_running_loop().call_later(HELD_LINES_S, held.end)
        return held

    def _held(self, machine_id: str) -> HeldLines:
        held = self._held_lines.get(machine_id)
        return held if held is not None else self.start_held_lines(machine_id)

    def hold_transcript_lines(self, machine_id: str, session_id: str, lines: list) -> None:
        held = self._held(machine_id)
        for line in lines:
            if isinstance(line, str) and not held.hold(TERMINAL, session_id, line, len(line)):
                return

    def hold_session_line(self, machine_id: str, session_id: str, event: object) -> None:
        """A headless session's line, kept only when the session's security
        context places it on this machine: the adoption takes back no other
        (``run_recovery._readopt_state``), and that index is in memory."""
        from core.session import session_state
        if (session_state.get_session_security(session_id) is None
                or session_state.placed_on(session_id) not in (machine_id, None)):
            return
        held = self._held(machine_id)
        if not held.ended:
            held.hold(SESSION, session_id, event, len(json.dumps(event)))

    def take_held_lines(self, machine_id: str, session_id: str) -> list:
        """The held lines of headless session ``session_id``, once."""
        held = self._held_lines.get(machine_id)
        return held.take(SESSION, session_id) if held is not None else []

    def end_held_lines(self, machine_id: str, conn: object) -> None:
        """The report of headless sessions of the sign-in on ``conn`` has been
        handled: every session taken back took its lines, the rest go."""
        held = self._held_lines.get(machine_id)
        if held is not None and self._connections.get(machine_id) is conn:
            held.end()

    def _notify_pty_status(self, session_id: str, state: str) -> None:
        """Fire the InteractiveSession's on_status (reconnecting/reconnected) for
        the dashboard viewer. No-op if the session is gone or has no viewer."""
        from core.session import interactive_session
        sess = interactive_session.get(session_id)
        if sess is not None:
            sess.notify_status(state)

    def is_session_in_grace(self, machine_id: str, session_id: str) -> bool:
        """True when a session's event queue is held in the reconnect-grace
        area (its satellite dropped but may reconnect within the window). The
        session is 'reconnecting', not severed — the dashboard reap must treat
        it as live (see is_session_stream_attached + the layer's
        session_idle_seconds, both of which special-case grace)."""
        held = self._grace_sessions.get(machine_id)
        return bool(held and session_id in held)

    def drop_grace_session(self, machine_id: str, session_id: str) -> None:
        """Drop a single session from the reconnect-grace area (the user
        aborted, or it was closed during grace) so a later reconnect does NOT
        re-adopt + resume an abandoned turn. Cancels the machine's grace timer
        once its last held session is gone."""
        held = self._grace_sessions.get(machine_id)
        if not held:
            return
        held.pop(session_id, None)
        if not held:
            self._grace_sessions.pop(machine_id, None)
            timer = self._grace_timers.pop(machine_id, None)
            if timer is not None and not timer.done():
                timer.cancel()
