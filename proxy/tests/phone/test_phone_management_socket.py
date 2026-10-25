"""The phone-management socket's opening: a daemon whose side closes while
the socket opens (a reconnect racing a proxy restart) is one INFO line,
never an ERROR traceback, and is never registered for config pushes."""

import asyncio
import logging

import pytest
from fastapi import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected
from websockets.exceptions import ConnectionClosed

from services.phone import phone_config
from ws import phone_management


class _Socket:
    def __init__(self, *, accept_raises=None, send_raises=None, close_raises=None, frames=()):
        self.headers = {"authorization": "Bearer key"}
        self._accept_raises = accept_raises
        self._send_raises = send_raises
        self._close_raises = close_raises
        self._frames = list(frames)
        self.sent = []
        self.closed = None
        self.registered_while_open = False

    async def accept(self):
        if self._accept_raises:
            raise self._accept_raises

    async def close(self, code=1000, reason=""):
        self.closed = code
        if self._close_raises:
            raise self._close_raises

    async def send_json(self, data):
        if self._send_raises:
            raise self._send_raises
        self.sent.append(data)

    async def receive_text(self):
        self.registered_while_open = self in phone_config._management_clients
        if self._frames:
            frame = self._frames.pop(0)
            if isinstance(frame, BaseException):
                raise frame
            return frame
        raise WebSocketDisconnect(1005)


@pytest.fixture
def opened(monkeypatch):
    monkeypatch.setattr(phone_management.config, "is_master_key", lambda bearer: bearer == "key")
    async def _config():
        return {"version": 7}
    monkeypatch.setattr(phone_management, "build_phone_config", _config)


def _run(socket, caplog):
    with caplog.at_level(logging.INFO, logger="otodock"):
        asyncio.run(phone_management.ws_phone_management_handler(socket, key=""))
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("exc", [
    WebSocketDisconnect(1005), ConnectionClosed(None, None), ClientDisconnected(),
])
def test_a_daemon_gone_at_the_accept_is_one_info_line(opened, caplog, exc):
    socket = _Socket(accept_raises=exc)
    assert _run(socket, caplog) == []
    assert socket.sent == [] and socket not in phone_config._management_clients
    assert any("closed while opening" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", [
    RuntimeError("Expected ASGI message 'websocket.accept', 'websocket.close' or "
                 "'websocket.http.response.start' but got 'websocket.send'."),
    ClientDisconnected(), WebSocketDisconnect(1006),
])
def test_a_first_send_on_a_stopping_server_is_one_info_line(opened, caplog, exc):
    socket = _Socket(send_raises=exc)
    assert _run(socket, caplog) == []
    assert socket not in phone_config._management_clients
    assert any("closed while opening" in r.getMessage() for r in caplog.records)


def test_a_refused_key_whose_daemon_left_is_quiet(opened, caplog):
    socket = _Socket(close_raises=ClientDisconnected())
    socket.headers = {"authorization": "Bearer wrong"}
    assert _run(socket, caplog) == []
    assert socket.closed == 4001 and socket.sent == []


def test_an_open_socket_is_registered_and_dropped_at_the_end(opened, caplog):
    socket = _Socket(frames=['{"type": "pong"}', '{"type": "request_config"}'])
    assert _run(socket, caplog) == []
    assert [m["type"] for m in socket.sent] == ["config_full", "config_full"]
    assert socket.registered_while_open
    assert socket not in phone_config._management_clients


def test_a_lost_connection_in_the_loop_is_no_error(opened, caplog):
    socket = _Socket(frames=[ClientDisconnected()])
    assert _run(socket, caplog) == []
    assert socket not in phone_config._management_clients


def test_a_real_error_while_opening_is_still_an_error(opened, caplog, monkeypatch):
    """Only the socket's own calls read a failure as the daemon leaving: a
    config that cannot be assembled is logged as the error it is."""
    async def broken():
        raise OSError("disk")

    monkeypatch.setattr(phone_management, "build_phone_config", broken)
    socket = _Socket()
    errors = _run(socket, caplog)
    assert [r.getMessage() for r in errors] == ["Phone management WebSocket error: disk"]
    assert socket not in phone_config._management_clients
