"""AMI DTMF event listener: protocol client, manager lifecycle, injection.

Drives ``AmiEventClient`` against a scripted fake AMI server on a loopback
socket (real asyncio streams — the protocol framing is the thing under
test), and ``AmiListenerManager`` with an injected client factory (lifecycle
diffing needs no sockets).
"""

import asyncio
import contextlib

import pytest
import pytest_asyncio

from telephony import ami_events
from telephony.ami_client import AMIError
from telephony.ami_events import (
    FLAT_SERVER_KEY, AmiEventClient, AmiListenerManager,
)
from telephony.audio_socket import AudioSocketConnection

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake AMI server
# ---------------------------------------------------------------------------

class FakeAmiServer:
    def __init__(self, *, login_ok=True, events_response="Success",
                 answer_pings=True):
        self.login_ok = login_ok
        self.events_response = events_response
        self.answer_pings = answer_pings
        self.received: list[dict] = []
        self.connections = 0
        self._server = None
        self._writer = None

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle, host="127.0.0.1", port=0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        # Close the live client transport BEFORE wait_closed(): on 3.12+
        # wait_closed() waits for every attached transport to detach, and a
        # handler that returned on EOF without closing its writer would
        # leave one attached forever.
        await self.drop_client()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def drop_client(self):
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
            self._writer = None

    def send_event(self, **fields):
        lines = "".join(f"{k}: {v}\r\n" for k, v in fields.items())
        self._writer.write((lines + "\r\n").encode())

    async def _handle(self, reader, writer):
        self.connections += 1
        self._writer = writer
        writer.write(b"Asterisk Call Manager/6.0.0\r\n")
        try:
            await self._serve(reader, writer)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve(self, reader, writer):
        while True:
            packet = {}
            while True:
                line = await reader.readline()
                if not line:
                    return
                text = line.decode().strip()
                if not text:
                    break
                if ": " in text:
                    k, v = text.split(": ", 1)
                    packet[k] = v
            if not packet:
                continue
            self.received.append(packet)
            action = packet.get("Action", "")
            aid = packet.get("ActionID", "")
            if action == "Login":
                resp = "Success" if self.login_ok else "Error"
                self._reply(writer, Response=resp, ActionID=aid,
                            Message="ok" if self.login_ok else "denied")
            elif action == "Events":
                self._reply(writer, Response=self.events_response,
                            ActionID=aid)
            elif action == "Ping":
                if self.answer_pings:
                    self._reply(writer, Response="Success", Ping="Pong",
                                ActionID=aid)
            # unknown actions deliberately unanswered (timeout tests)

    @staticmethod
    def _reply(writer, **fields):
        lines = "".join(f"{k}: {v}\r\n" for k, v in fields.items())
        writer.write((lines + "\r\n").encode())


@pytest_asyncio.fixture
async def ami_server():
    server = FakeAmiServer()
    await server.start()
    yield server
    await server.stop()


def _client(server, on_dtmf=None, **kw):
    return AmiEventClient(
        "127.0.0.1", server.port, "oto", "secret",
        server_key="7", on_dtmf=on_dtmf or (lambda *a: None),
        ping_interval_s=kw.pop("ping_interval_s", 10.0),
        read_timeout_s=kw.pop("read_timeout_s", 5.0),
        backoff_min_s=kw.pop("backoff_min_s", 0.05),
        backoff_max_s=kw.pop("backoff_max_s", 0.1),
    )


async def _run_until_connected(client, timeout=3.0):
    task = asyncio.create_task(client.run())
    deadline = asyncio.get_running_loop().time() + timeout
    while not client.connected:
        if asyncio.get_running_loop().time() > deadline:
            task.cancel()
            raise AssertionError("client never connected")
        await asyncio.sleep(0.01)
    return task


async def _stop(client, task):
    await client.close()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# ---------------------------------------------------------------------------
# AmiEventClient — protocol
# ---------------------------------------------------------------------------

async def test_login_subscribe_and_dtmf_dispatch(ami_server):
    got = []
    client = _client(ami_server, on_dtmf=lambda *a: got.append(a))
    task = await _run_until_connected(client)
    try:
        ami_server.send_event(
            Event="DTMFEnd", Direction="Received", Digit="5",
            Uniqueid="1723600000.42", DurationMs="120")
        await asyncio.sleep(0.1)
        assert got == [("7", "1723600000.42", "5", 120)]
        actions = [p.get("Action") for p in ami_server.received]
        assert actions[:2] == ["Login", "Events"]
        assert ami_server.received[0]["Events"] == "off"
        assert ami_server.received[1]["EventMask"] == "call,dtmf"
    finally:
        await _stop(client, task)


async def test_irrelevant_events_never_dispatch(ami_server):
    got = []
    client = _client(ami_server, on_dtmf=lambda *a: got.append(a))
    task = await _run_until_connected(client)
    try:
        ami_server.send_event(Event="DTMFEnd", Direction="Sent", Digit="5",
                              Uniqueid="1.1", DurationMs="80")
        ami_server.send_event(Event="DTMFBegin", Direction="Received",
                              Digit="5", Uniqueid="1.1")
        ami_server.send_event(Event="DTMFEnd", Direction="Received",
                              Digit="55", Uniqueid="1.1")  # not one char
        ami_server.send_event(Event="DTMFEnd", Direction="Received",
                              Digit="q", Uniqueid="1.1")   # invalid char
        ami_server.send_event(Event="Newchannel", Uniqueid="1.1")
        await asyncio.sleep(0.1)
        assert got == []
    finally:
        await _stop(client, task)


async def test_legacy_events_response_counts_as_success(ami_server):
    ami_server.events_response = "Events On"
    client = _client(ami_server)
    task = await _run_until_connected(client)
    try:
        events_actions = [p for p in ami_server.received
                          if p.get("Action") == "Events"]
        assert len(events_actions) == 1  # no fallback re-subscribe
    finally:
        await _stop(client, task)


async def test_events_failure_falls_back_to_full_mask(ami_server):
    ami_server.events_response = "Error"
    client = _client(ami_server)
    task = await _run_until_connected(client)
    try:
        masks = [p["EventMask"] for p in ami_server.received
                 if p.get("Action") == "Events"]
        assert masks == ["call,dtmf", "on"]
    finally:
        await _stop(client, task)


async def test_send_action_resolves_amid_events(ami_server):
    client = _client(ami_server)
    task = await _run_until_connected(client)
    try:
        ami_server.send_event(Event="Newchannel", Uniqueid="9.9")
        resp = await client.send_action({"Action": "Ping"})
        assert resp.get("Ping") == "Pong"
    finally:
        await _stop(client, task)


async def test_send_action_raises_when_disconnected(ami_server):
    client = _client(ami_server)
    with pytest.raises(AMIError):
        await client.send_action({"Action": "Ping"})


async def test_send_action_times_out_on_silence(ami_server, monkeypatch):
    monkeypatch.setattr(ami_events, "ACTION_TIMEOUT_S", 0.2)
    client = _client(ami_server)
    task = await _run_until_connected(client)
    try:
        with pytest.raises(AMIError):
            await client.send_action({"Action": "Unanswered"})
    finally:
        await _stop(client, task)


async def test_pending_futures_fail_on_disconnect(ami_server):
    client = _client(ami_server)
    task = await _run_until_connected(client)
    try:
        pending = asyncio.create_task(
            client.send_action({"Action": "Unanswered"}))
        await asyncio.sleep(0.05)
        await ami_server.drop_client()
        with pytest.raises(AMIError):
            await asyncio.wait_for(pending, timeout=2.0)
    finally:
        await _stop(client, task)


async def test_reconnects_after_connection_drop(ami_server):
    client = _client(ami_server)
    task = await _run_until_connected(client)
    try:
        await ami_server.drop_client()
        deadline = asyncio.get_running_loop().time() + 3.0
        while ami_server.connections < 2:
            assert asyncio.get_running_loop().time() < deadline, \
                "no reconnect"
            await asyncio.sleep(0.02)
    finally:
        await _stop(client, task)


async def test_login_failure_keeps_retrying_without_connected(ami_server):
    ami_server.login_ok = False
    client = _client(ami_server)
    task = asyncio.create_task(client.run())
    try:
        deadline = asyncio.get_running_loop().time() + 3.0
        while ami_server.connections < 2:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        assert not client.connected
    finally:
        await _stop(client, task)


async def test_action_injection_guard(ami_server):
    client = _client(ami_server)
    task = await _run_until_connected(client)
    try:
        with pytest.raises(AMIError):
            await client.send_action(
                {"Action": "Ping", "X": "a\r\nAction: Command"})
    finally:
        await _stop(client, task)


# ---------------------------------------------------------------------------
# AmiListenerManager — lifecycle + registration
# ---------------------------------------------------------------------------

class FakeEventClient:
    def __init__(self, host, port, username, secret, *, server_key, on_dtmf,
                 **kw):
        self.coords = (host, port, username, secret)
        self.server_key = server_key
        self.on_dtmf = on_dtmf
        self.closed = False
        self.unmatched_events = 0

    async def run(self):
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class CfgStub:
    def __init__(self, servers=None, flat=None, kill="on"):
        self._servers_map = servers or {}
        self._flat = flat or {}
        self._kill = kill

    def ami_event_servers(self):
        return self._servers_map

    @property
    def ami_dtmf_listener(self):
        return self._kill

    @property
    def ami_host(self):
        return self._flat.get("host", "")

    @property
    def ami_port(self):
        return int(self._flat.get("port", 5038))

    @property
    def ami_username(self):
        return self._flat.get("username", "")

    @property
    def ami_secret(self):
        return self._flat.get("secret", "")


def _mgr():
    made = []

    def factory(*args, **kw):
        client = FakeEventClient(*args, **kw)
        made.append(client)
        return client

    return AmiListenerManager(client_factory=factory), made


_ENTRY = {"host": "pbx", "port": 5038, "username": "u", "secret": "s"}


async def test_apply_starts_stops_and_restarts_listeners():
    mgr, made = _mgr()
    await mgr.apply(CfgStub(servers={"7": dict(_ENTRY)}))
    assert len(made) == 1 and not made[0].closed

    # Unchanged coords → no churn.
    await mgr.apply(CfgStub(servers={"7": dict(_ENTRY)}))
    assert len(made) == 1

    # Credential rotation → restart.
    await mgr.apply(CfgStub(servers={"7": dict(_ENTRY, secret="s2")}))
    assert made[0].closed and len(made) == 2

    # Server removed → stopped.
    await mgr.apply(CfgStub(servers={}))
    assert made[1].closed

    await mgr.stop()


async def test_kill_switch_stops_everything():
    mgr, made = _mgr()
    await mgr.apply(CfgStub(servers={"7": dict(_ENTRY)}))
    await mgr.apply(CfgStub(servers={"7": dict(_ENTRY)}, kill="off"))
    assert made[0].closed
    await mgr.stop()


async def test_flat_fallback_only_when_map_has_no_ami_entries():
    flat = {"host": "legacy", "username": "u", "secret": "s"}
    mgr, made = _mgr()
    await mgr.apply(CfgStub(servers={"7": dict(_ENTRY)}, flat=flat))
    assert [c.server_key for c in made] == ["7"]
    await mgr.stop()

    mgr, made = _mgr()
    await mgr.apply(CfgStub(servers={}, flat=flat))
    assert [c.server_key for c in made] == [FLAT_SERVER_KEY]
    await mgr.stop()


async def test_flat_fallback_requires_full_credentials():
    mgr, made = _mgr()
    await mgr.apply(CfgStub(servers={}, flat={"host": "legacy"}))
    assert made == []
    await mgr.stop()


async def test_concurrent_applies_are_serialized():
    mgr, made = _mgr()
    cfg = CfgStub(servers={"7": dict(_ENTRY)})
    await asyncio.gather(mgr.apply(cfg), mgr.apply(cfg))
    assert len(made) == 1
    await mgr.stop()


async def test_registration_routes_digits_to_the_right_call():
    mgr, made = _mgr()
    await mgr.apply(CfgStub(servers={"7": dict(_ENTRY)}))

    class Conn:
        def __init__(self):
            self.got = []

        def inject_dtmf(self, digit, duration_ms=0):
            self.got.append((digit, duration_ms))

    a, b = Conn(), Conn()
    mgr.register_call("7", "111.1", a, "PJSIP/trunk-00000001")
    mgr.register_call("7", "222.2", b)
    mgr._on_dtmf("7", "111.1", "5", 100)
    mgr._on_dtmf("7", "222.2", "9", 50)
    # Same uniqueid, different server → never crosses.
    mgr._on_dtmf("other", "111.1", "3", 10)
    assert a.got == [("5", 100)] and b.got == [("9", 50)]
    assert mgr.call_channel("7", "111.1") == "PJSIP/trunk-00000001"

    mgr.unregister_call("7", "111.1")
    mgr._on_dtmf("7", "111.1", "6", 10)
    assert a.got == [("5", 100)]
    assert made[0].unmatched_events == 1  # the post-unregister event

    await mgr.stop()
    mgr.unregister_call("7", "222.2")  # no-op after stop


async def test_resolve_server_key_prefers_exact_then_flat():
    mgr, made = _mgr()
    await mgr.apply(CfgStub(servers={"7": dict(_ENTRY)}))
    assert mgr.resolve_server_key(7) == "7"
    assert mgr.resolve_server_key(None) is None
    assert mgr.resolve_server_key(99) is None
    await mgr.stop()

    mgr, made = _mgr()
    await mgr.apply(
        CfgStub(servers={}, flat={"host": "h", "username": "u",
                                  "secret": "s"}))
    assert mgr.resolve_server_key(99) == FLAT_SERVER_KEY
    await mgr.stop()


async def test_overwriting_live_registration_warns(caplog):
    mgr, _ = _mgr()
    mgr.register_call("7", "1.1", object())
    with caplog.at_level("WARNING"):
        mgr.register_call("7", "1.1", object())
    assert any("overwriting" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# ConfigManager predicate
# ---------------------------------------------------------------------------

async def test_config_ami_event_servers_requires_full_credentials():
    """The proxy mirrors ami_host from the plain host for every non-Twilio
    server — eligibility must hinge on full credentials, or the manager
    would loop failed logins into the PBX's fail2ban."""
    from config_manager import ConfigManager

    cfg = ConfigManager()
    cfg.load({"settings": {}, "routes": [], "servers": {
        "1": {"adapter_type": "asterisk_freepbx", "ami_host": "pbx1",
              "ami_port": "5038", "ami_username": "u", "ami_secret": "s"},
        # host mirrored from the server row, AMI never configured:
        "2": {"adapter_type": "asterisk_freepbx", "ami_host": "pbx2"},
        "3": {"adapter_type": "twilio", "account_sid": "AC1",
              "auth_token": "t"},
    }})
    assert set(cfg.ami_event_servers()) == {"1"}
    assert cfg.ami_event_servers()["1"] == {
        "host": "pbx1", "port": 5038, "username": "u", "secret": "s"}
    assert cfg.ami_dtmf_listener == "on"  # daemon default


# ---------------------------------------------------------------------------
# AudioSocket injection surface
# ---------------------------------------------------------------------------

class _DummyWriter:
    def get_extra_info(self, name):
        return None


async def test_audiosocket_inject_poll_fifo_and_bounds():
    conn = AudioSocketConnection(reader=None, writer=_DummyWriter())
    assert conn.poll_dtmf() is None
    conn.inject_dtmf("1", 100)
    conn.inject_dtmf("2")
    assert conn.poll_dtmf() == ("1", 100)
    assert conn.poll_dtmf() == ("2", 0)
    assert conn.poll_dtmf() is None

    # Bounded drop-oldest — nothing drains it outside the PIN gate.
    for i in range(40):
        conn.inject_dtmf(str(i % 10), i)
    assert len(conn._dtmf_injected) == 32
    assert conn.poll_dtmf() == ("8", 8)  # 0..7 dropped

    conn._closed = True
    conn.inject_dtmf("9")
    assert len(conn._dtmf_injected) == 31  # closed conn ignores injects
