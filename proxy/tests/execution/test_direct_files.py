"""Direct-LLM builtin file tools: the platform's half (core/layers/direct/
files.py), its mount table's parity with the bwrap mount table, and the
honest files and the link cases of every tool.

Every file tool runs in the session's sandbox, in the file program. The
platform's text check reads ``SandboxBuilder.workspace_mount_table``, the
same ordered ``Mount`` list a machine renders as ``--bind`` / ``--ro-bind``
from the session's description, so a path the kernel would refuse is refused
here first, with its sentence.

Here the tools go the handler's way with the program run in this process on
the session's rows (``direct_program``): the text check, the program's own
rules on real files, the second look at a landing. What only the kernel's
mounts show (a mask inside a walk, a folder that is not in the sandbox, a
write under a read-only mount) is ``test_direct_files_sandboxed.py``'s.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import NamedTuple

import pytest

import config
from core.layers.direct import files as df
from core.layers.direct.files import FileToolError
from core.sandbox.sandbox import Mount, SandboxBuilder, SandboxConfig, empty_mount_dir
from tests.execution import direct_program

AGENT = "pa"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
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
         config_visible=None, libraries=()):
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
        knowledge_libraries=list(libraries),
        net_forwards=["8400"],
    )


class _Located(NamedTuple):
    virtual: str
    mount: Mount


def _resolve(cfg, raw, *, writing=False, mounts=None):
    """The text check of a path, as every handler makes it first."""
    vpath, mount = df.locate(mounts if mounts is not None else df.mount_table(cfg), raw,
                             cwd=df.session_cwd(cfg), writing=writing)
    return _Located(vpath, mount)


def _rw(cfg) -> dict[str, bool]:
    return {m.sandbox: m.rw for m in df.mount_table(cfg)}


class _Session:
    """The slice of a session the file program reads."""

    def __init__(self, cfg, mounts=None):
        self.sandbox_cfg = cfg
        self._mounts = mounts

    def mount_table(self):
        if self._mounts is None:
            self._mounts = df.mount_table(self.sandbox_cfg)
        return self._mounts


def _program(cfg, mounts):
    """A session's file program, run in this process on the session's rows,
    and the second look at a landing on the text."""
    session = _Session(cfg, mounts)

    async def call(operation, args, then=None):
        answer = direct_program.run(session, operation, args)
        if then is not None:
            await then(answer)
        return answer

    def judge(asked, landed):
        df.landing(session.mount_table(), asked, landed)

    return session, call, judge


def _read(cfg, raw, *, mounts=None, offset=None, limit=None, cwd=None) -> str:
    """A read as the handler makes it: the text check, the file program, the
    second look at a place a link lands."""
    session, call, judge = _program(cfg, mounts)
    vpath, _row = df.locate(session.mount_table(), raw, cwd=cwd or df.session_cwd(cfg))
    return asyncio.run(df.read(call, vpath, offset, limit, judge=judge))


async def _no_record(answer):
    return None


def _write(cfg, raw, content, *, mounts=None) -> int:
    """A write as the handler makes it: the text check for a write, then the file program."""
    session, call, _judge = _program(cfg, mounts)
    vpath, _row = df.locate(session.mount_table(), raw, cwd=df.session_cwd(cfg), writing=True)
    return asyncio.run(df.write(call, vpath, content, then=_no_record))[1]


def _edit(cfg, raw, old, new, replace_all=False, *, mounts=None) -> int:
    """An edit as the handler makes it."""
    session, call, _judge = _program(cfg, mounts)
    vpath, _row = df.locate(session.mount_table(), raw, cwd=df.session_cwd(cfg), writing=True)
    return asyncio.run(df.edit(call, vpath, old, new, replace_all, then=_no_record))


def _glob(cfg, raw, pattern, *, mounts=None, cwd=None) -> list[str]:
    """A search as the handler makes it."""
    session, call, judge = _program(cfg, mounts)
    vpath, _row = df.locate(session.mount_table(), raw, cwd=cwd or df.session_cwd(cfg))
    return asyncio.run(df.glob(call, vpath, pattern, judge=judge)).paths


# ---------------------------------------------------------------------------
# Mount table ↔ bwrap parity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,username", [
    ("manager", "alice"), ("editor", "alice"), ("viewer", "alice"),
    ("manager", ""), ("viewer", ""),
])
def test_bwrap_args_are_rendered_from_the_mount_table(tree, role, username):
    """What a machine renders for a CLI process is this table, entry for
    entry and in its order."""
    from tests.execution.test_sandbox import _rendered
    builder = SandboxBuilder(_cfg(tree, role=role, username=username))
    mounts = builder.workspace_mount_table()
    assert mounts and all(isinstance(m, Mount) for m in mounts)
    argv = _rendered(builder, ["true"])
    binds = [tuple(argv[i:i + 3]) for i, word in enumerate(argv[:-2]) if word in ("--bind", "--ro-bind")]
    want = [("--bind" if m.rw else "--ro-bind", m.host, m.sandbox) for m in mounts]
    at = [binds.index(entry) for entry in want]
    assert at == sorted(at) and len(set(at)) == len(want)


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
    # Where the bookkeeping finds the file, read from the row's text.
    assert df.record_place(df.mount_table(cfg), r.virtual, AGENT) == (
        "workspace/notes.md", agents.resolve() / AGENT / "workspace" / "notes.md")
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
        _read(cfg, "/workspace/link/secret.txt")
    # A write whose deepest EXISTING ancestor is the escaping link.
    with pytest.raises(FileToolError, match="symlink"):
        _write(cfg, "/workspace/link/new/deep.txt", "x")
    assert sorted(os.listdir(outside)) == ["secret.txt"]


def test_symlink_inside_the_mount_is_fine(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    (ws / "alias.md").symlink_to(ws / "notes.md")
    assert "line one" in _read(cfg, "/workspace/alias.md")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def test_read_shape_and_window(tree):
    cfg = _cfg(tree)
    assert _read(cfg, "/workspace/notes.md").splitlines() == [
        "     1\tline one", "     2\tline two", "     3\tline three"]
    assert _read(cfg, "/workspace/notes.md", offset=2, limit=1) == (
        "     2\tline two\n... (1 more lines: continue with offset=3)"
    )
    assert "no lines at offset 9" in _read(cfg, "/workspace/notes.md", offset=9)


def test_read_refuses_binary_folders_and_missing(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    (agents / AGENT / "workspace" / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(FileToolError, match="binary"):
        _read(cfg, "/workspace/blob.bin")
    with pytest.raises(FileToolError, match="folder"):
        _read(cfg, "/workspace")
    with pytest.raises(FileToolError, match="not found"):
        _read(cfg, "/workspace/nope.md")
    (agents / AGENT / "workspace" / "empty.md").write_text("")
    assert _read(cfg, "/workspace/empty.md") == "(empty file)"


def test_read_byte_cap(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    big = agents / AGENT / "workspace" / "big.txt"
    big.write_text("x" * 100 + "\n" * 1 + "y" * (df.READ_MAX_BYTES + 10))
    assert "read stopped at 256 KB" in _read(cfg, "/workspace/big.txt")


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
    assert _glob(cfg, "/workspace", "**/*.md") == ["/workspace/docs/a.md", "/workspace/notes.md"]
    assert _glob(cfg, "/workspace", "*.md") == ["/workspace/notes.md"]
    assert _glob(cfg, "/workspace", "docs/*") == ["/workspace/docs/a.md", "/workspace/docs/b.txt"]
    assert _glob(cfg, "/workspace", "nothing/*") == []
    with pytest.raises(FileToolError, match="relative"):
        _glob(cfg, "/workspace", "../*")
    with pytest.raises(FileToolError, match="not a folder"):
        _glob(cfg, "/workspace/notes.md", "*")


def test_glob_caps_at_200_entries(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    many = agents / AGENT / "workspace" / "many"
    many.mkdir()
    for i in range(250):
        (many / f"f{i:03d}.txt").write_text("")
    out = _glob(cfg, "/workspace/many", "*.txt")
    assert len(out) == df.GLOB_MAX_ENTRIES and out == sorted(out)


def test_write_edit_roundtrip_and_caps(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    path, host = "/workspace/new/deep.md", agents / AGENT / "workspace" / "new" / "deep.md"
    assert _write(cfg, path, "hello\nworld\n") == 12
    assert host.read_text() == "hello\nworld\n"
    assert _edit(cfg, path, "world", "there") == 1
    assert host.read_text() == "hello\nthere\n"
    with pytest.raises(FileToolError, match="not found"):
        _edit(cfg, path, "zzz", "y")
    _write(cfg, path, "a a a\n")
    with pytest.raises(FileToolError, match="appears 3 times"):
        _edit(cfg, path, "a", "b")
    assert _edit(cfg, path, "a", "b", replace_all=True) == 3
    with pytest.raises(FileToolError, match="identical"):
        _edit(cfg, path, "b", "b")
    with pytest.raises(FileToolError, match="required"):
        _edit(cfg, path, "", "b")
    with pytest.raises(FileToolError, match="up to 1024 KB"):
        _write(cfg, path, "x" * (df.WRITE_MAX_BYTES + 1))
    with pytest.raises(FileToolError, match="folder"):
        _write(cfg, "/workspace/new", "x")
    assert host.read_text() == "b b b\n" and sorted(os.listdir(host.parent)) == ["deep.md"]


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
        _write(cfg, "/workspace/escape", "x")
    assert not target.exists()


def test_existing_leaf_symlink_write_refused(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    (ws / "alias.md").symlink_to(ws / "notes.md")
    with pytest.raises(FileToolError, match="symlink"):
        _write(cfg, "/workspace/alias.md", "x")
    with pytest.raises(FileToolError, match="symlink"):
        _edit(cfg, "/workspace/alias.md", "line one", "x")
    assert (ws / "notes.md").read_text() == "line one\nline two\nline three\n"
    # A link in the middle of a write path is refused as well.
    (ws / "sub").mkdir()
    (ws / "alias-dir").symlink_to(ws / "sub")
    with pytest.raises(FileToolError, match="symlink"):
        _write(cfg, "/workspace/alias-dir/new.md", "x")
    assert not (ws / "sub" / "new.md").exists()


def test_read_of_swapped_alias_is_refused(tree, tmp_path):
    """An in-mount alias is readable; once it points out of the mount, the
    read is refused. What a swap between the judgement and the open meets is
    the program's strict open (``test_direct_file_program.py``)."""
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    (ws / "alias.md").symlink_to(ws / "notes.md")
    assert "line one" in _read(cfg, "/workspace/alias.md")
    secret = tmp_path / "config.env"
    secret.write_text("JWT_SECRET=1\n")
    (ws / "alias.md").unlink()
    (ws / "alias.md").symlink_to(secret)
    with pytest.raises(FileToolError):
        _read(cfg, "/workspace/alias.md")
    # A regular file swapped for a link, the same.
    (ws / "notes.md").unlink()
    (ws / "notes.md").symlink_to(secret)
    with pytest.raises(FileToolError):
        _read(cfg, "/workspace/notes.md")


def test_edit_never_writes_through_a_link(tree, tmp_path):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    outside = tmp_path / "outside.md"
    outside.write_text("keep me")
    (ws / "notes.md").unlink()
    (ws / "notes.md").symlink_to(outside)
    with pytest.raises(FileToolError, match="symlink"):
        _edit(cfg, "/workspace/notes.md", "keep", "gone")
    with pytest.raises(FileToolError, match="symlink"):
        _write(cfg, "/workspace/notes.md", "overwritten")
    assert outside.read_text() == "keep me"


def test_a_write_into_a_row_whose_folder_became_a_link_is_never_followed(tree):
    """A row's folder swapped for a link after the session read its table.
    Here the rows are plain paths, so the program's walk meets the link and
    refuses. In a sandbox the mount was made before the swap and does not
    move: ``test_direct_files_sandboxed.py`` holds that case."""
    agents, _ = tree
    cfg = _cfg(tree)
    mounts = df.mount_table(cfg)
    ws = agents / AGENT / "workspace"
    ws.rename(ws.with_name("workspace-real"))
    ws.symlink_to("workspace-real")
    with pytest.raises(FileToolError, match="symlink"):
        _write(cfg, "/workspace/x.md", "x", mounts=mounts)
    assert not (agents / AGENT / "workspace-real" / "x.md").exists()


# ---------------------------------------------------------------------------
# The root: every open beneath the agent's real folder, the row's own folder
# in the rel (a session can write the parent of some rows)
# ---------------------------------------------------------------------------

def _stand_in(tmp_path, name="stand-in"):
    """A folder of the test's own, outside the agent's, with one file."""
    folder = tmp_path / name
    folder.mkdir()
    (folder / "doc.md").write_text("stand-in text\n")
    return folder


def _swap(folder, target):
    """The folder moved aside and a link to ``target`` put in its place."""
    folder.rename(folder.with_name(folder.name + "-aside"))
    folder.symlink_to(target)


def _mirror(tree, *, writable=False, source="src"):
    """An owner's session with a library attached; its mirror holds doc.md."""
    agents, _ = tree
    cfg = _cfg(tree, libraries=[(source, "", writable)])
    mounts = df.mount_table(cfg)
    mirror = agents / AGENT / "knowledge" / "shared" / source
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "doc.md").write_text("mirror text\n")
    return cfg, mounts, mirror


def test_a_library_mirror_turned_link_is_never_read(tree, tmp_path):
    cfg, mounts, mirror = _mirror(tree)
    assert {m.sandbox: m.rw for m in mounts}["/knowledge/shared/src"] is False
    assert "mirror text" in _read(cfg, "/knowledge/shared/src/doc.md", mounts=mounts)
    _swap(mirror, _stand_in(tmp_path))
    with pytest.raises(FileToolError, match="symlink"):
        _read(cfg, "/knowledge/shared/src/doc.md", mounts=mounts)


def test_a_library_mirror_turned_link_into_the_agents_folder_is_refused(tree):
    agents, _ = tree
    cfg, mounts, mirror = _mirror(tree)
    bob = agents / AGENT / "users" / "bob" / "workspace"
    (bob / "doc.md").write_text("bob's text\n")
    _swap(mirror, bob)
    with pytest.raises(FileToolError, match="symlink"):
        _read(cfg, "/knowledge/shared/src/doc.md", mounts=mounts)


def test_a_library_mirror_turned_link_is_never_listed(tree, tmp_path):
    cfg, mounts, mirror = _mirror(tree)
    assert _glob(cfg, "/knowledge/shared/src", "*", mounts=mounts) == ["/knowledge/shared/src/doc.md"]
    _swap(mirror, _stand_in(tmp_path))
    with pytest.raises(FileToolError, match="symlink"):
        _glob(cfg, "/knowledge/shared/src", "*", mounts=mounts)
    assert "/knowledge/shared/src/doc.md" not in _glob(cfg, "/knowledge", "**/*", mounts=mounts)


def test_a_writable_folder_turned_link_after_the_resolve_is_never_written(tree, tmp_path):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "users" / "alice" / "workspace"
    (ws / "notes.md").write_text("keep\n")
    mounts = df.mount_table(cfg)
    stand_in = _stand_in(tmp_path)
    (stand_in / "notes.md").write_text("keep\n")
    _swap(ws, stand_in)
    with pytest.raises(FileToolError, match="symlink"):
        _write(cfg, "/users/alice/workspace/new.md", "x", mounts=mounts)
    with pytest.raises(FileToolError, match="symlink"):
        _edit(cfg, "/users/alice/workspace/notes.md", "keep", "gone", mounts=mounts)
    assert not (stand_in / "new.md").exists()
    assert (stand_in / "notes.md").read_text() == "keep\n"


def _outside(tree, home, **picks):
    agents, mcps = tree
    return SandboxConfig(
        role="external", username="", agent_name=AGENT, is_admin_agent=False,
        host_agents_dir=agents.resolve(), host_mcps_dir=mcps.resolve(),
        host_claude_dir=home / ".claude", config_visible=False, net_forwards=["8400"],
        external=True, external_home=str(home), **picks,
    )


def test_an_outside_pick_and_a_callers_memory_turned_links_are_never_read(tree, tmp_path):
    agents, _ = tree
    faq = agents / AGENT / "knowledge" / "faq"
    faq.mkdir()
    (faq / "doc.md").write_text("faq\n")
    home = agents / AGENT / "externals" / "phone" / "302101234567"
    home.mkdir(parents=True)
    cfg = _outside(tree, home, outside_files_write=True, outside_knowledge=("faq",))
    mounts = df.mount_table(cfg)
    memory = home / "context" / "memory"
    (memory / "doc.md").write_text("memory\n")
    paths = ("/knowledge/faq/doc.md", "/caller/context/memory/doc.md")
    assert "faq" in _read(cfg, paths[0], mounts=mounts) and "memory" in _read(cfg, paths[1], mounts=mounts)
    _swap(faq, _stand_in(tmp_path))
    _swap(memory, _stand_in(tmp_path, "stand-in-2"))
    for path in paths:
        with pytest.raises(FileToolError, match="symlink"):
            _read(cfg, path, mounts=mounts)


def test_a_link_to_another_row_or_a_neighbour_place_is_refused(tree):
    agents, _ = tree
    cfg = _cfg(tree, libraries=[("marketing", "", False), ("marketing-extra", "", False)])
    mounts = df.mount_table(cfg)
    base = agents / AGENT / "knowledge" / "shared"
    (base / "marketing-extra" / "doc.md").write_text("extra\n")
    (base / "marketing" / "x.md").symlink_to("../marketing-extra/doc.md")
    (agents / AGENT / "workspace" / "k.md").symlink_to("../knowledge/guide.md")
    for path in ("/knowledge/shared/marketing/x.md", "/workspace/k.md"):
        with pytest.raises(FileToolError, match="leaves the session folder"):
            _read(cfg, path, mounts=mounts)


# ---------------------------------------------------------------------------
# Honest files keep working
# ---------------------------------------------------------------------------

def test_an_agent_folder_the_operator_moved_keeps_every_tool(tree, tmp_path):
    agents, _ = tree
    disk = tmp_path / "disk"
    disk.mkdir()
    (agents / AGENT).rename(disk / AGENT)
    (agents / AGENT).symlink_to(disk / AGENT)
    cfg = _cfg(tree)
    assert "line one" in _read(cfg, "/workspace/notes.md")
    assert _glob(cfg, "/workspace", "*.md") == ["/workspace/notes.md"]
    new = "/users/alice/workspace/new.md"
    assert _write(cfg, new, "one\n") == 4 and _edit(cfg, new, "one", "two") == 1
    assert (disk / AGENT / "users" / "alice" / "workspace" / "new.md").read_text() == "two\n"


def test_honest_links_in_a_workspace_read_and_list_as_before(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    ws = agents / AGENT / "workspace"
    (ws / "reports").mkdir()
    (ws / "reports" / "a.md").write_text("report\n")
    (ws / "latest.md").symlink_to("reports/a.md")
    (ws / "shortcut").symlink_to("reports")
    (ws / "gone.md").symlink_to("reports/nothing.md")
    assert "report" in _read(cfg, "/workspace/latest.md")
    assert "report" in _read(cfg, "/workspace/shortcut/a.md")
    assert _glob(cfg, "/workspace/shortcut", "*") == ["/workspace/shortcut/a.md"]
    assert _glob(cfg, "/workspace", "**/*.md") == [
        "/workspace/latest.md", "/workspace/notes.md", "/workspace/reports/a.md"]
    with pytest.raises(FileToolError, match="not found"):
        _read(cfg, "/workspace/gone.md")


def test_libraries_read_and_write_as_before(tree):
    agents, _ = tree
    cfg, mounts, _mirror_dir = _mirror(tree, writable=True, source="team")
    assert "/knowledge/shared/team" not in {m.sandbox for m in mounts}
    assert _write(cfg, "/knowledge/shared/team/new.md", "hello\n", mounts=mounts) == 6
    assert (agents / AGENT / "knowledge" / "shared" / "team" / "new.md").read_text() == "hello\n"
    personal = _cfg(tree, mount_shared=False, libraries=[("team", "", False)])
    rows = df.mount_table(personal)
    assert {m.sandbox: m.rw for m in rows}["/knowledge/shared/team"] is False
    assert "hello" in _read(personal, "/knowledge/shared/team/new.md", mounts=rows)
    assert "/knowledge/shared/team/new.md" in _glob(personal, "/knowledge/shared/team", "*", mounts=rows)


def test_a_pipe_is_refused_at_once_and_never_listed(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    os.mkfifo(agents / AGENT / "workspace" / "pipe")
    with pytest.raises(FileToolError, match="cannot read"):
        _read(cfg, "/workspace/pipe")
    with pytest.raises(FileToolError, match="not a regular file"):
        _write(cfg, "/workspace/pipe", "x")
    assert _glob(cfg, "/workspace", "*") == ["/workspace/notes.md"]


# ---------------------------------------------------------------------------
# A link is judged where it lands, by the same table (the kernel's view)
# ---------------------------------------------------------------------------

def _masked_memory(tree):
    """The phone line's shape: ``/knowledge`` read only, its ``memory``
    folder masked by the empty folder."""
    from core.sandbox.sandbox import empty_mount_dir
    agents, _ = tree
    knowledge = agents / AGENT / "knowledge"
    (knowledge / "memory").mkdir()
    (knowledge / "memory" / "topic.md").write_text("shared memory\n")
    return [Mount(str(agents.resolve() / AGENT / "knowledge"), "/knowledge", False),
            Mount(str(empty_mount_dir()), "/knowledge/memory", False)], knowledge


def test_a_link_onto_a_mask_reads_nothing(tree):
    """A masked folder is empty for the session, also through a link: the
    read finds no file. (Before the reads moved into the sandbox the link
    read as the symlink sentence. Nothing was shown either way.)"""
    mounts, knowledge = _masked_memory(tree)
    (knowledge / "notes.md").symlink_to("memory/topic.md")
    for path in ("/knowledge/memory/topic.md", "/knowledge/notes.md"):
        with pytest.raises(FileToolError, match="not found"):
            _read(_cfg(tree), path, mounts=mounts, cwd="/knowledge")


def test_a_link_onto_the_agent_scopes_masked_state_is_refused(tree):
    agents, _ = tree
    cfg = _cfg(tree, role="editor")
    mounts = df.mount_table(cfg)
    state = agents / AGENT / "workspace" / ".claude"
    (state / "settings.json").write_text("{}")
    (agents / AGENT / "workspace" / "x.md").symlink_to(".claude/settings.json")
    with pytest.raises(FileToolError, match="cannot reach"):
        _read(cfg, "/workspace/x.md", mounts=mounts)
    assert "/workspace/x.md" not in _glob(cfg, "/workspace", "*", mounts=mounts)


def test_a_link_onto_a_sessions_own_engine_state_is_refused(tree):
    """The agent scope runs from workspace/.claude, so no mask covers it: a
    link that lands there is refused by the folder's name."""
    agents, _ = tree
    cfg = _cfg(tree, username="")
    mounts = df.mount_table(cfg)
    assert "/workspace/.claude" not in {m.sandbox for m in mounts}
    ws = agents / AGENT / "workspace"
    (ws / ".claude" / "settings.json").write_text("{}")
    (ws / "x.md").symlink_to(".claude/settings.json")
    (ws / "state").symlink_to(".claude")
    for path in ("/workspace/x.md", "/workspace/state/settings.json"):
        with pytest.raises(FileToolError, match="cannot reach"):
            _read(cfg, path, mounts=mounts)
    with pytest.raises(FileToolError, match="cannot reach"):
        _glob(cfg, "/workspace/state", "*", mounts=mounts)
    assert _glob(cfg, "/workspace", "*", mounts=mounts) == ["/workspace/notes.md"]


def test_a_mask_shows_only_its_own_empty_folder(tree, tmp_path, monkeypatch):
    """A path under a mask lands in the mask's empty folder, never beside it
    in the sessions folder."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(config, "SESSIONS_DIR", sessions)
    (sessions / "history.md").write_text("another session\n")
    mounts, _knowledge = _masked_memory(tree)
    with pytest.raises(FileToolError, match="not found"):
        _read(_cfg(tree), "/knowledge/memory/history.md", mounts=mounts, cwd="/knowledge")
    # No record is ever made for a place under a mask: it has no path below the agent's folder.
    assert df.record_place(mounts, "/knowledge/memory/history.md", AGENT) is None


def test_an_owners_glob_lists_a_read_only_mirror(tree):
    cfg, mounts, _mirror_dir = _mirror(tree)
    assert _glob(cfg, "/knowledge", "**/*.md", mounts=mounts) == [
        "/knowledge/guide.md", "/knowledge/shared/src/doc.md"]


def test_a_link_onto_a_masked_name_of_the_same_row_reads_nothing(tree):
    """A row on the agent's folder itself and a mask beneath it: a link onto
    the masked name lands under the mask, which is empty for the session."""
    from core.sandbox.sandbox import empty_mount_dir
    agents, _ = tree
    a = agents / AGENT
    (a / ".empty").mkdir()
    (a / ".empty" / "x.md").write_text("hidden\n")
    (a / "l.md").symlink_to(".empty/x.md")
    mounts = [Mount(str(agents.resolve() / AGENT), "/a", False), Mount(str(empty_mount_dir()), "/a/.empty", False)]
    # The link lands on the masked name: the mask is an empty folder there, so nothing is found.
    with pytest.raises(FileToolError, match="not found"):
        _read(_cfg(tree), "/a/l.md", mounts=mounts, cwd="/a")


def test_a_name_that_only_starts_like_a_row_is_not_that_row(tree):
    cfg, mounts, _mirror_dir = _mirror(tree)
    extra = tree[0] / AGENT / "knowledge" / "shared" / "src-extra"
    extra.mkdir()
    (extra / "doc.md").write_text("extra\n")
    res = _resolve(cfg, "/knowledge/shared/src-extra/doc.md", writing=True, mounts=mounts)
    assert res.mount.sandbox == "/knowledge" and res.mount.rw
    assert "/knowledge/shared/src-extra/doc.md" in _glob(cfg, "/knowledge/shared", "**/*", mounts=mounts)


# ---------------------------------------------------------------------------
# What a session may write and delete: one table, the same before and after
# the file tools moved into the sandbox
# ---------------------------------------------------------------------------
#
# Every kind of session against every kind of place. ``YES``: Write, Edit and
# Delete work there, and the file holds what each wrote. Anything else is the
# sentence of the refusal: each tool says it, the file program is never
# asked and the file stays as it was. The table passes on the handlers as
# they were in the proxy's own process and on the handlers that call the
# file program: the move changes none of it.
#
# The handlers are called below the gate, so this table holds the mounts'
# word alone. A memory file's refusal by Pass-1 and what a mode decides live
# in ``test_direct_builtins.py``. An ``.env`` file and a library's
# ``shared/`` root are held at the policy itself: ``test_path_policy.py``,
# ``test_path_policy_v2.py`` and the corpus of ``tests/auth/
# test_floor_corpus.py``.

YES = True
RO = "is read-only in this session"
OUT = "is not inside a session folder"
NONE = "no writable location in this session"

def _table_sessions(tree):
    """``(what, cfg, {place: writable})`` for every kind of session."""
    agents, mcps = tree
    home = agents / AGENT
    caller = home / "externals" / "phone" / "302101234567"
    line = home / "externals" / "line" / "scratch"
    faq = home / "knowledge" / "faq"
    for folder in (caller / "workspace", caller / "context" / "memory", line, faq):
        folder.mkdir(parents=True, exist_ok=True)
    library = [("src", "", False)]
    (home / "knowledge" / "shared" / "src").mkdir(parents=True, exist_ok=True)

    def plain(role, username, **more):
        claude = home / (f"users/{username}/.claude" if username else "workspace/.claude")
        claude.mkdir(parents=True, exist_ok=True)
        return SandboxConfig(
            role=role, username=username, agent_name=AGENT, is_admin_agent=False,
            host_agents_dir=agents.resolve(), host_mcps_dir=mcps.resolve(), host_claude_dir=claude.resolve(),
            net_forwards=["8400"], knowledge_libraries=list(library), **more)

    own, root, ws, kn, lib, conf, bob = (
        "/users/alice/workspace", "/users/alice", "/workspace", "/knowledge", "/knowledge/shared/src", "/config",
        "/users/bob/workspace")
    c_ws, c_ctx, c_mem, c_root, c_faq = (
        "/caller/workspace", "/caller/context", "/caller/context/memory", "/caller", "/knowledge/faq")
    return [
        ("a manager with a personal space", plain("manager", "alice"),
         {own: YES, "/users/alice/context": YES, root: RO, ws: YES, kn: YES, lib: RO, conf: YES, bob: OUT}),
        ("an admin with a personal space", plain("admin", "alice"),
         {own: YES, root: RO, ws: YES, kn: YES, lib: RO, conf: YES, bob: OUT}),
        ("an editor with a personal space", plain("editor", "alice"),
         {own: YES, root: RO, ws: YES, kn: RO, lib: RO, conf: OUT, bob: OUT}),
        ("a contributor with a personal space", plain("contributor", "alice"),
         {own: YES, root: RO, ws: YES, kn: RO, lib: RO, conf: OUT, bob: OUT}),
        ("a viewer with a personal space", plain("viewer", "alice"),
         {own: YES, root: RO, ws: RO, kn: RO, lib: RO, conf: OUT, bob: OUT}),
        ("a manager on a Personal only agent", plain("manager", "alice", mount_shared=False),
         {own: YES, root: RO, ws: OUT, kn: OUT, lib: RO, conf: YES, bob: OUT}),
        ("a viewer on a Personal only agent", plain("viewer", "alice", mount_shared=False),
         {own: YES, root: RO, ws: OUT, kn: OUT, lib: RO, conf: OUT, bob: OUT}),
        ("a manager on a Shared only agent", plain("manager", "", config_visible=True),
         {own: OUT, ws: YES, kn: YES, lib: RO, conf: YES}),
        ("an editor on a Shared only agent", plain("editor", "", config_visible=False),
         {own: OUT, ws: YES, kn: RO, lib: RO, conf: OUT}),
        ("a viewer on a Shared only agent", plain("viewer", "", config_visible=False),
         {own: OUT, ws: NONE, kn: NONE, lib: NONE, conf: OUT}),
        ("the agent's own run", plain("manager", "", config_visible=False),
         {own: OUT, ws: YES, kn: RO, lib: RO, conf: OUT}),
        ("a run a manager made", plain("manager", "", config_visible=False, knowledge_rw=True),
         {own: OUT, ws: YES, kn: YES, lib: RO, conf: OUT}),
        ("an outside person with the files pick",
         _outside(tree, caller, outside_files_write=True, outside_knowledge=("faq",)),
         {c_ws: YES, c_ctx: YES, c_mem: RO, c_root: RO, c_faq: RO, ws: OUT, kn: OUT, own: OUT}),
        ("an outside person without it", _outside(tree, caller, outside_knowledge=("faq",)),
         {c_ws: NONE, c_ctx: NONE, c_mem: NONE, c_root: NONE, c_faq: NONE, ws: OUT}),
        ("a wrap-up after a conversation",
         plain("manager", "", config_visible=False, wrapup_home=str(caller)),
         {c_ws: YES, c_ctx: YES, c_mem: RO, c_root: RO, ws: RO, kn: RO, conf: OUT}),
        ("the agent's own phone line", plain("manager", "", config_visible=False, engine_home=str(line)),
         {c_root: RO, ws: YES, kn: RO, conf: OUT}),
        ("a judge", plain("manager", "alice", read_only=True),
         {own: NONE, root: NONE, ws: NONE, kn: NONE, lib: NONE, conf: NONE, bob: OUT}),
    ]


def _place_on_the_server(cfg, path):
    """Where a sandbox path sits on the server, None for a place the session does not have."""
    best = None
    for m in df.mount_table(cfg):
        dest = m.sandbox.rstrip("/")
        if (path == dest or path.startswith(dest + "/")) and (best is None or len(dest) >= len(best.sandbox.rstrip("/"))):
            best = m
    if best is None or best.host == str(empty_mount_dir()):
        return None
    return Path(best.host + path[len(best.sandbox.rstrip("/")):])


def test_what_a_session_may_write_and_delete(tree, monkeypatch):
    from core.layers.direct import builtins as B
    from services.infra import file_bookkeeping
    from storage.files import recover_bin_store
    calls = direct_program.install(monkeypatch)
    monkeypatch.setattr(recover_bin_store, "capture", lambda *_a, **_k: None)
    monkeypatch.setattr(file_bookkeeping, "tombstone_path_sync", lambda *_a, **_k: None)
    monkeypatch.setattr(file_bookkeeping, "schedule_library_projection", lambda *_a, **_k: None)

    async def quiet(*_a, **_k):
        return None

    monkeypatch.setattr(file_bookkeeping, "push_file_delete", quiet)
    monkeypatch.setattr(file_bookkeeping, "push_file_write", quiet)

    class _Handled(_Session):
        session_id = "direct-table"

    wrong: list[tuple] = []

    async def cell(cfg, folder, want, what):
        session = _Handled(cfg)
        where = f"{what}: {folder}"
        seeded = _place_on_the_server(cfg, f"{folder}/table-seed.md")
        if seeded is not None and seeded.parent.is_dir():
            seeded.write_text("seed\n")
        else:
            seeded = None

        def held():
            return seeded.read_text() if seeded is not None and seeded.exists() else None

        asked = len(calls)
        new = await B.execute(session, "Write", {"file_path": f"{folder}/table-new.md", "content": "new\n"})
        edit = await B.execute(session, "Edit", {"file_path": f"{folder}/table-seed.md", "old_string": "seed",
                                                 "new_string": "edited"})
        after_edit = held()
        over = await B.execute(session, "Write", {"file_path": f"{folder}/table-seed.md", "content": "over\n"})
        after_over = held()
        gone = await B.execute(session, "Delete", {"file_path": f"{folder}/table-seed.md"})
        made = _place_on_the_server(cfg, f"{folder}/table-new.md")
        if want is YES:
            said = (new.startswith("Created "), edit.startswith("Edited "), over.startswith("Updated "),
                    gone.startswith("Deleted "))
            if said != (True, True, True, True):
                wrong.append((where, "a tool refused", new, edit, over, gone))
            elif (made.read_text(), after_edit, after_over, held()) != ("new\n", "edited\n", "over\n", None):
                wrong.append((where, "the files hold", made.read_text(), after_edit, after_over, held()))
            if made is not None and made.exists():
                made.unlink()
        else:
            for tool, said in (("Write", new), ("Edit", edit), ("Write over", over), ("Delete", gone)):
                if not (said.startswith("Error: ") and want in said):
                    wrong.append((where, tool, said))
            if len(calls) != asked:
                wrong.append((where, "the file program was asked", calls[asked:]))
            if made is not None and made.exists():
                wrong.append((where, "a file was made"))
            if seeded is not None and (after_edit, after_over, held()) != ("seed\n", "seed\n", "seed\n"):
                wrong.append((where, "the file changed", after_edit, after_over, held()))
        for left in (seeded, made):
            if left is not None and left.exists():
                left.unlink()

    async def every():
        for what, cfg, places in _table_sessions(tree):
            for folder, want in places.items():
                await cell(cfg, folder, want, what)

    asyncio.run(every())
    assert not wrong, "\n".join(str(cell) for cell in wrong)
