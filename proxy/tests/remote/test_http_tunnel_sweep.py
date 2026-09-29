"""Tunnel stream lifecycle: idle sweep, absolute age cap, per-machine cap,
satellite abort frame, and the invariant that reaping CLOSES the upstream
(cancels the dispatch task) instead of merely forgetting the entry.

Before 2026-09-04 the sweeper cut every long-lived MCP streamable-HTTP GET
at the 15-min clamp ("swept leaked stream" every ~15 min on any machine with
tunneled HTTP MCPs) and left the httpx response open until upstream spoke.
"""

import asyncio
import time

import pytest

from core.remote import satellite_http_tunnel as tun
from core.remote.satellite_http_tunnel import (
    SatelliteHttpTunnelDispatcher,
    _HttpStream,
)


class FakeConnection:
    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg: dict) -> None:
        self.sent.append(msg)


class FakeManager:
    def __init__(self, conn):
        self.conn = conn

    def get_connection(self, machine_id):
        return self.conn


def _stream(machine_id="m1", stream_id="s1", *, created_ago=0.0, active_ago=None,
            timeout_s=30):
    now = time.monotonic()
    st = _HttpStream(stream_id=stream_id, machine_id=machine_id, timeout_s=timeout_s)
    st.created_at = now - created_ago
    st.last_activity = now - (active_ago if active_ago is not None else created_ago)
    return st


async def _park(stream: _HttpStream, closed: list) -> None:
    """Stand-in dispatch task: 'holds an upstream response' until cancelled."""
    stream.upstream_open = True
    try:
        await asyncio.Event().wait()
    finally:
        stream.upstream_open = False
        closed.append(stream.stream_id)


@pytest.mark.asyncio
async def test_sweep_spares_active_stream_reaps_idle_and_closes_it():
    disp = SatelliteHttpTunnelDispatcher()
    closed: list[str] = []
    # Old but still delivering: activity 5 s ago.
    live = _stream(stream_id="live", created_ago=3600, active_ago=5)
    # Old and silent: idle past timeout + grace.
    idle = _stream(stream_id="idle", created_ago=3600, active_ago=3600)
    for st in (live, idle):
        disp._streams[(st.machine_id, st.stream_id)] = st
        st.task = asyncio.create_task(_park(st, closed))
    await asyncio.sleep(0)

    reaped = disp._sweep_once()
    await asyncio.sleep(0)

    assert reaped == [("m1", "idle")]
    assert ("m1", "live") in disp._streams
    assert ("m1", "idle") not in disp._streams
    assert idle.cancel_event.is_set()
    assert closed == ["idle"]          # the task was cancelled → upstream closed
    assert idle.upstream_open is False
    assert live.upstream_open is True
    live.task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sweep_absolute_age_cap_reaps_even_active_streams():
    disp = SatelliteHttpTunnelDispatcher()
    ancient = _stream(stream_id="old", created_ago=tun._STREAM_MAX_AGE_S + 10, active_ago=1)
    disp._streams[("m1", "old")] = ancient
    assert disp._sweep_once() == [("m1", "old")]


@pytest.mark.asyncio
async def test_silent_open_stream_still_expires_at_the_clamp():
    """No abort frame from an old satellite: a silent-but-open stream keeps
    today's behaviour (expires after timeout + grace)."""
    disp = SatelliteHttpTunnelDispatcher()
    st = _stream(stream_id="silent", created_ago=tun._MAX_STREAM_TIMEOUT_S + tun._STREAM_GRACE_S + 5,
                 active_ago=tun._MAX_STREAM_TIMEOUT_S + tun._STREAM_GRACE_S + 5,
                 timeout_s=tun._MAX_STREAM_TIMEOUT_S)
    st.upstream_open = True
    disp._streams[("m1", "silent")] = st
    assert disp._sweep_once() == [("m1", "silent")]


@pytest.mark.asyncio
async def test_abort_frame_closes_stream():
    disp = SatelliteHttpTunnelDispatcher()
    closed: list[str] = []
    st = _stream(stream_id="s9")
    disp._streams[("m1", "s9")] = st
    st.task = asyncio.create_task(_park(st, closed))
    await asyncio.sleep(0)

    assert disp.abort_stream("m1", "s9") is True
    await asyncio.sleep(0)
    assert closed == ["s9"]
    assert ("m1", "s9") not in disp._streams
    assert disp.abort_stream("m1", "s9") is False  # idempotent / unknown


@pytest.mark.asyncio
async def test_cancel_machine_streams_closes_upstreams():
    disp = SatelliteHttpTunnelDispatcher()
    closed: list[str] = []
    for sid in ("a", "b"):
        st = _stream(stream_id=sid)
        disp._streams[("m1", sid)] = st
        st.task = asyncio.create_task(_park(st, closed))
    other = _stream(machine_id="m2", stream_id="c")
    disp._streams[("m2", "c")] = other
    other.task = asyncio.create_task(_park(other, closed))
    await asyncio.sleep(0)

    await disp.cancel_machine_streams(FakeManager(FakeConnection()), "m1")
    await asyncio.sleep(0)
    assert sorted(closed) == ["a", "b"]
    assert ("m2", "c") in disp._streams
    other.task.cancel()
    await asyncio.sleep(0)


# --- Stream classes: MCP streams and hook streams are counted apart -------

def _frame(stream_id: str, path: str, method: str = "GET") -> dict:
    return {
        "stream_id": stream_id, "method": method, "path": path,
        "headers": {}, "body_b64": "", "body_eof": True, "timeout_s": 30,
    }


class _ParkedClient:
    """A client whose every request holds its upstream open until released,
    so an admitted stream stays in ``_streams`` with its slot taken."""

    def __init__(self):
        self.release = asyncio.Event()
        self.sent: list[str] = []

    def build_request(self, method, url, **kw):
        return url

    async def send(self, req, stream=False):
        self.sent.append(req)
        await self.release.wait()
        raise RuntimeError("released")


async def _admitted(disp, mgr, machine_id, stream_id, path, method="GET") -> bool:
    await disp.handle_request_frame(mgr, machine_id, _frame(stream_id, path, method))
    await asyncio.sleep(0)   # the dispatch task reaches the parked send
    return (machine_id, stream_id) in disp._streams


def _refused(conn: FakeConnection) -> bool:
    last = conn.sent[-1]
    return (last["type"] == "http_response" and last["status"] == 503
            and last["error"] == "too-many-streams")


async def _drain(disp):
    for key in list(disp._streams):
        disp._reap(key)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_mcp_streams_and_hook_streams_are_capped_separately(monkeypatch):
    monkeypatch.setattr(tun, "_MAX_MCP_STREAMS_PER_MACHINE", 2)
    monkeypatch.setattr(tun, "_MAX_HOOK_STREAMS_PER_MACHINE", 2)
    monkeypatch.setattr(tun, "_resolve_upstream_url", lambda p: "http://127.0.0.1:1/x")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    client = _ParkedClient()
    monkeypatch.setattr(disp, "_get_client", lambda is_mcp=False: client)
    try:
        assert await _admitted(disp, mgr, "m1", "mcp-1", "/mcp/file-tools/mcp/")
        # A queried MCP path is still an MCP stream.
        assert await _admitted(disp, mgr, "m1", "mcp-2", "/mcp/file-tools/mcp/?session_id=1")
        assert disp._open[("m1", "mcp")] == 2 and disp._open_mcp_total == 2
        # The machine's MCP streams are at their cap: a hook is still admitted.
        assert await _admitted(disp, mgr, "m1", "hook-1", "/v1/hooks/permission", "POST")
        assert disp._open[("m1", "hook")] == 1
        # A third MCP stream is refused with the protocol frame, at once.
        assert not await _admitted(disp, mgr, "m1", "mcp-3", "/mcp/file-tools/mcp/")
        assert _refused(conn)
        assert disp._open[("m1", "mcp")] == 2 and disp._open_mcp_total == 2
        # The mirror on another machine: hooks at their cap refuse a hook,
        # never an MCP stream.
        assert await _admitted(disp, mgr, "m2", "hook-a", "/v1/hooks/permission", "POST")
        assert await _admitted(disp, mgr, "m2", "hook-b", "/v1/hooks/stop", "POST")
        assert not await _admitted(disp, mgr, "m2", "hook-c", "/v1/hooks/permission", "POST")
        assert _refused(conn)
        assert await _admitted(disp, mgr, "m2", "mcp-a", "/mcp/file-tools/mcp/")
        assert disp._open[("m2", "hook")] == 2 and disp._open[("m2", "mcp")] == 1
        assert disp._open_mcp_total == 3
    finally:
        await _drain(disp)


@pytest.mark.asyncio
async def test_the_fleet_mcp_cap_refuses_mcp_streams_and_never_a_hook(monkeypatch):
    monkeypatch.setattr(tun, "_MAX_MCP_STREAMS_TOTAL", 2)
    monkeypatch.setattr(tun, "_resolve_upstream_url", lambda p: "http://127.0.0.1:1/x")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    client = _ParkedClient()
    monkeypatch.setattr(disp, "_get_client", lambda is_mcp=False: client)
    try:
        assert await _admitted(disp, mgr, "m1", "s1", "/mcp/file-tools/mcp/")
        assert await _admitted(disp, mgr, "m2", "s2", "/mcp/file-tools/mcp/")
        before = (dict(disp._open), disp._open_mcp_total)
        assert not await _admitted(disp, mgr, "m3", "s3", "/mcp/file-tools/mcp/")
        assert _refused(conn)
        # A refusal changes no counter.
        assert (dict(disp._open), disp._open_mcp_total) == before
        assert await _admitted(disp, mgr, "m3", "h3", "/v1/hooks/permission", "POST")
    finally:
        await _drain(disp)


@pytest.mark.asyncio
async def test_a_forgotten_stream_frees_its_slot_exactly_once(monkeypatch):
    monkeypatch.setattr(tun, "_resolve_upstream_url", lambda p: "http://127.0.0.1:1/x")
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    client = _ParkedClient()
    monkeypatch.setattr(disp, "_get_client", lambda is_mcp=False: client)
    assert await _admitted(disp, mgr, "m1", "s1", "/mcp/file-tools/mcp/")
    assert await _admitted(disp, mgr, "m1", "h1", "/v1/hooks/permission", "POST")
    # Reaped: the slot is freed now, and the cancelled dispatch's finally
    # does not free it again.
    disp._reap(("m1", "s1"))
    assert disp._open_mcp_total == 0 and ("m1", "mcp") not in disp._open
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert disp._open_mcp_total == 0 and ("m1", "mcp") not in disp._open
    # A finished dispatch frees its slot through the same path.
    client.release.set()
    for _ in range(20):
        if ("m1", "h1") not in disp._streams:
            break
        await asyncio.sleep(0.01)
    assert ("m1", "hook") not in disp._open
    # A stream inserted by hand was never admitted: reaping it touches no
    # counter.
    disp._streams[("m1", "x")] = _stream(stream_id="x")
    disp._reap(("m1", "x"))
    assert disp._open == {} and disp._open_mcp_total == 0
    # A machine's counters are gone with its streams.
    client.release.clear()
    assert await _admitted(disp, mgr, "m1", "s2", "/mcp/file-tools/mcp/")
    assert await _admitted(disp, mgr, "m1", "h2", "/v1/hooks/stop", "POST")
    await disp.cancel_machine_streams(mgr, "m1")
    await asyncio.sleep(0)
    assert disp._open == {} and disp._open_mcp_total == 0


@pytest.mark.asyncio
async def test_per_machine_hook_stream_cap_returns_503(monkeypatch):
    monkeypatch.setattr(tun, "_MAX_HOOK_STREAMS_PER_MACHINE", 2)
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    client = _ParkedClient()
    monkeypatch.setattr(disp, "_get_client", lambda is_mcp=False: client)
    try:
        for sid in ("a", "b"):
            assert await _admitted(disp, mgr, "m1", sid, "/v1/hooks/permission", "POST")
        await disp.handle_request_frame(mgr, "m1", {
            "stream_id": "c", "method": "GET", "path": "/v1/hooks/permission",
            "headers": {}, "body_b64": "", "body_eof": True, "timeout_s": 30,
        })
        assert ("m1", "c") not in disp._streams
        assert conn.sent[-1]["type"] == "http_response"
        assert conn.sent[-1]["status"] == 503
        assert conn.sent[-1]["error"] == "too-many-streams"
    finally:
        await _drain(disp)


def test_request_chunk_refreshes_activity():
    disp = SatelliteHttpTunnelDispatcher()
    st = _stream(stream_id="s1", created_ago=100, active_ago=100)
    disp._streams[("m1", "s1")] = st
    before = st.last_activity
    disp.handle_request_chunk("m1", {"stream_id": "s1", "body_b64": "", "body_eof": False})
    assert st.last_activity > before
