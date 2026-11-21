"""Tests for atomic writes + symlink skip in core.remote.file_sync.

These harden the sync protocol against races (partial writes visible to
concurrent readers) and silent data loss (symlinks round-tripping as
regular files).
"""

import base64
import contextlib
import os
import time
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
    assert isinstance(entry[3], bytes) and len(entry[3]) == 32
    # A malformed prime never raises and never poisons the cache.
    file_sync.prime_hash_cache(f, "not-a-hash")
    assert file_sync._HASH_CACHE[str(f)][3] == bytes.fromhex("ab" * 32) or str(f) not in file_sync._HASH_CACHE
    # A hit answers the string form; a miss hashes the file.
    file_sync._HASH_CACHE.clear()
    h, _st = file_sync._hash_listed(tmp_path, f.name, f, st_old, max_size=None)
    assert h == file_sync._hash_file(f)
    assert file_sync._HASH_CACHE_MAX >= 200_000


def test_a_forgotten_hash_is_read_again_from_the_file(tmp_path, monkeypatch):
    """``forget_hash`` drops one path's entry: a same-size rewrite that kept
    its mtime is hashed again by the next pass, the other entries stay."""
    from core.remote import file_sync
    monkeypatch.setattr(file_sync, "_HASH_CACHE", type(file_sync._HASH_CACHE)())
    f, other = tmp_path / "f.txt", tmp_path / "other.txt"
    f.write_bytes(b"aaaa")
    other.write_bytes(b"bbbb")
    old = time.time() - 60
    def _cached(p):
        return file_sync._hash_listed(tmp_path, p.name, p, os.lstat(p), max_size=None)[0]
    for p in (f, other):
        os.utime(p, (old, old))
        _cached(p)
    st = os.stat(f)
    with open(f, "r+b") as fh:  # the same inode, rewritten in place
        fh.write(b"cccc")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _cached(f) != file_sync._hash_file(f)
    file_sync.forget_hash(f)
    file_sync.forget_hash(tmp_path / "never-cached.txt")
    assert str(f) not in file_sync._HASH_CACHE and str(other) in file_sync._HASH_CACHE
    assert _cached(f) == file_sync._hash_file(f)


def _released_after(fn, pipes, timeout=15.0):
    """``fn()`` in a thread, joined within ``timeout``; every pipe in
    ``pipes`` is then opened for writing once, so a reader the code under
    test left waiting on one is let go whatever the test's outcome."""
    import threading
    out: dict = {}

    def _run():
        try:
            out["value"] = fn()
        except BaseException as e:  # noqa: BLE001  # handed back to the test
            out["error"] = e
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    waited = t.is_alive()
    for pipe in pipes:
        with contextlib.suppress(OSError):
            os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
    t.join(5)
    assert not waited, "the call waited on a pipe"
    if "error" in out:
        raise out["error"]
    return out["value"]


def test_a_pipe_in_a_synced_folder_is_left_out_and_never_waited_on(tmp_path: Path):
    from core.remote.file_sync import compute_manifest
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "ok.txt").write_text("ok")
    os.mkfifo(agent / "workspace" / "pipe")
    entries = _released_after(lambda: compute_manifest(agent), [agent / "workspace" / "pipe"])
    assert [e.path for e in entries] == ["workspace/ok.txt"]


def test_a_pipe_at_a_cache_tag_reads_as_no_marker(tmp_path: Path):
    from core.remote import file_sync
    agent = tmp_path / "agents" / "a1"
    build = agent / "workspace" / "proj" / "cache"
    build.mkdir(parents=True)
    (build / "x.txt").write_text("x")
    os.mkfifo(build / "CACHEDIR.TAG")
    rules = file_sync.validate_ignore_rules({
        "version": 1, "in_dir_markers": [{"file": "CACHEDIR.TAG", "signed": True}]})
    assert rules is not None
    entries = _released_after(lambda: file_sync.compute_manifest(agent, ignore_rules=rules),
                              [build / "CACHEDIR.TAG"])
    assert [e.path for e in entries] == ["workspace/proj/cache/x.txt"]


def test_a_folder_that_became_a_link_after_its_listing_lists_nothing(tmp_path: Path, monkeypatch):
    """The walk lists a folder by name; one swapped for a link after the
    listing is refused where it stands, and nothing under it is read."""
    from core.remote import file_sync
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    (agent / "workspace" / "ok.txt").write_text("ok")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("stand-in")
    (agent / "workspace" / "sub").symlink_to(outside)
    real_walk = os.walk

    def _walk_listed_before_the_swap(top, *a, **kw):
        yield from real_walk(top, *a, **kw)
        yield str(agent / "workspace" / "sub"), [], ["secret.txt"]
    monkeypatch.setattr(file_sync.os, "walk", _walk_listed_before_the_swap)
    assert [e.path for e in file_sync.compute_manifest(agent)] == ["workspace/ok.txt"]


def test_an_agent_folder_moved_behind_a_link_keeps_its_manifest(tmp_path: Path):
    """An operator who moved an agent's folder to another disk behind a link
    keeps syncing it (the render takes it the same way); a link below it is
    still never followed."""
    from core.remote import file_sync
    real = tmp_path / "disk2" / "a1"
    (real / "workspace").mkdir(parents=True)
    (real / "workspace" / "ok.txt").write_text("ok")
    (tmp_path / "secret.txt").write_text("stand-in")
    (real / "workspace" / "linked.txt").symlink_to(tmp_path / "secret.txt")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "a1").symlink_to(real)
    assert [e.path for e in file_sync.compute_manifest(tmp_path / "agents" / "a1")] == ["workspace/ok.txt"]


def test_the_platform_digest_never_reads_through_a_link_or_waits(tmp_path: Path, monkeypatch):
    import config
    from core.remote import remote_file_cache
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    ws = tmp_path / "agents" / "a1" / "workspace"
    ws.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("stand-in")
    (ws / "linked.txt").symlink_to(tmp_path / "secret.txt")
    (ws / "sub").symlink_to(tmp_path)
    os.mkfifo(ws / "pipe")
    (ws / "ok.txt").write_text("ok")
    assert remote_file_cache._platform_digest("a1", "workspace/linked.txt", ws / "linked.txt") == ""
    assert remote_file_cache._platform_digest("a1", "workspace/sub/secret.txt", ws / "sub" / "secret.txt") == ""
    assert _released_after(
        lambda: remote_file_cache._platform_digest("a1", "workspace/pipe", ws / "pipe"), [ws / "pipe"]) == ""
    assert remote_file_cache._platform_digest("a1", "workspace/ok.txt", ws / "ok.txt").startswith("sha256:")


def test_the_hash_of_a_name_never_waits_on_a_pipe(tmp_path: Path):
    """``_hash_file`` (a descriptor's path, a file the caller made) opens
    without waiting and refuses anything but a regular file."""
    import pytest

    from core.remote import file_sync
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(OSError):
        _released_after(lambda: file_sync._hash_file(tmp_path / "pipe"), [tmp_path / "pipe"])


def test_a_remembered_hash_never_lists_a_file_behind_a_swapped_folder(tmp_path: Path, monkeypatch):
    """A cache hit reads nothing, so it is the folder check that keeps a file
    behind a folder swapped for a link out of the manifest, even when the
    cache remembers a hash for its name."""
    import time as _time
    from collections import OrderedDict

    from core.remote import file_sync
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("stand-in")
    old = _time.time() - 60
    os.utime(outside / "secret.txt", (old, old))
    (agent / "workspace" / "sub").symlink_to(outside)
    file_sync.prime_hash_cache(agent / "workspace" / "sub" / "secret.txt", "sha256:" + "ab" * 32)
    real_walk = os.walk

    def _walk_listed_before_the_swap(top, *a, **kw):
        yield from real_walk(top, *a, **kw)
        yield str(agent / "workspace" / "sub"), [], ["secret.txt"]
    monkeypatch.setattr(file_sync.os, "walk", _walk_listed_before_the_swap)
    assert file_sync.compute_manifest(agent) == []


def _backdated(path: Path, data: bytes) -> os.stat_result:
    """``data`` at ``path``, its times a minute back (past the racy window)."""
    path.write_bytes(data)
    old = time.time() - 60
    os.utime(path, (old, old))
    return os.lstat(path)


def test_a_file_swapped_at_its_name_with_its_size_and_time_kept_is_hashed_again(tmp_path: Path, monkeypatch):
    """Another file renamed onto the name with the same size and the first
    one's time (a copy that keeps times) is a different file: its hash is
    read again, never taken from what the cache remembers of the name."""
    import hashlib
    from collections import OrderedDict

    from core.remote import file_sync
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    st = _backdated(agent / "workspace" / "doc.txt", b"aaaa")
    assert [e.hash for e in file_sync.compute_manifest(agent)] == [
        "sha256:" + hashlib.sha256(b"aaaa").hexdigest()]
    other = tmp_path / "other.txt"
    other.write_bytes(b"bbbb")
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(other, agent / "workspace" / "doc.txt")
    assert [e.hash for e in file_sync.compute_manifest(agent)] == [
        "sha256:" + hashlib.sha256(b"bbbb").hexdigest()]


def test_a_hash_read_while_the_file_changed_is_never_remembered(tmp_path: Path, monkeypatch):
    """A file that changes while it is hashed leaves nothing in the cache,
    so the next pass reads it again."""
    from collections import OrderedDict

    from core.remote import file_sync
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    doc = agent / "workspace" / "doc.txt"
    _backdated(doc, b"aaaa")
    real = file_sync._hash_fd
    hashed: list = []

    def _grows_while_hashed(fd):
        hashed.append(fd)
        digest = real(fd)
        if len(hashed) == 1:
            with open(doc, "ab") as fh:
                fh.write(b"b")
        return digest
    monkeypatch.setattr(file_sync, "_hash_fd", _grows_while_hashed)
    file_sync.compute_manifest(agent)
    assert str(doc) not in file_sync._HASH_CACHE
    file_sync.compute_manifest(agent)
    assert len(hashed) == 2


def test_an_unchanged_tree_opens_no_file_where_the_two_inodes_differ(tmp_path: Path, monkeypatch):
    """The cache compares the listing's status with a listing's status, never
    with the open file's (the satellite's rule, one shape on both sides): an
    open file reporting another inode still lets a second pass open nothing."""
    from collections import OrderedDict

    from core.remote import file_sync
    from services.infra import safe_fs
    real_fstat = os.fstat

    def fstat(fd):
        st = real_fstat(fd)
        return os.stat_result(
            (st.st_mode, st.st_ino + 1_000_003, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
             st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)),
            {"st_atime": st.st_atime, "st_mtime": st.st_mtime, "st_ctime": st.st_ctime,
             "st_atime_ns": st.st_atime_ns, "st_mtime_ns": st.st_mtime_ns, "st_ctime_ns": st.st_ctime_ns})
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    for name in ("a.txt", "b.txt", "c.txt"):
        _backdated(agent / "workspace" / name, name.encode())
    monkeypatch.setattr(os, "fstat", fstat)
    first = file_sync.compute_manifest(agent)
    opened: list = []
    real_open = safe_fs.open_regular_for_read

    def _counted(root, rel, **kw):
        opened.append(rel)
        return real_open(root, rel, **kw)
    monkeypatch.setattr(file_sync.safe_fs, "open_regular_for_read", _counted)
    assert [e.hash for e in file_sync.compute_manifest(agent)] == [e.hash for e in first]
    assert opened == []


def test_a_file_changed_between_its_listing_and_its_read_is_never_remembered(tmp_path: Path, monkeypatch):
    from collections import OrderedDict

    from core.remote import file_sync
    from services.infra import safe_fs
    monkeypatch.setattr(file_sync, "_HASH_CACHE", OrderedDict())
    agent = tmp_path / "agents" / "a1"
    (agent / "workspace").mkdir(parents=True)
    doc = agent / "workspace" / "doc.txt"
    _backdated(doc, b"aaaa")
    real_open = safe_fs.open_regular_for_read

    def _grows_before_the_read(root, rel, **kw):
        with open(doc, "ab") as fh:
            fh.write(b"b")
        return real_open(root, rel, **kw)
    monkeypatch.setattr(file_sync.safe_fs, "open_regular_for_read", _grows_before_the_read)
    file_sync.compute_manifest(agent)
    assert str(doc) not in file_sync._HASH_CACHE
