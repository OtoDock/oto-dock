"""Interactive PTY session for the satellite daemon — Claude.

The remote counterpart of the proxy's interactive Claude CLI: instead of
``claude -p`` streaming NDJSON (see ``cli_session.CLISession``), this spawns the
native Claude TUI under a PTY and is driven by the proxy over the WS. The shared
spine (PTY spawn + output/exit wiring, ``pty_input``/``pty_resize``/``pty_close``,
and the transcript-forwarding tail loop) lives in :class:`BasePtySession`;
this subclass adds only the Claude-specific bits — the ``.claude`` config tree, the
INTERACTIVE command (no ``-p``/stream-json/verbose, plus ``TERM``, and
``--permission-mode`` — NEVER ``--dangerously-skip-permissions``, which shows the
bypass-acceptance screen a no-viewer can't clear), the first-run seed, and locating
``<session_id>.jsonl`` by name. (Codex = :class:`CodexPtySession`.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path

from ..config import otodock_dir, session_tools_env, write_command_wrappers
from ..host import safe_fs
from ..sessions import session_homes
from ..sessions.cli_session import (
    _write_cli_hooks,
    _write_hook_scripts,
    remove_prompt_file,
    write_prompt_file,
)
from .pty_session_base import BasePtySession

logger = logging.getLogger("satellite")

# Claude Code's --permission-mode values a proxy before 1.9 could send as
# the platform mode. Anything else (its "dontAsk"/"auto") maps to "default":
# the PreToolUse hook still enforces the floor + auto-allows, so the TUI
# never blocks.
_CLAUDE_PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}
# The CLI words a 1.9 proxy's ``cli_permission_mode`` starts a terminal in
# by themselves: Auto (the CLI's classifier) and Plan; every other mode starts
# in "default", the hook deciding above it.
_TERMINAL_OWN_MODES = {"auto", "plan"}
# How many entries the ``projects`` folder may list before a search finds
# nothing (the platform's ``transcript_tailer._PROJECTS_MAX_ENTRIES``).
_PROJECTS_MAX_ENTRIES = 10_000


# The Claude Code TUI reads a key in its environment as a custom key a person
# must approve on screen, and says "Not logged in" until one does. A terminal
# on an API key signs in through a key helper given on its own command line
# instead: the helper prints the key from a variable of the terminal's
# environment, so nothing of the key is written to a file. The platform's own
# terminal does the same (``proxy/core/layers/cli/session.py``). The helper is
# this satellite's Python, which runs under the shell Claude Code uses on
# every system.
KEY_HELPER_ENV = "OTO_CLAUDE_API_KEY"


def key_helper_args(env: dict) -> list[str]:
    """The flags that sign a terminal in on the API key ``env`` carries,
    the key moved to ``KEY_HELPER_ENV`` in ``env`` (``[]``: no key)."""
    key = env.pop("ANTHROPIC_API_KEY", "")
    if not key:
        return []
    env[KEY_HELPER_ENV] = key
    helper = (f'"{sys.executable}" -c "import os,sys;'
              f"sys.stdout.write(os.environ.get('{KEY_HELPER_ENV}', ''))\"")
    return ["--settings", json.dumps({"apiKeyHelper": helper})]


def terminal_permission_mode(config: dict) -> str:
    """The ``--permission-mode`` the terminal starts in: the CLI's own word a
    1.9 proxy ships when it is Auto or Plan, else ``default``; an older
    proxy's platform word when the CLI knows it."""
    cli_word = config.get("cli_permission_mode") or ""
    if cli_word:
        return cli_word if cli_word in _TERMINAL_OWN_MODES else "default"
    mode = config.get("permission_mode", "default")
    return mode if mode in _CLAUDE_PERMISSION_MODES else "default"


def _seed_interactive_cli_config(claude_dir: Path, cwd: str, theme: str = "dark") -> None:
    """Pre-seed ``.claude.json`` so the interactive TUI launches PAST first-run.

    Satellite reimplementation of the proxy's
    ``cli_settings_manager.seed_interactive_cli_config`` (pure stdlib).
    Headless ``-p`` never runs onboarding, but the TUI does: a fresh
    CLAUDE_CONFIG_DIR has no "onboarding complete" state, so Claude runs
    theme-picker → login → per-folder trust on every launch and masks the env
    OAuth auth. Merge the skip-the-wizard flags in (idempotent; written BEFORE
    spawn, after which Claude owns the file).
    """
    cfg = claude_dir / ".claude.json"
    try:
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["hasCompletedOnboarding"] = True
    # Fresh config dir per spawn → once-per-install notices re-trigger every
    # session; mark the (noisy) auto-mode entry banner as already seen.
    data["hasSeenAutoModeEntryWarning"] = True
    # Suppress Claude Code's resume-from-summary picker (≥2.1.x, old + large
    # sessions): platform cold-flushed prompts would type into the picker and
    # the deferred Enter would confirm "resume from summary", degrading the
    # session. Same key the picker's "Don't ask me again" writes, and the one
    # 2.1.304's "Resume this conversation?" usage question checks; the proxy
    # twin (cli_settings_manager) carries the full rationale.
    data["resumeReturnDismissed"] = True
    data["theme"] = "light" if str(theme).lower().startswith("light") else "dark"
    if cwd:
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = data["projects"] = {}
        # Key the trust entry under BOTH the native (backslash) AND forward-slash
        # forms of the cwd. Claude Code on Windows works with forward-slash paths
        # internally (same reason pty_session forward-slashes the MCP config), so a
        # backslash-only key never matched its cwd lookup and the "trust this
        # folder?" dialog showed on EVERY launch. On Linux/macOS the two forms are
        # identical (no backslashes) → a single key, unchanged behaviour.
        for key in {cwd, cwd.replace("\\", "/")}:
            proj = projects.setdefault(key, {})
            if not isinstance(proj, dict):
                proj = projects[key] = {}
            proj["hasTrustDialogAccepted"] = True
            proj["hasCompletedProjectOnboarding"] = True
            proj.setdefault("projectOnboardingSeenCount", 1)
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        logger.warning("pty seed: could not write %s", cfg, exc_info=True)


class PtySession(BasePtySession):
    """A Claude TUI subprocess on a PTY, driven by the proxy over the WS."""

    execution_path = "claude-code-cli"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._claude_dir: Path | None = None
        # The folder the TUI reads: its home in ``_claude_dir`` when the start
        # asks for one (``sessions/session_homes.py``), else that dir.
        self._engine_dir: Path | None = None
        # The session id whose JSONL holds this run's turns: the resume id on
        # --resume (Claude appends to the original <id>.jsonl), else our own id.
        self._transcript_target_id = self.session_id

    async def _start_compute(self) -> None:
        """A terminal on a compute machine: the platform wrote the session's
        files into the shared folder and planned the TUI's argv; this machine
        renders the sandbox and spawns the plan with the plan's environment
        and no cwd of its own. The transcript tail reads the shared folder."""
        from ..sessions import credential_exit, sandbox_host, ssh_agent
        self._own_copy = await sandbox_host.ready_own_copy(self.config, self.sat_config,
                                                           self.session_id, self.agent_dir)
        self._claude_dir = self.agent_dir / self.config["claude_dir_relative"]
        # The session's SSH agent socket before the render, which binds it;
        # the credential exit's door before it too (the render binds the bundle).
        agent_env = await asyncio.to_thread(ssh_agent.start, self.session_id, self.config)
        await asyncio.to_thread(credential_exit.start, self.session_id, self.config)
        cmd = await sandbox_host.spawn_command(self.config, self.sat_config, "claude", for_pty=True)
        env = sandbox_host.spawn_env(self.config)
        ssh_agent.merge_env(env, agent_env, self.config)
        if self.config.get("resume") and self.config.get("session_id_for_resume"):
            self._transcript_target_id = self.config["session_id_for_resume"]
            self._transcript_skip_existing = True
        await self._wire_and_spawn(cmd, env, None)

    async def start(self) -> None:
        """Write config files + spawn the interactive Claude TUI under a PTY."""
        if self._compute:
            await self._start_compute()
            return
        # --- session files + env (mirrors CLISession.start setup) -------------
        # cwd may be an arbitrary absolute folder (otodock-CLI); the config
        # dir stays agent_dir-rooted via claude_dir_relative regardless.
        self._cwd = self._resolve_work_cwd()
        self._claude_dir = self.agent_dir / self.config["claude_dir_relative"]
        self._claude_dir.mkdir(parents=True, exist_ok=True)
        self._engine_dir = session_homes.open_home(
            self._claude_dir, self.session_id, self.config.get("session_home"))

        # Wipe Claude Code's built-in auto-memory (the otodock memory-mcp is the
        # single source of truth); leave session JSONLs for --resume.
        projects_dir = self._claude_dir / "projects"
        if projects_dir.exists():
            import shutil as _shutil
            for proj in projects_dir.iterdir():
                if proj.is_dir() and (proj / "memory").exists():
                    _shutil.rmtree(proj / "memory", ignore_errors=True)

        _write_hook_scripts(self._claude_dir, self.config.get("hook_scripts", {}))
        write_command_wrappers(self._claude_dir, self.config.get("command_wrappers", {}))

        prompt_file = write_prompt_file(
            self._claude_dir, self.session_id, self.config.get("system_prompt", ""))

        mcp_config = self.config.get("mcp_config")
        mcp_file = self._claude_dir / f"mcp-config-{self.session_id[:12]}.json"
        (self._claude_dir / "mcp-config.json").unlink(missing_ok=True)
        if mcp_config and mcp_config.get("mcpServers"):
            from ..sessions.mcp_interceptor import wrap_interceptor_in_mcp_config
            wrap_interceptor_in_mcp_config(mcp_config)
            mcp_raw = json.dumps(mcp_config, indent=2)
            otodock_fwd = str(otodock_dir()).replace("\\", "/")
            home_fwd = str(Path.home()).replace("\\", "/")
            mcp_raw = mcp_raw.replace("~/.oto-dock", otodock_fwd)
            mcp_raw = mcp_raw.replace("~/", home_fwd + "/")
            mcp_file.write_text(mcp_raw)
        else:
            mcp_config = None
            mcp_file.write_text(json.dumps({"mcpServers": {}}, indent=2))

        _write_cli_hooks(self._engine_dir, self.config.get("disallowed_tools"),
                         int(self.config.get("start_context_parts") or 0), scripts_dir=self._claude_dir)

        # OAuth file delivery (mirrors CLISession): the TUI authenticates from
        # .credentials.json in CLAUDE_CONFIG_DIR; rewritten on rotation by
        # ``credentials_update`` pushes.
        creds = self.config.get("credentials_json")
        if creds:
            session_homes.write_sign_in(self._engine_dir, ".credentials.json", creds)

        # Build env (mirrors CLISession): curate ambient → overlay platform env →
        # the SSH agent socket and the exit's door (OTO_SSH_KEY_DIR, SSH_AUTH_SOCK) →
        # CLAUDE_CONFIG_DIR + disable-auto-memory → translate sandbox-virtual paths.
        # The shared base already set OTO_SESSION_ID + UTF-8 + TERM.
        env = self._base_env()
        env.update(await self._session_agent_env())
        env["CLAUDE_CONFIG_DIR"] = str(self._engine_dir)
        # The interactive flag the local Claude TUI spawn sets (satellite
        # 0.5.121 — parity with cli/session.py): the PostToolUse forwarder and
        # the SubagentStop tracker stand down (the transcript the satellite
        # forwards feeds the proxy's rows and registry), the permission gate
        # reports the CLI's live mode. Without it the forwarder posted every
        # full tool result to a session with no pump.
        env["OTO_INTERACTIVE"] = "1"
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["DISABLE_AUTOUPDATER"] = "1"  # version pin/freeze (see VERSIONS.md)
        # Platform is the only skill source (mirrors proxy env_builder).
        env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"
        # Nested subagents (depth-3 default since CC 2.1.219) are invisible to
        # the platform — freeze at depth 1. Mirrors proxy env_builder.
        env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] = "1"
        # CC 2.1.233 removed Todo/Task tools on newer models; the dashboard
        # todo checklist depends on their frames. Mirrors proxy env_builder.
        env["CLAUDE_CODE_ENABLE_TODO_TOOLS"] = "1"
        # The advisor tool is a model call the platform can neither meter nor
        # show. Mirrors proxy env_builder.
        env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] = "1"
        # Tools that really start connect together, not three at a time.
        # Mirrors proxy env_builder.
        env["MCP_SERVER_CONNECTION_BATCH_SIZE"] = str(min(16, max(3, 2 * (os.cpu_count() or 1))))
        env.pop("CLAUDECODE", None)
        env = session_tools_env(self._translate_env(env), self._claude_dir)

        # Seed the TUI past the first-run wizard (theme/login/trust). Without this
        # the remote terminal hangs at the picker. cwd key = the satellite-
        # absolute cwd (what Claude sees). Theme defaults dark.
        _seed_interactive_cli_config(
            self._engine_dir, str(self._cwd), self.config.get("interactive_theme", "dark"),
        )

        # --- build the INTERACTIVE command -----------------------------------
        from ..host.cli_versions import resolve_spawn_bin_async
        claude_bin = await resolve_spawn_bin_async(
            "claude", self.sat_config.bin_hint("claude"), for_pty=True,
        )
        cmd = [claude_bin]  # NO "-p" — render the native TUI
        model = self.config.get("model", "")
        if model:
            cmd += ["--model", model]
        effort = self.config.get("effort", "")
        if effort:
            cmd += ["--effort", effort]
        cmd += ["--max-thinking-tokens", str(self.config.get("max_thinking_tokens", 100000))]
        # Interactive ALWAYS uses a real --permission-mode (never skip — that
        # shows the bypass screen a no-viewer can't accept). The PreToolUse hook
        # still enforces the floor + auto-allows in auto modes.
        cmd += ["--permission-mode", terminal_permission_mode(self.config)]
        if mcp_config is not None:
            cmd += ["--mcp-config", str(mcp_file)]
        _is_resume = bool(
            self.config.get("resume") and self.config.get("session_id_for_resume")
        )
        # The prompt file on fresh AND --resume starts, as the local TUI (which
        # reuses the -p argv) always did: transcripts persist messages only.
        # Claude Code ≥ 2.1.267 would otherwise re-send the prompt it recorded
        # on the conversation's first request on every resume — recording off;
        # and only the platform's settings.json is read (a plugin enabled from
        # this terminal at the local scope lands in settings.local.json). Both
        # flags exist on the previous pin 2.1.281 too.
        cmd += ["--append-system-prompt-file", str(prompt_file)]
        cmd += ["--system-prompt-snapshot", "off", "--setting-sources", "user"]
        cmd += key_helper_args(env)
        # NO --output-format/--input-format/--verbose: that's the -p pump's wire.
        if _is_resume:
            cmd += ["--resume", self.config["session_id_for_resume"]]
        else:
            cmd += ["--session-id", self.session_id]

        # Transcript forwarding: Claude writes/continues <id>.jsonl — the
        # resume id on --resume, else our session id. On resume, skip past the
        # existing (already-persisted) history.
        if _is_resume:
            self._transcript_target_id = self.config["session_id_for_resume"]
            self._transcript_skip_existing = True

        from ..sessions import sandbox_host
        await self._wire_and_spawn(sandbox_host.paired_command(cmd), env, str(self._cwd))

    def _on_close(self) -> None:
        if self._compute:
            return  # the prompt file is the platform's (swept there as a local terminal's)
        remove_prompt_file(self._claude_dir, self.session_id)
        session_homes.close_home(self._claude_dir, self.session_id, self.config.get("session_home"))

    # --- transcript forwarding (Claude: <id>.jsonl by name) ------------------
    def _locate_transcript_file(self) -> str | None:
        """Find ``<claude_dir>/projects/<hash>/<target_id>.jsonl``, as a path
        beneath the agent's folder. The project hash derives from the cwd, so
        search every project dir. Created by Claude shortly after spawn
        (resume: already present). The folder is listed and each candidate's
        status read beneath the agent's folder (``host/safe_fs``): a real
        project folder and a regular file only; a ``projects`` folder past
        ``_PROJECTS_MAX_ENTRIES`` entries finds nothing."""
        rel = self.config.get("claude_dir_relative")
        if self._claude_dir is None or not rel or not isinstance(rel, str):
            return None
        projects = f"{rel}/projects"
        name = f"{self._transcript_target_id}.jsonl"
        try:
            with safe_fs.open_root(self.agent_dir) as root:
                listing = safe_fs.list_beneath(root, projects, max_entries=_PROJECTS_MAX_ENTRIES)
                for proj in listing.dirs:
                    cand = f"{projects}/{proj}/{name}"
                    try:
                        st = safe_fs.lstat_beneath(root, cand)
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):
                        return cand
                if listing.links:
                    self._warn_once("link", "PTY %s: %s/%s is a link, which the terminal does not follow: "
                                    "a transcript there does not reach the chat",
                                    self.session_id[:8], projects, listing.links[0])
        except safe_fs.SymlinkRefused:
            self._warn_once("link", "PTY %s: %s is reached through a link, which the terminal does not "
                            "follow: its transcripts do not reach the chat", self.session_id[:8], projects)
        except OSError:
            logger.debug("PTY %s: transcript search failed", self.session_id[:8], exc_info=True)
        return None
