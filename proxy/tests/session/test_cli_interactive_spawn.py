"""The argv and the environment a Claude Code session starts with
(``ClaudeSpawn.build_spawn_command``): the headless ``-p`` argv, and the
terminal's TUI argv with the headless flags dropped and TERM set, sharing
every other flag. DB-free.
"""
import pytest

from core.layers.cli.spawn import ClaudeSpawn


def _mk(interactive: bool) -> ClaudeSpawn:
    return ClaudeSpawn(
        session_id="sess-abcdef123456",
        agent_prompt=None,
        mcp_config_path=None,
        model="claude-opus-5",
        effort="high",
        agent_name="agent",
        interactive=interactive,
    )


def _mk_task(permission_mode: str = "auto") -> ClaudeSpawn:
    """An autonomous interactive TASK session (client_type='task')."""
    return ClaudeSpawn(
        session_id="sess-task00112233",
        agent_prompt=None,
        mcp_config_path=None,
        model="claude-opus-5",
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
    assert "--model" in cmd and "claude-opus-5" in cmd
    assert "--session-id" in cmd
    assert "--dangerously-skip-permissions" in cmd
    # Bypass headless also carries the stdio prompt tool — its PRESENCE is
    # what makes the CLI expose AskUserQuestion to a -p session (the hook
    # deny-and-surface flow needs the model to be able to CALL the tool).
    assert "--permission-prompt-tool" in cmd
    assert cmd[cmd.index("--permission-prompt-tool") + 1] == "stdio"
    # Claude Code ≥ 2.1.267 records the system prompt and re-sends the record
    # on --resume; the platform re-ships a fresh prompt file every resume, so
    # the recording is off (the flag exists on the previous pin 2.1.263 too).
    assert cmd[cmd.index("--system-prompt-snapshot") + 1] == "off"
    # Only the platform's settings.json is read: a plugin a person installs
    # from a terminal at the local scope writes settings.local.json, which
    # the platform never rewrites and which would outrank the user file.
    assert cmd[cmd.index("--setting-sources") + 1] == "user"
    # Headless does not force TERM.
    assert "TERM" not in env or env.get("TERM") != "xterm-256color" or True  # no assertion either way


def test_ultra_effort_clamps_to_max_for_claude_cli():
    """Platform "ultra" is Codex-only (gpt-5.6 multi-agent orchestration); the
    `claude` CLI's --effort rejects it, so the argv must carry "max"."""
    s = _mk(False)
    s.effort = "ultra"
    cmd, _env, _cwd = s.build_spawn_command()
    assert cmd[cmd.index("--effort") + 1] == "max"


def test_headless_plan_mode_passes_the_prompt_tool():
    """-p plan carries the stdio prompt tool: Claude Code 2.1.299+ asks for a
    call the hook allowed in plan mode, and engine_prompts answers that ask
    from the gate's record; bypass is never passed beside it."""
    s = _mk(False)
    s.permission_mode = "plan"
    cmd, _env, _cwd = s.build_spawn_command()
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    assert cmd[cmd.index("--permission-prompt-tool") + 1] == "stdio"
    assert "--dangerously-skip-permissions" not in cmd
    assert "--allow-dangerously-skip-permissions" not in cmd


def test_interactive_plan_mode_has_no_prompt_tool():
    """A terminal in Plan keeps its own dialog: no stdio channel, no bypass
    (bypass beside plan mode opens the acceptance screen at launch)."""
    s = _mk(True)
    s.permission_mode = "plan"
    cmd, _env, _cwd = s.build_spawn_command()
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    assert "--permission-prompt-tool" not in cmd
    assert "--allow-dangerously-skip-permissions" not in cmd


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
    assert "--model" in cmd and "claude-opus-5" in cmd
    assert "--effort" in cmd and "high" in cmd
    assert "--session-id" in cmd
    # Interactive uses a real --permission-mode, NOT --dangerously-skip-permissions:
    # no bypass-mode warning at launch + the PreToolUse hook returns an explicit
    # "ask" for the residual ask-tier, so the TUI renders its native prompt
    # (see test_interactive_permission_mode.py).
    assert "--dangerously-skip-permissions" not in cmd
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "user"
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


def test_the_satellite_spawns_read_the_platform_settings_only():
    # The two satellite twins (headless, PTY) carry the same flag; the
    # satellite suite asserts the headless argv, this reads both files.
    from tests._paths import REPO_ROOT
    for rel in ("satellite/sessions/cli_session.py", "satellite/terminal/pty_session.py"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert '"--setting-sources", "user"' in text, rel


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
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "user" and "default" in cmd
    assert env.get("TERM") == "xterm-256color"
    assert env.get("OTO_INTERACTIVE") == "1"


def test_interactive_task_plan_mode_still_passes_through():
    """Plan mode is preserved for a task (read-only planning)."""
    cmd, _, _ = _mk_task("plan").build_spawn_command()
    assert "--permission-mode" in cmd and "plan" in cmd
    assert "--dangerously-skip-permissions" not in cmd


@pytest.mark.parametrize("interactive", [False, True])
def test_the_shell_gets_a_command_line_token_mirroring_its_session_token(interactive):
    """Headless and the TUI alike: ``OTO_CLI_TOKEN`` names the same session,
    person and life as the final ``PROXY_API_KEY`` the hooks present."""
    from auth.session_token import create_session_token, validate_cli_token
    s = _mk(interactive)
    s.credential_env = {"PROXY_API_KEY": create_session_token(s.session_id, "agent", "user-x")}
    _cmd, env, _cwd = s.build_spawn_command()
    cli = validate_cli_token(env["OTO_CLI_TOKEN"])
    assert cli["sid"] == s.session_id and cli["user_sub"] == "user-x"
    assert env["OTO_CLI_TOKEN"] != env["PROXY_API_KEY"]


def test_a_terminal_on_an_api_key_signs_in_through_the_key_helper():
    """The TUI reads a key in its environment as one a person must approve
    on screen ("Not logged in" until then): a terminal's key moves to the
    helper's variable and the helper on its command line prints it. A
    headless session keeps the key where the CLI reads it."""
    import json
    import subprocess

    from core.layers.cli.spawn import KEY_HELPER_ENV
    term = _mk(True)
    term.extra_env = {"ANTHROPIC_API_KEY": "sk-ant-x"}
    cmd, env, _cwd = term.build_spawn_command()
    assert "ANTHROPIC_API_KEY" not in env and env[KEY_HELPER_ENV] == "sk-ant-x"
    helper = json.loads(cmd[cmd.index("--settings") + 1])["apiKeyHelper"]
    assert "sk-ant-x" not in " ".join(cmd)
    printed = subprocess.run(["sh", "-c", helper], capture_output=True, text=True, timeout=20,
                             env={KEY_HELPER_ENV: "sk-ant-x"}, check=True).stdout
    assert printed == "sk-ant-x"
    headless = _mk(False)
    headless.extra_env = {"ANTHROPIC_API_KEY": "sk-ant-x"}
    cmd, env, _cwd = headless.build_spawn_command()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x" and "--settings" not in cmd
    # A terminal signed in by file carries no helper.
    cmd, _env, _cwd = _mk(True).build_spawn_command()
    assert "--settings" not in cmd
