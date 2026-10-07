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


# ---------------------------------------------------------------------------
# The machine's own state is refused on every pairing, before the home band
# and before the runtime-tree admission
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    from satellite import config
    root = tmp_path / "home" / ".oto-dock"
    (root / "agents" / "a1" / "workspace").mkdir(parents=True)
    (root / "agents" / "a2" / "workspace").mkdir(parents=True)
    (root / "mcps" / "community" / "foo").mkdir(parents=True)
    (root / "satellite.conf").write_text("x")
    monkeypatch.setattr(config, "otodock_dir", lambda: root)
    return root


def _check(state, raw, *, own="a1"):
    return sm._check_satellite_host_policy(
        raw, own_agent=own, agents_dir=state / "agents", mcps_dir=state / "mcps",
    )


def test_the_state_root_is_refused_even_with_full_fs_on(state, monkeypatch):
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: True)
    for raw in (str(state / "satellite.conf"), str(state), str(state / "browser-profiles" / "x")):
        with pytest.raises(ValueError, match="own OtoDock state"):
            _check(state, raw)


def test_another_agents_tree_and_the_mcps_folder_are_refused(state, monkeypatch):
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: True)
    with pytest.raises(ValueError, match="own OtoDock state"):
        _check(state, str(state / "agents" / "a2" / "workspace" / "x.md"))
    with pytest.raises(ValueError, match="own OtoDock state"):
        _check(state, str(state / "agents"))
    with pytest.raises(ValueError, match="own OtoDock state"):
        _check(state, str(state / "mcps" / "community" / "foo" / "server.py"))


def test_the_sessions_own_subtree_is_admitted_and_only_for_a_safe_slug(state, monkeypatch):
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: True)
    _check(state, str(state / "agents" / "a1" / "workspace" / "x.md"))  # no raise
    with pytest.raises(ValueError):
        _check(state, str(state / "agents" / "a2" / "workspace" / "x.md"), own="..")
    with pytest.raises(ValueError):
        _check(state, str(state / "agents" / "a1" / "workspace" / "x.md"), own="")


def test_a_link_into_the_state_is_judged_by_its_target(state, tmp_path, monkeypatch):
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: True)
    (tmp_path / "home" / "Desktop").mkdir(parents=True)
    os.symlink(state / "agents" / "a2", tmp_path / "home" / "Desktop" / "lnk")
    with pytest.raises(ValueError, match="own OtoDock state"):
        _check(state, str(tmp_path / "home" / "Desktop" / "lnk" / "workspace" / "x.md"))


def test_the_state_refusal_runs_before_the_runtime_admission(state, monkeypatch):
    root = state / f"claude-{os.getuid()}"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(sm, "_claude_runtime_root", lambda: str(root))
    with pytest.raises(ValueError, match="own OtoDock state"):
        _check(state, str(root / "task.out"))


def test_a_sibling_of_the_state_root_stays_a_home_band_question(state, monkeypatch):
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: True)
    _check(state, str(state.parent / ".oto-dock-notes" / "a.md"))  # no raise


# ---------------------------------------------------------------------------
# Windows: no network or device path, the device prefix never hides a root,
# and the legacy <home>/.oto-dock is the machine's state too
# ---------------------------------------------------------------------------


@pytest.fixture
def windows(monkeypatch):
    from satellite import config
    monkeypatch.setattr(config, "HOST", config.ROWS[config.WINDOWS])


@pytest.mark.parametrize("raw", [
    "//?/C:/Users/e/OtoDock/satellite.conf",
    "\\\\?\\C:\\Users\\e\\OtoDock\\satellite.conf",
    "//./C:/Users/e/x.txt",
    "//server/share/x.txt",
    "\\\\localhost\\c$\\Users\\e\\OtoDock\\satellite.conf",
])
def test_a_network_or_device_path_is_refused_on_windows(windows, raw):
    with pytest.raises(ValueError, match="network or device"):
        sm._validate_satellite_host_path(raw)


@pytest.mark.parametrize("raw", [
    "C:/Users/e/x.txt", "C:\\Users\\e\\x.txt", "d:/data/report.pdf", "Z:\\share\\x.txt",
    # Root-relative (the satellite's current drive): the policy check after
    # realpath judges where it lands.
    "/Users/e/x.txt", "\\Users\\e\\x.txt",
])
def test_drive_and_root_relative_windows_paths_pass(windows, raw):
    sm._validate_satellite_host_path(raw)


def test_posix_paths_keep_their_rule(monkeypatch):
    from satellite import config
    monkeypatch.setattr(config, "HOST", config.ROWS[config.LINUX])
    sm._validate_satellite_host_path("/home/e/x.txt")


class _WinPath(str):
    """A Windows spelling that ``Path.resolve`` would not touch on this host."""

    def resolve(self):
        return self

    def __truediv__(self, other):
        return _WinPath(f"{self}/{other}")


def test_a_device_prefix_in_the_resolved_path_never_hides_the_state(windows, monkeypatch):
    """``realpath`` keeps (or adds, for a long path) the ``\\\\?\\`` prefix;
    the compare sees the plain spelling."""
    from satellite import config
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: True)
    monkeypatch.setattr(config, "otodock_dir", lambda: _WinPath("C:/Users/e/OtoDock"))
    real = os.path.realpath
    resolved = {"C:/Users/e/OtoDock/agents/a2/x.md": "\\\\?\\C:\\Users\\e\\OtoDock\\agents\\a2\\x.md",
                "C:/Users/e/OtoDock": "C:\\Users\\e\\OtoDock"}
    monkeypatch.setattr(sm.os.path, "realpath",
                        lambda p, *a, **k: resolved.get(str(p).replace("\\", "/"), real(p, *a, **k)))
    with pytest.raises(ValueError, match="own OtoDock state"):
        sm._check_satellite_host_policy("C:/Users/e/OtoDock/agents/a2/x.md")


def test_the_legacy_state_folder_is_refused_on_windows(windows, tmp_path, monkeypatch):
    """The browser profiles live under ``<home>/.oto-dock`` on Windows (the
    proxy refuses that folder too); the satellite refuses it itself."""
    from satellite import config
    from satellite.host import satellite_policy
    monkeypatch.setattr(satellite_policy, "is_full_fs_allowed", lambda: True)
    home = tmp_path / "winhome"
    (home / ".oto-dock" / "browser-profiles").mkdir(parents=True)
    monkeypatch.setattr(config, "otodock_dir", lambda: home / "OtoDock")
    monkeypatch.setattr(sm.Path, "home", classmethod(lambda cls: home))
    with pytest.raises(ValueError, match="own OtoDock state"):
        sm._check_satellite_host_policy(str(home / ".oto-dock" / "browser-profiles" / "p" / "Cookies"))
    sm._check_satellite_host_policy(str(home / ".oto-dock-notes" / "a.md"))  # a sibling: no raise
    monkeypatch.setattr(config, "HOST", config.ROWS[config.LINUX])
    sm._check_satellite_host_policy(str(home / ".oto-dock" / "x"))  # the extra root is Windows only
