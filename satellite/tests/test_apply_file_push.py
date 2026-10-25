"""Tests for ``file_sync.apply_file_push`` hardening (sat 0.5.29).

The agent-tree push applier is brought to parity with the already-correct
satellite-host path:

  * the FIRST chunk truncates (a stale ``.partial`` from an aborted prior
    transfer must never be appended to → silent corruption),
  * the chunk-assembled bytes are sha256-verified against the platform's
    full-file hash BEFORE the atomic commit (and the inline write verifies too),
  * both write paths enforce the MAX_FILE_SIZE receive cap,
  * ``*.partial`` staging files are excluded from both manifests.

These drive ``apply_file_push`` / ``snapshot_agent_dir`` / ``compute_manifest``
directly with crafted messages, so they exercise the exact behavior.
"""

import base64
import hashlib
import os
import time

import pytest

from satellite.transport import file_sync
from satellite.transport.file_sync import (
    apply_file_push,
    compute_manifest,
    snapshot_agent_dir,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _h(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _chunk(rel: str, idx: int, total: int, data: bytes, *, last_hash: str = ""):
    return {
        "agent_slug": "a1",
        "path": rel,
        "action": "write_chunk",
        "chunk_index": idx,
        "total_chunks": total,
        "content_b64": _b64(data),
        "hash": last_hash,
    }


def test_chunked_write_happy_path(tmp_path):
    rel = "workspace/f.bin"
    parts = [b"abcd", b"efgh", b"ij"]
    data = b"".join(parts)
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        apply_file_push(
            tmp_path, _chunk(rel, i, 3, p, last_hash=_h(data) if last else "")
        )
    assert (tmp_path / rel).read_bytes() == data
    assert not (tmp_path / "workspace" / "f.bin.partial").exists()


def test_first_chunk_truncates_stale_partial(tmp_path):
    """A leftover .partial from an aborted transfer must not corrupt the next
    push — the first chunk truncates it (the core 1c fix)."""
    rel = "workspace/f.bin"
    (tmp_path / "workspace").mkdir(parents=True)
    (tmp_path / "workspace" / "f.bin.partial").write_bytes(b"GARBAGE-LEFTOVER")

    parts = [b"abcd", b"efij"]
    data = b"".join(parts)
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        apply_file_push(
            tmp_path, _chunk(rel, i, 2, p, last_hash=_h(data) if last else "")
        )
    # No garbage prefix: the stale partial was truncated by chunk 0, and the
    # hash verified clean on commit.
    assert (tmp_path / rel).read_bytes() == data


def test_chunked_hash_mismatch_rejected(tmp_path):
    rel = "workspace/f.bin"
    apply_file_push(tmp_path, _chunk(rel, 0, 2, b"abcd"))
    with pytest.raises(ValueError, match="hash mismatch"):
        apply_file_push(
            tmp_path, _chunk(rel, 1, 2, b"efgh", last_hash="sha256:deadbeef")
        )
    # No corrupt commit; the .partial is cleaned up.
    assert not (tmp_path / rel).exists()
    assert not (tmp_path / "workspace" / "f.bin.partial").exists()


def test_inline_write_ok_with_correct_hash(tmp_path):
    data = b"hello world"
    apply_file_push(tmp_path, {
        "agent_slug": "a1", "path": "workspace/x.txt", "action": "write",
        "content_b64": _b64(data), "hash": _h(data),
    })
    assert (tmp_path / "workspace" / "x.txt").read_bytes() == data


def test_inline_write_hash_mismatch_rejected(tmp_path):
    with pytest.raises(ValueError, match="hash mismatch"):
        apply_file_push(tmp_path, {
            "agent_slug": "a1", "path": "workspace/x.txt", "action": "write",
            "content_b64": _b64(b"hello"), "hash": "sha256:deadbeef",
        })
    assert not (tmp_path / "workspace" / "x.txt").exists()


def test_inline_write_no_hash_still_writes(tmp_path):
    """An inline write with no hash (legacy/synthetic) still writes — the verify
    is guarded on hash presence, so this is backward-safe."""
    apply_file_push(tmp_path, {
        "agent_slug": "a1", "path": "workspace/x.txt", "action": "write",
        "content_b64": _b64(b"plain"),
    })
    assert (tmp_path / "workspace" / "x.txt").read_bytes() == b"plain"


def test_oversized_chunk_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(file_sync, "MAX_FILE_SIZE", 4, raising=True)
    rel = "workspace/big.bin"
    with pytest.raises(ValueError, match="MAX_FILE_SIZE"):
        apply_file_push(tmp_path, _chunk(rel, 0, 1, b"abcdefgh"))  # 8 > 4
    assert not (tmp_path / "workspace" / "big.bin.partial").exists()
    assert not (tmp_path / rel).exists()


def test_oversized_inline_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(file_sync, "MAX_FILE_SIZE", 4, raising=True)
    with pytest.raises(ValueError, match="MAX_FILE_SIZE"):
        apply_file_push(tmp_path, {
            "agent_slug": "a1", "path": "workspace/big.txt", "action": "write",
            "content_b64": _b64(b"abcdefgh"), "hash": _h(b"abcdefgh"),
        })
    assert not (tmp_path / "workspace" / "big.txt").exists()


def test_partial_files_excluded_from_manifests(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    (ws / "real.txt").write_bytes(b"keep me")
    (ws / "x.bin.partial").write_bytes(b"transient staging")

    snap = snapshot_agent_dir(tmp_path)
    assert "workspace/real.txt" in snap
    assert "workspace/x.bin.partial" not in snap

    manifest_paths = {e["path"] for e in compute_manifest(tmp_path)}
    assert "workspace/real.txt" in manifest_paths
    assert "workspace/x.bin.partial" not in manifest_paths


class TestEmptyFileWrite:
    """Zero-byte writes are real (2026-07-19): the old truthy content gate
    silently skipped them while the handler acked ok — poisoning the
    platform's converged base."""

    def test_empty_inline_write_creates_file(self, tmp_path):
        msg = {"path": "workspace/pkg/__init__.py", "action": "write",
               "content_b64": "", "hash": _h(b"")}
        snap: dict = {}
        apply_file_push(tmp_path, msg, snapshot=snap)
        f = tmp_path / "workspace" / "pkg" / "__init__.py"
        assert f.exists() and f.stat().st_size == 0
        # Snapshot refreshed → no phantom file_changed echo.
        assert snap.get("workspace/pkg/__init__.py") == _h(b"")

    def test_missing_content_key_writes_nothing(self, tmp_path):
        msg = {"path": "workspace/big.bin", "action": "write", "size": 123}
        apply_file_push(tmp_path, msg)
        assert not (tmp_path / "workspace" / "big.bin").exists()


class TestDeletePrunesEmptyParents:
    def test_delete_prunes_to_depth1(self, tmp_path):
        d = tmp_path / "workspace" / "proj" / "sub"
        d.mkdir(parents=True)
        f = d / "a.txt"
        f.write_text("x")
        apply_file_push(tmp_path, {"path": "workspace/proj/sub/a.txt",
                                   "action": "delete"})
        assert not (tmp_path / "workspace" / "proj").exists()
        assert (tmp_path / "workspace").exists()

    def test_user_root_survives(self, tmp_path):
        d = tmp_path / "users" / "alice" / "notes"
        d.mkdir(parents=True)
        (d / "n.md").write_text("x")
        apply_file_push(tmp_path, {"path": "users/alice/notes/n.md",
                                   "action": "delete"})
        assert not d.exists()
        assert (tmp_path / "users" / "alice").exists()


# ---------------------------------------------------------------------------
# The applier opens beneath the agent root; a link component is
# refused, and a swap after the check never redirects a write
# ---------------------------------------------------------------------------


def _agent(tmp_path):
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace" / "sub").mkdir(parents=True)
    (agent / "users" / "other").mkdir(parents=True)
    return agent


def test_an_in_tree_link_component_is_refused_on_write(tmp_path):
    import os
    agent = _agent(tmp_path)
    os.symlink("../users/other", agent / "workspace" / "alias")
    with pytest.raises(OSError):
        apply_file_push(agent, {"path": "workspace/alias/f.txt", "action": "write",
                                "content_b64": _b64(b"x")})
    assert not (agent / "users" / "other" / "f.txt").exists()


def test_a_component_swapped_after_the_check_is_refused(tmp_path, monkeypatch):
    import contextlib
    import os

    from satellite.host import safe_fs
    agent = _agent(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f.txt").write_bytes(b"ORIGINAL")
    real = safe_fs.open_root
    state = {"done": False}

    @contextlib.contextmanager
    def _patched(root, rel=""):
        if not state["done"]:
            state["done"] = True
            d = agent / "workspace" / "sub"
            d.rmdir()
            os.symlink(victim, d)
        with real(root, rel) as r:
            yield r

    monkeypatch.setattr(safe_fs, "open_root", _patched)
    with pytest.raises(OSError):
        apply_file_push(agent, {"path": "workspace/sub/f.txt", "action": "write",
                                "content_b64": _b64(b"NEW"), "hash": _h(b"NEW")})
    assert (victim / "f.txt").read_bytes() == b"ORIGINAL"
    assert sorted(p.name for p in victim.iterdir()) == ["f.txt"]


def test_delete_mkdir_and_chunks_stay_beneath_the_root(tmp_path):
    import os
    agent = _agent(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_bytes(b"ORIGINAL")
    os.symlink(outside, agent / "workspace" / "lnk")
    with pytest.raises(OSError):
        apply_file_push(agent, {"path": "workspace/lnk/f.txt", "action": "delete"})
    with pytest.raises(OSError):
        apply_file_push(agent, {"path": "workspace/lnk/deeper", "action": "mkdir"})
    assert (outside / "f.txt").read_bytes() == b"ORIGINAL"
    # A link planted at the staging name is replaced by the fresh partial.
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"ORIGINAL")
    os.symlink(victim, agent / "workspace" / "big.bin.partial")
    data = b"A" * 4 + b"B" * 4
    apply_file_push(agent, _chunk("workspace/big.bin", 0, 2, data[:4]))
    apply_file_push(agent, _chunk("workspace/big.bin", 1, 2, data[4:], last_hash=_h(data)))
    assert (agent / "workspace" / "big.bin").read_bytes() == data
    assert victim.read_bytes() == b"ORIGINAL"


# --- the stale guard ----------------------------------------------------------


def _write_msg(rel: str, data: bytes, **extra) -> dict:
    return {"agent_slug": "a1", "path": rel, "action": "write",
            "content_b64": _b64(data), "hash": _h(data), **extra}


def _stat_of(path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _age(path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_a_file_changed_since_the_platform_saw_it_is_not_replaced(tmp_path):
    """A script holds the file open and appends: the push carrying the stat
    the platform knows is refused, the inode and the script's later bytes
    survive, nothing is staged, and the refusal names the current stat."""
    rel = "workspace/run.log"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"line 1\n")
    _age(target, 60)
    known = _stat_of(target)
    inode = target.stat().st_ino
    with open(target, "ab") as writer:
        writer.write(b"line 2\n")
        writer.flush()
        _age(target, 30)  # changed, and past the live window
        with pytest.raises(file_sync.StalePush) as info:
            apply_file_push(tmp_path, _write_msg(rel, b"platform\n", expect=known))
        writer.write(b"line 3\n")
        writer.flush()
    assert info.value.reason == "changed"
    assert info.value.size == len(b"line 1\nline 2\n")
    assert target.read_bytes() == b"line 1\nline 2\nline 3\n"
    assert target.stat().st_ino == inode
    assert [p.name for p in target.parent.iterdir()] == ["run.log"]


def test_a_matching_expectation_commits_and_reports_the_committed_stat(tmp_path):
    rel = "workspace/notes.md"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"v1")
    _age(target, 60)
    snap = {rel: _h(b"v1")}
    committed = apply_file_push(
        tmp_path, _write_msg(rel, b"v2", expect=_stat_of(target)), snapshot=snap)
    assert target.read_bytes() == b"v2"
    assert committed == _stat_of(target) and committed["size"] == 2
    assert snap[rel] == _h(b"v2")


@pytest.mark.parametrize("off_ns, stale", [(700, False), (-900, False), (1_500, True)])
def test_the_manifests_rounding_of_the_mtime_is_allowed(tmp_path, off_ns, stale):
    # The platform read the mtime as a float of seconds and turned it back
    # into ns: within a microsecond it is the same file.
    rel = "workspace/a.txt"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"v1")
    _age(target, 60)
    expect = _stat_of(target)
    expect["mtime_ns"] += off_ns
    if stale:
        with pytest.raises(file_sync.StalePush):
            apply_file_push(tmp_path, _write_msg(rel, b"v2", expect=expect))
        assert target.read_bytes() == b"v1"
    else:
        apply_file_push(tmp_path, _write_msg(rel, b"v2", expect=expect))
        assert target.read_bytes() == b"v2"


def test_a_file_written_moments_ago_is_live_and_an_idle_one_commits(tmp_path):
    rel = "workspace/out.txt"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"v1")  # its mtime is now
    with pytest.raises(file_sync.StalePush) as info:
        apply_file_push(tmp_path, _write_msg(rel, b"v2", expect="live"))
    assert info.value.reason == "live"
    assert (info.value.size, info.value.mtime_ns) == (2, target.stat().st_mtime_ns)
    assert target.read_bytes() == b"v1"
    _age(target, file_sync.LIVE_WRITE_WINDOW_S + 5)
    assert apply_file_push(tmp_path, _write_msg(rel, b"v2", expect="live")) == _stat_of(target)
    assert target.read_bytes() == b"v2"


def test_a_missing_file_and_a_frame_without_an_expectation_commit(tmp_path):
    rel = "workspace/new.txt"
    assert apply_file_push(tmp_path, _write_msg(rel, b"n", expect="live"))["size"] == 1
    # Just written, so live by the clock: a frame with no expectation (a
    # platform before 1.9.0) replaces it as before.
    assert apply_file_push(tmp_path, _write_msg(rel, b"nn"))["size"] == 2
    assert (tmp_path / rel).read_bytes() == b"nn"


def test_a_refused_first_chunk_leaves_no_partial_for_the_chunks_behind_it(tmp_path):
    """The platform pipelines chunks: after a stale first chunk the ones
    already on the wire find no transfer to append to and stage nothing,
    and an older aborted partial goes with the refusal."""
    rel = "workspace/big.bin"
    target = tmp_path / rel
    partial = tmp_path / "workspace" / "big.bin.partial"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    _age(target, 60)
    partial.write_bytes(b"GARBAGE-LEFTOVER")
    with pytest.raises(file_sync.StalePush):
        apply_file_push(tmp_path, {**_chunk(rel, 0, 3, b"ab"), "expect": {"size": 99, "mtime_ns": 1}})
    assert not partial.exists()
    with pytest.raises(ValueError, match="no transfer in progress"):
        apply_file_push(tmp_path, _chunk(rel, 1, 3, b"cd"))
    with pytest.raises(ValueError, match="no transfer in progress"):
        apply_file_push(tmp_path, _chunk(rel, 2, 3, b"ef", last_hash=_h(b"abcdef")))
    assert not partial.exists() and target.read_bytes() == b"old"
    # A later chunk never creates the folder of a transfer that is not there.
    with pytest.raises(ValueError, match="no transfer in progress"):
        apply_file_push(tmp_path, _chunk("workspace/new/dir/f.bin", 1, 2, b"cd"))
    assert not (tmp_path / "workspace" / "new").exists()


def test_a_chunked_push_is_refused_at_its_first_chunk_and_checked_again_at_its_last(tmp_path):
    rel = "workspace/big.bin"
    target = tmp_path / rel
    partial = tmp_path / "workspace" / "big.bin.partial"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    _age(target, 60)
    known = _stat_of(target)
    # A changed file: refused before any byte is staged.
    with pytest.raises(file_sync.StalePush):
        apply_file_push(tmp_path, {**_chunk(rel, 0, 2, b"ab"), "expect": {"size": 99, "mtime_ns": 1}})
    assert not partial.exists()
    # Staged on a matching first chunk; the file changes before the last.
    apply_file_push(tmp_path, {**_chunk(rel, 0, 2, b"ab"), "expect": known})
    assert partial.exists()
    target.write_bytes(b"newer")
    _age(target, 20)
    with pytest.raises(file_sync.StalePush) as info:
        apply_file_push(
            tmp_path, {**_chunk(rel, 1, 2, b"cd", last_hash=_h(b"abcd")), "expect": known})
    assert info.value.reason == "changed"
    assert not partial.exists() and target.read_bytes() == b"newer"
    # Unchanged throughout: committed, and the ack's stat is the file's.
    known = _stat_of(target)
    assert apply_file_push(tmp_path, {**_chunk(rel, 0, 2, b"ab"), "expect": known}) is None
    committed = apply_file_push(
        tmp_path, {**_chunk(rel, 1, 2, b"cd", last_hash=_h(b"abcd")), "expect": known})
    assert target.read_bytes() == b"abcd" and committed == _stat_of(target)
