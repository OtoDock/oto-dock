"""The link's message loop routes every platform frame the way it always has.

Pinned on the if/elif chain before it became a dispatch table: for each
frame type, the session manager's method the loop starts, the arguments it
passes, the task's name and owner, and the frames the loop answers inline
(the policy, the gateway, the update and the uninstall, the otodock
command's replies, the tunnel) with their order and their refusals.
"""

import asyncio
import json
import logging
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from satellite.transport import ws_client

pytestmark = pytest.mark.asyncio

# frame type -> (session manager method, whether the client is passed)
_SPAWNED = {
    "start_session": ("start_session", True),
    "send_message": ("send_message", True),
    "abort": ("abort", True),
    "interrupt_turn": ("interrupt_turn", False),
    "close_session": ("close_session", True),
    "pty_open": ("pty_open", True),
    "pty_input": ("pty_input", False),
    "pty_resize": ("pty_resize", False),
    "pty_close": ("pty_close", False),
    "pty_inject": ("pty_inject", True),
    "pty_local_detach": ("evict_local_conn", True),
    "stop_turn": ("stop_turn", False),
    "control_response": ("control_response", False),
    "control_request": ("control_request", False),
    "credentials_update": ("credentials_update", True),
    "check_session_resumable": ("check_session_resumable", True),
    "codex_compact": ("codex_compact", True),
    "steer_turn": ("steer_turn", True),
    "codex_steer": ("steer_turn", True),
    "codex_bg_terminals": ("codex_bg_terminals", True),
    "check_session_process": ("check_session_process", True),
    "resume_session_stream": ("resume_session_stream", True),
    "request_manifest": ("request_manifest", True),
    "file_push": ("file_push", True),
    "file_stat": ("file_stat", True),
    "session_files_retention": ("session_files_retention", True),
    "sync_mcps": ("sync_mcps", True),
    "sync_mcps_verify": ("sync_mcps_verify", True),
    "step_run": ("step_run", True),
}


class _Inbound:
    """The link's socket as the loop reads it: the frames, then the end."""

    def __init__(self, frames):
        self._frames = [f if isinstance(f, (str, bytes)) else json.dumps(f) for f in frames]
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _SM:
    """A session manager that records every call the loop makes."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._pulls: dict = {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        async def _method(*args):
            self.calls.append((name, args))

        return _method

    def file_pull_cancel(self, msg, client):
        self.calls.append(("file_pull_cancel", (msg, client)))


@pytest.fixture
def spawned(monkeypatch):
    seen: list[tuple] = []
    real = ws_client.spawn

    def _spawn(coro, *, name, owner):
        seen.append((name, owner))
        return real(coro, name=name, owner=owner)

    monkeypatch.setattr(ws_client, "spawn", _spawn)
    return seen


def _client(sm, *, insecure=False):
    c = ws_client.SatelliteWSClient(types.SimpleNamespace(), sm)
    c._authenticated = True
    c._insecure_link = insecure
    return c


async def _drive(c, frames):
    c._ws = _Inbound(frames)
    await c._message_loop()
    for _ in range(5):
        await asyncio.sleep(0)
    return c._ws


async def test_every_spawned_frame_starts_its_method_as_a_task(spawned):
    sm = _SM()
    c = _client(sm)
    frames = [{"type": t, "n": i} for i, t in enumerate(_SPAWNED)]
    await _drive(c, frames)
    assert spawned == [(method, "link") for method, _ in _SPAWNED.values()]
    assert [name for name, _ in sm.calls] == [method for method, _ in _SPAWNED.values()]
    for (name, args), frame, (_, with_client) in zip(sm.calls, frames, _SPAWNED.values(), strict=True):
        assert args[0] == frame, name
        assert args[1:] == ((c,) if with_client else ()), name


async def test_a_pull_is_registered_in_the_step_that_reads_it(spawned):
    sm = _SM()
    gate = asyncio.Event()

    async def _file_pull(msg, client):
        sm.calls.append(("file_pull", (msg, client)))
        await gate.wait()

    sm.file_pull = _file_pull
    c = _client(sm)
    c._ws = _Inbound([{"type": "file_pull", "request_id": "r1"},
                      {"type": "file_pull", "request_id": ""}])
    await c._message_loop()
    # Registered before the pull's task ran a step: a cancel read in the same
    # batch finds it. A pull with no id is not registered.
    assert list(sm._pulls) == ["r1"] and not sm._pulls["r1"].done()
    assert spawned == [("file_pull", "link"), ("file_pull", "link")]
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert sm._pulls == {}
    assert [args[0]["request_id"] for _, args in sm.calls] == ["r1", ""]


async def test_a_pull_cancel_is_answered_inline(spawned):
    sm = _SM()
    c = _client(sm)
    await _drive(c, [{"type": "file_pull_cancel", "request_id": "r1"}])
    assert spawned == []
    assert sm.calls == [("file_pull_cancel", ({"type": "file_pull_cancel", "request_id": "r1"}, c))]


async def test_a_policy_update_is_stored_inline(spawned, monkeypatch):
    from satellite.host import satellite_policy
    got = []
    monkeypatch.setattr(satellite_policy, "set_policy", got.append)
    await _drive(_client(_SM()), [{"type": "policy_update", "policy": {"allow_full_fs": True}},
                                  {"type": "policy_update"}])
    assert got == [{"allow_full_fs": True}, {}]
    assert spawned == []


async def test_the_gateway_without_a_tunnel_answers_an_error(spawned):
    c = _client(_SM())
    sent = []

    async def _enqueue(frame):
        sent.append(frame)

    c.enqueue_send = _enqueue
    c.tunnel = None
    await _drive(c, [{"type": "mcp_gateway_token", "command_id": "c1"}, {"type": "mcp_gateway_wipe"}])
    assert sent == [{"type": "ack", "command_id": "c1", "status": "error",
                     "error": "gateway unavailable or frame incomplete"}]


async def test_the_gateway_frames_are_acknowledged_inline(spawned):
    table = types.SimpleNamespace(pushed=[], wiped=[])
    table.push = lambda m: table.pushed.append(m["command_id"]) or m.get("ok", True)
    table.wipe = lambda m: table.wiped.append(m.get("command_id"))
    c = _client(_SM())
    c.tunnel = types.SimpleNamespace(gateway=types.SimpleNamespace(table=table))
    sent = []

    async def _enqueue(frame):
        sent.append(frame)

    c.enqueue_send = _enqueue
    await _drive(c, [{"type": "mcp_gateway_token", "command_id": "a"},
                     {"type": "mcp_gateway_token", "command_id": "b", "ok": False},
                     {"type": "mcp_gateway_wipe", "command_id": "c"},
                     {"type": "mcp_gateway_wipe"}])
    assert table.pushed == ["a", "b"] and table.wiped == ["c", None]
    assert spawned == []
    assert sent == [
        {"type": "ack", "command_id": "a", "status": "ok"},
        {"type": "ack", "command_id": "b", "status": "error",
         "error": "gateway unavailable or frame incomplete"},
        {"type": "ack", "command_id": "c", "status": "ok"},
    ]


async def test_update_and_uninstall_start_their_tasks_on_a_secure_link(spawned, monkeypatch):
    order = []

    async def _update(msg):
        order.append(("applied", msg["target_version"]))

    async def _uninstall():
        order.append(("uninstalled",))

    async def _ack(self, msg):
        order.append(("acked", msg["target_version"]))

    monkeypatch.setattr(ws_client, "_self_update_and_restart", _update)
    monkeypatch.setattr(ws_client, "_self_uninstall_and_exit", _uninstall)
    monkeypatch.setattr(ws_client.SatelliteWSClient, "_acknowledge_update", _ack)
    await _drive(_client(_SM()), [{"type": "update_required", "target_version": "9.9.9"},
                                  {"type": "uninstall"}])
    assert spawned == [("update", "link"), ("uninstall", "link")]
    assert order == [("acked", "9.9.9"), ("applied", "9.9.9"), ("uninstalled",)]


async def test_update_and_uninstall_are_refused_on_a_plaintext_link(spawned, monkeypatch):
    refused = []
    monkeypatch.setattr(ws_client.SatelliteWSClient, "_log_insecure_refusal",
                        lambda self, what: refused.append(what))

    async def _never(*a):
        raise AssertionError("applied over a plaintext link")

    monkeypatch.setattr(ws_client.SatelliteWSClient, "_acknowledge_update", _never)
    await _drive(_client(_SM(), insecure=True), [{"type": "update_required"}, {"type": "uninstall"}])
    assert refused == ["update_required", "uninstall"]
    assert spawned == []


async def test_the_otodock_command_replies_resolve_their_futures(spawned):
    c = _client(_SM())
    loop = asyncio.get_running_loop()
    futs = {rid: loop.create_future() for rid in ("a", "b", "c", "done")}
    futs["done"].set_result("earlier")
    c._local_session_futures = dict(futs)
    await _drive(c, [
        {"type": "local_session_opened", "request_id": "a"},
        {"type": "local_session_error", "request_id": "b"},
        {"type": "local_session_listed", "request_id": "c"},
        {"type": "local_session_opened", "request_id": "done"},
        {"type": "local_session_opened", "request_id": "unknown"},
    ])
    assert futs["a"].result()["type"] == "local_session_opened"
    assert futs["b"].result()["type"] == "local_session_error"
    assert futs["c"].result()["type"] == "local_session_listed"
    assert futs["done"].result() == "earlier"
    assert c._local_session_futures == {}
    assert spawned == []


async def test_tunnel_responses_are_dispatched_inline(spawned):
    got = []
    c = _client(_SM())
    c.tunnel = types.SimpleNamespace(dispatch_response=got.append)
    await _drive(c, [{"type": "http_response", "id": 1}, {"type": "http_response_chunk", "id": 2}])
    assert [m["id"] for m in got] == [1, 2]
    c.tunnel = None
    await _drive(c, [{"type": "http_response", "id": 3}])  # no tunnel: dropped
    assert spawned == []


async def test_pong_unknown_and_malformed_frames(spawned, caplog):
    sm = _SM()
    caplog.set_level(logging.DEBUG, logger=ws_client.logger.name)
    await _drive(_client(sm), [{"type": "pong"}, {"type": "no_such_frame"}, {"x": 1},
                               {"type": 5}, {"type": None}, {"type": ["start_session"]},
                               "not json {"])
    assert sm.calls == [] and spawned == []
    unknown = [r for r in caplog.records if r.getMessage().startswith("Unknown message type")]
    assert [r.getMessage() for r in unknown] == [
        "Unknown message type: no_such_frame", "Unknown message type: ", "Unknown message type: 5",
        "Unknown message type: None", "Unknown message type: ['start_session']"]
    assert all(r.levelno == logging.DEBUG for r in unknown)
    assert any(r.getMessage().startswith("Unreadable frame from platform (str, length 10)")
               for r in caplog.records)


async def test_a_failing_frame_is_logged_and_the_loop_goes_on(spawned, caplog, monkeypatch):
    from satellite.host import satellite_policy

    def _boom(policy):
        raise RuntimeError("bad policy")

    monkeypatch.setattr(satellite_policy, "set_policy", _boom)
    sm = _SM()
    await _drive(_client(sm), [{"type": "policy_update", "policy": {}}, {"type": "abort"}])
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        "Error handling policy_update"]
    assert [name for name, _ in sm.calls] == ["abort"]


async def test_a_frame_that_is_not_an_object_is_dropped_and_the_loop_goes_on(spawned, caplog):
    """A slow link's garbage must not cost a reconnect: a list, a JSON
    string, a number and a binary frame that is not UTF-8 are each logged by
    kind and length, never their content, and the frame behind them is
    routed."""
    sm = _SM()
    await _drive(_client(sm), [[1, "secret"], '"secret"', 5, b"\xff\xfe secret",
                               "[" * 100_000 + "]" * 100_000, {"type": "abort"}])
    assert [name for name, _ in sm.calls] == ["abort"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "Frame from platform is not an object (list, length 13)",
        "Frame from platform is not an object (str, length 8)",
        "Frame from platform is not an object (int, length 1)",
        "Unreadable frame from platform (bytes, length 9)",
        "Unreadable frame from platform (str, length 200000)",
    ]
    assert not any("secret" in m for m in warnings)


async def test_a_plaintext_link_still_routes_every_other_frame(spawned):
    sm = _SM()
    c = _client(sm, insecure=True)
    await _drive(c, [{"type": "start_session"}, {"type": "uninstall"}, {"type": "file_push"}])
    assert [name for name, _ in sm.calls] == ["start_session", "file_push"]
    assert spawned == [("start_session", "link"), ("file_push", "link")]


async def test_an_inline_frame_is_answered_before_the_next_frames_task_runs(spawned):
    order = []
    table = types.SimpleNamespace(push=lambda m: order.append("pushed") or True, wipe=lambda m: None)
    sm = _SM()

    async def _start(msg, client):
        order.append("started")

    sm.start_session = _start
    c = _client(sm)
    c.tunnel = types.SimpleNamespace(gateway=types.SimpleNamespace(table=table))

    async def _enqueue(frame):
        order.append(("ack", frame["command_id"]))

    c.enqueue_send = _enqueue
    await _drive(c, [{"type": "start_session"}, {"type": "mcp_gateway_token", "command_id": "g1"}])
    assert order == ["pushed", ("ack", "g1"), "started"]


async def test_a_frame_the_session_manager_cannot_take_is_logged(spawned, caplog):
    sm = types.SimpleNamespace(_pulls={})
    await _drive(_client(sm), [{"type": "start_session"}])
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        "Error handling start_session"]
    assert spawned == []
