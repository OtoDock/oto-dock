"""Characterization of the remote file flows: ``remote_file_flow`` with
``remote_file_cache`` (the host cache, the stat records, the platform-ahead
marks, pull_through, push_back, the listings), the transfer mixins
``satellite_file_transfer`` and ``satellite_file_verbs``, and the sync mixins
``remote_workspace_sync`` and ``remote_workspace_apply``.

The real ``SatelliteConnectionManager`` and its writer run against a fake
machine at the far end of the socket (``_Machine``: a websocket that answers
the frames it is sent), so a golden holds what went over the wire. The clock
is frozen at ``time.monotonic``; contexts, rows and files are real.

A private name of a mixin is read off the method that uses it
(``_sync_name``, ``_pull_name``), which finds it in whichever module holds that
method. A drift is a behaviour change, and the commit that records it says why.
"""
import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from core import background, placement
from core.remote import (
    remote_file_flow as flow,
    remote_workspace_sync as rws,
    satellite_file_transfer as sft,
)
from core.remote.file_sync import FileAction
from core.remote.remote_execution import RemoteExecutionLayer
from core.remote.satellite_connection import (
    SatelliteConnection,
    SatelliteConnectionManager,
)
from services.path_policy_v2 import PathRef
from storage.files import file_author_store, file_tombstones_store, sync_state_store
from storage.pg import get_conn
from tests._descriptor import gone, opened
from tests.fixtures.characterization.golden_master import check_golden

AGENT = "flow-agent"
MACHINE = "sat-one-0"
OTHER = "sat-two-0"
SESSION = "flow-session"
CURRENT = "0.5.170"
T0 = 1_700_000_000
T0_NS = T0 * 1_000_000_000
NOW = 1000.0


def _sync_name(method: str, name: str):
    return getattr(rws.RemoteWorkspaceSyncMixin, method).__globals__[name]


def _pull_name(name: str):
    return sft.SatelliteFileTransferMixin.pull_file_to_path.__globals__[name]


class _Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class _Machine:
    """A satellite at the far end of the socket: the writer's ``send_text``
    lands here, the frame is recorded and answered through the manager's
    ``handle_message``, as the receive loop hands over a real one."""

    def __init__(self, mgr, machine_id=MACHINE, *, version=CURRENT, os_name="linux", chunk=8):
        self.mgr, self.machine_id, self.chunk = mgr, machine_id, chunk
        self.version, self.os_name = version, os_name
        self.frames: list[dict] = []
        self.tree: dict[str, tuple[bytes, int]] = {}
        self.listed_size: dict[str, int] = {}
        self.skipped: list[dict] = []
        self.stale: set[str] = set()
        self.refused: set[str] = set()
        self.no_pull: dict[str, str] = {}
        self.bad_hash: set[str] = set()
        self.stat_fails = False
        self.acks_stat = True
        # Names this machine leaves unwritten and still acks ok, as a
        # Windows machine does for a name with a colon.
        self.not_written: set[str] = set()
        self.commit_ns = 7_000_000_000
        self._chunks: dict[str, bytes] = {}

    def put(self, path: str, data: bytes, mtime_ns: int = T0_NS) -> None:
        self.tree[path] = (data, mtime_ns)

    async def close(self, code=1000, reason=""):
        return None

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        self.frames.append(_wire(frame))
        kind = frame.get("type")
        if kind == "file_pull":
            await self._pull(frame)
        elif kind == "file_push" and frame.get("action") == "delete":
            self.tree.pop(frame["path"], None)
        elif kind == "file_push":
            await self._push(frame)
        elif kind == "file_stat":
            await self._stat(frame)
        elif kind == "request_manifest":
            await self._ack(frame, files=[*self._manifest(), *self.skipped])

    async def _ack(self, frame, *, status="ok", **fields) -> None:
        await self.mgr.handle_message(self.machine_id, {
            "type": "ack", "command_id": frame["command_id"], "status": status,
            "error": "the machine refused" if status == "error" else "", **fields})

    def _manifest(self) -> list[dict]:
        return [{"path": path, "hash": _sha(data), "mtime": ns / 1e9,
                 "size": self.listed_size.get(path, len(data))}
                for path, (data, ns) in sorted(self.tree.items())]

    async def _stat(self, frame) -> None:
        if self.stat_fails:
            await self._ack(frame, status="error")
            return
        held = self.tree.get(frame["path"])
        await self._ack(frame, exists=held is not None,
                        size=len(held[0]) if held else 0, mtime_ns=held[1] if held else 0)

    async def _pull(self, frame) -> None:
        path, rid = frame["path"], frame["request_id"]
        content = {"type": "file_content", "request_id": rid, "path": path}
        if path in self.no_pull or path not in self.tree:
            await self.mgr.handle_message(self.machine_id, {
                **content, "error": self.no_pull.get(path, "File not found")})
            return
        data = self.tree[path][0]
        parts = [data[i:i + self.chunk] for i in range(0, len(data), self.chunk)] or [b""]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            digest = _sha(b"tampered") if path in self.bad_hash else _sha(data)
            await self.mgr.handle_message(self.machine_id, {
                **content, "chunk_index": i, "total_chunks": len(parts),
                "content_b64": base64.b64encode(part).decode(), "hash": digest if last else ""})

    async def _push(self, frame) -> None:
        path = frame["path"]
        if path in self.stale:
            held = self.tree.get(path, (b"", T0_NS))
            await self._ack(frame, status="stale", reason="changed",
                            size=len(held[0]), mtime_ns=held[1])
            return
        if path in self.refused:
            await self._ack(frame, status="error")
            return
        if path in self.not_written:
            await self._ack(frame)
            return
        body = base64.b64decode(frame.get("content_b64") or "")
        if frame["action"] == "write_chunk":
            self._chunks[path] = self._chunks.get(path, b"") + body
            if frame["chunk_index"] < frame["total_chunks"] - 1:
                await self._ack(frame)
                return
            body = self._chunks.pop(path)
        self.tree[path] = (body, self.commit_ns)
        await self._ack(frame, **({"size": len(body), "mtime_ns": self.commit_ns}
                                  if self.acks_stat else {}))


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _wire(frame: dict) -> dict:
    """A frame as the golden holds it: ids named, a body as its text."""
    out = {k: ("<id>" if k in ("command_id", "request_id") else v) for k, v in frame.items()}
    if "content_b64" in out:
        body = base64.b64decode(out.pop("content_b64"))
        out["body"] = body.decode() if len(body) < 80 else f"<{len(body)} bytes>"
    return out


def _by_path(frames: list[dict]) -> list[dict]:
    """A merge's frames, whose order the window leaves open, in a fixed one."""
    return sorted(frames, key=lambda f: json.dumps(f, sort_keys=True))


def _norm(value):
    """Plain JSON values, the run's agents root named."""
    if isinstance(value, dict):
        return {_norm(str(k)): _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_norm(v) for v in value)
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        for root in (str(config.AGENTS_DIR.resolve()), str(config.AGENTS_DIR)):
            value = value.replace(root, "<agents>")
    return value


def _platform(rel: str, data: bytes = b"platform", *, ns: int = T0_NS, agent=AGENT) -> Path:
    target = config.AGENTS_DIR / agent / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    os.utime(target, ns=(ns, ns))
    return target


def _tree(agent=AGENT) -> dict[str, str]:
    root = config.AGENTS_DIR / agent
    return {p.relative_to(root).as_posix(): p.read_bytes().decode()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _username(sub: str, username: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        conn.commit()


def _rows(sql: str, *params) -> list[dict]:
    """Rows in an order of their own (a database's collation sorts paths its way)."""
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return sorted(rows, key=lambda r: (r.get("rel_path", ""), json.dumps(r, sort_keys=True)))


def _run(coro, limit: float = 60.0):
    """``asyncio.run`` under a limit in real time: nothing on the frozen clock
    ever times out, so a machine that stays silent would hang the worker."""
    async def _main():
        loop = asyncio.get_running_loop()
        guard = threading.Timer(limit, loop.call_soon_threadsafe, (asyncio.current_task().cancel,))
        guard.daemon = True
        guard.start()
        try:
            return await coro
        finally:
            guard.cancel()
    return asyncio.run(_main())


def _ctx(*, machine=MACHINE, agent=AGENT, role="manager", username="alice", **kw):
    from auth.path_policy import SecurityContext
    where = (placement.PlacementCapabilities(
        kind=placement.KIND_ADMIN_REMOTE, machine_id=machine,
        agents_dir="/sat/agents", home_dir="/sat") if machine else placement.LOCAL_PLACEMENT)
    return SecurityContext(role=role, username=username, agent=agent, is_admin_agent=False,
                           placement=where, **kw)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """The manager, a layer of this test alone, frozen time and the recorders
    at what the three modules call past their own code."""
    from core.session import session_manager, session_state
    from services.notifications import notification_manager
    from services.remote import workspace_fanout

    monkeypatch.setattr(session_state, "_SECURITY_INDEX", tmp_path / "security_index.json")
    monkeypatch.setattr(session_state, "_session_security", {})
    monkeypatch.setattr(session_state, "_session_security_ts", {})
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock)
    mgr = SatelliteConnectionManager()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: mgr)
    layer = RemoteExecutionLayer(mgr)
    monkeypatch.setattr(session_manager, "_remote_layer", layer)
    noted: dict[str, list] = {"fan_out_write": [], "fan_out_delete": [], "file_updated": [],
                              "notifications": []}
    flags = SimpleNamespace(targets=False)

    async def _mark(agent, rel, *, exclude_machine_id=None):
        return {OTHER: (1, 2)} if flags.targets else {}

    async def _fan_write(agent, rel, source, *, exclude_machine_id=None, marked=None, **kw):
        kind = "bytes" if isinstance(source, (bytes, bytearray)) else "path"
        noted["fan_out_write"].append({
            "agent": agent, "rel": rel, "exclude": exclude_machine_id, "marked": marked,
            kind: source.decode() if kind == "bytes" else str(source)})

    async def _fan_delete(agent, rel, *, exclude_machine_id=None):
        noted["fan_out_delete"].append({"agent": agent, "rel": rel,
                                        "exclude": exclude_machine_id})

    async def _file_updated(agent, rel, *, source="disk", **kw):
        noted["file_updated"].append(f"{agent}/{rel} {source}")

    async def _notify(title, body, **kw):
        noted["notifications"].append({"title": title, "body": body, **kw})
        return []

    monkeypatch.setattr(workspace_fanout, "mark_fan_out_targets", _mark)
    monkeypatch.setattr(workspace_fanout, "fan_out_write", _fan_write)
    monkeypatch.setattr(workspace_fanout, "fan_out_delete", _fan_delete)
    monkeypatch.setattr(notification_manager, "broadcast_file_updated", _file_updated)
    monkeypatch.setattr(notification_manager, "fire_notification", _notify)
    state = (flow._platform_ahead, flow._pull_stat_records, sft.LAST_FILE_CHANGED,
             rws._reconnect_walks, rws._reconnect_completed_at, rws._idle_sync_inflight)
    for table in state:
        table.clear()
    yield SimpleNamespace(mgr=mgr, layer=layer, clock=clock, noted=noted, flags=flags,
                          session=session_state, monkeypatch=monkeypatch)
    for table in state:
        table.clear()
    flow.cleanup_session(SESSION)


async def _settle() -> None:
    """A frame enqueued on the way out (a cancel, a scrub) reaches the machine."""
    for _ in range(5):
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def _online(rig, *machines):
    """Each machine connected, with its writer running."""
    for machine in machines:
        conn = SatelliteConnection(
            machine_id=machine.machine_id, ws=machine, satellite_version=machine.version,
            capabilities={"os": machine.os_name},
            clock_offset=0.0)
        rig.mgr._connections[machine.machine_id] = conn
        conn.writer_task = asyncio.create_task(rig.mgr._writer_loop(conn))
    try:
        yield
    finally:
        await _settle()
        for machine in machines:
            conn = rig.mgr._connections.pop(machine.machine_id)
            conn.writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await conn.writer_task


def _marks() -> dict:
    """The platform-ahead marks, their times as what is left on the clock."""
    now = time.monotonic()
    return {"|".join(key): {
        **mark,
        "in_flight_until": mark["in_flight_until"] and mark["in_flight_until"] - now,
        "retry_at": mark["retry_at"] and mark["retry_at"] - now,
    } for key, mark in flow._platform_ahead.items()}


def _took(machine: _Machine) -> list[dict]:
    """What the machine was sent since the last look."""
    frames, machine.frames = machine.frames, []
    return frames


# ---------------------------------------------------------------------------
# remote_file_flow
# ---------------------------------------------------------------------------


def test_stat_records_and_the_expectation_they_make(rig):
    flow.record_pushed_stat(MACHINE, AGENT, "workspace/a.txt", 5, 50)
    flow.record_committed_stat(MACHINE, AGENT, "workspace/ack.txt", {"size": 6, "mtime_ns": 60})
    flow.record_committed_stat(MACHINE, AGENT, "workspace/bare.txt", {"status": "ok"})
    flow.record_committed_stat(MACHINE, AGENT, "workspace/bool.txt", {"size": True, "mtime_ns": 1})
    check_golden("file-flow/stat-records", {
        "taken": [flow.take_push_record(MACHINE, AGENT, rel)
                  for rel in ("workspace/a.txt", "workspace/a.txt", "workspace/ack.txt",
                              "workspace/bare.txt", "workspace/bool.txt")],
        "another_machines": flow.take_push_record(OTHER, AGENT, "workspace/a.txt"),
        "expectations": [flow.push_expectation_of(s) for s in (
            {"exists": True, "size": 3, "mtime_ns": 9}, None, {"exists": False},
            {"size": "3", "mtime_ns": 9}, {"size": 3})],
        "stale_stats": [flow.stale_machine_stat(s) for s in (
            {"reason": "live", "size": 3, "mtime_ns": 9},
            {"reason": "live", "size": None, "mtime_ns": None},
            {"size": True, "mtime_ns": 9}, {})],
        "push_ack_stale": flow.PUSH_ACK_STALE,
    })


def test_the_platform_ahead_state_machine(rig):
    rel = "workspace/m.txt"
    steps: list = []

    def step(name, **extra):
        steps.append({"step": name, "marks": _marks(), "stale_hold": flow.stale_hold(
            MACHINE, AGENT, rel, platform_hash="", base_hash=None), **extra})

    step("nothing marked")
    flow.note_platform_ahead(MACHINE, AGENT, rel, 10, 100)
    step("a failed push, no record")
    flow.note_platform_ahead(MACHINE, AGENT, rel, 10, 100, in_flight_s=60.0)
    step("the same copy in flight keeps its baseline")
    flow.end_in_flight(MACHINE, AGENT, rel, (99, 99))
    step("another copy's window end changes nothing")
    flow.end_in_flight(MACHINE, AGENT, rel, (10, 100))
    step("its own window ends")
    flow.note_platform_ahead(MACHINE, AGENT, rel, 10, 100,
                             machine={"exists": True, "size": 1, "mtime_ns": 2})
    step("a probed baseline replaces")
    flow.note_platform_ahead(MACHINE, AGENT, rel, 10, 100, stale=True)
    step("a stale refusal holds")
    flow.note_platform_ahead(MACHINE, AGENT, rel, 20, 200)
    step("a mark of another copy, no platform file: the mark stands")
    flow.clear_platform_ahead(MACHINE, AGENT, rel, stat=(20, 200))
    step("an ack of another copy loses the baseline, the mark stands")
    target = _platform(rel, b"x" * 20, ns=200)
    flow.note_platform_ahead(MACHINE, AGENT, rel, 20, 200)
    step("the platform copy is the new one: replaced")
    flow.clear_platform_ahead(MACHINE, AGENT, rel, stat=(20, 200))
    step("an ack of the copy clears")
    flow.record_pushed_stat(MACHINE, AGENT, rel, 7, 70)
    flow.note_platform_ahead(MACHINE, AGENT, rel, 20, 200)
    step("the baseline is the machine's record")
    flow.clear_platform_ahead(MACHINE, AGENT, rel)
    step("a merge's push clears whatever stands")
    target.unlink()
    check_golden("file-flow/platform-ahead", steps)


def test_a_push_that_failed_marks_with_a_probed_baseline(rig):
    rel = "workspace/p.txt"
    machine = _Machine(rig.mgr)
    machine.put(rel, b"on machine", 55)
    got: dict = {}

    async def scene():
        fallback = {"exists": True, "size": 1, "mtime_ns": 2}
        async with _online(rig, machine):
            await flow.note_push_failed(rig.mgr, MACHINE, AGENT, rel, 10, 100, fallback=fallback)
            got["probed"] = _marks()
            got["probe_frames"] = _took(machine)
            flow.clear_platform_ahead(MACHINE, AGENT, rel)
            machine.stat_fails = True
            await flow.note_push_failed(rig.mgr, MACHINE, AGENT, rel, 10, 100, fallback=fallback)
            got["no_answer_takes_the_fallback"] = _marks()
            await flow.note_push_failed(rig.mgr, MACHINE, AGENT, rel, 11, 111, fallback=fallback)
            got["an_older_push_failing_under_a_newer_mark"] = _marks()

    _run(scene())
    check_golden("file-flow/note-push-failed", got)


def test_the_host_cache(rig):
    abs_path = "C:\\Users\\Ann\\Doc.TXT"
    machine = _Machine(rig.mgr)
    twin = "c:/Users/Ann/Doc.TXT"
    machine.put(abs_path, b"host bytes", 41)
    machine.put(twin, b"host bytes", 41)
    import dataclasses
    on_ann = _ctx()
    # The write-back asks the write rule: Ann's home on her machine.
    on_ann = dataclasses.replace(on_ann, placement=dataclasses.replace(
        on_ann.placement, os="windows", home_dir="C:/Users/Ann"))
    rig.session.set_session_security(SESSION, on_ann)
    rig.session.set_session_security("flow-peer", _ctx())
    root = flow.host_cache_session_root(SESSION)
    got: dict = {"session_root": root, "is_host_cache_path": {
        p: flow.is_host_cache_path(p) for p in (
            str(root), str(root) + "/x", str(root.parent), str(root.parent) + "x",
            str(root.parent.parent), "/etc/passwd")}}

    async def scene():
        async with _online(rig, machine):
            cached = await flow.pull_through_host_path(SESSION, abs_path)
            sidecar = flow._host_cache_paths(SESSION, machine.machine_id, abs_path)[1]
            got["first_read"] = {"path": cached, "bytes": cached.read_text(),
                                 "sidecar": json.loads(sidecar.read_text()), "wire": _took(machine)}
            same = await flow.pull_through_host_path(SESSION, twin)
            got["a_twin_of_the_path_revalidates"] = {"same_entry": same == cached, "wire": _took(machine)}
            machine.put(abs_path, b"host bytes v2", 42)
            await flow.pull_through_host_path(SESSION, abs_path)
            got["a_changed_stat_pulls"] = {"bytes": cached.read_text(), "wire": _took(machine)}
            cached.write_bytes(b"edited by a tool")
            got["own_push"] = {
                "ok": await flow.push_back_host_path(SESSION, str(cached)),
                "sidecar": json.loads(sidecar.read_text()), "wire": _took(machine)}
            got["refused"] = {
                "another_sessions_cache": await flow.push_back_host_path("flow-peer", str(cached)),
                "a_local_session": await flow.push_back_host_path("flow-gone", str(cached)),
                "climbing_out": await flow.push_back_host_path(
                    SESSION, str(root / ".." / ".." / AGENT / "x")),
                "the_root_itself": await flow.push_back_host_path(SESSION, str(root)),
            }
            sidecar.write_text(json.dumps({"machine_id": OTHER, "abs_path": "/etc/passwd"}))
            got["a_sidecar_of_another_machine"] = await flow.push_back_host_path(SESSION, str(cached))
            sidecar.write_text("{not json")
            got["a_broken_sidecar"] = await flow.push_back_host_path(SESSION, str(cached))
            got["refused_wire"] = _took(machine)
            machine.no_pull[abs_path] = "File not found"
            sidecar.unlink()
            got["a_failed_pull"] = await flow.pull_through_host_path(SESSION, abs_path)
            got["local_session_pull"] = await flow.pull_through_host_path("flow-gone", abs_path)
        flow.cleanup_session(SESSION)
        # The removal runs off the loop; the next start of the id waits for it.
        from core import file_commit
        assert await file_commit.settled(SESSION, 10)
        got["after_cleanup"] = root.exists()

    _run(scene())
    check_golden("file-flow/host-cache", _norm(got))


def test_who_the_flows_take_a_session_for(rig):
    held = SimpleNamespace(machine_id="machine-held", agent_name="held-agent")
    rig.monkeypatch.setitem(rig.layer._sessions, "s-held", held)
    rig.session.set_session_security("s-held", _ctx())
    rig.session.set_session_security("s-survivor", _ctx(machine="machine-survivor", agent="a-s"))
    rig.session.set_session_security("s-local", _ctx(machine=""))
    rig.session.set_session_security("s-closed", _ctx())
    rig.session.cleanup_session_permission_state("s-closed")
    check_golden("file-flow/session-info", {
        sid: {"remote": flow.is_remote_session(sid), "machine": flow.remote_machine_id(sid),
              "agent": flow.remote_session_agent(sid)}
        for sid in ("s-held", "s-survivor", "s-local", "s-closed", "s-never")})


def test_write_back_allowed_and_the_held_reason(rig):
    from storage.knowledge import db_knowledge_libraries as libraries
    libraries.promote("src-agent", created_by="user-admin")
    libraries.promote("src-ro", created_by="user-admin")
    libraries.attach("src-agent", AGENT, writable=True, created_by="user-admin")
    libraries.attach("src-ro", AGENT, writable=False, created_by="user-admin")
    paths = ("workspace/a.txt", "knowledge/n.md", "config/agent.md", "users/alice/workspace/a.txt",
             "users/bob/workspace/a.txt", "users/alice/.codex/config.toml",
             "knowledge/shared/src-agent/g.md", "knowledge/shared/src-ro/g.md",
             "workspace/../config/x", "C:/Users/x")
    sessions = {
        f"role-{role}": _ctx(role=role) for role in ("viewer", "contributor", "editor", "manager", "admin")
    } | {
        "service": _ctx(role="manager", username=""),
        "service-knowledge": _ctx(role="manager", username="", knowledge_rw=True),
        "another-agent": _ctx(agent="other-agent"),
    }
    for sid, ctx in sessions.items():
        rig.session.set_session_security(sid, ctx)

    async def scene():
        out = {}
        for sid in (*sessions, "no-context"):
            out[sid] = {p: await flow.write_back_allowed(sid, AGENT, p) for p in paths}
        return out

    check_golden("file-flow/write-back", {
        "verdicts": _run(scene()), "held_reason": flow.held_reason("knowledge/n.md")})


def test_a_read_through_pull_through(rig):
    rel = "workspace/r.txt"
    machine = _Machine(rig.mgr)
    rig.session.set_session_security(SESSION, _ctx())
    rig.session.set_session_security("flow-viewer", _ctx(role="viewer"))
    steps: list = []

    async def read(name, sid=SESSION, path=rel, sat=machine):
        held, fallback = [], []
        got = await flow.pull_through(sid, path, held=held, fallback=fallback)
        steps.append({"step": name, "path": got, "held": held, "fallback": fallback,
                      "wire": _took(sat), "tree": _tree() if (config.AGENTS_DIR / AGENT).exists() else {}})

    async def scene():
        async with _online(rig, machine):
            await read("a session the layer does not know", sid="flow-gone")
            await read("not canonical", path="C:/Users/x")
            await read("traversal", path="workspace/../../etc/passwd")
            _platform("knowledge/k.md", b"authoritative")
            machine.put("knowledge/k.md", b"never read")
            await read("held: the platform copy", sid="flow-viewer", path="knowledge/k.md")
            await read("held: no platform copy", sid="flow-viewer", path="knowledge/none.md")
            machine.put(rel, b"machine v1", 11)
            await read("the first read pulls")
            await read("the same stat is served without a transfer")
            machine.put(rel, b"machine v2", 22)
            await read("a changed stat pulls")
            machine.no_pull[rel] = "denied"
            machine.put(rel, b"machine v3", 33)
            await read("a failed pull serves the mirror")
            await read("no such file on either side", path="workspace/none.txt")
            del machine.no_pull[rel]
            machine.stat_fails = True
            await read("a failed probe pulls")

    _run(scene())
    check_golden("file-flow/pull-through", _norm(steps))


def test_a_read_of_a_copy_marked_ahead(rig):
    rel = "workspace/ahead.txt"
    machine = _Machine(rig.mgr)
    rig.session.set_session_security(SESSION, _ctx())
    data = b"platform bytes"
    target = _platform(rel, data, ns=500)
    size = len(data)
    baseline = {"exists": True, "size": 3, "mtime_ns": 33}
    machine.put(rel, b"old", 33)
    steps: list = []

    async def read(name):
        got = await flow.pull_through(SESSION, rel)
        await asyncio.gather(*list(flow._repush_tasks))
        steps.append({"step": name, "path": got, "bytes": target.read_text(), "wire": _took(machine),
                      "marks": _marks(), "record": flow._pull_stat_records.get((MACHINE, AGENT, rel))})

    async def scene():
        async with _online(rig, machine):
            flow.note_platform_ahead(MACHINE, AGENT, rel, size, 500, in_flight_s=60.0, machine=baseline)
            await read("in flight: served with no probe")
            rig.clock.now += 61.0
            machine.refused.add(rel)
            await read("past the window, the machine behind: served and pushed, the push fails")
            await read("inside the backoff: served, no push")
            rig.clock.now += 31.0
            machine.refused.clear()
            await read("past the backoff: pushed, the mark goes")
            await read("no mark: the record the ack left fast-paths")
            flow.note_platform_ahead(MACHINE, AGENT, rel, size, 500, machine=None)
            await read("no baseline: served and pushed")
            os.utime(target, ns=(600, 600))
            flow.note_platform_ahead(MACHINE, AGENT, rel, size, 500)
            await read("a later platform write drops the mark")
            saved = b"a later save"
            _platform(rel, saved, ns=700)
            flow.note_platform_ahead(MACHINE, AGENT, rel, len(saved), 700, machine=baseline, stale=True)
            machine.put(rel, b"machine wins", 44)
            await read("the machine changed after a stale refusal: its copy wins, the save is kept")

    _run(scene())
    bin_rows = _rows("SELECT rel_path, reason, size, file_hash FROM recover_bin")
    check_golden("file-flow/read-ahead", _norm({"steps": steps, "recover_bin": bin_rows}))


def test_push_back(rig):
    rel = "workspace/pb.txt"
    machine = _Machine(rig.mgr)
    rig.session.set_session_security(SESSION, _ctx())
    target = _platform(rel, b"edited", ns=900)
    machine.put(rel, b"before", 77)
    rig.flags.targets = True
    steps: list = []

    async def push(name, path=rel, sid=SESSION):
        ok = await flow.push_back(sid, path)
        steps.append({"step": name, "ok": ok, "wire": _took(machine), "marks": _marks(),
                      "record": flow._pull_stat_records.get((MACHINE, AGENT, path))})
        rig.noted["fan_out_write"], fan = [], rig.noted["fan_out_write"]
        steps[-1]["fan_out"] = fan

    async def scene():
        async with _online(rig, machine):
            await push("a session the layer does not know", sid="flow-gone")
            await push("not canonical", path="C:/x")
            await push("no such file", path="workspace/none.txt")
            await push("no record expects a live file; the ack records the commit")
            await push("the record is expected")
            machine.acks_stat = False
            await push("an ok that reports no written file is a refusal: marked, a probed baseline")
            machine.acks_stat = True
            flow.record_pushed_stat(MACHINE, AGENT, rel, 6, 66)
            machine.refused.add(rel)
            await push("a refusal marks with a probed baseline")
            machine.stat_fails = True
            flow.clear_platform_ahead(MACHINE, AGENT, rel)
            flow.record_pushed_stat(MACHINE, AGENT, rel, 6, 66)
            await push("no probe: the taken record is the baseline")
            machine.stat_fails = False
            machine.refused.clear()
            machine.stale.add(rel)
            await push("a stale answer holds, the ack's stat the baseline")
            machine.stale.clear()
            await push("an ack clears the mark")
            link = target.parent / "l.txt"
            link.symlink_to(target)
            await push("a link at the name is not pushed", path="workspace/l.txt")

    _run(scene())
    check_golden("file-flow/push-back", _norm(steps))


def test_the_listings_and_the_probe(rig, tmp_path):
    machine = _Machine(rig.mgr)
    for path, data in (("workspace/a.txt", b"aa"), ("workspace/sub/b.txt", b"b"),
                       ("knowledge/k.md", b"kk"), ("workspace/x.partial", b"p")):
        machine.put(path, data)
    machine.skipped = [{"path": "workspace/unread.txt", "skipped": "denied"},
                       {"path": "workspace", "dir": True, "skipped": "denied"},
                       {"path": "knowledge/far", "dir": True, "skipped": "denied"}]
    rig.session.set_session_security(SESSION, _ctx())
    got: dict = {}

    async def scene():
        async with _online(rig, machine):
            got["files"] = {p: await flow.list_remote_files(SESSION, p)
                            for p in ("", "workspace", "workspace/sub/", "none")}
            got["files_wire"] = _took(machine)
            got["entries"] = await flow.list_remote_entries(SESSION, "workspace")
            got["entries_skipped"] = await flow.list_remote_entries(
                SESSION, "workspace/sub", report_skipped=True)
            got["entries_wire"] = _took(machine)
            got["another_agent"] = await flow.list_remote_entries(SESSION, "", agent="other-agent")
            got["local"] = await flow.list_remote_files("flow-gone", "")
            got["stat_probe"] = {
                "file": await flow.stat_probe(SESSION, "workspace/a.txt"),
                "absent": await flow.stat_probe(SESSION, "workspace/none.txt"),
                "not_canonical": await flow.stat_probe(SESSION, "C:/x"),
                "local": await flow.stat_probe("flow-gone", "workspace/a.txt"),
            }
            dest = tmp_path / "snap" / "s.bin"
            got["pull_into"] = {
                "ok": await flow.pull_into(SESSION, "workspace/a.txt", dest),
                "bytes": dest.read_text(),
                "over_the_cap": await flow.pull_into(SESSION, "knowledge/k.md", dest, max_bytes=1),
                "another_agent": await flow.pull_into(SESSION, "workspace/a.txt", dest, agent="x"),
                "not_canonical": await flow.pull_into(SESSION, "C:/x", dest),
                "local": await flow.pull_into("flow-gone", "workspace/a.txt", dest),
            }

    _run(scene())
    check_golden("file-flow/listings", _norm(got))


def test_the_path_locks(rig):
    async def scene():
        one = await flow._acquire_global_path_lock(AGENT, "workspace/a.txt")
        same = await flow._acquire_global_path_lock(AGENT, "workspace/a.txt")
        other = await flow._acquire_global_path_lock(AGENT, "workspace/b.txt")
        fan = await flow.acquire_fanout_lock(AGENT, "workspace/a.txt")
        idle_before = one.idle()
        async with one:
            held = one.idle()
        return {"same_key_one_lock": one is same, "another_path": one is other,
                "fan_out_is_its_own": fan is one, "fan_out_again": fan is await flow.acquire_fanout_lock(
                    AGENT, "workspace/a.txt"),
                "idle": idle_before, "idle_while_held": held, "idle_after": one.idle()}
    check_golden("file-flow/path-locks", _run(scene()))


# ---------------------------------------------------------------------------
# satellite_file_transfer
# ---------------------------------------------------------------------------


def test_the_push_ceiling_and_the_staging_name():
    staging = _pull_name("_staging_name")

    def named(name):
        out = staging(name)
        marker = out[-len(".partial") - 17:-len(".partial")]
        assert marker[0] == "." and int(marker[1:], 16) >= 0
        return {"name": out.replace(marker, ".<hex>"), "bytes": len(os.fsencode(out))}

    check_golden("file-flow/transfer-names", {
        "push_ceiling_s": {str(size): sft.push_ceiling_s(size) for size in (
            0, 1, 16 * 1024, 600 * 16 * 1024, 600 * 16 * 1024 + 1, 1024 ** 3, 10 * 1024 ** 3)},
        "staging": [named(n) for n in ("a.txt", ".hidden", "x" * 300, "é" * 150, "ab" + "é" * 150)],
    })


def _changed(**kw) -> dict:
    return {"agent_slug": AGENT, "path": "workspace/a.txt", "action": "write",
            "session_id": SESSION, **kw}


def test_the_admission_of_a_file_changed_frame(rig, caplog):
    rig.session.set_session_security(SESSION, _ctx())
    rig.session.set_session_security("s-elsewhere", _ctx(machine=OTHER))
    rig.session.set_session_security("s-task", _ctx(machine="", role="editor", username=""))
    rig.session.set_session_security("s-unheld", _ctx(machine=""))
    rig.session.set_session_security("s-moved", _ctx())
    rig.monkeypatch.setitem(rig.layer._sessions, "s-task", SimpleNamespace(machine_id=MACHINE))
    rig.monkeypatch.setitem(rig.layer._sessions, "s-moved", SimpleNamespace(machine_id=OTHER))
    frames = {
        "no_agent": _changed(agent_slug=""), "no_path": _changed(path=""), "no_action": _changed(action=""),
        "no_session": _changed(session_id=""), "no_context": _changed(session_id="s-none"),
        "engine_machinery": _changed(session_id="s-none", path="workspace/.claude/.last-cleanup"),
        "admitted": _changed(), "a_delete": _changed(action="delete"),
        "bound_elsewhere": _changed(session_id="s-elsewhere"),
        "the_layer_holds_it_here": _changed(session_id="s-task"),
        "no_machine_and_not_held": _changed(session_id="s-unheld"),
        "the_layer_holds_it_elsewhere": _changed(session_id="s-moved"),
        "another_agent": _changed(agent_slug="other-agent"),
    }
    got = {}
    for name, frame in frames.items():
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="otodock.satellite"):
            admitted = sft._admit_file_changed(MACHINE, frame)
        got[name] = {"admitted": admitted and {
            "agent": admitted.agent_slug, "path": admitted.rel_path, "action": admitted.action,
            "session": admitted.session_id, "role": admitted.sec.role, "msg": admitted.msg is frame},
            "log": [f"{r.levelname} {r.getMessage()}" for r in caplog.records
                    if r.name == "otodock.satellite"]}
    check_golden("file-flow/transfer-admit", got)


def test_the_file_changed_stamps_and_the_lane(rig, caplog):
    rig.session.set_session_security(SESSION, _ctx())
    rig.session.set_session_security("s-two", _ctx(machine=OTHER))
    inline = {"content_b64": base64.b64encode(b"x").decode(), "hash": _sha(b"x"), "type": "file_changed"}
    rig.monkeypatch.setattr(config, "SAT_FILE_CHANGED_STAMP_MAX", 2)
    rig.monkeypatch.setattr(config, "SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE", 1)
    got: dict = {}

    def stamps():
        return {"|".join(k): v for k, v in sft.LAST_FILE_CHANGED.items()}

    async def settle():
        await asyncio.gather(*background.running("satellite_file_transfer"))

    async def scene():
        (config.AGENTS_DIR / AGENT).mkdir(parents=True)
        got["unstamped"] = sft.last_file_changed_at(MACHINE, AGENT)
        await rig.mgr.handle_message(MACHINE, _changed(session_id="s-none", **inline))
        got["a_refused_frame_stamps_nothing"] = stamps()
        await rig.mgr.handle_message(MACHINE, _changed(**inline))
        got["stamped"] = sft.last_file_changed_at(MACHINE, AGENT)
        rig.clock.now += 5.0
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="otodock.satellite"):
            await rig.mgr.handle_message(MACHINE, _changed(path="workspace/b.txt", **inline))
        got["a_frame_shed_past_the_ceiling_stamps_too"] = {
            "stamp": sft.last_file_changed_at(MACHINE, AGENT),
            "inflight": rig.mgr._fc_lanes[MACHINE].inflight,
            "log": [r.getMessage() for r in caplog.records if r.name == "otodock.satellite"]}
        rig.clock.now += 5.0
        await settle()
        got["the_applier_stamps_at_its_end"] = {
            "stamp": sft.last_file_changed_at(MACHINE, AGENT),
            "inflight": rig.mgr._fc_lanes[MACHINE].inflight, "tree": _tree()}
        (config.AGENTS_DIR / "agent-b").mkdir()
        rig.session.set_session_security("s-b", _ctx(agent="agent-b"))
        await rig.mgr.handle_message(MACHINE, _changed(agent_slug="agent-b", session_id="s-b", **inline))
        await settle()
        await rig.mgr.handle_message(OTHER, _changed(session_id="s-two", **inline))
        await settle()
        got["the_map_is_bounded_oldest_out"] = stamps()
        sft.evict_file_changed_stamps(MACHINE)
        got["a_machine_evicted"] = stamps()
        sft.evict_file_changed_stamps("machine-none")
        got["no_such_machine"] = stamps()

    _run(scene())
    check_golden("file-flow/transfer-stamps", got)


def _landed(dest: Path) -> dict:
    """What a pull left at its destination and beside it."""
    return {"bytes": dest.read_text() if dest.is_file() else None,
            "partials_left": sorted(p.name for p in dest.parent.glob(".*.partial"))
            if dest.parent.exists() else []}


def test_the_pull_through_the_mixin(rig, tmp_path):
    machine = _Machine(rig.mgr)
    machine.put("workspace/x.bin", b"0123456789" * 3)
    machine.put("workspace/empty.txt", b"")
    machine.put("workspace/tampered.bin", b"abc")
    machine.bad_hash.add("workspace/tampered.bin")
    ws = config.AGENTS_DIR / AGENT / "workspace"
    other_root = tmp_path / "pull"
    steps: list = []

    async def pull(name, rel, dest, *, sat=machine, slug=AGENT, **kw):
        seen: list = []
        try:
            ok = await rig.mgr.pull_file_to_path(
                sat.machine_id, PathRef("agent_tree", rel), dest, agent_slug=slug,
                progress_cb=lambda got, want: seen.append([got, want]), **kw)
        except ValueError as e:
            ok = f"ValueError: {e}"
        await _settle()
        steps.append({"step": name, "answer": ok, "progress": seen, "wire": _took(sat),
                      "pending": len(rig.mgr._pending_pulls), **_landed(dest)})

    async def scene():
        async with _online(rig, machine):
            await pull("three chunks into the agents tree", "workspace/x.bin", ws / "deep" / "x.bin")
            await pull("an empty file", "workspace/empty.txt", ws / "empty.txt")
            await pull("outside the tree, its parent made", "workspace/x.bin", other_root / "p" / "x.bin")
            await pull("the machine's refusal", "workspace/none.bin", ws / "none.bin")
            await pull("a hash that does not match", "workspace/tampered.bin", ws / "tampered.bin")
            await pull("past the caller's cap", "workspace/x.bin", ws / "capped.bin", max_bytes=5)
            await pull("no slug for the agent tree", "workspace/x.bin", ws / "y.bin", slug="")
            target = ws / "real"
            target.mkdir()
            (ws / "link").symlink_to(target)
            await pull("a link on the way refuses", "workspace/x.bin", ws / "link" / "z.bin")
            await pull("request fields replace the path", "workspace/x.bin", ws / "fields.bin",
                       request_fields={"type": "file_pull", "path": "workspace/x.bin", "scope": "move"})
            steps.append({"absent_machine": await rig.mgr.pull_file_to_path(
                "machine-none", PathRef("agent_tree", "workspace/x.bin"), ws / "n.bin", agent_slug=AGENT)})

    _run(scene())
    check_golden("file-flow/transfer-pull", _norm({
        "steps": steps, "outside": sorted(p.name for p in (other_root / "p").iterdir())}))


def test_the_push_and_the_stat_through_the_mixin(rig, tmp_path):
    machine = _Machine(rig.mgr)
    machine.put("workspace/held.txt", b"held", 88)
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * (600 * 1024))
    ref = PathRef("agent_tree", "workspace/out.bin")
    got: dict = {}

    async def push(name, source, *, sat=machine, path_ref=ref, **kw):
        ticks: list = []
        stale: dict = {}
        answer = await rig.mgr.push_file(
            sat.machine_id, path_ref, source, agent_slug=AGENT, stale_out=stale,
            progress_cb=lambda a, b: ticks.append([a, b]), **kw)
        got[name] = {"answer": answer, "ticks": ticks, "stale": stale, "wire": _took(sat),
                     "record": flow._pull_stat_records.get((sat.machine_id, AGENT, path_ref.value))}

    async def scene():
        async with _online(rig, machine):
            await push("inline bytes, the stat on the ack recorded", b"small")
            await push("inline, an expectation and a pinned hash", b"small", content_hash="sha256:pinned",
                       expect={"size": 1, "mtime_ns": 2})
            with opened(big) as source:
                await push("in chunks from a path", source, expect="live")
            machine.stale.add("workspace/held.txt")
            await push("a stale answer, the machine's stat out", b"x",
                       path_ref=PathRef("agent_tree", "workspace/held.txt"))
            machine.refused.add("workspace/refused.txt")
            await push("a refusal", b"x", path_ref=PathRef("agent_tree", "workspace/refused.txt"))
            await push("a host path", b"h", path_ref=PathRef("satellite_host", "/sat/ann/x.txt"))
            await push("a source that is gone", gone())
            got["no_connection"] = await rig.mgr.push_file("machine-none", ref, b"x", agent_slug=AGENT)
            with pytest.raises(ValueError) as refused:
                await rig.mgr.push_file(MACHINE, ref, b"x")
            got["no_slug"] = str(refused.value)
            got["stat"] = {
                "a_file": await rig.mgr.stat_file(MACHINE, PathRef("agent_tree", "workspace/held.txt"), agent_slug=AGENT),
                "absent": await rig.mgr.stat_file(MACHINE, PathRef("agent_tree", "workspace/none"), agent_slug=AGENT),
                "no_connection": await rig.mgr.stat_file("machine-none", ref, agent_slug=AGENT),
            }
            machine.stat_fails = True
            got["stat"]["an_error"] = await rig.mgr.stat_file(MACHINE, ref, agent_slug=AGENT)
            got["stat_wire"] = _took(machine)

    _run(scene())
    check_golden("file-flow/transfer-push", got)


def test_the_file_changed_applier(rig):
    _username("user-viewer", "bob")
    machine = _Machine(rig.mgr)
    rig.session.set_session_security(SESSION, _ctx(role="editor"))
    rig.session.set_session_security("s-viewer", _ctx(role="viewer"))
    rig.monkeypatch.setattr("services.remote.workspace_fanout.fanout_targets", lambda *a, **k: [OTHER])
    rig.flags.targets = True
    rel = "workspace/live.txt"
    steps: list = []

    def inline(data: bytes, **kw):
        return _changed(path=rel, content_b64=base64.b64encode(data).decode(), hash=_sha(data), **kw)

    async def apply(name, frame):
        await rig.mgr._apply_file_changed(MACHINE, frame)
        steps.append({
            "step": name, "tree": _tree(), "wire": _took(machine),
            "sync_state": _rows("SELECT rel_path, base_hash FROM sync_state"),
            "author": _rows("SELECT rel_path, last_writer FROM file_author"),
            "tombstones": _rows("SELECT rel_path, origin FROM file_tombstones"),
            "recover_bin": _rows("SELECT rel_path, reason, size FROM recover_bin"),
            "noted": json.loads(json.dumps(rig.noted)),
            "stamp": sft.last_file_changed_at(MACHINE, AGENT)})
        for table in ("fan_out_write", "fan_out_delete", "file_updated", "notifications"):
            rig.noted[table] = []

    async def scene():
        (config.AGENTS_DIR / AGENT).mkdir(parents=True)
        async with _online(rig, machine):
            await apply("a refused frame touches nothing", inline(b"no", session_id="s-none"))
            await apply("a role that may not write", inline(b"no", session_id="s-viewer"))
            await apply("a new file", inline(b"first"))
            await apply("the same machine's next write", inline(b"second"))
            _platform(rel, b"a colleague's", ns=T0_NS)
            await asyncio.to_thread(file_author_store.record, AGENT, rel, "bob")
            await apply("over a colleague's edit the merge base never saw", inline(b"third"))
            machine.put("workspace/large.bin", b"large file body")
            await apply("a large file is pulled", _changed(
                path="workspace/large.bin", size=15, hash=_sha(b"large file body")))
            await apply("an empty inline write", inline(b""))
            await apply("a delete", _changed(path=rel, action="delete"))
            await apply("a delete of nothing", _changed(path="workspace/none", action="delete"))
            await apply("a path outside the tree's scopes", _changed(path="workspace/../../x", action="delete"))

    _run(scene())
    check_golden("file-flow/transfer-apply", _norm(steps))


def test_the_file_changed_applier_on_knowledge_paths(rig, monkeypatch):
    from services.knowledge import library_projector
    from storage.knowledge import db_knowledge_libraries as libraries
    libraries.promote("src-agent", created_by="user-admin")
    libraries.promote("src-ro", created_by="user-admin")
    libraries.attach("src-agent", AGENT, writable=True, created_by="user-admin")
    libraries.attach("src-ro", AGENT, writable=False, created_by="user-admin")
    rig.session.set_session_security(SESSION, _ctx())
    calls: list = []
    answers = iter((True, False))

    async def _mirror_delete(agent, source, sub):
        calls.append(["propagate_mirror_delete", agent, source, sub])
        return next(answers)

    async def _mirror_write(agent, source, sub):
        calls.append(["propagate_mirror_write", agent, source, sub])

    async def _source_write(agent, sub, *, deleted=False):
        calls.append(["propagate_source_write", agent, sub, deleted])

    monkeypatch.setattr(library_projector, "propagate_mirror_delete", _mirror_delete)
    monkeypatch.setattr(library_projector, "propagate_mirror_write", _mirror_write)
    monkeypatch.setattr(library_projector, "propagate_source_write", _source_write)
    body = {"content_b64": base64.b64encode(b"k").decode(), "hash": _sha(b"k")}
    steps: list = []

    async def apply(name, **frame):
        await rig.mgr._apply_file_changed(MACHINE, _changed(**frame))
        await asyncio.gather(*background.running("satellite_file_transfer"))
        steps.append({"step": name, "projector": list(calls), "tree": _tree()})
        calls.clear()

    async def scene():
        (config.AGENTS_DIR / AGENT).mkdir(parents=True)
        await apply("a mirror delete reaches the source",
                    path="knowledge/shared/src-agent/sub/x.md", action="delete")
        await apply("a mirror delete the source ignores",
                    path="knowledge/shared/src-agent/sub/y.md", action="delete")
        await apply("a mirror write flows back", path="knowledge/shared/src-agent/sub/x.md", **body)
        await apply("a read-only mirror takes no write", path="knowledge/shared/src-ro/x.md", **body)
        await apply("a write of the agent's own knowledge", path="knowledge/own.md", **body)
        await apply("a delete of it", path="knowledge/own.md", action="delete")

    _run(scene())
    check_golden("file-flow/transfer-apply-knowledge", steps)


# ---------------------------------------------------------------------------
# remote_workspace_sync
# ---------------------------------------------------------------------------


def test_the_manifest_request_and_the_partition():
    partition = _sync_name("_sync_apply", "_partition_deferred_pulls")
    limit = 4 * 1024 * 1024
    actions = [
        FileAction("workspace/big.bin", "pull", base_hash="s1"),
        FileAction("workspace/edge.bin", "pull", base_hash="s2"),
        FileAction("workspace/under.bin", "pull", base_hash="s3"),
        FileAction("workspace/big_push.bin", "push", base_hash="p"),
        FileAction("workspace/conflict.bin", "pull", base_hash="s4", capture_side="platform"),
        FileAction("workspace/gone.txt", "delete_satellite", clear_base=True),
        FileAction("workspace/same.txt", "noop", base_hash="s5"),
        FileAction("workspace/unlisted.bin", "pull", base_hash="s6"),
    ]
    sizes = {"workspace/big.bin": limit + 1, "workspace/edge.bin": limit, "workspace/under.bin": limit - 1,
             "workspace/big_push.bin": limit + 1, "workspace/conflict.bin": limit + 1}
    foreground, deferred = partition(actions, sizes)
    only_small, none = partition(actions[2:3], sizes)
    check_golden("file-flow/sync-request-partition", {
        "manifest_request": {
            f"skipped={skipped}": rws.manifest_request(AGENT, report_skipped=skipped)
            for skipped in (True, False)},
        "foreground": [dataclasses.asdict(a) for a in foreground],
        "deferred": [dataclasses.asdict(a) for a in deferred],
        "nothing_deferred": {"foreground": [a.rel_path for a in only_small], "deferred": none,
                             "a_copy": only_small is not actions[2:3]},
    })


def test_keeping_a_mtime_and_reading_the_platform_copy():
    keep = _sync_name("_sync_pull", "_keep_mtime")
    read = _sync_name("_sync_capture_loser", "_platform_bytes")
    plain = _platform("workspace/k.txt", b"kept", ns=T0_NS)
    agent_dir = config.AGENTS_DIR / AGENT
    outside = _platform("workspace/target.txt", b"the target", ns=T0_NS)
    (agent_dir / "workspace" / "link.txt").symlink_to(outside)
    (agent_dir / "workspace" / "alias").symlink_to(agent_dir / "workspace")
    keep(AGENT, "workspace/k.txt", 12345.5)
    keep(AGENT, "workspace/none.txt", 1.0)
    keep(AGENT, "workspace/link.txt", 2.0)
    check_golden("file-flow/sync-platform-copy", {
        "keep_mtime": {"kept": plain.stat().st_mtime, "beyond_a_link": outside.stat().st_mtime},
        "platform_bytes": {rel: (lambda b: b.decode() if b is not None else None)(read(agent_dir, rel))
                           for rel in ("workspace/k.txt", "workspace/none.txt", "workspace/link.txt",
                                       "workspace/alias/k.txt", "workspace")},
    })


def test_the_reconnect_gates_and_debounce(rig, monkeypatch):
    async def gates():
        reconnect, manifest = rws._reconnect_gate(), rws._background_manifest_gate()
        return {"reconnect": reconnect._value, "manifest": manifest._value,
                "the_same_on_a_loop": reconnect is rws._reconnect_gate() and manifest is rws._background_manifest_gate()}, reconnect

    first, reconnect = asyncio.run(gates())
    second, again = asyncio.run(gates())
    steps = []

    def step(name):
        steps.append({"step": name, "hold": {m: rws._reconnect_hold_s(m) for m in ("m1", "m2")},
                      "stamped": dict(rws._reconnect_completed_at)})

    step("no walk yet")
    rws._note_reconnect_pass_done("m1")
    step("m1 completed")
    rig.clock.now += 10.0
    step("ten seconds on")
    rws._note_reconnect_pass_done("m2")
    step("m2 completed")
    rig.clock.now += 20.0
    step("the window ends for m1")
    rws._note_reconnect_pass_done("m2")
    step("a stamp drops those past the window")
    rig.clock.now += 45.0
    step("all past it")
    bounds: list = []
    monkeypatch.setattr(random, "uniform", lambda a, b: bounds.append([a, b]) or 2.5)
    jitter = rws._reconnect_jitter_s()
    check_golden("file-flow/sync-debounce", {
        "gates": first, "a_new_loop_a_new_gate": second, "the_loops_differ": reconnect is not again,
        "steps": steps, "jitter": jitter, "jitter_bounds": bounds})


class _WalkLayer(RemoteExecutionLayer):
    """A layer whose merge is recorded: the walk, not the merge, is under test."""

    def __init__(self, cm, trace, *, during=None):
        super().__init__(cm)
        self.trace, self.during = trace, during

    async def _initial_workspace_sync(self, machine_id, agent_slug, **kw):
        self.trace.append(["merge", machine_id, agent_slug, kw])
        if self.during is not None:
            await self.during(machine_id, agent_slug)


def test_the_reconnect_walk(rig, monkeypatch):
    from storage import remote_store
    from storage.identity import db_users
    _username("user-admin", "ada")
    _username("user-viewer", "vic")
    remote_store.create_remote_machine("m-admin", "Admin box", "user-admin", pairing_scope="admin")
    remote_store.create_remote_machine("m-user", "Vic's box", "user-viewer", pairing_scope="user")
    db_users.add_user_agent("user-viewer", "agent-1", "editor", "user-admin")
    for machine, agent in (("m-admin", "agent-2"), ("m-admin", "agent-1"), ("m-user", "agent-1"),
                           ("m-user", "agent-3"), ("m-gone", "agent-1")):
        sync_state_store.record_one(machine, agent, "workspace/a.txt", "sha256:x", 1.0)
    trace: list = []
    real_sleep = asyncio.sleep

    async def _sleep(seconds, *a, **k):
        trace.append(["sleep", seconds])
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    monkeypatch.setattr(random, "uniform", lambda a, b: 3.5)
    for machine_id in ("m-admin", "m-user", "m-gone"):
        rig.mgr._connections[machine_id] = SimpleNamespace(machine_id=machine_id)
    steps: list = []

    async def walk(name, machine_id, layer=None):
        await (layer or plain).sync_all_agents_on_reconnect(machine_id)
        steps.append({"step": name, "trace": list(trace), "hold": rws._reconnect_hold_s(machine_id),
                      "walks_left": sorted(rws._reconnect_walks)})
        trace.clear()

    plain = _WalkLayer(rig.mgr, trace)

    async def kick_during(machine_id, agent_slug):
        trace.append(["kick", machine_id])
        await kicker.sync_all_agents_on_reconnect(machine_id)
        kicker.during = None

    kicker = _WalkLayer(rig.mgr, trace, during=kick_during)

    async def scene():
        await walk("no agents tracked", "m-none")
        await walk("an admin-paired machine", "m-admin")
        rig.clock.now += 10.0
        await walk("the next walk waits out the window", "m-admin")
        await walk("a user-paired machine as its owner", "m-user")
        await walk("a machine gone from the store merges nothing, and completes", "m-gone")
        rig.clock.now += 40.0
        del rig.mgr._connections["m-user"]
        await walk("a drop cuts the pass short: no window restarts", "m-user")
        rig.mgr._connections["m-user"] = SimpleNamespace(machine_id="m-user")
        rig.clock.now += 40.0
        await walk("a kick during a pass earns one more", "m-user", kicker)
        await plain.presync_machine_agent("m-user", "agent-1")
        await plain.presync_machine_agent("m-none", "agent-1")
        steps.append({"step": "presync at the enable click", "trace": list(trace)})
        trace.clear()

    _run(scene())
    check_golden("file-flow/sync-reconnect-walk", steps)


def test_the_idle_sweep(rig, monkeypatch):
    from storage import remote_store
    remote_store.create_remote_machine("m-idle", "Idle box", "user-admin", pairing_scope="admin")
    _username("user-admin", "ada")
    for agent in ("changed", "same", "busy"):
        sync_state_store.record_one("m-idle", agent, "workspace/a.txt", "sha256:x", 1.0)
    conn = SimpleNamespace(
        machine_id="m-idle",
        agent_fingerprints={"changed": "fp2", "same": "fp1", "busy": "fp2", "untracked": "fp2"},
        synced_fingerprints={"changed": "fp1", "same": "fp1", "busy": "fp1"})
    rig.mgr._connections["m-idle"] = conn
    rig.mgr._connections["m-quiet"] = SimpleNamespace(agent_fingerprints={}, synced_fingerprints={})
    monkeypatch.setattr("services.remote.workspace_fanout._active_machine_ids",
                        lambda slug: {"m-idle"} if slug == "busy" else set())
    trace: list = []
    layer = _WalkLayer(rig.mgr, trace)

    async def scene():
        await layer.run_idle_fingerprint_sweep()
        await asyncio.gather(*list(layer._deferred_sync_tasks))

    _run(scene())
    check_golden("file-flow/sync-idle-sweep", {"merges": trace, "synced": conn.synced_fingerprints,
                                               "pending": sorted(rws._idle_sync_inflight)})


def _sync_scene(machine: _Machine) -> None:
    """Both trees and the base of one merge, one path for each thing a merge
    does; every mtime fixed, so the plan is the same on any box."""
    def base(rel, data):
        sync_state_store.record_one(MACHINE, AGENT, rel, _sha(data), T0)

    def both(rel, platform, sat, *, p_ns=T0_NS, s_ns=T0_NS):
        _platform(rel, platform, ns=p_ns)
        machine.put(rel, sat, s_ns)

    both("workspace/same.txt", b"same", b"same")
    _platform("workspace/plat-new.txt", b"new on the platform")
    machine.put("workspace/sat-new.txt", b"new on the machine")
    machine.put("workspace/tomb.txt", b"deleted on the platform")
    file_tombstones_store.record(AGENT, "workspace/tomb.txt", T0 + 100.0, origin="dashboard")
    _platform("workspace/sat-gone.txt", b"deleted on the machine")
    base("workspace/sat-gone.txt", b"deleted on the machine")
    both("workspace/sat-edit.txt", b"base", b"machine edit")
    base("workspace/sat-edit.txt", b"base")
    both("workspace/plat-edit.txt", b"platform edit", b"base")
    base("workspace/plat-edit.txt", b"base")
    both("workspace/clash.txt", b"platform later", b"machine earlier", p_ns=T0_NS + 10**10)
    file_author_store.record(AGENT, "workspace/clash.txt", "bob")
    both("workspace/refused-save.txt", b"a person's save", b"the machine's change")
    base("workspace/refused-save.txt", b"a person's save")
    flow.note_platform_ahead(MACHINE, AGENT, "workspace/refused-save.txt", 15, T0_NS, stale=True)
    both("workspace/stale.txt", b"platform edit", b"base")
    base("workspace/stale.txt", b"base")
    machine.stale.add("workspace/stale.txt")
    both("workspace/refused.txt", b"platform edit", b"base")
    base("workspace/refused.txt", b"base")
    machine.refused.add("workspace/refused.txt")
    both("workspace/clash-stale.txt", b"platform later", b"machine earlier", p_ns=T0_NS + 10**10)
    file_author_store.record(AGENT, "workspace/clash-stale.txt", "bob")
    machine.stale.add("workspace/clash-stale.txt")
    both("workspace/revived.txt", b"both again", b"both again")
    base("workspace/revived.txt", b"both again")
    file_tombstones_store.record(AGENT, "workspace/revived.txt", T0 + 100.0, origin="dashboard")
    _platform("workspace/plat-revived.txt", b"back on the platform")
    file_tombstones_store.record(AGENT, "workspace/plat-revived.txt", T0 + 100.0, origin="dashboard")
    base("workspace/both-gone.txt", b"nobody has it")
    machine.put("workspace/big.bin", b"a large file")
    machine.listed_size["workspace/big.bin"] = 5 * 1024 * 1024
    machine.put("users/bob/workspace/leak.txt", b"another person's")


def test_one_initial_workspace_sync(rig):
    _username("user-viewer", "bob")
    _username("user-admin", "alice")
    machine = _Machine(rig.mgr)
    _sync_scene(machine)
    ticks: list = []

    async def progress(done, total):
        ticks.append([done, total])

    async def scene():
        async with _online(rig, machine):
            await rig.layer._initial_workspace_sync(
                MACHINE, AGENT, target_username="alice", target_role="editor",
                progress_cb=progress)
            pending = list(rig.layer._deferred_sync_tasks)
            await asyncio.gather(*pending)
            return len(pending)

    deferred = _run(scene())

    def mtime(row):
        return {**row, "base_mtime": row["base_mtime"] if row["base_mtime"] < T0 + 1000 else "<pull time>"}

    check_golden("file-flow/sync-initial", _norm({
        "deferred_tasks": deferred,
        "wire": _by_path(machine.frames),
        "platform": _tree(),
        "machine": {path: data.decode() for path, (data, _ns) in sorted(machine.tree.items())},
        "sync_state": [mtime(r) for r in _rows(
            "SELECT rel_path, base_hash, base_mtime FROM sync_state")],
        "authors": _rows("SELECT rel_path, last_writer FROM file_author"),
        "tombstones": _rows("SELECT rel_path, origin FROM file_tombstones"),
        "recover_bin": _rows("SELECT rel_path, reason, scope, owner_sub, size, file_hash "
                             "FROM recover_bin"),
        "marks": _marks(),
        "fan_out_delete": rig.noted["fan_out_delete"],
        "file_updated": sorted(rig.noted["file_updated"]),
        "notifications": rig.noted["notifications"],
        "last_tick": ticks[-1],
    }))


def test_a_merge_keeps_a_refused_save_the_machine_never_held(rig):
    """A person's own save the machine refused (stale), its base older than
    both copies, and the machine's own later change: a clash. The merge takes
    the machine's copy and keeps the save, and its author is told. A scene of
    its own: one notice, whatever order the merge's captures land in."""
    _username("user-admin", "alice")
    machine = _Machine(rig.mgr)
    rel = "workspace/refused-own-save.txt"
    _platform(rel, b"alice's save", ns=T0_NS)
    machine.put(rel, b"alice's change", T0_NS + 10**10)
    sync_state_store.record_one(MACHINE, AGENT, rel, _sha(b"the copy before both"), T0)
    file_author_store.record(AGENT, rel, "alice")
    flow.note_platform_ahead(MACHINE, AGENT, rel, 12, T0_NS, stale=True)

    async def scene():
        async with _online(rig, machine):
            await rig.layer._initial_workspace_sync(
                MACHINE, AGENT, target_username="alice", target_role="editor")

    _run(scene())
    check_golden("file-flow/sync-refused-own-save", _norm({
        "platform": _tree(),
        "recover_bin": _rows("SELECT rel_path, reason, size, file_hash FROM recover_bin"),
        "notifications": rig.noted["notifications"],
        "marks": _marks(),
    }))


def test_an_ok_that_reports_no_written_file_is_a_refusal(rig):
    """A machine that acks a write ok but reports no committed file did not
    write it (a Windows machine skips a name with a colon): the push failed,
    no base is recorded, and the next merge pushes again instead of reading
    the file as deleted there."""
    machine = _Machine(rig.mgr)
    rel = "workspace/a:b.txt"
    _platform(rel, b"made on the platform")
    _platform("workspace/kept.txt", b"on both sides")
    machine.put("workspace/kept.txt", b"on both sides")
    machine.not_written.add(rel)

    async def merge():
        await rig.layer._initial_workspace_sync(MACHINE, AGENT, target_username=None,
                                                target_role="admin")

    async def scene():
        async with _online(rig, machine):
            await merge()
            assert sync_state_store.get_one(MACHINE, AGENT, rel) is None
            assert (MACHINE, AGENT, rel) in flow._platform_ahead
            await merge()

    _run(scene())
    assert _tree()[rel] == "made on the platform"
    assert rel not in machine.tree
    assert sync_state_store.get_one(MACHINE, AGENT, rel) is None
    assert _rows("SELECT rel_path FROM file_tombstones") == []


def test_an_ok_for_an_empty_file_is_an_ok(rig):
    """An empty file's committed stat has a size of 0: that ok records the
    file's base like any other."""
    machine = _Machine(rig.mgr)
    rel = "workspace/empty.txt"
    _platform(rel, b"")
    _platform("workspace/kept.txt", b"on both sides")
    machine.put("workspace/kept.txt", b"on both sides")

    async def scene():
        async with _online(rig, machine):
            await rig.layer._initial_workspace_sync(MACHINE, AGENT, target_username=None,
                                                    target_role="admin")

    _run(scene())
    assert machine.tree[rel][0] == b""
    assert sync_state_store.get_one(MACHINE, AGENT, rel)[0] == _sha(b"")


def test_merges_with_a_windows_machine(rig):
    """A Windows machine restores what the hash bug put on hold, and what it
    could not read is neither pulled nor taken for gone."""
    held = "workspace/held.txt"
    _platform("workspace/only-here.txt", b"the platform's")
    _platform("workspace/unreadable.txt", b"the platform's")
    sync_state_store.record_one(MACHINE, AGENT, "workspace/unreadable.txt", _sha(b"the platform's"), T0)
    file_tombstones_store.record(AGENT, held, T0 + 100.0, origin=file_tombstones_store.HOLD_ORIGIN)
    got: dict = {}

    async def merge(machine):
        async with _online(rig, machine):
            await rig.layer._initial_workspace_sync(MACHINE, AGENT, target_username=None,
                                                    target_role="admin", session_username="alice")
        return {"wire": _by_path(machine.frames), "platform": _tree(),
                "tombstones": _rows("SELECT rel_path, origin FROM file_tombstones")}

    async def scene():
        windows = _Machine(rig.mgr, version="0.5.150", os_name="windows")
        windows.put(held, b"the machine's copy", T0_NS)
        windows.skipped = [{"path": "workspace/unreadable.txt", "skipped": "sharing violation"}]
        got["windows"] = await merge(windows)
        got["restored_mtime"] = (config.AGENTS_DIR / AGENT / held).stat().st_mtime

    _run(scene())
    check_golden("file-flow/sync-windows", _norm(got))


def test_a_merge_with_nothing_to_do_and_one_with_no_answer(rig, caplog):
    machine = _Machine(rig.mgr)
    _platform("workspace/same.txt", b"same")
    machine.put("workspace/same.txt", b"same")
    sync_state_store.record_one(MACHINE, AGENT, "workspace/same.txt", _sha(b"same"), T0)
    got: dict = {}

    async def scene():
        async with _online(rig, machine):
            await rig.layer._initial_workspace_sync(MACHINE, AGENT, target_username=None, target_role="admin")
            got["in_sync_wire"] = _took(machine)
            await rig.layer._initial_workspace_sync(MACHINE, "no-such-agent", target_role="admin")
            await rig.layer._initial_workspace_sync(MACHINE, "a/b", target_role="admin")
            got["no_folder_wire"] = _took(machine)
        with caplog.at_level(logging.WARNING, logger="remote-layer"):
            await rig.layer._initial_workspace_sync(MACHINE, AGENT, target_username=None, target_role="admin")
        got["offline_log"] = [r.getMessage() for r in caplog.records if r.name == "remote-layer"]

    _run(scene())
    check_golden("file-flow/sync-no-merge", got)


def test_the_deferred_pulls(rig):
    machine = _Machine(rig.mgr)
    machine.put("workspace/deferred.bin", b"deferred bytes", 11)
    machine.no_pull["workspace/missing.bin"] = "File not found"
    (config.AGENTS_DIR / AGENT).mkdir(parents=True)
    file_tombstones_store.record(AGENT, "workspace/deferred.bin", 1.0, origin="dashboard")
    actions = [
        FileAction("workspace/deferred.bin", "pull", base_hash="sha256:s", drop_tombstone=True, keep_mtime=12345.5),
        FileAction("workspace/missing.bin", "pull", base_hash="sha256:m"),
        FileAction("workspace/../../out.bin", "pull", base_hash="sha256:o"),
    ]

    async def scene():
        async with _online(rig, machine):
            await rig.layer._run_deferred_pulls(MACHINE, AGENT, actions, "alice", {})
            await rig.layer._run_deferred_pulls(MACHINE, "a/b", actions, "alice", {})

    _run(scene())
    check_golden("file-flow/sync-deferred", _norm({
        "wire": machine.frames, "platform": _tree(),
        "mtime": (config.AGENTS_DIR / AGENT / "workspace/deferred.bin").stat().st_mtime,
        "sync_state": _rows("SELECT rel_path, base_hash FROM sync_state"),
        "authors": _rows("SELECT rel_path, last_writer FROM file_author"),
        "tombstones": _rows("SELECT rel_path FROM file_tombstones"),
        "file_updated": rig.noted["file_updated"],
    }))
