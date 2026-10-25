"""The proxy's half of the ``session_move`` frame: the gate on the
capability and the version, a push that names its scope (never a path), a
pull that asks for a session file, and the answers only the asked machine
may give; the ``chat_moved`` broadcast's audience."""

import asyncio
import base64
import hashlib
import json

import pytest

from core.remote.satellite_connection import (
    SatelliteConnection,
    SatelliteConnectionManager,
)
from services.path_policy_v2 import PathRef

pytestmark = pytest.mark.asyncio


class _Wire:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        pass


def _mgr(caps=None, version="0.5.157"):
    mgr = SatelliteConnectionManager()
    wire = _Wire()
    conn = SatelliteConnection(machine_id="m1", ws=wire)
    conn.capabilities = caps if caps is not None else {"session_move": True}
    conn.satellite_version = version
    mgr._connections["m1"] = conn
    conn.writer_task = asyncio.create_task(mgr._writer_loop(conn))
    return mgr, conn, wire


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def _sent(wire, n=1):
    for _ in range(200):
        if len(wire.sent) >= n:
            return wire.sent[n - 1]
        await asyncio.sleep(0.01)
    raise AssertionError("nothing sent")


async def test_the_gate_takes_the_flag_and_the_version():
    mgr, conn, _ = _mgr()
    assert mgr.satellite_supports_session_move("m1")
    conn.capabilities = {}
    assert not mgr.satellite_supports_session_move("m1")
    conn.capabilities = {"session_move": True}
    conn.satellite_version = "0.5.155"
    assert not mgr.satellite_supports_session_move("m1")
    # 0.5.156 flags the capability but resumes a carried Codex thread in the
    # folder its rollout recorded: its moves continue from a summary.
    conn.satellite_version = "0.5.156"
    assert not mgr.satellite_supports_session_move("m1")
    assert not mgr.satellite_supports_session_move("m-unknown")
    conn.writer_task.cancel()


async def test_a_push_carries_the_scope_and_the_chunk_fields(tmp_path):
    mgr, conn, wire = _mgr()
    src = tmp_path / "t.jsonl"
    data = b"x" * 10
    src.write_bytes(data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    fields = {"type": "session_move", "op": "push", "engine": "e", "agent_slug": "a",
              "username": "u", "refs": ["r"], "rel": ".claude/projects/k/r.jsonl", "move_id": "m"}
    task = asyncio.create_task(mgr.push_file("m1", PathRef(kind="agent_tree", value="x"), src,
                                             agent_slug="a", content_hash=digest,
                                             frame_fields=fields))
    frame = await _sent(wire)
    assert frame["type"] == "session_move" and frame["op"] == "push" and "path" not in frame
    assert frame["chunk_index"] == 0 and frame["total_chunks"] == 1 and frame["hash"] == digest
    assert base64.b64decode(frame["content_b64"]) == data
    await mgr.handle_message("m1", {"type": "ack", "command_id": frame["command_id"], "status": "ok"})
    assert await task
    conn.writer_task.cancel()


async def test_a_pull_asks_with_the_request_fields(tmp_path):
    mgr, conn, wire = _mgr()
    dest = tmp_path / "out.jsonl"
    fields = {"type": "session_move", "op": "pull", "rel": ".claude/projects/k/r.jsonl"}
    task = asyncio.create_task(mgr.pull_file_to_path(
        "m1", PathRef(kind="agent_tree", value="x"), dest, agent_slug="a", request_fields=fields))
    req = await _sent(wire)
    assert req["type"] == "session_move" and req["op"] == "pull" and "path" not in req
    data = b"hello"
    # Another machine's answer is ignored; the asked machine's lands.
    await mgr.handle_message("m2", {"type": "file_content", "request_id": req["request_id"],
                                    "error": "forged"})
    await _settle()
    assert not task.done()
    await mgr.handle_message("m1", {
        "type": "file_content", "request_id": req["request_id"], "path": "x", "chunk_index": 0,
        "total_chunks": 1, "content_b64": base64.b64encode(data).decode(),
        "hash": "sha256:" + hashlib.sha256(data).hexdigest()})
    assert await task and dest.read_bytes() == data
    conn.writer_task.cancel()


async def test_only_the_asked_machine_answers_a_command():
    mgr, conn, wire = _mgr()
    task = asyncio.create_task(mgr.send_command("m1", {"type": "session_move", "op": "list"},
                                                timeout=1.0))
    await _settle()
    cid = wire.sent[-1]["command_id"]
    await mgr.handle_message("m2", {"type": "ack", "command_id": cid, "status": "ok",
                                    "files": [{"rel": "evil"}]})
    await _settle()
    assert not task.done()
    await mgr.handle_message("m1", {"type": "ack", "command_id": cid, "status": "ok", "files": []})
    assert (await task)["files"] == []
    conn.writer_task.cancel()


async def test_the_moved_frame_reaches_the_audience_the_mover_and_a_focused_tab(monkeypatch):
    from services.notifications import notification_manager as nm

    class _Q:
        def __init__(self):
            self.items = []

        def put_nowait(self, f):
            self.items.append(f)

    class _C:
        def __init__(self, focus=None):
            self.queue = _Q()
            self.focus = focus

    owner, actor, admin, other = _C(), _C(), _C({"surface": "chat", "chat_id": "c1"}), _C()
    # A tab may SAY it shows the chat: only a person who may read it (the
    # caller's check, ``readers``) gets the frame that way.
    snoop = _C({"surface": "chat", "chat_id": "c1"})
    monkeypatch.setattr(nm, "_user_connections",
                        {"u-owner": [owner], "u-actor": [actor], "u-admin": [admin],
                         "u-other": [other], "u-snoop": [snoop]})
    monkeypatch.setattr(nm, "chat_status_targets", lambda o, a: [o])
    assert nm.chat_focused_subs("c1") == {"u-admin", "u-snoop"}
    nm.broadcast_chat_moved("u-owner", "ag", "u-actor", {"chat_id": "c1", "ok": True},
                            readers={"u-admin"})
    assert owner.queue.items and actor.queue.items and admin.queue.items
    assert not other.queue.items and not snoop.queue.items
    assert owner.queue.items[0]["type"] == "chat_moved"
