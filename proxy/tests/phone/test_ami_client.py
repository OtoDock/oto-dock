"""Unit tests for the proxy-side AMI client — ``phone_adapters/ami.py``.

Drives the real client against a fake asyncio AMI server that speaks just enough
of the manager protocol: banner + a stray ``FullyBooted`` event (to prove
ActionID filtering), ``Login``, ``DBPut``, ``DBGet`` (the two-part ``Response``
ack **then** a ``DBGetResponse`` event), ``DBDel``, ``Logoff``. No live PBX.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from services.phone.phone_adapters import PhoneAdapterError, ami as ami_mod
from services.phone.phone_adapters.ami import AMIClient

GOOD_SECRET = "s3cr3t"


async def _send(writer, fields: dict) -> None:
    msg = "\r\n".join(f"{k}: {v}" for k, v in fields.items()) + "\r\n\r\n"
    writer.write(msg.encode())
    await writer.drain()


async def _read_action(reader) -> dict | None:
    """Read one action (header lines until blank) — None on EOF."""
    packet: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            return None
        text = line.decode().strip()
        if not text:
            return packet
        if ": " in text:
            k, v = text.split(": ", 1)
            packet[k] = v


async def _handler(reader, writer, *, secret: str, store: dict, drop_banner: bool) -> None:
    if drop_banner:
        await asyncio.sleep(0.5)  # never send the banner → client banner-timeout
        with contextlib.suppress(Exception):
            writer.close()
        return
    writer.write(b"Asterisk Call Manager/6.0.1\r\n")
    await writer.drain()
    # A real Asterisk emits this unsolicited right after connect — the client
    # must skip it (no ActionID) and still find its Login response.
    await _send(writer, {"Event": "FullyBooted", "Privilege": "system,all",
                         "Status": "Fully Booted"})
    while True:
        action = await _read_action(reader)
        if action is None:
            break
        name = action.get("Action")
        aid = action.get("ActionID", "")
        if name == "Login":
            ok = action.get("Secret") == secret
            await _send(writer, {
                "Response": "Success" if ok else "Error", "ActionID": aid,
                "Message": "Authentication accepted" if ok else "Authentication failed",
            })
            if not ok:
                break  # Asterisk drops the connection on a bad login
        elif name == "DBPut":
            store[(action.get("Family"), action.get("Key"))] = action.get("Val", "")
            await _send(writer, {"Response": "Success", "ActionID": aid,
                                 "Message": "Updated database successfully"})
        elif name == "DBGet":
            key = (action.get("Family"), action.get("Key"))
            if key in store:
                # ack first, value in a SEPARATE event, then list-complete.
                await _send(writer, {"Response": "Success", "ActionID": aid,
                                     "Message": "Result will follow"})
                await _send(writer, {"Event": "DBGetResponse", "ActionID": aid,
                                     "Family": key[0], "Key": key[1], "Val": store[key]})
                await _send(writer, {"Event": "DBGetComplete", "ActionID": aid,
                                     "EventList": "Complete", "ListItems": "1"})
            else:
                await _send(writer, {"Response": "Error", "ActionID": aid,
                                     "Message": "Database entry not found"})
        elif name == "DBDel":
            key = (action.get("Family"), action.get("Key"))
            if key in store:
                del store[key]
                await _send(writer, {"Response": "Success", "ActionID": aid,
                                     "Message": "Key deleted successfully"})
            else:
                await _send(writer, {"Response": "Error", "ActionID": aid,
                                     "Message": "Database entry does not exist"})
        elif name == "Logoff":
            await _send(writer, {"Response": "Goodbye", "ActionID": aid,
                                 "Message": "Thanks for all the fish."})
            break
        else:
            await _send(writer, {"Response": "Error", "ActionID": aid,
                                 "Message": "Unknown action"})
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()


@contextlib.asynccontextmanager
async def fake_ami(*, secret: str = GOOD_SECRET, store: dict | None = None,
                   drop_banner: bool = False):
    store = {} if store is None else store

    async def cb(r, w):
        await _handler(r, w, secret=secret, store=store, drop_banner=drop_banner)

    server = await asyncio.start_server(cb, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        async with server:
            await server.start_serving()
            yield host, port, store
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=2)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_login_put_get_del_happy():
    async def scenario():
        async with fake_ami() as (host, port, store):
            async with AMIClient(host=host, port=port, username="u", secret=GOOD_SECRET) as ami:
                await ami.db_put("otodock", "route_uuid/200", "uuid-1")
                assert ("otodock", "route_uuid/200") in store
                got = await ami.db_get("otodock", "route_uuid/200")
                assert got == "uuid-1"  # value came via the DBGetResponse event
                await ami.db_del("otodock", "route_uuid/200")
                assert ("otodock", "route_uuid/200") not in store
                # reading a now-deleted key returns None
                assert await ami.db_get("otodock", "route_uuid/200") is None

    asyncio.run(scenario())


def test_db_get_missing_returns_none():
    async def scenario():
        async with fake_ami() as (host, port, _store):
            async with AMIClient(host=host, port=port, username="u", secret=GOOD_SECRET) as ami:
                assert await ami.db_get("otodock", "route_uuid/nope") is None

    asyncio.run(scenario())


def test_db_del_missing_is_idempotent():
    async def scenario():
        async with fake_ami() as (host, port, _store):
            async with AMIClient(host=host, port=port, username="u", secret=GOOD_SECRET) as ami:
                # no raise on an absent key — best-effort deprovision relies on this
                await ami.db_del("otodock", "route_uuid/absent")

    asyncio.run(scenario())


def test_login_auth_failure_raises_502():
    async def scenario():
        async with fake_ami(secret=GOOD_SECRET) as (host, port, _store):
            with pytest.raises(PhoneAdapterError) as ei:
                async with AMIClient(host=host, port=port, username="u", secret="wrong"):
                    pass
            assert ei.value.status_code == 502
            assert "login failed" in ei.value.message.lower()

    asyncio.run(scenario())


def test_connection_refused_raises_502():
    async def scenario():
        port = _free_port()  # bound then freed → nothing listening
        with pytest.raises(PhoneAdapterError) as ei:
            async with AMIClient(host="127.0.0.1", port=port, username="u", secret="x"):
                pass
        assert ei.value.status_code == 502

    asyncio.run(scenario())


def test_banner_timeout_raises_504(monkeypatch):
    monkeypatch.setattr(ami_mod, "_CONNECT_TIMEOUT", 0.3)
    monkeypatch.setattr(ami_mod, "_READ_TIMEOUT", 0.2)

    async def scenario():
        async with fake_ami(drop_banner=True) as (host, port, _store):
            with pytest.raises(PhoneAdapterError) as ei:
                async with AMIClient(host=host, port=port, username="u", secret="x"):
                    pass
            assert ei.value.status_code == 504

    asyncio.run(scenario())
