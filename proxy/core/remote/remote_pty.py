"""Proxy-side handle for an interactive PTY running on a remote satellite.

This mirrors :class:`core.sandbox.pty_relay.PtyProcess`'s interface (``write`` /
``resize`` / ``close`` / ``terminate`` / ``scrollback`` / ``closed`` / ``pid``)
so :class:`core.session.interactive_session.InteractiveSession` drives a REMOTE PTY
exactly like a local one. Everything above the PtyProcess seam — the readiness
gate, cold-submit, the scrollback replay, the dashboard viewer (``_attach_pty_
viewer``), the permission drainer — is unchanged; only the transport differs.

Instead of an OS master fd, a ``RemotePtyProcess`` talks to the satellite over
the EXISTING satellite WebSocket (no new connection):

    proxy → satellite
      pty_open    {session_id, agent_slug, execution_path, config, rows, cols}
                  (sent via send_command → ack carries the satellite pid)
      pty_input   {session_id, data_b64}
      pty_resize  {session_id, rows, cols}
      pty_close   {session_id}
    satellite → proxy
      pty_output  {session_id, data_b64}   → feed_output(...)
      pty_exit    {session_id, code}        → feed_exit(...)

Incoming ``pty_output`` / ``pty_exit`` frames are routed by
``core.remote.satellite_connection.SatelliteConnectionManager.handle_message`` to the
``RemotePtyProcess`` registered under ``(machine_id, session_id)`` (see
``feed_output`` / ``feed_exit`` below).

This is the dumb-pipe design: the satellite owns only the raw
PTY; ALL the interactive intelligence stays on the proxy.

Scope notes:
  * Scrollback is buffered HERE on the proxy (fed by ``pty_output``). It survives
    a dashboard reconnect but NOT a satellite reconnect — a satellite-side replay
    is a future enhancement.
  * Output is lossless only as far as the satellite's send queue + WS/TCP
    backpressure allow; a dedicated lossless lane is a future enhancement.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from collections import deque
from collections.abc import Iterable

from core.sandbox.pty_relay import DEFAULT_SCROLLBACK_BYTES, ExitCb, OutputCb

logger = logging.getLogger("otodock.remote_pty")

# The dashboard xterm is a MIRROR, not the controlling terminal — the satellite
# PTY is. Terminal-QUERY sequences in the output (DA/DSR, DECRQM, kitty probe,
# XTVERSION, XTWINOPS reports, OSC color/clipboard probes, DCS requests) are
# stripped (live + scrollback) so the mirror has nothing to answer: a mirror
# reply (e.g. Primary DA `\x1b[?1;2c`) rides `pty_input` back into the app's
# composer as stray bytes, and because the proxy REPLAYS the scrollback to every
# (re)attaching viewer, the mirror re-answers each buffered query on every
# chat-switch (the Codex `[?1;2c` composer-fill; the Windows ConPTY emits a DA
# query on its OUTER side, Unix PTYs don't). The app's real capability handshake
# is still served by the satellite PTY. Vocabulary + rationale live in
# ``core.terminal_queries`` (shared with the input-side strip and mirrored to
# the satellite's replay sanitizer).
from core.background import spawn
from core.terminal_queries import (
    PasteModeTracker,
    strip_queries as _strip_terminal_queries,
)

# (machine_id, session_id) -> RemotePtyProcess. The single routing table for
# inbound pty_output / pty_exit frames. An entry is added on construction and
# removed when the handle's exit fires (satellite-reported or local close).
_remote_ptys: dict[tuple[str, str], RemotePtyProcess] = {}


def feed_output(machine_id: str, session_id: str, data: bytes) -> None:
    """Route a satellite ``pty_output`` frame to its RemotePtyProcess."""
    rp = _remote_ptys.get((machine_id, session_id))
    if rp is not None:
        rp._feed_output(data)


def feed_exit(machine_id: str, session_id: str, code: int | None) -> None:
    """Route a satellite ``pty_exit`` frame to its RemotePtyProcess."""
    rp = _remote_ptys.get((machine_id, session_id))
    if rp is not None:
        rp._feed_exit(code)


def cancel_machine_ptys(machine_id: str) -> None:
    """Fire-exit every remote PTY on a machine (WS deregister / reconnect).

    Mirrors ``SatelliteHttpTunnelDispatcher.cancel_machine_streams`` — when a
    satellite drops, its in-flight remote PTYs are dead; surface that as an exit
    so the InteractiveSession tears down (dashboard shows "session ended") rather
    than hanging on a stream that will never resume. (A future grace-window
    re-adopt is a planned enhancement.)
    """
    for (mid, _sid), rp in list(_remote_ptys.items()):
        if mid == machine_id:
            rp._feed_exit(None)


def has_machine_ptys(machine_id: str) -> bool:
    """True if any remote PTY handle is currently held for ``machine_id``.

    Used by ``satellite_connection.deregister`` to decide whether a WS drop needs
    a PTY reconnect-grace window instead of an immediate kill.
    """
    return any(mid == machine_id for (mid, _sid) in _remote_ptys)


def machine_pty_session_ids(machine_id: str) -> set[str]:
    """Session ids of every remote PTY currently held for ``machine_id``."""
    return {sid for (mid, sid) in _remote_ptys if mid == machine_id}


def reconcile_machine_ptys(
    machine_id: str, alive_session_ids: Iterable[str],
) -> tuple[list[str], list[str], list[str]]:
    """Reconcile proxy PTY handles against the satellite's live set on reconnect.

    The satellite reports its still-alive interactive PTY session ids right after
    (re)auth (the ``pty_alive`` frame). For ``machine_id``:

      * **re-adopt** handles whose session is still alive on the satellite — keep
        them (the registry already routes their replayed output; the viewer
        re-syncs size + does a clean reset+replay on the `reconnected` status,
        so no resize/repaint kick is needed here);
      * **exit** handles whose session is GONE on the satellite (its child died
        during the disconnect) → fire ``on_exit`` → InteractiveSession teardown +
        resume-on-send;
      * report **orphans** — sessions alive on the satellite with NO proxy handle
        (we grace-expired it / the chat was closed / the proxy restarted) — so the
        caller sends ``pty_close`` to reap the satellite child (no reverse-orphan).

    Returns ``(readopted, exited, orphans)`` lists of session ids.
    """
    alive = set(alive_session_ids)
    held = {sid for (mid, sid) in list(_remote_ptys) if mid == machine_id}
    readopted = sorted(held & alive)
    exited = sorted(held - alive)
    orphans = sorted(alive - held)
    # Re-adopted handles are kept as-is. The viewer re-syncs size + does a clean
    # reset+replay on the `reconnected` status (refresh-on-reconnect),
    # so there's no resize kick here (it would only add a competing repaint).
    for sid in exited:
        rp = _remote_ptys.get((machine_id, sid))
        if rp is not None:
            rp._feed_exit(None)
    return readopted, exited, orphans


class RemotePtyProcess:
    """A satellite-hosted PTY, driven over the WS with the PtyProcess interface.

    Construct via :func:`spawn_remote_pty` (which sends ``pty_open`` and waits for
    the ack). The handle is registered in ``_remote_ptys`` for inbound routing
    from its constructor, so output that races the ack is not lost.
    """

    def __init__(
        self,
        *,
        machine_id: str,
        session_id: str,
        rows: int,
        cols: int,
        on_output: OutputCb,
        on_exit: ExitCb | None,
        scrollback_limit: int,
    ) -> None:
        self.machine_id = machine_id
        self.session_id = session_id
        # Satellite-reported pid (filled from the pty_open ack) — for log parity
        # with PtyProcess.pid; the proxy never signals it directly.
        self.pid: int | None = None
        self.rows = rows
        self.cols = cols
        self._on_output = on_output
        self._on_exit = on_exit
        self._scrollback_limit = scrollback_limit
        self._scrollback: deque[bytes] = deque()
        self._scrollback_len = 0
        self._paste_mode = PasteModeTracker()
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._exit_fired = False
        _remote_ptys[(machine_id, session_id)] = self

    # -- output: satellite -> proxy (fed by handle_message) -------------------
    def _feed_output(self, data: bytes) -> None:
        if self._closed or not data:
            return
        # Drop terminal-query sequences so the mirror xterm never answers them
        # (the satellite PTY is the real terminal) — prevents the DA-response
        # (`\x1b[?1;2c`) cursor-jump loop on Windows ConPTY + scrollback replay.
        data = _strip_terminal_queries(data)
        if not data:
            return
        self._remember(data)
        try:
            out = self._on_output(data)
            if asyncio.iscoroutine(out):
                spawn(out, name="output", owner="remote_pty")
        except Exception:
            logger.exception("remote pty %s: on_output failed", self.session_id[:8])

    def _feed_exit(self, code: int | None) -> None:
        # The satellite reports the PTY process ended (or the machine dropped).
        self._fire_exit(code)

    def _remember(self, data: bytes) -> None:
        self._paste_mode.feed(data)
        self._scrollback.append(data)
        self._scrollback_len += len(data)
        while self._scrollback_len > self._scrollback_limit and self._scrollback:
            self._scrollback_len -= len(self._scrollback.popleft())
            self._paste_mode.trimmed = True

    def scrollback(self) -> bytes:
        """Recent rendered output, for replay to a (re)attaching viewer. Once the ring has dropped
        the program's bracketed-paste toggle, the mode it holds comes first
        (``terminal_queries.PasteModeTracker``): a fresh mirror would
        otherwise paste unbracketed and the TUI submit the first line."""
        return self._paste_mode.replay_prefix() + b"".join(self._scrollback)

    # -- input: proxy -> satellite --------------------------------------------
    def write(self, data: bytes) -> None:
        """Write raw bytes (keystrokes) to the remote PTY's stdin."""
        if self._closed or not data:
            return
        self._send_ff({
            "type": "pty_input",
            "session_id": self.session_id,
            "data_b64": base64.b64encode(data).decode(),
        })

    def resize(self, rows: int, cols: int) -> None:
        """Relay a client window resize to the remote PTY (SIGWINCH there)."""
        if self._closed:
            return
        self.rows, self.cols = rows, cols
        self._send_ff({
            "type": "pty_resize",
            "session_id": self.session_id,
            "rows": rows,
            "cols": cols,
        })

    # -- lifecycle ------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def terminate(self) -> None:
        """Public kill — lease takeover, mode toggle, or idle reap."""
        self.close(signal_child=True)

    def close(self, *, signal_child: bool = True) -> None:
        """Stop the remote PTY and fire ``on_exit``. Idempotent.

        ``signal_child=False`` when the child already exited (the satellite's
        ``pty_exit`` arrived first) — then we don't send a redundant pty_close.
        """
        if self._closed:
            return
        self._closed = True
        if signal_child:
            self._send_ff({"type": "pty_close", "session_id": self.session_id})
        # Mirror PtyProcess.close → on_exit. The satellite's pty_exit (if it
        # still arrives) is deduped by the _exit_fired guard in _fire_exit.
        self._fire_exit(None)

    def _fire_exit(self, code: int | None) -> None:
        _remote_ptys.pop((self.machine_id, self.session_id), None)
        self._closed = True
        if self._exit_fired:
            return
        self._exit_fired = True
        if self._on_exit is not None:
            try:
                res = self._on_exit(code)
                if asyncio.iscoroutine(res):
                    spawn(res, name="exit", owner="remote_pty")
            except Exception:
                logger.exception("remote pty %s: on_exit failed", self.session_id[:8])

    # -- transport ------------------------------------------------------------
    def _send_ff(self, msg: dict) -> None:
        """Fire-and-forget a frame to the satellite (off the caller's stack).

        ``write``/``resize``/``close`` are SYNC (the PtyProcess contract), but
        the manager's send is async — schedule it on the loop. Frames are tiny
        and ordered within the writer's control lane.
        """
        from core.remote.satellite_connection import get_connection_manager
        mgr = get_connection_manager()
        spawn(mgr.send_fire_and_forget(self.machine_id, msg),
              name="send_fire_and_forget", owner="remote_pty")


async def spawn_remote_pty(
    *,
    machine_id: str,
    session_id: str,
    agent_slug: str,
    execution_path: str,
    config_payload: dict,
    rows: int,
    cols: int,
    on_output: OutputCb,
    on_exit: ExitCb | None = None,
    scrollback_limit: int = DEFAULT_SCROLLBACK_BYTES,
    timeout: float = 60.0,
) -> RemotePtyProcess:
    """Open an interactive PTY on ``machine_id`` and return a loop-integrated handle.

    Sends ``pty_open`` (the interactive analogue of ``start_session``) carrying
    the SAME ``config_payload`` the satellite uses for the ``-p`` path
    (``RemoteExecutionLayer._build_start_payload``); the satellite assembles the
    interactive argv (no ``-p``/stream-json, plus ``TERM``) and spawns it under a
    PTY. Waits for the ack (carries the satellite pid). Raises ``RuntimeError`` on
    ack error / timeout (the warmup then fails cleanly, same as the ``-p`` path).
    """
    rp = RemotePtyProcess(
        machine_id=machine_id,
        session_id=session_id,
        rows=rows,
        cols=cols,
        on_output=on_output,
        on_exit=on_exit,
        scrollback_limit=scrollback_limit,
    )
    from core.remote.satellite_connection import get_connection_manager
    mgr = get_connection_manager()
    try:
        ack = await mgr.send_command(machine_id, {
            "type": "pty_open",
            "session_id": session_id,
            "agent_slug": agent_slug,
            "execution_path": execution_path,
            "config": config_payload,
            "rows": rows,
            "cols": cols,
        }, timeout=timeout)
    except Exception:
        _remote_ptys.pop((machine_id, session_id), None)
        raise
    rp.pid = ack.get("pid")
    logger.info(
        "remote pty spawned machine=%s session=%s pid=%s (%dx%d)",
        machine_id[:8], session_id[:8], rp.pid, cols, rows,
    )
    return rp
