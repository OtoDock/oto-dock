"""``services.infra.path_confinement`` — the normalize-then-prefix barriers.

``resolve_under`` follows symlinks and confines the RESULT; ``join_under``
is lexical (never touches the filesystem) and refuses anything that lands on
or above the root; ``config.get_agent_dir`` joins an agent name behind that
lexical gate.
"""

import os
from pathlib import Path

import pytest

import config
from services.infra.path_confinement import (
    PathOutsideRoot,
    join_under,
    normalize_rel_path,
    resolve_under,
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


def test_get_agent_dir_joins_a_slug_and_refuses_anything_else(monkeypatch, tmp_path):
    """Every slug a released install can have joins as ``AGENTS_DIR / name``
    always did (no shape rule beyond one segment); an empty, absolute,
    multi-segment or escaping name is refused before any path is built."""
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    for slug in ("my-agent", "My_Agent.v2", "x" * 90, "agent.."):
        assert config.get_agent_dir(slug) == tmp_path / "agents" / slug
    for bad in ("", ".", "..", "../other", "/tmp/x", "a/../..", "a/b", "a\\b", "x/.."):
        with pytest.raises(PathOutsideRoot):
            config.get_agent_dir(bad)


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


def test_a_name_that_cannot_be_an_agent_folder_answers_404_not_500():
    """An admin passes ``require_agent_access`` for any string; a name the
    barrier refuses then reaches no folder and the app answers 404."""
    from fastapi.testclient import TestClient

    from app import app
    from auth import roles
    from auth.providers import UserContext, get_current_user
    admin = UserContext(sub="user-admin", email="a@t.com", name="A", role=roles.ADMIN, agents=[])
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        client = TestClient(app)
        for bad in ("../x", "..", "a/b", ""):
            r = client.post("/v1/media/token", json={"agent": bad, "path": "workspace/a.mp4"})
            assert r.status_code == 404, (bad, r.status_code, r.text)
            assert r.json()["detail"] == "Agent not found"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
