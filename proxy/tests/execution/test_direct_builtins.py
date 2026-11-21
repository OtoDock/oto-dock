"""Direct-LLM client-side builtins (core/layers/direct/builtins.py): the
registry, the two-pass permission gate (path policy, then tier × mode) and
the file-tool handlers with their platform bookkeeping."""

from __future__ import annotations

import asyncio
import os

import pytest

from auth.path_policy import SecurityContext
from core.layers.direct import builtins as B, files as df
from core.sandbox.sandbox import SandboxConfig
from core.session.session_state import set_session_security
from tests.execution import direct_program


@pytest.fixture(autouse=True)
def _file_program(monkeypatch):
    """The file program runs in this process, on the test's own tree."""
    direct_program.install(monkeypatch)


AGENT = "pa"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    import config
    agents = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    a = agents / AGENT
    for d in ("config", "workspace", "knowledge/memory",
              "users/alice/workspace", "users/alice/context", "users/alice/.claude"):
        (a / d).mkdir(parents=True)
    (a / "workspace" / "notes.md").write_text("line one\nline two\n")
    mcps = tmp_path / "mcps"
    mcps.mkdir()
    return agents, mcps


def _cfg(tree, role="manager", username="alice", libraries=()):
    agents, mcps = tree
    claude = agents / AGENT / (f"users/{username}/.claude" if username else "workspace/.claude")
    claude.mkdir(parents=True, exist_ok=True)
    return SandboxConfig(
        role=role, username=username, agent_name=AGENT, is_admin_agent=False,
        host_agents_dir=agents.resolve(), host_mcps_dir=mcps.resolve(),
        host_claude_dir=claude.resolve(), net_forwards=["8400"],
        knowledge_libraries=list(libraries),
    )


class _Session:
    """The slice of DirectSession the builtins touch."""

    def __init__(self, cfg, sid):
        self.session_id = sid
        self.sandbox_cfg = cfg
        self._mt = None

    def mount_table(self):
        if self._mt is None:
            self._mt = df.mount_table(self.sandbox_cfg) if self.sandbox_cfg else []
        return self._mt


def _ctx(role="manager", username="alice"):
    return SecurityContext(role=role, username=username, agent=AGENT, is_admin_agent=False)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_exposes_the_five_file_tools():
    defs = B.file_tool_defs()
    assert [t["name"] for t in defs] == ["Read", "Glob", "Write", "Edit", "Delete"]
    for t in defs:
        assert t["description"] and t["input_schema"]["type"] == "object"
    assert B.is_builtin("Read") and B.is_builtin("Delete")
    assert not B.is_builtin("mcp__memory-mcp__memory") and not B.is_builtin("Bash")
    assert B.get("Delete").tier == B.TIER_DESTRUCTIVE
    assert B.get("Write").tier == B.TIER_EDIT and B.get("Glob").tier == B.TIER_OPEN


def test_direct_session_lists_the_file_tools():
    from core.layers.direct.session import DirectSession
    s = DirectSession("direct-defs", AGENT, "prompt", mcp_manager=None, provider="openai")
    assert {"Read", "Glob", "Write", "Edit", "Delete"} <= {t["name"] for t in s.tools}
    assert s.mount_table() == []  # no sandbox config → nothing resolvable


# ---------------------------------------------------------------------------
# Pass-2: tier × mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["ask", "autoReview", "dontAsk", "auto", "plan", "acceptEdits"])
def test_open_tier_always_allows(mode):
    assert B.decide(B.TIER_OPEN, mode) == "allow"


@pytest.mark.parametrize("mode,expected", [
    ("ask", "prompt"), ("autoReview", "allow"), ("dontAsk", "allow"),
    ("auto", "allow"), ("plan", "deny"),
    # A word no release writes (one from before 1.9) is no more than Ask.
    ("acceptEdits", "prompt"), ("default", "prompt"),
])
def test_edit_tier(mode, expected):
    assert B.decide(B.TIER_EDIT, mode) == expected


@pytest.mark.parametrize("mode,expected", [
    ("ask", "prompt"), ("autoReview", "prompt"), ("dontAsk", "allow"),
    ("auto", "allow"), ("plan", "deny"), ("acceptEdits", "prompt"),
])
def test_destructive_tier_prompts_even_in_auto(mode, expected):
    assert B.decide(B.TIER_DESTRUCTIVE, mode) == expected


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_gate_runs_path_policy_then_the_tier_table(tree):
    s = _Session(_cfg(tree), "direct-gate")
    set_session_security(s.session_id, _ctx())
    # Pass-1: memory files are managed by the platform (path policy).
    outcome, reason = B.gate(
        s, {"name": "Write", "input": {"file_path": "/knowledge/memory/x.md", "content": "y"}},
        "autoReview",
    )
    assert outcome == "deny" and "memory" in reason
    write = {"name": "Write", "input": {"file_path": "/workspace/x.md", "content": "y"}}
    assert B.gate(s, write, "autoReview") == ("allow", "")
    assert B.gate(s, write, "ask")[0] == "prompt"
    assert B.gate(s, {"name": "Delete", "input": {"file_path": "/workspace/x.md"}}, "autoReview")[0] == "prompt"
    assert B.gate(s, {"name": "Read", "input": {"file_path": "/workspace/x.md"}}, "plan") == ("allow", "")
    outcome, reason = B.gate(s, write, "plan")
    assert outcome == "deny" and "plan mode" in reason
    assert B.gate(s, {"name": "Nope", "input": {}}, "ask")[0] == "deny"


def test_gate_without_security_context_fails_closed(tree):
    s = _Session(_cfg(tree), "direct-no-ctx")
    outcome, reason = B.gate(
        s, {"name": "Read", "input": {"file_path": "/workspace/notes.md"}}, "ask",
    )
    assert outcome == "deny" and "no longer active" in reason


def test_gate_viewer_cannot_write_shared_folders(tree):
    s = _Session(_cfg(tree, role="viewer"), "direct-viewer-gate")
    set_session_security(s.session_id, _ctx(role="viewer"))
    outcome, reason = B.gate(
        s, {"name": "Write", "input": {"file_path": "/workspace/x.md", "content": "y"}},
        "dontAsk",
    )
    assert outcome == "deny" and reason


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_edit_read_glob_delete_roundtrip(tree, monkeypatch):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-rt")
    set_session_security(s.session_id, _ctx())
    writes: list = []
    deletes: list = []

    async def _push(agent, rel, host, *, writer=None):
        writes.append((agent, rel, writer))

    async def _delete(agent, agent_dir, target, *, expect=None):
        # What the sandbox saw of the file comes with the delete.
        st = target.lstat()
        assert expect == (st.st_size, st.st_ino, st.st_ctime_ns)
        deletes.append((agent, agent_dir, target))
        target.unlink()
        return False

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)
    monkeypatch.setattr("services.infra.file_bookkeeping.delete_platform_file", _delete)

    out = await B.execute(s, "Write", {"file_path": "/workspace/out/report.md", "content": "# R\n"})
    assert out.startswith("Created /workspace/out/report.md")
    target = agents / AGENT / "workspace" / "out" / "report.md"
    assert target.read_text() == "# R\n"
    assert writes == [(AGENT, "workspace/out/report.md", "alice")]

    out = await B.execute(s, "Edit", {
        "file_path": "/workspace/out/report.md", "old_string": "# R", "new_string": "# Report",
    })
    assert "1 replacement" in out and target.read_text() == "# Report\n"
    assert len(writes) == 2

    assert await B.execute(s, "Read", {"file_path": "/workspace/out/report.md"}) == "     1\t# Report"
    out = await B.execute(s, "Glob", {"pattern": "**/*.md", "path": "/workspace"})
    assert out.splitlines() == ["/workspace/notes.md", "/workspace/out/report.md"]
    # Glob defaults to the session cwd (/users/alice here).
    assert await B.execute(s, "Glob", {"pattern": "**/*.md"}) == "(no matches)"

    out = await B.execute(s, "Delete", {"file_path": "/workspace/out/report.md"})
    assert "Recover bin" in out and not target.exists()
    assert deletes and deletes[0][0] == AGENT and deletes[0][2] == target.resolve()
    assert deletes[0][1] == agents.resolve() / AGENT


@pytest.mark.asyncio
async def test_agent_scope_writes_record_no_author(tree, monkeypatch):
    s = _Session(_cfg(tree, username=""), "direct-agent-scope")
    set_session_security(s.session_id, _ctx(username=""))
    writers: list = []

    async def _push(agent, rel, host, *, writer=None):
        writers.append(writer)

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)
    out = await B.execute(s, "Write", {"file_path": "shared.md", "content": "x"})
    assert out.startswith("Created /workspace/shared.md")
    assert writers == [None]


@pytest.mark.asyncio
async def test_write_refuses_without_a_writable_root(tree):
    s = _Session(_cfg(tree, role="viewer", username=""), "direct-viewer")
    msg = "Error: no writable location in this session: every folder is read-only"
    assert await B.execute(s, "Write", {"file_path": "/workspace/x.md", "content": "y"}) == msg
    assert await B.execute(s, "Delete", {"file_path": "/workspace/notes.md"}) == msg
    assert "line one" in await B.execute(s, "Read", {"file_path": "/workspace/notes.md"})


@pytest.mark.asyncio
async def test_a_change_under_a_read_only_folder_is_refused_on_the_text_before_any_call(tree, monkeypatch):
    """The text check words the refusal first, with its own sentence, for
    each of the three tools: the program is not asked."""
    calls = direct_program.install(monkeypatch)
    editor = _Session(_cfg(tree, role="editor"), "direct-editor")
    (tree[0] / AGENT / "knowledge" / "guide.md").write_text("# Guide\n")
    said = "Error: /knowledge/guide.md is read-only in this session"
    assert await B.execute(editor, "Write", {"file_path": "/knowledge/guide.md", "content": "x"}) == said
    assert await B.execute(editor, "Edit", {"file_path": "/knowledge/guide.md", "old_string": "Guide",
                                            "new_string": "x"}) == said
    assert await B.execute(editor, "Delete", {"file_path": "/knowledge/guide.md"}) == said
    viewer = _Session(_cfg(tree, role="viewer", username=""), "direct-viewer-edit")
    assert await B.execute(viewer, "Edit", {"file_path": "/workspace/notes.md", "old_string": "line", "new_string": "x"}) == \
        "Error: no writable location in this session: every folder is read-only"
    assert calls == [] and (tree[0] / AGENT / "knowledge" / "guide.md").read_text() == "# Guide\n"


@pytest.mark.asyncio
async def test_a_write_with_no_place_for_its_record_still_succeeds_and_says_so_in_the_log(tree, monkeypatch, caplog):
    s = _Session(_cfg(tree), "direct-write-noplace")
    pushed: list = []

    async def _push(agent, rel, host, *, writer=None):
        pushed.append(rel)

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)
    monkeypatch.setattr(df, "record_place", lambda mounts, vpath, agent: None)
    with caplog.at_level("WARNING", logger="direct-runner"):
        out = await B.execute(s, "Write", {"file_path": "/workspace/w.md", "content": "x"})
    assert out == "Created /workspace/w.md (1 bytes)" and pushed == []
    assert [r.getMessage() for r in caplog.records] == [
        "Write of /workspace/w.md: no path below the agent's folder, no record was made"]


@pytest.mark.asyncio
async def test_skill_returns_the_materialized_body(tree):
    agents, _ = tree
    cfg = _cfg(tree)
    skills = cfg.host_claude_dir / "skills"
    (skills / "task-scheduling-guide").mkdir(parents=True)
    (skills / "task-scheduling-guide" / "SKILL.md").write_text(
        "---\nname: task-scheduling-guide\ndescription: Guide\n---\n\n# Guide\n\nUse cron.\n"
    )
    (skills / ".quarantine").mkdir()
    (skills / "broken").mkdir()  # no SKILL.md → not listed
    s = _Session(cfg, "direct-skill")
    out = await B.execute(s, "Skill", {"name": "task-scheduling-guide"})
    assert out == "# Skill: task-scheduling-guide\n\n# Guide\n\nUse cron."
    out = await B.execute(s, "Skill", {"name": "nope"})
    assert out == "Error: unknown skill 'nope'. Available: task-scheduling-guide"
    for bad in ("../secrets", "Task Scheduling", "", "a" * 70):
        out = await B.execute(s, "Skill", {"name": bad})
        assert out.startswith("Error: name must be a skill id"), bad
    assert B.get("Skill").tier == B.TIER_OPEN
    assert "Skill" in [t["name"] for t in B.client_tool_defs()]


@pytest.mark.asyncio
async def test_a_skill_reached_through_a_link_is_neither_listed_nor_read(tree, tmp_path):
    """The skills are copies the materializer makes as plain files: a skill
    folder or a SKILL.md replaced by a link is never read."""
    cfg = _cfg(tree)
    skills = cfg.host_claude_dir / "skills"
    (skills / "kept").mkdir(parents=True)
    (skills / "kept" / "SKILL.md").write_text("# Kept\n")
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    (stand_in / "SKILL.md").write_text("# Theirs\n")
    (skills / "linked").symlink_to(stand_in)
    (skills / "leaf").mkdir()
    (skills / "leaf" / "SKILL.md").symlink_to(stand_in / "SKILL.md")
    s = _Session(cfg, "direct-skill-links")
    for name in ("linked", "leaf"):
        out = await B.execute(s, "Skill", {"name": name})
        assert out == f"Error: unknown skill '{name}'. Available: kept", out


@pytest.mark.asyncio
async def test_skill_without_a_skills_dir(tree):
    s = _Session(_cfg(tree), "direct-skill-empty")
    out = await B.execute(s, "Skill", {"name": "anything"})
    assert out == "Error: unknown skill 'anything'. No on-demand skills are enabled for this session."


@pytest.mark.asyncio
async def test_failures_become_result_text(tree):
    s = _Session(_cfg(tree), "direct-errors")
    assert await B.execute(s, "Read", {"file_path": "/workspace/nope.md"}) == \
        "Error: File not found: /workspace/nope.md"
    assert await B.execute(s, "Delete", {"file_path": "/workspace"}) == \
        "Error: /workspace is a folder. Delete removes single files."
    assert await B.execute(s, "Nope", {}) == "Error: Unknown tool 'Nope'"
    bare = _Session(None, "direct-bare")
    assert await B.execute(bare, "Read", {"file_path": "/workspace/notes.md"}) == \
        "Error: file tools are not available in this session (no sandbox)"


# ---------------------------------------------------------------------------
# The gate inside a meeting: a Direct participant is gated as every other
# participant, by the host chat's mode through the one authority
# ---------------------------------------------------------------------------

_HOST = "direct-host-sess"
_PUMP = "meeting-mdirect"


@pytest.fixture
def participant(monkeypatch):
    from core.session import session_state
    sid = "direct-part-sess"
    session_state.register_session_state(sid, "auto", SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False, session_scope="agent",
    ))
    monkeypatch.setitem(session_state._sessions, sid, {"is_meeting": True, "client_type": "meeting"})
    session_state.set_meeting_session_info(sid, _HOST, _PUMP, AGENT, "direct-host-chat")
    # Where a card lands is the meeting pump's queue, which a live pump
    # reads (where a card shows with no pump is
    # tests/hooks/test_hook_cards_between_turns.py's).
    from core.session import engine_prompts
    monkeypatch.setattr(engine_prompts, "queue_has_reader", lambda queue: True)
    yield sid
    session_state.cleanup_meeting_session_info(sid)
    session_state.cleanup_session_permission_state(sid)
    session_state.cleanup_session_permission_state(_PUMP)
    session_state._session_modes.pop(_HOST, None)


@pytest.mark.asyncio
async def test_a_session_outside_meetings_keeps_the_builtin_table(tree, monkeypatch):
    from core.session import session_events
    called = []

    async def _pre(*a, **k):
        called.append(a)
        return {"decision": "allow"}
    monkeypatch.setattr(session_events, "pre_tool", _pre)
    s = _Session(_cfg(tree), "direct-plain")
    set_session_security(s.session_id, _ctx())
    write = {"name": "Write", "input": {"file_path": "/workspace/x.md", "content": "y"}}
    assert await B.gate_for_session(s, write, "ask") == B.gate(s, write, "ask")
    assert called == []


@pytest.mark.asyncio
async def test_a_participants_builtin_goes_to_the_one_authority(tree, monkeypatch, participant):
    from core.session import session_events
    answers = iter([{"decision": "allow"}, {"decision": "deny", "reason": "Tool use denied by user."}])
    called = []

    async def _pre(sid, name, tool_input, **k):
        called.append((sid, name, tool_input))
        return next(answers)
    monkeypatch.setattr(session_events, "pre_tool", _pre)
    s = _Session(_cfg(tree), participant)
    write = {"name": "Write", "input": {"file_path": "/workspace/x.md", "content": "y"}}
    assert await B.gate_for_session(s, write, "auto") == ("allow", "")
    assert await B.gate_for_session(s, write, "auto") == ("deny", "Tool use denied by user.")
    assert called == [(participant, "Write", write["input"])] * 2


@pytest.mark.asyncio
async def test_a_meeting_session_without_its_mapping_is_refused(tree, monkeypatch, participant):
    from core.session import session_events, session_state

    async def _pre(*a, **k):
        raise AssertionError("an ended meeting asks nobody")
    monkeypatch.setattr(session_events, "pre_tool", _pre)
    session_state.cleanup_meeting_session_info(participant)
    s = _Session(_cfg(tree), participant)
    read = {"name": "Read", "input": {"file_path": "/workspace/notes.md"}}
    assert await B.gate_for_session(s, read, "auto") == ("deny", B.MEETING_ENDED)


@pytest.mark.asyncio
async def test_a_participants_write_asks_in_the_host_chat_under_its_default_mode(tree, participant):
    """The real authority: the host chat's default mode asks on the meeting
    pump's queue with the speaker's badge, and the person's answer lets the
    write through; after a routing tool even a read is refused."""
    import asyncio

    from core.session import session_state
    session_state.set_session_mode(_HOST, "ask")
    s = _Session(_cfg(tree), participant)
    write = {"name": "Write", "input": {"file_path": "/workspace/x.md", "content": "y"}}
    gate = asyncio.ensure_future(B.gate_for_session(s, write, "auto"))
    queue = session_state.get_permission_queue(_PUMP)
    prompt = await asyncio.wait_for(queue.get(), timeout=5)
    assert prompt["tool_name"] == "Write" and prompt["meeting_agent"] == AGENT
    session_state.resolve_permission(prompt["request_id"], True)
    assert await asyncio.wait_for(gate, timeout=5) == ("allow", "")

    session_state.mark_meeting_turn_routed(participant, "direct_to")
    outcome, reason = await B.gate_for_session(
        s, {"name": "Read", "input": {"file_path": "/workspace/notes.md"}}, "auto")
    assert outcome == "deny" and "direct_to" in reason


# ---------------------------------------------------------------------------
# Delete removes the name it was given; the bookkeeping takes the rel written
# ---------------------------------------------------------------------------

@pytest.fixture
def platform_delete(monkeypatch):
    """The real platform delete, its database and fan-out steps stood in."""
    from services.infra import file_bookkeeping
    from storage.files import recover_bin_store
    captured: list = []
    monkeypatch.setattr(recover_bin_store, "capture", lambda agent, rel, *_a, **_k: captured.append(rel))
    monkeypatch.setattr(file_bookkeeping, "tombstone_path_sync", lambda *_a, **_k: None)
    monkeypatch.setattr(file_bookkeeping, "schedule_library_projection", lambda *_a, **_k: None)

    async def _no_push(*_a, **_k):
        return None

    monkeypatch.setattr(file_bookkeeping, "push_file_delete", _no_push)
    return captured


def _swapped_after_the_look(monkeypatch, swap):
    """The sandbox's look answers, then ``swap`` runs before the platform
    removes: what a writer could change between the two."""
    done: list[bool] = []

    async def call_program(session, operation, args, *, then=None):
        answer = direct_program.run(session, operation, args)
        if operation == "stat" and answer.get("ok") is True and not done:
            done.append(True)
            swap()
        if then is not None:
            await then(answer)
        return answer

    monkeypatch.setattr(df, "call_program", call_program)


@pytest.mark.asyncio
async def test_delete_never_reaches_a_read_only_mirror_through_a_link(tree, platform_delete):
    agents, _ = tree
    s = _Session(_cfg(tree, libraries=[("src", "", False)]), "direct-del-mirror")
    s.mount_table()
    knowledge = agents / AGENT / "knowledge"
    (knowledge / "shared" / "src" / "doc.md").write_text("mirror\n")
    (knowledge / "alias").symlink_to("shared/src")
    out = await B.execute(s, "Delete", {"file_path": "/knowledge/alias/doc.md"})
    assert out == "Error: The path goes through a symlink to a place the file tools cannot reach."
    assert (knowledge / "shared" / "src" / "doc.md").read_text() == "mirror\n" and platform_delete == []


@pytest.mark.asyncio
async def test_delete_never_reaches_engine_state_through_a_link(tree, platform_delete):
    agents, _ = tree
    s = _Session(_cfg(tree, username=""), "direct-del-state")
    ws = agents / AGENT / "workspace"
    (ws / ".claude" / "login.json").write_text("{}")
    (ws / "state").symlink_to(".claude")
    out = await B.execute(s, "Delete", {"file_path": "/workspace/state/login.json"})
    assert out == "Error: The path goes through a symlink to a place the file tools cannot reach."
    assert (ws / ".claude" / "login.json").exists() and platform_delete == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["another person's folder", "a folder outside the agent's"])
async def test_delete_through_a_folder_swapped_after_the_look_is_refused(tree, monkeypatch, platform_delete, target):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-swap")
    ws = agents / AGENT / "workspace"
    (ws / "d").mkdir()
    (ws / "d" / "x.md").write_text("mine\n")
    bob = agents / AGENT / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)
    (bob / "x.md").write_text("mine\n")           # the same size, so only the place tells them apart
    stand_in = agents.parent / "stand-in"
    stand_in.mkdir()
    (stand_in / "x.md").write_text("mine\n")

    def swap():
        (ws / "d").rename(ws / "d-aside")
        (ws / "d").symlink_to("../users/bob/workspace" if "person" in target else stand_in)

    _swapped_after_the_look(monkeypatch, swap)
    out = await B.execute(s, "Delete", {"file_path": "/workspace/d/x.md"})
    # The owner walks beneath the agents folder with no link followed.
    assert out == ("Error: cannot delete /workspace/d/x.md: the path goes through a symlink, "
                   "so use the real path instead")
    assert (bob / "x.md").exists() and (stand_in / "x.md").exists() and (ws / "d-aside" / "x.md").exists()
    assert platform_delete == []


@pytest.mark.asyncio
async def test_delete_of_a_file_link_never_removes_its_target(tree, monkeypatch, platform_delete):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-leaf")
    ws = agents / AGENT / "workspace"
    (ws / "reports").mkdir()
    (ws / "reports" / "a.md").write_text("report\n")
    (ws / "latest.md").symlink_to("reports/a.md")
    out = await B.execute(s, "Delete", {"file_path": "/workspace/latest.md"})
    assert out == "Error: /workspace/latest.md is not a regular file. Delete removes regular files only."
    (ws / "latest.md").unlink()
    (ws / "latest.md").write_text("plain\n")

    def swap():
        (ws / "latest.md").unlink()
        (ws / "latest.md").symlink_to("reports/a.md")

    _swapped_after_the_look(monkeypatch, swap)
    out = await B.execute(s, "Delete", {"file_path": "/workspace/latest.md"})
    assert out == "Error: File not found: /workspace/latest.md"
    assert (ws / "reports" / "a.md").read_text() == "report\n" and platform_delete == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["another file moved over the name", "written in place", "gone"])
async def test_a_file_that_is_not_the_one_the_sandbox_saw_is_not_deleted(tree, monkeypatch, platform_delete, change):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-race")
    ws = agents / AGENT / "workspace"
    (ws / "x.md").write_text("mine\n")

    def swap():
        if change == "another file moved over the name":
            (ws / "theirs").write_text("mine\n")           # the same size, another file
            os.replace(ws / "theirs", ws / "x.md")
        elif change == "written in place":
            with open(ws / "x.md", "a") as fh:
                fh.write("theirs\n")
        else:
            (ws / "x.md").unlink()

    _swapped_after_the_look(monkeypatch, swap)
    out = await B.execute(s, "Delete", {"file_path": "/workspace/x.md"})
    assert out == "Error: File not found: /workspace/x.md"
    assert platform_delete == [] and (change == "gone" or (ws / "x.md").exists())


@pytest.mark.asyncio
async def test_a_look_that_answers_falsely_removes_nothing(tree, monkeypatch, platform_delete):
    """No answer of the program widens what the platform removes: values
    that are no file's, and the values of another file, remove nothing."""
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-false")
    ws = agents / AGENT / "workspace"
    other = (ws / "notes.md").lstat()
    (ws / "x.md").write_text("mine\n")
    for false in ({"ok": True}, {"ok": True, "size": True, "inode": 1, "changed": 1},
                  {"ok": True, "size": other.st_size, "inode": other.st_ino, "changed": other.st_ctime_ns}):
        async def call_program(session, operation, args, *, then=None, answer=false):
            await then(answer)
            return answer

        monkeypatch.setattr(df, "call_program", call_program)
        out = await B.execute(s, "Delete", {"file_path": "/workspace/x.md"})
        assert out.startswith("Error: "), out
    assert (ws / "x.md").exists() and (ws / "notes.md").exists() and platform_delete == []


@pytest.mark.asyncio
async def test_delete_says_when_the_file_was_too_large_for_the_recover_bin(tree, monkeypatch):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-large")

    async def _delete(agent, agent_dir, target, *, expect=None):
        target.unlink()
        return True                         # the capture was skipped for size

    monkeypatch.setattr("services.infra.file_bookkeeping.delete_platform_file", _delete)
    out = await B.execute(s, "Delete", {"file_path": "/workspace/notes.md"})
    assert out == "Deleted /workspace/notes.md (too large for the Recover bin: this cannot be undone)"


@pytest.mark.asyncio
async def test_a_look_whose_removal_never_ran_is_no_delete(tree, monkeypatch, platform_delete):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-nothen")

    async def call_program(session, operation, args, *, then=None):
        return direct_program.run(session, operation, args)        # an answer, and nothing after it

    monkeypatch.setattr(df, "call_program", call_program)
    out = await B.execute(s, "Delete", {"file_path": "/workspace/notes.md"})
    assert out == "Error: cannot delete /workspace/notes.md: the file tools failed"
    assert (agents / AGENT / "workspace" / "notes.md").exists() and platform_delete == []


@pytest.mark.asyncio
async def test_a_look_that_lands_under_another_name_removes_nothing(tree, monkeypatch, platform_delete):
    """A landing that names another file of the same writable folder would
    pass the text check and Pass-1. The platform still removes only under
    the name the model gave: neither file goes, and nothing is asked again."""
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-name")
    set_session_security(s.session_id, _ctx())      # so that Pass-1 would let the place through
    ws = agents / AGENT / "workspace"
    (ws / "x.md").write_text("mine\n")
    asked: list = []

    async def call_program(session, operation, args, *, then=None):
        asked.append(args)
        answer = ({"ok": False, "code": "lands", "landed": "/workspace/notes.md"} if len(asked) == 1
                  else direct_program.run(session, operation, args))
        if then is not None:
            await then(answer)
        return answer

    monkeypatch.setattr(df, "call_program", call_program)
    out = await B.execute(s, "Delete", {"file_path": "/workspace/x.md"})
    assert out == f"Error: {df.ELSEWHERE}"
    assert len(asked) == 1 and (ws / "x.md").exists() and (ws / "notes.md").exists() and platform_delete == []


@pytest.mark.asyncio
@pytest.mark.parametrize("removed", [True, False])
async def test_a_delete_whose_removal_began_and_did_not_finish_reads_as_not_known(tree, monkeypatch, removed):
    """Once the owner was asked to remove the file, a failure that is no
    refusal of its own leaves it unknown whether the file is gone. The tool
    says so, and never that the file could not be deleted."""
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-unfinished")

    async def _delete(agent, agent_dir, target, *, expect=None):
        if removed:
            target.unlink()                               # the file went, then the record's write failed
        raise RuntimeError("the store is away")

    monkeypatch.setattr("services.infra.file_bookkeeping.delete_platform_file", _delete)
    out = await B.execute(s, "Delete", {"file_path": "/workspace/notes.md"})
    assert out == ("Error: It is not known whether /workspace/notes.md was deleted: the delete did not finish. "
                   "Look for the file to see.")
    assert (agents / AGENT / "workspace" / "notes.md").exists() is not removed


@pytest.mark.asyncio
async def test_a_file_with_no_place_below_the_agents_folder_is_not_deleted(tree, monkeypatch, platform_delete):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-noplace")
    monkeypatch.setattr(df, "record_place", lambda mounts, vpath, agent: None)
    out = await B.execute(s, "Delete", {"file_path": "/workspace/notes.md"})
    assert out == "Error: /workspace/notes.md is not a regular file. Delete removes regular files only."
    assert (agents / AGENT / "workspace" / "notes.md").exists() and platform_delete == []


@pytest.mark.asyncio
async def test_a_delete_stopped_before_its_removal_begins_removes_nothing(tree, monkeypatch, platform_delete):
    """A Stop reaches the handler first, and the look's answer comes back
    afterwards: nothing is removed, as before the move into the sandbox."""
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-stop")
    ws = agents / AGENT / "workspace"
    (ws / "x.md").write_text("mine\n")
    held: list = []
    asked = asyncio.Event()

    async def call_program(session, operation, args, *, then=None):
        held.append((then, direct_program.run(session, operation, args)))
        asked.set()
        await asyncio.Event().wait()            # the answer is on its way: the handler waits

    monkeypatch.setattr(df, "call_program", call_program)
    call = asyncio.ensure_future(B.execute(s, "Delete", {"file_path": "/workspace/x.md"}))
    await asyncio.wait_for(asked.wait(), 5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    then, answer = held[0]
    assert answer["ok"] is True
    await then(answer)                          # the piece goes on, as the general part runs it
    assert (ws / "x.md").read_text() == "mine\n" and platform_delete == []


@pytest.mark.asyncio
async def test_a_delete_stopped_after_its_removal_began_is_whole(tree, monkeypatch):
    """The removal is what follows the look's answer, so once it has begun
    it runs to its end whatever happens to the handler: the capture, the
    removal, the tombstone, the library's delete and the push."""
    from services.infra import file_bookkeeping
    from storage.files import recover_bin_store
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-whole")
    ws = agents / AGENT / "workspace"
    (ws / "x.md").write_text("mine\n")
    steps: list = []
    monkeypatch.setattr(recover_bin_store, "capture", lambda agent, rel, *_a, **_k: steps.append(("bin", rel)))
    monkeypatch.setattr(file_bookkeeping, "tombstone_path_sync", lambda agent, rel, **_k: steps.append(("tombstone", rel)))
    monkeypatch.setattr(file_bookkeeping, "schedule_library_projection",
                        lambda agent, rel, *, deleted: steps.append(("library", rel)))

    async def push(agent, rel):
        steps.append(("push", rel))

    monkeypatch.setattr(file_bookkeeping, "push_file_delete", push)
    began = asyncio.Event()

    async def call_program(session, operation, args, *, then=None):
        answer = direct_program.run(session, operation, args)
        piece = asyncio.ensure_future(then(answer))     # the piece is the program's own task
        began.set()
        await asyncio.shield(piece)
        return answer

    monkeypatch.setattr(df, "call_program", call_program)
    call = asyncio.ensure_future(B.execute(s, "Delete", {"file_path": "/workspace/x.md"}))
    await asyncio.wait_for(began.wait(), 5)
    await asyncio.sleep(0)                      # the removal has begun
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    for _ in range(200):
        if len(steps) == 4:
            break
        await asyncio.sleep(0.01)
    rel = "workspace/x.md"
    assert steps == [("bin", rel), ("tombstone", rel), ("library", rel), ("push", rel)] and not (ws / "x.md").exists()


@pytest.mark.asyncio
async def test_delete_through_an_honest_folder_link_still_deletes(tree, platform_delete):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-del-honest")
    set_session_security(s.session_id, _ctx())      # the place a link lands goes through the path policy
    ws = agents / AGENT / "workspace"
    (ws / "reports").mkdir()
    (ws / "reports" / "a.md").write_text("report\n")
    (ws / "shortcut").symlink_to("reports")
    out = await B.execute(s, "Delete", {"file_path": "/workspace/shortcut/a.md"})
    assert "Recover bin" in out and not (ws / "reports" / "a.md").exists()
    assert platform_delete == ["workspace/reports/a.md"]


@pytest.mark.asyncio
async def test_delete_in_a_moved_agent_folder_reads_one_sentence(tree, platform_delete):
    agents, _ = tree
    disk = agents.parent / "disk"
    disk.mkdir()
    (agents / AGENT).rename(disk / AGENT)
    (agents / AGENT).symlink_to(disk / AGENT)
    s = _Session(_cfg(tree), "direct-del-moved")
    out = await B.execute(s, "Delete", {"file_path": "/workspace/notes.md"})
    assert out == ("Error: cannot delete /workspace/notes.md: this agent's folder was moved to another "
                   "place, and Delete does not work there.")
    assert (disk / AGENT / "workspace" / "notes.md").exists()


@pytest.mark.asyncio
async def test_the_write_bookkeeping_takes_the_rel_written(tree, monkeypatch):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-write-rel")
    set_session_security(s.session_id, _ctx())
    writes: list = []

    async def _push(agent, rel, host, *, writer=None):
        writes.append((rel, host, writer))

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)
    ws = agents / AGENT / "workspace"

    async def call_program(session, operation, args, *, then=None):
        answer = direct_program.run(session, operation, args)
        (ws / "out").rename(ws / "out-aside")           # the folder becomes a link once the file is written
        (ws / "out").symlink_to("../users/alice/workspace")
        await then(answer)
        return answer

    monkeypatch.setattr(df, "call_program", call_program)
    out = await B.execute(s, "Write", {"file_path": "/workspace/out/r.md", "content": "x"})
    assert out.startswith("Created /workspace/out/r.md")
    # The record names the path that was written, from the row's text: nothing is resolved again.
    assert writes == [("workspace/out/r.md", agents.resolve() / AGENT / "workspace" / "out" / "r.md", "alice")]


@pytest.mark.asyncio
async def test_a_written_files_record_is_what_follows_the_programs_answer(tree, monkeypatch):
    """The bookkeeping rides the call as its ``then``: it has run by the
    time the call returns, so a stop of the turn after the program was asked
    never leaves a written file with no author."""
    s = _Session(_cfg(tree), "direct-write-then")
    set_session_security(s.session_id, _ctx())
    order: list = []

    async def _push(agent, rel, host, *, writer=None):
        order.append(("record", rel, writer))

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)

    async def call_program(session, operation, args, *, then=None):
        answer = direct_program.run(session, operation, args)
        await then(answer)
        order.append(("the call returns", operation))
        raise asyncio.CancelledError            # the turn was stopped while the handler waited

    monkeypatch.setattr(df, "call_program", call_program)
    for tool, args in (("Write", {"file_path": "/workspace/w.md", "content": "one\n"}),
                       ("Edit", {"file_path": "/workspace/w.md", "old_string": "one", "new_string": "two"})):
        with pytest.raises(asyncio.CancelledError):
            await B.execute(s, tool, args)
    assert order == [("record", "workspace/w.md", "alice"), ("the call returns", "write"),
                     ("record", "workspace/w.md", "alice"), ("the call returns", "edit")]


@pytest.mark.asyncio
async def test_a_write_that_was_refused_or_not_answered_makes_no_record(tree, monkeypatch, caplog):
    s = _Session(_cfg(tree), "direct-write-norecord")
    set_session_security(s.session_id, _ctx())
    records: list = []

    async def _push(agent, rel, host, *, writer=None):
        records.append(rel)

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)
    for answer in ({"ok": False, "code": "read_only"}, {"ok": False, "code": "timed_out", "seconds": 30},
                   {"ok": False, "code": "unsent"}, {"ok": "yes"}):
        async def call_program(session, operation, args, *, then=None, answer=answer):
            await then(answer)
            return answer

        monkeypatch.setattr(df, "call_program", call_program)
        with caplog.at_level("WARNING", logger="direct-runner"):
            out = await B.execute(s, "Write", {"file_path": "/workspace/w.md", "content": "x"})
        assert out.startswith("Error: "), out
    assert records == []
    # Only the call that was asked and not answered leaves a line: the file may be there with no author.
    said = [r.getMessage() for r in caplog.records if "no record was made" in r.getMessage()]
    assert said == ["Write of /workspace/w.md was asked and not answered (timed_out): no record was made"]


@pytest.mark.asyncio
async def test_a_failing_record_is_logged_and_the_write_still_succeeds(tree, monkeypatch, caplog):
    s = _Session(_cfg(tree), "direct-write-recordfails")
    set_session_security(s.session_id, _ctx())

    async def _push(agent, rel, host, *, writer=None):
        raise RuntimeError("the database is away")

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)
    with caplog.at_level("ERROR", logger="direct-runner"):
        out = await B.execute(s, "Write", {"file_path": "/workspace/w.md", "content": "x"})
    assert out == "Created /workspace/w.md (1 bytes)"
    assert any("write bookkeeping failed for /workspace/w.md" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_write_and_edit_follow_no_link_and_say_so(tree, monkeypatch):
    agents, _ = tree
    s = _Session(_cfg(tree), "direct-write-links")

    async def _push(*_a, **_k):
        return None

    monkeypatch.setattr("services.infra.file_bookkeeping.push_file_write", _push)
    ws = agents / AGENT / "workspace"
    (ws / "real").mkdir()
    (ws / "real" / "a.md").write_text("a\n")
    (ws / "alias").symlink_to("real")
    (ws / "ln.md").symlink_to("real/a.md")
    through = "the path goes through a symlink, so use the real path instead"
    assert await B.execute(s, "Write", {"file_path": "/workspace/alias/a.md", "content": "x"}) == \
        f"Error: cannot write /workspace/alias/a.md: {through}"
    assert await B.execute(s, "Edit", {"file_path": "/workspace/alias/a.md", "old_string": "a", "new_string": "b"}) == \
        f"Error: cannot edit /workspace/alias/a.md: {through}"
    for tool, more in (("Write", {"content": "x"}), ("Edit", {"old_string": "a", "new_string": "b"})):
        assert await B.execute(s, tool, {"file_path": "/workspace/ln.md", **more}) == \
            "Error: /workspace/ln.md is a symlink. Write the real path instead."
    assert (ws / "real" / "a.md").read_text() == "a\n"
