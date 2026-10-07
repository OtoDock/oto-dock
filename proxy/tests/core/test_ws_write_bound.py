"""The bound on a WebSocket's write buffer (app.py, _BoundedWebSocketProtocol).

A reader that stops draining used to grow the transport's buffer without
limit while every send returned at once. Now a sender waits at the high
mark, a reader that drains nothing for the stall window is dropped, one
that drains anything at all is kept, and a close while paused returns at
once instead of waiting on the dead reader.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import threading
import time

import pytest
import uvicorn

import config

FRAME = "x" * 65536
STATE: dict = {}


async def _app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            await send({"type": msg["type"] + ".complete"})
            if msg["type"] == "lifespan.shutdown":
                return
    if scope["type"] != "websocket":
        return
    await receive()
    await send({"type": "websocket.accept"})
    STATE.update(sent=0, disconnected=False, close_took=None)

    async def flood():
        try:
            while True:
                await send({"type": "websocket.send", "text": FRAME})
                STATE["sent"] += 1
        except OSError:
            STATE["disconnected"] = True

    task = asyncio.create_task(flood())
    try:
        if scope["path"] == "/close-when-asked":
            while not STATE.get("close"):
                await asyncio.sleep(0.02)
            t0 = time.monotonic()
            await send({"type": "websocket.close"})
            STATE["close_took"] = time.monotonic() - t0
        await task
    finally:
        task.cancel()


@pytest.fixture
def server(monkeypatch):
    from app import _BoundedWebSocketProtocol

    monkeypatch.setattr(config, "WS_WRITE_BUFFER_MAX_BYTES", 128 * 1024)
    monkeypatch.setattr(config, "WS_WRITE_STALL_S", 2.0)
    STATE.clear()
    sock = socket.socket()
    # A small kernel send buffer (inherited by the accepted sockets): the
    # backlog a reader must drain before uvloop sees progress is then the
    # protocol's own buffer, not megabytes of kernel queue.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    cfg = uvicorn.Config(_app, ws=_BoundedWebSocketProtocol, log_level="error", lifespan="on")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        yield port
    finally:
        srv.should_exit = True
        t.join(5)
        sock.close()


def _ws_connect(port: int, path: str) -> socket.socket:
    """A raw handshake: the client reads only what the test makes it read.
    Its receive buffer is small so a trickle of reads reopens the TCP
    window at once (a large buffer defers the window update until half of
    it is free), which is what a slow reader on a saturated link does."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
    s.connect(("127.0.0.1", port))
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    s.settimeout(5)
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    assert b" 101 " in buf
    return s


def _wait_until(pred, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _sends_stalled(seconds: float = 0.5) -> bool:
    """True once the server's send count stops moving for ``seconds``."""
    deadline = time.monotonic() + 10
    last, since = -1, time.monotonic()
    while time.monotonic() < deadline:
        now = STATE.get("sent", 0)
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= seconds:
            return True
        time.sleep(0.02)
    return False


def test_a_reader_that_drains_nothing_is_dropped(server):
    s = _ws_connect(server, "/flood")
    assert _sends_stalled(), "the sender never paused"
    stalled_at = STATE["sent"]
    t0 = time.monotonic()
    assert _wait_until(lambda: STATE["disconnected"], 8.0)
    took = time.monotonic() - t0
    assert took < 5.0, took
    assert STATE["sent"] == stalled_at
    s.close()


def test_a_reader_that_keeps_draining_is_kept(server):
    s = _ws_connect(server, "/flood")
    assert _sends_stalled()
    # Three stall windows of steady reading. uvloop hands the whole backlog
    # to the kernel as one write and reports progress once the kernel took
    # it all, so a reader must drain a backlog per window to count as
    # alive: about 70 KiB/s at the shipped 4 MiB and 60 s.
    for _ in range(120):
        s.recv(65536)
        time.sleep(0.05)
    assert not STATE["disconnected"]
    assert STATE["sent"] > 0
    s.close()
    assert _wait_until(lambda: STATE["disconnected"], 5.0)


def test_a_close_while_paused_returns_at_once(server):
    s = _ws_connect(server, "/close-when-asked")
    assert _sends_stalled()
    STATE["close"] = True
    assert _wait_until(lambda: STATE.get("close_took") is not None, 5.0)
    assert STATE["close_took"] < 1.0, STATE["close_took"]
    assert _wait_until(lambda: STATE["disconnected"], 5.0)
    s.close()


def test_the_stall_check_aborts_a_closing_transport_that_cannot_drain():
    """The keepalive's pong timeout CLOSES a dead reader first, and
    uvloop's close waits for the buffer that reader never drains: the
    stall check must abort a closing transport, not stand aside for it."""
    from app import _BoundedWebSocketProtocol

    class _Transport:
        aborted = False

        def is_closing(self):
            return True

        def get_write_buffer_size(self):
            return 4096

        def abort(self):
            self.aborted = True

    proto = _BoundedWebSocketProtocol.__new__(_BoundedWebSocketProtocol)
    proto.writable = asyncio.Event()           # cleared: paused
    proto.transport = _Transport()
    proto.client = ("10.0.0.9", 51000)
    proto._paused_at_bytes = 4096
    proto._stall_timer = None
    proto._lost = False
    proto._stall_check()
    assert proto.transport.aborted
    # After the loss, nothing to abort.
    proto.transport = _Transport()
    proto._lost = True
    proto._stall_check()
    assert not proto.transport.aborted
