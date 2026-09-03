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

import json
import logging
from pathlib import Path

from ..sessions.cli_session import _write_cli_hooks, _write_hook_scripts
from ..config import otodock_dir
from .pty_session_base import BasePtySession

logger = logging.getLogger("satellite")

# Claude Code's valid --permission-mode values. Anything else from the proxy
# (e.g. the platform's "dontAsk"/"auto") maps to "default" — the PreToolUse hook
# still enforces the floor + auto-allows, so the TUI never blocks.
_CLAUDE_PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}


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
    except Exception:
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
    # session. Same key the picker's "Don't ask me again" writes; the proxy
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
    except Exception:
        logger.warning("pty seed: could not write %s", cfg, exc_info=True)


class PtySession(BasePtySession):
    """A Claude TUI subprocess on a PTY, driven by the proxy over the WS."""

    execution_path = "claude-code-cli"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._claude_dir: Path | None = None
        # The session id whose JSONL holds this run's turns: the resume id on
        # --resume (Claude appends to the original <id>.jsonl), else our own id.
        self._transcript_target_id = self.session_id

    async def start(self) -> None:
        """Write config files + spawn the interactive Claude TUI under a PTY."""
        # --- session files + env (mirrors CLISession.start setup) -------------
        # cwd may be an arbitrary absolute folder (otodock-CLI); the config
        # dir stays agent_dir-rooted via claude_dir_relative regardless.
        self._cwd = self._resolve_work_cwd()
        self._claude_dir = self.agent_dir / self.config["claude_dir_relative"]
        self._claude_dir.mkdir(parents=True, exist_ok=True)

        # Wipe Claude Code's built-in auto-memory (the otodock memory-mcp is the
        # single source of truth); leave session JSONLs for --resume.
        projects_dir = self._claude_dir / "projects"
        if projects_dir.exists():
            import shutil as _shutil
            for proj in projects_dir.iterdir():
                if proj.is_dir() and (proj / "memory").exists():
                    _shutil.rmtree(proj / "memory", ignore_errors=True)

        _write_hook_scripts(self._claude_dir, self.config.get("hook_scripts", {}))

        prompt_file = self._claude_dir / "system-prompt.md"
        prompt_file.write_text(self.config.get("system_prompt", ""))

        mcp_config = self.config.get("mcp_config")
        mcp_file = self._claude_dir / "mcp-config.json"
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

        _write_cli_hooks(self._claude_dir, self.config.get("disallowed_tools"))

        # OAuth file delivery (mirrors CLISession): the TUI authenticates from
        # .credentials.json in CLAUDE_CONFIG_DIR; rewritten on rotation by
        # ``credentials_update`` pushes.
        creds = self.config.get("credentials_json")
        if creds:
            creds_path = self._claude_dir / ".credentials.json"
            creds_path.write_text(json.dumps(creds))
            creds_path.chmod(0o600)

        # Build env (mirrors CLISession): curate ambient → overlay platform env →
        # session-file broker (OTO_SSH_KEY_DIR + credentials_dir token files) →
        # CLAUDE_CONFIG_DIR + disable-auto-memory → translate sandbox-virtual paths.
        # The shared base already set OTO_SESSION_ID + UTF-8 + TERM.
        env = self._base_env()
        env.update(await self._materialize_session_files())
        env["CLAUDE_CONFIG_DIR"] = str(self._claude_dir)
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
        env.pop("CLAUDECODE", None)
        env = self._translate_env(env)

        # Seed the TUI past the first-run wizard (theme/login/trust). Without this
        # the remote terminal hangs at the picker. cwd key = the satellite-
        # absolute cwd (what Claude sees). Theme defaults dark.
        _seed_interactive_cli_config(
            self._claude_dir, str(self._cwd), self.config.get("interactive_theme", "dark"),
        )

        # --- build the INTERACTIVE command -----------------------------------
        from ..host.cli_versions import resolve_spawn_bin_async
        claude_bin = await resolve_spawn_bin_async(
            "claude", self.sat_config.claude_bin, for_pty=True,
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
        _pm = self.config.get("permission_mode", "default")
        if _pm not in _CLAUDE_PERMISSION_MODES:
            _pm = "default"
        cmd += ["--permission-mode", _pm]
        if mcp_config is not None:
            cmd += ["--mcp-config", str(mcp_file)]
        _is_resume = bool(
            self.config.get("resume") and self.config.get("session_id_for_resume")
        )
        if not _is_resume:
            cmd += ["--append-system-prompt-file", str(prompt_file)]
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

        await self._wire_and_spawn(cmd, env, str(self._cwd))

    # --- transcript forwarding (Claude: <id>.jsonl by name) ------------------
    def _locate_transcript_file(self) -> "Path | None":
        """Find ``<claude_dir>/projects/<hash>/<target_id>.jsonl``. The project
        hash derives from the cwd, so search every project dir. Created by Claude
        shortly after spawn (resume: already present)."""
        if self._claude_dir is None:
            return None
        projects = self._claude_dir / "projects"
        if not projects.is_dir():
            return None
        try:
            for proj in projects.iterdir():
                cand = proj / f"{self._transcript_target_id}.jsonl"
                if cand.is_file():
                    return cand
        except OSError:
            return None
        return None
