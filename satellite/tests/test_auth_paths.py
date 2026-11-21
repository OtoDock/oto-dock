"""Unit tests for the central allowlist (satellite/host/auth_paths.py).

Covers the rules that protect against compromised-proxy attempts to write
or read outside the satellite's authorized roots.
"""

import os

# Add satellite parent dir to sys.path so we can import as a package.
import sys
from pathlib import Path

import pytest

_SAT_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_SAT_PARENT) not in sys.path:
    sys.path.insert(0, str(_SAT_PARENT))

from satellite.host import auth_paths


def test_is_safe_slug_accepts_plain():
    assert auth_paths.is_safe_slug("my-agent")
    assert auth_paths.is_safe_slug("AgentName")
    assert auth_paths.is_safe_slug("agent_123")
    assert auth_paths.is_safe_slug("a")  # single char ok


def test_is_safe_slug_rejects_traversal_and_separators():
    assert not auth_paths.is_safe_slug("")
    assert not auth_paths.is_safe_slug("..")
    assert not auth_paths.is_safe_slug("../etc")
    assert not auth_paths.is_safe_slug("a/b")
    assert not auth_paths.is_safe_slug("a\\b")
    assert not auth_paths.is_safe_slug(".")
    assert not auth_paths.is_safe_slug(".hidden")
    assert not auth_paths.is_safe_slug("a b")  # space
    assert not auth_paths.is_safe_slug("a:b")  # colon


def test_is_authorized_relative_path_accepts_clean():
    assert auth_paths.is_authorized_relative_path("workspace/notes.md")
    assert auth_paths.is_authorized_relative_path("users/alice/file.txt")
    assert auth_paths.is_authorized_relative_path("file.txt")
    assert auth_paths.is_authorized_relative_path("a/b/c/d.bin")


def test_is_authorized_relative_path_rejects_traversal():
    assert not auth_paths.is_authorized_relative_path("")
    assert not auth_paths.is_authorized_relative_path("..")
    assert not auth_paths.is_authorized_relative_path("../etc/passwd")
    assert not auth_paths.is_authorized_relative_path("workspace/../../../etc")
    assert not auth_paths.is_authorized_relative_path("/etc/passwd")
    assert not auth_paths.is_authorized_relative_path("file\0name")


def test_assert_agent_path_safe_returns_resolved_target(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "alpha").mkdir()
    target = auth_paths.assert_agent_path_safe(
        "workspace/foo.txt", "alpha", agents,
    )
    expected = (agents / "alpha" / "workspace" / "foo.txt").resolve()
    assert target == expected


def test_assert_agent_path_safe_rejects_bad_slug(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    with pytest.raises(ValueError, match="unsafe agent_slug"):
        auth_paths.assert_agent_path_safe("foo.txt", "..", agents)
    with pytest.raises(ValueError, match="unsafe agent_slug"):
        auth_paths.assert_agent_path_safe("foo.txt", "", agents)
    with pytest.raises(ValueError, match="unsafe agent_slug"):
        auth_paths.assert_agent_path_safe("foo.txt", "a/b", agents)


def test_assert_agent_path_safe_rejects_path_traversal(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    with pytest.raises(ValueError, match="unsafe rel_path"):
        auth_paths.assert_agent_path_safe("../../etc/passwd", "alpha", agents)
    with pytest.raises(ValueError, match="unsafe rel_path"):
        auth_paths.assert_agent_path_safe("/etc/passwd", "alpha", agents)
    with pytest.raises(ValueError, match="unsafe rel_path"):
        auth_paths.assert_agent_path_safe("", "alpha", agents)


def test_assert_agent_path_safe_rejects_symlink_escape(tmp_path):
    """If a symlink inside the agent dir points outside, the resolved
    target is outside; we must reject."""
    agents = tmp_path / "agents"
    (agents / "alpha").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("hush")
    # Symlink inside agent dir → outside.
    link = agents / "alpha" / "escape"
    os.symlink(str(outside), str(link))
    with pytest.raises(ValueError, match="target escapes agent_dir"):
        auth_paths.assert_agent_path_safe("escape/secret.txt", "alpha", agents)


def test_is_path_under_root(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    inside = root / "alpha" / "file.txt"
    inside.parent.mkdir(parents=True)
    inside.write_text("hi")
    outside = tmp_path / "other"
    outside.mkdir()
    assert auth_paths.is_path_under_root(inside, root)
    assert not auth_paths.is_path_under_root(outside / "x.txt", root)


def test_is_path_under_root_is_the_tar_and_frame_containment(tmp_path):
    """The shapes the sync_mcps and self-update extractors hand it
    (core-seams phase 3): a ``..`` member, an absolute member
    (pathlib lets it win the join) and a sibling whose name extends the root
    (the separator-less ``startswith(str(root))`` admitted it) are outside;
    a member below the root and the root itself are inside."""
    root = tmp_path / "pkg.new"
    root.mkdir()
    (tmp_path / "pkg.newevil").mkdir()
    assert auth_paths.is_path_under_root(root / "manifest.json", root)
    assert auth_paths.is_path_under_root(root / "a" / "b.py", root)
    assert auth_paths.is_path_under_root(root, root)
    assert not auth_paths.is_path_under_root(root / ".." / "pkg.newevil" / "x", root)
    assert not auth_paths.is_path_under_root(root / "/etc/passwd", root)
    assert not auth_paths.is_path_under_root(tmp_path / "pkg.newevil" / "x", root)
    assert not auth_paths.is_path_under_root(root / "a\x00b", root)


def test_is_authorized_relative_path_is_the_credentials_update_rule():
    """``credentials_update`` hands it the slash-normalised ``dir_relative``."""
    assert auth_paths.is_authorized_relative_path("users/alice/.claude")
    assert not auth_paths.is_authorized_relative_path("users/../.claude")
    assert not auth_paths.is_authorized_relative_path("/users/alice/.claude")
    assert not auth_paths.is_authorized_relative_path("users/a\x00b/.claude")


def test_agent_rel_is_the_checked_slug_prefixed_rel():
    from satellite.host.auth_paths import agent_rel
    assert agent_rel("workspace/x.txt", "a1") == "a1/workspace/x.txt"
    assert agent_rel("./workspace//x.txt", "a1") == "a1/workspace/x.txt"
    for bad in (("../x", "a1"), ("/abs", "a1"), ("workspace/x", "../a1"), ("", "a1"),
                ("workspace/x\x00", "a1"), (".", "a1")):
        with pytest.raises(ValueError):
            agent_rel(*bad)


# --- unpack_tarball: the update's and a tool's package ------------------------

def _archive(*members):
    """An open archive of ``(TarInfo, bytes or None)`` pairs."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for info, data in members:
            if data is not None:
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


def _info(name, kind=None, mode=0o644, linkname=""):
    import tarfile
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.linkname = kind or tarfile.REGTYPE, mode, linkname
    return info


def _good():
    import tarfile
    return (_info("pkg", tarfile.DIRTYPE, 0o775), None), \
        (_info("pkg/a.py", mode=0o664), b"a"), (_info("pkg/run.sh", mode=0o4775), b"run")


@pytest.mark.parametrize("filtered", [True, False])
def test_unpack_tarball_unpacks_a_good_archive(tmp_path, request, filtered):
    if not filtered:
        request.getfixturevalue("python_without_tar_filter")
    dest = tmp_path / "new"
    dest.mkdir()
    with _archive(*_good()) as tar:
        auth_paths.unpack_tarball(tar, dest)
    assert (dest / "pkg" / "a.py").read_bytes() == b"a"
    assert (dest / "pkg" / "run.sh").read_bytes() == b"run"
    if os.name == "posix":
        # The data filter's modes: no set-id bit, no write for others.
        assert (dest / "pkg" / "a.py").stat().st_mode & 0o7777 == 0o644
        assert (dest / "pkg" / "run.sh").stat().st_mode & 0o7777 == 0o755


def _bad_members():
    import tarfile
    return {
        "a link": _info("pkg/out", tarfile.SYMTYPE, linkname="../../etc"),
        "a link inside": _info("pkg/in", tarfile.SYMTYPE, linkname="a.py"),
        "a hard link": _info("pkg/hard", tarfile.LNKTYPE, linkname="pkg/a.py"),
        "a device": _info("pkg/dev", tarfile.CHRTYPE),
        "a pipe": _info("pkg/fifo", tarfile.FIFOTYPE),
        "an absolute name": _info("/etc/cron.d/x"),
        "a drive": _info("C:/Windows/x"),
        "a drive-relative name": _info("C:x"),
        "a share": _info("\\\\server\\share\\x"),
        "a rooted Windows name": _info("\\Windows\\x"),
        "a name that climbs out": _info("../x"),
        "a name that climbs out by backslashes": _info("pkg\\..\\..\\x"),
        "a name with .. inside": _info("pkg/../pkg/b.py"),
    }


@pytest.mark.parametrize("kind", sorted(_bad_members()))
def test_unpack_tarball_without_the_filter_refuses_before_writing(tmp_path, python_without_tar_filter, kind):
    dest = tmp_path / "new"
    dest.mkdir()
    bad = _bad_members()[kind]
    data = b"x" if bad.isreg() else None
    with _archive(*_good(), (bad, data)) as tar, pytest.raises(ValueError):
        auth_paths.unpack_tarball(tar, dest)
    assert list(dest.iterdir()) == []
    assert not (tmp_path / "x").exists()
