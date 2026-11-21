"""The CLI argv denies an outside session the shell, sub-agents, workflows,
and the web and write tools its managers did not pick."""

from __future__ import annotations

from core.events import tool_roles
from core.layers.cli.layer import CLIExecutionLayer
from core.layers.cli.spawn import ClaudeSpawn


def _cmd(**kw) -> list[str]:
    s = ClaudeSpawn(
        session_id="00000000-0000-4000-8000-000000000000", agent_prompt="x",
        mcp_config_path=None, permission_mode="auto", model="claude-sonnet-5",
        effort="medium", agent_name="support", **kw,
    )
    return s._build_persistent_cmd()


def test_external_sessions_disallow_the_shell_and_what_was_not_picked():
    from types import SimpleNamespace
    cmd = _cmd(disallowed_tools=CLIExecutionLayer().session_denied_tools(external=True, read_only=False))
    i = cmd.index("--disallowedTools")
    never = ",".join(sorted(tool_roles.OUTSIDE_NEVER_NAMES))
    assert cmd[i + 1] == (f"Bash,Monitor,PowerShell,Agent,Task,Workflow,{never},"
                          "WebFetch,WebSearch,Write,Edit,MultiEdit,NotebookEdit")
    assert "--dangerously-skip-permissions" in cmd
    picked = SimpleNamespace(outside_web=True, outside_files_write=True, outside_grants=())
    denied = CLIExecutionLayer().session_denied_tools(external=True, read_only=False, ctx=picked)
    assert denied == ["Bash", "Monitor", "PowerShell", "Agent", "Task", "Workflow",
                      *sorted(tool_roles.OUTSIDE_NEVER_NAMES)]


def test_the_functions_a_pick_leaves_out_are_denied_by_name(monkeypatch):
    """An attached server's known functions outside the grant ride the argv
    too, so the model is not offered what the floor would refuse; a server
    picked whole, or one never listed, adds nothing."""
    from types import SimpleNamespace

    from services.mcp import mcp_permissions
    known = {"crm": ["find", "update", "delete"], "files": ["read", "write"]}
    monkeypatch.setattr(mcp_permissions, "known_tools", lambda key: list(known.get(key, [])))
    ctx = SimpleNamespace(outside_web=True, outside_files_write=True,
                          outside_grants=(("crm", ("find",)), ("files", None), ("unlisted", ("x",))))
    denied = CLIExecutionLayer().session_denied_tools(external=True, read_only=False, ctx=ctx)
    assert [d for d in denied if d.startswith("mcp__")] == ["mcp__crm__update", "mcp__crm__delete"]


def test_other_sessions_are_untouched():
    assert "--disallowedTools" not in _cmd()


def test_the_functions_a_limit_leaves_out_are_denied_for_every_session(monkeypatch):
    """A tool limit (services/mcp/tool_limits) rides the argv of a chat or
    a run as a pick rides an outside session's: the known functions outside
    the limit, never a server without one."""
    from types import SimpleNamespace

    from services.mcp import mcp_permissions
    known = {"mail": ["read_mail", "send_mail", "list_labels"], "crm": ["find"]}
    monkeypatch.setattr(mcp_permissions, "known_tools", lambda key: list(known.get(key, [])))
    ctx = SimpleNamespace(tool_limits=(("mail", ("read_mail", "list_labels")), ("unlisted", ("x",))))
    denied = CLIExecutionLayer().session_denied_tools(external=False, read_only=False, ctx=ctx)
    assert denied == ["mcp__mail__send_mail"]
    assert CLIExecutionLayer().session_denied_tools(external=False, read_only=False,
                                                    ctx=SimpleNamespace(tool_limits=())) == []
