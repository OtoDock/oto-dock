"""``services/infra/safe_fs``: opening beneath a root no symlink swap can
redirect. Every test runs twice: through
``openat2`` and through the per-component fallback walk, so both paths are
covered on every run.

The tree each test gets: ``root/a/b/f.txt`` inside, ``outside/secret``
beside the root (what an escape would reach).
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import threading

import pytest

from services.infra import path_confinement, safe_fs


@pytest.fixture(params=["openat2", "walk"])
def resolution(request, monkeypatch):
    if request.param == "openat2":
        if not safe_fs._probe_openat2():
            pytest.skip("openat2 unavailable on this kernel")
        monkeypatch.setattr(safe_fs, "_openat2_ok", True)
    else:
        monkeypatch.setattr(safe_fs, "_openat2_ok", False)
    return request.param


@pytest.fixture
def tree(tmp_path, resolution):
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f.txt").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"SECRET")
    return root, outside


def _no_partials(root) -> bool:
    return not [p for p in root.rglob("*.partial")]


# ── reads ──────────────────────────────────────────────────────────────


def test_reads_a_regular_file(tree):
    root, _ = tree
    assert safe_fs.read_bytes_beneath(root, "a/b/f.txt", max_size=100) == b"inside"
    fd, st = safe_fs.open_regular_for_read(root, "a/b/f.txt")
    try:
        assert st.st_size == 6 and stat.S_ISREG(st.st_mode)
    finally:
        os.close(fd)


def test_a_missing_path_stays_file_not_found(tree):
    root, _ = tree
    with pytest.raises(FileNotFoundError):
        safe_fs.read_bytes_beneath(root, "a/b/nope", max_size=100)
    with pytest.raises(FileNotFoundError):
        safe_fs.read_bytes_beneath(root, "a/nope/f.txt", max_size=100)


def test_leaf_symlink_is_refused_even_inside_the_tree(tree):
    root, outside = tree
    (root / "a" / "leak").symlink_to(outside / "secret")
    (root / "a" / "alias").symlink_to("b/f.txt")
    for rel in ("a/leak", "a/alias"):
        with pytest.raises(safe_fs.SymlinkRefused):
            safe_fs.read_bytes_beneath(root, rel, max_size=100)


def test_intermediate_symlink_is_refused(tree):
    root, outside = tree
    (root / "a" / "out").symlink_to(outside)
    (root / "a" / "in").symlink_to("b")
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.read_bytes_beneath(root, "a/out/secret", max_size=100)
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.read_bytes_beneath(root, "a/in/f.txt", max_size=100)
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.open_dir_beneath(root, "a/in")


@pytest.mark.parametrize("rel", [
    "../outside/secret", "a/../../outside/secret", "a/b/../f.txt", "/etc/passwd",
    "a//b/f.txt", "a/./b/f.txt", "a/b/f.txt/", "./a", "a/b\x00/f.txt",
])
def test_dot_absolute_and_empty_segments_are_refused_before_any_syscall(tree, rel, monkeypatch):
    root, _ = tree
    monkeypatch.setattr(safe_fs, "_open_parts", lambda *a, **k: pytest.fail("reached a syscall"))
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.open_beneath(root, rel)


def test_a_refusal_is_an_oserror(tree):
    root, outside = tree
    (root / "a" / "leak").symlink_to(outside / "secret")
    with pytest.raises(OSError) as exc:
        safe_fs.read_bytes_beneath(root, "a/leak", max_size=100)
    assert isinstance(exc.value, safe_fs.SafeFsError)
    assert exc.value.errno == errno.ELOOP


def test_hardlink_is_refused_when_a_single_link_is_required(tree):
    root, outside = tree
    os.link(outside / "secret", root / "a" / "hl")
    with pytest.raises(safe_fs.HardlinkRefused):
        safe_fs.read_bytes_beneath(root, "a/hl", max_size=100, single_link=True)
    # Not detectable as an escape otherwise: the caller asks for it where it matters.
    assert safe_fs.read_bytes_beneath(root, "a/hl", max_size=100) == b"SECRET"


def test_fifo_and_directory_are_refused_without_blocking(tree):
    root, _ = tree
    os.mkfifo(root / "a" / "pipe")
    with pytest.raises(safe_fs.NotRegularFile):
        safe_fs.open_regular_for_read(root, "a/pipe")
    with pytest.raises(safe_fs.NotRegularFile):
        safe_fs.open_regular_for_read(root, "a/b")


def test_size_cap(tree):
    root, _ = tree
    with pytest.raises(safe_fs.FileTooLarge):
        safe_fs.read_bytes_beneath(root, "a/b/f.txt", max_size=3)
    with pytest.raises(safe_fs.FileTooLarge):
        safe_fs.open_regular_for_read(root, "a/b/f.txt", max_size=3)


def test_iter_fd_serves_a_range_from_the_descriptor(tree):
    root, _ = tree
    fd, _st = safe_fs.open_regular_for_read(root, "a/b/f.txt")
    try:
        assert b"".join(safe_fs.iter_fd(fd, 2, 3, chunk_size=1)) == b"sid"
        assert b"".join(safe_fs.iter_fd(fd)) == b"inside"
        # The path re-opens the very file, whatever happens to the name.
        os.rename(root / "a" / "b" / "f.txt", root / "a" / "b" / "moved")
        with open(safe_fs.fd_path(fd), "rb") as again:
            assert again.read() == b"inside"
    finally:
        os.close(fd)


def test_an_o_path_open_never_hands_out_a_link(tree):
    root, outside = tree
    (root / "a" / "leak").symlink_to(outside / "secret")
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.open_beneath(root, "a/leak", os.O_PATH)
    fd = safe_fs.open_beneath(root, "a/b/f.txt", os.O_PATH)
    os.close(fd)


def test_a_fifo_or_socket_is_refused_by_the_plain_open_too(tree):
    import socket
    root, _ = tree
    os.mkfifo(root / "a" / "pipe")
    with pytest.raises(safe_fs.NotRegularFile):
        safe_fs.open_beneath(root, "a/pipe")
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(root / "a" / "sock"))
        with pytest.raises(safe_fs.NotRegularFile):
            safe_fs.open_beneath(root, "a/sock")
    finally:
        sock.close()


def test_root_given_as_a_symlink_is_followed_once(tree, tmp_path):
    root, _ = tree
    alias = tmp_path / "root-alias"
    alias.symlink_to(root)
    assert safe_fs.read_bytes_beneath(alias, "a/b/f.txt", max_size=100) == b"inside"


def test_a_sub_root_is_reached_strictly(tree, tmp_path):
    # An agent's folder can be swapped by any container that mounts the
    # agents tree: open_root(AGENTS_DIR, agent) refuses the link.
    root, outside = tree
    with safe_fs.open_root(root, "a") as agent:
        assert safe_fs.read_bytes_beneath(agent, "b/f.txt", max_size=100) == b"inside"
    (root / "swapped").symlink_to(outside)
    with pytest.raises(safe_fs.SymlinkRefused):
        with safe_fs.open_root(root, "swapped"):
            pass


@pytest.mark.parametrize("bad", ["", "relative/root", -1, -100, True])
def test_a_root_must_be_absolute_or_a_descriptor(bad):
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.read_bytes_beneath(bad, "x", max_size=1)


def test_root_given_as_a_descriptor_is_left_open(tree):
    root, _ = tree
    with safe_fs.open_root(root) as rootfd:
        assert safe_fs.read_bytes_beneath(rootfd, "a/b/f.txt", max_size=100) == b"inside"
        os.fstat(rootfd)  # still open


# ── the race the helper exists for ─────────────────────────────────────


def test_a_swap_between_check_and_use_is_refused(tree):
    root, outside = tree
    (root / "a" / "sub").mkdir()
    (root / "a" / "sub" / "doc").write_bytes(b"mine")
    # The check a route makes today passes on the real directory...
    path_confinement.resolve_under(root / "a" / "sub" / "doc", root)
    # ...then the session swaps the directory for a link before the open.
    os.rename(root / "a" / "sub", root / "a" / "sub-real")
    (root / "a" / "sub").symlink_to(outside)
    (outside / "doc").write_bytes(b"SECRET")
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.read_bytes_beneath(root, "a/sub/doc", max_size=100)
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.atomic_write_beneath(root, "a/sub/doc", b"PWNED")
    assert (outside / "doc").read_bytes() == b"SECRET"


def test_a_racing_swap_never_yields_the_outside_bytes(tree):
    root, outside = tree
    sub = root / "a" / "sw"
    sub.mkdir()
    (sub / "f").write_bytes(b"ok")
    (outside / "f").write_bytes(b"SECRET")
    stop = threading.Event()

    def flip():
        while not stop.is_set():
            try:
                os.rename(sub, root / "a" / "sw-real")
                os.symlink(outside, sub)
                os.unlink(sub)
                os.rename(root / "a" / "sw-real", sub)
            except OSError:
                pass

    flipper = threading.Thread(target=flip)
    flipper.start()
    seen = set()
    try:
        for _ in range(3000):
            with contextlib.suppress(OSError):
                seen.add(safe_fs.read_bytes_beneath(root, "a/sw/f", max_size=100))
            with contextlib.suppress(OSError):
                safe_fs.atomic_write_beneath(root, "a/sw/w", b"x", fsync=False)
    finally:
        stop.set()
        flipper.join()
    assert b"SECRET" not in seen
    assert not (outside / "w").exists()


# ── writes ─────────────────────────────────────────────────────────────


def test_atomic_write_replaces_a_leaf_symlink_instead_of_writing_through_it(tree):
    root, outside = tree
    (root / "a" / "w").symlink_to(outside / "secret")
    safe_fs.atomic_write_beneath(root, "a/w", b"new")
    assert (outside / "secret").read_bytes() == b"SECRET"
    assert not (root / "a" / "w").is_symlink()
    assert (root / "a" / "w").read_bytes() == b"new"
    assert _no_partials(root)


def test_atomic_write_keeps_the_existing_mode_or_sets_the_one_asked(tree):
    root, _ = tree
    f = root / "a" / "b" / "f.txt"
    f.chmod(0o640)
    safe_fs.atomic_write_beneath(root, "a/b/f.txt", b"v2")
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    safe_fs.atomic_write_beneath(root, "a/b/f.txt", [b"v", b"3"], mode=0o600)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert f.read_bytes() == b"v3"


def test_atomic_write_never_exposes_new_content_under_looser_bits(tree):
    root, _ = tree
    f = root / "a" / "b" / "f.txt"
    f.chmod(0o600)
    with safe_fs.atomic_writer(root, "a/b/f.txt") as fh:
        fh.write(b"secret")
        [tmp] = [p for p in (root / "a" / "b").iterdir() if p.name.endswith(".partial")]
        assert stat.S_IMODE(tmp.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_a_long_multibyte_name_still_gets_a_temp(tree):
    root, _ = tree
    name = "\u6587" * 80  # 240 bytes: a legal name, too long once decorated by characters
    safe_fs.atomic_write_beneath(root, f"a/{name}", b"x")
    assert (root / "a" / name).read_bytes() == b"x"


def test_atomic_write_exclusive_and_mkdirs(tree):
    root, _ = tree
    with pytest.raises(FileExistsError):
        safe_fs.atomic_write_beneath(root, "a/b/f.txt", b"x", exclusive=True)
    assert (root / "a/b/f.txt").read_bytes() == b"inside"
    with pytest.raises(FileNotFoundError):
        safe_fs.atomic_write_beneath(root, "n/m/new", b"z")
    safe_fs.atomic_write_beneath(root, "n/m/new", b"z", mkdirs=True, exclusive=True)
    assert (root / "n/m/new").read_bytes() == b"z"
    assert _no_partials(root)


def test_atomic_writer_leaves_nothing_behind_on_an_exception(tree):
    root, _ = tree
    with pytest.raises(RuntimeError):
        with safe_fs.atomic_writer(root, "a/b/f.txt") as fh:
            fh.write(b"half")
            raise RuntimeError("producer failed")
    assert (root / "a/b/f.txt").read_bytes() == b"inside"
    assert _no_partials(root)


def test_mkdirs_over_a_file_is_file_exists(tree):
    root, _ = tree
    with pytest.raises(FileExistsError):
        safe_fs.mkdirs_beneath(root, "a/b/f.txt")


def test_mkdirs_refuses_a_symlinked_component(tree):
    root, outside = tree
    (root / "a" / "out").symlink_to(outside)
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.mkdirs_beneath(root, "a/out/x/y")
    assert not (outside / "x").exists()
    safe_fs.mkdirs_beneath(root, "a/new/deep")
    assert (root / "a/new/deep").is_dir()
    safe_fs.mkdirs_beneath(root, "a/new/deep")  # exist_ok
    with pytest.raises(FileExistsError):
        safe_fs.mkdirs_beneath(root, "a/new/deep", exist_ok=False)


def test_a_write_never_lands_outside_through_a_dangling_leaf(tree):
    root, outside = tree
    (root / "a" / "dangling").symlink_to(outside / "created")
    with pytest.raises(safe_fs.SymlinkRefused):
        os.close(safe_fs.open_beneath(root, "a/dangling", os.O_WRONLY | os.O_CREAT))
    assert not (outside / "created").exists()
    safe_fs.atomic_write_beneath(root, "a/dangling", b"x")
    assert not (outside / "created").exists()
    assert (root / "a" / "dangling").read_bytes() == b"x"


# ── copy, move, remove ─────────────────────────────────────────────────


def test_a_copy_is_capped_while_it_runs(tree):
    root, _ = tree
    src = root / "a" / "grow"
    src.write_bytes(b"x" * (1536 * 1024))
    appended = []

    def grow(chunk):
        if not appended:
            with open(src, "ab") as more:
                more.write(b"y" * (1024 * 1024))
            appended.append(True)

    with pytest.raises(safe_fs.FileTooLarge):
        safe_fs.copy_file_beneath(root, "a/grow", root, "a/copy", max_size=2 * 1024 * 1024,
                                  on_chunk=grow)
    assert not (root / "a" / "copy").exists()
    assert _no_partials(root)


def test_a_copy_drops_setuid_and_can_set_its_bits(tree):
    root, _ = tree
    (root / "a" / "b" / "f.txt").chmod(0o4755)
    safe_fs.copy_file_beneath(root, "a/b/f.txt", root, "a/c1")
    assert stat.S_IMODE((root / "a" / "c1").stat().st_mode) == 0o755
    safe_fs.copy_file_beneath(root, "a/b/f.txt", root, "a/c2", mode=0o640)
    assert stat.S_IMODE((root / "a" / "c2").stat().st_mode) == 0o640


def test_copy_file_keeps_bits_and_times_and_refuses_a_symlink_source(tree):
    root, outside = tree
    src = root / "a" / "b" / "f.txt"
    src.chmod(0o640)
    os.utime(src, ns=(1_000_000_000, 2_000_000_000))
    safe_fs.copy_file_beneath(root, "a/b/f.txt", root, "c/f.txt", mkdirs=True)
    dst = root / "c" / "f.txt"
    assert dst.read_bytes() == b"inside"
    assert stat.S_IMODE(dst.stat().st_mode) == 0o640
    assert dst.stat().st_mtime_ns == 2_000_000_000
    (root / "a" / "leak").symlink_to(outside / "secret")
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.copy_file_beneath(root, "a/leak", root, "c/leak")


def test_copytree_link_policies(tree):
    root, outside = tree
    (root / "a" / "out").symlink_to(outside)
    (root / "a" / "alias").symlink_to("b/f.txt")
    os.mkfifo(root / "a" / "pipe")
    skipped = safe_fs.copytree_beneath(root, "a", root, "skip")
    assert sorted(skipped) == ["a/alias", "a/out", "a/pipe"]
    assert (root / "skip/b/f.txt").read_bytes() == b"inside"
    assert not (root / "skip" / "out").exists()
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.copytree_beneath(root, "a", root, "refuse", symlinks="refuse")
    # "copy" recreates a link that stays inside; one that leaves is skipped.
    skipped = safe_fs.copytree_beneath(root, "a", root, "links", symlinks="copy")
    assert os.readlink(root / "links" / "alias") == "b/f.txt"
    assert not (root / "links" / "out").exists()
    assert sorted(skipped) == ["a/out", "a/pipe"]
    with pytest.raises(FileExistsError):
        safe_fs.copytree_beneath(root, "a", root, "links")
    safe_fs.copytree_beneath(root, "a", root, "links", dirs_exist_ok=True, symlinks="copy")
    # A relative link that stays inside at its old depth but not its new one.
    (root / "a" / "b" / "up").symlink_to("../../outside-ish")
    skipped = safe_fs.copytree_beneath(root, "a/b", root, "flat", symlinks="copy")
    assert skipped == ["a/b/up"] and not os.path.lexists(root / "flat" / "up")


def test_copytree_ignore_and_no_copy_into_itself(tree):
    root, _ = tree
    (root / "a" / "b" / ".marker").write_bytes(b"m")
    safe_fs.copytree_beneath(root, "a", root, "ign",
                             ignore=lambda rel, names: {n for n in names if n.startswith(".")})
    assert (root / "ign/b/f.txt").exists() and not (root / "ign/b/.marker").exists()
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.copytree_beneath(root, "a", root, "a/b/c")
    with safe_fs.open_root(root, "a") as a_handle:
        # The same tree through another root: the copy never copies itself.
        safe_fs.copytree_beneath(root, "a", a_handle, "b/inner")
    assert not (root / "a" / "b" / "inner" / "b" / "inner").exists()


def test_rename_replace_and_no_replace(tree):
    root, outside = tree
    (root / "a" / "g").write_bytes(b"g")
    with pytest.raises(FileExistsError):
        safe_fs.rename_beneath(root, "a/g", "a/b/f.txt")
    safe_fs.rename_beneath(root, "a/g", "a/h")
    assert (root / "a/h").read_bytes() == b"g"
    (root / "a" / "out").symlink_to(outside)
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.rename_beneath(root, "a/h", "a/out/h")
    assert not (outside / "h").exists()
    # A link moves as the link itself.
    safe_fs.rename_beneath(root, "a/out", "a/out2")
    assert os.readlink(root / "a" / "out2") == str(outside)


def test_move_falls_back_to_copy_across_filesystems(tree, monkeypatch):
    root, outside = tree
    (root / "a" / "lnk").symlink_to(outside)

    def cross_device(*_a, **_k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(safe_fs, "rename_beneath", cross_device)
    (root / "a" / "lnk").unlink()
    (root / "a" / "alias").symlink_to("b/f.txt")
    safe_fs.move_beneath(root, "a", "moved")
    assert (root / "moved/b/f.txt").read_bytes() == b"inside"
    assert os.readlink(root / "moved" / "alias") == "b/f.txt"
    assert not (root / "a").exists()
    assert (outside / "secret").exists()


def test_an_incomplete_cross_device_move_keeps_the_source(tree, monkeypatch):
    root, outside = tree
    (root / "a" / "out").symlink_to(outside)  # would leave the destination root
    monkeypatch.setattr(safe_fs, "rename_beneath",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    with pytest.raises(OSError) as exc:
        safe_fs.move_beneath(root, "a", "moved")
    assert exc.value.errno == errno.EXDEV
    assert (root / "a" / "b" / "f.txt").exists() and not (root / "moved").exists()


def test_missing_ok(tree):
    root, _ = tree
    with pytest.raises(FileNotFoundError):
        safe_fs.unlink_beneath(root, "a/none")
    safe_fs.unlink_beneath(root, "a/none", missing_ok=True)
    safe_fs.rmtree_beneath(root, "a/none/deeper", missing_ok=True)


def test_unlink_removes_a_link_not_its_target_and_rmtree_refuses_one(tree):
    root, outside = tree
    (root / "a" / "out").symlink_to(outside)
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.rmtree_beneath(root, "a/out")
    safe_fs.unlink_beneath(root, "a/out")
    assert (outside / "secret").exists()
    safe_fs.rmtree_beneath(root, "a/b")
    assert not (root / "a" / "b").exists()
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.rmtree_beneath(root, "")


def test_rmtree_does_not_follow_a_link_inside_the_tree(tree):
    root, outside = tree
    (root / "a" / "b" / "out").symlink_to(outside)
    safe_fs.rmtree_beneath(root, "a")
    assert (outside / "secret").read_bytes() == b"SECRET"


# ── walk and path helpers ──────────────────────────────────────────────


def test_walk_lists_by_kind_and_never_enters_a_link(tree):
    root, outside = tree
    (root / "a" / "lnk").symlink_to(outside)
    (root / "a" / "c").mkdir()
    steps = list(safe_fs.walk_beneath(root))
    assert [s.rel for s in steps] == ["", "a", "a/b", "a/c"]
    step_a = steps[1]
    assert step_a.dirs == ["b", "c"] and step_a.symlinks == ["lnk"]
    assert steps[2].files == ["f.txt"]


def test_a_walk_step_handle_is_dead_after_its_step(tree):
    root, _ = tree
    steps = list(safe_fs.walk_beneath(root))
    with pytest.raises(ValueError):
        _ = steps[1].dirfd


def test_a_directory_the_walk_cannot_enter_is_never_skipped_silently(tree, monkeypatch):
    root, _ = tree
    for i in range(5):
        (root / "a" / f"d{i}").mkdir()
    real_open = os.open

    def exhausted(path, flags, *args, **kwargs):
        if path == "d3":
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safe_fs.os, "open", exhausted)
    with pytest.raises(OSError) as exc:
        list(safe_fs.walk_beneath(root))
    assert exc.value.errno == errno.EMFILE
    errors = []
    seen = [s.rel for s in safe_fs.walk_beneath(root, onerror=errors.append)]
    assert "a/d3" not in seen and "a/d4" in seen
    assert [e.errno for e in errors] == [errno.EMFILE]
    with pytest.raises(OSError):
        safe_fs.copytree_beneath(root, "a", root, "copy")
    assert not (root / "copy").exists()  # a failed copy removes what it made


def test_a_wide_tree_holds_one_handle_per_level(tree):
    root, _ = tree
    for i in range(300):
        (root / "a" / f"w{i:03}").mkdir()
    before = len(os.listdir("/proc/self/fd"))
    peak = 0
    for _step in safe_fs.walk_beneath(root):
        peak = max(peak, len(os.listdir("/proc/self/fd")) - before)
    assert peak < 10


def test_walk_can_be_pruned(tree):
    root, _ = tree
    seen = []
    for step in safe_fs.walk_beneath(root):
        seen.append(step.rel)
        step.dirs[:] = [d for d in step.dirs if d != "b"]
    assert seen == ["", "a"]


def test_canonical_rel_follows_in_tree_links_only(tree):
    root, outside = tree
    (root / "a" / "alias").symlink_to("b")
    (root / "a" / "out").symlink_to(outside)
    assert safe_fs.canonical_rel(root, "a/alias/f.txt") == "a/b/f.txt"
    assert safe_fs.canonical_rel(root, "a/alias/new-file") == "a/b/new-file"
    with pytest.raises(safe_fs.EscapeRefused):
        safe_fs.canonical_rel(root, "a/out/secret")


def test_rel_under_and_split_under(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert safe_fs.rel_under(str(root / "a" / "b"), root) == "a/b"
    assert safe_fs.rel_under(str(root), root) == ""
    for bad in (str(tmp_path / "rootx" / "a"), str(tmp_path)):
        with pytest.raises(safe_fs.EscapeRefused):
            safe_fs.rel_under(bad, root)
    for bad in (f"{root}/users/pm/../other/secret", f"{root}/a/./b", f"{root}//a", "relative/path"):
        with pytest.raises(safe_fs.UnsafePathError):
            safe_fs.rel_under(bad, root)
    other = tmp_path / "cache"
    assert safe_fs.split_under(str(other / "x.mp4"), [root, other]) == (str(other), "x.mp4")
    nested = root / ".remote-host-cache"
    assert safe_fs.split_under(str(nested / "s1" / "doc.pdf"), [root, nested]) == (str(nested), "s1/doc.pdf")
    with pytest.raises(safe_fs.EscapeRefused):
        safe_fs.split_under("/etc/passwd", [root, other])


# ── the probe ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EPERM])
def test_the_probe_selects_the_walk_when_openat2_is_missing_or_filtered(monkeypatch, code):
    def refused(*_a, **_k):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(safe_fs, "_raw_openat2", refused)
    assert safe_fs._probe_openat2() is False


def test_the_env_knob_forces_the_walk(monkeypatch):
    monkeypatch.setenv("OTODOCK_SAFE_FS_NO_OPENAT2", "1")
    assert safe_fs._probe_openat2() is False


def test_the_file_tools_copy_is_byte_identical():
    """The file-tools image carries its own copy of this module (no proxy
    import there); the two live in one tree and move together."""
    from pathlib import Path
    here = Path(__file__).resolve()
    repo = here.parents[3]
    src = repo / "proxy" / "services" / "infra" / "safe_fs.py"
    copy = repo / "mcps" / "custom" / "file-tools-mcp" / "safe_fs.py"
    assert copy.read_bytes() == src.read_bytes(), "refresh with: cp proxy/services/infra/safe_fs.py mcps/custom/file-tools-mcp/safe_fs.py"
