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


def _one_below(version: str) -> str:
    """A satellite one patch below ``version``: below the platform's latest,
    so the sign-in pushes the update."""
    head, _, last = version.rpartition(".")
    return f"{head}.{int(last) - 1}"


def _auth(machine_id, secret, version=None, capabilities=None):
    return {
        "type": "auth", "machine_id": machine_id, "machine_secret": secret,
        "capabilities": {"os": "linux", "installed_clis": ["claude-code"], **(capabilities or {})},
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
    ws = FakeSatelliteWS(_auth(mid, secret, version=_one_below(sat_ws.SATELLITE_VERSION_LATEST)))
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
    ws2 = FakeSatelliteWS(_auth(mid, secret, version=_one_below(sat_ws.SATELLITE_VERSION_LATEST)))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws2)
        await _settle()
    assert ws2.sent[0]["type"] == "auth_result" and ws2.sent[0]["status"] == "ok"
    row = await _row(mid)
    assert int(row["update_rollback_count"]) == 1
    assert row["update_rollback_target"] == sat_ws.SATELLITE_VERSION_LATEST


@pytest.mark.asyncio
async def test_below_the_floor_the_push_stops_at_the_rollback_that_reaches_the_limit(loop_db_guard, monkeypatch):
    """A satellite below the floor is pushed at each sign-in until its
    rollbacks of the target reach ``MAX_AUTO_UPDATE_ROLLBACKS``. The sign-in
    that records the last one reads the count it just wrote, so it pushes
    nothing and refuses the machine until Update now."""
    from api.remote import remote_machines as rm
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    monkeypatch.setattr(sat_ws, "MIN_SATELLITE_VERSION", sat_ws.SATELLITE_VERSION_LATEST)
    mid, secret = await _make_machine()
    old = _one_below(sat_ws.SATELLITE_VERSION_LATEST)
    for rolled_back in range(sat_ws.MAX_AUTO_UPDATE_ROLLBACKS):
        ws = FakeSatelliteWS(_auth(mid, secret, version=old))
        with loop_db_guard.active():
            await sat_ws.ws_satellite_handler(ws)
            await _settle()
        assert [f["type"] for f in ws.sent] == ["auth_result", "update_required"]
        assert int((await _row(mid))["update_rollback_count"] or 0) == rolled_back
    last = FakeSatelliteWS(_auth(mid, secret, version=old))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(last)
        await _settle()
    assert "update_required" not in [f.get("type") for f in last.sent]
    assert last.closed is not None and last.closed[0] == 4001
    assert int((await _row(mid))["update_rollback_count"]) == sat_ws.MAX_AUTO_UPDATE_ROLLBACKS
    assert mid not in sat_ws._pending_pushed_updates


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
    old = _one_below(sat_ws.SATELLITE_VERSION_LATEST)
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
    monkeypatch.setattr(sat_ws, "_UPDATE_ACK_WAIT_S", 0.2)
    mid, secret = await _make_machine()
    old = _one_below(sat_ws.SATELLITE_VERSION_LATEST)
    target = sat_ws.SATELLITE_VERSION_LATEST
    # The satellite under test is one below the floor, so the acknowledging
    # generation is set to it: the test is about the ack, not the floor.
    monkeypatch.setattr(sat_ws, "UPDATE_ACK_SATELLITE_VERSION", old)
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
    old = _one_below(sat_ws.SATELLITE_VERSION_LATEST)

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


# ── a machine gone before the post-auth answer ──

_CLOSED_TRANSPORT = RuntimeError(
    "unable to perform operation on <TCPTransport closed=True reading=False>; "
    "the handler is closed")


class _GoneOnAnswer(FakeSatelliteWS):
    """The auth frame arrives, then the peer is gone when the answer goes out."""

    def __init__(self, *a, exc: BaseException, **kw):
        super().__init__(*a, **kw)
        self.exc = exc

    async def send_text(self, text):
        raise self.exc


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [WebSocketDisconnect(1006), _CLOSED_TRANSPORT],
                         ids=["disconnect", "closed-transport"])
async def test_a_machine_gone_before_the_answer_ends_the_handler_quietly(
    loop_db_guard, monkeypatch, exc,
):
    from core.remote.satellite_connection import get_connection_manager
    registered = []

    async def _register(*a, **k):
        registered.append(a)
    monkeypatch.setattr(get_connection_manager(), "register", _register)
    mid, secret = await _make_machine()
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(_GoneOnAnswer(_auth(mid, secret), exc=exc))
        await _settle()
    assert registered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", [False, True], ids=["answered", "gone-before-answer"])
async def test_an_update_that_landed_is_recorded_whether_or_not_the_answer_went_out(
    loop_db_guard, monkeypatch, gone,
):
    """The version write precedes the answer, so the next connect reads the
    new version as the row's own: the update's result is recorded on this
    connect, the one that saw the version change, answered or not."""
    from core.remote.satellite_connection import get_connection_manager
    from storage import remote_store
    from storage.pg import run_db
    cm = get_connection_manager()
    lanes, banners = [], []
    real_persist = cm.persist_lane

    def _persist(machine_id, lane, value):
        lanes.append((lane, value))
        return real_persist(machine_id, lane, value)

    async def _updated(*a, **k):
        banners.append(a[2])
    monkeypatch.setattr(cm, "persist_lane", _persist)
    monkeypatch.setattr(sat_ws, "_broadcast_satellite_updated", _updated)
    mid, secret = await _make_machine()
    await run_db(remote_store.set_satellite_version, mid, "0.5.100")
    ws = (_GoneOnAnswer(_auth(mid, secret), exc=WebSocketDisconnect(1006)) if gone
          else FakeSatelliteWS(_auth(mid, secret)))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
    latest = sat_ws.SATELLITE_VERSION_LATEST
    assert [v for lane, v in lanes if lane == "update_result"] == [
        {"target_version": latest, "error": None}]
    assert banners == [latest]
    # The next connect on the same version records nothing more.
    lanes.clear()
    banners.clear()
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(FakeSatelliteWS(_auth(mid, secret)))
        await _settle()
    assert [lane for lane, _ in lanes if lane == "update_result"] == [] and banners == []


@pytest.mark.asyncio
async def test_a_fault_building_the_answer_is_not_read_as_a_gone_machine(loop_db_guard, monkeypatch):
    def _broken(machine, **k):
        raise RuntimeError("a policy builder bug")
    monkeypatch.setattr(sat_ws, "_auth_ok", _broken)
    mid, secret = await _make_machine()
    with pytest.raises(RuntimeError, match="policy builder"):
        await sat_ws.ws_satellite_handler(FakeSatelliteWS(_auth(mid, secret)))


@pytest.mark.asyncio
async def test_the_answer_names_the_sign_in_and_the_version_is_written_only_when_it_changed(monkeypatch):
    """Each sign-in's answer carries a fresh ``report_id`` (the alive report's
    identity), and a machine that signs in again on the version its row
    already holds writes nothing for it."""
    from storage import remote_store
    mid, secret = await _make_machine()
    writes: list[str] = []
    real = remote_store.set_satellite_version
    monkeypatch.setattr(remote_store, "set_satellite_version",
                        lambda machine_id, version: writes.append(version) or real(machine_id, version))
    ids = []
    for _ in range(3):
        ws = FakeSatelliteWS(_auth(mid, secret))
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
        ids.append(ws.sent[0].get("report_id"))
    assert all(ids) and len(set(ids)) == 3
    assert writes == [sat_ws.SATELLITE_VERSION_LATEST]
    assert (await _row(mid))["satellite_version"] == sat_ws.SATELLITE_VERSION_LATEST


@pytest.mark.asyncio
async def test_a_busy_machine_connects_on_its_version_and_its_update_waits_for_an_idle_moment(
    loop_db_guard, monkeypatch,
):
    """A machine running a session takes the update when it is idle (the
    push would end the session): it connects on its version and is marked.
    At zero sessions, on "Update now", and from a satellite that sends no
    count it is pushed at the sign-in."""
    from api.remote import remote_machines as rm
    from core.remote.satellite_connection import get_connection_manager
    from storage import remote_store
    from storage.pg import run_db
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    mid, secret = await _make_machine()
    old = _one_below(sat_ws.SATELLITE_VERSION_LATEST)
    # Above the floor: below it the update is pushed at the sign-in whatever runs.
    monkeypatch.setattr(sat_ws, "MIN_SATELLITE_VERSION", old)
    links = get_connection_manager().links

    async def sign_in(**caps):
        ws = FakeSatelliteWS(_auth(mid, secret, version=old, capabilities=caps))
        with loop_db_guard.active():
            await sat_ws.ws_satellite_handler(ws)
            await _settle()
        return [f["type"] for f in ws.sent]

    assert await sign_in(active_sessions=2) == ["auth_result"]
    assert links.update_waiting(mid) == sat_ws.SATELLITE_VERSION_LATEST
    assert mid not in sat_ws._pending_pushed_updates
    assert await sign_in(active_sessions=0) == ["auth_result", "update_required"]
    assert links.update_waiting(mid) == ""
    sat_ws._pending_pushed_updates.pop(mid, None)
    assert await sign_in() == ["auth_result", "update_required"]
    sat_ws._pending_pushed_updates.pop(mid, None)
    await run_db(remote_store.set_pending_update, mid, True)
    assert await sign_in(active_sessions=2) == ["auth_result", "update_required"]
    sat_ws._pending_pushed_updates.pop(mid, None)
    # A sign-in on the target version clears a mark.
    links.update_waits(mid, sat_ws.SATELLITE_VERSION_LATEST)
    ws = FakeSatelliteWS(_auth(mid, secret, capabilities={"active_sessions": 1}))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
    assert links.update_waiting(mid) == ""


@pytest.mark.asyncio
async def test_a_machine_a_start_is_on_its_way_to_takes_its_update_once_the_start_is_in(
    loop_db_guard, monkeypatch,
):
    """A sign-in with no live session but a session start on its way to the
    machine (a build placed it there) connects on its version and is marked:
    the push would end that session as it begins."""
    from api.remote import remote_machines as rm
    from core.remote.satellite_connection import get_connection_manager
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    mid, secret = await _make_machine()
    old = _one_below(sat_ws.SATELLITE_VERSION_LATEST)
    monkeypatch.setattr(sat_ws, "MIN_SATELLITE_VERSION", old)
    links = get_connection_manager().links
    links.hold_for_start(mid, 60.0)
    try:
        ws = FakeSatelliteWS(_auth(mid, secret, version=old, capabilities={"active_sessions": 0}))
        with loop_db_guard.active():
            await sat_ws.ws_satellite_handler(ws)
            await _settle()
        assert [f["type"] for f in ws.sent] == ["auth_result"]
        assert links.update_waiting(mid) == sat_ws.SATELLITE_VERSION_LATEST
    finally:
        links.get(mid).start_held_until = 0.0
        links.update_waits(mid, "")


@pytest.mark.asyncio
async def test_a_sign_in_on_the_newest_version_clears_a_queued_update(loop_db_guard, monkeypatch):
    """A press while the machine restarted queued an update, and it signed in
    on the version its row already held: the sign-in clears the mark. A
    sign-in on an older version keeps it: that sign-in is pushed the update."""
    from api.remote import remote_machines as rm
    from storage import remote_store
    from storage.pg import run_db
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    mid, secret = await _make_machine()
    latest = sat_ws.SATELLITE_VERSION_LATEST
    await run_db(remote_store.set_satellite_version, mid, latest)
    await run_db(remote_store.set_pending_update, mid, True)
    ws = FakeSatelliteWS(_auth(mid, secret, version=latest))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
    assert [f["type"] for f in ws.sent] == ["auth_result"]
    row = await _row(mid)
    assert row["pending_update"] is False and row["satellite_version"] == latest

    await run_db(remote_store.set_pending_update, mid, True)
    ws = FakeSatelliteWS(_auth(mid, secret, version=_one_below(latest)))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
    assert [f["type"] for f in ws.sent] == ["auth_result", "update_required"]
    assert (await _row(mid))["pending_update"] is True


@pytest.mark.asyncio
async def test_a_sign_in_that_cannot_take_the_update_clears_a_queued_one(loop_db_guard, monkeypatch):
    """A plaintext link takes no update: a mark left from before would push
    at once the day its link turns secure, whatever runs there then. (A
    floor below the newest version, as between two major releases.)"""
    from storage import remote_store
    from storage.pg import run_db
    monkeypatch.setattr(sat_ws, "MIN_SATELLITE_VERSION", "0.5.171")
    mid, secret = await _make_machine()
    await run_db(remote_store.set_pending_update, mid, True)
    ws = FakeSatelliteWS(_auth(mid, secret, version=_one_below(sat_ws.SATELLITE_VERSION_LATEST),
                               capabilities={"insecure_transport": True}))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
    assert [f["type"] for f in ws.sent] == ["auth_result"]
    assert (await _row(mid))["pending_update"] is False


@pytest.mark.asyncio
async def test_the_sign_ins_push_marks_the_update_running_before_it_sends(loop_db_guard, monkeypatch):
    """A press while the machine restarts after the push at its sign-in finds
    the update running; its next sign-in ends the mark."""
    from api.remote import remote_machines as rm
    from core.remote.satellite_connection import get_connection_manager
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "deadbeef"))
    mid, secret = await _make_machine()
    links = get_connection_manager().links
    latest = sat_ws.SATELLITE_VERSION_LATEST
    seen_at_send: list[str] = []

    class _Watching(FakeSatelliteWS):
        async def send_text(self, text):
            if json.loads(text).get("type") == "update_required":
                seen_at_send.append(links.update_running(mid))
            await super().send_text(text)

    ws = _Watching(_auth(mid, secret, version=_one_below(latest)))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
    assert seen_at_send == [latest]
    assert links.update_running(mid) == latest
    machine = await _row(mid)
    assert rm.machine_update_state(mid, machine, None) == "running"
    ws = FakeSatelliteWS(_auth(mid, secret, version=latest))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
    assert links.update_running(mid) == ""

    # A push whose send failed never left: no update runs.
    class _PushFails(FakeSatelliteWS):
        async def send_text(self, text):
            if json.loads(text).get("type") == "update_required":
                raise RuntimeError("the socket is gone")
            await super().send_text(text)

    ws = _PushFails(_auth(mid, secret, version=_one_below(latest)))
    with loop_db_guard.active():
        await sat_ws.ws_satellite_handler(ws)
        await _settle()
    assert links.update_running(mid) == ""


@pytest.mark.asyncio
async def test_the_idle_hook_pushes_the_waiting_update_once(monkeypatch):
    from unittest.mock import AsyncMock

    from api.remote import remote_machines as rm
    from core.remote import satellite_link
    from core.remote.satellite_connection import get_connection_manager
    mid, _secret = await _make_machine()
    pushed = AsyncMock(return_value=True)
    monkeypatch.setattr(rm, "push_update_online", pushed)
    satellite_link.reset_hooks()
    sat_ws.register()
    try:
        links = get_connection_manager().links
        links.update_waits(mid, "")
        links.idle(mid)
        await asyncio.sleep(0.05)
        pushed.assert_not_awaited()
        links.update_waits(mid, sat_ws.SATELLITE_VERSION_LATEST)
        links.idle(mid)
        links.idle(mid)
        await asyncio.sleep(0.1)
        assert pushed.await_count == 1
        assert pushed.await_args.args[0] == mid and pushed.await_args.args[1]["id"] == mid
        assert links.update_waiting(mid) == ""
    finally:
        satellite_link.reset_hooks()


class _ComputeSocket(FakeSatelliteWS):
    """A compute machine's socket: from loopback on the main listener, as the
    bare-metal unit dials it."""

    scope = {"client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8400), "headers": []}


@pytest.mark.asyncio
async def test_a_compute_machine_signs_in_only_on_the_platforms_own_version(loop_db_guard, monkeypatch):
    """During an update the old compute container may sign in to the new
    server: any version but the server's own is refused, so no session
    reaches a render that does not know its description."""
    from core.config import deployment
    from storage import remote_store
    from storage.pg import run_db
    monkeypatch.setattr(deployment.config, "RUNNING_IN_DOCKER", False, raising=False)

    def _mk():
        remote_store.create_remote_machine("c-auth-1", "Compute", "", pairing_scope="compute")
        return remote_store.mint_compute_secret("c-auth-1")

    secret = await run_db(_mk)
    caps = {"compute": True}
    older = _ComputeSocket(_auth("c-auth-1", secret, version=_one_below(sat_ws.SATELLITE_VERSION_LATEST),
                                 capabilities=caps))
    await sat_ws.ws_satellite_handler(older)
    assert older.closed is not None and older.closed[0] == 4001
    assert "not the platform's own" in older.sent[-1].get("reason", "")
    assert not any(f.get("type") == "update_required" for f in older.sent)
    same = _ComputeSocket(_auth("c-auth-1", secret, capabilities=caps))
    await sat_ws.ws_satellite_handler(same)
    await _settle()
    assert same.sent[0]["type"] == "auth_result" and same.sent[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_a_machine_refused_for_its_version_is_recorded_and_its_next_sign_in_clears_it(loop_db_guard,
                                                                                         monkeypatch):
    """Below the floor with automatic updates off the link is refused, and
    the refusal is kept, so the machine's card and every chat aimed at it
    say it needs an update; a plaintext link is kept as one the installer
    updates; an accepted sign-in clears it."""
    from api.remote import machine_update
    from services.remote import machine_away
    from storage import remote_store
    from storage.pg import run_db
    mid, secret = await _make_machine()
    await run_db(remote_store.set_auto_update_enabled, mid, False)
    old = _one_below(sat_ws.MIN_SATELLITE_VERSION)
    try:
        for caps, plaintext in (({}, False), ({"insecure_transport": True}, True)):
            ws = FakeSatelliteWS(_auth(mid, secret, version=old, capabilities=caps))
            with loop_db_guard.active():
                await sat_ws.ws_satellite_handler(ws)
                await _settle()
            assert machine_away.refused_version(mid) == (old, plaintext)
        row = await run_db(remote_store.get_remote_machine, mid)
        assert machine_update.machine_update_state(mid, row, None) == "manual"
        machine_away.note_refused(mid, old, plaintext=False)
        assert machine_update.machine_update_state(mid, row, None) == "refused"
        ws = FakeSatelliteWS(_auth(mid, secret, version=sat_ws.SATELLITE_VERSION_LATEST))
        with loop_db_guard.active():
            await sat_ws.ws_satellite_handler(ws)
            await _settle()
        assert machine_away.refused_version(mid) is None
    finally:
        machine_away.clear_refused(mid)
