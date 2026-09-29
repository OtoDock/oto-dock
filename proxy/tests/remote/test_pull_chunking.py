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


async def _start_pull(mgr, conn, dest, *, timeout=2.0):
    """Kick off a pull and return (task, request_id, stream) once the
    file_pull request has been enqueued."""
    task = asyncio.create_task(
        mgr.pull_file_to_path(
            "m1", PathRef("agent_tree", "workspace/x.bin"), dest,
            agent_slug="agent-1", timeout=timeout,
        )
    )
    # Let pull_file_to_path open its root (a worker thread) and run up to
    # its `await wait_for(future)`.
    for _ in range(400):
        if conn.sent:
            break
        await asyncio.sleep(0.005)
    rid = conn.sent[0]["request_id"]
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

    assert await asyncio.wait_for(task, timeout=1.0) is True
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

    assert await asyncio.wait_for(task, timeout=1.0) is True
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

    assert await asyncio.wait_for(task, timeout=1.0) is True
    assert dest.read_bytes() == b""


@pytest.mark.asyncio
async def test_hash_mismatch_fails_and_cleans(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    task, rid, st = await _start_pull(mgr, conn, dest)
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, b"data", last_hash="sha256:deadbeef"))

    assert await asyncio.wait_for(task, timeout=1.0) is False
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

    assert await asyncio.wait_for(task, timeout=1.0) is False
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()


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

    assert await asyncio.wait_for(task, timeout=1.0) is False
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()


@pytest.mark.asyncio
async def test_timeout_cleans_partial(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    task, rid, st = await _start_pull(mgr, conn, dest, timeout=0.15)
    # Deliver only the first of two chunks → never finalizes → times out.
    mgr._on_pull_chunk(st, _chunk(rid, 0, 2, b"partial-data"))
    assert Path(str(dest) + ".partial").exists()  # partial opened mid-stream

    assert await asyncio.wait_for(task, timeout=1.0) is False
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()
    assert rid not in mgr._pending_pulls


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
            agent_slug="agent-1", timeout=2.0,
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
    assert await asyncio.wait_for(task, timeout=1.0) is True
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
