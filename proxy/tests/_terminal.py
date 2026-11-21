"""A terminal session for tests, on a stand-in PTY.

The platform starts no process: a terminal's PTY runs on a machine, and the
session (``core.session.interactive_session.InteractiveSession``) drives it
through a handle (``core.remote.remote_pty.RemotePtyProcess``). A test whose
subject is the session itself (its lease, its readiness gate, its prompt
queue, the idle reaper, the drainer) needs no machine: ``register`` builds
the session and registers it as a start does, on a ``StandInPty``::

    from tests._terminal import register

    s = await register(session_id="sid-1", chat_id="chat-1", agent_name="agent")
    s._mark_ready()
    s.write_input(b"ping\\n")      # comes back to the session's viewers
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from collections.abc import Callable
from typing import Any

from core import placement
from core.session import interactive_session as isess
from core.session.interactive_common import (
    DEFAULT_COLS,
    DEFAULT_ROWS,
    DEFAULT_SCROLLBACK_BYTES,
    _load_chat_row,
    _token_expires_at,
)
from storage.agents.schema import DEFAULT_EXECUTION_PATH

_pids = itertools.count(1001)


class StandInPty:
    """The seam a session drives its PTY through, with no machine behind it:
    ``write``, ``resize``, ``close``, ``terminate``, ``scrollback``,
    ``closed``, ``pid``, ``rows`` and ``cols``, ``exit_reason`` and
    ``open_token`` (``RemotePtyProcess``'s).

    It answers as a program that prints back what it reads. Every write
    comes back through ``on_output`` and stays in the ring ``scrollback``
    replays, as the bytes written (no terminal sits between: a carriage
    return stays one, an escape stays an escape, Ctrl-C signals nobody), on
    the loop's next pass and never inside the write: a session finishes the
    call that wrote before it hears its own echo. ``exit_code`` is a program
    that ends by itself right after its start, with that code. A close, a
    terminate or the program's own end reaches ``on_exit`` once: the code
    for the program's end, ``None`` for a close (a machine's handle answers
    a close before its PTY reports).
    """

    def __init__(
        self,
        *,
        on_output: Callable[[bytes], object],
        on_exit: Callable[[int | None], object] | None = None,
        rows: int = DEFAULT_ROWS,
        cols: int = DEFAULT_COLS,
        scrollback_limit: int = DEFAULT_SCROLLBACK_BYTES,
        exit_code: int | None = None,
    ) -> None:
        self.pid = next(_pids)
        self.rows = rows
        self.cols = cols
        self.exit_reason = ""
        self.open_token = ""
        self._on_output = on_output
        self._on_exit = on_exit
        self._scrollback_limit = scrollback_limit
        self._ring: deque[bytes] = deque()
        self._ring_len = 0
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._exit_fired = False
        if exit_code is not None:
            self._loop.call_soon(self._end, exit_code)

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        self._loop.call_soon(self._print, bytes(data))

    def _print(self, data: bytes) -> None:
        if self._closed:
            return
        self._ring.append(data)
        self._ring_len += len(data)
        while self._ring_len > self._scrollback_limit and self._ring:
            self._ring_len -= len(self._ring.popleft())
        self._on_output(data)

    def scrollback(self) -> bytes:
        return b"".join(self._ring)

    def resize(self, rows: int, cols: int) -> None:
        if not self._closed:
            self.rows, self.cols = rows, cols

    @property
    def closed(self) -> bool:
        return self._closed

    def terminate(self) -> None:
        self.close(signal_child=True)

    def close(self, *, signal_child: bool = True) -> None:
        if not self._closed:
            self._end(None)

    def _end(self, code: int | None) -> None:
        self._closed = True
        if self._exit_fired:
            return
        self._exit_fired = True
        if self._on_exit is not None:
            self._on_exit(code)


async def register(
    *,
    session_id: str,
    chat_id: str,
    agent_name: str,
    env: dict | None = None,
    rows: int = DEFAULT_ROWS,
    cols: int = DEFAULT_COLS,
    user_sub: str = "",
    role: str = "",
    username: str = "",
    target: str = placement.LOCAL,
    scrollback_limit: int = DEFAULT_SCROLLBACK_BYTES,
    execution_path: str = DEFAULT_EXECUTION_PATH,
    prompt_in_argv: bool = False,
    tui_theme: str = "dark",
    pty: Callable[..., Any] = StandInPty,
    payer: str | None = None,
    **program: Any,
) -> isess.InteractiveSession:
    """Register a terminal session under ``session_id`` on a stand-in PTY.

    The session is built as ``interactive_session.register_remote`` builds
    one (the chat's row read off the loop, the end of its token read from
    ``env``'s ``PROXY_API_KEY``, the one key of ``env`` a session reads) and
    goes through the same spine, ``_register_session``: the lease, the chat
    slot, the transcript seek, the registry, the drainer and the readiness
    gate. No machine is asked for its OS. ``target`` is the platform's own
    place unless given, so the session takes the branches of a terminal
    whose transcript the platform reads itself and whose ring is raw; a
    machine's id makes it that machine's.

    ``pty`` builds the handle and is called as ``spawn_remote_pty`` builds
    a ``RemotePtyProcess``: with ``rows``, ``cols``, ``on_output``,
    ``on_exit`` and ``scrollback_limit``, and with ``program``
    (``StandInPty``'s switches). A test with a PTY of its own passes its
    builder; one that raises fails the registration as a failed start does.
    ``payer`` is the account its config was built to pay with, as
    ``register_remote`` takes it (None: a session taken back after a
    restart).
    """
    session = isess.InteractiveSession(
        session_id=session_id, chat_id=chat_id, agent_name=agent_name,
        user_sub=user_sub, role=role, username=username, target=target,
        rows=rows, cols=cols,
        execution_path=execution_path, prompt_in_argv=prompt_in_argv,
        tui_theme=tui_theme,
        chat_row=await _load_chat_row(chat_id),
        token_expires_at=_token_expires_at(env),
        payer=payer,
    )

    async def _make_pty(s: isess.InteractiveSession) -> Any:
        return pty(rows=rows, cols=cols, on_output=s._fanout_output, on_exit=s._on_pty_exit,
                   scrollback_limit=scrollback_limit, **program)

    return await isess._register_session(session, pty_factory=_make_pty)
