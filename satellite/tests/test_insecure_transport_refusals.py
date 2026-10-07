"""F65: a satellite on a plaintext link it opted into announces the link
(``insecure_transport``) and refuses the frames that carry code or end the
install — ``update_required``, ``uninstall`` and the 4006 self-uninstall
close — so a machine the operator can only trust over the wire never runs
code it cannot authenticate. Each refusal is driven through the client's own
handling (the auth phase, the message loop, the reconnect loop), with the
update and the uninstall replaced by recorders, and checked against a secure
link, where the same frame is acted on.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from satellite.transport import ws_client
from satellite.transport.ws_client import SatelliteWSClient, assert_transport_secure

INSECURE_URL = "ws://8.8.8.8:8400"
SECURE_URL = "wss://8.8.8.8"


def test_assert_transport_secure_reports_the_opted_in_plaintext_link():
    assert assert_transport_secure("wss://8.8.8.8") is False
    # a LAN host over ws:// is a note, not insecure-opt-in
    assert assert_transport_secure("ws://192.168.1.10:8400") is False
    # a public host over ws:// with the opt-in is the insecure link
    assert assert_transport_secure("ws://8.8.8.8", allow_insecure=True) is True


class _SM:
    """The session manager surface the paths under test touch."""

    pty_sessions: dict = {}
    sessions: dict = {}
    turn_state: dict = {}

    def detect_capabilities(self) -> dict:
        return {"os": "linux"}

    def kill_steps(self, reason: str) -> None:
        pass

    def headless_sessions_alive(self) -> list:
        return []


def _client(*, insecure: bool) -> SatelliteWSClient:
    cfg = SimpleNamespace(machine_id="m-1", machine_secret="s", platform_url=(
        INSECURE_URL if insecure else SECURE_URL), allow_insecure_transport=insecure)
    c = SatelliteWSClient(cfg, _SM())
    c._insecure_link = insecure
    return c


@pytest.fixture
def actions(monkeypatch):
    """The update and the uninstall, recorded instead of run."""
    seen: dict[str, list] = {"update": [], "uninstall": []}

    async def _update(msg):
        seen["update"].append(msg)

    async def _uninstall():
        seen["uninstall"].append(True)

    monkeypatch.setattr(ws_client, "_self_update_and_restart", _update)
    monkeypatch.setattr(ws_client, "_self_uninstall_and_exit", _uninstall)
    return seen


class _AuthWS:
    """The auth exchange: records the auth frame, answers ``reply``."""

    def __init__(self, reply: dict):
        self.reply = reply
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        return json.dumps(self.reply)


_UPDATE = {"type": "update_required", "target_version": "9.9.9", "previous_version": "0.5.1",
           "tarball_b64": "", "expected_sha256": ""}
_UNINSTALL_REPLY = {"type": "auth_result", "status": "rejected", "reason": "machine_deleted",
                    "action": "uninstall"}


@pytest.mark.asyncio
async def test_the_auth_phase_update_is_refused_on_a_plaintext_link(actions):
    c = _client(insecure=True)
    c._ws = _AuthWS(_UPDATE)
    with pytest.raises(ConnectionError, match="plaintext"):
        await c._authenticate()
    assert actions["update"] == []
    # the link is announced in the auth frame's capabilities
    assert c._ws.sent[0]["capabilities"]["insecure_transport"] is True
    assert c._insecure_refusals == {"update_required"}


@pytest.mark.asyncio
async def test_the_auth_phase_update_is_applied_on_a_secure_link(actions):
    c = _client(insecure=False)
    c._ws = _AuthWS(_UPDATE)
    with pytest.raises(ConnectionError, match="without restart"):
        await c._authenticate()
    assert [m["target_version"] for m in actions["update"]] == ["9.9.9"]
    assert "insecure_transport" not in c._ws.sent[0]["capabilities"]


@pytest.mark.asyncio
async def test_the_auth_phase_uninstall_is_refused_on_a_plaintext_link(actions):
    c = _client(insecure=True)
    c._ws = _AuthWS(_UNINSTALL_REPLY)
    with pytest.raises(ws_client._InsecureUninstallRefused):
        await c._authenticate()
    assert actions["uninstall"] == []
    assert c._insecure_refusals == {"uninstall"}


@pytest.mark.asyncio
async def test_the_auth_phase_uninstall_runs_on_a_secure_link(actions):
    c = _client(insecure=False)
    c._ws = _AuthWS(_UNINSTALL_REPLY)
    await c._authenticate()
    assert actions["uninstall"] == [True]


class _LoopWS:
    """The message loop's socket: yields ``frames`` and ends."""

    def __init__(self, frames: list[dict]):
        self.frames = frames

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for frame in self.frames:
            yield json.dumps(frame)


async def _drive_loop(c: SatelliteWSClient, frames: list[dict]) -> None:
    c._ws = _LoopWS(frames)
    await c._message_loop()
    for _ in range(3):  # the handlers run as tasks
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_loops_update_and_uninstall_are_refused_on_a_plaintext_link(actions):
    c = _client(insecure=True)
    await _drive_loop(c, [_UPDATE, {"type": "uninstall"}, _UPDATE])
    assert actions == {"update": [], "uninstall": []}
    # said once per kind, however often the frame comes
    assert c._insecure_refusals == {"update_required", "uninstall"}


@pytest.mark.asyncio
async def test_the_loops_update_and_uninstall_run_on_a_secure_link(actions):
    c = _client(insecure=False)
    await _drive_loop(c, [_UPDATE, {"type": "uninstall"}])
    assert [m["target_version"] for m in actions["update"]] == ["9.9.9"]
    assert actions["uninstall"] == [True]


class _Stop(BaseException):
    """Ends the reconnect loop from the fake connect (no handler catches it)."""


def _closing_connect(monkeypatch, c: SatelliteWSClient) -> list:
    """Every connect opens, then the auth phase sees the 4006 close; a
    second connect ends the test."""
    calls: list = []

    class _CM:
        async def __aenter__(self):
            return SimpleNamespace(transport=SimpleNamespace(get_extra_info=lambda key: None))

        async def __aexit__(self, *exc):
            return False

    def _connect(url, **kw):
        calls.append(url)
        if len(calls) > 1:
            raise _Stop()
        return _CM()

    async def _closed():
        raise ConnectionClosed(Close(4006, "machine_deleted"), None)

    monkeypatch.setattr(ws_client.websockets, "connect", _connect)
    monkeypatch.setattr(ws_client, "_INSECURE_RECONNECT_WAIT_S", 0.0)
    c._authenticate = _closed
    return calls


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_the_4006_close_is_refused_on_a_plaintext_link(actions, monkeypatch):
    c = _client(insecure=True)
    calls = _closing_connect(monkeypatch, c)
    with pytest.raises(_Stop):
        await c.connect_forever()
    # refused and reconnected, never uninstalled
    assert actions["uninstall"] == [] and len(calls) == 2
    assert c._insecure_refusals == {"the 4006 close"}


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_the_4006_close_uninstalls_on_a_secure_link(actions, monkeypatch):
    c = _client(insecure=False)
    calls = _closing_connect(monkeypatch, c)
    await c.connect_forever()
    assert actions["uninstall"] == [True] and len(calls) == 1
