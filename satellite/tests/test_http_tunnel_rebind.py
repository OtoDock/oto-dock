"""The local tunnel's self-check and rebind (0.5.168).

A Windows satellite once kept its link to the platform for hours while nothing
listened on its loopback tunnel port any more: CPython's Proactor loop closes a
listening socket for good after one failed accept and reports it to the loop's
exception handler ("Accept failed on a socket"), which swallowed it.

The dev box and CI are Linux, whose selector loop never closes a listener on a
failed accept. The failure is reproduced by what the Proactor does to the
socket after one (``loop._stop_serving(sock)``: the reader gone, the socket
closed, the server object none the wiser), and the handler by the context dict
the Proactor hands it. Windows' bind rule is reproduced by a bind that answers
"address in use".
"""

import asyncio
import errno
import json
import logging
import socket
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
import websockets

from satellite import config
from satellite.transport import http_tunnel, ws_client
from satellite.transport.http_tunnel import (
    LocalTunnelServer,
    make_loop_exception_handler,
)
from satellite.transport.ws_client import SatelliteWSClient


class FakeWS:
    """The WS client surface the tunnel touches."""

    def __init__(self):
        self._authenticated = True
        self.sent: list[dict] = []
        self.tunnel = None
        self.heartbeats = 0

    async def enqueue_send(self, msg: dict) -> None:
        self.sent.append(msg)

    async def heartbeat_now(self) -> None:
        self.heartbeats += 1

    def link_down_for(self) -> float:
        return 42.0


@pytest_asyncio.fixture
async def tunnel():
    ws = FakeWS()
    t = LocalTunnelServer(ws)
    ws.tunnel = t
    await t.start()
    yield t
    await t.stop()


def _close_under(t: LocalTunnelServer) -> None:
    """What the Proactor does to the listening socket after a failed accept."""
    asyncio.get_running_loop()._stop_serving(t._sock)


async def _status(port: int, path: str = "/v1/admin/users") -> int:
    async with aiohttp.ClientSession() as http, http.get(f"http://127.0.0.1:{port}{path}") as resp:
        return resp.status


_REAL_CREATE_SERVER = socket.create_server


def _bind_fails(monkeypatch, *, when=lambda port: True):
    real = _REAL_CREATE_SERVER

    def create_server(address, **kw):
        if when(address[1]):
            raise OSError(errno.EADDRINUSE, "Address already in use")
        return real(address, **kw)
    monkeypatch.setattr(http_tunnel.socket, "create_server", create_server)


@pytest.mark.asyncio
async def test_a_healthy_link_passes_and_never_reaches_the_platform(tunnel):
    assert await tunnel.check_and_heal("a test") is True
    assert tunnel.state() == {
        "tunnel_ok": True, "local_tunnel_port": tunnel.port,
        "tunnel_rebinds": 0, "last_tunnel_error": "",
    }
    async with aiohttp.ClientSession() as http, \
            http.get(f"http://127.0.0.1:{tunnel.port}/v1/tunnel/self-check") as resp:
        assert resp.status == 200
        assert await resp.text() == tunnel._token
    assert tunnel.ws_client.sent == []


@pytest.mark.asyncio
async def test_a_listener_closed_under_the_server_is_rebound_on_its_port(tunnel):
    port = tunnel.port
    _close_under(tunnel)
    with pytest.raises(aiohttp.ClientConnectorError):
        await _status(port)

    assert await tunnel.check_and_heal("the heartbeat") is True
    assert tunnel.port == port
    assert tunnel.rebinds == 1
    assert "was closed" in tunnel.last_error
    assert await _status(port) == 403  # served again (the allowlist answers)
    assert tunnel.ws_client.heartbeats == 0  # the heartbeat carries it itself


@pytest.mark.asyncio
async def test_a_port_another_program_took_moves_and_says_so(tunnel):
    port = tunnel.port
    moved: list[int] = []
    tunnel.on_port = moved.append
    _close_under(tunnel)
    squatter = socket.create_server(("127.0.0.1", port))  # listens: a real conflict
    try:
        assert await tunnel.check_and_heal("the heartbeat") is True
    finally:
        squatter.close()
    assert tunnel.port != port and moved == [tunnel.port]
    assert await _status(tunnel.port) == 403


@pytest.mark.asyncio
async def test_windows_keeps_a_port_only_its_old_connections_hold(tunnel, monkeypatch, caplog):
    """Windows refuses a plain bind while connections the dead listener
    accepted hold the port. With nobody listening there, the rebind shares it
    with them (SO_REUSEADDR) and keeps the port every session holds."""
    monkeypatch.setattr(config, "HOST", config.ROWS[config.WINDOWS])
    port = tunnel.port
    _close_under(tunnel)
    _bind_fails(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="satellite.tunnel"):
        assert await tunnel.check_and_heal("the heartbeat") is True
    assert tunnel.port == port
    assert "beside its open connections" in caplog.text
    assert await _status(port) == 403


@pytest.mark.asyncio
async def test_windows_never_shares_a_port_another_program_listens_on(tunnel, monkeypatch):
    monkeypatch.setattr(config, "HOST", config.ROWS[config.WINDOWS])
    port = tunnel.port
    _close_under(tunnel)
    squatter = socket.create_server(("127.0.0.1", port))
    _bind_fails(monkeypatch, when=lambda p: p == port)
    try:
        assert await tunnel.check_and_heal("the heartbeat") is True
    finally:
        squatter.close()
    assert tunnel.port != port


@pytest.mark.asyncio
async def test_an_accept_failure_is_logged_and_heals_at_once(tunnel, caplog):
    loop = asyncio.get_running_loop()
    handler = make_loop_exception_handler(tunnel, quiet_close_resets=True)
    port = tunnel.port
    with socket.socket() as reported:
        context = {
            "message": "Accept failed on a socket",
            "exception": ConnectionResetError(10054, "An existing connection was forcibly closed"),
            "socket": reported,
        }
        with caplog.at_level(logging.WARNING, logger="satellite.tunnel"):
            handler(loop, context)
            _close_under(tunnel)  # the Proactor closes it after the handler returns
            await tunnel._heal_task
    assert "stopped accepting" in caplog.text and "forcibly closed" in caplog.text
    assert tunnel.rebinds == 1 and tunnel.port == port
    assert tunnel.ws_client.heartbeats == 1  # told the platform at once
    assert await _status(port) == 403


@pytest.mark.asyncio
async def test_an_accept_failure_during_a_running_check_heals_at_its_end(tunnel, monkeypatch):
    """The heartbeat's check passed its round trip a moment before the
    socket closed: the accept failure's heal follows that check at once,
    not at the next tick, and runs once however many failures arrive."""
    real = tunnel._round_trip
    gate = asyncio.Event()
    calls = {"n": 0}

    async def held_then_real():
        calls["n"] += 1
        if calls["n"] == 1:
            await gate.wait()  # the round trip passed while the socket was open
            return "", False
        return await real()
    monkeypatch.setattr(tunnel, "_round_trip", held_then_real)

    port = tunnel.port
    check = asyncio.create_task(tunnel.check_and_heal("the heartbeat"))
    await asyncio.sleep(0.05)
    _close_under(tunnel)
    tunnel.heal_soon("an accept failure")
    tunnel.heal_soon("an accept failure")
    gate.set()
    assert await check is True  # the heartbeat's own check saw the socket open
    for _ in range(100):
        if tunnel.rebinds:
            break
        await asyncio.sleep(0.02)
    await tunnel._heal_task
    assert tunnel.rebinds == 1 and tunnel.port == port
    assert tunnel.ws_client.heartbeats == 1
    assert await _status(port) == 403


@pytest.mark.asyncio
async def test_a_chained_heal_never_runs_after_stop(tunnel, monkeypatch):
    gate = asyncio.Event()

    async def held():
        await gate.wait()
        return "", False
    monkeypatch.setattr(tunnel, "_round_trip", held)
    check = asyncio.create_task(tunnel.check_and_heal("the heartbeat"))
    await asyncio.sleep(0.05)
    tunnel.heal_soon("an accept failure")
    await tunnel.stop()
    with pytest.raises(asyncio.CancelledError):
        await check
    await asyncio.sleep(0.05)
    assert tunnel._heal_task.done() and tunnel.rebinds == 0


def test_the_close_time_reset_noise_stays_quiet_on_windows_only():
    seen: list[dict] = []
    loop = SimpleNamespace(default_exception_handler=seen.append)
    tunnel = SimpleNamespace(heal_soon=lambda trigger: seen.append({"heal": trigger}))
    reset = {"message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
             "exception": ConnectionResetError()}
    winerror = {"message": "[WinError 10054] An existing connection was forcibly closed"}
    other = {"message": "Task exception was never retrieved", "exception": ValueError("x")}

    quiet = make_loop_exception_handler(tunnel, quiet_close_resets=True)
    for ctx in (reset, winerror, other):
        quiet(loop, ctx)
    assert seen == [other]

    seen.clear()
    loud = make_loop_exception_handler(tunnel, quiet_close_resets=False)
    for ctx in (reset, winerror, other):
        loud(loop, ctx)
    assert seen == [reset, winerror, other]


@pytest.mark.asyncio
async def test_slow_answers_rebind_only_after_three_and_never_report_down(tunnel, monkeypatch):
    real = tunnel._round_trip
    calls = {"n": 0}

    async def slow_then_real():
        calls["n"] += 1
        if calls["n"] <= 4:
            return "no answer at 127.0.0.1:1 within 3 s", False
        return await real()
    monkeypatch.setattr(tunnel, "_round_trip", slow_then_real)

    assert await tunnel.check_and_heal("the heartbeat") is True
    assert await tunnel.check_and_heal("the heartbeat") is True
    assert tunnel.rebinds == 0
    # The third slow answer rebinds; the slow check right after is not a failure.
    assert await tunnel.check_and_heal("the heartbeat") is True
    assert tunnel.rebinds == 1 and tunnel.ok


@pytest.mark.asyncio
async def test_a_rebind_that_cannot_listen_reports_down_until_it_can(tunnel, monkeypatch, caplog):
    port = tunnel.port
    _close_under(tunnel)
    _bind_fails(monkeypatch)
    assert await tunnel.check_and_heal("the heartbeat") is False
    assert tunnel.state()["tunnel_ok"] is False
    assert "could not listen again" in tunnel.last_error

    # Still down at the next tick: the try is logged quietly, the down
    # warning itself repeats only every five minutes.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="satellite.tunnel"):
        assert await tunnel.check_and_heal("the heartbeat") is False
    assert caplog.records == []

    monkeypatch.undo()
    assert await tunnel.check_and_heal("the heartbeat") is True
    assert tunnel.port == port and tunnel.state()["tunnel_ok"] is True


@pytest.mark.asyncio
async def test_a_refusal_while_the_link_is_down_is_logged_once_a_minute(tunnel, caplog):
    tunnel.ws_client._authenticated = False
    with caplog.at_level(logging.WARNING, logger="satellite.tunnel"):
        assert await _status(tunnel.port, "/v1/hooks/permission") == 503
        assert await _status(tunnel.port, "/v1/hooks/permission") == 503
    lines = [r for r in caplog.records if "refused /v1/hooks/permission with 503" in r.getMessage()]
    assert len(lines) == 1
    assert "down for 42 s" in lines[0].getMessage()


def test_the_self_check_stays_out_of_the_access_log():
    access_filter = http_tunnel._SelfCheckAccessFilter()

    def record(line: str) -> logging.LogRecord:
        return logging.LogRecord("aiohttp.access", logging.INFO, "", 0, line, None, None)
    assert not access_filter.filter(record('127.0.0.1 "GET /v1/tunnel/self-check HTTP/1.1" 200'))
    assert access_filter.filter(record('127.0.0.1 "POST /v1/hooks/permission HTTP/1.1" 200'))


def _client(tmp_path, cfg=None):
    sm = SimpleNamespace(
        sessions={}, pty_sessions={}, turn_state={},
        compute_agent_fingerprints=lambda: {},
        config=SimpleNamespace(agents_dir=tmp_path),
        detect_capabilities=lambda: {"os": "linux"},
        kill_steps=lambda reason: None, cancel_pulls=lambda: 0,
    )
    return SatelliteWSClient(cfg or SimpleNamespace(), sm)


@pytest.mark.asyncio
async def test_the_heartbeat_carries_the_link_down_then_up(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_client, "_HEARTBEAT_INTERVAL", 0.01)
    c = _client(tmp_path)
    c._authenticated = True
    frames: list[dict] = []
    got = asyncio.Event()

    async def capture(msg):
        frames.append(msg)
        got.set()
    monkeypatch.setattr(c, "enqueue_send", capture)
    t = LocalTunnelServer(c)
    c.tunnel = t
    await t.start()
    port = t.port
    try:
        _close_under(t)
        _bind_fails(monkeypatch)
        loop_task = asyncio.create_task(c._heartbeat_loop())
        await asyncio.wait_for(got.wait(), 10)
        down = frames[-1]
        assert down["type"] == "heartbeat" and down["tunnel_ok"] is False
        assert down["last_tunnel_error"]

        monkeypatch.setattr(http_tunnel.socket, "create_server", _REAL_CREATE_SERVER)
        while frames[-1]["tunnel_ok"] is False:
            got.clear()
            await asyncio.wait_for(got.wait(), 10)
        up = frames[-1]
        assert up["tunnel_ok"] is True and up["local_tunnel_port"] == port
        assert up["tunnel_rebinds"] == 1
        loop_task.cancel()
        await loop_task  # the loop returns on its cancel
    finally:
        await t.stop()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_the_auth_capabilities_carry_the_link_state(tmp_path, monkeypatch):
    """The check runs before the socket opens (never inside the platform's
    auth window), and the auth frame reports its result."""
    auths: list[dict] = []
    seen = asyncio.Event()

    async def _refuse(ws):
        auths.append(json.loads(await ws.recv()))
        seen.set()
        await ws.close(1013, "not now")

    async with websockets.serve(_refuse, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
        c = _client(tmp_path, SimpleNamespace(
            machine_id="m-1", machine_secret="s", platform_url=url, allow_insecure_transport=False))
        t = LocalTunnelServer(c)
        c.tunnel = t
        await t.start()
        _close_under(t)
        runner = asyncio.create_task(c.connect_forever())
        try:
            await asyncio.wait_for(seen.wait(), 10)
        finally:
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runner
            await t.stop()
    caps = auths[0]["capabilities"]
    assert caps["tunnel_ok"] is True and caps["tunnel_rebinds"] == 1
    assert caps["local_tunnel_port"] == t.port
    assert "was closed" in caps["last_tunnel_error"]
