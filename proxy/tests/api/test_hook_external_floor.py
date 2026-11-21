"""The permission-hook floor for an outside session, before the path gate
and before any mode branch: the shell, sub-agents and workflows never; the
web tools and the write tools only when a manager picked them; a tool of an
attached server only when its function was granted."""

from __future__ import annotations

import uuid

import pytest

from api.hooks.hooks import decide_tool_permission
from auth.path_policy import SecurityContext
from core.events import tool_roles
from core.session import session_state


def _register(**facts) -> str:
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "auto", SecurityContext(
        role="manager", username="", agent="support", is_admin_agent=False,
        session_scope="agent", principal="external", external_claim="phone:+1", **facts,
    ))
    return sid


@pytest.fixture
def external_session():
    sid = _register()
    yield sid
    session_state.cleanup_session_permission_state(sid)


@pytest.fixture
def picked_session():
    """An outside session whose managers picked the web, the files Read and
    write, and the crm's find function."""
    sid = _register(outside_web=True, outside_files_write=True,
                    outside_grants=(("crm", ("find",)), ("calls", None)))
    yield sid
    session_state.cleanup_session_permission_state(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", tool_roles.names_of(tool_roles.SHELL) + tool_roles.names_of(tool_roles.SUBAGENT)
                         + tool_roles.names_of(tool_roles.WORKFLOW))
async def test_denied_even_for_a_manager_in_auto_mode(external_session, picked_session, tool):
    for sid in (external_session, picked_session):
        decision = await decide_tool_permission(sid, tool, {"command": "ls", "prompt": "x", "name": "w"})
        assert decision["decision"] == "deny"
        assert "outside people" in decision["reason"]


@pytest.mark.asyncio
async def test_reading_and_searching_are_not_floored(external_session):
    decision = await decide_tool_permission(external_session, "Glob", {"pattern": "*.md"})
    assert decision["decision"] == "allow"
    decision = await decide_tool_permission(external_session, "Grep", {"pattern": "x"})
    assert decision["decision"] == "allow"


@pytest.mark.asyncio
async def test_the_web_tools_follow_the_web_pick(external_session, picked_session):
    for tool, args in (("WebSearch", {"query": "otodock phone routes"}),
                       ("WebFetch", {"url": "https://docs.otodock.io/features/phone"})):
        decision = await decide_tool_permission(external_session, tool, args)
        assert decision["decision"] == "deny" and "web" in decision["reason"]
        decision = await decide_tool_permission(picked_session, tool, args)
        assert decision["decision"] == "allow"
    # The SSRF gate still guards a picked WebFetch.
    decision = await decide_tool_permission(picked_session, "WebFetch", {"url": "http://192.168.1.10/admin"})
    assert decision["decision"] == "deny" and "private" in decision["reason"]


@pytest.mark.asyncio
async def test_the_write_tools_follow_the_files_pick(external_session, tmp_path):
    """Without the files pick every write tool is floored; with it a write
    inside the person's own folder goes on to the path gate and passes,
    and one outside it is still refused there."""
    home = tmp_path / "agents" / "support" / "externals" / "phone" / "1"
    (home / "workspace").mkdir(parents=True)
    picked = _register(outside_web=True, outside_files_write=True, external_home=str(home))
    def args(path: str) -> dict:
        # A tool that names its file by path, and a patch that names it in
        # the patch text (Codex's apply_patch).
        patch = f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch\n"
        return {"file_path": path, "content": "x", "old_string": "a", "new_string": "b",
                "edits": [], "input": patch}
    try:
        for tool in tool_roles.names_of(tool_roles.WRITE):
            decision = await decide_tool_permission(external_session, tool, args("/caller/workspace/a.md"))
            assert decision["decision"] == "deny" and "Writing files" in decision["reason"], tool
            decision = await decide_tool_permission(picked, tool, args("/caller/workspace/a.md"))
            assert decision["decision"] == "allow", (tool, decision)
            decision = await decide_tool_permission(picked, tool, args("/workspace/a.md"))
            assert decision["decision"] == "deny" and "outside people" in decision["reason"], (tool, decision)
    finally:
        session_state.cleanup_session_permission_state(picked)


@pytest.mark.asyncio
async def test_a_tools_function_follows_the_grant(external_session, picked_session):
    for sid, tool, expect in (
        (external_session, "mcp__crm__find", "deny"),      # nothing granted
        (picked_session, "mcp__crm__find", "allow"),        # granted by name
        (picked_session, "mcp__crm__update", "deny"),       # not granted
        (picked_session, "mcp__calls__end_call", "allow"),  # every function
        (picked_session, "mcp__other__x", "deny"),          # not attached
    ):
        decision = await decide_tool_permission(sid, tool, {"q": "x"})
        assert decision["decision"] == expect, (tool, decision)
        if expect == "deny":
            assert "outside people" in decision["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["collaborationspawn_agent", "collaborationsend_message",
                                  "collaboration__spawn_agent", "collaboration__followup_task",
                                  "image_gen.imagegen", "image_genimagegen"])
async def test_codexs_tools_are_refused_under_every_join_of_their_names(picked_session, tool):
    """Codex joins a namespace and a tool with a dot, a double underscore or
    nothing (its hook drops the dot: the T1 walk saw ``collaborationspawn_agent``):
    the floor refuses every join, with or without the engine's translation
    in front of it, and the hook denies the call."""
    from api.hooks.permission import outside_floor
    from core.layers.codex import tool_names
    assert tool_roles.outside_never(tool) and tool_roles.role_of(tool) == ""
    assert outside_floor(picked_session, tool, "")
    assert outside_floor(picked_session, tool_names.canonical(tool), "")
    decision = await decide_tool_permission(picked_session, tool, {"message": "hello"})
    assert decision["decision"] == "deny" and "outside people" in decision["reason"]


def test_an_ordinary_tool_is_not_a_never_name():
    for name in ("Read", "clock__curr_time", "clock.sleep", "mcp__crm__find", "collaboration"):
        assert not tool_roles.outside_never(name), name


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(tool_roles.OUTSIDE_NEVER_NAMES))
async def test_the_engine_tools_with_no_role_an_outside_session_never_gets(picked_session, tool):
    """The messages between a session's agents, a worktree, a design sync,
    Codex's agents and its image generation: refused by name whatever was
    picked, and never a role (an ordinary chat's answer does not move)."""
    from api.hooks.permission import outside_floor
    assert outside_floor(picked_session, tool, "") == f"{tool} is not available to outside people."
    assert tool_roles.role_of(tool) == ""
