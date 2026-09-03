"""Satellite-host policy re-check (`_check_satellite_host_policy`) — the
defense-in-depth gate on the file push/pull channel, including the
Claude-CLI runtime-tree admission (`_is_claude_runtime_path`) with its
realpath + ownership tightenings.
"""

import os

import pytest

from satellite.sessions import session_manager as sm


@pytest.fixture(autouse=True)
def _full_fs_off(monkeypatch):
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: False)


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    """A fake claude runtime root the helper resolves to (own-uid, 0700)."""
    root = tmp_path / f"claude-{os.getuid()}"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(sm, "_claude_runtime_root", lambda: str(root))
    return root


def test_home_paths_still_admitted():
    sm._check_satellite_host_policy(str(sm.Path.home() / "some" / "file.txt"))


def test_outside_home_rejected():
    with pytest.raises(ValueError, match="outside the OS user's home"):
        sm._check_satellite_host_policy("/tmp/evil.txt")


def test_full_fs_on_admits_everything(monkeypatch):
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: True)
    sm._check_satellite_host_policy("/etc/hosts")


def test_runtime_tree_admitted(runtime_root):
    tree = runtime_root / "-home-dave" / "sid-1234" / "scratchpad"
    tree.mkdir(parents=True)
    (tree / "notes.txt").write_text("x")
    sm._check_satellite_host_policy(str(tree / "notes.txt"))
    # Not-yet-existing file inside the tree (push of a new file) also passes.
    sm._check_satellite_host_policy(str(tree / "new-file.txt"))


def test_runtime_tree_symlink_escape_rejected(runtime_root, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret")
    tree = runtime_root / "-home-dave" / "sid-1234"
    tree.mkdir(parents=True)
    (tree / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="outside the OS user's home"):
        sm._check_satellite_host_policy(str(tree / "link"))


def test_runtime_tree_foreign_owned_root_rejected(runtime_root, monkeypatch):
    tree = runtime_root / "x"
    tree.mkdir()
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.realpath(str(path)) == os.path.realpath(str(runtime_root)):
            class _St:
                st_uid = st.st_uid + 1
                st_mode = st.st_mode
            return _St()
        return st

    monkeypatch.setattr(sm.os, "stat", fake_stat)
    with pytest.raises(ValueError, match="outside the OS user's home"):
        sm._check_satellite_host_policy(str(tree / "f.txt"))


def test_runtime_tree_world_writable_root_rejected(runtime_root):
    os.chmod(runtime_root, 0o777)
    with pytest.raises(ValueError, match="outside the OS user's home"):
        sm._check_satellite_host_policy(str(runtime_root / "x" / "f.txt"))


def test_paths_outside_runtime_root_unaffected(runtime_root):
    with pytest.raises(ValueError, match="outside the OS user's home"):
        sm._check_satellite_host_policy("/tmp/claude-99999/other/f.txt")
