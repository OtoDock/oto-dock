"""Resume spawns must re-ship the agent system prompt.

The CLI rebuilds its system prompt from each invocation's flags — session
transcripts persist messages only — so a ``--resume`` argv without
``--append-system-prompt-file`` re-warms the session with the stock SDK
prompt (no agent identity, duties, or client context). DB-free.
"""
from pathlib import Path

from core.layers.cli.session import PersistentSession


def _mk(resume: bool, claude_dir) -> PersistentSession:
    return PersistentSession(
        session_id="sess-resume001122",
        agent_prompt="AGENT IDENTITY BLOCK",
        mcp_config_path=None,
        model="claude-opus-5",
        effort="high",
        agent_name="agent",
        resume=resume,
        extra_env={"CLAUDE_CONFIG_DIR": str(claude_dir)},
    )


def test_fresh_spawn_appends_system_prompt(tmp_path):
    cmd, _env, _cwd = _mk(False, tmp_path).build_spawn_command()
    assert "--append-system-prompt-file" in cmd
    assert "--session-id" in cmd


def test_resume_spawn_appends_system_prompt(tmp_path):
    cmd, _env, _cwd = _mk(True, tmp_path).build_spawn_command()
    assert "--resume" in cmd
    assert "--append-system-prompt-file" in cmd
    prompt_file = Path(cmd[cmd.index("--append-system-prompt-file") + 1])
    assert "AGENT IDENTITY BLOCK" in prompt_file.read_text()
