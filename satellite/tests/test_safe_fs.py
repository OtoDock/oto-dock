"""``satellite.host.safe_fs``: the satellite's twin of the proxy helper, for
the applier, the pull and the stat. The POSIX branch runs for real on the
Linux suite; the Windows branch runs through the module's seam over a real
temporary tree, with reparse tags injected by the fake, so its checks (the
chain, the leaf, the re-check before the commit, the random temp) are
exercised without a Windows machine.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
from types import SimpleNamespace

import pytest

from satellite import config
from satellite.host import safe_fs


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "agents"
    (root / "a1" / "workspace" / "sub").mkdir(parents=True)
    (root / "a1" / "workspace" / "sub" / "f.txt").write_bytes(b"inside")
    (root / "a1" / "users" / "other").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_bytes(b"ORIGINAL")
    return root, outside


def _victim_untouched(outside):
    assert (outside / "f.txt").read_bytes() == b"ORIGINAL"
    assert sorted(p.name for p in outside.iterdir()) == ["f.txt"]


# ── the string guard ───────────────────────────────────────────────────


@pytest.mark.parametrize("rel", ["/abs", "a/../b", "./a", "a//b", "a/", "", "a\x00b"])
def test_bad_rels_are_refused_before_any_call(tree, rel):
    root, _ = tree
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.atomic_write_beneath(root, rel, b"x")


def test_a_relative_or_empty_root_is_refused(tree):
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.lstat_beneath("agents", "a1/workspace/sub/f.txt")


# ── POSIX: links refused at every component, writes atomic ─────────────


def test_write_read_and_stat_of_a_regular_file(tree):
    root, _ = tree
    safe_fs.atomic_write_beneath(root, "a1/workspace/sub/g.txt", b"new")
    assert (root / "a1" / "workspace" / "sub" / "g.txt").read_bytes() == b"new"
    assert safe_fs.read_bytes_beneath(root, "a1/workspace/sub/g.txt", max_size=10) == b"new"
    st = safe_fs.lstat_beneath(root, "a1/workspace/sub/g.txt")
    assert stat.S_ISREG(st.st_mode) and st.st_size == 3
    assert not list((root / "a1" / "workspace" / "sub").glob("*.partial"))


def test_a_leaf_link_is_replaced_never_written_through(tree):
    root, outside = tree
    (root / "a1" / "workspace" / "leak.txt").symlink_to(outside / "f.txt")
    safe_fs.atomic_write_beneath(root, "a1/workspace/leak.txt", b"new")
    _victim_untouched(outside)
    leaf = root / "a1" / "workspace" / "leak.txt"
    assert not leaf.is_symlink() and leaf.read_bytes() == b"new"


def test_a_link_component_is_refused_for_every_operation(tree):
    root, outside = tree
    (root / "a1" / "workspace" / "lnk").symlink_to(outside)
    for op in (
        lambda: safe_fs.atomic_write_beneath(root, "a1/workspace/lnk/f.txt", b"new"),
        lambda: safe_fs.mkdirs_beneath(root, "a1/workspace/lnk/deeper"),
        lambda: safe_fs.unlink_beneath(root, "a1/workspace/lnk/f.txt"),
        lambda: safe_fs.rename_beneath(root, "a1/workspace/sub/f.txt", "a1/workspace/lnk/f.txt", replace=True),
        lambda: safe_fs.read_bytes_beneath(root, "a1/workspace/lnk/f.txt", max_size=100),
        lambda: safe_fs.lstat_beneath(root, "a1/workspace/lnk/f.txt"),
        lambda: os.close(safe_fs.open_append_beneath(root, "a1/workspace/lnk/f.txt")),
    ):
        with pytest.raises(safe_fs.SymlinkRefused):
            op()
    _victim_untouched(outside)
    assert (root / "a1" / "workspace" / "sub" / "f.txt").exists()


def test_an_in_tree_link_into_another_users_folder_is_refused(tree):
    root, _ = tree
    (root / "a1" / "workspace" / "alias").symlink_to("../users/other")
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.atomic_write_beneath(root, "a1/workspace/alias/f.txt", b"new")
    assert not (root / "a1" / "users" / "other" / "f.txt").exists()


def test_a_swap_after_the_root_is_held_is_refused(tree):
    """The component is swapped once the root is held: the walk from the
    held handle refuses it, the outside file stays as it was."""
    root, outside = tree
    real = safe_fs._parent_posix
    state = {"done": False}

    def _swap(rootfd, parts, **kw):
        if not state["done"] and parts:
            state["done"] = True
            d = root / "a1" / "workspace" / "sub"
            (d / "f.txt").unlink()
            d.rmdir()
            os.symlink(outside, d)
        return real(rootfd, parts, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(safe_fs, "_parent_posix", _swap)
        with pytest.raises(safe_fs.SymlinkRefused):
            safe_fs.atomic_write_beneath(root, "a1/workspace/sub/f.txt", b"new")
    _victim_untouched(outside)


def test_a_flipping_component_never_writes_outside(tree):
    root, outside = tree
    d = root / "a1" / "workspace" / "sub"
    stop = threading.Event()

    def _flip():
        import contextlib
        while not stop.is_set():
            with contextlib.suppress(OSError):
                if d.is_symlink():
                    d.unlink()
                    d.mkdir()
                else:
                    for p in d.iterdir():
                        p.unlink()
                    d.rmdir()
                    os.symlink(outside, d)

    t = threading.Thread(target=_flip)
    t.start()
    import contextlib
    try:
        for _ in range(400):
            with contextlib.suppress(OSError):
                safe_fs.atomic_write_beneath(root, "a1/workspace/sub/f.txt", b"new")
    finally:
        stop.set()
        t.join()
    _victim_untouched(outside)


def test_append_truncate_and_commit_of_a_staged_partial(tree):
    root, outside = tree
    (root / "a1" / "workspace" / "big.bin.partial").symlink_to(outside / "f.txt")
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.open_append_beneath(root, "a1/workspace/big.bin.partial", truncate=True)
    safe_fs.unlink_beneath(root, "a1/workspace/big.bin.partial")
    fd = safe_fs.open_append_beneath(root, "a1/workspace/big.bin.partial", truncate=True)
    os.write(fd, b"A" * 4)
    os.close(fd)
    fd = safe_fs.open_append_beneath(root, "a1/workspace/big.bin.partial")
    os.write(fd, b"B" * 4)
    os.close(fd)
    safe_fs.commit_partial(root, "a1/workspace/big.bin.partial", "a1/workspace/big.bin")
    assert (root / "a1" / "workspace" / "big.bin").read_bytes() == b"AAAABBBB"
    _victim_untouched(outside)


def test_rename_refuses_an_existing_target_unless_replace(tree):
    root, _ = tree
    (root / "a1" / "workspace" / "g.txt").write_bytes(b"g")
    with pytest.raises(FileExistsError):
        safe_fs.rename_beneath(root, "a1/workspace/sub/f.txt", "a1/workspace/g.txt")
    safe_fs.rename_beneath(root, "a1/workspace/sub/f.txt", "a1/workspace/g.txt", replace=True)
    assert (root / "a1" / "workspace" / "g.txt").read_bytes() == b"inside"
    safe_fs.rename_beneath(root, "a1/workspace/g.txt", "a1/workspace/new/dir/h.txt", mkdirs=True)
    assert (root / "a1" / "workspace" / "new" / "dir" / "h.txt").exists()


def test_rmdir_and_unlink_and_missing_ok(tree):
    root, _ = tree
    safe_fs.unlink_beneath(root, "a1/workspace/sub/f.txt")
    safe_fs.unlink_beneath(root, "a1/workspace/sub/f.txt", missing_ok=True)
    with pytest.raises(FileNotFoundError):
        safe_fs.unlink_beneath(root, "a1/workspace/sub/f.txt")
    safe_fs.rmdir_beneath(root, "a1/workspace/sub")
    assert not (root / "a1" / "workspace" / "sub").exists()
    (root / "a1" / "workspace" / "lnkdir").symlink_to(root / "a1" / "users")
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.rmdir_beneath(root, "a1/workspace/lnkdir")
    safe_fs.unlink_beneath(root, "a1/workspace/lnkdir")
    assert (root / "a1" / "users").is_dir()


def test_a_fifo_is_refused_without_blocking(tree):
    root, _ = tree
    os.mkfifo(root / "a1" / "workspace" / "pipe")
    with pytest.raises(safe_fs.NotRegularFile):
        safe_fs.read_bytes_beneath(root, "a1/workspace/pipe", max_size=10)
    with pytest.raises(safe_fs.NotRegularFile):
        os.close(safe_fs.open_append_beneath(root, "a1/workspace/pipe"))


def test_the_size_cap_is_checked(tree):
    root, _ = tree
    with pytest.raises(safe_fs.FileTooLarge):
        safe_fs.read_bytes_beneath(root, "a1/workspace/sub/f.txt", max_size=2)


def _sample_folder(tree):
    """``a1/workspace/mix``: a folder, a file, a link and a pipe."""
    root, outside = tree
    mix = root / "a1" / "workspace" / "mix"
    (mix / "dir").mkdir(parents=True)
    (mix / "file.txt").write_bytes(b"f")
    (mix / "link").symlink_to(outside)
    os.mkfifo(mix / "pipe")
    return root, outside, mix


def test_list_beneath_names_a_folders_entries_by_kind(tree):
    root, _outside, _mix = _sample_folder(tree)
    got = safe_fs.list_beneath(root, "a1/workspace/mix", max_entries=10)
    assert (got.dirs, got.files, got.links, got.other) == (["dir"], ["file.txt"], ["link"], ["pipe"])
    top = safe_fs.list_beneath(root, max_entries=10)
    assert top.dirs == ["a1"] and not (top.files or top.links or top.other)
    with safe_fs.open_root(root, "a1/workspace") as r:
        assert safe_fs.list_beneath(r, "mix", max_entries=10).links == ["link"]


def test_list_beneath_refuses_past_its_cap_and_a_link_on_the_way(tree):
    root, outside, _mix = _sample_folder(tree)
    assert len(safe_fs.list_beneath(root, "a1/workspace/mix", max_entries=4).other) == 1
    with pytest.raises(safe_fs.TooManyEntries):
        safe_fs.list_beneath(root, "a1/workspace/mix", max_entries=3)
    assert issubclass(safe_fs.TooManyEntries, safe_fs.SafeFsError)
    for rel in ("a1/workspace/mix/link", "a1/workspace/mix/link/sub"):
        with pytest.raises(safe_fs.SymlinkRefused):
            safe_fs.list_beneath(root, rel, max_entries=10)
    with pytest.raises(NotADirectoryError):
        safe_fs.list_beneath(root, "a1/workspace/mix/file.txt", max_entries=10)
    with pytest.raises(FileNotFoundError):
        safe_fs.list_beneath(root, "a1/workspace/absent", max_entries=10)
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.list_beneath(root, "a1/../a1", max_entries=10)
    _victim_untouched(outside)


def test_rmtree_beneath_removes_a_tree_and_only_the_links_in_it(tree):
    root, outside, mix = _sample_folder(tree)
    deep = mix / "dir" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "g.txt").write_bytes(b"g")
    (mix / "dir" / "b" / "back").symlink_to(root / "a1" / "users", target_is_directory=True)
    safe_fs.rmtree_beneath(root, "a1/workspace/mix")
    assert not mix.exists()
    _victim_untouched(outside)
    assert (root / "a1" / "users" / "other").is_dir()
    assert (root / "a1" / "workspace" / "sub" / "f.txt").read_bytes() == b"inside"


def test_rmtree_beneath_refuses_a_link_a_file_and_the_root(tree):
    root, outside, mix = _sample_folder(tree)
    (root / "a1" / "workspace" / "alias").symlink_to(mix, target_is_directory=True)
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.rmtree_beneath(root, "a1/workspace/alias")
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.rmtree_beneath(root, "a1/workspace/mix/link/x")
    with pytest.raises(NotADirectoryError):
        safe_fs.rmtree_beneath(root, "a1/workspace/mix/file.txt")
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.rmtree_beneath(root, "")
    assert (mix / "dir").is_dir() and (root / "a1" / "workspace" / "alias").is_symlink()
    _victim_untouched(outside)


def test_rmtree_beneath_missing_ok(tree):
    root, _ = tree
    with pytest.raises(FileNotFoundError):
        safe_fs.rmtree_beneath(root, "a1/workspace/absent")
    safe_fs.rmtree_beneath(root, "a1/workspace/absent", missing_ok=True)
    safe_fs.rmtree_beneath(root, "a1/absent/deeper", missing_ok=True)


def test_rmtree_beneath_stops_at_a_folder_swapped_for_a_link_on_its_way_down(tree):
    """A folder inside the tree becomes a link while the tree goes: the walk
    down refuses it and nothing outside goes."""
    root, outside, mix = _sample_folder(tree)
    (mix / "dir" / "inner").mkdir()
    real = safe_fs._step_dir
    state = {"done": False}

    def _swap(dirfd, name, shown):
        if name == "inner" and not state["done"]:
            state["done"] = True
            (mix / "dir" / "inner").rmdir()
            os.symlink(outside, mix / "dir" / "inner")
        return real(dirfd, name, shown)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(safe_fs, "_step_dir", _swap)
        with pytest.raises(safe_fs.SymlinkRefused):
            safe_fs.rmtree_beneath(root, "a1/workspace/mix")
    _victim_untouched(outside)


def test_open_root_reaches_a_subfolder_strictly_and_passes_a_root_through(tree):
    root, outside = tree
    with safe_fs.open_root(root, "a1") as r:
        safe_fs.atomic_write_beneath(r, "workspace/sub/h.txt", b"h")
        with safe_fs.open_root(r) as same:
            assert same is r
    assert (root / "a1" / "workspace" / "sub" / "h.txt").exists()
    (root / "lnk").symlink_to(outside)
    with pytest.raises(safe_fs.SymlinkRefused):
        with safe_fs.open_root(root, "lnk"):
            pass


# ── the module imports where the POSIX names are absent ────────────────


def test_the_module_imports_without_the_posix_names(monkeypatch):
    import importlib
    monkeypatch.setitem(sys.modules, "fcntl", None)
    for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_PATH", "O_NOCTTY", "O_CLOEXEC"):
        monkeypatch.delattr(os, name, raising=False)
    mod = importlib.reload(safe_fs)
    assert mod.SafeFsError is not None
    monkeypatch.undo()
    importlib.reload(safe_fs)


# ── Windows: the checked chain through the seam ───────────────────────


class _FakeWin:
    """The Windows branch over a real temporary tree: ``tags`` names the
    paths whose lstat reports a reparse tag, ``calls`` records the order."""

    def __init__(self, monkeypatch, tags=None):
        self.tags = dict(tags or {})
        self.calls: list[tuple] = []
        win = config.of(config.WINDOWS)
        monkeypatch.setattr(config, "HOST", win)
        monkeypatch.setattr(safe_fs, "_w_lstat", self._lstat)
        monkeypatch.setattr(safe_fs, "_w_entry_lstat", lambda entry: self._lstat(entry.path))
        for name in ("_w_open", "_w_mkdir", "_w_replace", "_w_rename", "_w_unlink", "_w_rmdir", "_w_fstat",
                     "_w_rmtree"):
            monkeypatch.setattr(safe_fs, name, self._record(name, getattr(safe_fs, name)))

    def _record(self, name, real):
        def _f(*a):
            self.calls.append((name, *a))
            return real(*a)
        return _f

    def _lstat(self, path):
        self.calls.append(("_w_lstat", path))
        st = os.lstat(path)
        tag = self.tags.get(os.path.normpath(path), 0)
        if tag:
            fields = list(st)
            ns = SimpleNamespace(
                st_mode=st.st_mode, st_ino=st.st_ino, st_dev=st.st_dev, st_nlink=st.st_nlink,
                st_uid=st.st_uid, st_gid=st.st_gid, st_size=st.st_size, st_atime=st.st_atime,
                st_mtime=st.st_mtime, st_ctime=st.st_ctime, st_reparse_tag=tag, st_mtime_ns=st.st_mtime_ns,
            )
            del fields
            return ns
        return st


SYMLINK_TAG = 0xA000000C
MOUNT_POINT_TAG = 0xA0000003
CLOUD_PLACEHOLDER_TAG = 0x9000601A


def test_windows_branch_writes_and_reads_through_a_checked_chain(tree, monkeypatch):
    root, _ = tree
    fake = _FakeWin(monkeypatch)
    safe_fs.atomic_write_beneath(root, "a1/workspace/sub/g.txt", b"new")
    assert (root / "a1" / "workspace" / "sub" / "g.txt").read_bytes() == b"new"
    names = [c[0] for c in fake.calls]
    # every component checked, the temp opened, the chain re-checked, then the commit
    assert names.index("_w_open") < names.index("_w_replace")
    assert names.count("_w_lstat") >= 6
    tmp = fake.calls[names.index("_w_open")][1]
    assert os.path.basename(tmp).startswith(".g.txt.") and tmp.endswith(".partial")
    assert safe_fs.read_bytes_beneath(root, "a1/workspace/sub/g.txt", max_size=10) == b"new"


def test_windows_branch_refuses_a_junction_or_symlink_component(tree, monkeypatch):
    root, outside = tree
    sub = os.path.normpath(str(root / "a1" / "workspace" / "sub"))
    for tag in (SYMLINK_TAG, MOUNT_POINT_TAG):
        _FakeWin(monkeypatch, tags={sub: tag})
        with pytest.raises(safe_fs.SymlinkRefused):
            safe_fs.atomic_write_beneath(root, "a1/workspace/sub/f.txt", b"new")
        with pytest.raises(safe_fs.SymlinkRefused):
            safe_fs.read_bytes_beneath(root, "a1/workspace/sub/f.txt", max_size=100)
        with pytest.raises(safe_fs.SymlinkRefused):
            safe_fs.mkdirs_beneath(root, "a1/workspace/sub/deeper")
    assert (root / "a1" / "workspace" / "sub" / "f.txt").read_bytes() == b"inside"


def test_windows_branch_refuses_a_symlink_leaf_and_admits_a_placeholder(tree, monkeypatch):
    root, _ = tree
    leaf = os.path.normpath(str(root / "a1" / "workspace" / "sub" / "f.txt"))
    _FakeWin(monkeypatch, tags={leaf: SYMLINK_TAG})
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.read_bytes_beneath(root, "a1/workspace/sub/f.txt", max_size=100)
    with pytest.raises(safe_fs.SymlinkRefused):
        os.close(safe_fs.open_append_beneath(root, "a1/workspace/sub/f.txt"))
    _FakeWin(monkeypatch, tags={leaf: CLOUD_PLACEHOLDER_TAG})
    assert safe_fs.read_bytes_beneath(root, "a1/workspace/sub/f.txt", max_size=100) == b"inside"


def test_windows_branch_refuses_separators_inside_a_name(tree, monkeypatch):
    root, _ = tree
    _FakeWin(monkeypatch)
    for rel in ("a1/workspace/sub\\x", "a1/workspace/f:stream"):
        with pytest.raises(safe_fs.UnsafePathError):
            safe_fs.atomic_write_beneath(root, rel, b"x")


def test_windows_branch_rename_unlink_and_rmdir(tree, monkeypatch):
    root, _ = tree
    _FakeWin(monkeypatch)
    (root / "a1" / "workspace" / "g.txt").write_bytes(b"g")
    with pytest.raises(FileExistsError):
        safe_fs.rename_beneath(root, "a1/workspace/sub/f.txt", "a1/workspace/g.txt")
    safe_fs.rename_beneath(root, "a1/workspace/sub/f.txt", "a1/workspace/g.txt", replace=True)
    assert (root / "a1" / "workspace" / "g.txt").read_bytes() == b"inside"
    safe_fs.unlink_beneath(root, "a1/workspace/g.txt")
    safe_fs.rmdir_beneath(root, "a1/workspace/sub")
    assert not (root / "a1" / "workspace" / "sub").exists()
    sub = os.path.normpath(str(root / "a1" / "users" / "other"))
    _FakeWin(monkeypatch, tags={sub: MOUNT_POINT_TAG})
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.rmdir_beneath(root, "a1/users/other")


def test_windows_branch_lists_a_name_surrogate_as_a_link(tree, monkeypatch):
    root, _ = tree
    ws = root / "a1" / "workspace"
    (ws / "junction").mkdir()
    (ws / "placeholder.txt").write_bytes(b"p")
    fake = _FakeWin(monkeypatch, tags={
        os.path.normpath(str(ws / "junction")): MOUNT_POINT_TAG,
        os.path.normpath(str(ws / "placeholder.txt")): CLOUD_PLACEHOLDER_TAG,
    })
    got = safe_fs.list_beneath(root, "a1/workspace", max_entries=10)
    assert (got.dirs, got.files, got.links, got.other) == (["sub"], ["placeholder.txt"], ["junction"], [])
    checked = [c[1] for c in fake.calls if c[0] == "_w_lstat"]
    assert os.path.normpath(str(root / "a1")) in checked and os.path.normpath(str(ws)) in checked
    with pytest.raises(safe_fs.TooManyEntries):
        safe_fs.list_beneath(root, "a1/workspace", max_entries=2)
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.list_beneath(root, "a1/workspace/junction", max_entries=10)
    _FakeWin(monkeypatch, tags={os.path.normpath(str(root / "a1")): SYMLINK_TAG})
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.list_beneath(root, "a1/workspace", max_entries=10)


def test_windows_branch_rmtree_checks_the_chain_and_the_name_first(tree, monkeypatch):
    root, _ = tree
    ws = root / "a1" / "workspace"
    fake = _FakeWin(monkeypatch, tags={os.path.normpath(str(ws / "sub")): MOUNT_POINT_TAG})
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.rmtree_beneath(root, "a1/workspace/sub")
    assert (ws / "sub" / "f.txt").exists()
    assert not [c for c in fake.calls if c[0] == "_w_rmtree"]
    _FakeWin(monkeypatch, tags={os.path.normpath(str(ws)): SYMLINK_TAG})
    with pytest.raises(safe_fs.SymlinkRefused):
        safe_fs.rmtree_beneath(root, "a1/workspace/sub")
    fake = _FakeWin(monkeypatch)
    with pytest.raises(NotADirectoryError):
        safe_fs.rmtree_beneath(root, "a1/workspace/sub/f.txt")
    safe_fs.rmtree_beneath(root, "a1/workspace/absent", missing_ok=True)
    safe_fs.rmtree_beneath(root, "a1/absent/deeper", missing_ok=True)
    with pytest.raises(FileNotFoundError):
        safe_fs.rmtree_beneath(root, "a1/workspace/absent")
    safe_fs.rmtree_beneath(root, "a1/workspace/sub")
    assert not (ws / "sub").exists()
    removed = [c[1] for c in fake.calls if c[0] == "_w_rmtree"]
    assert removed == [os.path.join(str(ws), "sub")]


def test_the_branch_follows_the_host_table(tree, monkeypatch):
    root, _ = tree
    assert safe_fs._posix() is True
    monkeypatch.setattr(config, "HOST", config.of(config.WINDOWS))
    assert safe_fs._posix() is False
    monkeypatch.setattr(config, "HOST", config.of(config.DARWIN))
    assert safe_fs._posix() is True
    assert errno.ELOOP  # the refusal's errno on every family
