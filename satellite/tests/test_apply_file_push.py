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
