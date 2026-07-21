"""Tests for atomic writes + symlink skip in core.remote.file_sync.

These harden the sync protocol against races (partial writes visible to
concurrent readers) and silent data loss (symlinks round-tripping as
regular files).
"""

import base64
import os
from pathlib import Path



def test_symlink_skipped_from_manifest(tmp_path: Path):
    """compute_manifest must not include symlinks — they don't round-trip."""
    from core.remote.file_sync import compute_manifest

    real = tmp_path / "real.txt"
    real.write_text("content")
    link = tmp_path / "link.txt"
    os.symlink(real, link)

    entries = compute_manifest(tmp_path)
    paths = [e.path for e in entries]
    assert "real.txt" in paths
    assert "link.txt" not in paths


def test_prepare_outgoing_skips_symlinks(tmp_path: Path):
    """prepare_outgoing_files must not include symlinks even if listed."""
    from core.remote.file_sync import prepare_outgoing_files

    real = tmp_path / "real.txt"
    real.write_text("hi")
    link = tmp_path / "link.txt"
    os.symlink(real, link)

    msgs = prepare_outgoing_files(tmp_path, ["real.txt", "link.txt"])
    paths = [m["path"] for m in msgs]
    assert paths == ["real.txt"]


def test_apply_incoming_write_is_atomic(tmp_path: Path):
    """A 'write' action uses .partial + rename so readers never see a
    half-written file."""
    from core.remote.file_sync import apply_incoming_file

    content = b"some content"
    b64 = base64.b64encode(content).decode()
    apply_incoming_file(tmp_path, "foo.txt", "write", b64)

    dest = tmp_path / "foo.txt"
    assert dest.read_bytes() == content
    # No .partial left behind
    assert not (tmp_path / "foo.txt.partial").exists()


def test_apply_incoming_chunked_only_commits_on_final(tmp_path: Path):
    """write_chunk appends to .partial until final_chunk=True, then renames."""
    from core.remote.file_sync import apply_incoming_file

    dest = tmp_path / "big.bin"
    partial = tmp_path / "big.bin.partial"

    chunk1 = b"A" * 32
    chunk2 = b"B" * 32

    apply_incoming_file(tmp_path, "big.bin", "write_chunk",
                        base64.b64encode(chunk1).decode(), final_chunk=False)
    # Not yet committed
    assert not dest.exists()
    assert partial.is_file()
    assert partial.read_bytes() == chunk1

    apply_incoming_file(tmp_path, "big.bin", "write_chunk",
                        base64.b64encode(chunk2).decode(), final_chunk=True)
    # Now committed atomically
    assert dest.read_bytes() == chunk1 + chunk2
    assert not partial.exists()


def test_apply_incoming_delete(tmp_path: Path):
    from core.remote.file_sync import apply_incoming_file
    f = tmp_path / "gone.txt"
    f.write_text("here")
    apply_incoming_file(tmp_path, "gone.txt", "delete")
    assert not f.exists()


def test_apply_incoming_rejects_traversal(tmp_path: Path):
    """Path traversal attempts (../) are silently rejected, no write."""
    from core.remote.file_sync import apply_incoming_file
    apply_incoming_file(
        tmp_path, "../escaped.txt", "write",
        base64.b64encode(b"evil").decode(),
    )
    # Parent dir must NOT have the file
    assert not (tmp_path.parent / "escaped.txt").exists()
