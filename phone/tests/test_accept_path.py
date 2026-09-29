"""The AudioSocket accept path: the bound on connections that have not yet
identified themselves, the per-host share of it, the identifying-frame
timeout and the service a real peer gets while idle connections are held.

A stub config with no routes drives the real ``_handle_connection`` on a
loopback server: every peer is refused right after its UUID frame (unknown
route), so what these tests watch is the pending bookkeeping alone.
Source addresses come from 127.0.0.0/8, which every Linux loopback accepts.
"""

import asyncio
import contextlib
import logging
import struct
import time
import uuid

import pytest

import main as phone_main


class _Cfg:
    max_live_calls = 50

    def resolve_inbound_route(self, u):
        return None

    def get_outbound_route(self, r):
        return None

    def get_default_outbound_route(self):
        return None


@pytest.fixture(autouse=True)
def _reset_pending():
    phone_main._pending_total = 0
    phone_main._pending_by_peer.clear()
    yield
    phone_main._pending_total = 0
    phone_main._pending_by_peer.clear()


async def _server():
    log = logging.getLogger("accept-test")
    srv = await asyncio.start_server(
        lambda r, w: phone_main._handle_connection(r, w, _Cfg(), log, None),
        "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def _connect(port, src="127.0.0.1"):
    return await asyncio.open_connection("127.0.0.1", port, local_addr=(src, 0))


async def _closed_within(reader, seconds):
    """True when the server closes the connection within ``seconds``."""
    try:
        return await asyncio.wait_for(reader.read(1), seconds) == b""
    except asyncio.TimeoutError:
        return False


async def _close_all(conns):
    for _r, w in conns:
        w.close()
    for _r, w in conns:
        with contextlib.suppress(Exception):
            await w.wait_closed()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_global_pending_ceiling_closes_the_extra_connection():
    srv, port = await _server()
    conns = []
    try:
        per_host = phone_main.PENDING_PER_PEER_MAX
        hosts = phone_main.PENDING_MAX // per_host
        for k in range(1, hosts + 1):
            for _ in range(per_host):
                conns.append(await _connect(port, f"127.0.0.{k}"))
        await asyncio.sleep(0.05)
        assert phone_main._pending_total == phone_main.PENDING_MAX
        extra_r, extra_w = await _connect(port, f"127.0.0.{hosts + 1}")
        assert await _closed_within(extra_r, 0.5)
        extra_w.close()
        # The held connections are still open: the ceiling shed only the extra one.
        assert not await _closed_within(conns[0][0], 0.2)
    finally:
        await _close_all(conns)
        srv.close()
        await srv.wait_closed()
    assert phone_main._pending_total == 0


@pytest.mark.asyncio
async def test_per_host_ceiling_keeps_other_hosts_served():
    srv, port = await _server()
    conns = []
    try:
        for _ in range(phone_main.PENDING_PER_PEER_MAX):
            conns.append(await _connect(port, "127.0.0.1"))
        await asyncio.sleep(0.05)
        ninth_r, ninth_w = await _connect(port, "127.0.0.1")
        assert await _closed_within(ninth_r, 0.5)
        ninth_w.close()
        other_r, other_w = await _connect(port, "127.0.0.2")
        assert not await _closed_within(other_r, 0.3)  # served: parked for its UUID frame
        other_w.close()
    finally:
        await _close_all(conns)
        srv.close()
        await srv.wait_closed()
    assert phone_main._pending_by_peer == {}


@pytest.mark.asyncio
async def test_a_peer_is_answered_at_once_after_the_idle_connections_close():
    srv, port = await _server()
    conns = []
    try:
        for _ in range(phone_main.PENDING_PER_PEER_MAX):
            conns.append(await _connect(port, "127.0.0.1"))
        await _close_all(conns)
        conns = []
        r, w = await _connect(port, "127.0.0.1")
        w.write(struct.pack(">BH", 0x01, 16) + uuid.uuid4().bytes)
        await w.drain()
        t0 = time.monotonic()
        assert await _closed_within(r, 1.0)  # unknown route: refused right after the frame
        assert time.monotonic() - t0 < 1.0
        w.close()
    finally:
        await _close_all(conns)
        srv.close()
        await srv.wait_closed()


@pytest.mark.asyncio
async def test_identifying_frame_read_times_out_at_two_seconds():
    srv, port = await _server()
    try:
        r, w = await _connect(port)
        t0 = time.monotonic()
        assert await _closed_within(r, 3.5)
        elapsed = time.monotonic() - t0
        assert 1.4 <= elapsed <= 3.5, elapsed
        w.close()
    finally:
        srv.close()
        await srv.wait_closed()
    assert phone_main._pending_total == 0
