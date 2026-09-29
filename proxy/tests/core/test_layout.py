"""The agent tree leaf — ``core/layout.py`` — and its two mirrors: the
byte copy the satellite runs (``satellite/_vendored/layout.py``, hash-pinned
in ``satellite/config.py``) and the dashboard's ``lib/layout/tree.ts`` (read
here by regex, the way ``tests/remote/test_placement.py`` reads its mirror).
Core-seams phase 10."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from core import layout
from tests._paths import REPO_ROOT

_VENDORED = REPO_ROOT / "satellite" / "_vendored" / "layout.py"
_SAT_CONFIG = REPO_ROOT / "satellite" / "config.py"
_MIRROR = REPO_ROOT / "dashboard" / "src" / "lib" / "layout" / "tree.ts"


# ---------------------------------------------------------------------------
# the words
# ---------------------------------------------------------------------------

def test_the_words_are_the_frozen_folder_names():
    assert (layout.USERS, layout.WORKSPACE, layout.KNOWLEDGE, layout.CONFIG, layout.CONTEXT) == (
        "users", "workspace", "knowledge", "config", "context")
    assert layout.CREDENTIALS_DIR == ".credentials"
    assert layout.HEADS == ("users", "workspace", "knowledge", "config")
    assert layout.FILE_HEADS == ("workspace", "knowledge", "users")
    assert layout.USER_SUBDIRS == ("workspace", "context")
    assert layout.TREE_ROOTS == ("/users", "/workspace", "/knowledge", "/config")
    assert [r[1:] for r in layout.TREE_ROOTS] == list(layout.HEADS)


def test_the_leaf_never_spells_an_engine_dir():
    """The engines' config dirs are the engine axis's words — the leaf
    composes them from the name a caller hands in, never from a literal."""
    import ast
    tree = ast.parse(Path(layout.__file__).read_text(encoding="utf-8"))
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value in (".claude", ".codex")}
    docstrings = {ast.get_docstring(n) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))}
    assert not literals or all(any(lit in (d or "") for d in docstrings) for lit in literals), literals


# ---------------------------------------------------------------------------
# the relative and virtual spellings
# ---------------------------------------------------------------------------

def test_relative_spellings():
    assert layout.user_rel("alice") == "users/alice"
    assert layout.scope_root("alice") == "users/alice"
    assert layout.scope_root("") == "workspace"
    assert layout.scope_workspace("alice") == "users/alice/workspace"
    assert layout.scope_workspace("") == "workspace"


def test_virtual_spellings():
    assert layout.virtual_user_root("alice") == "/users/alice"
    assert layout.virtual_workspace("alice") == "/users/alice/workspace"
    assert layout.virtual_workspace("") == "/workspace"


def test_host_compositions(tmp_path):
    a = tmp_path / "agents" / "pa"
    assert layout.user_dir(a, "alice") == a / "users" / "alice"
    assert layout.workspace_dir(a, "alice") == a / "users" / "alice" / "workspace"
    assert layout.workspace_dir(a, "") == a / "workspace"
    assert layout.context_dir(a, "alice") == a / "users" / "alice" / "context"
    assert layout.state_dir(a, "alice", ".claude") == a / "users" / "alice" / ".claude"
    assert layout.state_dir(a, "", ".codex") == a / "workspace" / ".codex"


# ---------------------------------------------------------------------------
# the questions — exactly the sites' rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel, head", [
    ("users", "users"), ("users/", "users"), ("users/alice/x", "users"),
    ("usersx/a", ""), ("/users/a", ""), ("workspace", "workspace"),
    ("workspace/a.md", "workspace"), ("knowledge/refs", "knowledge"),
    ("config/agent.md", "config"), ("context/x", ""), ("", ""), ("a\\b", ""),
])
def test_head_of(rel, head):
    assert layout.head_of(rel) == head


@pytest.mark.parametrize("rel, personal", [
    ("users/alice", True), ("users/alice/workspace", True), ("users/", True),
    ("users", False), ("usersx/a", False), ("/users/a", False), ("workspace/a", False),
])
def test_is_personal(rel, personal):
    assert layout.is_personal(rel) is personal


@pytest.mark.parametrize("rel, user", [
    ("users/alice", "alice"), ("users/alice/sub", "alice"), ("/users/alice", "alice"),
    ("users/alice/", "alice"), ("users", ""), ("users/", ""), ("users//x", ""),
    ("workspace", ""), ("", ""), ("foo/bar", ""),
])
def test_user_of(rel, user):
    assert layout.user_of(rel) == user


@pytest.mark.parametrize("path, root, ok", [
    ("/workspace", "/workspace", True), ("/workspace/x", "/workspace", True),
    ("/workspaces", "/workspace", False), ("/workspace2/x", "/workspace", False),
    ("/config/", "/config", True), ("/users", "/users", True), ("/users/a", "/users", True),
    ("", "/users", False),
])
def test_under(path, root, ok):
    assert layout.under(path, root) is ok


# ---------------------------------------------------------------------------
# the satellite's translation — the cases the translator's suite held
# ---------------------------------------------------------------------------

STATE = (".claude", ".codex")


@pytest.fixture
def agent_dir(tmp_path):
    return tmp_path / "agents" / "my-agent"


@pytest.mark.parametrize("value, username, expect", [
    ("/users/alice/workspace", "alice", "{a}/users/alice/workspace"),
    ("/users/alice/workspace/foo.png", "alice", "{a}/users/alice/workspace/foo.png"),
    ("/workspace", "", "{a}/workspace"),
    ("/workspace/.screenshots/sid-xyz/foo.png", "", "{a}/workspace/.screenshots/sid-xyz/foo.png"),
    ("/config", "alice", "{a}/config"),
    ("/config/prompt.md", "alice", "{a}/config/prompt.md"),
    ("/knowledge", "alice", "{a}/knowledge"),
    ("/knowledge", "", "{a}/knowledge"),
    ("/knowledge/refs/template.md", "alice", "{a}/knowledge/refs/template.md"),
    ("/knowledge/.credentials/google-tokens", "", "{a}/knowledge/.credentials/google-tokens"),
    ("/.claude/settings.json", "alice", "{a}/users/alice/.claude/settings.json"),
    ("/.claude/settings.json", "", "{a}/workspace/.claude/settings.json"),
    ("/.codex/config.toml", "alice", "{a}/users/alice/.codex/config.toml"),
    ("/.codex/config.toml", "", "{a}/workspace/.codex/config.toml"),
    ("/.claude", "alice", "{a}/users/alice/.claude"),
    ("/users", "alice", "{a}/users"),
    ("/workspace", "alice", "{a}/workspace"),
    ("/config/", "alice", "{a}/config/"),
])
def test_host_of_virtual_translates(agent_dir, value, username, expect):
    assert layout.host_of_virtual(value, agent_dir, username, STATE) == expect.format(a=agent_dir)


@pytest.mark.parametrize("value", [
    "https://example.com/foo", "http://100.64.5.10:8400", "some-api-key-XXXX", "",
    "/tmp/scratch.txt", "users/alice/workspace", "/workspaces/x", "/.claudex/y", "/configs",
])
def test_host_of_virtual_passes_through(agent_dir, value):
    assert layout.host_of_virtual(value, agent_dir, "alice", STATE) == value


def test_host_of_virtual_without_state_dirs_leaves_them_alone(agent_dir):
    assert layout.host_of_virtual("/.claude/x", agent_dir, "alice") == "/.claude/x"


def test_host_of_virtual_keeps_the_base_as_a_string(tmp_path):
    # the satellite's rule: string concatenation, the trailing slash of the
    # base dropped, the value's slashes kept as they came
    base = tmp_path / "a"
    assert layout.host_of_virtual("/workspace/x", Path(str(base) + "/"), "", STATE) == f"{base}/workspace/x"


# ---------------------------------------------------------------------------
# the twin the mount table and the write policy share
# ---------------------------------------------------------------------------

def test_the_writable_user_subdirs_are_the_leaf_s_and_credentials_stay_out():
    from auth import path_policy
    assert path_policy._USER_DIR_WRITABLE_SUBDIRS[:2] == layout.USER_SUBDIRS
    assert layout.CREDENTIALS_DIR not in path_policy._USER_DIR_WRITABLE_SUBDIRS
    assert path_policy._EXTERNAL_WRITABLE_SUBDIRS == layout.USER_SUBDIRS


# ---------------------------------------------------------------------------
# the vendored copy and the dashboard mirror
# ---------------------------------------------------------------------------

def test_the_vendored_copy_is_byte_identical_and_its_hash_current():
    src = Path(layout.__file__).read_bytes()
    assert _VENDORED.read_bytes() == src
    digest = hashlib.sha256(src).hexdigest()
    m = re.search(r'^SHARED_LAYOUT_HASH = "([0-9a-f]{64})"$', _SAT_CONFIG.read_text(encoding="utf-8"), re.M)
    assert m and m.group(1) == digest, "run scripts/sync-satellite-code.sh"
    assert layout.self_hash() == digest


def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, name
    return re.findall(r"'([^']*)'", m.group(1))


def test_the_dashboard_mirror_is_in_lock_step():
    text = _MIRROR.read_text(encoding="utf-8")
    assert _ts_const_strings(text, "HEAD") == [
        layout.USERS, layout.WORKSPACE, layout.KNOWLEDGE, layout.CONFIG, layout.CONTEXT]
    # the derived tuples reference HEAD.* — check their member names
    for name, py in (("HEADS", layout.HEADS), ("FILE_HEADS", layout.FILE_HEADS),
                     ("USER_SUBDIRS", layout.USER_SUBDIRS)):
        m = re.search(rf"export const {name} = \[(.+?)\] as const", text)
        assert m, name
        members = [x.strip().split(".")[1].lower() for x in m.group(1).split(",")]
        assert members == list(py), name
