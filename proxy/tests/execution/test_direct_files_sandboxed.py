"""The file program inside a real sandbox: what the kernel's mounts add.

Each test renders a session's role table as a compute machine would (the
real launcher, pasta and bwrap, this host standing in for the machine),
starts the file program in it exactly as its open would (its text in the
environment, its facts as its argument) and speaks to it over its pipes.
Where a test loosens the program's own rules it shows the kernel alone: what
is not in the sandbox is not there, whatever the program is told.

Skips unless pasta and bwrap are present, as the sandbox suite's own
integration tests do.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess

import pytest
from mcp.client.stdio import get_default_environment

from core.layers.direct import files as df, sandbox_program as sp
from core.sandbox import sandbox_render
from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount
from core.sandbox.session_config_dir import sandbox_python
from tests.execution import test_direct_files as base
from tests.execution.test_direct_files import (
    AGENT,
    _cfg,
    _mirror,
    _outside,
    _stand_in,
    _swap,
)
from tests.execution.test_sandbox import _host, _needs_netns

pytestmark = _needs_netns

#: The agents tree of the resolver's own tests: one agent, two people.
tree = base.tree


class _Sandboxed:
    """The file program of one session, started as its open starts it."""

    def __init__(self, cfg: SandboxConfig, *, loosen=None):
        builder = SandboxBuilder(cfg)
        description = sp.program_description(builder)
        self.facts = df.program_facts(description, builder)
        if loosen is not None:
            self.facts = loosen(self.facts)
        inner = [sandbox_python(), "-I", "-c", sp._START, json.dumps(self.facts)]
        env = {**get_default_environment(), **builder.get_env_overrides(),
               sp.TEXT_ENV: sp.program_text(df.PROGRAM)}
        self.proc = subprocess.Popen(sandbox_render.render(description, _host(builder), inner), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._id = 0
        assert self._ask("initialize", {"protocolVersion": "2025-06-18"})["result"]["capabilities"] == {"tools": {}}

    def _ask(self, method: str, params: dict) -> dict:
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                                          "params": params}).encode() + b"\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read().decode(errors="replace")[-600:]
        return json.loads(line)

    def call(self, operation: str, **args) -> dict:
        reply = self._ask("tools/call", {"name": operation, "arguments": args})
        return json.loads(reply["result"]["content"][0]["text"])

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.proc.stdin.close()
        self.proc.wait(timeout=15)
        self.proc.stdout.close()
        self.proc.stderr.close()


def _with(**more):
    """Facts with a rule of the program taken away, or a row it never had."""
    def loosen(facts):
        return {**facts, **{k: (facts[k] + v if k == "rows" else v) for k, v in more.items()}}
    return loosen


def _agent(tree):
    return tree[0] / AGENT


# -- what a session reads ---------------------------------------------------

def test_a_person_reads_and_searches_their_own_and_the_agents_folders(tree):
    (_agent(tree) / "users" / "alice" / "workspace" / "mine.md").write_text("mine\n")
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("read", path="/workspace/notes.md")["text"].startswith("line one")
        assert program.call("read", path="/users/alice/workspace/mine.md")["text"] == "mine\n"
        assert program.call("read", path="/knowledge/guide.md")["text"] == "# Guide\n"
        assert program.call("glob", path="/workspace", pattern="*.md")["rels"] == ["notes.md"]
        assert program.call("glob", path="/users/alice", pattern="**/*.md")["rels"] == ["workspace/mine.md"]


def test_a_viewer_reads_what_the_table_gives_a_viewer(tree):
    cfg = _cfg(tree, role="viewer")
    table = {m.sandbox for m in df.mount_table(cfg)}
    with _Sandboxed(cfg) as program:
        for folder, path in (("/knowledge", "/knowledge/guide.md"), ("/workspace", "/workspace/notes.md")):
            assert (program.call("read", path=path)["ok"] is True) == (folder in table), path
        assert program.call("read", path="/knowledge/guide.md")["ok"] is True


def test_an_agents_own_run_reads_the_agents_folders(tree):
    with _Sandboxed(_cfg(tree, username="")) as program:
        assert program.call("read", path="/workspace/notes.md")["ok"] is True
        assert not [row for row in program.facts["rows"] if row.startswith("/users")]


def test_the_skills_folder_is_in_the_sandbox(tree):
    for scope, folder in (("alice", "users/alice/.claude"), ("", "workspace/.claude")):
        skill = _agent(tree) / folder / "skills" / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"body of {scope or 'the agent'}\n")
        with _Sandboxed(_cfg(tree, username=scope)) as program:
            assert program.call("skills") == {"ok": True, "names": ["alpha"]}
            assert program.call("skill", name="alpha")["text"] == f"body of {scope or 'the agent'}\n"


# -- what the kernel does not show, whatever the program is told ---------------

def test_another_persons_folder_is_not_in_the_sandbox(tree):
    (_agent(tree) / "users" / "bob" / "workspace" / "b.md").write_text("bob's\n")
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("read", path="/users/bob/workspace/b.md")["code"] == "not_a_session_folder"
    with _Sandboxed(_cfg(tree), loosen=_with(rows=["/users/bob"])) as program:
        assert program.call("read", path="/users/bob/workspace/b.md")["code"] == "missing"
        assert program.call("glob", path="/users/bob", pattern="**/*")["code"] == "not_a_folder"


def test_a_masked_folder_is_empty_in_the_sandbox(tree):
    state = _agent(tree) / "workspace" / ".claude"
    state.mkdir(exist_ok=True)
    (state / "settings.json").write_text("the agent scope's own state\n")
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("read", path="/workspace/.claude/settings.json")["code"] == "refused_name"
    with _Sandboxed(_cfg(tree), loosen=_with(refused=[], glob_skip=[])) as program:
        assert program.call("read", path="/workspace/.claude/settings.json")["code"] == "missing"
        assert program.call("glob", path="/workspace", pattern="**/settings.json")["rels"] == []


def test_a_link_onto_a_masked_folder_reads_nothing(tree):
    state = _agent(tree) / "workspace" / ".claude"
    state.mkdir(exist_ok=True)
    (state / "settings.json").write_text("state\n")
    (_agent(tree) / "workspace" / "s.json").symlink_to(".claude/settings.json")
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("read", path="/workspace/s.json")["code"] == "elsewhere"
    with _Sandboxed(_cfg(tree), loosen=_with(refused=[])) as program:
        landed = program.call("read", path="/workspace/s.json")
        assert landed == {"ok": False, "code": "lands", "landed": "/workspace/.claude/settings.json"}
        assert program.call("read", path=landed["landed"], strict=True)["code"] == "missing"


def test_a_link_to_another_persons_file_leads_nowhere(tree):
    (_agent(tree) / "users" / "bob" / "workspace" / "b.md").write_text("bob's\n")
    (_agent(tree) / "users" / "alice" / "workspace" / "peek.md").symlink_to("../../bob/workspace/b.md")
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("read", path="/users/alice/workspace/peek.md")["code"] == "leaves"
        assert program.call("glob", path="/users/alice/workspace", pattern="*")["rels"] == []
    with _Sandboxed(_cfg(tree), loosen=_with(rows=["/users"])) as program:
        assert program.call("read", path="/users/alice/workspace/peek.md", strict=True)["code"] == "link"


def test_a_link_to_another_row_is_refused(tree):
    (_agent(tree) / "workspace" / "k.md").symlink_to("../knowledge/guide.md")
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("read", path="/workspace/k.md")["code"] == "leaves"


def test_a_manifest_mount_and_a_tool_folder_are_not_in_a_programs_sandbox(tree):
    agents, mcps = tree
    data = mcps / "community" / "x" / "data"
    data.mkdir(parents=True)
    (data / "marker.md").write_text("the tool's folder\n")
    own = _agent(tree) / "workspace" / "x"
    own.mkdir()
    (own / "marker.md").write_text("the agent's own folder\n")
    base = _cfg(tree)
    cfg = SandboxConfig(
        role=base.role, username=base.username, agent_name=base.agent_name, is_admin_agent=False,
        host_agents_dir=base.host_agents_dir, host_mcps_dir=base.host_mcps_dir,
        host_claude_dir=base.host_claude_dir, net_forwards=["8400"],
        mcp_sandbox_mounts=[SandboxMount(str(data), "/workspace/x", "rw")],
        mcp_dir_binds=[str(mcps / "community" / "x")])
    session = SandboxBuilder(cfg).describe()
    assert [b.sandbox for b in session.extra_binds] == ["/workspace/x"] and session.mcp_binds
    with _Sandboxed(cfg, loosen=_with(rows=[str(mcps)])) as program:
        assert program.call("read", path="/workspace/x/marker.md")["text"] == "the agent's own folder\n"
        assert program.call("read", path=f"{data}/marker.md")["code"] == "missing"


def test_a_library_mirror_turned_link_is_never_read(tree, tmp_path):
    """A link put where a mirror was, before a session's start or after it,
    leads to nothing the session reads: what it points at is not in the
    sandbox, and the program follows no link out of the mirror's row."""
    agents, _mcps = tree
    cfg, _mounts, mirror = _mirror(tree)
    (agents / AGENT / "users" / "bob" / "workspace" / "doc.md").write_text("bob's text\n")
    with _Sandboxed(cfg) as program:
        assert program.call("read", path="/knowledge/shared/src/doc.md")["text"] == "mirror text\n"
        assert program.call("glob", path="/knowledge/shared/src", pattern="*")["rels"] == ["doc.md"]
        _swap(mirror, _stand_in(tmp_path))
        assert program.call("read", path="/knowledge/shared/src/doc.md")["code"] == "leaves"
        assert program.call("glob", path="/knowledge/shared/src", pattern="*")["code"] == "leaves"
        assert "shared/src/doc.md" not in program.call("glob", path="/knowledge", pattern="**/*")["rels"]
        mirror.unlink()
        mirror.symlink_to("../../users/bob/workspace")
        assert program.call("read", path="/knowledge/shared/src/doc.md")["code"] == "leaves"
        assert program.call("read", path="/knowledge/shared/src/doc.md", strict=True)["code"] == "link"


def test_an_outside_callers_tree_reads_and_its_picks_alone(tree):
    home = _agent(tree) / "externals" / "phone" / "302101234567"
    faq = _agent(tree) / "knowledge" / "faq"
    for folder in (home / "workspace", home / "context" / "memory", faq):
        folder.mkdir(parents=True)
    (home / "workspace" / "mine.md").write_text("the caller's\n")
    (faq / "doc.md").write_text("faq\n")
    with _Sandboxed(_outside(tree, home, outside_files_write=True, outside_knowledge=("faq",))) as program:
        assert program.call("read", path="/caller/workspace/mine.md")["text"] == "the caller's\n"
        assert program.call("read", path="/knowledge/faq/doc.md")["text"] == "faq\n"
        assert program.call("read", path="/knowledge/guide.md")["code"] in ("missing", "not_a_session_folder")
        assert program.call("read", path="/workspace/notes.md")["code"] == "not_a_session_folder"


def test_an_agent_folder_the_operator_moved_reads_as_before(tree, tmp_path):
    agents, _mcps = tree
    disk = tmp_path / "disk"
    disk.mkdir()
    (agents / AGENT).rename(disk / AGENT)
    (agents / AGENT).symlink_to(disk / AGENT)
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("read", path="/workspace/notes.md")["text"].startswith("line one")
        assert program.call("glob", path="/workspace", pattern="*.md")["rels"] == ["notes.md"]


# -- what a session writes: the kernel's mounts are the wall --------------------
#
# The platform's text check words an early refusal. Under it the mounts
# decide, whatever the program is told: each test below that loosens the
# facts sends the write straight to the program, past every rule of its own.

def _told_nothing(facts):
    """The program told that every path is a row and no name is refused."""
    return {**facts, "rows": ["/"], "refused": []}


def test_a_person_writes_edits_and_looks_where_the_table_lets_them(tree):
    home = _agent(tree)
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("write", path="/workspace/out/new.md", content="one\n") == {
            "ok": True, "bytes": 4, "created": True}
        made = home / "workspace" / "out" / "new.md"
        assert made.read_text() == "one\n"
        assert program.call("edit", path="/workspace/out/new.md", old="one", new="two") == {"ok": True, "count": 1}
        assert made.read_text() == "two\n"
        for path in ("/users/alice/workspace/mine.md", "/users/alice/context/c.md", "/knowledge/k.md", "/config/c.md"):
            assert program.call("write", path=path, content="x")["ok"] is True, path
        # A new file and its folder are the session's own account's, with the umask's bits.
        mask = os.umask(0)
        os.umask(mask)
        assert made.stat().st_uid == os.getuid() and made.parent.stat().st_uid == os.getuid()
        assert made.stat().st_mode & 0o777 == 0o666 & ~mask and made.parent.stat().st_mode & 0o777 == 0o777 & ~mask
        assert not [n for n in os.listdir(made.parent) if n.endswith(".partial")]


def test_the_sandbox_and_the_platform_see_one_file_by_the_same_three_values(tree):
    """The delete's check rests on it: what the program answers for a file
    inside the sandbox is what the platform's own look shows outside it."""
    from services.infra import file_bookkeeping
    target = _agent(tree) / "workspace" / "notes.md"
    with _Sandboxed(_cfg(tree)) as program:
        answer = program.call("stat", path="/workspace/notes.md")
        assert df.file_facts(answer) == file_bookkeeping._facts_of(target.lstat())
        program.call("write", path="/workspace/notes.md", content="again\n")
        again = program.call("stat", path="/workspace/notes.md")
        assert df.file_facts(again) == file_bookkeeping._facts_of(target.lstat()) != df.file_facts(answer)


def _read_only_rows(tree):
    """Every kind of row a session may only read, by the session that has it:
    ``(what, cfg, a path there, an existing file there or None)``."""
    home = _agent(tree)
    caller = home / "externals" / "phone" / "302101234567"
    faq = home / "knowledge" / "faq"
    for folder in (caller / "workspace", caller / "context" / "memory", faq, home / "knowledge" / ".credentials"):
        folder.mkdir(parents=True, exist_ok=True)
    (caller / "workspace" / "mine.md").write_text("the caller's\n")
    (caller / "context" / "memory" / "m.md").write_text("memory\n")
    (faq / "doc.md").write_text("faq\n")
    (home / "users" / "alice" / "top.md").write_text("at the root\n")
    library, _mounts, mirror = _mirror(tree)
    writable_library, _m, _d = _mirror(tree, writable=True, source="open")
    judge = dataclasses.replace(_cfg(tree), read_only=True)
    wrapup = SandboxConfig(
        role="manager", username="", agent_name=AGENT, is_admin_agent=False,
        host_agents_dir=tree[0].resolve(), host_mcps_dir=tree[1].resolve(),
        host_claude_dir=caller / ".claude", config_visible=False, net_forwards=["8400"], wrapup_home=str(caller))
    return library, writable_library, [
        ("a viewer's workspace", _cfg(tree, role="viewer"), "/workspace/x.md", "/workspace/notes.md"),
        ("a viewer's knowledge", _cfg(tree, role="viewer"), "/knowledge/x.md", "/knowledge/guide.md"),
        ("an editor's knowledge", _cfg(tree, role="editor"), "/knowledge/x.md", "/knowledge/guide.md"),
        ("a person's own root", _cfg(tree), "/users/alice/x.md", "/users/alice/top.md"),
        ("a library's read-only copy under a writable knowledge", library, "/knowledge/shared/src/x.md",
         "/knowledge/shared/src/doc.md"),
        ("a hidden folder", _cfg(tree), "/knowledge/.credentials/x.md", None),
        ("the agent's own run: knowledge", _cfg(tree, username=""), "/knowledge/x.md", "/knowledge/guide.md"),
        ("a viewer on a Shared only agent", _cfg(tree, role="viewer", username=""), "/workspace/x.md",
         "/workspace/notes.md"),
        ("an outside person without the files pick", _outside(tree, caller), "/caller/workspace/x.md",
         "/caller/workspace/mine.md"),
        ("an outside person's memory", _outside(tree, caller, outside_files_write=True),
         "/caller/context/memory/x.md", "/caller/context/memory/m.md"),
        ("an outside person's picked knowledge", _outside(tree, caller, outside_files_write=True,
                                                         outside_knowledge=("faq",)),
         "/knowledge/faq/x.md", "/knowledge/faq/doc.md"),
        ("a judge", judge, "/workspace/x.md", "/workspace/notes.md"),
        ("a judge's own space", judge, "/users/alice/workspace/x.md", None),
        ("a wrap-up: the shared workspace", wrapup, "/workspace/x.md", "/workspace/notes.md"),
        ("a wrap-up: the person's memory", wrapup, "/caller/context/memory/x.md", "/caller/context/memory/m.md"),
    ]


def _host_of(cfg, path):
    """The place on the server of a sandbox path, from the session's table."""
    best = None
    for m in df.mount_table(cfg):
        dest = m.sandbox.rstrip("/")
        if (path == dest or path.startswith(dest + "/")) and (best is None or len(dest) >= len(best.sandbox.rstrip("/"))):
            best = m
    return None if best is None else best.host + path[len(best.sandbox.rstrip("/")):]


def test_the_kernel_refuses_a_change_under_every_read_only_row(tree):
    _library, _writable, rows = _read_only_rows(tree)
    for what, cfg, new, existing in rows:
        with _Sandboxed(cfg, loosen=_told_nothing) as program:
            assert program.call("write", path=new, content="x")["code"] == "read_only", what
            assert program.call("write", path=new.replace("x.md", "made/x.md"), content="x")["code"] == "read_only", what
            if existing is not None:
                before = open(_host_of(cfg, existing)).read()
                assert program.call("write", path=existing, content="x")["code"] == "read_only", what
                assert program.call("edit", path=existing, old=before[0], new="#")["code"] == "read_only", what
                # The delete's look says so too: the platform then removes nothing.
                assert program.call("stat", path=existing)["code"] == "read_only", what
                assert open(_host_of(cfg, existing)).read() == before, what
        host = _host_of(cfg, new)
        assert host is None or not os.path.exists(host), what


def test_a_librarys_writable_copy_is_written_where_knowledge_is(tree):
    _library, writable, _rows = _read_only_rows(tree)
    with _Sandboxed(writable) as program:
        assert program.call("write", path="/knowledge/shared/open/new.md", content="x")["ok"] is True
        assert program.call("stat", path="/knowledge/shared/open/doc.md")["ok"] is True
    assert (_agent(tree) / "knowledge" / "shared" / "open" / "new.md").read_text() == "x"


def test_what_is_not_in_the_session_is_not_written_whatever_the_program_is_told(tree):
    """Another person's space, a folder of no row, the folders the sandbox
    makes for its mount points, its own temp folder and the system's: a
    write there never reaches the server's files."""
    home = _agent(tree)
    (home / "users" / "bob" / "workspace" / "b.md").write_text("bob's\n")
    marker = "oto-direct-files-write-marker.md"
    with _Sandboxed(_cfg(tree, role="editor")):
        pass                    # a session's first sandbox makes its own folders: they are there from here on
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    with _Sandboxed(_cfg(tree, role="editor")) as program:
        # With its own rules: none of these is a row.
        for path in ("/users/bob/workspace/x.md", "/config/x.md", f"/users/{marker}", f"/tmp/{marker}",
                     f"/etc/{marker}", f"/{marker}"):
            assert program.call("write", path=path, content="x")["code"] == "not_a_session_folder", path
    with _Sandboxed(_cfg(tree, role="editor"), loosen=_told_nothing) as program:
        for path in ("/users/bob/workspace/x.md", "/config/x.md", f"/users/{marker}", f"/tmp/{marker}",
                     f"/etc/{marker}", f"/{marker}"):
            program.call("write", path=path, content="x")       # wherever it lands, it is not the server
        assert program.call("edit", path="/users/bob/workspace/b.md", old="bob", new="x")["code"] == "missing"
        assert program.call("stat", path="/users/bob/workspace/b.md")["code"] == "missing"
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == before
    assert (home / "users" / "bob" / "workspace" / "b.md").read_text() == "bob's\n"
    for outside in (f"/tmp/{marker}", f"/etc/{marker}", f"/{marker}", f"/users/{marker}"):
        assert not os.path.exists(outside), outside


def test_a_manifest_mount_inside_a_writable_folder_is_not_where_a_write_lands(tree):
    agents, mcps = tree
    data = mcps / "community" / "x" / "data"
    data.mkdir(parents=True)
    own = _agent(tree) / "workspace" / "x"
    own.mkdir()
    base = _cfg(tree)
    cfg = dataclasses.replace(base, mcp_sandbox_mounts=[SandboxMount(str(data), "/workspace/x", "rw")],
                              mcp_dir_binds=[str(mcps / "community" / "x")])
    assert [b.sandbox for b in SandboxBuilder(cfg).describe().extra_binds] == ["/workspace/x"]
    with _Sandboxed(cfg) as program:
        assert program.call("write", path="/workspace/x/new.md", content="mine")["ok"] is True
    # The agent's own folder took it, which is the path the policy judged and the record names.
    assert (own / "new.md").read_text() == "mine" and not (data / "new.md").exists()


def test_a_write_follows_no_link_out_of_the_session(tree, tmp_path):
    home = _agent(tree)
    (home / "users" / "bob" / "workspace" / "b.md").write_text("bob's\n")
    (home / "workspace" / "to-bob").symlink_to("../users/bob/workspace")
    (home / "workspace" / "b-link.md").symlink_to("../users/bob/workspace/b.md")
    (home / "workspace" / "out").symlink_to(_stand_in(tmp_path))
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("write", path="/workspace/to-bob/new.md", content="x")["code"] == "link"
        assert program.call("write", path="/workspace/out/doc.md", content="x")["code"] == "link"
        assert program.call("write", path="/workspace/b-link.md", content="x")["code"] == "link_at_name"
        assert program.call("edit", path="/workspace/b-link.md", old="bob", new="x")["code"] == "link_at_name"
        assert program.call("stat", path="/workspace/to-bob/b.md")["code"] == "leaves"
        assert program.call("stat", path="/workspace/b-link.md")["code"] == "special"
    assert (home / "users" / "bob" / "workspace" / "b.md").read_text() == "bob's\n"
    assert sorted(os.listdir(home / "users" / "bob" / "workspace")) == ["b.md"]
    assert (tmp_path / "stand-in" / "doc.md").read_text() == "stand-in text\n"


def test_a_row_whose_folder_became_a_link_after_the_start_is_the_folder_that_was_mounted(tree, tmp_path):
    """A mount does not move. A row's folder swapped for a link once the
    program runs is still the folder the write lands in, never where the
    link leads. The record then names the path as the table spells it, and
    the platform's own opens follow no link there, so nothing is handed on
    from another place."""
    from core import agent_folder
    from services.infra import safe_fs
    home = _agent(tree)
    cfg = _cfg(tree)
    mounts = df.mount_table(cfg)
    stand_in = _stand_in(tmp_path)
    with _Sandboxed(cfg) as program:
        _swap(home / "workspace", stand_in)
        assert program.call("write", path="/workspace/late.md", content="x")["ok"] is True
        assert program.call("read", path="/workspace/notes.md")["text"].startswith("line one")
    assert (home / "workspace-aside" / "late.md").read_text() == "x"
    assert sorted(os.listdir(stand_in)) == ["doc.md"]
    assert df.record_place(mounts, "/workspace/late.md", AGENT) == ("workspace/late.md", home / "workspace" / "late.md")
    with pytest.raises(OSError):
        safe_fs.open_regular_for_read(agent_folder.agent_root(AGENT), "workspace/late.md")


def test_an_agent_folder_the_operator_moved_is_written_as_before(tree, tmp_path):
    agents, _mcps = tree
    disk = tmp_path / "disk"
    disk.mkdir()
    (agents / AGENT).rename(disk / AGENT)
    (agents / AGENT).symlink_to(disk / AGENT)
    with _Sandboxed(_cfg(tree)) as program:
        assert program.call("write", path="/workspace/new.md", content="x")["ok"] is True
        assert program.call("edit", path="/workspace/notes.md", old="line one", new="first")["ok"] is True
        assert program.call("stat", path="/workspace/new.md")["ok"] is True
    assert (disk / AGENT / "workspace" / "new.md").read_text() == "x"
    assert (disk / AGENT / "workspace" / "notes.md").read_text().startswith("first")


@pytest.mark.parametrize("who, where, writes", [
    ("with the files pick", "/caller/workspace/new.md", True),
    ("with the files pick", "/caller/context/new.md", True),
    ("with the files pick", "/caller/new.md", False),
    ("without it", "/caller/workspace/new.md", False),
])
def test_an_outside_person_writes_their_own_tree_only_with_the_files_pick(tree, who, where, writes):
    home = _agent(tree) / "externals" / "phone" / "302101234567"
    for folder in (home / "workspace", home / "context" / "memory"):
        folder.mkdir(parents=True)
    with _Sandboxed(_outside(tree, home, outside_files_write=who.startswith("with "))) as program:
        answer = program.call("write", path=where, content="x")
        assert (answer["ok"] is True) == writes, answer
        assert program.call("write", path="/workspace/new.md", content="x")["code"] == "not_a_session_folder"
