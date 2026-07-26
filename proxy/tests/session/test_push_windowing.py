"""Tests for the bounded-window chunked file push (proxy side, 1b).

``SatelliteConnectionManager.push_file`` sends ``write_chunk`` frames on the
BULK lane in windows of ``PUSH_WINDOW_CHUNKS``: a ``command_id`` is attached to
the last chunk of each window (and the final chunk) and the push awaits that ack
before sending the next window. The satellite commits + sha256-verifies only on
the final chunk. A non-ok / timed-out / WS-dropped window aborts the transfer.

These drive ``push_file`` against a fake connection whose ``enqueue_send``
records frames and feeds an ``ack`` back through the manager (the satellite
round-trip), so windowing + early-abort are exercised deterministically.
"""

import base64
import hashlib

import pytest

from core.remote import satellite_connection as sc
from core.remote.satellite_connection import SatelliteConnectionManager
from services.path_policy_v2 import PathRef


def _h(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _AckingConn:
    """Fake connection: records enqueued frames and auto-acks any frame carrying
    a command_id by resolving the manager's pending future (mirrors the
    satellite ack round-trip without a real WS or writer task)."""

    def __init__(self, mgr, machine_id, status_for=None):
        self.mgr = mgr
        self.machine_id = machine_id
        self.frames: list[dict] = []
        self.status_for = status_for or (lambda i, f: "ok")

    async def enqueue_send(self, msg: dict, *, bulk: bool = False) -> None:
        idx = len(self.frames)
        self.frames.append({"_bulk": bulk, **msg})
        cmd = msg.get("command_id")
        if cmd:
            status = self.status_for(idx, msg)
            await self.mgr.handle_message(self.machine_id, {
                "type": "ack", "command_id": cmd,
                "status": status, "error": "" if status == "ok" else "boom",
            })


@pytest.mark.asyncio
async def test_inline_small_push_uses_bulk_and_acks():
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    mgr._connections["m1"] = conn

    data = b"hello world"
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/x.txt"), data, agent_slug="a1",
    )
    assert ok is True
    assert len(conn.frames) == 1
    f = conn.frames[0]
    assert f["action"] == "write"
    assert f["_bulk"] is True          # file data rides the bulk lane (1a)
    assert f["hash"] == _h(data)
    assert base64.b64decode(f["content_b64"]) == data


@pytest.mark.asyncio
async def test_chunked_push_windows_and_commits(monkeypatch):
    # Shrink chunk + window so a tiny payload exercises multi-window behavior.
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4, raising=True)
    monkeypatch.setattr(sc, "PUSH_WINDOW_CHUNKS", 2, raising=True)
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    mgr._connections["m1"] = conn

    data = b"abcdefghij"  # 10 bytes / 4 → 3 chunks: [abcd][efgh][ij]
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/x.bin"), data, agent_slug="a1",
    )
    assert ok is True
    fr = conn.frames
    assert [c["action"] for c in fr] == ["write_chunk"] * 3
    assert all(c["_bulk"] is True for c in fr)
    assert [c["chunk_index"] for c in fr] == [0, 1, 2]
    assert all(c["total_chunks"] == 3 for c in fr)
    # Reassembled bytes are correct + in order.
    assert b"".join(base64.b64decode(c["content_b64"]) for c in fr) == data
    # Flush (command_id) only at the window boundary (idx 1) + final (idx 2).
    assert not fr[0].get("command_id")
    assert fr[1].get("command_id")
    assert fr[2].get("command_id")
    # Full-file hash only on the final chunk; intermediate flushes carry none.
    assert fr[0]["hash"] == "" and fr[1]["hash"] == ""
    assert fr[2]["hash"] == _h(data)


@pytest.mark.asyncio
async def test_chunked_push_aborts_early_on_error_ack(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4, raising=True)
    monkeypatch.setattr(sc, "PUSH_WINDOW_CHUNKS", 2, raising=True)
    mgr = SatelliteConnectionManager()
    # Error on every flush → the FIRST window boundary (chunk idx 1) aborts it.
    conn = _AckingConn(mgr, "m1", status_for=lambda i, f: "error")
    mgr._connections["m1"] = conn

    data = b"abcdefghijklmnop"  # 16 bytes / 4 → 4 chunks; first flush at idx 1
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/x.bin"), data, agent_slug="a1",
    )
    assert ok is False
    # Only the first window (chunks 0, 1) was sent — the rest were aborted.
    assert [c["chunk_index"] for c in conn.frames] == [0, 1]


@pytest.mark.asyncio
async def test_push_returns_false_when_not_connected():
    mgr = SatelliteConnectionManager()
    ok = await mgr.push_file(
        "missing", PathRef("agent_tree", "workspace/x.txt"), b"x", agent_slug="a1",
    )
    assert ok is False


# --- Path-source streaming (Feature D) --------------------------------------


@pytest.mark.asyncio
async def test_path_source_small_uses_inline_write(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    mgr._connections["m1"] = conn

    data = b"tiny payload"
    src = tmp_path / "x.txt"
    src.write_bytes(data)
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/x.txt"), src, agent_slug="a1",
    )
    assert ok is True
    assert len(conn.frames) == 1
    f = conn.frames[0]
    assert f["action"] == "write"
    assert f["hash"] == _h(data)
    assert base64.b64decode(f["content_b64"]) == data


@pytest.mark.asyncio
async def test_path_source_chunked_reassembles_and_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4, raising=True)
    monkeypatch.setattr(sc, "PUSH_WINDOW_CHUNKS", 2, raising=True)
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    mgr._connections["m1"] = conn

    data = b"abcdefghij"  # 3 chunks of 4
    src = tmp_path / "x.bin"
    src.write_bytes(data)
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/x.bin"), src, agent_slug="a1",
    )
    assert ok is True
    fr = conn.frames
    assert [c["action"] for c in fr] == ["write_chunk"] * 3
    assert b"".join(base64.b64decode(c["content_b64"]) for c in fr) == data
    assert fr[2]["hash"] == _h(data)  # streamed pre-hash == content hash


@pytest.mark.asyncio
async def test_path_source_missing_file_returns_false(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    mgr._connections["m1"] = conn
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/x.bin"), tmp_path / "nope.bin",
        agent_slug="a1",
    )
    assert ok is False
    assert conn.frames == []


@pytest.mark.asyncio
async def test_path_source_shrink_mid_push_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4, raising=True)
    monkeypatch.setattr(sc, "PUSH_WINDOW_CHUNKS", 2, raising=True)
    mgr = SatelliteConnectionManager()
    src = tmp_path / "x.bin"
    src.write_bytes(b"abcdefghijklmnop")  # 4 chunks

    def _truncate_on_first_flush(idx, frame):
        # After the first window ack the file shrinks under the reader.
        src.write_bytes(b"abcd")
        return "ok"

    conn = _AckingConn(mgr, "m1", status_for=_truncate_on_first_flush)
    mgr._connections["m1"] = conn
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/x.bin"), src, agent_slug="a1",
    )
    assert ok is False
    # First window (chunks 0,1) went out; the short read aborted before any
    # further frame — never a truncated stream under a stale total_chunks.
    assert [c["chunk_index"] for c in conn.frames] == [0, 1]


@pytest.mark.asyncio
async def test_progress_cb_flush_points_and_terminal(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4, raising=True)
    monkeypatch.setattr(sc, "PUSH_WINDOW_CHUNKS", 2, raising=True)
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    mgr._connections["m1"] = conn

    ticks: list[tuple[int, int]] = []
    data = b"abcdefghijklmnopqr"  # 18 bytes → 5 chunks; flush at idx 1, 3, 4
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "w.bin"), data, agent_slug="a1",
        progress_cb=lambda s, t: ticks.append((s, t)),
    )
    assert ok is True
    assert ticks == [(8, 18), (16, 18), (18, 18)]  # window acks + terminal


@pytest.mark.asyncio
async def test_progress_cb_async_and_raising(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4, raising=True)
    monkeypatch.setattr(sc, "PUSH_WINDOW_CHUNKS", 2, raising=True)
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    mgr._connections["m1"] = conn

    seen: list[tuple[int, int]] = []

    async def _async_cb(s, t):
        seen.append((s, t))
        raise RuntimeError("broken callback")

    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "w.bin"), b"abcdefghij", agent_slug="a1",
        progress_cb=_async_cb,
    )
    assert ok is True          # a raising callback never aborts the transfer
    assert seen[-1] == (10, 10)  # async cb was awaited


@pytest.mark.asyncio
async def test_inline_push_progress_terminal():
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    mgr._connections["m1"] = conn
    ticks: list[tuple[int, int]] = []
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "w.txt"), b"hi", agent_slug="a1",
        progress_cb=lambda s, t: ticks.append((s, t)),
    )
    assert ok is True
    assert ticks == [(2, 2)]


# --- Per-satellite cap gate (Feature D) --------------------------------------


@pytest.mark.asyncio
async def test_old_satellite_capped_at_legacy(monkeypatch):
    """A pre-0.5.103 satellite must never be sent a file above the legacy cap
    even when the config cap is higher."""
    monkeypatch.setattr("core.remote.file_sync.MAX_FILE_SIZE", 1000, raising=True)
    monkeypatch.setattr(
        "core.remote.file_sync.LEGACY_SYNC_MAX_FILE_BYTES", 10, raising=True,
    )
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    conn.satellite_version = "0.5.102"
    mgr._connections["m1"] = conn

    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "w.bin"), b"x" * 11, agent_slug="a1",
    )
    assert ok is False
    assert conn.frames == []  # gated BEFORE any frame goes out

    # Same payload to a 0.5.103 satellite proceeds (config cap applies).
    conn2 = _AckingConn(mgr, "m2")
    conn2.satellite_version = "0.5.103"
    mgr._connections["m2"] = conn2
    ok2 = await mgr.push_file(
        "m2", PathRef("agent_tree", "w.bin"), b"x" * 11, agent_slug="a1",
    )
    assert ok2 is True


@pytest.mark.asyncio
async def test_config_cap_enforced_for_new_satellite(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_FILE_SIZE", 8, raising=True)
    mgr = SatelliteConnectionManager()
    conn = _AckingConn(mgr, "m1")
    conn.satellite_version = "0.5.103"
    mgr._connections["m1"] = conn
    ok = await mgr.push_file(
        "m1", PathRef("agent_tree", "w.bin"), b"x" * 9, agent_slug="a1",
    )
    assert ok is False
    assert conn.frames == []
