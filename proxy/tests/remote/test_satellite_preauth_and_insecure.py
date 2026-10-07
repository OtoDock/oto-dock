"""The satellite socket's hardening (F64, F65): the pre-auth hold is bounded
per client address and released once a peer authenticates; a satellite on an
opted-in plaintext link (``insecure_transport`` in its handshake) that is
below the floor is refused with the installer sentence instead of a tarball
push. Drives the real ``ws_satellite_handler`` through the auth-offload rig.
"""

import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from storage import remote_store
from storage.pg import run_db
from ws import satellite as sat


class FakeSatelliteWS:
    def __init__(self, auth: dict, *, server_port: int = 8443):
        self._auth = json.dumps(auth)
        self._served = False
        self.accepted = False
        self.sent: list[dict] = []
        self.closed: tuple | None = None
        # lan_check.resolve reads scope["server"] / ["client"] / ["headers"].
        self.scope = {"type": "websocket", "server": ("0.0.0.0", server_port),
                      "client": ("203.0.113.9", 5555), "headers": []}

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self._served:
            raise WebSocketDisconnect(1000)
        self._served = True
        return self._auth

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def iter_text(self):
        return
        yield  # pragma: no cover


@pytest.fixture(autouse=True)
def _clean():
    sat._preauth.clear()
    sat._pending_pushed_updates.clear()
    yield
    sat._preauth.clear()
    sat._pending_pushed_updates.clear()


async def _machine(scope="admin"):
    def _mk():
        rec = remote_store.create_remote_machine("m-pre-1", "box", "user-admin", pairing_scope=scope)
        return remote_store.exchange_pairing_token("m-pre-1", rec["pairing_token"])
    return "m-pre-1", await run_db(_mk)


def _auth(mid, secret, *, version, insecure=False):
    caps = {"os": "linux", "installed_clis": ["claude-code"]}
    if insecure:
        caps["insecure_transport"] = True
    return {"type": "auth", "machine_id": mid, "machine_secret": secret,
            "capabilities": caps, "satellite_version": version}


async def _settle():
    from core.remote.satellite_connection import get_connection_manager
    await asyncio.sleep(0.05)
    assert await get_connection_manager().drain_persists()


# --- F64: the pre-auth hold ---

def test_the_preauth_hold_is_bounded_per_address_and_released():
    addr = "203.0.113.4"
    assert all(sat._preauth_acquire(addr) for _ in range(sat._PREAUTH_PER_ADDRESS))
    assert sat._preauth_acquire(addr) is False  # the cap
    assert sat._preauth_acquire("203.0.113.5") is True  # another address, own budget
    sat._preauth_release(addr)
    assert sat._preauth_acquire(addr) is True
    for _ in range(sat._PREAUTH_PER_ADDRESS + 5):
        sat._preauth_release(addr)
    assert addr not in sat._preauth  # no leak, no negative count


@pytest.mark.asyncio
async def test_the_handler_releases_the_preauth_slot_for_an_authenticated_socket():
    mid, secret = await _machine()
    ws = FakeSatelliteWS(_auth(mid, secret, version=sat.SATELLITE_VERSION_LATEST))
    await sat.ws_satellite_handler(ws)
    await _settle()
    assert ws.sent[0]["status"] == "ok"
    # the wrapper released the slot: the address holds nothing after the socket closed
    assert sat._preauth.get("203.0.113.9", 0) == 0


@pytest.mark.asyncio
async def test_a_socket_past_the_address_cap_is_refused_before_accept():
    for _ in range(sat._PREAUTH_PER_ADDRESS):
        assert sat._preauth_acquire("203.0.113.9")
    ws = FakeSatelliteWS(_auth("m", "x", version=sat.SATELLITE_VERSION_LATEST))
    await sat.ws_satellite_handler(ws)
    assert ws.accepted is False and ws.closed[0] == 1013


# --- F65: an insecure link is not pushed code ---

@pytest.mark.asyncio
async def test_an_insecure_satellite_below_the_floor_is_refused_with_the_installer_sentence(monkeypatch):
    monkeypatch.setattr(sat, "SATELLITE_VERSION_LATEST", "9.9.9")
    pushed = []
    monkeypatch.setattr(sat, "note_update_pushed", lambda *a, **k: pushed.append(a))
    mid, secret = await _machine()
    ws = FakeSatelliteWS(_auth(mid, secret, version="0.5.10", insecure=True))
    await sat.ws_satellite_handler(ws)
    rejected = ws.sent[-1]
    assert rejected["status"] == "rejected" and "re-run the installer" in rejected["reason"]
    assert ws.closed[0] == 4001 and pushed == []


@pytest.mark.asyncio
async def test_an_insecure_satellite_above_the_floor_connects_without_a_push(monkeypatch):
    # LATEST ahead of the reported version but the reported version is above
    # MIN: a secure satellite would be offered an update; an insecure one is
    # not pushed and connects normally.
    monkeypatch.setattr(sat, "SATELLITE_VERSION_LATEST", "9.9.9")
    pushed = []
    monkeypatch.setattr(sat, "note_update_pushed", lambda *a, **k: pushed.append(a))
    mid, secret = await _machine()
    ws = FakeSatelliteWS(_auth(mid, secret, version=sat.MIN_SATELLITE_VERSION, insecure=True))
    await sat.ws_satellite_handler(ws)
    await _settle()
    assert ws.sent[0]["status"] == "ok" and pushed == []
