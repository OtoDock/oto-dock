"""Tests for the streaming chunked file pull (proxy side).

``SatelliteConnectionManager.pull_file_to_path`` registers a ``_PullStream``,
sends one ``file_pull``, and reassembles the satellite's ``file_content`` chunks
straight to a ``.partial`` on disk — atomically renamed (sha256-verified) on the
final chunk. These tests drive chunks directly through ``_on_pull_chunk`` so we
exercise reassembly, hash verification, the size cap, error responses, and
cleanup without a real WebSocket.
"""

import asyncio
import base64
import hashlib
import time
from pathlib import Path

import pytest

from core.remote.satellite_connection import SatelliteConnectionManager
from services.path_policy_v2 import PathRef


class _FakeConn:
    """Captures messages the manager enqueues (the file_pull request)."""

    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg: dict) -> None:
        self.sent.append(msg)


def _chunk(rid, idx, total, data, *, last_hash=""):
    return {
        "request_id": rid,
        "path": "workspace/x.bin",
        "chunk_index": idx,
        "total_chunks": total,
        "content_b64": base64.b64encode(data).decode(),
        "hash": last_hash,
    }


async def _start_pull(mgr, conn, dest, *, stall_s=2.0):
    """Kick off a pull and return (task, request_id, stream) once the
    file_pull request has been enqueued."""
    before = sum(1 for m in conn.sent if m.get("type") == "file_pull")
    task = asyncio.create_task(
        mgr.pull_file_to_path(
            "m1", PathRef("agent_tree", "workspace/x.bin"), dest,
            agent_slug="agent-1", stall_s=stall_s,
        )
    )
    # Let pull_file_to_path open its root (a worker thread) and run up to
    # its `await wait_for(future)`.
    pulls: list = []
    for _ in range(400):
        pulls = [m for m in conn.sent if m.get("type") == "file_pull"]
        if len(pulls) > before:
            break
        await asyncio.sleep(0.005)
    rid = pulls[-1]["request_id"]
    return task, rid, mgr._pending_pulls[rid]


@pytest.mark.asyncio
async def test_multi_chunk_reassembles(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    data = b"A" * 1000 + b"B" * 1000 + b"C" * 137
    task, rid, st = await _start_pull(mgr, conn, dest)

    h = hashlib.sha256()
    parts = [data[0:1000], data[1000:2000], data[2000:]]
    for i, part in enumerate(parts):
        h.update(part)
        is_last = i == len(parts) - 1
        mgr._on_pull_chunk(
            st, _chunk(rid, i, len(parts), part,
                       last_hash=f"sha256:{h.hexdigest()}" if is_last else ""),
        )

    assert await asyncio.wait_for(task, timeout=5.0) is True
    assert dest.read_bytes() == data
    assert not Path(str(dest) + ".partial").exists()
    assert rid not in mgr._pending_pulls


@pytest.mark.asyncio
async def test_single_chunk_success(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    data = b"small-payload"
    task, rid, st = await _start_pull(mgr, conn, dest)
    h = hashlib.sha256(data)
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, data, last_hash=f"sha256:{h.hexdigest()}"))

    assert await asyncio.wait_for(task, timeout=5.0) is True
    assert dest.read_bytes() == data


@pytest.mark.asyncio
async def test_empty_file(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "empty.bin"

    task, rid, st = await _start_pull(mgr, conn, dest)
    h = hashlib.sha256(b"")
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, b"", last_hash=f"sha256:{h.hexdigest()}"))

    assert await asyncio.wait_for(task, timeout=5.0) is True
    assert dest.read_bytes() == b""


@pytest.mark.asyncio
async def test_hash_mismatch_fails_and_cleans(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    task, rid, st = await _start_pull(mgr, conn, dest)
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, b"data", last_hash="sha256:deadbeef"))

    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()


@pytest.mark.asyncio
async def test_error_response_fails(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    task, rid, st = await _start_pull(mgr, conn, dest)
    mgr._on_pull_chunk(st, {"request_id": rid, "path": "x", "error": "File not found"})

    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("error,level", [("File not found", "DEBUG"), ("Permission denied", "WARNING")])
async def test_a_missing_path_is_logged_at_debug_other_failures_warn(tmp_path, caplog, error, level):
    # A lazy pull of a file the machine does not have yet (an output a tool
    # has not written) is routine; a real failure stays a warning.
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "out.bin")
    with caplog.at_level("DEBUG"):
        mgr._on_pull_chunk(st, {"request_id": rid, "path": "x", "error": error})
        assert await asyncio.wait_for(task, timeout=5.0) is False
    rows = [r for r in caplog.records if "file pull failed" in r.getMessage()]
    assert [r.levelname for r in rows] == [level]


@pytest.mark.asyncio
async def test_size_cap_fails(tmp_path, monkeypatch):
    import core.remote.file_sync as fs
    monkeypatch.setattr(fs, "MAX_FILE_SIZE", 10)

    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    task, rid, st = await _start_pull(mgr, conn, dest)
    # 20 bytes > 10-byte cap → reject before writing a finished file.
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, b"X" * 20, last_hash="sha256:whatever"))

    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()


@pytest.mark.asyncio
async def test_a_stall_cleans_partial(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    task, rid, st = await _start_pull(mgr, conn, dest, stall_s=0.15)
    # Deliver only the first of two chunks → never finalizes → stalls.
    mgr._on_pull_chunk(st, _chunk(rid, 0, 2, b"partial-data"))
    assert Path(str(dest) + ".partial").exists()  # partial opened mid-stream

    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()
    assert rid not in mgr._pending_pulls


@pytest.mark.asyncio
async def test_a_pull_arriving_steadily_past_the_stall_succeeds(tmp_path):
    """The old overall deadline is gone: chunks that keep coming keep the
    pull alive however long it runs."""
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"
    task, rid, st = await _start_pull(mgr, conn, dest, stall_s=0.6)
    parts = [bytes([i]) * 100 for i in range(10)]
    h = hashlib.sha256()
    for i, part in enumerate(parts):
        await asyncio.sleep(0.15)    # 1.5 s in all, each gap well under the stall
        h.update(part)
        last = i == len(parts) - 1
        await mgr.handle_message("m1", {"type": "file_content", **_chunk(
            rid, i, len(parts), part, last_hash=f"sha256:{h.hexdigest()}" if last else "")})
    assert await asyncio.wait_for(task, timeout=5.0) is True
    assert dest.read_bytes() == b"".join(parts)


@pytest.mark.asyncio
async def test_a_pull_waiting_behind_another_pull_is_not_stalled(tmp_path):
    """Below 0.5.137 a satellite sends one pull whole before the next one's
    first chunk: the second pull waits past its stall while the link moves,
    and still succeeds."""
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    first, rid1, _ = await _start_pull(mgr, conn, tmp_path / "a.bin", stall_s=0.5)
    second = asyncio.create_task(mgr.pull_file_to_path(
        "m1", PathRef("agent_tree", "workspace/y.bin"), tmp_path / "b.bin",
        agent_slug="agent-1", stall_s=0.5))
    while len(conn.sent) < 2:
        await asyncio.sleep(0.005)
    rid2 = conn.sent[1]["request_id"]
    h = hashlib.sha256()
    for i in range(8):                # the first file, 0.96 s
        await asyncio.sleep(0.12)
        h.update(b"a")
        await mgr.handle_message("m1", {"type": "file_content", **_chunk(
            rid1, i, 8, b"a", last_hash=f"sha256:{h.hexdigest()}" if i == 7 else "")})
    assert not second.done()
    await mgr.handle_message("m1", {"type": "file_content", **_chunk(
        rid2, 0, 1, b"b", last_hash=f"sha256:{hashlib.sha256(b'b').hexdigest()}")})
    assert await asyncio.wait_for(first, timeout=5.0) is True
    assert await asyncio.wait_for(second, timeout=5.0) is True


@pytest.mark.asyncio
async def test_a_pull_whose_connection_was_replaced_meanwhile_goes_out_on_the_new_one(tmp_path, monkeypatch):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    new = _FakeConn()
    mgr._connections["m1"] = conn
    real_open = mgr._open_pull_root

    def _open_and_replace(dest):
        mgr._connections["m1"] = new   # a reconnect while the root opened
        return real_open(dest)

    monkeypatch.setattr(mgr, "_open_pull_root", _open_and_replace)
    task = asyncio.create_task(mgr.pull_file_to_path(
        "m1", PathRef("agent_tree", "workspace/x.bin"), tmp_path / "out.bin",
        agent_slug="agent-1"))
    for _ in range(400):
        if new.sent:
            break
        await asyncio.sleep(0.005)
    assert conn.sent == [] and [m["type"] for m in new.sent] == ["file_pull"]
    rid = new.sent[0]["request_id"]
    mgr._on_pull_chunk(mgr._pending_pulls[rid], {"request_id": rid, "error": "File not found"})
    assert await asyncio.wait_for(task, timeout=2.0) is False
    assert mgr._pending_pulls == {}


@pytest.mark.asyncio
async def test_a_deregister_leaves_a_complete_pull_to_its_waiter(tmp_path):
    from unittest.mock import patch
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    conn.session_queues = {}
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"
    task, rid, st = await _start_pull(mgr, conn, dest)
    data = b"whole"
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, data,
                                  last_hash=f"sha256:{hashlib.sha256(data).hexdigest()}"))
    with patch("storage.remote_store.update_machine_status"):
        await mgr.deregister("m1")     # before the waiter resumed
    assert await asyncio.wait_for(task, timeout=5.0) is True
    assert dest.read_bytes() == data


@pytest.mark.asyncio
async def test_no_connection_returns_false(tmp_path):
    mgr = SatelliteConnectionManager()
    dest = tmp_path / "out.bin"
    ok = await mgr.pull_file_to_path(
        "missing", PathRef("agent_tree", "workspace/x.bin"), dest,
        agent_slug="agent-1",
    )
    assert ok is False
    assert not dest.exists()


# ---------------------------------------------------------------------------
# A pull into the agents tree opens beneath the agents root: no component
# is followed, a link at the partial's name is replaced, never written through
# ---------------------------------------------------------------------------


def _agents_tree(tmp_path, monkeypatch):
    import config
    agents = tmp_path / "agents"
    (agents / "agent-1").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    return agents, outside


@pytest.mark.asyncio
async def test_a_link_at_a_parent_never_carries_the_pull_outside_the_tree(tmp_path, monkeypatch):
    agents, outside = _agents_tree(tmp_path, monkeypatch)
    (agents / "agent-1" / "workspace").symlink_to(outside)
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = agents / "agent-1" / "workspace" / "x.bin"
    data = b"Q" * 10
    ok = await asyncio.wait_for(
        mgr.pull_file_to_path(
            "m1", PathRef("agent_tree", "workspace/x.bin"), dest,
            agent_slug="agent-1", stall_s=2.0,
        ),
        timeout=2.0,
    )
    assert ok is False
    assert conn.sent == []
    assert not (outside / "x.bin").exists()
    assert not (outside / "x.bin.partial").exists()
    assert mgr._pending_pulls == {}
    assert data


@pytest.mark.asyncio
async def test_a_link_at_the_partial_name_is_replaced_never_written_through(tmp_path, monkeypatch):
    agents, outside = _agents_tree(tmp_path, monkeypatch)
    (agents / "agent-1" / "workspace").mkdir()
    target = outside / "t"
    target.write_bytes(b"keep")
    dest = agents / "agent-1" / "workspace" / "x.bin"
    Path(str(dest) + ".partial").symlink_to(target)
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    data = b"Q" * 10
    task, rid, st = await _start_pull(mgr, conn, dest)
    h = hashlib.sha256(data)
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, data, last_hash=f"sha256:{h.hexdigest()}"))
    assert await asyncio.wait_for(task, timeout=5.0) is True
    assert dest.read_bytes() == data
    assert target.read_bytes() == b"keep"
    assert not Path(str(dest) + ".partial").exists()
    assert rid not in mgr._pending_pulls


# ---------------------------------------------------------------------------
# The commit runs on the file-commit executor, after the stream left the
# pending map
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_runs_off_the_loop_after_the_stream_is_popped(tmp_path, monkeypatch):
    import os
    import threading
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"
    data = b"Z" * 3000
    task, rid, st = await _start_pull(mgr, conn, dest)
    seen = {}
    real_fsync = os.fsync

    def _fsync(fd):
        seen["thread"] = threading.current_thread().name
        seen["pending"] = rid in mgr._pending_pulls
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", _fsync)
    h = hashlib.sha256(data)
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, data, last_hash=f"sha256:{h.hexdigest()}"))
    assert await task is True
    assert dest.read_bytes() == data
    assert seen["thread"].startswith("file-commit")
    assert seen["pending"] is False
    # A late chunk after the final one is ignored.
    mgr._on_pull_chunk(st, _chunk(rid, 1, 1, b"LATE"))
    assert dest.read_bytes() == data
    assert not Path(str(dest) + ".partial").exists()


# ---------------------------------------------------------------------------
# A pull the proxy gives up on is cancelled on a 0.5.137 satellite
# ---------------------------------------------------------------------------


class _PacedConn(_FakeConn):
    """A satellite that paces its pulls (0.5.137): the cancel goes on the
    control lane without an await."""

    def __init__(self, version="0.5.137", paced=True):
        super().__init__()
        self.satellite_version = version
        self.capabilities = {"paced_transfers": paced}

    def enqueue_send_nowait(self, msg, *, bulk=False):
        self.sent.append(msg)


def _cancels(conn):
    return [m for m in conn.sent if m.get("type") == "file_pull_cancel"]


@pytest.mark.asyncio
async def test_a_stalled_pull_is_cancelled_on_the_satellite(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _PacedConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "out.bin", stall_s=0.1)
    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert _cancels(conn) == [{"type": "file_pull_cancel", "request_id": rid}]


@pytest.mark.asyncio
async def test_a_pull_its_caller_left_is_cancelled_and_a_proxy_refusal_too(tmp_path, monkeypatch):
    mgr = SatelliteConnectionManager()
    conn = _PacedConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "a.bin")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m["request_id"] for m in _cancels(conn)] == [rid]

    monkeypatch.setattr("core.remote.file_sync.MAX_FILE_SIZE", 4)
    task, rid2, st = await _start_pull(mgr, conn, tmp_path / "b.bin")
    mgr._on_pull_chunk(st, _chunk(rid2, 0, 2, b"too large"))
    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert [m["request_id"] for m in _cancels(conn)] == [rid, rid2]


@pytest.mark.asyncio
async def test_a_pull_the_satellite_ended_or_an_old_satellite_gets_no_cancel(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _PacedConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "a.bin")
    mgr._on_pull_chunk(st, {"request_id": rid, "error": "File not found"})
    assert await asyncio.wait_for(task, timeout=5.0) is False
    for old in (_PacedConn(version="0.5.136"), _PacedConn(paced=False)):
        mgr._connections["m1"] = old
        task, rid, st = await _start_pull(mgr, old, tmp_path / "b.bin", stall_s=0.1)
        assert await asyncio.wait_for(task, timeout=5.0) is False
        assert _cancels(old) == []
    assert _cancels(conn) == []


@pytest.mark.asyncio
async def test_a_pull_whose_connection_was_replaced_sends_no_cancel(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _PacedConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "a.bin", stall_s=0.2)
    new = _PacedConn()
    mgr._connections["m1"] = new          # replaced: the old socket answers nothing
    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert _cancels(conn) == [] and _cancels(new) == []


@pytest.mark.asyncio
async def test_a_pull_has_no_ceiling_before_its_first_chunk(tmp_path):
    """A pull an older satellite queues behind another one for more than
    ten minutes waits on the connection's progress alone."""
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "out.bin", stall_s=0.5)
    st.started -= 700                    # queued for 11+ minutes
    for _ in range(4):
        conn.last_transfer_at = time.monotonic()   # the connection moves
        await asyncio.sleep(0.1)
    assert not task.done()
    h = hashlib.sha256(b"z")
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, b"z", last_hash=f"sha256:{h.hexdigest()}"))
    assert await asyncio.wait_for(task, 5.0) is True


@pytest.mark.asyncio
async def test_a_pull_a_paced_satellite_never_starts_fails_at_its_floor(tmp_path):
    """A 0.5.137 satellite interleaves its pulls, so one that sends no
    chunk for ten minutes while the connection moves has failed."""
    mgr = SatelliteConnectionManager()
    conn = _PacedConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "out.bin", stall_s=0.5)
    st.started -= 700
    for _ in range(8):                    # the connection keeps moving
        conn.last_transfer_at = time.monotonic()
        await asyncio.sleep(0.2)
        if task.done():
            break
    assert task.done() and task.result() is False     # the floor, not a stall
    assert _cancels(conn)[-1]["request_id"] == rid


@pytest.mark.asyncio
async def test_a_caller_that_leaves_during_the_commit_leaves_no_partial(tmp_path, monkeypatch):
    """The commit job runs whatever the caller does: cancelled while the
    job waits for a pool worker, the pull cleans up instead of renaming
    (the caller's path lock went with it), leaving no partial and no root
    descriptor."""
    from core import file_commit
    gate = asyncio.Event()
    real_run = file_commit.run

    async def held_run(fn, *args):
        await gate.wait()
        return await real_run(fn, *args)
    monkeypatch.setattr(file_commit, "run", held_run)
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"
    task, rid, st = await _start_pull(mgr, conn, dest)
    h = hashlib.sha256(b"z")
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, b"z", last_hash=f"sha256:{h.hexdigest()}"))
    await asyncio.sleep(0.05)              # the caller waits on the held commit
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    for _ in range(100):
        if st.rootfd == -1:
            break
        await asyncio.sleep(0.01)
    assert st.rootfd == -1 and st.abandoned
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()
