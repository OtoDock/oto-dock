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


class _PinnedClock:
    """The pull's stall clock, moved by the test alone: a worker slowed by
    a loaded box never reads as a stalled connection."""

    def __init__(self) -> None:
        self.now = time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def pinned_clock(monkeypatch):
    from core.remote import satellite_file_transfer
    clock = _PinnedClock()
    monkeypatch.setattr(satellite_file_transfer, "time", clock)
    return clock


def _partials(dest: Path) -> list[Path]:
    """The staging files a pull into ``dest`` left beside it."""
    return sorted(dest.parent.glob(f".{dest.name}.*.partial"))


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
    assert _partials(dest) == []
    assert rid not in mgr._pending_pulls


@pytest.mark.asyncio
async def test_the_progress_callback_hears_each_chunk_and_never_stops_the_pull(tmp_path):
    """A move's ring follows a pull: the callback gets the bytes received
    and the size the caller expects after each written chunk; one that
    raises is dropped and the pull completes."""
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"
    data = b"A" * 1000 + b"B" * 1000
    seen: list[tuple[int, int]] = []

    def progress(got, expected):
        seen.append((got, expected))
        raise RuntimeError("a broken listener")

    task = asyncio.create_task(mgr.pull_file_to_path(
        "m1", PathRef("agent_tree", "workspace/x.bin"), dest, agent_slug="agent-1",
        stall_s=2.0, max_bytes=len(data), progress_cb=progress))
    for _ in range(400):
        if any(m.get("type") == "file_pull" for m in conn.sent):
            break
        await asyncio.sleep(0.005)
    rid = conn.sent[-1]["request_id"]
    st = mgr._pending_pulls[rid]
    mgr._on_pull_chunk(st, _chunk(rid, 0, 2, data[:1000]))
    mgr._on_pull_chunk(st, _chunk(rid, 1, 2, data[1000:],
                                  last_hash=f"sha256:{hashlib.sha256(data).hexdigest()}"))
    assert await asyncio.wait_for(task, timeout=5.0) is True
    assert seen == [(1000, 2000), (2000, 2000)]
    assert dest.read_bytes() == data


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
    assert _partials(dest) == []


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
    assert _partials(dest) == []


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
    assert _partials(dest) == []


@pytest.mark.asyncio
async def test_the_callers_cap_fails_before_the_bytes_land(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"
    task = asyncio.create_task(mgr.pull_file_to_path(
        "m1", PathRef("agent_tree", "workspace/x.bin"), dest,
        agent_slug="agent-1", stall_s=2.0, max_bytes=8,
    ))
    for _ in range(400):
        if any(m.get("type") == "file_pull" for m in conn.sent):
            break
        await asyncio.sleep(0.005)
    rid = next(m["request_id"] for m in conn.sent if m.get("type") == "file_pull")
    st = mgr._pending_pulls[rid]
    mgr._on_pull_chunk(st, _chunk(rid, 0, 2, b"X" * 6))
    mgr._on_pull_chunk(st, _chunk(rid, 1, 2, b"X" * 6, last_hash="sha256:whatever"))
    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert not dest.exists()
    assert _partials(dest) == []


@pytest.mark.parametrize("cap, chunks, landed", [
    (8, [b"X" * 9], False),             # past the cap on the first chunk
    (8, [b"X" * 4, b"X" * 4], True),    # exactly at the cap
    (0, [b"X" * 40], True),             # 0 and None: only the sync's own cap
    (None, [b"X" * 40], True),
])
@pytest.mark.asyncio
async def test_the_callers_cap_at_every_edge(tmp_path, cap, chunks, landed):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"
    task = asyncio.create_task(mgr.pull_file_to_path(
        "m1", PathRef("agent_tree", "workspace/x.bin"), dest,
        agent_slug="agent-1", stall_s=2.0, max_bytes=cap,
    ))
    for _ in range(400):
        if any(m.get("type") == "file_pull" for m in conn.sent):
            break
        await asyncio.sleep(0.005)
    rid = next(m["request_id"] for m in conn.sent if m.get("type") == "file_pull")
    st = mgr._pending_pulls[rid]
    digest = f"sha256:{hashlib.sha256(b''.join(chunks)).hexdigest()}"
    for i, data in enumerate(chunks):
        last = i == len(chunks) - 1
        mgr._on_pull_chunk(st, _chunk(rid, i, len(chunks), data, last_hash=digest if last else ""))
    assert await asyncio.wait_for(task, timeout=5.0) is landed
    assert dest.exists() is landed
    assert _partials(dest) == []


@pytest.mark.asyncio
async def test_a_stall_cleans_partial(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"

    task, rid, st = await _start_pull(mgr, conn, dest, stall_s=0.15)
    # Deliver only the first of two chunks → never finalizes → stalls.
    mgr._on_pull_chunk(st, _chunk(rid, 0, 2, b"partial-data"))
    assert len(_partials(dest)) == 1  # partial opened mid-stream

    assert await asyncio.wait_for(task, timeout=5.0) is False
    assert not dest.exists()
    assert _partials(dest) == []
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
    while len(conn.sent) < 2:  # noqa: ASYNC110  # a fake connection's sent list: the fake has no event
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
    assert not list(outside.glob("*x.bin*"))
    assert mgr._pending_pulls == {}
    assert data


@pytest.mark.asyncio
async def test_nothing_planted_beside_the_destination_is_written_into(tmp_path, monkeypatch):
    """A pull stages under its own random name: a link or a hard link to
    another file planted at a predictable staging name is never opened."""
    import os
    agents, outside = _agents_tree(tmp_path, monkeypatch)
    ws = agents / "agent-1" / "workspace"
    ws.mkdir()
    target = outside / "t"
    target.write_bytes(b"keep")
    other = ws / "other.txt"
    other.write_bytes(b"theirs")
    dest = ws / "x.bin"
    Path(str(dest) + ".partial").symlink_to(target)  # noqa: ASYNC240  # a test's own temporary files
    os.link(other, ws / ".x.bin.partial")
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    data = b"Q" * 10
    task, rid, st = await _start_pull(mgr, conn, dest)
    h = hashlib.sha256(data)
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, data, last_hash=f"sha256:{h.hexdigest()}"))
    assert await asyncio.wait_for(task, timeout=5.0) is True
    assert dest.read_bytes() == data
    assert target.read_bytes() == b"keep" and other.read_bytes() == b"theirs"
    assert _partials(dest) == []
    assert rid not in mgr._pending_pulls


@pytest.mark.asyncio
async def test_two_pulls_into_one_destination_never_share_a_staging_file(tmp_path):
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "out.bin"
    t1, r1, s1 = await _start_pull(mgr, conn, dest)
    t2, r2, s2 = await _start_pull(mgr, conn, dest)
    assert s1.partial_rel != s2.partial_rel
    mgr._on_pull_chunk(s1, _chunk(r1, 0, 2, b"one-"))
    mgr._on_pull_chunk(s2, _chunk(r2, 0, 2, b"two-"))
    for st, rid, part in ((s1, r1, b"one-"), (s2, r2, b"two-")):
        h = hashlib.sha256(part + b"end")
        mgr._on_pull_chunk(st, _chunk(rid, 1, 2, b"end", last_hash=f"sha256:{h.hexdigest()}"))
    assert await asyncio.wait_for(t1, timeout=5.0) is True
    assert await asyncio.wait_for(t2, timeout=5.0) is True
    # Whichever commit ran last, the file is one whole pull, never a mix.
    assert dest.read_bytes() in (b"one-end", b"two-end")
    assert _partials(dest) == []


@pytest.mark.asyncio
async def test_only_a_platform_copy_primes_the_sync_hash_cache(tmp_path, monkeypatch):
    from core.remote import file_sync
    agents, _ = _agents_tree(tmp_path, monkeypatch)
    (agents / "agent-1" / "workspace").mkdir()
    primed: list = []
    monkeypatch.setattr(file_sync, "prime_hash_cache", lambda p, h: primed.append(p))
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    data = b"Q" * 10
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    for dest in (agents / "agent-1" / "workspace" / "x.bin", tmp_path / "snapshot" / "x.bin"):
        task, rid, st = await _start_pull(mgr, conn, dest)
        mgr._on_pull_chunk(st, _chunk(rid, 0, 1, data, last_hash=digest))
        assert await asyncio.wait_for(task, timeout=5.0) is True
    assert primed == [agents / "agent-1" / "workspace" / "x.bin"]


@pytest.mark.asyncio
async def test_a_pull_cancelled_while_its_root_opens_closes_it(tmp_path, monkeypatch):
    """The root opens in a thread that runs on after a cancel: the caller
    waits for it (its mkdir never lands after the caller cleaned up) and
    the handle it opened is closed."""
    import os
    import threading
    opened: list[int] = []
    release = threading.Event()
    real = SatelliteConnectionManager._open_pull_root

    def slow_open(dest):
        release.wait(5)
        out = real(dest)
        opened.append(out[0])
        return out

    monkeypatch.setattr(SatelliteConnectionManager, "_open_pull_root", staticmethod(slow_open))
    mgr = SatelliteConnectionManager()
    mgr._connections["m1"] = _FakeConn()
    task = asyncio.create_task(mgr.pull_file_to_path(
        "m1", PathRef("agent_tree", "workspace/x.bin"), tmp_path / "d" / "x.bin",
        agent_slug="agent-1"))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()            # waiting for the thread, not gone
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    assert opened and (tmp_path / "d").is_dir()
    with pytest.raises(OSError):
        os.fstat(opened[0])


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
    assert _partials(dest) == []


@pytest.mark.asyncio
async def test_a_caller_cancelled_during_the_commit_waits_for_the_job(tmp_path, monkeypatch):
    """The commit job runs on after a cancel: the caller marks the stream
    abandoned and waits for the job before the cancel propagates, so its
    own cleanup (a template read removing its temp folder) never races a
    rename into it; the job finds the mark and removes the staged bytes."""
    import threading
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    dest = tmp_path / "d" / "out.bin"
    task, rid, st = await _start_pull(mgr, conn, dest)
    release = threading.Event()
    ran: list[bool] = []
    real_commit = SatelliteConnectionManager._commit_pull_sync

    def slow_commit(self, stream):
        release.wait(5)
        ok = real_commit(self, stream)
        ran.append(ok)
        return ok
    monkeypatch.setattr(SatelliteConnectionManager, "_commit_pull_sync", slow_commit)
    data = b"Q" * 100
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, data, last_hash=f"sha256:{hashlib.sha256(data).hexdigest()}"))
    await asyncio.sleep(0.05)       # the job is parked on the executor
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done() and st.abandoned      # waiting for the job, not gone
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    assert ran == [False]
    assert not dest.exists() and _partials(dest) == []


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
async def test_a_pull_has_no_ceiling_before_its_first_chunk(tmp_path, pinned_clock):
    """A pull an older satellite queues behind another one for more than
    ten minutes waits on the connection's progress alone."""
    mgr = SatelliteConnectionManager()
    conn = _FakeConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "out.bin", stall_s=0.5)
    st.started -= 700                    # queued for 11+ minutes
    conn.last_transfer_at = pinned_clock.now      # the connection moves
    await asyncio.sleep(0.4)
    assert not task.done()
    h = hashlib.sha256(b"z")
    mgr._on_pull_chunk(st, _chunk(rid, 0, 1, b"z", last_hash=f"sha256:{h.hexdigest()}"))
    assert await asyncio.wait_for(task, 5.0) is True


@pytest.mark.asyncio
async def test_a_pull_a_paced_satellite_never_starts_fails_at_its_floor(tmp_path, pinned_clock):
    """A 0.5.137 satellite interleaves its pulls, so one that sends no
    chunk for ten minutes while the connection moves has failed."""
    mgr = SatelliteConnectionManager()
    conn = _PacedConn()
    mgr._connections["m1"] = conn
    task, rid, st = await _start_pull(mgr, conn, tmp_path / "out.bin", stall_s=0.5)
    st.started -= 700
    conn.last_transfer_at = pinned_clock.now      # the connection keeps moving
    for _ in range(8):
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
    descriptor, and the caller's cancel propagates once the job is done."""
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
    await asyncio.sleep(0.05)
    assert not task.done() and st.abandoned   # waiting for the job, not gone
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    assert st.rootfd == -1 and st.abandoned
    assert not dest.exists()
    assert _partials(dest) == []
