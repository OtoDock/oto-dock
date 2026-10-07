"""The AudioSocket accept path: the bound on connections that have not yet
identified themselves, the per-host share of it, the headroom kept for the
configured PBX addresses, the identifying-frame timeout and the service a
real peer gets while idle connections are held.

A stub config with no routes drives the real ``_handle_connection`` on a
loopback server: every peer is refused right after its UUID frame (unknown
route), so what these tests watch is the pending bookkeeping alone.
Source addresses come from 127.0.0.0/8, which every Linux loopback accepts.
IPv6 peers are driven through a stub writer that reports the address, since
a loopback interface holds only ``::1``.
"""

import asyncio
import contextlib
import logging
import socket
import struct
import time
import uuid

import pytest

import main as phone_main
from config_manager import ConfigManager


class _Cfg:
    max_live_calls = 50

    def __init__(self, pbx_hosts=()):
        self._pbx_hosts = list(pbx_hosts)

    def resolve_inbound_route(self, u):
        return None

    def get_outbound_route(self, r):
        return None

    def get_default_outbound_route(self):
        return None

    def pbx_hosts(self):
        return list(self._pbx_hosts)


@pytest.fixture(autouse=True)
def _reset_pending(monkeypatch):
    phone_main._pending_total = 0
    phone_main._pending_by_peer.clear()
    monkeypatch.setattr(phone_main, "_pbx_peers", phone_main._PbxPeers())
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


class _StubWriter:
    """A writer that reports ``host`` as its peer and records its close."""

    def __init__(self, host):
        self._peer = (host, 40000, 0, 0) if ":" in host else (host, 40000)
        self.closed = False

    def get_extra_info(self, name, default=None):
        return self._peer if name == "peername" else default

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


async def _stub_connect(host):
    """Drive ``_handle_connection`` for a peer at ``host`` that never sends
    its frame: an admitted one stays parked, a refused one is closed."""
    writer = _StubWriter(host)
    task = asyncio.create_task(phone_main._handle_connection(
        asyncio.StreamReader(), writer, _Cfg(), logging.getLogger("accept-test"), None))
    for _ in range(3):
        await asyncio.sleep(0)
    return task, writer


async def _cancel_all(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_peer_key_unwraps_an_ipv4_mapped_address():
    assert phone_main._peer_key("::ffff:127.0.0.9") == "127.0.0.9"
    assert phone_main._peer_key("127.0.0.9") == "127.0.0.9"


def test_peer_key_groups_ipv6_by_its_64_and_keeps_a_pbx_address_whole():
    a = phone_main._peer_key("2001:db8:1:0:1234:5678:9abc:def0")
    b = phone_main._peer_key("2001:db8:1::2")
    assert a == b == "2001:db8:1::/64"
    assert phone_main._peer_key("2001:db8:2::2") == "2001:db8:2::/64"
    phone_main._pbx_peers.addresses = frozenset({"2001:db8:1::5"})
    assert phone_main._peer_key("2001:db8:1::5") == "2001:db8:1::5"
    assert phone_main._peer_key("2001:db8:1::6") == "2001:db8:1::/64"


@pytest.mark.asyncio
async def test_ipv6_addresses_in_one_64_share_one_peer_share():
    tasks = []
    try:
        for k in range(1, phone_main.PENDING_PER_PEER_MAX + 1):
            task, writer = await _stub_connect(f"2001:db8:1::{k:x}")
            tasks.append(task)
            assert not writer.closed
        task, writer = await _stub_connect("2001:db8:1:0:ffff:ffff:ffff:1")
        assert writer.closed and task.done()
        task, writer = await _stub_connect("2001:db8:2::1")
        tasks.append(task)
        assert not writer.closed
        assert phone_main._pending_by_peer == {
            "2001:db8:1::/64": phone_main.PENDING_PER_PEER_MAX,
            "2001:db8:2::/64": 1,
        }
    finally:
        await _cancel_all(tasks)
    assert phone_main._pending_total == 0
    assert phone_main._pending_by_peer == {}


@pytest.mark.asyncio
async def test_an_ipv4_mapped_peer_counts_against_its_ipv4_share():
    tasks = []
    try:
        for k in range(phone_main.PENDING_PER_PEER_MAX):
            host = "::ffff:192.0.2.7" if k % 2 else "192.0.2.7"
            task, writer = await _stub_connect(host)
            tasks.append(task)
            assert not writer.closed
        _task, writer = await _stub_connect("::ffff:192.0.2.7")
        assert writer.closed
        assert phone_main._pending_by_peer == {
            "192.0.2.7": phone_main.PENDING_PER_PEER_MAX}
    finally:
        await _cancel_all(tasks)


@pytest.mark.asyncio
async def test_other_peers_are_closed_at_the_ceiling_below_the_pbx_reserve():
    srv, port = await _server()
    conns = []
    try:
        per_host = phone_main.PENDING_PER_PEER_MAX
        ceiling = phone_main.PENDING_MAX - phone_main.PENDING_PBX_RESERVE
        hosts = ceiling // per_host
        for k in range(1, hosts + 1):
            for _ in range(per_host):
                conns.append(await _connect(port, f"127.0.0.{k}"))
        await asyncio.sleep(0.05)
        assert phone_main._pending_total == ceiling
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
async def test_a_pbx_address_is_admitted_past_the_other_peers_ceiling_until_pending_max():
    per_host = phone_main.PENDING_PER_PEER_MAX
    reserve_hosts = phone_main.PENDING_PBX_RESERVE // per_host
    pbx = [f"127.0.0.{50 + i}" for i in range(reserve_hosts + 1)]
    await phone_main._pbx_peers.refresh(
        _Cfg(pbx).pbx_hosts(), logging.getLogger("accept-test"))
    srv, port = await _server()
    conns = []
    try:
        hosts = (phone_main.PENDING_MAX - phone_main.PENDING_PBX_RESERVE) // per_host
        for k in range(1, hosts + 1):
            for _ in range(per_host):
                conns.append(await _connect(port, f"127.0.0.{k}"))
        await asyncio.sleep(0.05)
        other_r, other_w = await _connect(port, f"127.0.0.{hosts + 1}")
        assert await _closed_within(other_r, 0.5)
        other_w.close()
        for host in pbx[:-1]:
            for _ in range(per_host):
                conns.append(await _connect(port, host))
        await asyncio.sleep(0.05)
        assert phone_main._pending_total == phone_main.PENDING_MAX
        for r, _w in conns[-per_host * reserve_hosts:]:
            assert not await _closed_within(r, 0.05)
        last_r, last_w = await _connect(port, pbx[-1])
        assert await _closed_within(last_r, 0.5)
        last_w.close()
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


def test_pbx_hosts_are_every_servers_ami_host_and_the_flat_one():
    cfg = ConfigManager()
    cfg.load({"settings": {"ami_host": "pbx.lan"}, "routes": [], "servers": {
        "1": {"adapter_type": "asterisk_freepbx", "ami_host": "pbx.lan"},
        "2": {"adapter_type": "asterisk_freepbx", "ami_host": "192.0.2.20"},
        "3": {"adapter_type": "twilio", "account_sid": "AC1", "auth_token": "t"},
    }})
    assert cfg.pbx_hosts() == ["pbx.lan", "192.0.2.20"]
    assert ConfigManager().pbx_hosts() == []


@pytest.mark.asyncio
async def test_pbx_resolution_skips_dns_for_a_literal_and_keeps_the_last_answer_on_a_failure(
        monkeypatch, caplog):
    asked = []
    answer = {"pbx.lan": [("192.0.2.10", 0), ("::ffff:192.0.2.11", 0, 0, 0)]}

    async def getaddrinfo(host, port, **kwargs):
        asked.append(host)
        if host not in answer:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", sa) for sa in answer[host]]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    log = logging.getLogger("accept-test")
    pbx = phone_main._pbx_peers
    await pbx.refresh(["pbx.lan", "192.0.2.20"], log)
    assert pbx.addresses == {"192.0.2.10", "192.0.2.11", "192.0.2.20"}
    assert asked == ["pbx.lan"]

    answer.clear()
    with caplog.at_level(logging.WARNING, logger="accept-test"):
        await pbx.refresh(["pbx.lan", "192.0.2.20"], log)
        await pbx.refresh(["pbx.lan", "192.0.2.20"], log)
    assert pbx.addresses == {"192.0.2.10", "192.0.2.11", "192.0.2.20"}
    assert len([r for r in caplog.records if "pbx.lan" in r.getMessage()]) == 1

    await pbx.refresh(["192.0.2.20"], log)
    assert pbx.addresses == {"192.0.2.20"}


@pytest.mark.asyncio
async def test_a_malformed_pbx_host_is_skipped_and_the_refresh_task_is_held_until_done(caplog):
    # Both names fail the resolver's IDNA encoding (an empty label, a label
    # over 63 bytes) before any DNS query is made.
    hosts = ["pbx..lan", "x" * 64 + ".lan", "192.0.2.20"]
    with caplog.at_level(logging.WARNING, logger="accept-test"):
        phone_main._schedule_pbx_refresh(_Cfg(hosts), logging.getLogger("accept-test"))
        assert len(phone_main._pbx_refresh_tasks) == 1
        await asyncio.gather(*phone_main._pbx_refresh_tasks)
        await asyncio.sleep(0)
    assert phone_main._pbx_refresh_tasks == set()
    assert phone_main._pbx_peers.addresses == {"192.0.2.20"}
    assert len([r for r in caplog.records if "did not resolve" in r.getMessage()]) == 2
