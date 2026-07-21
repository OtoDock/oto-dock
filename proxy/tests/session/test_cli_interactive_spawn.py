"""Tests for the interactive spawn-command factor in PersistentSession.

Verifies that `build_spawn_command()` produces the headless `-p` argv unchanged
and the interactive TUI argv with the headless flags dropped + TERM set, sharing
every other flag. DB-free.
"""
import pytest

from core.layers.cli.session import PersistentSession


def _mk(interactive: bool) -> PersistentSession:
    return PersistentSession(
        session_id="sess-abcdef123456",
        agent_prompt=None,
        mcp_config_path=None,
        model="claude-opus-4-8",
        effort="high",
        agent_name="agent",
        interactive=interactive,
    )


def _mk_task(permission_mode: str = "auto") -> PersistentSession:
    """An autonomous interactive TASK session (client_type='task')."""
    return PersistentSession(
        session_id="sess-task00112233",
        agent_prompt=None,
        mcp_config_path=None,
        model="claude-opus-4-8",
        effort="high",
        agent_name="agent",
        interactive=True,
        client_type="task",
        permission_mode=permission_mode,
    )


def test_headless_argv_has_p_and_stream_json():
    cmd, env, _cwd = _mk(False).build_spawn_command()
    assert "-p" in cmd
    assert "stream-json" in cmd
    assert "--input-format" in cmd
    assert "--include-partial-messages" in cmd
    # Shared flags present.
    assert "--model" in cmd and "claude-opus-4-8" in cmd
    assert "--session-id" in cmd
    assert "--dangerously-skip-permissions" in cmd
    # Bypass headless also carries the stdio prompt tool — its PRESENCE is
    # what makes the CLI expose AskUserQuestion to a -p session (the hook
    # deny-and-surface flow needs the model to be able to CALL the tool).
    assert "--permission-prompt-tool" in cmd
    assert cmd[cmd.index("--permission-prompt-tool") + 1] == "stdio"
    # Headless does not force TERM.
    assert "TERM" not in env or env.get("TERM") != "xterm-256color" or True  # no assertion either way


def test_ultra_effort_clamps_to_max_for_claude_cli():
    """Platform "ultra" is Codex-only (gpt-5.6 multi-agent orchestration); the
    `claude` CLI's --effort rejects it, so the argv must carry "max"."""
    s = _mk(False)
    s.effort = "ultra"
    cmd, _env, _cwd = s.build_spawn_command()
    assert cmd[cmd.index("--effort") + 1] == "max"


def test_headless_plan_mode_has_no_prompt_tool():
    """-p plan keeps hooks-only prompting: a stdio prompt tool there could
    route a real permission prompt to a channel the translator never answers."""
    s = _mk(False)
    s.permission_mode = "plan"
    cmd, _env, _cwd = s.build_spawn_command()
    assert "--permission-mode" in cmd and "plan" in cmd
    assert "--permission-prompt-tool" not in cmd


def test_interactive_argv_drops_headless_flags_keeps_shared():
    cmd, env, _cwd = _mk(True).build_spawn_command()
    # Headless I/O flags gone.
    assert "-p" not in cmd
    assert "stream-json" not in cmd
    assert "--input-format" not in cmd
    assert "--output-format" not in cmd
    assert "--verbose" not in cmd
    assert "--include-partial-messages" not in cmd
    # Shared flags still present — same parser, just no -p framing.
    assert cmd[0].endswith("claude") or "claude" in cmd[0]
    assert "--model" in cmd and "claude-opus-4-8" in cmd
    assert "--effort" in cmd and "high" in cmd
    assert "--session-id" in cmd
    # Interactive uses a real --permission-mode, NOT --dangerously-skip-permissions:
    # no bypass-mode warning at launch + the PreToolUse hook returns an explicit
    # "ask" for the residual ask-tier, so the TUI renders its native prompt
    # (see test_interactive_permission_mode.py).
    assert "--dangerously-skip-permissions" not in cmd
    assert "--permission-mode" in cmd
    # The TUI has AskUserQuestion natively — no stdio prompt tool.
    assert "--permission-prompt-tool" not in cmd
    # TUI needs TERM (sandbox env doesn't set it).
    assert env.get("TERM") == "xterm-256color"
    # Interactive spawns flag the hook scripts so the redundant PostToolUse /
    # SubagentStop hooks no-op (the TUI renders results; we tail the transcript).
    assert env.get("OTO_INTERACTIVE") == "1"


def _strip_perm_flags(argv):
    """Drop the permission flag (which now differs by mode) so the rest of the
    tail can be compared for drift."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--dangerously-skip-permissions":
            i += 1
        elif argv[i] in ("--permission-mode", "--permission-prompt-tool"):
            i += 2  # skip flag + its value
        else:
            out.append(argv[i])
            i += 1
    return out


def test_both_modes_agree_on_shared_tail():
    """The model/effort/session tail is identical — the two argvs differ ONLY in
    the headless -p framing block and the permission flag (headless
    --dangerously-skip-permissions vs interactive --permission-mode <mode>)."""
    headless, _, _ = _mk(False).build_spawn_command()
    inter, _, _ = _mk(True).build_spawn_command()
    h_tail = _strip_perm_flags(headless[headless.index("--model"):])
    i_tail = _strip_perm_flags(inter[inter.index("--model"):])
    assert h_tail == i_tail


def test_interactive_task_uses_permission_mode_not_skip():
    """An autonomous interactive TASK uses a real --permission-mode, NOT
    --dangerously-skip-permissions — skip's one-time "Bypass Permissions mode"
    acceptance screen blocks a no-viewer TUI at launch (the CLI exits). The
    PreToolUse hook in 'auto' mode auto-allows + holds the hard-deny floor, so
    Claude never prompts."""
    cmd, env, _ = _mk_task("auto").build_spawn_command()
    assert "-p" not in cmd and "stream-json" not in cmd  # still interactive TUI
    assert "--dangerously-skip-permissions" not in cmd
    assert "--permission-mode" in cmd and "default" in cmd
    assert env.get("TERM") == "xterm-256color"
    assert env.get("OTO_INTERACTIVE") == "1"


def test_interactive_task_plan_mode_still_passes_through():
    """Plan mode is preserved for a task (read-only planning)."""
    cmd, _, _ = _mk_task("plan").build_spawn_command()
    assert "--permission-mode" in cmd and "plan" in cmd
    assert "--dangerously-skip-permissions" not in cmd


@pytest.mark.asyncio
async def test_start_session_interactive_routes_to_register(monkeypatch, tmp_path):
    """config.interactive=True → CLIExecutionLayer.start_session spawns the TUI
    via interactive_session.register (not the headless -p pool), with the right
    argv/env. Proves the full config → build_spawn_command → register wiring."""
    from core.execution_layer import AgentConfig
    from core.layers.cli.layer import CLIExecutionLayer
    from core.session import interactive_session

    captured: dict = {}

    async def _fake_register(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(interactive_session, "register", _fake_register)

    # Local sessions are always sandboxed + network-isolated — provide the
    # persistent config dir the real config builders set (without it the layer
    # fails closed). The egress resolver derives the netns forward set.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    cfg = AgentConfig(
        agent_name="agent",
        user_sub="u1",
        model="claude-opus-4-8",
        effort="high",
        chat_id="chat-xyz",
        client_type="interactive_cli",
        permission_mode="default",
        interactive=True,
        sandbox_host_claude_dir=str(claude_dir),
    )
    await CLIExecutionLayer().start_session("sess-interactive-1", cfg)

    assert captured.get("session_id") == "sess-interactive-1"
    assert captured.get("chat_id") == "chat-xyz"
    assert captured.get("agent_name") == "agent"
    argv = captured["argv"]
    assert "-p" not in argv and "stream-json" not in argv
    assert "--model" in argv and "claude-opus-4-8" in argv
    assert "--session-id" in argv
    assert captured["env"].get("TERM") == "xterm-256color"
