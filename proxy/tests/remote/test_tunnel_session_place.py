"""A bearer that names a session works only through the tunnel of the machine
that holds that session, and a hook's answer waits for the files its own
machine is owed."""

import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from auth import tokens
from auth.session_token import create_session_token
from core.credentials import mcp_broker
from core.remote import (
    machine_push_lane,
    remote_pty,
    satellite_connection as sc,
    satellite_http_tunnel as tun,
)
from core.session import session_state
from tests._timing import bound
from tests.remote import link_fakes as lf


@pytest.fixture
def cm(monkeypatch):
    manager = sc.SatelliteConnectionManager()
    monkeypatch.setattr(sc, "_connection_manager", manager)
    monkeypatch.setattr(sc, "_persist_write", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_kick_reconnect_steps", lambda machine_id: None)
    from core.session import session_manager
    layer = SimpleNamespace(_sessions={})
    layer.owns_session = lambda sid: sid in layer._sessions
    monkeypatch.setattr(session_manager, "_remote_layer", layer)
    manager.layer = layer
    tun._recent_machine.clear()
    machine_push_lane.reset()
    yield manager
    tun._recent_machine.clear()
    machine_push_lane.reset()
    remote_pty._remote_ptys.clear()


def _sid() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_a_session_is_placed_by_the_tables_set_before_its_start_frame_leaves(cm):
    a, b = lf.FakeMachine(cm, "m-a"), lf.FakeMachine(cm, "m-b")
    await a.connect()
    await b.connect()
    # A headless session: its queue exists before the start frame is sent,
    # the remote layer's record only after the machine's ack.
    queued = _sid()
    cm.create_session_queue("m-a", queued, "claude-code-cli", maxsize=4)
    assert tun.session_place(cm, "m-a", queued) == tun.HERE
    assert tun.session_place(cm, "m-b", queued) == tun.ELSEWHERE
    # A terminal: its handle is registered at its construction.
    terminal = _sid()
    remote_pty.RemotePtyProcess(machine_id="m-b", session_id=terminal, rows=24, cols=80,
                                on_output=lambda d: None, on_exit=None, scrollback_limit=64)
    assert tun.session_place(cm, "m-b", terminal) == tun.HERE
    assert tun.session_place(cm, "m-a", terminal) == tun.ELSEWHERE
    # Held in the reconnect window: still that machine's.
    await a.drop()
    assert tun.session_place(cm, "m-a", queued) == tun.HERE
    assert tun.session_place(cm, "m-b", queued) == tun.ELSEWHERE


@pytest.mark.asyncio
async def test_a_session_no_table_knows_is_starting_only_while_it_is_marked_so(cm):
    await lf.FakeMachine(cm, "m-a").connect()
    starting, nobody = _sid(), _sid()
    session_state.mark_starting(starting, 60)
    try:
        assert tun.session_place(cm, "m-a", starting) == tun.STARTING
        assert tun.session_place(cm, "m-a", nobody) == tun.UNKNOWN
    finally:
        session_state.clear_starting(starting)


@pytest.mark.asyncio
async def test_a_session_the_platforms_own_sandbox_holds_is_elsewhere(cm):
    from core.layers.direct.session import _direct_sessions
    await lf.FakeMachine(cm, "m-a").connect()
    local = _sid()
    _direct_sessions[local] = object()
    try:
        assert tun.session_place(cm, "m-a", local) == tun.ELSEWHERE
    finally:
        _direct_sessions.pop(local, None)


@pytest.mark.asyncio
async def test_a_closed_sessions_last_calls_still_resolve_to_its_own_machine(cm):
    a = lf.FakeMachine(cm, "m-a")
    await a.connect()
    await lf.FakeMachine(cm, "m-b").connect()
    sid = _sid()
    cm.create_session_queue("m-a", sid, "claude-code-cli", maxsize=4)
    assert tun.session_place(cm, "m-a", sid) == tun.HERE
    cm.remove_session_queue("m-a", sid)                 # the session closed
    assert tun.session_place(cm, "m-a", sid) == tun.HERE
    assert tun.session_place(cm, "m-b", sid) == tun.ELSEWHERE


@pytest.mark.asyncio
async def test_a_context_that_names_a_machine_places_a_session_nothing_else_holds(cm, monkeypatch):
    await lf.FakeMachine(cm, "m-a").connect()
    sid = _sid()
    monkeypatch.setattr(session_state, "get_session_security",
                        lambda s: lf.security("agent", "m-a") if s == sid else None)
    assert tun.session_place(cm, "m-a", sid) == tun.HERE
    assert tun.session_place(cm, "m-b", sid) == tun.ELSEWHERE


# -- the dispatcher ------------------------------------------------------------


async def _ask(cm, machine: lf.FakeMachine, path: str, headers: dict, monkeypatch, *,
               upstream=None) -> dict:
    disp = tun.SatelliteHttpTunnelDispatcher()
    monkeypatch.setattr(tun, "_resolve_upstream_url", lambda p: "http://127.0.0.1:1/x")
    client = MagicMock()
    client.build_request = MagicMock(return_value="REQ")
    client.send = upstream or AsyncMock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr(disp, "_get_client", lambda is_mcp=False: client)
    stream_id = _sid()
    await disp.handle_request_frame(cm, machine.machine_id, {
        "stream_id": stream_id, "method": "POST", "path": path, "headers": headers,
        "body_b64": "", "body_eof": True, "timeout_s": 30,
    })
    for _ in range(200):
        got = [f for f in machine.frames if f.get("stream_id") == stream_id]
        if got and (got[-1].get("body_eof") or got[-1].get("error")):
            return {**got[0], "frames": got, "client": client}
        await asyncio.sleep(0.01)
    raise AssertionError("the tunnel never answered")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/hooks/file", "/mcp/file-tools/mcp/"])
async def test_a_token_of_another_machines_session_is_refused_for_both_stream_classes(
        cm, monkeypatch, path):
    a, b = lf.FakeMachine(cm, "m-a"), lf.FakeMachine(cm, "m-b")
    await a.connect()
    await b.connect()
    sid = _sid()
    cm.create_session_queue("m-a", sid, "claude-code-cli", maxsize=4)
    token = create_session_token(sid, "agent", "user-admin")
    frame = await _ask(cm, b, path, {"Authorization": f"Bearer {token}"}, monkeypatch)
    assert (frame["status"], frame["error"]) == (401, "session-not-on-machine")
    frame["client"].send.assert_not_called()


@pytest.mark.asyncio
async def test_every_kind_of_bearer_that_names_a_session_is_judged(cm, monkeypatch):
    a, b = lf.FakeMachine(cm, "m-a"), lf.FakeMachine(cm, "m-b")
    await a.connect()
    await b.connect()
    sid = _sid()
    cm.create_session_queue("m-a", sid, "claude-code-cli", maxsize=4)
    session = create_session_token(sid, "agent", "user-admin")
    bearers = {
        "command line": tokens.mint(tokens.CLI, {"sid": sid, "agent": "agent"}, ttl_s=60),
        "tool credentials": mcp_broker.mint_token(sid, "github"),
        "a tool's renewal": mcp_broker.mint_renewal(sid, "gws"),
    }
    assert session
    for name, token in bearers.items():
        frame = await _ask(cm, b, "/v1/hooks/cli-credential",
                           {"Authorization": f"Bearer {token}"}, monkeypatch)
        assert (frame["status"], frame["error"]) == (401, "session-not-on-machine"), name
        own = await _ask(cm, a, "/v1/hooks/cli-credential",
                         {"Authorization": f"Bearer {token}"}, monkeypatch)
        assert own["error"] == "upstream-ConnectError", name          # reached the client


@pytest.mark.asyncio
async def test_a_starting_session_is_told_to_ask_again_and_two_headers_are_refused(cm, monkeypatch):
    a = lf.FakeMachine(cm, "m-a")
    await a.connect()
    sid = _sid()
    session_state.mark_starting(sid, 60)
    try:
        token = create_session_token(sid, "agent", "user-admin")
        frame = await _ask(cm, a, "/v1/hooks/file", {"Authorization": f"Bearer {token}"}, monkeypatch)
        assert (frame["status"], frame["error"]) == (503, "session-starting")
        two = await _ask(cm, a, "/v1/hooks/file",
                         {"Authorization": f"Bearer {token}", "authorization": "Bearer other"},
                         monkeypatch)
        assert (two["status"], two["error"]) == (400, "ambiguous-authorization")
    finally:
        session_state.clear_starting(sid)


@pytest.mark.asyncio
async def test_a_bearer_that_names_no_session_passes_as_before(cm, monkeypatch):
    a = lf.FakeMachine(cm, "m-a")
    await a.connect()
    frame = await _ask(cm, a, "/v1/hooks/file", {"Authorization": "Bearer a-steps-own-claim"},
                       monkeypatch)
    assert frame["error"] == "upstream-ConnectError"
    bare = await _ask(cm, a, "/v1/hooks/file", {}, monkeypatch)
    assert bare["error"] == "upstream-ConnectError"


class _Response:
    status_code = 200
    headers: dict = {}

    def __init__(self, on_read=None):
        self._on_read = on_read

    async def aiter_raw(self):
        if self._on_read is not None:
            self._on_read()
        yield b'{"ok": true}'

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_a_hooks_answer_waits_until_its_own_machine_has_what_the_route_wrote(cm, monkeypatch):
    """The route wrote a file for the session's agent while it ran: the
    answer's last frame goes out only when the asking machine's lane has
    pushed it. Another machine's lane is never waited for."""
    a, frozen = lf.FakeMachine(cm, "m-a"), lf.FakeMachine(cm, "m-frozen", mode=lf.FROZEN)
    await a.connect()
    await frozen.connect()
    sid = _sid()
    cm.create_session_queue("m-a", sid, "claude-code-cli", maxsize=4)
    token = create_session_token(sid, "agent", "user-admin")
    release = asyncio.Event()
    pushing = asyncio.Event()

    async def push(machine_id, ref, source, **kw):
        pushing.set()
        if machine_id == "m-a":
            await release.wait()
            return True
        await asyncio.sleep(3600)

    monkeypatch.setattr(cm, "push_file", push)
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", config.AGENTS_DIR)
    copy = config.AGENTS_DIR / "agent" / "workspace" / "made-by-the-route.txt"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(b"x")

    def route_writes():
        machine_push_lane.mark("m-a", "agent", "workspace/made-by-the-route.txt")
        machine_push_lane.mark("m-frozen", "agent", "workspace/made-by-the-route.txt")

    upstream = AsyncMock(return_value=_Response(on_read=route_writes))
    asking = asyncio.ensure_future(_ask(cm, a, "/v1/hooks/ui",
                                        {"Authorization": f"Bearer {token}"}, monkeypatch,
                                        upstream=upstream))
    await asyncio.wait_for(pushing.wait(), 5)
    await asyncio.sleep(0.05)
    assert not asking.done()                      # its own machine lacks the file
    started = time.monotonic()
    release.set()
    frame = await asyncio.wait_for(asking, 5)
    assert time.monotonic() - started < bound(1.0)     # never the frozen machine's push
    assert frame["status"] == 200 and frame["frames"][-1]["body_eof"] is True
    copy.unlink()


@pytest.mark.asyncio
async def test_a_hook_that_wrote_nothing_and_a_prompt_hook_answer_at_once(cm, monkeypatch):
    a = lf.FakeMachine(cm, "m-a")
    await a.connect()
    sid = _sid()
    cm.create_session_queue("m-a", sid, "claude-code-cli", maxsize=4)
    token = create_session_token(sid, "agent", "user-admin")
    # Something older is still owed to the machine: no hook waits for it.
    monkeypatch.setattr(cm, "push_file", AsyncMock(side_effect=lambda *a, **k: asyncio.sleep(3600)))
    machine_push_lane.mark("m-a", "agent", "workspace/older.txt")
    for path in ("/v1/hooks/ui", "/v1/hooks/permission"):
        started = time.monotonic()
        frame = await _ask(cm, a, path, {"Authorization": f"Bearer {token}"}, monkeypatch,
                           upstream=AsyncMock(return_value=_Response()))
        assert frame["status"] == 200 and time.monotonic() - started < bound(1.0)
