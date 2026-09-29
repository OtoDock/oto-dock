"""Process limits the proxy sets for itself (startup.py, app.py): the open-file
limit, the default executor, and the HTTP header deadline.

At the default soft limit of 1024 descriptors about a thousand idle sockets, or
a few dozen Direct-LLM sessions, make the proxy silently drop every new
connection, and without a header deadline uvicorn holds a socket that never
sends a byte forever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import socket
import subprocess
import threading
import time

import psycopg
import pytest
import uvicorn

import config
import startup


# ---------------------------------------------------------------------------
# The open-file limit
# ---------------------------------------------------------------------------

def _fake_limits(monkeypatch, soft, hard):
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda _r: (soft, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda _r, lim: calls.append(lim))
    return calls


def test_nofile_is_raised_to_65536(monkeypatch, caplog):
    calls = _fake_limits(monkeypatch, 1024, 524288)
    caplog.set_level(logging.INFO)
    assert startup.raise_nofile_limit() == 65536
    assert calls == [(65536, 524288)]
    assert "65536" in caplog.text


def test_nofile_is_never_lowered(monkeypatch):
    calls = _fake_limits(monkeypatch, 1048576, 1048576)
    assert startup.raise_nofile_limit() == 1048576
    assert calls == []


def test_a_low_hard_limit_warns(monkeypatch, caplog):
    calls = _fake_limits(monkeypatch, 1024, 4096)
    caplog.set_level(logging.INFO)
    assert startup.raise_nofile_limit() == 4096
    assert calls == [(4096, 4096)]
    assert any(r.levelno == logging.WARNING and "4096" in r.getMessage() for r in caplog.records)


def test_queries_and_spawns_work_above_fd_1024():
    """psycopg waits with poll, not select(): a connection whose descriptor
    is above 1024 must work, and so must a subprocess spawn."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 2048:
        pytest.skip("hard descriptor limit too low for this test")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 2048), hard))
    pipes = []
    try:
        while not pipes or pipes[-1][1] < 1100:
            pipes.append(os.pipe())
        with psycopg.connect(config.DATABASE_URL) as conn:
            assert conn.pgconn.socket > 1024
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert subprocess.run(["true"]).returncode == 0

        async def _spawn():
            proc = await asyncio.create_subprocess_exec("true")
            return await proc.wait()

        assert asyncio.run(_spawn()) == 0
    finally:
        for r, w in pipes:
            os.close(r)
            os.close(w)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# ---------------------------------------------------------------------------
# The default executor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_default_executor_is_sized_from_config(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_EXECUTOR_WORKERS", 7)
    loop = asyncio.get_running_loop()
    ex = startup.set_default_executor(loop)
    try:
        assert ex._max_workers == 7
        name = await asyncio.to_thread(lambda: threading.current_thread().name)
        assert name.startswith("asyncio")
    finally:
        ex.shutdown(wait=False)


# ---------------------------------------------------------------------------
# The header deadline
# ---------------------------------------------------------------------------

async def _app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            await send({"type": msg["type"] + ".complete"})
            if msg["type"] == "lifespan.shutdown":
                return
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        msg = await receive()
        await send({"type": "websocket.send", "text": "echo:" + msg.get("text", "")})
        await receive()
        return
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok:" + body})


@pytest.fixture
def server():
    from app import _HeaderDeadlineProtocol

    class _Quick(_HeaderDeadlineProtocol):
        header_deadline_s = 0.5

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    cfg = uvicorn.Config(_app, http=_Quick, ws="websockets-sansio", log_level="error",
                         timeout_keep_alive=5, lifespan="on")
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


def _closed_within(s: socket.socket, seconds: float) -> float | None:
    s.settimeout(seconds)
    t = time.monotonic()
    try:
        data = s.recv(1024)
    except TimeoutError:
        return None
    return time.monotonic() - t if data == b"" else None


def test_an_idle_socket_is_closed_at_the_deadline(server):
    s = socket.create_connection(("127.0.0.1", server))
    took = _closed_within(s, 3.0)
    assert took is not None and took < 1.5


def test_half_sent_headers_are_closed_at_the_deadline(server):
    s = socket.create_connection(("127.0.0.1", server))
    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
    took = _closed_within(s, 3.0)
    assert took is not None and took < 1.5


def _get(s: socket.socket, body: bytes = b"") -> bytes:
    s.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
    s.settimeout(3)
    return s.recv(4096)


def test_keep_alive_slow_body_and_websocket_are_unaffected(server):
    s = socket.create_connection(("127.0.0.1", server))
    assert b"200 OK" in _get(s, b"one")
    time.sleep(1.0)  # longer than the header deadline, shorter than keep-alive
    assert b"ok:two" in _get(s, b"two")

    slow = socket.create_connection(("127.0.0.1", server))
    slow.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\n")
    time.sleep(1.0)  # the body arrives after the deadline
    slow.sendall(b"late")
    slow.settimeout(3)
    assert b"ok:late" in slow.recv(4096)

    from websockets.sync.client import connect
    with connect(f"ws://127.0.0.1:{server}/") as ws:
        time.sleep(1.0)
        ws.send("hi")
        assert ws.recv(timeout=3) == "echo:hi"
