"""Unit tests for the central allowlist (satellite/host/auth_paths.py).

Covers the rules that protect against compromised-proxy attempts to write
or read outside the satellite's authorized roots.
"""

import os
from pathlib import Path

import pytest

# Add satellite parent dir to sys.path so we can import as a package.
import sys
_SAT_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_SAT_PARENT) not in sys.path:
    sys.path.insert(0, str(_SAT_PARENT))

from satellite.host import auth_paths  # noqa: E402


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
