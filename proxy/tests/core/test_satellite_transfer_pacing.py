"""A push over a slow uplink keeps the satellite's keepalive alive.

The real reproduction of the drop: uvicorn with the bounded protocol runs a
real ``SatelliteConnectionManager`` writer and ``push_file``, a TCP relay
forwards the server's bytes to the client at a fixed slow rate, and a
websockets client (the satellite) pings every 0.5 s and gives the pong 2 s.
Every byte the proxy hands over ahead of a pong delays it, so a push that
hands over more than the link carries in 2 s fails the client's keepalive.
The bulk credit bounds those bytes. Scaled: 128 KiB of credit and 64 KiB
chunks against a 2 s pong here, 1 MiB and 512 KiB against 30 s in the
field. Without the credit the whole file is handed over at once and the
client fails with "keepalive ping timeout" within seconds.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import socket
import threading
import time

import pytest
import uvicorn
import websockets

RATE = 256 * 1024          # bytes per second, server to client
SIZE = 1024 * 1024         # the pushed file
STATE: dict = {}


class _AsgiWs:
    def __init__(self, send):
        self._send = send

    async def send_text(self, text: str) -> None:
        await self._send({"type": "websocket.send", "text": text})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._send({"type": "websocket.close", "code": code})


async def _app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            await send({"type": msg["type"] + ".complete"})
            if msg["type"] == "lifespan.shutdown":
                return
    if scope["type"] != "websocket":
        return
    from core.remote.satellite_connection import SatelliteConnection, SatelliteConnectionManager
    from services.path_policy_v2 import PathRef

    await receive()
    await send({"type": "websocket.accept"})
    mgr = SatelliteConnectionManager()

    async def _no_deregister(*_a, **_k):
        STATE["deregistered"] = True

    mgr.deregister = _no_deregister
    conn = SatelliteConnection(machine_id="m1", ws=_AsgiWs(send))
    mgr._connections["m1"] = conn
    conn.writer_task = asyncio.create_task(mgr._writer_loop(conn))

    async def _push():
        t0 = time.monotonic()
        STATE["ok"] = await mgr.push_file(
            "m1", PathRef("agent_tree", "workspace/big.bin"), STATE["data"], agent_slug="a1",
        )
        STATE["push_s"] = time.monotonic() - t0
        STATE["inflight_after"] = getattr(getattr(conn, "bulk_credit", None), "inflight", 0)

    pusher = asyncio.create_task(_push())
    try:
        while True:
            msg = await receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("text"):
                await mgr.handle_message("m1", json.loads(msg["text"]))
    finally:
        # The satellite closes right behind its last ack: let the push see it.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(pusher), 5)
        pusher.cancel()
        conn.writer_task.cancel()


@pytest.fixture
def server():
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    from app import _BoundedWebSocketProtocol
    cfg = uvicorn.Config(_app, ws=_BoundedWebSocketProtocol, log_level="error",
                         lifespan="on", ws_ping_interval=None)
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


async def _relay(upstream_port: int) -> tuple[asyncio.Server, int]:
    """Client to server at full speed, server to client at RATE."""

    async def _handle(c_reader, c_writer):
        u_reader, u_writer = await asyncio.open_connection("127.0.0.1", upstream_port, limit=16384)
        u_sock = u_writer.get_extra_info("socket")
        u_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)

        async def _up():
            while data := await c_reader.read(65536):
                u_writer.write(data)
                await u_writer.drain()
            u_writer.close()

        async def _down():
            slice_s = 0.02
            while data := await u_reader.read(int(RATE * slice_s)):
                c_writer.write(data)
                await c_writer.drain()
                await asyncio.sleep(len(data) / RATE)
            c_writer.close()

        await asyncio.gather(_up(), _down(), return_exceptions=True)

    srv = await asyncio.start_server(_handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def _satellite(port: int) -> dict:
    """Ack every frame that carries a command_id, rebuild the file as a
    0.5.76 satellite does (index 0 truncates, the final hash commits)."""
    out = {"partial": b"", "committed": None, "error": None}
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/", ping_interval=0.5, ping_timeout=2,
        max_size=16 * 1024 * 1024, compression=None,
    ) as ws:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "file_push":
                    continue
                if msg["action"] == "write":
                    out["committed"] = base64.b64decode(msg["content_b64"])
                elif msg["action"] == "write_chunk":
                    block = base64.b64decode(msg["content_b64"])
                    out["partial"] = (b"" if msg["chunk_index"] == 0 else out["partial"]) + block
                    if msg.get("hash"):
                        assert msg["hash"] == "sha256:" + hashlib.sha256(out["partial"]).hexdigest()
                        out["committed"] = out["partial"]
                if msg.get("command_id"):
                    await ws.send(json.dumps({"type": "ack", "command_id": msg["command_id"],
                                              "status": "ok", "error": ""}))
                if out["committed"] is not None:
                    return out
        except websockets.ConnectionClosed as e:
            out["error"] = f"{e.rcvd.code if e.rcvd else e.sent.code if e.sent else '?'} {e}"
    return out


@pytest.mark.slow
def test_a_push_on_a_slow_uplink_keeps_the_keepalive_and_commits(server, monkeypatch):
    import config
    from core.remote import file_sync, satellite_file_transfer as sft
    monkeypatch.setattr(config, "BULK_CREDIT_BYTES", 128 * 1024, raising=False)
    monkeypatch.setattr(sft, "PUSH_SLOW_CHUNK_BYTES", 32 * 1024)
    monkeypatch.setattr(file_sync, "MAX_CHUNK_SIZE", 64 * 1024)
    STATE.clear()
    STATE["data"] = os.urandom(SIZE)

    async def _run():
        relay, rport = await _relay(server)
        async with relay:
            return await asyncio.wait_for(_satellite(rport), timeout=30)

    out = asyncio.run(_run())
    assert out["error"] is None, out["error"]
    assert out["committed"] == STATE["data"]
    deadline = time.monotonic() + 5
    while "ok" not in STATE and time.monotonic() < deadline:
        time.sleep(0.02)
    assert STATE.get("ok") is True
    assert STATE.get("inflight_after") == 0
