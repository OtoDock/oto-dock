"""``ws_satellite_handler`` (proxy/ws/satellite.py) — the auth handshake never
touches the DB on the event loop.

A reconnect storm after a stall re-auths the whole fleet at once; before
2026-09-04 every store call in the handshake (machine row, policy setting,
secret check, version stamp, rollback bookkeeping, pause clear) ran
synchronously on the loop. The handler is driven end-to-end with a fake
WebSocket against the real test DB, with the loop guard ARMED for the whole
call. Frames and close codes are pinned so the offload changed nothing
visible.
"""

import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect

from ws import satellite as sat_ws


class FakeSatelliteWS:
    """Minimal starlette-WebSocket stand-in: one inbound auth frame, then EOF.
    ``receive()`` (the update push's watch) yields ``frames`` and then waits;
    after a close it gives the disconnect uvicorn queues for it: the close's
    own code on a live socket, ``gone_code`` when the peer was already gone."""

    def __init__(self, auth: dict | None = None, raw: str | None = None,
                 frames: list[dict] | None = None, gone_code: int | None = None):
        self._auth = raw if raw is not None else json.dumps(auth)
        self._served = False
        self.accepted = False
        self.sent: list[dict] = []
        self.closed: tuple | None = None
        self.frames = list(frames or [])
        self.gone_code = gone_code

    async def receive(self):
        if self.closed is not None:
            return {"type": "websocket.disconnect",
                    "code": self.gone_code if self.gone_code is not None else self.closed[0]}
        if self.frames and any(f.get("type") == "update_required" for f in self.sent):
            return {"type": "websocket.receive", "text": json.dumps(self.frames.pop(0))}
        await asyncio.sleep(3600)

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
        yield  # pragma: no cover — makes this an (empty) async generator


@pytest.fixture(autouse=True)
def _clean_pushed(monkeypatch):
    sat_ws._pending_pushed_updates.clear()
    # The hold after a proxy start has its own test.
    monkeypatch.setattr(sat_ws, "UPDATE_HOLD_S", 0.0)
    yield
    sat_ws._pending_pushed_updates.clear()


async def _make_machine(scope="admin"):
    from storage import remote_store
    from storage.pg import run_db

    def _mk():
        rec = remote_store.create_remote_machine(
            "m-auth-1", "box", "user-admin", pairing_scope=scope,
        )
        secret = remote_store.exchange_pairing_token("m-auth-1", rec["pairing_token"])
        return secret

    return "m-auth-1", await run_db(_mk)


async def _row(machine_id):
    from storage import remote_store
    from storage.pg import run_db
    return await run_db(remote_store.get_remote_machine, machine_id)


def _auth(machine_id, secret, version=None):
    return {
        "type": "auth", "machine_id": machine_id, "machine_secret": secret,
        "capabilities": {"os": "linux", "installed_clis": ["claude-code"]},
        "satellite_version": version or sat_ws.SATELLITE_VERSION_LATEST,
    }


async def _settle():
    """Let the register()-spawned tasks + persister land, then disarm."""
    from core.remote.satellite_connection import get_connection_manager
    await asyncio.sleep(0.05)
    assert await get_connection_manager().drain_persists()


@pytest.mark.asyncio
async def test_ok_auth_frames_unchanged_and_no_db_on_loop(loop_db_guard):
    mid, secret = await _make_machine()
    ws = FakeSatelliteWS(_auth(mid, secret))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
    assert ws.accepted
    first = ws.sent[0]
    assert first["type"] == "auth_result" and first["status"] == "ok"
    assert set(first["policy"]) >= {"allow_full_fs", "device_grants",
                                    "sync_max_file_bytes", "sync_ignore_rules"}
    assert set(first["cli_pins"]) == {"claude_code", "codex"}
    row = await _row(mid)
    assert row["satellite_version"] == sat_ws.SATELLITE_VERSION_LATEST
    assert row["status"] == "disconnected"      # EOF → deregister → persisted
    assert row["last_seen"]                     # exact last contact carried


@pytest.mark.asyncio
async def test_a_registration_ticks_the_machines_credential_gateway(loop_db_guard, monkeypatch):
    """The gateway on the machine served from memory while its link was
    down: its reconnect runs a gateway tick for it, so a credential revoked
    meanwhile stops there within seconds."""
    from core.remote import mcp_gateway_push
    ticked = []
    monkeypatch.setattr(mcp_gateway_push, "tick_machine_soon", ticked.append)
    mid, secret = await _make_machine()
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(FakeSatelliteWS(_auth(mid, secret)))
        await _settle()
    assert ticked == [mid]


@pytest.mark.asyncio
async def test_unknown_machine_rejected_4006(loop_db_guard):
    ws = FakeSatelliteWS(_auth("nope", "x"))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert ws.sent[-1]["reason"] == "machine_deleted"
    assert ws.sent[-1]["action"] == "uninstall"
    assert ws.closed[0] == 4006


@pytest.mark.asyncio
async def test_bad_secret_rejected_4001(loop_db_guard):
    mid, _secret = await _make_machine()
    ws = FakeSatelliteWS(_auth(mid, "wrong"))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert ws.sent[-1]["status"] == "rejected"
    assert ws.closed == (4001, "Auth rejected")


@pytest.mark.asyncio
async def test_user_paired_disabled_rejected_4005(loop_db_guard):
    from storage import database as db
    from storage.pg import run_db
    mid, secret = await _make_machine(scope="user")
    await run_db(db.set_platform_setting, "allow_user_paired_machines", "0")
    try:
        ws = FakeSatelliteWS(_auth(mid, secret))
        with loop_db_guard.active():
            await sat_ws.ws_satellite_handler(ws)
        assert ws.closed[0] == 4005
    finally:
        await run_db(db.set_platform_setting, "allow_user_paired_machines", "1")


@pytest.mark.asyncio
async def test_update_push_4007_and_rollback_bookkeeping(loop_db_guard, monkeypatch):
    from api.remote import remote_machines as rm
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    mid, secret = await _make_machine()

    # 1. Old satellite → auth ok first (its 5 s wait is over before the
    #    tarball goes), then the tarball push + close 4007, target remembered.
    ws = FakeSatelliteWS(_auth(mid, secret, version=sat_ws.MIN_SATELLITE_VERSION))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert [f["type"] for f in ws.sent] == ["auth_result", "update_required"]
    assert ws.sent[0]["status"] == "ok" and ws.sent[0]["cli_pins"] == {}
    assert ws.sent[0]["policy"] == sat_ws._auth_ok(await _row(mid))["policy"]
    assert ws.sent[-1]["target_version"] == sat_ws.SATELLITE_VERSION_LATEST
    assert ws.closed == (4007, "updating")
    assert sat_ws._pending_pushed_updates[mid] == sat_ws.SATELLITE_VERSION_LATEST

    # 2. It reconnects BELOW the target → rollback recorded off-loop, no
    #    re-push, connects on the old version.
    ws2 = FakeSatelliteWS(_auth(mid, secret, version=sat_ws.MIN_SATELLITE_VERSION))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws2)
        await _settle()
    assert ws2.sent[0]["type"] == "auth_result" and ws2.sent[0]["status"] == "ok"
    row = await _row(mid)
    assert int(row["update_rollback_count"]) == 1
    assert row["update_rollback_target"] == sat_ws.SATELLITE_VERSION_LATEST


@pytest.mark.asyncio
async def test_a_push_whose_socket_was_gone_counts_no_rollback(loop_db_guard, monkeypatch):
    """A push lost with a dying socket (the proxy's close found the peer
    gone) is a delivery failure: nothing remembered, so the reconnect below
    the target is offered the update again instead of recording a rollback."""
    from api.remote import remote_machines as rm
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    banners = []

    async def _updating(*a, **k):
        banners.append(a)
    monkeypatch.setattr(sat_ws, "_broadcast_satellite_updating", _updating)
    mid, secret = await _make_machine()
    old = sat_ws.MIN_SATELLITE_VERSION
    ws = FakeSatelliteWS(_auth(mid, secret, version=old), gone_code=1005)
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert [f["type"] for f in ws.sent] == ["auth_result", "update_required"]
    assert mid not in sat_ws._pending_pushed_updates and banners == []
    ws2 = FakeSatelliteWS(_auth(mid, secret, version=old))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws2)
        await _settle()
    assert [f["type"] for f in ws2.sent] == ["auth_result", "update_required"]
    assert int((await _row(mid))["update_rollback_count"] or 0) == 0
    assert sat_ws._pending_pushed_updates[mid] == sat_ws.SATELLITE_VERSION_LATEST and banners


@pytest.mark.asyncio
async def test_an_acknowledged_push_counts_and_a_lost_ack_falls_back_to_the_socket(
    loop_db_guard, monkeypatch,
):
    """A satellite that acknowledges is counted on its ``update_received``
    even when its socket is gone at the close; with no acknowledgement the
    socket's state decides, so a lost ack never switches the budget off."""
    from api.remote import remote_machines as rm
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    monkeypatch.setattr(sat_ws, "UPDATE_ACK_SATELLITE_VERSION", sat_ws.MIN_SATELLITE_VERSION)
    monkeypatch.setattr(sat_ws, "_UPDATE_ACK_WAIT_S", 0.2)
    mid, secret = await _make_machine()
    old = sat_ws.MIN_SATELLITE_VERSION
    target = sat_ws.SATELLITE_VERSION_LATEST
    acked = FakeSatelliteWS(_auth(mid, secret, version=old), gone_code=1005, frames=[
        {"type": "heartbeat"}, {"type": "update_received", "target_version": target}])
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(acked)
    assert sat_ws._pending_pushed_updates.pop(mid) == target
    silent_live = FakeSatelliteWS(_auth(mid, secret, version=old))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(silent_live)
    assert sat_ws._pending_pushed_updates.pop(mid) == target
    silent_gone = FakeSatelliteWS(_auth(mid, secret, version=old), gone_code=1001)
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(silent_gone)
    assert mid not in sat_ws._pending_pushed_updates


@pytest.mark.asyncio
async def test_an_update_waits_out_the_proxys_start_and_drops_a_socket_that_ends(
    loop_db_guard, monkeypatch,
):
    from api.remote import remote_machines as rm
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    monkeypatch.setattr(sat_ws, "UPDATE_HOLD_S", 0.3)
    monkeypatch.setattr(sat_ws, "_serving_since", None)
    mid, secret = await _make_machine()
    old = sat_ws.MIN_SATELLITE_VERSION

    class _EndsInTheHold(FakeSatelliteWS):
        async def receive(self):
            await asyncio.sleep(0.05)
            return {"type": "websocket.disconnect", "code": 1006}

    ws = _EndsInTheHold(_auth(mid, secret, version=old))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert [f["type"] for f in ws.sent] == ["auth_result"] and ws.closed is None
    assert mid not in sat_ws._pending_pushed_updates
    # Past the window the push goes out at once.
    monkeypatch.setattr(sat_ws, "_serving_since", sat_ws.time.monotonic() - 60)
    ws2 = FakeSatelliteWS(_auth(mid, secret, version=old))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws2)
    assert [f["type"] for f in ws2.sent] == ["auth_result", "update_required"]


@pytest.mark.asyncio
async def test_an_oversized_first_frame_is_closed_before_any_parse(loop_db_guard, monkeypatch):
    """The first frame is sized before it is parsed: one past the cap
    is rejected and closed without a ``json.loads``; one at the cap is
    parsed and follows the normal handshake."""
    seen: list[int] = []
    real_loads = json.loads

    def _spy(text, *a, **kw):
        seen.append(len(text) if isinstance(text, str) else -1)
        return real_loads(text, *a, **kw)

    monkeypatch.setattr(sat_ws.json, "loads", _spy)
    cap = sat_ws._MAX_AUTH_FRAME_CHARS
    ws = FakeSatelliteWS(raw="x" * (cap + 1))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert ws.closed == (4001, "Auth frame too large")
    assert ws.sent[-1] == {"type": "auth_result", "status": "rejected",
                           "reason": "Auth frame too large"}
    assert (cap + 1) not in seen

    probe = {"type": "auth", "machine_id": "nope", "machine_secret": "x", "pad": ""}
    probe["pad"] = "p" * (cap - len(json.dumps(probe)))
    frame = json.dumps(probe)
    assert len(frame) == cap
    ws2 = FakeSatelliteWS(raw=frame)
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws2)
    assert cap in seen
    assert ws2.closed[0] == 4006            # parsed, then the unknown-machine path


@pytest.mark.asyncio
async def test_a_first_frame_that_is_not_an_object_is_rejected_not_crashed(loop_db_guard):
    """A well-formed JSON first frame that is not an object (an array, a
    number) takes the "first message must be auth" refusal like any other
    wrong first message, instead of raising out of the handler."""
    for raw in ("[0, 0, 0]", "42", '"auth"', "null"):
        ws = FakeSatelliteWS(raw=raw)
        with loop_db_guard.active():
            await sat_ws.ws_satellite_handler(ws)
        assert ws.closed == (4001, "Expected auth message"), raw
        assert ws.sent[-1]["reason"] == "First message must be auth"


@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [
    ("machine_id", {"$ne": ""}), ("machine_id", 7), ("machine_secret", ["x"]),
    ("satellite_version", 130), ("capabilities", ["os", "linux"]), ("capabilities", "linux"),
])
async def test_an_auth_field_of_the_wrong_type_is_refused(loop_db_guard, field, value):
    """Each auth field has its type: a string id, secret and version, an
    object of capabilities. Anything else answers a refusal and closes; it
    never reaches the store, and no connection is registered."""
    from core.remote.satellite_connection import get_connection_manager
    mid, secret = await _make_machine()
    ws = FakeSatelliteWS({**_auth(mid, secret), field: value})
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert ws.sent[-1]["type"] == "auth_result" and ws.sent[-1]["status"] == "rejected"
    assert ws.closed[0] == 4001
    assert mid not in get_connection_manager()._connections


@pytest.mark.asyncio
async def test_a_binary_first_frame_is_refused_not_crashed(loop_db_guard):
    class _Binary(FakeSatelliteWS):
        async def receive_text(self):
            raise KeyError("text")

    ws = _Binary(raw="")
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert ws.closed[0] == 4001


@pytest.mark.asyncio
async def test_every_refusal_survives_a_gone_peer(loop_db_guard):
    """A peer that left before its refusal: the send fails, the handler
    still closes instead of raising, on every refusal of the handshake."""
    class _Gone(FakeSatelliteWS):
        async def send_text(self, text):
            raise RuntimeError("peer gone")

    mid, secret = await _make_machine()
    for auth, code in (({"type": "auth", "machine_id": ""}, 4001),
                       (_auth("nope", "x"), 4006),
                       (_auth(mid, "wrong"), 4001)):
        ws = _Gone(auth)
        with loop_db_guard.active():
            await sat_ws.ws_satellite_handler(ws)
        assert ws.closed[0] == code, auth


@pytest.mark.asyncio
async def test_a_wrong_first_frame_from_a_gone_peer_still_closes(loop_db_guard):
    """The refusal's send may fail on a peer that already left; the handler
    still reaches the close instead of raising out of the auth path."""
    class _Gone(FakeSatelliteWS):
        async def send_text(self, text):
            raise RuntimeError("peer gone")

    ws = _Gone(raw="[0]")
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert ws.closed == (4001, "Expected auth message")
