"""Direct-LLM builtin file tools — the mount-table resolver + text operations
(core/layers/direct/files.py) and their parity with the bwrap mount table.

The resolver is the boundary for in-proxy tools (no bubblewrap underneath):
it consumes ``SandboxBuilder.workspace_mount_table`` — the same ordered
``Mount`` list ``_workspace_mounts`` renders as ``--bind`` / ``--ro-bind`` —
so a path the kernel would refuse for a CLI process is refused here too.
"""

from __future__ import annotations

import pytest

from core.layers.direct import files as df
from core.layers.direct.files import FileToolError
from core.sandbox.sandbox import Mount, SandboxBuilder, SandboxConfig

AGENT = "pa"


@pytest.fixture
def tree(tmp_path):
    agents = tmp_path / "agents"
    a = agents / AGENT
    for d in (
        "config/context", "workspace", "knowledge",
        "users/alice/workspace", "users/alice/context", "users/alice/.claude",
        "users/bob/workspace",
    ):
        (a / d).mkdir(parents=True)
    (a / "workspace" / "notes.md").write_text("line one\nline two\nline three\n")
    (a / "knowledge" / "guide.md").write_text("# Guide\n")
    mcps = tmp_path / "mcps"
    mcps.mkdir()
    return agents, mcps


def _cfg(tree, role="manager", username="alice", *, mount_shared=True,
         config_visible=None):
    agents, mcps = tree
    claude = agents / AGENT / (f"users/{username}/.claude" if username else "workspace/.claude")
    claude.mkdir(parents=True, exist_ok=True)
    return SandboxConfig(
        role=role,
        username=username,
        agent_name=AGENT,
        is_admin_agent=False,
        host_agents_dir=agents.resolve(),
        host_mcps_dir=mcps.resolve(),
        host_claude_dir=claude.resolve(),
        config_visible=config_visible,
        mount_shared=mount_shared,
        net_forwards=["8400"],
    )


def _resolve(cfg, raw, *, writing=False):
    return df.resolve(df.mount_table(cfg), raw, cwd=df.session_cwd(cfg), writing=writing)


def _rw(cfg) -> dict[str, bool]:
    return {m.sandbox: m.rw for m in df.mount_table(cfg)}


# ---------------------------------------------------------------------------
# Mount table ↔ bwrap parity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,username", [
    ("manager", "alice"), ("editor", "alice"), ("viewer", "alice"),
    ("manager", ""), ("viewer", ""),
])
def test_bwrap_args_are_rendered_from_the_mount_table(tree, role, username):
    builder = SandboxBuilder(_cfg(tree, role=role, username=username))
    mounts = builder.workspace_mount_table()
    assert mounts and all(isinstance(m, Mount) for m in mounts)
    rendered: list[str] = []
    for m in mounts:
        rendered += ["--bind" if m.rw else "--ro-bind", m.host, m.sandbox]
    assert builder._workspace_mounts() == rendered


def test_role_matrix(tree):
    # The agent's own CLI state is masked (empty, read-only) out of every
    # session that does not run from it.
    masked = {"/workspace/.claude": False, "/workspace/.codex": False}
    assert _rw(_cfg(tree, "manager", "alice")) == {
        "/users/alice": False, "/users/alice/workspace": True,
        "/users/alice/context": True, "/users/alice/.claude": True,
        "/config": True, "/knowledge": True, "/workspace": True, **masked,
    }
    assert _rw(_cfg(tree, "editor", "alice")) == {
        "/users/alice": False, "/users/alice/workspace": True,
        "/users/alice/context": True, "/users/alice/.claude": True,
        "/knowledge": False, "/workspace": True, **masked,
    }
    assert _rw(_cfg(tree, "viewer", "alice")) == {
        "/users/alice": False, "/users/alice/workspace": True,
        "/users/alice/context": True, "/users/alice/.claude": True,
        "/knowledge": False, "/workspace": False, **masked,
    }
    # Agent scope (service sessions / Shared-only chats): no user dir.
    assert _rw(_cfg(tree, "manager", "")) == {"/workspace": True, "/knowledge": False}
    # Below the editor tier a person never runs from the agent's state (the
    # engines refuse the session); anything run as them sees it masked.
    assert _rw(_cfg(tree, "viewer", "")) == {"/workspace": False, "/knowledge": False, **masked}
    assert not df.has_writable_mount(df.mount_table(_cfg(tree, "viewer", "")))
    # Personal-only: no shared roots at all.
    assert "/workspace" not in _rw(_cfg(tree, "manager", "alice", mount_shared=False))


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def test_absolute_virtual_paths_map_through_the_mount(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    r = _resolve(cfg, "/workspace/notes.md")
    assert r.host == agents.resolve() / AGENT / "workspace" / "notes.md"
    assert r.mount.sandbox == "/workspace" and r.virtual == "/workspace/notes.md"
    assert _resolve(cfg, "/knowledge/guide.md").mount.sandbox == "/knowledge"
    assert _resolve(cfg, "/config/agent.md", writing=True).mount.sandbox == "/config"


def test_relative_paths_anchor_on_the_session_cwd(tree):
    cfg = _cfg(tree)
    r = _resolve(cfg, "workspace/todo.md", writing=True)
    assert r.virtual == "/users/alice/workspace/todo.md" and r.mount.rw
    # The user dir ROOT is read-only (RO root + RW subdirs, like bwrap).
    with pytest.raises(FileToolError, match="read-only"):
        _resolve(cfg, "stray.md", writing=True)
    assert _resolve(cfg, "stray.md").virtual == "/users/alice/stray.md"
    agent_scope = _cfg(tree, username="")
    assert _resolve(agent_scope, "a.md").virtual == "/workspace/a.md"


def test_dotdot_collapses_before_matching(tree):
    editor = _cfg(tree, role="editor")
    with pytest.raises(FileToolError, match="not inside"):
        _resolve(editor, "/workspace/../config/agent.md")
    assert _resolve(editor, "/workspace/sub/../notes.md").virtual == "/workspace/notes.md"


def test_runtime_and_secret_subtrees_are_refused(tree):
    cfg = _cfg(tree)
    for p in (
        "/knowledge/.credentials/token.json",
        "/users/alice/.claude/settings.json",
        "/workspace/.git/config",
        "/workspace/repo/.codex/config.toml",
    ):
        with pytest.raises(FileToolError, match="not accessible"):
            _resolve(cfg, p)


def test_other_users_and_unmounted_roots(tree):
    cfg = _cfg(tree)
    with pytest.raises(FileToolError, match="not inside"):
        _resolve(cfg, "/users/bob/workspace/x.md")
    with pytest.raises(FileToolError, match="not inside"):
        _resolve(cfg, "/etc/passwd")
    with pytest.raises(FileToolError, match="not a session folder"):
        _resolve(cfg, "~/x.md")
    with pytest.raises(FileToolError, match="required"):
        _resolve(cfg, "")


def test_read_only_mounts_refuse_writes(tree):
    editor = _cfg(tree, role="editor")
    with pytest.raises(FileToolError, match="read-only"):
        _resolve(editor, "/knowledge/new.md", writing=True)
    assert _resolve(editor, "/workspace/new.md", writing=True).mount.rw
    # A viewer service session has no writable root at all.
    viewer_agent = _cfg(tree, role="viewer", username="")
    with pytest.raises(FileToolError, match="no writable location"):
        _resolve(viewer_agent, "/workspace/x.md", writing=True)


def test_personal_only_has_no_shared_roots(tree):
    cfg = _cfg(tree, mount_shared=False)
    with pytest.raises(FileToolError, match="not inside"):
        _resolve(cfg, "/workspace/notes.md")
    assert _resolve(cfg, "/users/alice/workspace/x.md", writing=True).mount.rw


def test_symlink_escape_is_refused(tree, tmp_path):
    agents, _ = tree
    cfg = _cfg(tree)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (agents / AGENT / "workspace" / "link").symlink_to(outside)
    with pytest.raises(FileToolError, match="symlink"):
        _resolve(cfg, "/workspace/link/secret.txt")
    # A write whose deepest EXISTING ancestor is the escaping link.
    with pytest.raises(FileToolError, match="symlink"):
        _resolve(cfg, "/workspace/link/new/deep.txt", writing=True)


def test_symlink_inside_the_mount_is_fine(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    (ws / "alias.md").symlink_to(ws / "notes.md")
    assert "line one" in df.read_numbered(_resolve(cfg, "/workspace/alias.md"))


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def test_read_numbered_shape_and_window(tree):
    r = _resolve(_cfg(tree), "/workspace/notes.md")
    out = df.read_numbered(r)
    assert out.splitlines() == ["     1\tline one", "     2\tline two", "     3\tline three"]
    assert df.read_numbered(r, offset=2, limit=1) == (
        "     2\tline two\n... (1 more lines — continue with offset=3)"
    )
    assert "no lines at offset 9" in df.read_numbered(r, offset=9)


def test_read_refuses_binary_folders_and_missing(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    (agents / AGENT / "workspace" / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(FileToolError, match="binary"):
        df.read_numbered(_resolve(cfg, "/workspace/blob.bin"))
    with pytest.raises(FileToolError, match="folder"):
        df.read_numbered(_resolve(cfg, "/workspace"))
    with pytest.raises(FileToolError, match="not found"):
        df.read_numbered(_resolve(cfg, "/workspace/nope.md"))
    (agents / AGENT / "workspace" / "empty.md").write_text("")
    assert df.read_numbered(_resolve(cfg, "/workspace/empty.md")) == "(empty file)"


def test_read_byte_cap(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    big = agents / AGENT / "workspace" / "big.txt"
    big.write_text("x" * 100 + "\n" * 1 + "y" * (df.READ_MAX_BYTES + 10))
    out = df.read_numbered(_resolve(cfg, "/workspace/big.txt"))
    assert "read stopped at 256 KB" in out


def test_glob_matches_skips_runtime_dirs_and_sorts(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    (ws / "docs").mkdir()
    (ws / "docs" / "a.md").write_text("a")
    (ws / "docs" / "b.txt").write_text("b")
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "x.md").write_text("x")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD.md").write_text("h")
    r = _resolve(cfg, "/workspace")
    assert df.glob_paths(r, "**/*.md") == ["/workspace/docs/a.md", "/workspace/notes.md"]
    assert df.glob_paths(r, "*.md") == ["/workspace/notes.md"]
    assert df.glob_paths(r, "docs/*") == ["/workspace/docs/a.md", "/workspace/docs/b.txt"]
    assert df.glob_paths(r, "nothing/*") == []
    with pytest.raises(FileToolError, match="relative"):
        df.glob_paths(r, "../*")
    with pytest.raises(FileToolError, match="not a folder"):
        df.glob_paths(_resolve(cfg, "/workspace/notes.md"), "*")


def test_glob_caps_at_200_entries(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    many = agents / AGENT / "workspace" / "many"
    many.mkdir()
    for i in range(250):
        (many / f"f{i:03d}.txt").write_text("")
    out = df.glob_paths(_resolve(cfg, "/workspace/many"), "*.txt")
    assert len(out) == df.GLOB_MAX_ENTRIES and out == sorted(out)


def test_write_edit_roundtrip_and_caps(tree):
    cfg = _cfg(tree)
    r = _resolve(cfg, "/workspace/new/deep.md", writing=True)
    assert df.write_text(r, "hello\nworld\n") == 12
    assert r.host.read_text() == "hello\nworld\n"
    assert df.edit_text(r, "world", "there") == 1
    assert r.host.read_text() == "hello\nthere\n"
    with pytest.raises(FileToolError, match="not found"):
        df.edit_text(r, "zzz", "y")
    df.write_text(r, "a a a\n")
    with pytest.raises(FileToolError, match="appears 3 times"):
        df.edit_text(r, "a", "b")
    assert df.edit_text(r, "a", "b", replace_all=True) == 3
    with pytest.raises(FileToolError, match="identical"):
        df.edit_text(r, "b", "b")
    with pytest.raises(FileToolError, match="required"):
        df.edit_text(r, "", "b")
    with pytest.raises(FileToolError, match="up to 1024 KB"):
        df.write_text(r, "x" * (df.WRITE_MAX_BYTES + 1))
    with pytest.raises(FileToolError, match="folder"):
        df.write_text(_resolve(cfg, "/workspace/new", writing=True), "x")


# ---------------------------------------------------------------------------
# Symlinked leaves and the strict opens
# ---------------------------------------------------------------------------

def test_dangling_leaf_symlink_write_refused(tree, tmp_path):
    """The PoC's shape: a dangling link in /workspace to a file that does
    not exist yet outside the mount. The write is refused and the outside
    file is never created."""
    agents, _ = tree
    cfg = _cfg(tree)
    outside = tmp_path / "OUTSIDE"
    outside.mkdir()
    target = outside / "planted.txt"
    (agents / AGENT / "workspace" / "escape").symlink_to(target)
    with pytest.raises(FileToolError, match="symlink"):
        _resolve(cfg, "/workspace/escape", writing=True)
    assert not target.exists()


def test_existing_leaf_symlink_write_refused(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    (ws / "alias.md").symlink_to(ws / "notes.md")
    with pytest.raises(FileToolError, match="symlink"):
        _resolve(cfg, "/workspace/alias.md", writing=True)
    assert (ws / "notes.md").read_text() == "line one\nline two\nline three\n"
    # A link in the middle of a write path is refused as well.
    (ws / "sub").mkdir()
    (ws / "alias-dir").symlink_to(ws / "sub")
    with pytest.raises(FileToolError, match="symlink"):
        df.write_text(_resolve(cfg, "/workspace/alias-dir/new.md", writing=True), "x")
    assert not (ws / "sub" / "new.md").exists()


def test_read_of_swapped_alias_is_refused(tree, tmp_path):
    """An in-mount alias is readable; once its target is swapped out of the
    mount after the resolve, the read is refused at the open."""
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    (ws / "alias.md").symlink_to(ws / "notes.md")
    r = _resolve(cfg, "/workspace/alias.md")
    assert "line one" in df.read_numbered(r)
    secret = tmp_path / "config.env"
    secret.write_text("JWT_SECRET=1\n")
    (ws / "alias.md").unlink()
    (ws / "alias.md").symlink_to(secret)
    with pytest.raises(FileToolError):
        df.read_numbered(r)
    # A regular file swapped for a link after the resolve, the same.
    r = _resolve(cfg, "/workspace/notes.md")
    (ws / "notes.md").unlink()
    (ws / "notes.md").symlink_to(secret)
    with pytest.raises(FileToolError):
        df.read_numbered(r)


def test_edit_never_writes_through_a_link(tree, tmp_path):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    outside = tmp_path / "outside.md"
    outside.write_text("keep me")
    r = _resolve(cfg, "/workspace/notes.md", writing=True)
    (ws / "notes.md").unlink()
    (ws / "notes.md").symlink_to(outside)
    with pytest.raises(FileToolError):
        df.edit_text(r, "keep", "gone")
    with pytest.raises(FileToolError):
        df.write_text(r, "overwritten")
    assert outside.read_text() == "keep me"


def test_write_under_a_moved_mount_root_is_refused(tree):
    """The mount table trusts its host paths by name (the builder verified
    them when it was made); a folder swapped for a link afterwards is
    caught at the mount root instead of followed."""
    agents, _ = tree
    cfg = _cfg(tree)
    mounts = df.mount_table(cfg)
    cwd = df.session_cwd(cfg)
    ws = agents / AGENT / "workspace"
    ws.rename(ws.with_name("workspace-real"))
    ws.symlink_to("workspace-real")
    with pytest.raises(FileToolError, match="moved"):
        df.resolve(mounts, "/workspace/x.md", cwd=cwd, writing=True)
    assert not (agents / AGENT / "workspace-real" / "x.md").exists()
