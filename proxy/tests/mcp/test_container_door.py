"""A Docker tool's door (``services/containers/door.py``) and its idle sweep
(``idle.py``): answers from the record, one wake for many calls, a call's
keepalives, the sentences, a stateful server held by the door, and the
sweep's rules. A fake MCP server answers in process (``httpx.MockTransport``);
no test reaches a container engine (``lifecycle`` is replaced)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from core.credentials import mcp_gateway
from core.credentials.mcp_gateway import CONTAINER, GatewayCredential
from services.containers import door, idle, lifecycle, state


class _Srv:
    port = 8933
    health_endpoint = "/health"
    starts = ""
    image = "ghcr.io/otodock/video-tools:1"
    proxy_callbacks = False
    docker_compose = "docker-compose.yml"
    service_name = ""
    transport = "http"
    runtime = "docker"


class _Manifest:
    def __init__(self, tmp: Path, name: str = "video-tools"):
        self.name = name
        self.label = "Video Tools"
        self.server_name = ""
        self.server = _Srv()
        self.mcp_dir = tmp


class _Stream(httpx.AsyncByteStream):
    def __init__(self, gen, tool=None):
        self._gen = gen
        self._tool = tool

    async def __aiter__(self):
        async for chunk in self._gen:
            yield chunk

    async def aclose(self) -> None:
        if self._tool is not None:
            self._tool.closed += 1
        await self._gen.aclose()


class FakeTool:
    """An MCP server behind its port: stateless, or keeping sessions with a
    stream that pings."""

    def __init__(self, *, stateful: bool = False):
        self.running = True
        self.stateful = stateful
        self.inits = 0
        self.calls: list[dict] = []
        self.deletes: list[str] = []
        self.replies: list[dict] = []
        self.call_delay = 0.0
        self.end_stream = asyncio.Event()
        self.sessions = 0
        # The server behind the port answers (a front may answer before it).
        self.ready = True
        # Every request the tool was sent: method, rpc, Authorization and the
        # session id of its query.
        self.seen: list[dict] = []
        self.send_delay = 0.0
        self.cancelled = 0
        self.closed = 0
        # What a session's own handshake and the stream's GET answer, and a
        # call's answer in place of the usual one (a callable of the message).
        self.init_status = 200
        self.get_status = 200
        self.call_answer = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.running:
            raise httpx.ConnectError("refused", request=request)
        rpc = ""
        if request.method == "POST" and request.content:
            rpc = str((json.loads(request.content) or {}).get("method") or "")
        self.seen.append({"method": request.method, "rpc": rpc,
                          "auth": request.headers.get("authorization", ""),
                          "sid": request.url.params.get("session_id", ""),
                          "sids": request.url.params.get_list("session_id")})
        if request.method == "GET":
            if request.url.path == "/health":
                return httpx.Response(200, json={"ok": True})
            if not self.stateful or self.get_status != 200:
                return httpx.Response(405 if not self.stateful else self.get_status)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_Stream(self._pings()))
        if request.method == "DELETE":
            if request.headers.get("mcp-session-id") != "srv-probe":
                self.deletes.append(request.headers.get("mcp-session-id", ""))
            return httpx.Response(200)
        msg = json.loads(request.content)
        if "method" not in msg:
            self.replies.append(msg)
            return httpx.Response(202)
        if not self.ready and msg.get("id") != "otodock-probe":
            return httpx.Response(502)
        if msg["method"] == "initialize" and msg.get("id") == "otodock-probe":
            # The door's readiness probe: answered when the server is up.
            if not self.ready:
                return httpx.Response(502)
            headers = {"mcp-session-id": "srv-probe"} if self.stateful else {}
            return httpx.Response(200, headers=headers, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}})
        if msg["method"] == "initialize" and msg.get("id") != "otodock-probe" and self.init_status != 200:
            return httpx.Response(self.init_status, json={"error": "invalid_token"})
        if msg["method"] == "initialize":
            self.inits += 1
            self.sessions += 1
            headers = {"mcp-session-id": f"srv-{self.sessions}"} if self.stateful else {}
            return httpx.Response(200, headers=headers, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "video-tools", "version": "1"}}})
        if msg["method"].startswith("notifications/"):
            return httpx.Response(202)
        if msg["method"] == "tools/list":
            # A tool with a sign-in lists what its account may use.
            account = request.headers.get("authorization", "").removeprefix("Bearer ")
            extra = [{"name": f"for-{account}", "inputSchema": {}}] if account.startswith("tok-") else []
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "tools": [{"name": "render", "description": "Render a video.", "inputSchema": {"type": "object"}},
                          *extra]}})
        if msg["method"] == "tools/call":
            self.calls.append({"msg": msg, "session": request.headers.get("mcp-session-id", "")})
            try:
                await asyncio.sleep(self.send_delay)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            if self.call_answer is not None:
                return self.call_answer(msg)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_Stream(self._answer(msg), self))
        return httpx.Response(400)

    async def _answer(self, msg):
        await asyncio.sleep(self.call_delay)
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "done"}]}}
        yield b"event: message\ndata: " + json.dumps(body).encode() + b"\n\n"

    async def _cut(self, msg):
        yield b": progress\n\n"
        raise httpx.ReadError("the container stopped")

    async def _pings(self):
        yield b'event: message\ndata: {"jsonrpc":"2.0","id":"p1","method":"ping"}\n\n'
        await self.end_stream.wait()


_RECORD = {
    "initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "video-tools", "version": "1"}},
    "lists": {"tools/list": [{"name": "render", "inputSchema": {}}]}}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """A fake tool, its record store, its wake and sleep."""
    from services.mcp import mcp_permissions, mcp_registry, tool_listing
    from storage.mcp import mcp_store
    door.reset_for_tests()
    idle.reset_for_tests()
    tool = FakeTool()
    manifest = _Manifest(tmp_path)
    records: dict = {}
    wakes: list[float] = []
    sleeps: list[str] = []

    def wake(m):
        wakes.append(time.monotonic())
        time.sleep(rig_state["wake_s"])
        if rig_state["wake_fails"]:
            state.failed_start(m.name, rig_state["wake_fails"])
            return False
        tool.running = True
        return True

    rig_state = {"wake_s": 0.0, "wake_fails": "", "fp": "fp1"}
    client = httpx.AsyncClient(transport=httpx.MockTransport(tool.handler))
    monkeypatch.setattr(mcp_gateway, "client", lambda *a, **k: client)
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda name: manifest if name == manifest.name else None)
    monkeypatch.setattr(mcp_registry, "resolve_server_config",
                        lambda m, agent, **k: {"type": "http", "url": "http://tool:8933/mcp/"})
    monkeypatch.setattr(tool_listing, "container_fingerprint",
                        lambda m, account="": rig_state["fp"] + (f"-{account}" if account else ""))
    monkeypatch.setattr(tool_listing, "saved_platform", lambda key, fp: records.get((key, fp)))
    # A session's own records, by its scope (agent, person), as the stand-in's.
    scoped: dict = {}
    monkeypatch.setattr(tool_listing, "saved", lambda scope, key, fp: scoped.get((scope, key, fp)))

    def keep(scope, key, fp, listing):
        scoped[(scope, key, fp)] = tool_listing._clean(listing)
        return True
    monkeypatch.setattr(tool_listing, "keep", keep)

    def keep_platform(key, fp, listing, agents):
        records[(key, fp)] = tool_listing._clean(listing)
        return True
    monkeypatch.setattr(tool_listing, "keep_platform", keep_platform)
    monkeypatch.setattr(mcp_store, "get_mcp_agents", lambda name: ["agent-a"])
    monkeypatch.setattr(mcp_store, "get_mcp_state", lambda name: {"enabled": True})
    monkeypatch.setattr(mcp_store, "get_keep_running", set)
    monkeypatch.setattr(mcp_permissions, "record_tool_hints", lambda server, tools: None)
    monkeypatch.setattr(lifecycle, "wake_container", wake)
    monkeypatch.setattr(lifecycle, "managed", lambda m: True)
    monkeypatch.setattr(lifecycle, "sleep_container", lambda m: sleeps.append(m.name) or True)
    records[("video-tools", "fp1")] = _RECORD
    cred = GatewayCredential(upstream="http://tool:8933", path="/mcp/", allowlist_key="", proxy_local=True,
                             manifest=manifest.name, kind=CONTAINER)
    yield {"tool": tool, "manifest": manifest, "records": records, "scoped": scoped, "wakes": wakes,
           "sleeps": sleeps, "cred": cred, "state": rig_state}
    door.reset_for_tests()
    idle.reset_for_tests()


def _post(sid, cred, msg, *, credential=(), query=b"", headers=()):
    return door.step(sid, "video-tools", cred, "POST", "/mcp/", query,
                     [("Content-Type", "application/json"), *headers], json.dumps(msg).encode(),
                     credential=credential)


async def _body(answered) -> bytes:
    return b"".join([chunk async for chunk in answered.aiter_raw()])


def _answer_of(raw: bytes, rid):
    if raw.lstrip().startswith(b"{"):
        return json.loads(raw)
    for event in raw.replace(b"\r\n", b"\n").split(b"\n\n"):
        data = [ln[5:].strip() for ln in event.split(b"\n") if ln.startswith(b"data:")]
        if data:
            obj = json.loads(b"\n".join(data))
            if obj.get("id") == rid:
                return obj
    return None


def _call(rid=7):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": "render", "arguments": {}}}


def _init(rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {"roots": {}},
                       "clientInfo": {"name": "claude-code", "version": "2"}}}


# --- the record --------------------------------------------------------------


def test_the_handshake_and_the_list_come_from_the_record_while_the_tool_sleeps(rig):
    rig["records"][("video-tools", "fp1")] = {
        "initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                       "serverInfo": {"name": "video-tools", "version": "1"}},
        "lists": {"tools/list": [{"name": "render", "inputSchema": {}}]}}
    rig["tool"].running = False
    state.of("video-tools").asleep = True

    async def scenario():
        init = _answer_of(await _body(await _post("s1", rig["cred"], _init())), 1)
        listed = _answer_of(await _body(await _post(
            "s1", rig["cred"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})), 2)
        return init, listed

    init, listed = asyncio.run(scenario())
    assert init["result"]["serverInfo"]["name"] == "video-tools"
    assert [t["name"] for t in listed["result"]["tools"]] == ["render"]
    assert rig["wakes"] == [] and rig["tool"].inits == 0
    assert state.of("video-tools").asleep


@pytest.mark.parametrize("early", [False, True])
def test_a_call_s_tool_is_woken_at_the_handshake_and_no_other(rig, early):
    """A call's tool (the build's ``call``) starts beside the engine; any other
    sleeps through the handshake and the lists until a call."""
    import dataclasses
    cred = dataclasses.replace(rig["cred"], wakes_early=early)
    rig["tool"].running = False
    state.of("video-tools").asleep = True

    async def scenario():
        await _body(await _post("s1", cred, _init()))
        await _body(await _post("s1", cred, _list()))
        for _ in range(50):
            if rig["wakes"]:
                break
            await asyncio.sleep(0.01)
        return len(rig["wakes"])

    assert asyncio.run(scenario()) == (1 if early else 0)


def test_with_no_record_the_door_reads_the_tool_once_and_binds_no_session(rig):
    rig["records"].clear()

    async def scenario():
        first = _answer_of(await _body(await _post("s1", rig["cred"], _init())), 1)
        second = _answer_of(await _body(await _post("s2", rig["cred"], _init())), 1)
        return first, second

    first, second = asyncio.run(scenario())
    assert first["result"]["serverInfo"]["name"] == "video-tools" == second["result"]["serverInfo"]["name"]
    assert rig["tool"].inits == 1
    assert ("video-tools", "fp1") in rig["records"]
    assert not door._door.bindings


def test_a_call_naming_no_tool_of_the_record_starts_nothing(rig):
    rig["records"][("video-tools", "fp1")] = {
        "initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
        "lists": {"tools/list": [{"name": "render", "inputSchema": {}}]}}
    state.of("video-tools").asleep = True
    msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}}
    answer = _answer_of(asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], msg)))), 3)
    assert answer["error"]["code"] == -32602
    assert rig["wakes"] == []


# --- the first call ------------------------------------------------------------


def test_a_call_wakes_the_sleeping_tool_and_answers(rig):
    rig["tool"].running = False
    state.of("video-tools").asleep = True

    async def scenario():
        return _answer_of(await _body(await _post("s1", rig["cred"], _call())), 7)

    answer = asyncio.run(scenario())
    assert answer["result"]["content"][0]["text"] == "done"
    assert len(rig["wakes"]) == 1
    assert not state.of("video-tools").asleep
    assert state.of("video-tools").in_flight == 0


def test_two_calls_at_once_start_the_tool_once(rig):
    rig["tool"].running = False
    rig["state"]["wake_s"] = 0.3
    state.of("video-tools").asleep = True

    async def scenario():
        answers = await asyncio.gather(*[
            _body(await _post(sid, rig["cred"], _call(i))) for i, sid in ((1, "s1"), (2, "s2"))])
        return [_answer_of(raw, i) for raw, i in zip(answers, (1, 2), strict=True)]

    answers = asyncio.run(scenario())
    assert all(a["result"]["content"][0]["text"] == "done" for a in answers)
    assert len(rig["wakes"]) == 1


def test_a_container_stopped_by_hand_is_woken_at_its_first_call(rig):
    rig["tool"].running = False

    answer = _answer_of(asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call())))), 7)
    assert answer["result"]["content"][0]["text"] == "done"
    assert len(rig["wakes"]) == 1


def test_a_start_past_the_budget_says_it_is_still_starting(rig, monkeypatch):
    monkeypatch.setattr(door, "WAKE_BUDGET_S", 0.2)
    monkeypatch.setattr(door, "KEEPALIVE_S", 0.05)
    rig["tool"].running = False
    rig["state"]["wake_s"] = 0.6
    state.of("video-tools").asleep = True

    raw = asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call()))))
    answer = _answer_of(raw, 7)
    assert answer["result"]["isError"] is True
    assert answer["result"]["content"][0]["text"] == "The Video Tools tool is still starting. Try again in a minute."
    assert b": keepalive" in raw


@pytest.mark.parametrize("why, sentence", [
    ("compose failed", "The Video Tools tool could not start. An admin can see why on the MCP page."),
    ("Error: no such image: video-tools:1",
     "The Video Tools tool needs its image again. An admin can press Start on the MCP page."),
])
def test_a_failed_start_answers_its_sentence(rig, why, sentence):
    rig["tool"].running = False
    rig["state"]["wake_fails"] = why
    state.of("video-tools").asleep = True

    answer = _answer_of(asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call())))), 7)
    assert answer["result"]["content"][0]["text"] == sentence
    assert state.of("video-tools").failed == (state.NO_IMAGE if "image" in why else state.FAILED)
    assert state.of("video-tools").in_flight == 0


def test_a_long_call_carries_keepalives_until_its_answer(rig, monkeypatch):
    monkeypatch.setattr(door, "KEEPALIVE_S", 0.05)
    rig["tool"].call_delay = 0.3

    raw = asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call()))))
    assert raw.count(b": keepalive") >= 3
    assert _answer_of(raw, 7)["result"]["content"][0]["text"] == "done"


def test_a_call_in_flight_is_use_until_its_answer_ends(rig, monkeypatch):
    rig["tool"].call_delay = 0.2

    async def scenario():
        answered = await _post("s1", rig["cred"], _call())
        before = state.in_use("video-tools")
        chunks = answered.aiter_raw()
        await chunks.__anext__()
        during = state.in_use("video-tools")
        async for _chunk in chunks:
            pass
        return before, during, state.in_use("video-tools")

    before, during, after = asyncio.run(scenario())
    assert not before and during and not after


# --- a server that keeps sessions --------------------------------------------


def test_the_door_holds_a_stateful_server_and_answers_its_pings(rig):
    rig["tool"].stateful = True

    async def scenario():
        await _body(await _post("s1", rig["cred"], _call(1)))
        await _body(await _post("s1", rig["cred"], _call(2)))
        for _ in range(50):
            if rig["tool"].replies:
                break
            await asyncio.sleep(0.01)
        bound = state.in_use("video-tools")
        door.end_session("s1")
        await asyncio.sleep(0.05)
        return bound

    bound = asyncio.run(scenario())
    assert rig["tool"].inits == 1
    assert {c["session"] for c in rig["tool"].calls} == {"srv-1"}
    assert rig["tool"].replies == [{"jsonrpc": "2.0", "id": "p1", "result": {}}]
    assert bound
    assert rig["tool"].deletes == ["srv-1"]
    assert not state.in_use("video-tools")


def test_a_session_whose_server_session_ended_hears_the_pages_closed(rig):
    rig["tool"].stateful = True

    async def scenario():
        await _body(await _post("s1", rig["cred"], _call(1)))
        rig["tool"].end_stream.set()
        for _ in range(50):
            if not door._door.bindings:
                break
            await asyncio.sleep(0.01)
        rig["tool"].end_stream = asyncio.Event()
        second = _answer_of(await _body(await _post("s1", rig["cred"], _call(2))), 2)
        third = _answer_of(await _body(await _post("s1", rig["cred"], _call(3))), 3)
        door.end_session("s1")
        return second, third

    second, third = asyncio.run(scenario())
    assert second["result"]["content"][0]["text"].startswith("The Video Tools tool was restarted")
    assert second["result"]["content"][1]["text"] == "done"
    assert third["result"]["content"][0]["text"] == "done"


def test_the_engine_sees_a_stateless_server(rig):
    rig["tool"].stateful = True

    async def scenario():
        got = await door.step("s1", "video-tools", rig["cred"], "GET", "/mcp/", b"", [], b"")
        call = await _post("s1", rig["cred"], _call())
        headers = dict(call.headers)
        await _body(call)
        door.end_session("s1")
        return got.status_code, headers

    status, headers = asyncio.run(scenario())
    assert status == 405
    assert "mcp-session-id" not in {k.lower() for k in headers}


def test_a_session_held_before_a_restart_hears_the_pages_closed(rig, tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    (tmp_path / "container-sessions.json").write_text(json.dumps([["s1", "video-tools"]]))
    assert door.load_held() == 1
    # Taking a session back after a restart runs its close bookkeeping: the
    # mark stays for its next call.
    door.end_session("s1")
    answer = _answer_of(asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call())))), 7)
    assert answer["result"]["content"][0]["text"].startswith("The Video Tools tool was restarted")


# --- the platform's own dials ----------------------------------------------------


def test_ready_wakes_a_sleeping_tool_for_a_platform_dial(rig):
    rig["tool"].running = False
    state.of("video-tools").asleep = True
    assert asyncio.run(door.ready(rig["manifest"])) == ""
    assert len(rig["wakes"]) == 1


def test_hold_is_use_for_its_block(rig):
    with door.hold(rig["manifest"]):
        assert state.in_use("video-tools")
    assert not state.in_use("video-tools")


# --- the idle sweep --------------------------------------------------------------


@pytest.fixture
def sweep(rig, monkeypatch):
    import config
    from services.mcp import mcp_updater
    monkeypatch.setattr(config, "OTODOCK_TOOLS_IDLE_MINUTES", 15)
    monkeypatch.setattr(idle, "_enabled_tools", lambda: [rig["manifest"]])
    monkeypatch.setattr(idle, "_reconcile", lambda manifests: None)
    monkeypatch.setattr(idle, "_direct_dials", lambda manifests: {})
    monkeypatch.setattr(mcp_updater, "update_running", lambda name: False)
    state.of("video-tools").last_use = time.monotonic() - 16 * 60
    return rig


def test_a_quiet_tool_sleeps_after_the_spell(sweep):
    assert asyncio.run(idle.sweep_once()) == ["video-tools"]
    assert state.of("video-tools").asleep
    assert sweep["sleeps"] == ["video-tools"]


def test_a_tool_used_inside_the_spell_keeps_running(sweep):
    state.of("video-tools").last_use = time.monotonic() - 14 * 60
    assert asyncio.run(idle.sweep_once()) == []


def test_a_call_in_flight_or_a_bound_session_keeps_it_running(sweep):
    state.of("video-tools").in_flight = 1
    assert asyncio.run(idle.sweep_once()) == []
    state.of("video-tools").in_flight = 0
    state.of("video-tools").bound.add("s1")
    assert asyncio.run(idle.sweep_once()) == []
    assert sweep["sleeps"] == []


@pytest.fixture
def busy(sweep, monkeypatch):
    """The tool declares a busy endpoint; ``answers`` is what it says."""
    answers: list = []
    monkeypatch.setattr(sweep["manifest"].server, "busy_endpoint", "/busy", raising=False)
    monkeypatch.setattr(sweep["manifest"].server, "tool_timeout_sec", 3600, raising=False)

    async def ask(manifest):
        answers.append(manifest.name)
        return sweep["state"].get("busy", False)
    monkeypatch.setattr(idle, "_still_working", ask)
    sweep["answers"] = answers
    return sweep


def test_a_tool_that_says_it_is_working_stays_awake_within_its_bound(busy):
    busy["state"]["busy"] = True
    assert asyncio.run(idle.sweep_once()) == []
    assert busy["answers"] == ["video-tools"] and busy["sleeps"] == []
    held = state.of("video-tools").held_since
    assert held
    # Still within its declared hour: kept, the hold's start unchanged.
    assert asyncio.run(idle.sweep_once(now=held + 59 * 60)) == []
    assert state.of("video-tools").held_since == held
    # Past it: put to sleep anyway, the hold cleared.
    assert asyncio.run(idle.sweep_once(now=held + 61 * 60)) == ["video-tools"]
    assert state.of("video-tools").held_since == 0.0


def test_a_tool_that_says_no_or_does_not_answer_sleeps(busy):
    busy["state"]["busy"] = False
    assert asyncio.run(idle.sweep_once()) == ["video-tools"]


def test_a_use_ends_a_hold_and_a_use_during_the_ask_keeps_it(busy, monkeypatch):
    busy["state"]["busy"] = True
    assert asyncio.run(idle.sweep_once()) == []
    state.of("video-tools").last_use = time.monotonic()
    assert asyncio.run(idle.sweep_once()) == []
    assert state.of("video-tools").held_since == 0.0

    async def used_meanwhile(manifest):
        state.of("video-tools").last_use = time.monotonic() - 16 * 60 + 1
        return False
    state.of("video-tools").last_use = time.monotonic() - 16 * 60
    monkeypatch.setattr(idle, "_still_working", used_meanwhile)
    assert asyncio.run(idle.sweep_once()) == []


def test_a_no_a_start_and_a_stop_each_end_a_hold(busy, monkeypatch):
    """The Admin row reads the hold: it ends when the tool says no (even if
    a compose run then keeps it from sleeping), and at any start, restart,
    update or stop of its container."""
    busy["state"]["busy"] = True
    assert asyncio.run(idle.sweep_once()) == []
    assert state.of("video-tools").held_since
    busy["state"]["busy"] = False
    monkeypatch.setattr(idle, "_sleep_if_free", lambda manifest: False)
    assert asyncio.run(idle.sweep_once()) == []
    assert state.of("video-tools").held_since == 0.0
    for end in (state.started, state.removed):
        state.of("video-tools").held_since = 5.0
        end("video-tools")
        assert state.of("video-tools").held_since == 0.0


def test_an_admins_stop_ends_a_hold(monkeypatch):
    """The admin's Stop puts the tool to sleep through the same stop the
    sweep uses: the hold ends there too, so its row reads no job."""
    import subprocess
    from types import SimpleNamespace

    from services.mcp import docker_manager
    manifest = SimpleNamespace(name="video-tools", mcp_dir=Path("/tmp"))
    monkeypatch.setattr(docker_manager, "_target_service", lambda m: "video-tools")
    monkeypatch.setattr(docker_manager, "_compose_cmd", lambda m, *a: ["true"])
    monkeypatch.setattr(docker_manager, "_compose_env", lambda: {})
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stderr=""))
    state.of("video-tools").held_since = 5.0
    assert lifecycle.sleep_container(manifest) is True
    assert state.of("video-tools").held_since == 0.0


def test_a_tool_that_declares_no_busy_endpoint_is_never_asked(sweep, monkeypatch):
    async def ask(manifest):
        raise AssertionError("never asked")
    monkeypatch.setattr(idle, "_still_working", ask)
    assert asyncio.run(idle.sweep_once()) == ["video-tools"]


def test_the_hold_bound_is_the_tools_own_call_limit():
    from types import SimpleNamespace
    assert idle.hold_bound_s(SimpleNamespace(server=SimpleNamespace(tool_timeout_sec=600))) == 600
    assert idle.hold_bound_s(SimpleNamespace(server=SimpleNamespace(tool_timeout_sec=0))) == idle.HOLD_DEFAULT_S


def test_the_admin_row_says_how_long_a_working_tool_stays_awake(busy):
    from api.mcp import mcp_containers
    assert mcp_containers.row_facts(busy["manifest"], set())["docker_working_minutes"] == 0
    busy["state"]["busy"] = True
    asyncio.run(idle.sweep_once())
    assert mcp_containers.row_facts(busy["manifest"], set())["docker_working_minutes"] == 60


def _ask_with(monkeypatch, handler):
    """``_still_working`` against a stand-in tool on the client's transport."""
    from types import SimpleNamespace

    from core.credentials import mcp_gateway
    from services.mcp import mcp_registry
    manifest = SimpleNamespace(name="video-tools", server=SimpleNamespace(busy_endpoint="/busy"))
    monkeypatch.setattr(mcp_registry, "resolve_server_config",
                        lambda m, account, mcp_config_format: {"url": "http://127.0.0.1:8933/mcp"})
    monkeypatch.setattr(mcp_gateway, "client",
                        lambda proxy_local: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asyncio.run(idle._still_working(manifest))


def test_the_ask_reads_the_tools_answer(monkeypatch):
    """The ask goes to the tool's own port and believes only ``busy: true``."""
    seen: list = []
    for body, want in ((b'{"busy": true}', True), (b'{"busy": "yes"}', False), (b"{}", False),
                       (b"[true]", False), (b"not json", False)):
        def handler(request, body=body):
            seen.append(str(request.url))
            return httpx.Response(200, content=body)
        assert _ask_with(monkeypatch, handler) is want
    assert seen[0] == "http://127.0.0.1:8933/busy"

    def down(request):
        raise httpx.ConnectError("down")
    assert _ask_with(monkeypatch, down) is False
    assert _ask_with(monkeypatch, lambda request: httpx.Response(503, content=b'{"busy": true}')) is False


def test_a_tool_that_trickles_its_answer_is_a_no_within_the_bound(monkeypatch):
    """One byte a second never trips a per-read timeout: the whole ask is
    bounded, so one slow tool cannot hold the sweep or keep itself awake."""
    monkeypatch.setattr(idle, "ASK_TIMEOUT_S", 0.3)

    async def trickle():
        for byte in b'{"busy": true}':
            await asyncio.sleep(0.2)
            yield bytes([byte])
    began = time.monotonic()
    assert _ask_with(monkeypatch, lambda request: httpx.Response(200, content=trickle())) is False
    assert time.monotonic() - began < 2.0


def test_a_long_answer_is_a_no(monkeypatch):
    """The answer is read up to 1 KB: a longer one is a no, never buffered."""
    pad = b'{"busy": true, "pad": "' + b"x" * 2048 + b'"}'
    assert _ask_with(monkeypatch, lambda request: httpx.Response(200, content=pad)) is False
    short = b'{"busy": true, "pad": "' + b"x" * 900 + b'"}'
    assert _ask_with(monkeypatch, lambda request: httpx.Response(200, content=short)) is True


def test_a_warm_tool_never_sleeps(sweep, monkeypatch):
    from storage.mcp import mcp_store
    sweep["manifest"].server.starts = "with_platform"
    assert asyncio.run(idle.sweep_once()) == []
    sweep["manifest"].server.starts = ""
    monkeypatch.setattr(mcp_store, "get_keep_running", lambda: {"video-tools"})
    assert asyncio.run(idle.sweep_once()) == []


def test_no_sleep_while_a_compose_run_holds_the_tool_or_an_update_waits(sweep, monkeypatch):
    import threading

    from services.mcp import mcp_updater
    held = threading.Event()
    release = threading.Event()

    def hold_lock():
        with state.of("video-tools").lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold_lock)
    t.start()
    held.wait(5)
    try:
        assert asyncio.run(idle.sweep_once()) == []
        assert not state.of("video-tools").asleep
    finally:
        release.set()
        t.join()
    monkeypatch.setattr(mcp_updater, "update_running", lambda name: True)
    assert asyncio.run(idle.sweep_once()) == []


def test_no_sleep_while_a_session_from_before_the_start_dials_it_directly(sweep, monkeypatch):
    from core.session import session_state
    monkeypatch.setattr(idle, "_direct_dials", lambda manifests: {"old": frozenset({"video-tools"})})
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: object() if sid == "old" else None)
    assert asyncio.run(idle.sweep_once()) == []
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: None)
    assert asyncio.run(idle.sweep_once()) == ["video-tools"]


def test_a_spell_of_zero_keeps_every_tool_running(sweep, monkeypatch):
    import config
    monkeypatch.setattr(config, "OTODOCK_TOOLS_IDLE_MINUTES", 0)
    assert asyncio.run(idle.sweep_once()) == []


def test_the_door_answers_in_use_for_the_updater(sweep):
    from services.mcp import mcp_registry, mcp_updater
    state.of("video-tools").in_flight = 1
    assert asyncio.run(mcp_updater.mcp_in_use("video-tools")) is True
    state.of("video-tools").in_flight = 0
    assert mcp_registry.get_manifest("video-tools") is not None
    assert asyncio.run(mcp_updater.mcp_in_use("video-tools")) is False


def test_a_wake_waits_for_the_server_behind_a_front_that_answers_first(rig):
    """The browser's front answers before the browser: the wake's probe is a
    handshake, so the call goes on only once the server answers it."""
    rig["tool"].running = False
    rig["tool"].ready = False
    state.of("video-tools").asleep = True

    async def scenario():
        async def ready_later():
            await asyncio.sleep(0.6)
            rig["tool"].ready = True
        later = asyncio.ensure_future(ready_later())
        raw = await _body(await _post("s1", rig["cred"], _call()))
        await later
        return raw

    answer = _answer_of(asyncio.run(scenario()), 7)
    assert answer["result"]["content"][0]["text"] == "done"
    assert len(rig["wakes"]) == 1


def test_a_call_cut_on_its_way_back_is_not_sent_again(rig, monkeypatch):
    """A call the server may have run (its answer cut) answers the sentence
    and is never sent a second time."""
    real = rig["tool"].handler

    async def cut(request):
        if request.method == "POST" and b'"tools/call"' in request.content:
            rig["tool"].calls.append({"cut": True})
            raise httpx.ReadError("cut", request=request)
        return await real(request)
    rig["tool"].handler = cut
    client = httpx.AsyncClient(transport=httpx.MockTransport(cut))
    monkeypatch.setattr(mcp_gateway, "client", lambda *a, **k: client)
    answer = _answer_of(asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call())))), 7)
    assert answer["result"]["content"][0]["text"] == (
        "The Video Tools tool did not answer. Check what it did before trying again.")
    assert len(rig["tool"].calls) == 1
    assert rig["wakes"] == []


def test_a_stop_of_the_platform_keeps_who_held_a_server_session(rig, tmp_path, monkeypatch):
    """A proxy's stop closes every session (its close bookkeeping): the file of
    held server sessions keeps them, so the next start marks them."""
    import config
    from core import drain
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(drain, "stopping", lambda: True)
    rig["tool"].stateful = True

    async def scenario():
        await _body(await _post("s1", rig["cred"], _call(1)))
        await _body(await _post("s2", rig["cred"], _call(2)))
        # The stop cancels the door's tasks, the server streams' holders among
        # them, and closes the sessions.
        door._door.bindings[("s2", "video-tools")].listener.cancel()
        await asyncio.sleep(0.05)
        door.end_session("s1")
        await asyncio.sleep(0.05)
        assert await door._held_writer.written(timeout=30)

    asyncio.run(scenario())
    held = json.loads((tmp_path / "container-sessions.json").read_text())
    assert sorted(held) == [["s1", "video-tools"], ["s2", "video-tools"]]
    door.reset_for_tests()
    assert door.load_held() == 2


# --- the door's place in the gateway, and a tool with a sign-in ---------------


def _ctx(username: str, **kw):
    from auth.path_policy import SecurityContext
    return SecurityContext(role="manager", username=username, agent="studio", is_admin_agent=False,
                           session_scope="user", **kw)


@pytest.fixture
def signed(rig, monkeypatch):
    """The tool behind a sign-in: each session's credential from its own
    token file, its account on its bundle, the gateway's step in front."""
    from types import SimpleNamespace

    from core.credentials import mcp_broker
    from core.credentials.mcp_gateway import Refusal, TokenRef
    from core.session import session_state
    sessions = {"s1": _ctx("alice"), "s2": _ctx("bob"), "s3": _ctx("carol")}
    # The account each session's bundle was built on, and its token file's secret.
    accounts = {"s1": "alice|work", "s2": "bob|work"}
    secrets = {"alice|work": "tok-alice", "bob|work": "tok-bob", "alice|home": "tok-alice-home"}
    creds = {sid: GatewayCredential(upstream="http://tool:8933", path="/mcp/", allowlist_key="video-tools",
                                    proxy_local=True, manifest="video-tools", kind=CONTAINER,
                                    token_ref=TokenRef(token_dir=sid, account_label="work"))
             for sid in sessions}
    steps: list[str] = []
    real_step = door.step

    async def step(session_id, *a, **k):
        steps.append(session_id)
        return await real_step(session_id, *a, **k)
    monkeypatch.setattr(door, "step", step)
    monkeypatch.setattr(session_state, "get_session_security", sessions.get)
    monkeypatch.setattr(mcp_gateway, "credential", lambda sid, mcp: creds.get(sid) if mcp == "video-tools" else None)
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: True)

    def token_secret(ref, name):
        secret = secrets.get(accounts.get(ref.token_dir, ""))
        return (secret, None, ()) if secret else Refusal("no_value", "No sign-in.")
    monkeypatch.setattr(mcp_gateway, "_token_secret", token_secret)
    monkeypatch.setattr(mcp_broker, "get", lambda sid, mcp: SimpleNamespace(account=accounts.get(sid, "")))
    monkeypatch.setattr(door, "_FP_MEMO_S", 0.0)
    rig.update(sessions=sessions, accounts=accounts, steps=steps)
    return rig


def _forward(sid, msg, *, query=b"", headers=()):
    return mcp_gateway.prepare_forward(sid, "video-tools", "POST", "/mcp/", query,
                                       [("Content-Type", "application/json"), *headers], json.dumps(msg).encode())


def _list(rid=2):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/list"}


def test_a_call_with_a_sign_in_wakes_the_sleeping_tool_once_and_reaches_it_with_that_sign_in(signed):
    signed["tool"].running = False
    signed["tool"].call_delay = 0.2
    state.of("video-tools").asleep = True

    async def scenario():
        answered = await _forward("s1", _call())
        chunks = answered.aiter_raw()
        first = await chunks.__anext__()
        during = state.of("video-tools").in_flight
        rest = b"".join([c async for c in chunks])
        return first + rest, during

    raw, during = asyncio.run(scenario())
    assert _answer_of(raw, 7)["result"]["content"][0]["text"] == "done"
    assert len(signed["wakes"]) == 1
    assert during == 1 and state.of("video-tools").in_flight == 0 and not state.in_use("video-tools")
    sent = [r for r in signed["tool"].seen if r["method"] == "POST" and r["rpc"] != "initialize"
            or r["rpc"] == "initialize" and r["sid"]]
    assert sent and all(r["auth"] == "Bearer tok-alice" for r in sent)
    # The record was read with that sign-in, kept in alice's own scope, and
    # holds no credential.
    assert list(signed["scoped"]) == [(("studio", "alice"), "video-tools", "fp1-alice|work")]
    assert "tok-" not in json.dumps(list(signed["scoped"].values())).replace("for-tok-alice", "")
    assert signed["records"] == {("video-tools", "fp1"): _RECORD}


def test_a_person_with_no_sign_in_wakes_nothing(signed):
    signed["tool"].running = False
    state.of("video-tools").asleep = True
    out = asyncio.run(_forward("s3", _call()))
    assert isinstance(out, mcp_gateway.Refusal) and out.reason == "no_value"
    assert signed["steps"] == [] and signed["wakes"] == [] and signed["tool"].seen == []
    assert state.of("video-tools").asleep


def test_one_account_s_record_is_never_served_to_another_and_a_changed_account_reads_again(signed):
    signed["records"].clear()

    async def tools(sid):
        return [t["name"] for t in _answer_of(await _body(await _forward(sid, _list())), 2)["result"]["tools"]]

    async def scenario():
        alice = await tools("s1")
        bob = await tools("s2")
        alice_again = await tools("s1")
        signed["accounts"]["s1"] = "alice|home"
        alice_home = await tools("s1")
        return alice, bob, alice_again, alice_home

    alice, bob, alice_again, alice_home = asyncio.run(scenario())
    assert alice == alice_again == ["render", "for-tok-alice"]
    assert bob == ["render", "for-tok-bob"]
    assert alice_home == ["render", "for-tok-alice-home"]
    assert sorted(signed["scoped"]) == [
        (("studio", "alice"), "video-tools", "fp1-alice|home"),
        (("studio", "alice"), "video-tools", "fp1-alice|work"),
        (("studio", "bob"), "video-tools", "fp1-bob|work")]
    assert signed["records"] == {}


def test_the_sweep_sleeps_a_tool_with_a_sign_in_and_never_under_its_call(signed, monkeypatch):
    import config
    from services.mcp import mcp_updater
    monkeypatch.setattr(config, "OTODOCK_TOOLS_IDLE_MINUTES", 15)
    monkeypatch.setattr(idle, "_enabled_tools", lambda: [signed["manifest"]])
    monkeypatch.setattr(idle, "_reconcile", lambda manifests: None)
    monkeypatch.setattr(idle, "_direct_dials", lambda manifests: {})
    monkeypatch.setattr(mcp_updater, "update_running", lambda name: False)
    signed["tool"].call_delay = 0.2

    async def scenario():
        answered = await _forward("s1", _call())
        chunks = answered.aiter_raw()
        await chunks.__anext__()
        state.of("video-tools").last_use = time.monotonic() - 16 * 60
        during = await idle.sweep_once()
        _ = [c async for c in chunks]
        state.of("video-tools").last_use = time.monotonic() - 16 * 60
        return during, await idle.sweep_once()

    during, after = asyncio.run(scenario())
    assert during == [] and after == ["video-tools"]
    assert signed["sleeps"] == ["video-tools"] and state.of("video-tools").asleep


def test_the_door_comes_after_the_outside_grants_and_the_tool_limits(signed):
    """A function an outside pick or a tool limit leaves off is refused by
    the gateway: the door is never reached, so nothing wakes."""
    signed["tool"].running = False
    state.of("video-tools").asleep = True
    signed["sessions"]["s1"] = _ctx("", principal="external", external_claim="phone:+1",
                                    outside_grants=(("video-tools", ("probe",)),))
    signed["sessions"]["s2"] = _ctx("bob", tool_limits=(("video-tools", ("probe",)),))
    out = asyncio.run(_forward("s1", _call()))
    assert isinstance(out, mcp_gateway.Refusal) and out.reason == "outside_refused"
    out = asyncio.run(_forward("s2", _call()))
    assert isinstance(out, mcp_gateway.Refusal) and out.reason == "limit_refused"
    assert signed["steps"] == [] and signed["wakes"] == [] and signed["tool"].seen == []


def test_the_tool_sees_the_session_of_the_token_only(signed):
    """A query naming another session never reaches the tool: every request
    carries the session the token was verified for, once."""
    raw = asyncio.run(_body(asyncio.run(_forward("s1", _call(), query=b"session_id=other&x=1"))))
    assert _answer_of(raw, 7)["result"]["content"][0]["text"] == "done"
    sent = [r for r in signed["tool"].seen if r["sid"]]
    assert sent and all(r["sids"] == ["s1"] for r in sent)
    assert not any("other" in r["sids"] for r in signed["tool"].seen)


def test_a_changed_fingerprint_reads_the_tool_again(rig, monkeypatch):
    monkeypatch.setattr(door, "_FP_MEMO_S", 0.0)

    async def scenario():
        await _body(await _post("s1", rig["cred"], _init()))
        before = rig["tool"].inits
        rig["state"]["fp"] = "fp2"
        listed = _answer_of(await _body(await _post("s1", rig["cred"], _list())), 2)
        return before, listed

    before, listed = asyncio.run(scenario())
    assert before == 0 and rig["tool"].inits == 1
    assert ("video-tools", "fp2") in rig["records"]
    assert [t["name"] for t in listed["result"]["tools"]] == ["render"]


# --- the callback a tool makes to the proxy -----------------------------------


def test_the_callback_token_is_minted_from_the_request_s_own_token_after_a_restart(rig):
    from auth import session_token
    cred = GatewayCredential(upstream="http://tool:8933", path="/mcp/", allowlist_key="", proxy_local=True,
                             manifest="video-tools", kind=CONTAINER, callbacks=True)
    # Its floor a minute back: the callback carries it, never a fresh one.
    floor = int(time.time()) - 60
    token = session_token.create_session_token("s1", "studio", "user-alice", external="", issued_at=floor)
    # A restart forgets which person each session's tokens were minted for.
    session_token._SESSION_USERS.clear()
    rig["tool"].stateful = True

    async def scenario():
        await _body(await _post("s1", cred, _call(), headers=[("Authorization", f"Bearer {token}")]))
        await _body(await _post("s2", cred, _call(8), headers=[("Authorization", f"Bearer {token}")]))
        for _ in range(50):
            if any(r["method"] == "GET" for r in rig["tool"].seen):
                break
            await asyncio.sleep(0.01)
        door.end_session("s1")
        door.end_session("s2")

    asyncio.run(scenario())
    s1 = [r for r in rig["tool"].seen if r["sid"] == "s1" and r["rpc"] in ("initialize", "tools/call")]
    assert [r["rpc"] for r in s1] == ["initialize", "tools/call"]
    assert all(r["auth"].startswith("Bearer ") for r in s1)
    claims = session_token.validate_callback_token(s1[-1]["auth"].removeprefix("Bearer "))
    assert claims["sid"] == "s1" and claims["user_sub"] == "user-alice" and claims["iat"] == floor
    # A token that is not the session's own adds nothing, and the held stream
    # (its GET, the answers to its pings) carries no callback.
    assert all(r["auth"] == "" for r in rig["tool"].seen if r["sid"] == "s2")
    held = [r for r in rig["tool"].seen if r["method"] == "GET" or (r["method"] == "POST" and not r["rpc"])]
    assert held and all(r["auth"] == "" for r in held)


# --- a call the carrier drops ---------------------------------------------------


@pytest.mark.parametrize("where", ["send", "answer"])
def test_a_dropped_call_closes_its_request_and_gives_back_its_count(rig, monkeypatch, where):
    monkeypatch.setattr(door, "KEEPALIVE_S", 0.05)
    if where == "send":
        rig["tool"].send_delay = 5.0
    else:
        rig["tool"].call_delay = 5.0

    async def scenario():
        answered = await _post("s1", rig["cred"], _call())
        chunks = answered.aiter_raw()
        await chunks.__anext__()
        during = state.of("video-tools").in_flight
        await answered.aclose()
        for _ in range(50):
            if rig["tool"].cancelled or rig["tool"].closed:
                break
            await asyncio.sleep(0.01)
        # Read before the loop's end, which cancels whatever is left running.
        return during, state.of("video-tools").in_flight, rig["tool"].cancelled, rig["tool"].closed

    during, after, cancelled, closed = asyncio.run(scenario())
    assert during == 1 and after == 0
    assert (cancelled, closed) == ((1, 0) if where == "send" else (0, 1))


# --- sessions the door holds ----------------------------------------------------


def test_the_sweep_lets_go_a_session_the_platform_no_longer_knows(sweep, monkeypatch):
    from core.session import session_state
    sweep["tool"].stateful = True
    known = {"s1"}
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: object() if sid in known else None)

    async def scenario():
        await _body(await _post("s1", sweep["cred"], _call()))
        state.of("video-tools").last_use = time.monotonic() - 16 * 60
        held = await idle.sweep_once()
        known.clear()
        gone = await idle.sweep_once()
        await asyncio.sleep(0.05)
        return held, gone

    held, gone = asyncio.run(scenario())
    assert held == [] and gone == ["video-tools"]
    assert not door._door.bindings and sweep["tool"].deletes == ["srv-1"]


def test_a_session_s_own_end_takes_it_off_the_held_file(rig, tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    rig["tool"].stateful = True

    async def scenario():
        await _body(await _post("s1", rig["cred"], _call(1)))
        await _body(await _post("s2", rig["cred"], _call(2)))
        door.end_session("s1")
        await asyncio.sleep(0.05)
        door.end_session("s2")
        await asyncio.sleep(0.05)
        assert await door._held_writer.written(timeout=30)

    asyncio.run(scenario())
    assert json.loads((tmp_path / "container-sessions.json").read_text()) == []


def test_the_direct_dials_of_a_session_from_before_the_door_are_read_from_its_build(rig, monkeypatch, tmp_path):
    """A session taken back at this start whose config names the tool's own
    port holds it awake; one naming the gateway's route does not."""
    import config
    from core.session import session_state
    from services.mcp import mcp_session_config
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(mcp_gateway, "descriptor_dir", lambda: tmp_path / "descriptors")
    (tmp_path / "descriptors").mkdir()
    for sid, url in (("old", "http://127.0.0.1:8933/mcp/"),
                     ("new", mcp_gateway.entry_url("video-tools", "/mcp/")),
                     ("ended", "http://127.0.0.1:8933/mcp/")):
        (tmp_path / "descriptors" / f"{sid}.json").write_text("{}")
        folder = mcp_session_config.session_build_dir(sid)
        folder.mkdir(parents=True)
        (folder / "mcp.json").write_text(json.dumps({"mcpServers": {"video-tools": {"type": "http", "url": url}}}))
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: None if sid == "ended" else object())
    assert idle._direct_dials([rig["manifest"]]) == {"old": frozenset({"video-tools"})}


# --- the hosted rule ------------------------------------------------------------


def test_a_hosted_install_keeps_no_tool_running(sweep, monkeypatch):
    import config
    from api.mcp import mcp_containers
    from storage.mcp import mcp_store
    monkeypatch.setattr(mcp_store, "get_keep_running", lambda: {"video-tools"})
    m = sweep["manifest"]
    assert asyncio.run(idle.sweep_once()) == []
    assert mcp_containers.row_facts(m, {"video-tools"})["docker_keep_running"] is True
    assert lifecycle.stays_warm(m) and lifecycle.starts_at_boot(m)
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    # The switch stored before reads as off: the row, the boot and the sweep.
    assert mcp_containers.row_facts(m, {"video-tools"})["docker_keep_running"] is False
    assert not lifecycle.stays_warm(m) and not lifecycle.starts_at_boot(m)
    assert asyncio.run(idle.sweep_once()) == ["video-tools"]


# --- a call always gets an answer --------------------------------------------------


def _streamed(data: bytes):
    async def gen():
        yield data
    return _Stream(gen())


def _silent(tool="Video Tools"):
    return f"The {tool} tool did not answer. Check what it did before trying again."


def test_a_call_whose_answer_is_cut_midway_reads_did_not_answer(rig):
    """The container killed in a render: the stream ends with no message for
    the call, so the door says so at once, never a wait for the engine's limit."""
    tool = rig["tool"]
    tool.call_answer = lambda msg: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                  stream=_Stream(tool._cut(msg)))
    raw = asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call()))))
    assert b": progress" in raw
    assert _answer_of(raw, 7)["result"]["content"][0]["text"] == _silent()
    assert state.of("video-tools").in_flight == 0


@pytest.mark.parametrize("status, body", [
    (500, b'{"detail": "boom"}'),
    (401, b'{"error": "invalid_token"}'),
    (200, b""),
    (200, b'{"jsonrpc": "2.0", "id": 99, "result": {}}'),
])
def test_a_body_with_no_answer_for_the_call_reads_did_not_answer(rig, status, body):
    """A body that is no JSON-RPC message is never sent on; a JSON-RPC message
    for another id is, and the call still gets its sentence."""
    rig["tool"].call_answer = lambda msg: httpx.Response(
        status, headers={"content-type": "application/json"}, stream=_streamed(body))
    raw = asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call()))))
    assert _answer_of(raw, 7)["result"]["content"][0]["text"] == _silent()
    assert (b'"id": 99' in raw) == (b'"id": 99' in body)
    assert b"boom" not in raw and b"invalid_token" not in raw
    assert rig["wakes"] == []


def test_a_json_rpc_answer_in_a_plain_body_goes_on_as_it_was(rig):
    rig["tool"].call_answer = lambda msg: httpx.Response(200, headers={"content-type": "application/json"},
                                                         stream=_streamed(json.dumps({
                                                             "jsonrpc": "2.0", "id": msg["id"],
                                                             "result": {"content": [{"type": "text",
                                                                                     "text": "plain"}]}}).encode()))
    raw = asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _call()))))
    assert _answer_of(raw, 7)["result"]["content"][0]["text"] == "plain"
    assert _silent().encode() not in raw


# --- a refused sign-in is a refusal, never a sleeping tool -----------------------------------


def _refusal_text(raw: bytes, rid=7):
    answer = _answer_of(raw, rid)
    return answer["result"]["content"][0]["text"] if "result" in answer else answer["error"]["message"]


def test_a_call_the_server_refuses_for_its_sign_in_reads_the_gateways_sentence(signed):
    signed["tool"].call_answer = lambda msg: httpx.Response(
        401, headers={"content-type": "application/json"}, stream=_streamed(b'{"error": "invalid_token"}'))
    raw = asyncio.run(_body(asyncio.run(_forward("s1", _call()))))
    assert _refusal_text(raw) == mcp_gateway._REFUSAL_VENDOR_401
    assert signed["wakes"] == [] and not state.of("video-tools").asleep


def test_a_handshake_that_refuses_the_sign_in_wakes_nothing(signed):
    """The session's own handshake answered 401: no wake, no second bind,
    the gateway's sentence; with no record yet the engine's handshake reads
    it too."""
    signed["tool"].init_status = 401
    signed["scoped"][(("studio", "alice"), "video-tools", "fp1-alice|work")] = _RECORD
    raw = asyncio.run(_body(asyncio.run(_forward("s1", _call()))))
    assert _refusal_text(raw) == mcp_gateway._REFUSAL_VENDOR_401
    assert signed["wakes"] == [] and not state.of("video-tools").asleep
    binds = [r for r in signed["tool"].seen if r["rpc"] == "initialize" and r["sid"]]
    assert len(binds) == 1
    init = asyncio.run(_body(asyncio.run(_forward("s2", _init()))))
    assert _refusal_text(init, 1) == mcp_gateway._REFUSAL_VENDOR_401
    assert signed["wakes"] == []
    assert not [k for k in signed["scoped"] if k[0] == ("studio", "bob")]


# --- the first read, the start, the stream, the orphans, the records ---------------------------


def test_a_first_read_still_under_way_says_the_tool_is_starting(rig, monkeypatch):
    """The wake is held until the first read and its capture have given up;
    the next read comes once the wake ended, with a session's own budget."""
    budget = door.CAPTURE_BUDGET_S
    monkeypatch.setattr(door, "CAPTURE_BUDGET_S", 0.1)
    rig["records"].clear()
    rig["tool"].running = False
    state.of("video-tools").asleep = True
    released = threading.Event()
    wake = lifecycle.wake_container
    monkeypatch.setattr(lifecycle, "wake_container", lambda m: released.wait(30) and wake(m))

    async def scenario():
        first = _answer_of(await _body(await _post("s1", rig["cred"], _init())), 1)
        await asyncio.gather(*door._door.captures.values())
        failed_while_starting = state.of("video-tools").failed
        released.set()
        await door._door.wakes["video-tools"]
        monkeypatch.setattr(door, "CAPTURE_BUDGET_S", budget)
        second = _answer_of(await _body(await _post("s2", rig["cred"], _init())), 1)
        return first, failed_while_starting, second

    first, failed_while_starting, second = asyncio.run(scenario())
    assert first["error"]["message"] == "The Video Tools tool is still starting. Try again in a minute."
    assert failed_while_starting == "" and state.of("video-tools").failed == ""
    assert second["result"]["serverInfo"]["name"] == "video-tools"


def test_a_slow_start_that_worked_is_not_marked_failed(rig, monkeypatch):
    """The probe's time counts from compose's return: a compose run longer
    than the wake's budget that ended well leaves no failed mark."""
    monkeypatch.setattr(door, "WAKE_BUDGET_S", 0.2)
    rig["tool"].running = False
    rig["state"]["wake_s"] = 0.4
    state.of("video-tools").asleep = True
    assert asyncio.run(door._run_wake(rig["manifest"])) == ""
    assert state.of("video-tools").failed == "" and not state.of("video-tools").asleep


def test_a_server_that_offers_no_stream_keeps_its_session(rig):
    rig["tool"].stateful = True
    rig["tool"].get_status = 405

    async def scenario():
        answers = []
        for rid in (1, 2, 3):
            answers.append(_answer_of(await _body(await _post("s1", rig["cred"], _call(rid))), rid))
            await asyncio.sleep(0.02)
        bound = state.in_use("video-tools")
        door.end_session("s1")
        await asyncio.sleep(0.05)
        return answers, bound

    answers, bound = asyncio.run(scenario())
    assert rig["tool"].inits == 1 and bound
    assert all(a["result"]["content"][0]["text"] == "done" for a in answers)
    assert rig["tool"].deletes == ["srv-1"]


def test_a_session_gone_without_its_close_is_let_go_with_the_spell_at_zero(sweep, monkeypatch):
    import config
    from core.session import session_state
    monkeypatch.setattr(config, "OTODOCK_TOOLS_IDLE_MINUTES", 0)
    sweep["tool"].stateful = True
    known = {"s1"}
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: object() if sid in known else None)

    async def scenario():
        await _body(await _post("s1", sweep["cred"], _call()))
        held = dict(door._door.bindings)
        known.clear()
        await idle.sweep_once()
        await asyncio.sleep(0.05)
        return held

    held = asyncio.run(scenario())
    assert held and not door._door.bindings
    assert sweep["tool"].deletes == ["srv-1"]


def test_the_records_in_memory_are_one_per_tool_and_scope_and_bounded(rig, monkeypatch):
    monkeypatch.setattr(door, "_FP_MEMO_S", 0.0)
    rig["records"].clear()

    async def scenario():
        await _body(await _post("s1", rig["cred"], _init()))
        rig["state"]["fp"] = "fp2"
        await _body(await _post("s1", rig["cred"], _init()))
        return dict(door._door.records), dict(door._door.captures)

    records, captures = asyncio.run(scenario())
    assert list(records) == [("video-tools", None, "fp2")]
    assert captures == {}
    assert door._RECORDS_MAX == 256


def test_a_record_the_store_does_not_keep_is_held_in_memory_and_says_so(rig, monkeypatch, caplog):
    import logging

    from services.mcp import tool_listing
    monkeypatch.setattr(tool_listing, "keep_platform", lambda *a: False)
    rig["records"].clear()
    with caplog.at_level(logging.WARNING, logger="otodock.containers"):
        answer = _answer_of(asyncio.run(_body(asyncio.run(_post("s1", rig["cred"], _init())))), 1)
    assert answer["result"]["serverInfo"]["name"] == "video-tools"
    assert any("held in memory only" in r.getMessage() for r in caplog.records)


def test_the_row_reads_the_switch_from_the_set_it_is_given(rig, monkeypatch):
    from api.mcp import mcp_containers
    from storage.mcp import mcp_store

    def no_read():
        raise AssertionError("the row read the store")
    monkeypatch.setattr(mcp_store, "get_keep_running", no_read)
    assert mcp_containers.row_facts(rig["manifest"], {"video-tools"})["docker_keep_running"] is True
    assert mcp_containers.row_facts(rig["manifest"], set())["docker_keep_running"] is False


def test_a_session_s_end_writes_the_held_file_off_the_loop(rig, tmp_path, monkeypatch, loop_disk_guard):
    """A close ends the session's server sessions on the loop: the held file
    is encoded there and written on its own thread."""
    import config
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    rig["tool"].stateful = True

    held_file = tmp_path / "container-sessions.json"

    def reads(want) -> bool:
        return held_file.exists() and json.loads(held_file.read_text()) == want

    async def until(want) -> None:
        for _ in range(1000):
            if reads(want):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"the held file never read {want}")

    async def scenario():
        await _body(await _post("s1", rig["cred"], _call(1)))
        await until([["s1", "video-tools"]])
        with loop_disk_guard.active():
            door.end_session("s1")
        await until([])

    asyncio.run(scenario())
