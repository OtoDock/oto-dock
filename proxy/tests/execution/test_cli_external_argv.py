"""The CLI argv denies the shell on external sessions."""

from __future__ import annotations

from core.layers.cli.layer import CLIExecutionLayer
from core.layers.cli.session import PersistentSession


def _cmd(**kw) -> list[str]:
    s = PersistentSession(
        session_id="00000000-0000-4000-8000-000000000000", agent_prompt="x",
        mcp_config_path=None, permission_mode="auto", model="claude-sonnet-5",
        effort="medium", agent_name="support", **kw,
    )
    return s._build_persistent_cmd()


def test_external_sessions_disallow_the_shell():
    cmd = _cmd(disallowed_tools=CLIExecutionLayer().session_denied_tools(external=True, read_only=False))
    i = cmd.index("--disallowedTools")
    assert cmd[i + 1] == "Bash,Monitor,PowerShell"
    assert "--dangerously-skip-permissions" in cmd


def test_other_sessions_are_untouched():
    assert "--disallowedTools" not in _cmd()
