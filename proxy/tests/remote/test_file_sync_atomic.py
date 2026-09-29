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


# ---------------------------------------------------------------------------
# The applier opens beneath the agent root; a component swapped after
# the check never redirects a write, a delete or a directory creation
# ---------------------------------------------------------------------------


def _swap_on_open(monkeypatch, agent_dir, victim):
    import contextlib
    from services.infra import safe_fs
    real = safe_fs.open_root
    state = {"done": False}

    @contextlib.contextmanager
    def _patched(root, rel=""):
        if not state["done"]:
            state["done"] = True
            d = agent_dir / "workspace" / "sub"
            for child in d.iterdir():
                child.unlink()
            d.rmdir()
            os.symlink(victim, d)
        with real(root, rel) as fd:
            yield fd

    monkeypatch.setattr(safe_fs, "open_root", _patched)


def _tree(tmp_path):
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace" / "sub").mkdir(parents=True)
    (agent / "workspace" / "sub" / "f.txt").write_bytes(b"mine")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f.txt").write_bytes(b"ORIGINAL")
    return agent, victim


def test_apply_incoming_write_refuses_a_swapped_component(tmp_path, monkeypatch):
    from core.remote.file_sync import apply_incoming_file
    agent, victim = _tree(tmp_path)
    _swap_on_open(monkeypatch, agent, victim)
    import pytest
    with pytest.raises(OSError):
        apply_incoming_file(agent, "workspace/sub/f.txt", "write", base64.b64encode(b"NEW").decode())
    assert (victim / "f.txt").read_bytes() == b"ORIGINAL"
    assert sorted(p.name for p in victim.iterdir()) == ["f.txt"]


def test_apply_incoming_delete_and_mkdir_refuse_a_swapped_component(tmp_path, monkeypatch):
    from core.remote.file_sync import apply_incoming_file
    agent, victim = _tree(tmp_path)
    _swap_on_open(monkeypatch, agent, victim)
    import pytest
    with pytest.raises(OSError):
        apply_incoming_file(agent, "workspace/sub/f.txt", "delete")
    assert (victim / "f.txt").read_bytes() == b"ORIGINAL"
    with pytest.raises(OSError):
        apply_incoming_file(agent, "workspace/sub/newdir", "mkdir")
    assert sorted(p.name for p in victim.iterdir()) == ["f.txt"]


def test_apply_incoming_chunks_stage_beneath_the_root(tmp_path):
    """The chunk partial is created and committed beneath the root; a link at
    the partial's name is replaced, never written through."""
    from core.remote.file_sync import apply_incoming_file
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"ORIGINAL")
    (agent / "workspace" / "big.bin.partial").symlink_to(victim)
    apply_incoming_file(agent, "workspace/big.bin", "write_chunk",
                        base64.b64encode(b"A" * 8).decode(), final_chunk=False)
    apply_incoming_file(agent, "workspace/big.bin", "write_chunk",
                        base64.b64encode(b"B" * 8).decode(), final_chunk=True)
    assert victim.read_bytes() == b"ORIGINAL"
    assert (agent / "workspace" / "big.bin").read_bytes() == b"A" * 8 + b"B" * 8


def test_prepare_outgoing_refuses_a_link_component(tmp_path):
    from core.remote.file_sync import prepare_outgoing_files
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_bytes(b"SECRET")
    (agent / "workspace" / "sub").symlink_to(outside)
    (agent / "workspace" / "ok.txt").write_bytes(b"ok")
    msgs = prepare_outgoing_files(agent, ["workspace/sub/s.txt", "workspace/ok.txt"])
    assert [m["path"] for m in msgs] == ["workspace/ok.txt"]


def test_hash_cache_stores_the_raw_digest_and_primes_tolerantly(tmp_path, monkeypatch):
    from core.remote import file_sync
    f = tmp_path / "f.bin"
    f.write_bytes(b"content")
    st = os.stat(f)
    st_old = os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                             st.st_size, st.st_atime, st.st_mtime - 10, st.st_ctime))
    # An entry the cache keeps is the 32-byte digest, not the 71-char string.
    monkeypatch.setattr(file_sync, "_HASH_CACHE", type(file_sync._HASH_CACHE)())
    file_sync.prime_hash_cache(f, "sha256:" + "ab" * 32)
    entry = file_sync._HASH_CACHE[str(f)]
    assert isinstance(entry[2], bytes) and len(entry[2]) == 32
    # A malformed prime never raises and never poisons the cache.
    file_sync.prime_hash_cache(f, "not-a-hash")
    assert file_sync._HASH_CACHE[str(f)][2] == bytes.fromhex("ab" * 32) or str(f) not in file_sync._HASH_CACHE
    # A hit answers the string form; a miss hashes the file.
    file_sync._HASH_CACHE.clear()
    h = file_sync._hash_file_cached(f, st_old)
    assert h == file_sync._hash_file(f)
    assert file_sync._HASH_CACHE_MAX >= 200_000
