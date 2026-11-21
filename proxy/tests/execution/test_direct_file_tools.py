"""The platform's half of the file tools: the calls to the file program,
the second look at a place a link lands, what follows a write's answer, the
delete's look, and the sentences.

The program here is a script of answers, also false ones: no answer of the
program may widen what the platform asks it to open. What the program itself
does is ``test_direct_file_program.py``'s, and what the kernel's mounts add
``test_direct_files_sandboxed.py``'s.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter

import pytest

from auth.path_policy import PathDecision, SecurityContext
from core.layers.direct import builtins as B, files as df, sandbox_program as sp
from core.layers.direct.files import FileToolError
from core.sandbox import sandbox_render as render
from core.sandbox.sandbox import Mount, SandboxConfig
from core.session.session_state import (
    cleanup_session_permission_state,
    set_session_security,
)
from tests.execution.test_direct_sandbox_program import (  # noqa: F401  # ``world`` is the stand-in machine's fixture
    _no_give_back,
    _program as _a_program,
    world,
)

AGENT = "pa"
MOUNTS = [Mount("/host/pa/workspace", "/workspace", True),
          Mount("/host/pa/users/alice", "/users/alice", False),
          Mount("/host/pa/users/alice/workspace", "/users/alice/workspace", True),
          Mount("/host/empty", "/workspace/.claude", False)]


class _Script:
    """A file program that answers what a test wrote down, and keeps every
    call it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, operation, args, then=None):
        self.calls.append((operation, args))
        answer = self.answers.pop(0) if self.answers else {"ok": False, "code": "missing"}
        if then is not None:
            # As the general part runs it: once the program was asked, with its answer.
            await then(answer)
        return answer


def _judge(seen):
    def judge(asked, landed):
        seen.append((asked, landed))
    return judge


def _run(coro):
    return asyncio.run(coro)


# -- the text check -----------------------------------------------------------

def test_locate_judges_the_text_and_opens_nothing():
    assert df.locate(MOUNTS, "notes.md", cwd="/workspace") == ("/workspace/notes.md", MOUNTS[0])
    assert df.locate(MOUNTS, "/users/alice/workspace/a/../b.md", cwd="/")[0] == "/users/alice/workspace/b.md"
    with pytest.raises(FileToolError, match="the .claude subtree"):
        df.locate(MOUNTS, "/workspace/.claude/settings.json", cwd="/")
    with pytest.raises(FileToolError, match="not inside a session folder"):
        df.locate(MOUNTS, "/etc/passwd", cwd="/")
    with pytest.raises(FileToolError, match="read-only in this session"):
        df.locate(MOUNTS, "/users/alice/x.md", cwd="/", writing=True)


@pytest.mark.parametrize("landed, sentence", [
    ("/workspace/sub/b.md", None),
    ("/users/alice/workspace/b.md", df.LEAVES),          # another row
    ("/workspacex/b.md", df.ELSEWHERE),                   # no session folder
    ("/workspace/.claude/settings.json", df.ELSEWHERE),   # a refused name
    ("/workspace/a/../b.md", df.ELSEWHERE),               # not a normal path
    ("/etc/passwd", df.ELSEWHERE),
])
def test_a_landing_is_judged_again_on_the_text(landed, sentence):
    if sentence is None:
        df.landing(MOUNTS, "/workspace/link.md", landed)
        return
    with pytest.raises(FileToolError) as refused:
        df.landing(MOUNTS, "/workspace/link.md", landed)
    assert str(refused.value) == sentence


def test_a_link_is_followed_only_inside_the_row_it_starts_in():
    df.landing(MOUNTS, "/users/alice/link.md", "/users/alice/workspace/b.md")       # a row below its own
    with pytest.raises(FileToolError, match="leaves the session folder"):
        df.landing(MOUNTS, "/users/alice/workspace/link.md", "/users/alice/b.md")   # out of its own


# -- read ---------------------------------------------------------------------

def test_read_numbers_the_programs_text():
    program = _Script({"ok": True, "text": "one\ntwo\nthree\n", "truncated": False})
    out = _run(df.read(program, "/workspace/n.md", 2, 1, judge=_judge([])))
    assert out == "     2\ttwo\n... (1 more lines: continue with offset=3)"
    assert program.calls == [("read", {"path": "/workspace/n.md"})]


def test_read_says_where_the_cap_stopped_it_and_what_an_empty_file_is():
    cut = _Script({"ok": True, "text": "x\n", "truncated": True}, {"ok": True, "text": "", "truncated": False})
    assert _run(df.read(cut, "/workspace/n.md", judge=_judge([]))).endswith("... (read stopped at 256 KB)")
    assert _run(df.read(cut, "/workspace/n.md", judge=_judge([]))) == "(empty file)"


def test_a_read_through_a_link_is_judged_where_it_lands_and_opened_there_strictly():
    program = _Script({"ok": False, "code": "lands", "landed": "/workspace/sub/b.md"},
                      {"ok": True, "text": "gamma\n", "truncated": False})
    seen: list = []
    assert _run(df.read(program, "/workspace/link.md", judge=_judge(seen))) == "     1\tgamma"
    assert seen == [("/workspace/link.md", "/workspace/sub/b.md")]
    assert program.calls == [("read", {"path": "/workspace/link.md"}),
                             ("read", {"path": "/workspace/sub/b.md", "strict": True})]


def test_a_landing_the_judge_refuses_is_never_opened():
    program = _Script({"ok": False, "code": "lands", "landed": "/workspace/sub/b.md"})

    def refuse(asked, landed):
        raise FileToolError("the policy says no")

    with pytest.raises(FileToolError, match="the policy says no"):
        _run(df.read(program, "/workspace/link.md", judge=refuse))
    assert len(program.calls) == 1


@pytest.mark.parametrize("first", [
    {"ok": False, "code": "lands"},                       # no place named
    {"ok": False, "code": "lands", "landed": 7},
])
def test_a_landing_with_no_place_is_refused(first):
    program = _Script(first)
    with pytest.raises(FileToolError) as refused:
        _run(df.read(program, "/workspace/link.md", judge=_judge([])))
    assert str(refused.value) == df.ELSEWHERE and len(program.calls) == 1


def test_a_second_landing_is_never_followed():
    program = _Script({"ok": False, "code": "lands", "landed": "/workspace/b.md"},
                      {"ok": False, "code": "lands", "landed": "/workspace/c.md"})
    with pytest.raises(FileToolError) as refused:
        _run(df.read(program, "/workspace/link.md", judge=_judge([])))
    assert str(refused.value) == df.ELSEWHERE and len(program.calls) == 2


@pytest.mark.parametrize("answer, sentence", [
    ({"code": "missing"}, "File not found: /workspace/n.md"),
    ({"code": "folder"}, "/workspace/n.md is a folder. Use Glob to list it."),
    ({"code": "binary"}, "/workspace/n.md is a binary file. Read returns text only. "
                         "Documents and images go through the file-tools MCP."),
    ({"code": "special"}, "cannot read /workspace/n.md: it is not a regular file"),
    ({"code": "link"}, "cannot read /workspace/n.md: the path goes through a symlink, so use the real path instead"),
    ({"code": "leaves"}, df.LEAVES),
    ({"code": "elsewhere"}, df.ELSEWHERE),
    ({"code": "refused_name", "name": ".git"}, "/workspace/n.md: the .git subtree is not accessible to tools"),
    ({"code": "not_a_session_folder"}, "/workspace/n.md is not inside a session folder"),
    ({"code": sp.NOT_RUNNING},
     "The file tools cannot reach OtoDock's compute machine right now. Send your message again in a moment."),
    ({"code": sp.NOT_STARTED, "detail": "the tool could not start: no"},
     "The file tools could not start on OtoDock's compute machine. Try again in a moment."),
    ({"code": sp.ENDED},
     "The file tools stopped before they answered. Try again in a moment."),
    ({"code": sp.RESTING, "seconds": 42},
     "The file tools stopped or could not start on OtoDock's compute machine, several times in a row. "
     "They are tried again in 42 seconds."),
    ({"code": sp.TIMED_OUT, "seconds": 30},
     "The file tools did not answer in 30 seconds and were stopped. They start again at the next call. "
     "A search of a very large folder can take that long: name a smaller folder."),
    ({"code": sp.TIMED_OUT, "seconds": "a text where a number belongs"},
     "The file tools did not answer in some seconds and were stopped. They start again at the next call. "
     "A search of a very large folder can take that long: name a smaller folder."),
    ({"code": "os", "detail": "Permission denied"}, "cannot read /workspace/n.md: Permission denied"),
    ({"code": sp.GARBLED}, "cannot read /workspace/n.md: the file tools failed"),
    ({}, "cannot read /workspace/n.md: the file tools failed"),
    ({"ok": "yes", "text": "not a real success"}, "cannot read /workspace/n.md: the file tools failed"),
])
def test_each_refusal_reads_as_its_sentence(answer, sentence):
    program = _Script({"ok": False, **answer})
    with pytest.raises(FileToolError) as refused:
        _run(df.read(program, "/workspace/n.md", judge=_judge([])))
    assert str(refused.value) == sentence


# -- glob ---------------------------------------------------------------------

def test_glob_joins_the_hits_onto_the_asked_folder():
    program = _Script({"ok": True, "rels": ["sub/b.md", "a.md"]})
    assert _run(df.glob(program, "/workspace", " **/*.md ", judge=_judge([]))) == df.Found(
        ["/workspace/a.md", "/workspace/sub/b.md"], 0)
    assert program.calls == [("glob", {"pattern": "**/*.md", "path": "/workspace"})]


def test_glob_keeps_only_hits_below_the_folder_and_at_most_the_cap():
    false = ["ok.md", "../up.md", "/abs.md", "a/../../b.md", 7, "", "a//b.md"]
    assert _run(df.glob(_Script({"ok": True, "rels": false}), "/workspace", "*", judge=_judge([]))).paths == [
        "/workspace/ok.md"]
    many = [f"f{i:04d}.md" for i in range(df.GLOB_MAX_ENTRIES + 50)]
    assert len(_run(df.glob(_Script({"ok": True, "rels": many}), "/workspace", "*", judge=_judge([]))).paths) \
        == df.GLOB_MAX_ENTRIES
    assert _run(df.glob(_Script({"ok": True, "rels": "no list"}), "/workspace", "*", judge=_judge([]))).paths == []


@pytest.mark.parametrize("unread, kept", [
    (3, 3), (1, 1), (0, 0), (None, 0), (True, 0), (-2, 0), ("many", 0), (2.5, 0),
])
def test_glob_keeps_the_count_of_folders_that_could_not_be_opened(unread, kept):
    answer = {"ok": True, "rels": ["a.md"]} if unread is None else {"ok": True, "rels": ["a.md"], "unread": unread}
    assert _run(df.glob(_Script(answer), "/workspace", "*", judge=_judge([]))) == df.Found(["/workspace/a.md"], kept)


@pytest.mark.parametrize("pattern, sentence", [
    ("", "pattern is required (e.g. **/*.md)"), ("   ", "pattern is required (e.g. **/*.md)"),
    ("/abs/*.md", "pattern must be relative to the folder (e.g. **/*.md)"),
    ("../*.md", "pattern must be relative to the folder (e.g. **/*.md)"),
])
def test_glob_refuses_a_pattern_before_any_call(pattern, sentence):
    program = _Script()
    with pytest.raises(FileToolError) as refused:
        _run(df.glob(program, "/workspace", pattern, judge=_judge([])))
    assert str(refused.value) == sentence and program.calls == []


def test_glob_of_a_folder_link_walks_where_it_lands():
    program = _Script({"ok": False, "code": "lands", "landed": "/workspace/sub"}, {"ok": True, "rels": ["b.md"]})
    seen: list = []
    assert _run(df.glob(program, "/workspace/alias", "*", judge=_judge(seen))).paths == ["/workspace/alias/b.md"]
    assert seen == [("/workspace/alias", "/workspace/sub")]
    assert program.calls[1] == ("glob", {"pattern": "*", "path": "/workspace/sub", "strict": True})


def test_glob_of_what_is_no_folder_reads_as_its_sentence():
    with pytest.raises(FileToolError, match="/workspace/n.md is not a folder"):
        _run(df.glob(_Script({"ok": False, "code": "not_a_folder"}), "/workspace/n.md", "*", judge=_judge([])))


# -- skills -------------------------------------------------------------------

def test_skill_is_none_for_a_skill_that_is_not_there_and_a_sentence_for_a_program_that_is_not():
    assert _run(df.skill(_Script({"ok": True, "text": "body"}), "alpha")) == "body"
    for code in ("missing", "too_large", "link", "special", "folder"):
        assert _run(df.skill(_Script({"ok": False, "code": code}), "alpha")) is None
    with pytest.raises(FileToolError) as refused:
        _run(df.skill(_Script({"ok": False, "code": sp.NOT_RUNNING}), "alpha"))
    assert str(refused.value) == df.NOT_REACHABLE


def test_skill_names_are_the_programs_list():
    assert _run(df.skill_names(_Script({"ok": True, "names": ["b", "a", 7]}))) == ["a", "b"]
    with pytest.raises(FileToolError):
        _run(df.skill_names(_Script({"ok": False, "code": sp.TIMED_OUT})))


# -- the facts ----------------------------------------------------------------

def test_the_facts_name_the_rows_without_the_refused_folders():
    binds = tuple(render.Bind(render.ROOT_AGENT, rel, sandbox, True) for rel, sandbox in [
        ("workspace", "/workspace"), ("users/alice", "/users/alice/"),
        ("users/alice/.claude/permission_gate.py", "/users/alice/.claude/permission_gate.py"),
        ("workspace/.claude", "/workspace/.claude"), ("knowledge/.credentials", "/knowledge/.credentials")])
    description = render.SandboxDescription(agent=AGENT, binds=binds, mcp_binds=(), mcps_root="", extra_binds=(),
                                            cwd="/users/alice", proxy_port=8400, forwards=("proxy",))

    class _Builder:
        def get_env_overrides(self):
            return {"CLAUDE_CONFIG_DIR": "/users/alice/.claude"}

    facts = df.program_facts(description, _Builder())
    assert facts["rows"] == ["/users/alice", "/workspace"]
    assert facts["refused"] == sorted(df.REFUSED_COMPONENTS) and facts["glob_skip"] == sorted(df.GLOB_SKIP_DIRS)
    assert facts["read_max"] == df.READ_MAX_BYTES and facts["glob_max"] == df.GLOB_MAX_ENTRIES
    assert facts["skills"] == "/users/alice/.claude/skills" and facts["skill_max"] > 0


def test_the_file_tools_program_is_one_spec_of_the_general_part():
    assert isinstance(df.PROGRAM, sp.ProgramSpec) and df.PROGRAM.name == "oto:files"
    assert df.PROGRAM.facts is df.program_facts and df.PROGRAM.starts_with_call
    assert "OPERATIONS = " in df.PROGRAM.source


# -- the handlers -------------------------------------------------------------

@pytest.fixture
def session(tmp_path, monkeypatch):
    import config
    agents = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    for folder in ("config", "workspace", "knowledge", "users/alice/workspace", "users/alice/context",
                   "users/alice/.claude"):
        (agents / AGENT / folder).mkdir(parents=True)
    (tmp_path / "mcps").mkdir()
    cfg = SandboxConfig(
        role="manager", username="alice", agent_name=AGENT, is_admin_agent=False,
        host_agents_dir=agents.resolve(), host_mcps_dir=(tmp_path / "mcps").resolve(),
        host_claude_dir=(agents / AGENT / "users/alice/.claude").resolve(), net_forwards=["8400"])

    class _Session:
        session_id = "direct-file-tools"
        sandbox_cfg = cfg
        mcp_manager = None
        catalog = None
        _table = None

        def mount_table(self):
            if self._table is None:
                self._table = df.mount_table(cfg)
            return self._table

    set_session_security(_Session.session_id, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    yield _Session()
    cleanup_session_permission_state(_Session.session_id)


def _scripted(monkeypatch, *answers):
    program = _Script(*answers)

    async def call_program(_session, operation, args, *, then=None):
        return await program(operation, args, then)

    monkeypatch.setattr(df, "call_program", call_program)
    return program


def test_read_goes_to_the_program_with_the_normalized_path(session, monkeypatch):
    program = _scripted(monkeypatch, {"ok": True, "text": "hi\n", "truncated": False})
    assert _run(B.execute(session, "Read", {"file_path": "workspace/../workspace/n.md"})) == "     1\thi"
    assert program.calls == [("read", {"path": "/users/alice/workspace/n.md"})]


@pytest.mark.parametrize("path, text", [
    ("/workspace/.claude/settings.json", "the .claude subtree is not accessible"),
    ("/etc/passwd", "is not inside a session folder"),
    ("~/notes.md", "'~' is not a session folder here"),
    ("", "file_path is required"),
])
def test_read_refuses_on_the_text_before_any_call(session, monkeypatch, path, text):
    program = _scripted(monkeypatch)
    assert text in _run(B.execute(session, "Read", {"file_path": path}))
    assert program.calls == []


def test_a_landing_goes_through_pass_one_before_it_is_opened(session, monkeypatch):
    from auth import path_policy
    judged: list = []

    def check(tool, tool_input, ctx):
        judged.append((tool, tool_input))
        return PathDecision(False, "the policy refuses that place"), None

    monkeypatch.setattr(path_policy, "check_tool_access", check)
    program = _scripted(monkeypatch, {"ok": False, "code": "lands", "landed": "/workspace/sub/b.md"})
    out = _run(B.execute(session, "Read", {"file_path": "/workspace/link.md"}))
    assert out == "Error: the policy refuses that place"
    assert judged == [("Read", {"file_path": "/workspace/sub/b.md"})] and len(program.calls) == 1


def test_a_landing_is_refused_once_the_sessions_context_is_gone(session, monkeypatch):
    calls: list = []

    async def call_program(_session, operation, args):
        calls.append((operation, args))
        # The session ends between the gate and the landing.
        cleanup_session_permission_state(session.session_id)
        return {"ok": False, "code": "lands", "landed": "/workspace/sub/b.md"}

    monkeypatch.setattr(df, "call_program", call_program)
    out = _run(B.execute(session, "Read", {"file_path": "/workspace/link.md"}))
    assert out == "Error: Session is no longer active. Send a new message to continue."
    assert calls == [("read", {"path": "/workspace/link.md"})]      # nothing is opened: no second call


@pytest.mark.parametrize("landed, sentence", [
    ("/users/alice/workspace/b.md", df.LEAVES),
    ("/workspace/.claude/settings.json", df.ELSEWHERE),
    ("/proc/self/environ", df.ELSEWHERE),
])
def test_a_false_landing_opens_nothing(session, monkeypatch, landed, sentence):
    program = _scripted(monkeypatch, {"ok": False, "code": "lands", "landed": landed},
                        {"ok": True, "text": "must never be asked for", "truncated": False})
    assert _run(B.execute(session, "Read", {"file_path": "/workspace/link.md"})) == f"Error: {sentence}"
    assert len(program.calls) == 1


def test_a_session_whose_program_cannot_be_reached_is_told_so(session):
    assert _run(B.execute(session, "Read", {"file_path": "/workspace/n.md"})) == f"Error: {df.NOT_REACHABLE}"
    assert _run(B.execute(session, "Glob", {"pattern": "*"})) == f"Error: {df.NOT_REACHABLE}"


def test_a_session_with_no_sandbox_has_no_file_tools(session, monkeypatch):
    program = _scripted(monkeypatch)
    session.sandbox_cfg = None
    assert "no sandbox" in _run(B.execute(session, "Read", {"file_path": "/workspace/n.md"}))
    assert "no sandbox" in _run(B.execute(session, "Skill", {"name": "alpha"}))
    assert program.calls == []


def test_glob_searches_the_sessions_folder_when_none_is_named(session, monkeypatch):
    program = _scripted(monkeypatch, {"ok": True, "rels": ["a.md"]}, {"ok": True, "rels": []})
    assert _run(B.execute(session, "Glob", {"pattern": "*.md"})) == "/users/alice/a.md"
    assert program.calls[0] == ("glob", {"pattern": "*.md", "path": "/users/alice"})
    assert _run(B.execute(session, "Glob", {"pattern": "*.md", "path": "/workspace"})) == "(no matches)"


def test_glob_says_where_it_stopped(session, monkeypatch):
    many = [f"f{i:04d}.md" for i in range(df.GLOB_MAX_ENTRIES)]
    _scripted(monkeypatch, {"ok": True, "rels": many})
    out = _run(B.execute(session, "Glob", {"pattern": "*", "path": "/workspace"}))
    assert out.endswith(f"... (stopped at {df.GLOB_MAX_ENTRIES} entries: narrow the pattern)")


def test_glob_says_what_it_could_not_open(session, monkeypatch):
    many = [f"f{i:04d}.md" for i in range(df.GLOB_MAX_ENTRIES)]
    _scripted(monkeypatch, {"ok": True, "rels": ["a.md"], "unread": 2}, {"ok": True, "rels": [], "unread": 1},
              {"ok": True, "rels": many, "unread": 4}, {"ok": True, "rels": ["a.md"]})

    def glob():
        return _run(B.execute(session, "Glob", {"pattern": "*", "path": "/workspace"}))

    assert glob() == "/workspace/a.md\n... (2 folders could not be opened and were not searched)"
    assert glob() == "(no matches)\n... (1 folder could not be opened and was not searched)"
    assert glob().endswith(f"... (stopped at {df.GLOB_MAX_ENTRIES} entries: narrow the pattern)\n"
                           "... (4 folders could not be opened and were not searched)")
    assert glob() == "/workspace/a.md"                    # a search that opened every folder says nothing more


def test_the_platform_keeps_its_own_caps_on_an_answer():
    """The program's caps are the first line. The platform keeps its own on
    what it takes out of an answer, so a program that is no longer ours can
    hand it no more than one of ours."""
    from core.sandbox import skills_materializer
    big = "x" * (df.READ_MAX_BYTES + 5000)
    out = _run(df.read(_Script({"ok": True, "text": big, "truncated": False}), "/workspace/n.md", judge=_judge([])))
    assert out.count("x") == df.READ_MAX_BYTES and out.endswith(f"... (read stopped at {df.READ_MAX_BYTES // 1024} KB)")
    exact = "x" * df.READ_MAX_BYTES
    out = _run(df.read(_Script({"ok": True, "text": exact, "truncated": False}), "/workspace/n.md", judge=_judge([])))
    assert out.count("x") == df.READ_MAX_BYTES and "read stopped" not in out
    cap = skills_materializer._MAX_SKILL_BYTES
    assert len(_run(df.skill(_Script({"ok": True, "text": "y" * (cap + 1)}), "alpha"))) == cap
    long = "d/" * 3000 + "f.md"
    found = _run(df.glob(_Script({"ok": True, "rels": [long, "ok.md"]}), "/workspace", "*", judge=_judge([])))
    assert found.paths == ["/workspace/ok.md"]
    for answer in ({"ok": False, "code": "os", "detail": "z" * 5000},
                   {"ok": False, "code": "refused_name", "name": "n" * 5000}):
        with pytest.raises(FileToolError) as refused:
            _run(df.read(_Script(answer), "/workspace/n.md", judge=_judge([])))
        assert len(str(refused.value)) < 300


def test_skill_returns_the_body_without_its_frontmatter(session, monkeypatch):
    program = _scripted(monkeypatch, {"ok": True, "text": "---\nname: alpha\n---\nThe body.\n"})
    assert _run(B.execute(session, "Skill", {"name": " alpha "})) == "# Skill: alpha\n\nThe body."
    assert program.calls == [("skill", {"name": "alpha"})]


def test_an_unknown_skill_names_what_the_session_has(session, monkeypatch):
    program = _scripted(monkeypatch, {"ok": False, "code": "missing"}, {"ok": True, "names": ["alpha", "beta"]})
    out = _run(B.execute(session, "Skill", {"name": "gamma"}))
    assert out == "Error: unknown skill 'gamma'. Available: alpha, beta"
    assert [c[0] for c in program.calls] == ["skill", "skills"]


def test_a_name_that_is_no_skill_id_never_reaches_the_program_as_a_skill(session, monkeypatch):
    program = _scripted(monkeypatch, {"ok": True, "names": []})
    out = _run(B.execute(session, "Skill", {"name": "../workspace"}))
    assert out == ("Error: name must be a skill id from the # Skills list. "
                   "No on-demand skills are enabled for this session.")
    assert [c[0] for c in program.calls] == ["skills"]


def test_a_skill_the_program_cannot_be_reached_for_says_so(session, monkeypatch):
    _scripted(monkeypatch, {"ok": False, "code": sp.NOT_RUNNING})
    assert _run(B.execute(session, "Skill", {"name": "alpha"})) == f"Error: {df.NOT_REACHABLE}"


# -- a path inside a row, judged twice in kind ------------------------------------

@pytest.mark.parametrize("path", [
    "/workspace", "/workspace/notes.md", "/workspace/sub/deep/a.md", "/users/alice/x.md",
    "/users/alice/workspace/a/b.md", "/workspace/.claudex/a.md", "/workspace/git/config",
    "/", "/users", "/users/bob/x.md", "/usersx/alice/x.md", "/workspacex/b.md", "/etc/passwd",
    "/workspace/.claude/settings.json", "/workspace/sub/.git/config", "/users/alice/.credentials/k",
    "/users/alice/workspace/.codex",
])
def test_the_text_check_and_the_program_admit_the_same_paths(path):
    """``locate`` words an early refusal on the platform, and the file
    program admits a path beside the kernel. Neither can load the other, so
    this table holds the two to the same verdict and the same row for a
    path the platform sends."""
    from core.layers.direct import file_program

    binds = tuple(render.Bind(render.ROOT_AGENT, m.host, m.sandbox, m.rw) for m in MOUNTS)
    description = render.SandboxDescription(agent=AGENT, binds=binds, mcp_binds=(), mcps_root="", extra_binds=(),
                                            cwd="/workspace", proxy_port=8400, forwards=("proxy",))

    class _Builder:
        def get_env_overrides(self):
            return {}

    facts = df.program_facts(description, _Builder())
    try:
        _vpath, mount = df.locate(MOUNTS, path, cwd="/")
        platform = mount.sandbox.rstrip("/") or "/"
    except FileToolError:
        platform = None
    try:
        program = file_program._admit(facts, path)
    except file_program._No:
        program = None
    assert platform == program, path


# -- a search's pattern, judged twice in kind -------------------------------------

@pytest.mark.parametrize("pattern", [
    "**/*.md", "a/b.md", " *.md ", "a/..b", "...", "", "   ", "/abs/*.md", " /abs", "../x", "a/../b", "..",
])
def test_the_platform_and_the_program_refuse_the_same_patterns(tmp_path, pattern):
    """``glob_pattern`` refuses a pattern before any call, and the file
    program refuses it again beside the kernel. Neither can load the other,
    so this table holds the two to the same verdict."""
    from core.layers.direct import file_program

    root = str(tmp_path.resolve())
    facts = {"rows": [root], "refused": [], "glob_skip": [], "glob_max": 5}
    try:
        df.glob_pattern(pattern)
        platform = True
    except FileToolError:
        platform = False
    answer = file_program.OPERATIONS["glob"](facts, {"path": root, "pattern": pattern})
    assert (answer.get("ok") is True) is platform, pattern
    assert (answer.get("code") == "bad_pattern") is not platform, pattern


# -- the place a link lands, judged twice in kind ----------------------------------

def test_the_program_and_the_platform_judge_a_landing_alike(tmp_path):
    """The program names where a path through a link lands only when that
    place stays in the path's row and off the refused names, and
    ``files.landing`` judges the named place again by the same rules, so
    that no answer of the program widens what is opened. One tree holds the
    two to the same verdict. Where both refuse, the word may differ: a place
    in no row at all "leaves" for the program and is "elsewhere" for the
    platform."""
    from core.layers.direct import file_program

    root = tmp_path.resolve()
    ws, other, outside = root / "ws", root / "other", root / "outside"
    for folder in (ws / "sub", ws / ".git", other, outside):
        folder.mkdir(parents=True)
    targets = {"in.md": ws / "sub" / "b.md", "out.md": other / "o.md", "away.md": outside / "s.md",
               "hid.md": ws / ".git" / "config", "deep.md": ws / "sub" / "deeper" / "c.md"}
    (ws / "sub" / "deeper").mkdir()
    for target in targets.values():
        target.write_text("x\n")
    mounts = [Mount(str(ws), str(ws), True), Mount(str(other), str(other), True)]
    facts = {"rows": [str(ws), str(other)], "refused": sorted(df.REFUSED_COMPONENTS)}
    verdicts = {}
    for name, target in targets.items():
        (ws / name).symlink_to(target)
        asked = str(ws / name)
        with pytest.raises(file_program._No) as no:          # a path through a link is never opened
            file_program._landing(facts, asked, str(ws), False)
        answer = no.value.answer
        try:
            df.landing(mounts, asked, str(target))
            platform = "lands"
        except FileToolError as refused:
            platform = str(refused)
        assert (answer["code"] == "lands") is (platform == "lands"), name
        if answer["code"] == "lands":
            assert answer["landed"] == str(target)
        verdicts[name] = (answer["code"], platform)
    assert verdicts == {
        "in.md": ("lands", "lands"), "deep.md": ("lands", "lands"),
        "out.md": ("leaves", df.LEAVES), "hid.md": ("elsewhere", df.ELSEWHERE),
        "away.md": ("leaves", df.ELSEWHERE),
    }


# -- a search's walk, written twice in kind ----------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root opens every folder")
def test_the_programs_search_and_safe_fs_walk_the_same_tree(tmp_path):
    """``safe_fs.walk_beneath`` walks for the proxy's own process, the file
    program's search inside a sandbox. Neither can load the other, so one
    tree holds the two to the same walk: the same files, no link entered,
    and the same folder told as not opened."""
    from core.layers.direct import file_program
    from services.infra import safe_fs

    root = tmp_path.resolve() / "ws"
    for folder in ("sub/deep", "locked", "empty"):
        (root / folder).mkdir(parents=True)
    for name in ("a.md", "sub/b.md", "sub/deep/c.md", "locked/in.md"):
        (root / name).write_text("x\n")
    (root / "linked").symlink_to(root / "sub")
    (root / "locked").chmod(0)
    try:
        answer = file_program.OPERATIONS["glob"](
            {"rows": [str(root)], "refused": [], "glob_skip": [], "glob_max": 50}, {"path": str(root), "pattern": "**/*"})
        errors: list = []
        files = [f"{step.rel}/{name}" if step.rel else name
                 for step in safe_fs.walk_beneath(root, "", onerror=errors.append) for name in step.files]
    finally:
        (root / "locked").chmod(0o700)
    assert answer["rels"] == sorted(files) == ["a.md", "sub/b.md", "sub/deep/c.md"]
    assert answer["unread"] == len(errors) == 1


# -- the walk with no link followed, written twice in kind ----------------------

def _link_cases(root):
    """One tree for both opens: what each must open and what each must refuse."""
    import os
    (root / "sub").mkdir()
    (root / "plain.md").write_text("plain\n")
    (root / "sub" / "deep.md").write_text("deep\n")
    (root / "link.md").symlink_to(root / "plain.md")
    (root / "alias").symlink_to(root / "sub")
    (root / "dangling.md").symlink_to(root / "nowhere.md")
    os.mkfifo(root / "pipe")
    return [("plain.md", True), ("sub/deep.md", True), ("link.md", False), ("alias/deep.md", False),
            ("dangling.md", False), ("pipe", False), ("sub", False), ("gone.md", False), ("plain.md/x", False)]


def test_the_programs_open_and_safe_fs_keep_the_same_rules(tmp_path):
    """``safe_fs`` opens for the proxy's own process, the file program's
    walk inside a sandbox. Neither can load the other, so this table holds
    the two to the same verdicts: no link followed at any part, no pipe
    waited on, regular files only."""
    import os
    import time

    from core.layers.direct import file_program
    from services.infra import safe_fs

    root = tmp_path.resolve()
    for rel, opens in _link_cases(root):
        began = time.monotonic()
        try:
            fd, _stat = safe_fs.open_regular_for_read(root, rel)
            os.close(fd)
            platform = True
        except OSError:
            platform = False
        try:
            os.close(file_program._open_file(str(root / rel)))
            program = True
        except (file_program._No, OSError):
            program = False
        assert platform is opens and program is opens, rel
        assert time.monotonic() - began < 2, rel


# -- Write, Edit and the delete's look: the platform's half -------------------

async def _nothing(answer):
    return None


async def _nothing_at(path, answer):
    return None


def test_write_sends_the_text_once_and_answers_what_the_program_said():
    program = _Script({"ok": True, "bytes": 4, "created": True}, {"ok": True, "bytes": 4, "created": False})
    assert _run(df.write(program, "/workspace/n.md", "one\n", then=_nothing)) == (True, 4)
    assert _run(df.write(program, "/workspace/n.md", "two\n", then=_nothing)) == (False, 4)
    assert program.calls == [("write", {"path": "/workspace/n.md", "content": "one\n"}),
                             ("write", {"path": "/workspace/n.md", "content": "two\n"})]


@pytest.mark.parametrize("content, sentence", [
    (7, "content must be a string"),
    ("x" * (df.WRITE_MAX_BYTES + 1),
     "content is 1024 KB. Write accepts up to 1024 KB. Use the file-tools MCP for larger documents."),
    ("é" * (df.WRITE_MAX_BYTES // 2 + 1),
     "content is 1024 KB. Write accepts up to 1024 KB. Use the file-tools MCP for larger documents."),
    ("a\ud800b", "content holds a character that cannot be written as text"),
])
def test_write_refuses_before_any_call_what_cannot_be_written(content, sentence):
    program = _Script()
    with pytest.raises(FileToolError) as refused:
        _run(df.write(program, "/workspace/n.md", content, then=_nothing))
    assert str(refused.value) == sentence and program.calls == []


def test_a_path_that_is_no_text_is_refused_on_the_text():
    for raw in ("/workspace/a\ud800.md", "notes\udfff.md"):
        with pytest.raises(FileToolError) as refused:
            df.locate(MOUNTS, raw, cwd="/workspace")
        assert str(refused.value) == "file_path holds a character that cannot be written as text"


def test_a_write_at_its_cap_is_sent():
    program = _Script({"ok": True, "bytes": df.WRITE_MAX_BYTES, "created": True})
    assert _run(df.write(program, "/workspace/n.md", "x" * df.WRITE_MAX_BYTES, then=_nothing)) == (
        True, df.WRITE_MAX_BYTES)
    assert df.WRITE_MAX_BYTES == 1024 * 1024        # what the relay's test sends in its worst text


def test_edit_sends_both_strings_and_answers_the_count():
    program = _Script({"ok": True, "count": 3})
    assert _run(df.edit(program, "/workspace/n.md", "a", "b", True, then=_nothing)) == 3
    assert program.calls == [("edit", {"path": "/workspace/n.md", "old": "a", "new": "b", "replace_all": True})]


@pytest.mark.parametrize("old, new, sentence", [
    ("", "b", "old_string is required"),
    (None, "b", "old_string is required"),
    ("a", None, "new_string must be a string"),
    ("a", "a", "old_string and new_string are identical. There is nothing to change."),
    ("a\udfff", "b", "old_string holds a character that cannot be written as text"),
    ("a", "b\ud800", "new_string holds a character that cannot be written as text"),
    ("a" * (df.WRITE_MAX_BYTES // 2), "b" * (df.WRITE_MAX_BYTES // 2 + 1),
     "old_string and new_string together are larger than 1024 KB. Edit works on text files up to that size."),
])
def test_edit_refuses_before_any_call_what_cannot_be_an_edit(old, new, sentence):
    program = _Script()
    with pytest.raises(FileToolError) as refused:
        _run(df.edit(program, "/workspace/n.md", old, new, then=_nothing))
    assert str(refused.value) == sentence and program.calls == []


def test_an_edit_at_its_bounds_is_sent():
    program = _Script({"ok": True, "count": 1})
    half = df.WRITE_MAX_BYTES // 2
    assert _run(df.edit(program, "/workspace/n.md", "a" * half, "b" * half, then=_nothing)) == 1


def test_an_edit_the_program_refused_raises_its_sentence():
    """Only the program's own ``ok`` is a success. Every other answer is a
    refusal with its sentence, also one that names a count beside it."""
    for answer in ({"ok": False, "code": "not_found_text"}, {"ok": False, "code": "not_found_text", "count": 2}):
        with pytest.raises(FileToolError) as refused:
            _run(df.edit(_Script(answer), "/workspace/n.md", "a", "b", then=_nothing))
        assert str(refused.value) == "old_string was not found in /workspace/n.md"
    for answer in ({}, {"count": 1}, {"ok": None, "count": 1}, {"ok": "yes", "count": 1}, {"ok": 1, "count": 1}):
        with pytest.raises(FileToolError, match="cannot edit /workspace/n.md"):
            _run(df.edit(_Script(answer), "/workspace/n.md", "a", "b", then=_nothing))


def test_an_edit_the_program_called_done_without_a_count_says_what_is_known(session, monkeypatch):
    """The program said that it changed the file, and the record is made on
    that word. So the tool never says that the edit failed: it says that
    the file was changed and that the count is not known."""
    for count in (None, "3", True, 1.0, -1, 0):
        assert _run(df.edit(_Script({"ok": True, "count": count}), "/workspace/n.md", "a", "b", then=_nothing)) is None
    recorded: list = []

    async def _push(agent, rel, host, *, writer=None):
        recorded.append(rel)

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)
    _scripted(monkeypatch, {"ok": True})
    said = _run(B.execute(session, "Edit", {"file_path": "/workspace/n.md", "old_string": "a", "new_string": "b"}))
    assert said == ("Edited /workspace/n.md. The file tools did not say how many places changed. Read it to see.")
    assert recorded == ["workspace/n.md"]


def test_a_write_that_times_out_while_its_restart_raises_reads_as_not_known(
        world, monkeypatch):  # noqa: F811
    """The general part and the file tools' sentences together: the program
    was asked, so whatever fails after that, the tool says that it is not
    known whether the file was written, and the record's side gets the
    same answer."""
    machine, counted = world

    async def run():
        program = _a_program()
        assert (await program.call("echo", {}))["ok"] is True
        _no_give_back(monkeypatch, counted)
        machine.silent, machine.hold_close = True, True
        seen: list = []

        async def then(answer):
            seen.append(answer)

        async def call(operation, args, then=None):
            return await program.call("echo", args, then)       # the stand-in machine's program, silent from here

        with pytest.raises(FileToolError) as said:
            await asyncio.wait_for(df.write(call, "/workspace/n.md", "x", then=then), 10)
        assert str(said.value) == _NOT_KNOWN["write"]
        assert seen == [{"ok": False, "code": sp.TIMED_OUT, "seconds": 1}]
        machine.hold_close = False
        await program.close()
    asyncio.run(run())


@pytest.mark.parametrize("verb, answer, sentence", [
    ("write", {"code": "folder"}, "/workspace/n.md is a folder"),
    ("write", {"code": "link_at_name"}, "/workspace/n.md is a symlink. Write the real path instead."),
    ("write", {"code": "link"},
     "cannot write /workspace/n.md: the path goes through a symlink, so use the real path instead"),
    ("write", {"code": "special"}, "cannot write /workspace/n.md: it is not a regular file"),
    ("write", {"code": "read_only"},
     "cannot write /workspace/n.md: the file system refused the write (read-only or no permission)"),
    ("write", {"code": "denied"},
     "cannot write /workspace/n.md: the file system refused the write (read-only or no permission)"),
    ("write", {"code": "no_space"},
     "cannot write /workspace/n.md: not enough storage in the agent's bucket for this write"),
    ("write", {"code": "too_large"}, "content is larger than 1024 KB. Write accepts up to that size."),
    ("write", {"code": "bad_content"}, "cannot write /workspace/n.md: the text holds a character that cannot be written"),
    ("write", {"code": "os", "detail": "Not a directory"}, "cannot write /workspace/n.md: Not a directory"),
    ("write", {"code": "refused_name", "name": ".git"}, "/workspace/n.md: the .git subtree is not accessible to tools"),
    ("write", {"code": "not_a_session_folder"}, "/workspace/n.md is not inside a session folder"),
    ("edit", {"code": "missing"}, "File not found: /workspace/n.md"),
    ("edit", {"code": "folder"}, "/workspace/n.md is a folder"),
    ("edit", {"code": "binary"}, "/workspace/n.md is a binary file. Edit works on text files."),
    ("edit", {"code": "not_text"}, "/workspace/n.md is not valid UTF-8 text"),
    ("edit", {"code": "too_large"}, "/workspace/n.md is larger than 1024 KB. Edit works on text files up to that size."),
    ("edit", {"code": "not_found_text"}, "old_string was not found in /workspace/n.md"),
    ("edit", {"code": "many", "count": 3},
     "old_string appears 3 times in /workspace/n.md. Add surrounding context to make it unique, or set replace_all."),
    ("edit", {"code": "many", "count": "a text where a number belongs"},
     "old_string appears several times in /workspace/n.md. Add surrounding context to make it unique, "
     "or set replace_all."),
    ("edit", {"code": "grows"}, "cannot edit /workspace/n.md: the result would be larger than 1024 KB"),
    ("edit", {"code": "changed"}, "/workspace/n.md changed while Edit worked on it. Nothing was written. Read it again."),
    ("delete", {"code": "missing"}, "File not found: /workspace/n.md"),
    ("delete", {"code": "folder"}, "/workspace/n.md is a folder. Delete removes single files."),
    ("delete", {"code": "special"}, "/workspace/n.md is not a regular file. Delete removes regular files only."),
    ("delete", {"code": "read_only"},
     "cannot delete /workspace/n.md: the file system refused the write (read-only or no permission)"),
])
def test_each_refusal_of_a_change_reads_as_its_sentence(verb, answer, sentence):
    assert str(df.refusal({"ok": False, **answer}, "/workspace/n.md", verb=verb)) == sentence


_NOT_KNOWN = {"write": "It is not known whether /workspace/n.md was written: the file tools gave no answer. "
                       "Read it to see.",
              "edit": "It is not known whether /workspace/n.md was changed: the file tools gave no answer. "
                      "Read it to see."}


@pytest.mark.parametrize("verb", ["write", "edit"])
@pytest.mark.parametrize("code", [sp.TIMED_OUT, sp.ENDED, sp.GARBLED, sp.CLOSED])
def test_a_change_that_was_asked_and_not_answered_never_says_that_nothing_was_written(verb, code):
    said = str(df.refusal({"ok": False, "code": code, "seconds": 30}, "/workspace/n.md", verb=verb))
    assert said == _NOT_KNOWN[verb]


@pytest.mark.parametrize("verb", ["write", "edit"])
@pytest.mark.parametrize("answer, sentence", [
    ({"code": sp.NOT_RUNNING}, df.NOT_REACHABLE),
    ({"code": sp.NOT_STARTED, "detail": "no"}, df.NOT_STARTED),
    ({"code": sp.RESTING, "seconds": 42}, df.RESTING.format(seconds=42)),
    ({"code": sp.UNSENT}, "The file tools could not send this call: it is too large, or it holds a character "
                          "that is no text. Nothing was changed."),
])
def test_a_change_the_program_was_never_asked_for_says_what_is_true(verb, answer, sentence):
    said = str(df.refusal({"ok": False, **answer}, "/workspace/n.md", verb=verb))
    assert said == sentence and "not known" not in said


def test_a_look_that_got_no_answer_reads_as_a_read_does():
    # The delete's look changes nothing: with no answer nothing was removed, and the sentence is the read's.
    assert str(df.refusal({"ok": False, "code": sp.ENDED}, "/workspace/n.md", verb="delete")) == df.STOPPED
    assert str(df.refusal({"ok": False, "code": sp.CLOSED}, "/workspace/n.md", verb="read")) == df.NOT_REACHABLE


def test_what_follows_a_write_gets_the_programs_answer_whatever_it_is():
    seen: list = []

    async def then(answer):
        seen.append(answer)

    refused = {"ok": False, "code": "read_only"}
    with pytest.raises(FileToolError):
        _run(df.write(_Script(refused), "/workspace/n.md", "x", then=then))
    done = {"ok": True, "bytes": 1, "created": True}
    _run(df.write(_Script(done), "/workspace/n.md", "x", then=then))
    assert seen == [refused, done]


def test_the_look_keeps_the_last_name_and_rides_each_call_with_the_path_it_asked_for():
    seen: list = []

    async def then(path, answer):
        seen.append((path, answer.get("code") or "ok"))

    judged: list = []
    facts = {"ok": True, "size": 4, "inode": 7, "changed": 9}
    program = _Script({"ok": False, "code": "lands", "landed": "/workspace/sub/n.md"}, facts)
    assert _run(df.look(program, "/workspace/alias/n.md", judge=_judge(judged), then=then)) == facts
    assert judged == [("/workspace/alias/n.md", "/workspace/sub/n.md")]
    assert program.calls == [("stat", {"path": "/workspace/alias/n.md"}),
                             ("stat", {"path": "/workspace/sub/n.md", "strict": True})]
    assert seen == [("/workspace/alias/n.md", "lands"), ("/workspace/sub/n.md", "ok")]


@pytest.mark.parametrize("landed", [
    "/workspace/sub/other.md", "/workspace/sub", "/workspace/sub/n.md/", "/workspace/sub/n.md/x", "/workspace/n.MD",
])
def test_a_look_that_lands_under_another_last_name_is_never_asked_again(landed):
    """The name a Delete removes is the name the model gave and the approval
    showed. The platform holds that itself: a landing under another last
    name is refused before the place is judged or looked at."""
    seen: list = []

    async def then(path, answer):
        seen.append((path, answer.get("code") or "ok"))

    judged: list = []
    program = _Script({"ok": False, "code": "lands", "landed": landed},
                      {"ok": True, "size": 4, "inode": 7, "changed": 9})
    with pytest.raises(FileToolError) as refused:
        _run(df.look(program, "/workspace/alias/n.md", judge=_judge(judged), then=then))
    assert str(refused.value) == df.ELSEWHERE
    assert program.calls == [("stat", {"path": "/workspace/alias/n.md"})]
    assert judged == [] and seen == [("/workspace/alias/n.md", "lands")]


@pytest.mark.parametrize("answer", [
    {"size": 1, "inode": 2}, {"size": True, "inode": 2, "changed": 3}, {"size": 1, "inode": "2", "changed": 3},
    {"size": 1, "inode": 2, "changed": -3}, {"size": 1.0, "inode": 2, "changed": 3}, {},
])
def test_values_that_are_not_three_whole_numbers_are_no_file(answer):
    with pytest.raises(FileToolError):
        df.file_facts({"ok": True, **answer})


def test_the_three_values_are_handed_on_as_they_are():
    big = 2 ** 63 + 5
    assert df.file_facts({"ok": True, "size": 0, "inode": big, "changed": 1_763_000_000_123_456_789}) == (
        0, big, 1_763_000_000_123_456_789)


def test_a_landing_for_a_write_must_be_writable_on_the_text():
    df.landing(MOUNTS, "/workspace/alias/n.md", "/workspace/sub/n.md", writing=True)
    nested = [*MOUNTS, Mount("/host/pa/workspace/lib", "/workspace/lib", False)]
    df.landing(nested, "/workspace/alias/n.md", "/workspace/lib/n.md")            # a read may land there
    with pytest.raises(FileToolError) as refused:
        df.landing(nested, "/workspace/alias/n.md", "/workspace/lib/n.md", writing=True)
    assert str(refused.value) == df.ELSEWHERE


def test_the_bookkeepings_place_is_read_from_the_rows_text(tmp_path, monkeypatch):
    import config
    agents = tmp_path / "agents"
    (agents / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    home = str(agents / AGENT)
    from core.sandbox.sandbox import empty_mount_dir
    mounts = [Mount(f"{home}/workspace", "/workspace", True),
              Mount(f"{home}/users/alice/workspace", "/users/alice/workspace", True),
              Mount(str(empty_mount_dir()), "/workspace/hidden", False),
              Mount(os.devnull, "/workspace/null.md", False),
              Mount(str(tmp_path / "elsewhere"), "/other", True)]
    assert df.record_place(mounts, "/workspace/out/r.md", AGENT) == (
        "workspace/out/r.md", agents / AGENT / "workspace" / "out" / "r.md")
    assert df.record_place(mounts, "/users/alice/workspace/n.md", AGENT)[0] == "users/alice/workspace/n.md"
    assert df.record_place(mounts, "/workspace/hidden/x.md", AGENT) is None       # a mask has no place
    assert df.record_place(mounts, "/workspace/null.md", AGENT) is None
    assert df.record_place(mounts, "/other/x.md", AGENT) is None                  # not in the agent's folder
    assert df.record_place(mounts, "/nowhere/x.md", AGENT) is None
    assert not (agents / AGENT / "workspace" / "out").exists()                    # nothing was made or opened


def test_the_facts_carry_the_write_cap():
    description = render.SandboxDescription(
        agent=AGENT, binds=(render.Bind(render.ROOT_AGENT, "workspace", "/workspace", True),), mcp_binds=(),
        mcps_root="/opt/mcps", extra_binds=(), cwd="/workspace", proxy_port=8400, forwards=(render.PROXY_FORWARD,))

    class _Builder:
        def get_env_overrides(self):
            return {}

    assert df.program_facts(description, _Builder())["write_max"] == df.WRITE_MAX_BYTES == 1024 * 1024


# -- the sixth rule written twice: the write ------------------------------------

def test_the_programs_temp_file_is_one_the_platform_knows(tmp_path):
    """The sync, the projector and retention pass over a ``.partial`` file,
    and retention removes one left behind: the program's temp file must be
    one of those, name for name."""
    from core.layers.direct import file_program
    from services.infra import safe_fs
    for name in ("a.md", "n" * 250, "α" * 150 + "b", ".hidden", "a.partial"):
        temp = file_program._temp_name(name)
        assert safe_fs.is_partial_of(temp, name), name
        assert len(os.fsencode(temp)) <= 255
    assert (file_program._TEMP_NAME_BYTES, file_program._TEMP_TAG_BYTES) == (
        safe_fs._TEMP_NAME_BYTES, safe_fs._TEMP_TAG_BYTES)


def _write_cases(root):
    """One tree for both writers: what each must write and what each must refuse."""
    (root / "sub").mkdir()
    (root / "plain.md").write_text("plain\n")
    (root / "alias").symlink_to(root / "sub")
    (root / "out").symlink_to(root.parent)
    return [("plain.md", True), ("new.md", True), ("sub/deep/new.md", True), ("alias/new.md", False),
            ("out/new.md", False), ("plain.md/x.md", False), ("sub", False)]


def test_the_programs_write_and_safe_fs_keep_the_same_rules(tmp_path):
    """``safe_fs.atomic_writer`` writes for the proxy's own process, the
    file program's write inside a sandbox: a temp file beside the name,
    moved over it, parents made, no link followed on the way. One table for
    both. Where they differ by design is said below."""
    from core.layers.direct import file_program
    from services.infra import safe_fs

    for who in ("platform", "program"):
        root = (tmp_path / who).resolve()
        root.mkdir()
        facts = {"rows": [str(root)], "refused": [], "write_max": 64}
        for rel, writes in _write_cases(root):
            if who == "platform":
                try:
                    safe_fs.atomic_write_beneath(root, rel, b"new\n", mkdirs=True)
                    wrote = True
                except OSError:
                    wrote = False
            else:
                wrote = file_program.OPERATIONS["write"](facts, {"path": f"{root}/{rel}", "content": "new\n"})["ok"]
            assert wrote is writes, (who, rel)
            if writes:
                assert (root / rel).read_text() == "new\n", (who, rel)
        assert not [n for _d, _s, names in os.walk(root) for n in names if n.endswith(".partial")]
        assert not (root.parent / "new.md").exists()
    # By design: a link AT the name. The platform's writer replaces the link itself. The file tool
    # refuses it, as it always did, so that a model never thinks it wrote where the link leads.
    root = (tmp_path / "program").resolve()
    (root / "ln.md").symlink_to(root / "plain.md")
    facts = {"rows": [str(root)], "refused": [], "write_max": 64}
    assert file_program.OPERATIONS["write"](facts, {"path": f"{root}/ln.md", "content": "x"})["code"] == "link_at_name"
    assert (root / "plain.md").read_text() == "new\n"


def test_the_sandbox_and_the_delete_owner_read_the_same_three_values(tmp_path):
    """What the program answers for a file is what the owner compares."""
    from core.layers.direct import file_program
    from services.infra import file_bookkeeping
    target = tmp_path.resolve() / "a.md"
    target.write_text("a")
    answer = file_program.OPERATIONS["stat"]({"rows": [str(target.parent)], "refused": []}, {"path": str(target)})
    assert df.file_facts(answer) == file_bookkeeping._facts_of(os.lstat(target))


# -- the platform's half opens nothing ------------------------------------------

#: Modules through which a file can be reached: a binding from one of them is
#: a decision, named in the test below.
_FILE_FAMILIES = ("os", "posixpath", "ntpath", "shutil", "tempfile", "glob", "fcntl", "io", "pathlib",
                  "subprocess", "mmap", "importlib", "builtins", "codecs", "fileinput",
                  "services.infra", "services.remote", "services.knowledge", "storage.files",
                  "core.agent_folder", "core.remote")
#: The names such a binding has in the two modules, each watched wherever it is used.
_FILE_OWNERS = ("os", "posixpath", "shutil", "tempfile", "glob", "fcntl", "safe_fs", "file_bookkeeping",
                "agent_folder")
#: What ``posixpath`` does with text alone.
_TEXT_ONLY = {"join", "normpath", "split", "dirname", "basename", "isabs", "commonpath", "splitext", "sep"}
_FILE_METHODS = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "iterdir", "rglob", "unlink",
                 "mkdir", "touch", "lstat", "stat", "exists", "rename", "symlink_to", "is_symlink", "is_file",
                 "is_dir", "readlink", "rmdir", "chmod", "hardlink_to", "samefile", "expanduser", "resolve",
                 "walk"}


def _in_family(name: str) -> bool:
    return any(name == family or name.startswith(family + ".") for family in _FILE_FAMILIES)


def _file_reach(source: str) -> Counter[str]:
    """Every way the module ``source`` could reach a file, by name and how
    often: each binding from a module of ``_FILE_FAMILIES`` (at the top or
    inside a function, with its alias), each use of such a name, each file
    method and each way around a name (``open``, ``__import__``, ``eval``,
    ``exec``, a watched module handed on as a value)."""
    import ast

    tree = ast.parse(source)
    out: Counter[str] = Counter()
    owners_of_attributes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _in_family(alias.name):
                    out[f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")] += 1
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                if _in_family(module) or _in_family(f"{module}.{alias.name}"):
                    out[f"from {module} import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")] += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                "open", "__import__", "eval", "exec"):
            out[f"{node.func.id}()"] += 1
        elif isinstance(node, ast.Attribute):
            owners_of_attributes.add(id(node.value))
            owner = ast.unparse(node.value)
            if owner == "safe_fs":
                if not node.attr[0].isupper():
                    out[f"safe_fs.{node.attr}"] += 1          # an exception's name is no helper
            elif owner == "posixpath":
                if node.attr not in _TEXT_ONLY:
                    out[f"posixpath.{node.attr}"] += 1
            elif owner in ("os", "os.path", "shutil", "tempfile", "glob", "fcntl"):
                if node.attr not in ("devnull", "path"):
                    out[f"{owner}.{node.attr}"] += 1
            elif owner in ("agent_folder", "file_bookkeeping"):
                out[f"{owner}.{node.attr}"] += 1
            elif owner == "asyncio" and node.attr in ("to_thread", "get_running_loop", "create_subprocess_exec",
                                                      "create_subprocess_shell"):
                out[f"asyncio.{node.attr}"] += 1
            elif node.attr in _FILE_METHODS:
                out[f".{node.attr}"] += 1
    # A watched module is only ever the owner of an attribute: one that is
    # assigned, handed on or asked with ``getattr`` is a way around its name.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FILE_OWNERS and id(node) not in owners_of_attributes:
            out[f"{node.id} as a value"] += 1
    return out


def test_the_platforms_half_of_the_file_tools_opens_no_file_of_a_session():
    """``files.py`` and ``builtins.py`` judge text and call the program: every
    open of a session's file is the program's, inside the sandbox. A file
    call, a safe helper or work in a thread added there later fails here,
    also one bound by an import of its own, at the top or inside a function,
    and also one more call of a name that is allowed once. What is allowed,
    each by name and by count: the read of the program's own source when the
    module loads, the agent's folder taken from text (``beneath_agent``),
    one look at whether the agent's own folder is a link, to word the
    refusal of a Delete in a folder an operator moved, and one call each of
    the bookkeeping's owner for a write and for a delete, which run after
    the sandbox's judgement.

    What this guard holds is the text of the two modules. It does not see
    into a module outside the watched families that they call
    (``core.sandbox``, ``sandbox_program``, the session, ``config``,
    ``auth``, ``storage`` but for ``storage.files``): a file helper written
    there is held by the two other guards. ``tests/core/
    test_agent_tree_surface.py`` asks a row with a reason for every raw file
    call, and ``tests/core/test_safe_fs_roots.py`` asks a named root for
    every safe helper."""
    import inspect

    from core.layers.direct import builtins, files

    assert _file_reach(inspect.getsource(files)) == {
        "import posixpath": 1, "from pathlib import Path": 1, "from core import agent_folder": 1,
        "from services.infra.path_confinement import PathOutsideRoot": 1,
        "from services.infra.path_confinement import normalize_rel_path": 1,
        ".read_text": 1, "agent_folder.beneath_agent": 1,
    }
    assert _file_reach(inspect.getsource(builtins)) == {
        "import os": 1, "from pathlib import Path": 1,
        "from services.infra import file_bookkeeping": 2, "from services.infra import safe_fs": 1,
        "file_bookkeeping.push_file_write": 1, "file_bookkeeping.delete_platform_file": 1,
        "os.path.islink": 1, "asyncio.to_thread": 1,
    }


def test_the_guard_of_the_platforms_half_counts_each_finding():
    """One more call of a name that is allowed once is a finding: the guard
    cannot tell a removal that follows the sandbox's look from one that
    does not, so it holds that there is one."""
    twice = ("from services.infra import file_bookkeeping\n"
             "await file_bookkeeping.delete_platform_file(a, b, c)\n"
             "await file_bookkeeping.delete_platform_file(a, b, d)\n"
             "x = agent_folder.beneath_agent(h, a)\ny = agent_folder.beneath_agent(h, b)\n"
             "os.path.islink(p)\nos.path.islink(q)\n")
    assert _file_reach(f"async def f():\n{twice}".replace("\n", "\n    ").rstrip()) == {
        "from services.infra import file_bookkeeping": 1, "file_bookkeeping.delete_platform_file": 2,
        "agent_folder.beneath_agent": 2, "os.path.islink": 2,
    }


@pytest.mark.parametrize("source, finding", [
    # A helper bound by its own name, at the top and inside a function, with and without an alias.
    ("from services.infra.safe_fs import lstat_beneath as _peek", "from services.infra.safe_fs import lstat_beneath as _peek"),
    ("def f(root):\n    from services.infra.safe_fs import lstat_beneath\n    return lstat_beneath(root, 'a')",
     "from services.infra.safe_fs import lstat_beneath"),
    ("def f(host, agent):\n    from services.infra.safe_fs import lstat_beneath as _peek\n"
     "    root, where = agent_folder.beneath_agent(host, agent)\n    return _peek(root, where)",
     "from services.infra.safe_fs import lstat_beneath as _peek"),
    ("from services.infra import safe_fs as fs", "from services.infra import safe_fs as fs"),
    ("import services.infra.safe_fs", "import services.infra.safe_fs"),
    ("from services.infra.file_bookkeeping import tombstone_path_sync",
     "from services.infra.file_bookkeeping import tombstone_path_sync"),
    ("from services.remote import workspace_fanout", "from services.remote import workspace_fanout"),
    ("from storage.files import recover_bin_store", "from storage.files import recover_bin_store"),
    ("from os import open as o", "from os import open as o"),
    ("from os.path import exists", "from os.path import exists"),
    ("import shutil", "import shutil"),
    ("import io", "import io"),
    ("from pathlib import PurePosixPath as P", "from pathlib import PurePosixPath as P"),
    ("from . import safe_fs\nsafe_fs.lstat_beneath(1, 2)", "safe_fs.lstat_beneath"),
    # A use of a name that is already bound.
    ("safe_fs.lstat_beneath(root, 'a')", "safe_fs.lstat_beneath"),
    ("file_bookkeeping.tombstone_path_sync('a', 'b')", "file_bookkeeping.tombstone_path_sync"),
    ("posixpath.realpath(p)", "posixpath.realpath"),
    ("posixpath.exists(p)", "posixpath.exists"),
    ("os.stat(p)", "os.stat"),
    ("agent_folder.root_of(a)", "agent_folder.root_of"),
    ("Path(p).resolve()", ".resolve"),
    ("host.read_bytes()", ".read_bytes"),
    ("open(p)", "open()"),
    # A way around the name.
    ("fs = safe_fs", "safe_fs as a value"),
    ("getattr(safe_fs, 'lstat_beneath')(root, 'a')", "safe_fs as a value"),
    ("call(os)", "os as a value"),
    ("__import__('os').stat(p)", "__import__()"),
    ("import importlib", "import importlib"),
    ("eval('open(p)')", "eval()"),
    ("async def f():\n    await asyncio.to_thread(work)", "asyncio.to_thread"),
])
def test_the_guard_of_the_platforms_half_sees_each_way_to_a_file(source, finding):
    """The guard above is only as good as what its reader finds: each way
    to reach a file that a later change could take, written out once."""
    assert finding in _file_reach(source), _file_reach(source)


def test_the_guard_of_the_platforms_half_passes_what_is_only_text():
    assert _file_reach("import posixpath\nx = posixpath.join(posixpath.normpath(a), posixpath.basename(b))\n"
                       "raise safe_fs.SymlinkRefused(x)\ny = os.devnull\n") == {"import posixpath": 1}
