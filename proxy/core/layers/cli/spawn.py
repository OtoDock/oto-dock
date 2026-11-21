"""The command line and the environment of a Claude Code session.

``ClaudeSpawn`` assembles what a session's process starts with: its argv,
its environment, its system prompt file and its own copy of the MCP config.
``CLIExecutionLayer.prepare_spawn`` builds one for the plan a machine
starts the session from.
"""

import contextlib
import fnmatch
import hashlib
import json
import logging
import os
import stat
import time
from pathlib import Path

import config
from core.layers.cli.helpers import _build_client_context, _build_env
from core.permission_modes import AUTO_REVIEW_MODE, PLAN_MODE, cli_mode, terminal_mode

logger = logging.getLogger("otodock")

# A session's system prompt file is ``system-prompt-<h>.json``, ``<h>`` the
# first 12 hex characters of sha256 of its session id. Every session of one
# person on one agent starts from the same folder, so the name is per
# session. It has the shape of a per-session MCP config copy because the
# never-sync rule every released machine carries matches that shape
# (``_CLAUDE_HOST_LOCAL_RE``, core/remote/file_sync.py and the satellite's
# twin): the file stays on this host with no rule to change on either side.
# The CLI reads the file as text whatever its suffix.
_PROMPT_STEM = "system-prompt"
_PROMPT_GLOB = f"{_PROMPT_STEM}-{'[0-9a-f]' * 12}.json"
# A folder upgraded from an earlier release holds one file all its sessions
# shared. A start removes it.
_SHARED_PROMPT_FILENAME = "system-prompt.md"

# The Claude Code TUI reads a key in its environment as a custom key a person
# must approve on screen, and says "Not logged in" until one does. A terminal
# on an API key signs in through a key helper given on its own command line
# instead: the helper prints the key from a variable of the terminal's
# environment, so nothing of the key is written to a file. The satellite's
# terminal does the same (``satellite/terminal/pty_session.py``).
KEY_HELPER_ENV = "OTO_CLAUDE_API_KEY"


def key_helper_args(env: dict[str, str]) -> list[str]:
    """The flags that sign a terminal in on the API key ``env`` carries,
    the key moved to ``KEY_HELPER_ENV`` in ``env`` (``[]``: no key)."""
    key = env.pop("ANTHROPIC_API_KEY", "")
    if not key:
        return []
    env[KEY_HELPER_ENV] = key
    return ["--settings", json.dumps({"apiKeyHelper": f'printf %s "${KEY_HELPER_ENV}"'})]


def _prompt_filename(session_id: str) -> str:
    """The session's prompt file name. An empty session id hashes too, so
    it keeps one stable name of the same shape."""
    return f"{_PROMPT_STEM}-{hashlib.sha256(session_id.encode()).hexdigest()[:12]}.json"


#: The session's tool config copy is read whole, at most this much.
_TOOL_CONFIG_MAX_BYTES = 4 * 1024 * 1024


def _prompt_root(folder: Path) -> tuple[str | Path, str]:
    """``(root, rel)`` a session's prompt folder is reached beneath: the
    agent's real folder for an engine folder (``core.agent_folder``), the
    sessions folder for the fallback with no agent; ``EscapeRefused`` for
    any other. The one place the root is chosen."""
    from core.agent_folder import beneath_agent
    from services.infra import safe_fs
    try:
        return config.SESSIONS_DIR, safe_fs.rel_under(folder, config.SESSIONS_DIR)
    except safe_fs.EscapeRefused:
        return beneath_agent(folder)


def remove_prompt_file(path: str | Path) -> None:
    """Remove one session's prompt file beneath its root: a link at the name
    goes and its target stays, a link on the way refuses. A file already
    gone is no error. Blocking: off the event loop."""
    from services.infra import safe_fs
    p = Path(path)
    root, rel = _prompt_root(p.parent)
    safe_fs.unlink_beneath(root, f"{rel}/{p.name}" if rel else p.name, missing_ok=True)


def _write_prompt_file(host_dir: str | Path, content: str, session_id: str) -> Path:
    """Write a session's system prompt to its own file inside the given
    directory.

    For sandboxed sessions this is the host .claude/ dir; for non-sandboxed
    sessions it's the CLAUDE_CONFIG_DIR or a fallback dir.  Returns the
    host path (caller must translate to sandbox path if needed). The shared
    file is removed, and the prompt files older than a day are swept: a
    close removes its session's file, and the terminal's builder writes one
    and owns no close.

    Every step runs beneath the root that holds the directory (the agent's
    real folder for an engine folder, the sessions folder for the fallback)
    with no link at any part below it: the session writes this folder and
    knows its own file's name, so a link at the name is replaced, never
    written through, and a folder outside both roots refuses the start.
    """
    from services.infra import safe_fs
    d = Path(host_dir)
    root, rel = _prompt_root(d)

    def _in(name: str) -> str:
        return f"{rel}/{name}" if rel else name

    if rel:
        safe_fs.mkdirs_beneath(root, rel)
    cutoff = time.time() - 24 * 3600
    stale: list[str] = []
    for step in safe_fs.walk_beneath(root, rel):
        for name in step.files + step.symlinks:
            if name == _SHARED_PROMPT_FILENAME:
                stale.append(name)
            elif fnmatch.fnmatchcase(name, _PROMPT_GLOB):
                try:
                    st = os.stat(name, dir_fd=step.dirfd, follow_symlinks=False)
                except OSError:
                    continue
                if (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)) and st.st_mtime < cutoff:
                    stale.append(name)
        break
    for name in stale:
        with contextlib.suppress(OSError):
            safe_fs.unlink_beneath(root, _in(name), missing_ok=True)
    name = _prompt_filename(session_id)
    safe_fs.atomic_write_beneath(root, _in(name), content.encode("utf-8"), fsync=False)
    return d / name


class ClaudeSpawn:
    """What a Claude Code session's process starts with: the argv, the
    environment, the prompt file and the MCP config copy of one session."""

    def __init__(
        self,
        session_id: str,
        agent_prompt: str | None,
        mcp_config_path: Path | None,
        permission_mode: str = "auto",
        client_type: str = "",
        resume: bool = False,
        use_native_permissions: bool = False,
        model: str = "",
        effort: str = "",
        extra_env: dict[str, str] | None = None,
        credential_env: dict[str, str] | None = None,
        sandbox_builder=None,      # SandboxBuilder | None
        agent_name: str = "",      # needed for non-sandboxed per-agent CWD
        interactive: bool = False,  # run the native TUI (no -p)
        disallowed_tools: list[str] | None = None,  # --disallowedTools (external sessions)
    ):
        self.session_id = session_id
        # Built-in tools this session must never see (external routes: no
        # shell — the layer's session_denied_tools, from its descriptor).
        self.disallowed_tools = list(disallowed_tools or [])
        self.agent_prompt = agent_prompt
        self.mcp_config_path = mcp_config_path
        self.permission_mode = permission_mode
        self.client_type = client_type
        self.resume = resume
        self.use_native_permissions = use_native_permissions
        # Caller (via config_builder) always passes the resolved model. If we
        # somehow arrive here with empty, keep the empty string — starting the
        # CLI will fail with a clearer error than AttributeError on missing env.
        self.model = model or ""
        self.effort = effort or config.DEFAULT_EFFORT_LEVEL
        self.extra_env = extra_env
        self.credential_env = credential_env
        self.sandbox_builder = sandbox_builder
        self.agent_name = agent_name
        self.interactive = interactive
        self._prompt_file: Path | None = None  # the session's own system prompt file

    @staticmethod
    def _strip_session_id(url: str) -> str:
        """Remove all session_id= params from a URL, returning the clean base."""
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params.pop("session_id", None)
        clean_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=clean_query))

    def _swap_session_jwt(self, conf: dict) -> bool:
        """Swap the session-JWT sentinel bearer (set at config-build time for
        Docker MCPs declaring ``server.proxy_callbacks``, e.g. file-tools) for a
        real, session-scoped JWT now that the session_id is known. The MCP
        forwards this bearer on its proxy callbacks (resolve-path,
        document-preview). Returns True if a swap happened. No-op for entries
        with a real vendor bearer or no headers.
        """
        from auth.session_token import swap_session_jwt_bearer
        headers = conf.get("headers")
        if not isinstance(headers, dict):
            return False
        new = swap_session_jwt_bearer(
            headers.get("Authorization", ""), self.session_id, self.agent_name,
        )
        if new is None:
            return False
        headers["Authorization"] = new
        return True

    def _inject_session_id_into_sse_inplace(self, mcp_config_path: Path) -> None:
        """Set ?session_id= on URL-based MCP URLs, modifying the file in place.

        Used for sandboxed sessions where the MCP config is inside .claude/.
        Replaces any previous session_id (persistent file accumulates them).
        The copy is read and written whole beneath its root
        (:func:`_prompt_root`: the agent's real folder for an engine folder)
        with no link followed, since the session writes that folder: a copy
        that is not there, or not a plain file, is left as it is.
        """
        from services.infra import safe_fs
        path = Path(mcp_config_path)
        try:
            root, rel = _prompt_root(path.parent)
            rel = f"{rel}/{path.name}" if rel else path.name
            data = json.loads(safe_fs.read_bytes_beneath(root, rel, max_size=_TOOL_CONFIG_MAX_BYTES))
        except (OSError, ValueError):
            return

        servers = data.get("mcpServers", {})
        modified = False
        for _name, conf in servers.items():
            if conf.get("type") in ("sse", "streamable-http", "http") and "url" in conf:
                base = self._strip_session_id(conf["url"])
                sep = "&" if "?" in base else "?"
                conf["url"] = f"{base}{sep}session_id={self.session_id}"
                modified = True
            if self._swap_session_jwt(conf):
                modified = True

        if modified:
            safe_fs.atomic_write_beneath(root, rel, json.dumps(data, indent=2).encode("utf-8"),
                                         mode=0o644, fsync=False)

    def _build_persistent_cmd(self) -> list[str]:
        """Build the CLI command for a persistent process."""
        # xhigh is a distinct reasoning level (between high and max) supported
        # only on Opus 4.7+ and the OpenAI gpt-5 family. For models without
        # support, silently fall back to "max" — the API would reject xhigh
        # otherwise and the user expects their "most effort" choice to work.
        # Every other platform level (low/medium/high/max) passes through
        # unchanged to Anthropic.
        wire_effort = self.effort
        if wire_effort == "ultra":
            # Platform "ultra" is Codex-only (the Sols, Terra and the Astras:
            # multi-agent orchestration). The `claude` CLI's --effort rejects it — clamp
            # to the ceiling. (Claude's own orchestration mode, "ultracode",
            # is a session setting, not an effort level — see configuration/models.py.)
            wire_effort = "max"
        if wire_effort == "xhigh" and not config.get_model_supports_xhigh(self.model):
            wire_effort = "max"
            logger.info(
                "Session %s: xhigh not supported on "
                "model=%s, falling back to max", self.session_id[:8], self.model
            )
        cmd = [config.CLAUDE_BIN]
        if not self.interactive:
            # Headless `-p` stream-json protocol. Interactive mode runs the
            # native TUI instead: no -p, no stream-json
            # framing, no partial-message events — the PTY carries the rendered
            # terminal. Every other flag below is shared (the CLI's arg parser is
            # the same in both modes).
            cmd += [
                "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--include-partial-messages",
            ]
        cmd += [
            "--model", self.model,
            "--effort", wire_effort,
            "--max-thinking-tokens", str(config.MAX_THINKING_TOKENS),
        ]

        if self.resume:
            cmd.extend(["--resume", self.session_id])
        else:
            cmd.extend(["--session-id", self.session_id])
            # A new conversation under this id holds none of the riding part.
            from core.session import start_context
            start_context.reset(self.session_id)

        if self.use_native_permissions:
            cmd.extend(["--permission-mode", cli_mode(self.permission_mode)])
        elif self.interactive:
            # Interactive TUI — chat AND autonomous task — uses a real
            # --permission-mode, NEVER --dangerously-skip-permissions. Skip shows
            # the one-time "Bypass Permissions mode" ACCEPTANCE SCREEN at TUI
            # launch (verified live: a no-viewer task can't accept it →
            # the cold-prompt flush is misdirected → the CLI exits with the run
            # stuck). The PreToolUse hook is the floor on every surface: a CHAT
            # gets the hook's defer→native prompt (or its classifier, in auto)
            # for the residual; a TASK runs the unattended mode → the gate
            # returns allow/deny (never defer) so Claude never prompts, while
            # the hard-deny floor still blocks. Auto and Plan start in the CLI's
            # own mode; every other mode in `default`, the hook doing the real
            # mode-based gating (terminal_mode).
            cmd.extend(["--permission-mode", terminal_mode(self.permission_mode)])
        elif self.permission_mode == PLAN_MODE:
            # Claude Code 2.1.299+ no longer runs a call the PreToolUse hook
            # allowed when it does not read the call as read-only in plan
            # mode: it asks, and a headless spawn with no prompt tool reads
            # that ask as a refusal (an open-tier MCP call, a read-tier
            # command). The prompt tool carries the ask here, and
            # core/session/engine_prompts answers it from the gate's record
            # of what it allowed in Plan. Bypass beside plan mode cannot
            # replace it: the CLI honours that pair in a terminal only. The
            # tool's presence also exposes ExitPlanMode and AskUserQuestion,
            # which a Plan spawn without it never offers.
            cmd.extend(["--permission-mode", "plan", "--permission-prompt-tool", "stdio"])
        else:
            # Headless -p: the PreToolUse hook gates. In Auto the CLI runs its
            # own auto mode (a call the hook leaves undecided goes to its
            # classifier); every other mode bypasses the CLI's own checks (no
            # TUI, so the bypass-mode warning never renders) and the hook
            # decides everything.
            if self.permission_mode == AUTO_REVIEW_MODE:
                cmd.extend(["--permission-mode", cli_mode(AUTO_REVIEW_MODE)])
            else:
                cmd.append("--dangerously-skip-permissions")
            # The prompt-tool's PRESENCE is what makes the CLI expose
            # AskUserQuestion to a headless session (absent from every -p
            # roster since at least 2.1.193 — no flag/env/model re-adds
            # it). The PreToolUse hook denies-and-surfaces the question
            # before the tool executes; the CLI still asks over this
            # channel for some calls (a heredoc writing a file, a wide
            # ``rm -rf`` under bypass; the request after its reviewer's
            # decline limit in auto), which core/session/engine_prompts
            # answers like a card or refuses.
            cmd.extend(["--permission-prompt-tool", "stdio"])

        if self.disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(self.disallowed_tools)])

        # Claude Code ≥ 2.1.267 records the system prompt on a conversation's
        # first request and re-sends the record on every later request AND on
        # --resume; the platform re-sends the prompt file on every resume ON
        # PURPOSE (fresh persona, memory topics, skills, the client context —
        # see the --append-system-prompt-file note below), so the recording is
        # off. Only the platform's settings.json is read: a plugin a person
        # enables from a terminal at the local scope lands in settings.local.json,
        # which the platform never rewrites. Both flags exist on 2.1.281.
        cmd.extend(["--system-prompt-snapshot", "off", "--setting-sources", "user"])

        if self.mcp_config_path:
            if self.sandbox_builder:
                # Sandbox: mcp_config_path is the sandbox-internal path
                # (e.g. /users/alice/.claude/personal-assistant-abc.json). The
                # actual file is on the host in the .claude/ dir — inject
                # session IDs there using the real filename (not hardcoded).
                mcp_filename = Path(str(self.mcp_config_path)).name
                host_mcp_config = self.sandbox_builder.cfg.host_claude_dir / mcp_filename
                self._inject_session_id_into_sse_inplace(host_mcp_config)
                cmd.extend(["--mcp-config", str(self.mcp_config_path)])

        # System prompt: injected on fresh AND --resume starts. The CLI
        # rebuilds its system prompt from each invocation's flags — session
        # transcripts persist messages only — so skipping this on resume
        # would restart the session with the stock SDK prompt (no agent
        # identity). Nothing duplicates: the transcript carries no prompt.
        client_context = _build_client_context(self.mcp_config_path, self.client_type)
        parts = []
        if self.agent_prompt:
            parts.append(self.agent_prompt)
        if client_context:
            parts.append(client_context)
        if parts:
            prompt_text = "\n\n".join(parts)
            # Write to a file to avoid Linux MAX_ARG_STRLEN limit
            # (128KB per argument). Large user-context docs + MCP skills
            # can exceed this when passed inline.
            #
            # Write into the .claude/ dir so it's accessible inside the
            # sandbox. The CLI arg must use the sandbox-internal path.
            if self.sandbox_builder:
                host_dir = self.sandbox_builder.cfg.host_claude_dir
                sandbox_claude_dir = self.sandbox_builder.get_env_overrides().get(
                    "CLAUDE_CONFIG_DIR", "/workspace/.claude"
                )
                self._prompt_file = _write_prompt_file(host_dir, prompt_text, self.session_id)
                cmd.extend(["--append-system-prompt-file",
                            f"{sandbox_claude_dir}/{self._prompt_file.name}"])
            elif self.extra_env and "CLAUDE_CONFIG_DIR" in self.extra_env:
                host_dir = Path(self.extra_env["CLAUDE_CONFIG_DIR"])
                self._prompt_file = _write_prompt_file(host_dir, prompt_text, self.session_id)
                cmd.extend(["--append-system-prompt-file", str(self._prompt_file)])
            else:
                # Fallback: write next to sessions dir
                fallback_dir = config.SESSIONS_DIR / "prompt-files"
                fallback_dir.mkdir(exist_ok=True)
                self._prompt_file = _write_prompt_file(
                    fallback_dir, prompt_text, self.session_id)
                cmd.extend(["--append-system-prompt-file", str(self._prompt_file)])

        return cmd

    @property
    def prompt_file(self) -> Path | None:
        """The session's own system prompt file on the host, once
        ``build_spawn_command`` wrote it."""
        return self._prompt_file

    def build_spawn_command(self) -> tuple[list[str], dict[str, str], str | None]:
        """Assemble ``(argv, env, cwd)`` for spawning the CLI.

        One assembly for a headless session and a terminal: the native TUI
        runs from the same argv and env as ``-p``, so there is no drift.
        Writes the system-prompt file as a side effect (both need it). A
        terminal also gets ``TERM``: the sandbox env overrides don't set it
        and a TUI needs it. The argv carries no sandbox prefix: the machine
        that starts the session renders the sandbox itself, and the env
        carries the sandbox's own overrides.
        """
        cmd = self._build_persistent_cmd()

        # Username + role come from the sandbox config (same source the
        # CWD/mounts use). Forwarded to env_builder so the OTO_* standard
        # env vars resolve correctly (per-MCP path_env values are pre-baked
        # in self.credential_env by config_builder; see path_roles.py).
        _sb_username = (
            self.sandbox_builder.cfg.username
            if self.sandbox_builder else ""
        )
        _sb_role = (
            self.sandbox_builder.cfg.role
            if self.sandbox_builder else ""
        )
        proc_env = _build_env(self.session_id, credential_env=self.credential_env,
                              agent_name=self.agent_name, username=_sb_username,
                              user_role=_sb_role)
        if self.extra_env:
            proc_env.update(self.extra_env)
        if self.interactive:
            cmd += key_helper_args(proc_env)

        # Sandbox: its env overrides, and no CWD (the sandbox sets its own).
        if self.sandbox_builder:
            proc_env.update(self.sandbox_builder.get_env_overrides())
            cwd = None
        elif self.extra_env and "CLAUDE_CONFIG_DIR" in self.extra_env:
            # Non-sandboxed with per-user/scope .claude/ dir:
            # CWD is the parent of .claude/ (users/{username}/ or workspace/)
            cwd = str(Path(self.extra_env["CLAUDE_CONFIG_DIR"]).parent)
        else:
            # Legacy fallback: per-agent CWD
            if self.agent_name:
                agent_dir = config.get_agent_dir(self.agent_name)
                cwd = str(agent_dir) if agent_dir.exists() else str(config.AGENTS_DIR)
            else:
                cwd = str(config.AGENTS_DIR)

        if self.interactive:
            # A TUI with no TERM renders garbage; the sandbox env doesn't set it.
            proc_env.setdefault("TERM", "xterm-256color")
            # The viewer is xterm.js (full 24-bit support), but without this
            # hint the CLIs downgrade to 256-color SGR — muddier theme colors
            # AND the dashboard's truecolor diff brand-tint (ptyBrandColors)
            # never matches.
            proc_env.setdefault("COLORTERM", "truecolor")
            # Tell the hook scripts they're under the interactive TUI. The
            # PostToolUse/SubagentStop/Stop hooks are redundant here (the TUI
            # renders results; we feed the subagent registry + persistence from
            # the transcript tailer) and were surfacing "hook error" noise in the
            # TUI on some tool results — they no-op early when this is set. The
            # PreToolUse permission gate is NOT gated (still enforces baseline +
            # path rules + the dashboard prompt).
            proc_env["OTO_INTERACTIVE"] = "1"

        # Last, over the final session token: the shell's command-line token.
        from auth.session_token import attach_cli_token
        attach_cli_token(proc_env)
        return cmd, proc_env, cwd
