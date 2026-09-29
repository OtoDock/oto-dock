"""``services.infra.path_confinement`` — the normalize-then-prefix barriers.

``resolve_under`` follows symlinks and confines the RESULT; ``join_under``
is lexical (never touches the filesystem) and refuses anything that lands on
or above the root; ``safe_agent_dir`` is ``config.get_agent_dir`` behind
that lexical gate.
"""

import os
from pathlib import Path

import pytest

import config
from services.infra.path_confinement import (
    PathOutsideRoot, join_under, normalize_rel_path, resolve_under, safe_agent_dir,
)


def test_resolve_under_accepts_root_and_children(tmp_path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    assert resolve_under(root, root) == root.resolve()
    # A not-yet-existing leaf resolves through its existing parents.
    assert resolve_under(root / "a" / "new.txt", root) == root.resolve() / "a" / "new.txt"


def test_resolve_under_refuses_dotdot_and_string_prefix_siblings(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "root-sibling").mkdir()
    with pytest.raises(PathOutsideRoot):
        resolve_under(root / ".." / "root-sibling", root)
    # Sharing the root's characters is not containment.
    with pytest.raises(PathOutsideRoot):
        resolve_under(tmp_path / "root-sibling", root)


def test_resolve_under_judges_symlinks_by_their_target(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "link")
    with pytest.raises(PathOutsideRoot):
        resolve_under(root / "link" / "x.txt", root)
    # A symlink that stays inside resolves to its target.
    (root / "real").mkdir()
    os.symlink(root / "real", root / "inner")
    assert resolve_under(root / "inner" / "f", root) == root.resolve() / "real" / "f"


def test_join_under_is_lexical_and_strictly_below(tmp_path):
    root = tmp_path / "root"  # never created — no filesystem access
    assert join_under(root, "a", "b.txt") == root / "a" / "b.txt"
    for parts in (("..",), ("",), (".",), ("a", ".."), ("/etc/passwd",), ("a/../..",)):
        with pytest.raises(PathOutsideRoot):
            join_under(root, *parts)


def test_join_under_does_not_follow_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(tmp_path, root / "up")
    # Lexically inside; a caller that must refuse symlinks uses resolve_under.
    assert join_under(root, "up", "x") == root / "up" / "x"


def test_safe_agent_dir_matches_config_and_refuses_escape(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    assert safe_agent_dir("my-agent") == config.get_agent_dir("my-agent")
    for bad in ("", "..", "../other", "/tmp/x", "a/../.."):
        with pytest.raises(PathOutsideRoot):
            safe_agent_dir(bad)


def test_normalize_rel_path_is_the_segment_guard():
    """The leading and trailing slashes go; an empty, ``.`` or ``..`` segment
    and a NUL are refused; backslashes and unicode are names (core-seams
    phase 3)."""
    assert normalize_rel_path("workspace/x") == "workspace/x"
    assert normalize_rel_path("/workspace/x/") == "workspace/x"
    assert normalize_rel_path("a\\b") == "a\\b"
    assert normalize_rel_path("．．/x") == "．．/x"
    for bad in ("", "/", ".", "..", "a/../b", "a//b", "./a", "a/.", "../a", "a/..", "a\x00b", None):
        with pytest.raises(PathOutsideRoot):
            normalize_rel_path(bad)
    # The canonical test a caller makes when a leading slash must be an error.
    assert normalize_rel_path("/workspace/x") != "/workspace/x"
    # Unlike join_under, an interior ``..`` is refused, not collapsed.
    assert join_under("/root", "a/../b") == Path("/root/b")
