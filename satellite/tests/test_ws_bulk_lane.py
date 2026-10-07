"""The bulk lane (0.5.137): file pulls never crowd out control frames.

A pull used to read the whole file on the loop and queue every chunk at once
in the control FIFO, so a heartbeat, an ack or a hook call waited behind the
whole file on the machine's uplink. Now the chunks ride a bounded lane drained
after control and PTY, the producer waits for room, a drop stops the pulls and
purges their chunks, and the platform's ``file_pull_cancel`` stops one.
"""

import asyncio
import json
import os
import socket
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.asyncio


class _SlowWS:
    """A ws whose send takes a moment, as a slow uplink does."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, s):
        await asyncio.sleep(0.002)
        self.sent.append(json.loads(s))


def _client(sm=None, connected=True):
    from satellite.transport.ws_client import SatelliteWSClient
    c = SatelliteWSClient(types.SimpleNamespace(), sm or types.SimpleNamespace())
    if connected:
        c._ws = _SlowWS()
        c._authenticated = True
    return c


def _sm(tmp_path):
    from satellite.config import SatelliteConfig
    from satellite.sessions.session_manager import SessionManager
    cfg = SatelliteConfig(
        machine_id="m", machine_secret="s", platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=tmp_path / "agents", mcps_dir=tmp_path / "mcps",
    )
    return SessionManager(cfg)


def _file(tmp_path, chunks: int, chunk: int) -> None:
    d = tmp_path / "agents" / "a1" / "workspace"
    d.mkdir(parents=True, exist_ok=True)
    (d / "big.bin").write_bytes(os.urandom(chunks * chunk))


def _pull(rid="r1"):
    return {"type": "file_pull", "request_id": rid, "agent_slug": "a1",
            "path": "workspace/big.bin"}


async def test_a_heartbeat_goes_out_between_the_chunks_of_a_large_pull(tmp_path, monkeypatch):
    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "CHUNK_SIZE", 64)
    _file(tmp_path, 42, 64)
    sm = _sm(tmp_path)
    c = _client(sm)
    pull = asyncio.create_task(sm.file_pull(_pull(), c))
    for _ in range(100):             # the pull has started producing
        await asyncio.sleep(0.005)
        if not c._bulk_send_queue.empty():
            break
    await c.enqueue_send({"type": "heartbeat"})
    writer = asyncio.create_task(c._writer_loop())
    await asyncio.wait_for(pull, 5)
    while not c._lanes_empty():
        await asyncio.sleep(0.01)
    writer.cancel()
    types_ = [(m["type"], m.get("chunk_index")) for m in c._ws.sent]
    hb = types_.index(("heartbeat", None))
    assert hb < types_.index(("file_content", 3)), types_[:8]
    assert [i for t, i in types_ if t == "file_content"] == list(range(42))


async def test_the_bulk_lane_holds_at_most_its_bound(tmp_path, monkeypatch):
    from satellite.transport import file_sync, ws_client
    monkeypatch.setattr(file_sync, "CHUNK_SIZE", 64)
    _file(tmp_path, 20, 64)
    sm = _sm(tmp_path)
    c = _client(sm)
    pull = asyncio.create_task(sm.file_pull(_pull(), c))
    await asyncio.sleep(0.05)        # no writer: the producer parks on a full lane
    assert c._bulk_send_queue.qsize() == ws_client._BULK_LANE_SIZE
    assert not pull.done()
    writer = asyncio.create_task(c._writer_loop())
    await asyncio.wait_for(pull, 5)
    writer.cancel()


async def test_a_drop_stops_the_pulls_and_purges_bulk_but_keeps_control(tmp_path, monkeypatch):
    """What connect_forever's finally does on a drop: the parked producer is
    cancelled first, then the lane is purged, so no stale chunk reaches the
    next connection, while acks and heartbeats stay queued for it."""
    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "CHUNK_SIZE", 64)
    _file(tmp_path, 20, 64)
    sm = _sm(tmp_path)
    c = _client(sm)
    pull = asyncio.create_task(sm.file_pull(_pull(), c))
    await asyncio.sleep(0.05)
    await c.enqueue_send({"type": "ack", "command_id": "x"})
    c._ws = None
    c._authenticated = False
    assert sm.cancel_pulls() == 1
    while True:
        try:
            c._bulk_send_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    with pytest.raises(asyncio.CancelledError):
        await pull
    await asyncio.sleep(0.01)
    assert c._bulk_send_queue.empty()
    assert c._send_queue.qsize() == 1
    assert sm._pulls == {}
    # A pull asked for while disconnected produces nothing.
    assert await c.enqueue_bulk({"type": "file_content"}) is False


async def test_the_platforms_cancel_stops_a_pull(tmp_path, monkeypatch):
    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "CHUNK_SIZE", 64)
    _file(tmp_path, 20, 64)
    sm = _sm(tmp_path)
    c = _client(sm)
    pull = asyncio.create_task(sm.file_pull(_pull("r9"), c))
    await c.enqueue_bulk({"type": "file_content", "request_id": "other"})
    await asyncio.sleep(0.05)
    sm.file_pull_cancel({"type": "file_pull_cancel", "request_id": "r9"}, c)
    sm.file_pull_cancel({"type": "file_pull_cancel", "request_id": "unknown"}, c)
    with pytest.raises(asyncio.CancelledError):
        await pull
    assert sm._pulls == {}
    writer = asyncio.create_task(c._writer_loop())
    await asyncio.sleep(0.05)
    writer.cancel()
    # The cancelled pull's queued chunks went too: only the other frame left.
    assert [m["request_id"] for m in c._ws.sent] == ["other"]


async def test_the_message_loop_routes_the_cancel(tmp_path):
    sm = types.SimpleNamespace(seen=[])
    sm.file_pull_cancel = lambda msg, ws: sm.seen.append(msg["request_id"])
    c = _client(sm)

    class _Inbound:
        def __init__(self, frames):
            self._frames = frames

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._frames:
                raise StopAsyncIteration
            return json.dumps(self._frames.pop(0))

    c._ws = _Inbound([{"type": "file_pull_cancel", "request_id": "r1"}])
    await c._message_loop()
    assert sm.seen == ["r1"]


async def test_a_cancel_right_behind_its_pull_stops_it(tmp_path, monkeypatch):
    """A cancel read in the same batch as its pull, before the pull's task
    ran its first step: the pull never sends a chunk."""
    from satellite.transport import file_sync
    monkeypatch.setattr(file_sync, "CHUNK_SIZE", 64)
    _file(tmp_path, 20, 64)
    sm = _sm(tmp_path)
    c = _client(sm)

    class _Inbound:
        def __init__(self, frames):
            self._frames = frames

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._frames:
                raise StopAsyncIteration
            return json.dumps(self._frames.pop(0))

    wire = c._ws
    c._ws = _Inbound([_pull("r1"), {"type": "file_pull_cancel", "request_id": "r1"}])
    await c._message_loop()
    await asyncio.sleep(0.1)
    c._ws = wire
    writer = asyncio.create_task(c._writer_loop())
    await asyncio.sleep(0.05)
    writer.cancel()
    assert [m for m in wire.sent if m.get("request_id") == "r1"] == []
    assert sm._pulls == {}


async def test_a_bulk_frame_goes_after_a_run_of_pty_frames():
    from satellite.transport import ws_client
    c = _client()
    c._pty_send_queue = asyncio.Queue(maxsize=64)
    for i in range(20):
        await c.enqueue_pty({"type": "pty_output", "i": i, "data_b64": ""})
    await c.enqueue_bulk({"type": "file_content", "chunk_index": 0})
    writer = asyncio.create_task(c._writer_loop())
    while not c._lanes_empty():
        await asyncio.sleep(0.01)
    writer.cancel()
    kinds = [m["type"] for m in c._ws.sent]
    assert kinds.index("file_content") == ws_client._PTY_FRAMES_PER_BULK


async def test_control_goes_before_pty_and_bulk():
    c = _client()
    await c.enqueue_bulk({"type": "file_content"})
    await c.enqueue_pty({"type": "pty_output", "data_b64": ""})
    await c.enqueue_send({"type": "heartbeat"})
    writer = asyncio.create_task(c._writer_loop())
    while not c._lanes_empty():
        await asyncio.sleep(0.01)
    writer.cancel()
    assert [m["type"] for m in c._ws.sent] == ["heartbeat", "pty_output", "file_content"]


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="no TCP_NOTSENT_LOWAT here")
async def test_tcp_notsent_lowat_is_set_beside_the_keepalive():
    from satellite.transport import ws_client
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    s = socket.create_connection(srv.getsockname())
    try:
        ws_client._set_keepalive(s)
        got = s.getsockopt(socket.IPPROTO_TCP, ws_client._notsent_lowat_option())
        assert got == ws_client._NOTSENT_LOWAT_BYTES
    finally:
        s.close()
        srv.close()


async def test_a_pull_cancelled_while_its_file_opens_closes_the_descriptor(tmp_path, monkeypatch):
    import threading
    from satellite.host import safe_fs
    _file(tmp_path, 2, 64)
    sm = _sm(tmp_path)
    c = _client(sm)
    gate = threading.Event()
    opened: list[int] = []
    real = safe_fs.open_regular_for_read

    def _slow_open(*a, **k):
        gate.wait(5)
        fd, st = real(*a, **k)
        opened.append(fd)
        return fd, st

    monkeypatch.setattr(safe_fs, "open_regular_for_read", _slow_open)
    pull = asyncio.create_task(sm.file_pull(_pull("r5"), c))
    await asyncio.sleep(0.05)
    pull.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pull
    for _ in range(100):
        await asyncio.sleep(0.01)
        if opened:
            break
    await asyncio.sleep(0.05)
    with pytest.raises(OSError):
        os.fstat(opened[0])           # closed by the callback, not leaked


async def test_paced_transfers_is_a_capability(tmp_path):
    assert _sm(tmp_path).detect_capabilities()["paced_transfers"] is True
