"""Direct-LLM client-side builtins (core/layers/direct/builtins.py): the
registry, the two-pass permission gate (path policy, then tier × mode) and
the file-tool handlers with their platform bookkeeping."""

from __future__ import annotations

import pytest

from auth.path_policy import SecurityContext
from core.layers.direct import builtins as B, files as df
from core.sandbox.sandbox import SandboxConfig
from core.session.session_state import set_session_security

AGENT = "pa"


@pytest.fixture
def tree(tmp_path):
    agents = tmp_path / "agents"
    a = agents / AGENT
    for d in ("config", "workspace", "knowledge/memory",
              "users/alice/workspace", "users/alice/context", "users/alice/.claude"):
        (a / d).mkdir(parents=True)
    (a / "workspace" / "notes.md").write_text("line one\nline two\n")
    mcps = tmp_path / "mcps"
    mcps.mkdir()
    return agents, mcps


def _cfg(tree, role="manager", username="alice"):
    agents, mcps = tree
    claude = agents / AGENT / (f"users/{username}/.claude" if username else "workspace/.claude")
    claude.mkdir(parents=True, exist_ok=True)
    return SandboxConfig(
        role=role, username=username, agent_name=AGENT, is_admin_agent=False,
        host_agents_dir=agents.resolve(), host_mcps_dir=mcps.resolve(),
        host_claude_dir=claude.resolve(), net_forwards=["8400"],
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

@pytest.mark.parametrize("mode", ["default", "acceptEdits", "dontAsk", "auto", "plan"])
def test_open_tier_always_allows(mode):
    assert B.decide(B.TIER_OPEN, mode) == "allow"


@pytest.mark.parametrize("mode,expected", [
    ("default", "prompt"), ("acceptEdits", "allow"), ("dontAsk", "allow"),
    ("auto", "allow"), ("plan", "deny"),
])
def test_edit_tier(mode, expected):
    assert B.decide(B.TIER_EDIT, mode) == expected


@pytest.mark.parametrize("mode,expected", [
    ("default", "prompt"), ("acceptEdits", "prompt"), ("dontAsk", "allow"),
    ("auto", "allow"), ("plan", "deny"),
])
def test_destructive_tier_prompts_even_in_accept_edits(mode, expected):
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
        "acceptEdits",
    )
    assert outcome == "deny" and "memory" in reason
    write = {"name": "Write", "input": {"file_path": "/workspace/x.md", "content": "y"}}
    assert B.gate(s, write, "acceptEdits") == ("allow", "")
    assert B.gate(s, write, "default")[0] == "prompt"
    assert B.gate(s, {"name": "Delete", "input": {"file_path": "/workspace/x.md"}}, "acceptEdits")[0] == "prompt"
    assert B.gate(s, {"name": "Read", "input": {"file_path": "/workspace/x.md"}}, "plan") == ("allow", "")
    outcome, reason = B.gate(s, write, "plan")
    assert outcome == "deny" and "plan mode" in reason
    assert B.gate(s, {"name": "Nope", "input": {}}, "default")[0] == "deny"


def test_gate_without_security_context_fails_closed(tree):
    s = _Session(_cfg(tree), "direct-no-ctx")
    outcome, reason = B.gate(
        s, {"name": "Read", "input": {"file_path": "/workspace/notes.md"}}, "default",
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

    async def _delete(agent, agent_dir, target):
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
    msg = "Error: no writable location in this session — every folder is read-only"
    assert await B.execute(s, "Write", {"file_path": "/workspace/x.md", "content": "y"}) == msg
    assert await B.execute(s, "Delete", {"file_path": "/workspace/notes.md"}) == msg
    assert "line one" in await B.execute(s, "Read", {"file_path": "/workspace/notes.md"})


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
        "Error: /workspace is a folder — Delete removes single files"
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
    assert await B.gate_for_session(s, write, "default") == B.gate(s, write, "default")
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
    session_state.set_session_mode(_HOST, "default")
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
