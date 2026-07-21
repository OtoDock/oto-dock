"""PersistentSession class and lifecycle management.

Wraps long-lived Claude processes with warm MCP servers. Sessions persist
across turns via stream-json stdin/stdout protocol.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import AsyncIterator

import config
from core.session.session_state import (
    _active_processes, _aborted_sessions, session_exists,
    _record_session_use, resolve_session_permissions,
    get_hook_activity, get_session_user_tz, reset_subagent_registry,
    resolve_bg_command_frame, clear_session_liveness,
)
from core.events.bg_command_state import reset_bg_command_registry
from core.layers.cli.helpers import (
    ClaudeStreamChunk, _build_env, _kill_process,
    _build_client_context,
)
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.layers.cli.settle import (
    FOREIGN_SKIP_SILENCE_S,
    ForeignSkipGate,
    SettleController,
    chunk_is_content,
    is_foreign_result,
)
import contextlib

logger = logging.getLogger("claude-proxy")

_PROMPT_FILENAME = "system-prompt.md"

# claude-code#63943: a graceful interrupt mid-thinking can persist an unsigned
# thinking block in the CLI's session history; every later API call then 400s
# on the block's missing/invalid signature. Matched against the error result
# of the first turn(s) after an interrupt to trigger the killpg+resume repair.
_THINKING_SIG_RE = re.compile(
    r"thinking.{0,120}signature|signature.{0,120}thinking", re.I | re.S,
)

# Strong refs for the wedge-repair kill tasks — a bare create_task is
# GC-collectable mid-flight.
_repair_tasks: set[asyncio.Task] = set()


def _write_prompt_file(host_dir: str | Path, content: str) -> Path:
    """Write system prompt to a file inside the given directory.

    For sandboxed sessions this is the host .claude/ dir; for non-sandboxed
    sessions it's the CLAUDE_CONFIG_DIR or a fallback dir.  Returns the
    host path (caller must translate to sandbox path if needed).
    """
    d = Path(host_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / _PROMPT_FILENAME
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Persistent Sessions — long-lived Claude processes with warm MCP servers
# ---------------------------------------------------------------------------

class PersistentSession:
    """Wraps a long-lived Claude process using --input-format stream-json.

    MCP servers start once and persist across turns. Messages are written
    as NDJSON to stdin; responses are read from stdout in the same
    stream-json format as one-shot mode.
    """

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
    ):
        self.session_id = session_id
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
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()  # serializes concurrent requests
        self.last_activity = time.monotonic()
        self._started = False
        self._closed = False
        self._init_done = False  # True after MCP init event received
        self._prompt_file: Path | None = None  # temp file for system prompt
        # Graceful-interrupt bookkeeping: `_turn_active` spans a send_message
        # drive (interrupt_turn only fires into a live turn; the layer's
        # watchdog polls it for "the turn closed"); `_turn_seq` distinguishes
        # the interrupted turn from a successor so a slow watchdog can't kill
        # a fresh turn; `_post_interrupt_watch` arms the #63943 wedge detector.
        self._turn_active = False
        self._turn_seq = 0
        self._post_interrupt_watch = False

    @staticmethod
    def _strip_session_id(url: str) -> str:
        """Remove all session_id= params from a URL, returning the clean base."""
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
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
        """
        try:
            data = json.loads(mcp_config_path.read_text())
        except Exception:
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
            mcp_config_path.write_text(json.dumps(data, indent=2))

    def _inject_session_id_into_sse(self, mcp_config_path: Path) -> Path:
        """Set ?session_id= on URL-based MCP URLs for per-session event routing.

        Returns the original path if no URL servers found, or a temp path
        with the modified config.
        """
        try:
            data = json.loads(mcp_config_path.read_text())
        except Exception:
            return mcp_config_path

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

        if not modified:
            return mcp_config_path

        # Write to a per-session temp file
        out_dir = config.SESSIONS_DIR / "sse-mcp-configs"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"{self.session_id[:12]}.json"
        out.write_text(json.dumps(data, indent=2))
        return out

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
            # Platform "ultra" is Codex-only (gpt-5.6 Sol/Terra multi-agent
            # orchestration). The `claude` CLI's --effort rejects it — clamp
            # to the ceiling. (Claude's own orchestration mode, "ultracode",
            # is a session setting, not an effort level — see config.py.)
            wire_effort = "max"
        if wire_effort == "xhigh" and not config.get_model_supports_xhigh(self.model):
            wire_effort = "max"
            logger.info(
                f"Session {self.session_id[:8]}: xhigh not supported on "
                f"model={self.model}, falling back to max"
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

        if self.use_native_permissions:
            cmd.extend(["--permission-mode", self.permission_mode])
        elif self.interactive:
            # Interactive TUI — chat AND autonomous task — uses a real
            # --permission-mode, NEVER --dangerously-skip-permissions. Skip shows
            # the one-time "Bypass Permissions mode" ACCEPTANCE SCREEN at TUI
            # launch (verified live: a no-viewer task can't accept it →
            # the cold-prompt flush is misdirected → the CLI exits with the run
            # stuck). The PreToolUse hook is the floor on every surface: a CHAT
            # (default/acceptEdits) gets the hook's defer→native prompt for the
            # ask-tier; a TASK runs "auto" mode → the gate returns allow/deny
            # (never defer) so the hook auto-allows and Claude never prompts, while
            # the hard-deny floor still blocks. Plan/acceptEdits pass through;
            # everything else (default/auto/dontAsk) → default; the hook does the
            # real mode-based gating.
            if self.permission_mode in ("plan", "acceptEdits"):
                cmd.extend(["--permission-mode", self.permission_mode])
            else:
                cmd.extend(["--permission-mode", "default"])
        else:
            # Headless -p: bypass permissions, our PreToolUse hook does gating
            # (no TUI, so the bypass-mode warning never renders).
            if self.permission_mode == "plan":
                cmd.extend(["--permission-mode", "plan"])
            else:
                cmd.append("--dangerously-skip-permissions")
                # The prompt-tool's PRESENCE is what makes the CLI expose
                # AskUserQuestion to a headless session (absent from every -p
                # roster since at least 2.1.193 — no flag/env/model re-adds
                # it). Bypass mode never raises permission prompts and the
                # PreToolUse hook denies-and-surfaces the question before the
                # tool executes, so the stdio permission channel stays silent.
                # Deliberately NOT added to the plan/-p or native-permission
                # branches: there a real prompt could route to stdio, which
                # the stream translator does not answer.
                cmd.extend(["--permission-prompt-tool", "stdio"])

        if self.mcp_config_path:
            if self.sandbox_builder:
                # Sandbox: mcp_config_path is the sandbox-internal path
                # (e.g. /users/alice/.claude/personal-assistant-abc.json). The
                # actual file is on the host in the .claude/ dir — inject
                # session IDs there using the real filename (not hardcoded).
                mcp_filename = Path(str(self.mcp_config_path)).name
                host_mcp_config = self.sandbox_builder.cfg.host_claude_dir / mcp_filename
                if host_mcp_config.exists():
                    self._inject_session_id_into_sse_inplace(host_mcp_config)
                cmd.extend(["--mcp-config", str(self.mcp_config_path)])
            elif self.mcp_config_path.exists():
                mcp_path = self._inject_session_id_into_sse(self.mcp_config_path)
                cmd.extend(["--mcp-config", str(mcp_path)])

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
                _write_prompt_file(host_dir, prompt_text)
                self._prompt_file = host_dir / _PROMPT_FILENAME
                cmd.extend(["--append-system-prompt-file",
                            f"{sandbox_claude_dir}/{_PROMPT_FILENAME}"])
            elif self.extra_env and "CLAUDE_CONFIG_DIR" in self.extra_env:
                host_dir = Path(self.extra_env["CLAUDE_CONFIG_DIR"])
                self._prompt_file = _write_prompt_file(host_dir, prompt_text)
                cmd.extend(["--append-system-prompt-file", str(self._prompt_file)])
            else:
                # Fallback: write next to sessions dir
                fallback_dir = config.SESSIONS_DIR / "prompt-files"
                fallback_dir.mkdir(exist_ok=True)
                self._prompt_file = _write_prompt_file(fallback_dir, prompt_text)
                cmd.extend(["--append-system-prompt-file", str(self._prompt_file)])

        return cmd

    def build_spawn_command(self) -> tuple[list[str], dict[str, str], str | None]:
        """Assemble ``(argv, env, cwd)`` for spawning the CLI.

        Shared by the headless :meth:`start` and the interactive PTY launcher
        (the CLI layer's interactive branch), which reuses this to drive the native TUI
        from the exact same argv/env as ``-p`` — only the spawn differs (PTY vs
        pipes), so there is no drift. Writes the system-prompt file as a side
        effect (both modes need it). In interactive mode adds ``TERM`` — the
        sandbox env overrides don't set it and a TUI needs it.
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

        # Sandbox: wrap command with bwrap, override env, let bwrap set CWD
        if self.sandbox_builder:
            cmd = self.sandbox_builder.build_command_prefix(cmd)
            proc_env.update(self.sandbox_builder.get_env_overrides())
            cwd = None  # bwrap handles CWD via --chdir
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

        return cmd, proc_env, cwd

    async def start(self) -> None:
        """Spawn the persistent Claude process.

        MCP servers start loading immediately in the background.
        The init event is deferred until the first message is sent,
        but MCPs are already warm by then.
        """
        if self._started:
            return

        cmd, proc_env, cwd = self.build_spawn_command()
        logger.info(
            f"Starting persistent session {self.session_id} "
            f"(cmd={' '.join(cmd[:6])}...)"
        )

        # 200MB stdout buffer — default 64KB readline silently fails with
        # asyncio.LimitOverrunError on large MCP tool-result JSON lines
        # (e.g. unifi-network's list_devices, file-tools' big payloads).
        # When that happens the stream reader dies mid-turn, the pump
        # ends without emitting `done`, and the UI freezes with a
        # spinning tool-call icon that never resolves. Satellite-side
        # cli_session.py + codex_session.py carry the same bump.
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=proc_env,
            start_new_session=True,
            limit=200 * 1024 * 1024,
        )
        self._started = True
        self.last_activity = time.monotonic()

        # Register for abort support
        _active_processes[self.session_id] = self.proc

        logger.info(
            f"Persistent session {self.session_id} started "
            f"(pid={self.proc.pid}) — MCP loading in background"
        )

    async def _wait_for_init(self, timeout: float = 60.0) -> None:
        """Wait for the system init event (MCP servers loaded) before sending prompts.

        Claude Code emits a system init event on stdout once all MCP servers
        have connected (or failed). We MUST wait for this before sending any
        prompt — otherwise Claude processes the prompt without tools and
        produces empty/useless output.

        Only needed for the FIRST message. Subsequent messages are fine
        because MCPs are already loaded.
        """
        if self._init_done or self.proc is None or self.proc.stdout is None:
            return

        logger.info(f"Persistent session {self.session_id}: waiting for MCP init...")
        try:
            while True:
                raw_line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=timeout,
                )
                if not raw_line:
                    raise RuntimeError("Process exited before init")
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "system" and data.get("subtype") == "init":
                    mcp_servers = data.get("mcp_servers", [])
                    connected = [s["name"] for s in mcp_servers if s.get("status") == "connected"]
                    failed = [s["name"] for s in mcp_servers if s.get("status") == "failed"]
                    logger.info(
                        f"Persistent session {self.session_id}: MCP ready — "
                        f"connected={connected}, failed={failed}"
                    )
                    self._init_done = True
                    return
        except asyncio.TimeoutError:
            logger.warning(
                f"Persistent session {self.session_id}: MCP init timeout after {timeout}s, "
                f"proceeding anyway"
            )
            self._init_done = True

    async def _drain_stale_output(self) -> None:
        """Drain any leftover stdout from a previous interrupted response.

        When a client disconnects mid-stream (e.g. phone barge-in breaks SSE),
        the Claude process continues generating its response. The output stays
        in the pipe buffer. If we don't drain it, the next send_message() would
        read stale data and desync (returning the OLD response as the new one).

        Uses a short initial timeout (50ms) to check for stale data — fast
        path for the common case (clean pipe). If stale data IS found, uses
        a longer timeout (2s) to wait for the 'result' event that signals
        the end of the previous turn.
        """
        if self.proc is None or self.proc.stdout is None:
            return

        drained: Counter[str] = Counter()
        timeout = 0.05  # initial probe — fast exit if pipe is clean
        while True:
            try:
                raw_line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=timeout,
                )
                if not raw_line:
                    break  # EOF
                drained["?"] += 1
                timeout = 2.0  # stale data confirmed — wait for result event
                # Check if we hit the result event (end of previous turn)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    with contextlib.suppress(json.JSONDecodeError):
                        data = json.loads(line)
                        drained["?"] -= 1
                        drained[data.get("type", "?")] += 1
                        # A backgrounded command may have completed while the
                        # session was idle between turns — resolve it (badge clear
                        # + registry) instead of discarding the frame, else its
                        # badge sticks forever and the agent never gets nudged.
                        resolve_bg_command_frame(self.session_id, data)
                        if data.get("type") == "result":
                            break  # Previous response fully drained
            except asyncio.TimeoutError:
                break  # No more stale data — pipe is clean

        if drained:
            # Type census only, never payloads — a swallowed late answer from
            # an orphaned prior turn is otherwise invisible.
            logger.warning(
                f"Persistent session {self.session_id}: drained "
                f"{sum(drained.values())} stale lines from interrupted "
                f"response (types={dict(drained)}, "
                f"results={drained.get('result', 0)})"
            )

    async def drain_bg_commands(self, budget: float = 2.0) -> bool:
        """Between turns, read whatever stdout the IDLE session has buffered and
        resolve any background-command completion frames found (badge clear +
        registry). Returns True if at least one command was resolved.

        Background bash has NO completion hook (unlike subagents' SubagentStop),
        so this active read is the only post-turn completion signal — the
        bg-command monitor polls it. Acquires ``self.lock`` (the SAME lock that
        serializes send_message, via layer.session_lock) with a short timeout: if
        a user turn holds it, back off and let that turn's own translator resolve
        completions. Because it only reads while holding the lock exclusively, it
        never races the turn reader or consumes turn output."""
        if self.proc is None or self.proc.stdout is None:
            return False
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False  # a turn is in flight — retry next poll
        progressed = False
        try:
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                try:
                    raw_line = await asyncio.wait_for(
                        self.proc.stdout.readline(), timeout=0.4,
                    )
                except asyncio.TimeoutError:
                    break  # no more buffered data right now
                if not raw_line:
                    break  # EOF — process exited
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if resolve_bg_command_frame(self.session_id, data):
                    progressed = True
        finally:
            self.lock.release()
        return progressed

    async def send_control_request(self, subtype: str, **kwargs) -> dict:
        """Send a control request to the CLI and wait for its response.

        Used for mid-session changes: set_permission_mode, set_model,
        set_max_thinking_tokens.  Must be called with self.lock held
        (between turns, not during streaming).
        """
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("Session is not running")

        request_id = str(uuid.uuid4())
        msg = json.dumps({
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": subtype, **kwargs},
        })
        self.proc.stdin.write((msg + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        logger.info(
            f"Persistent session {self.session_id}: "
            f"sent control_request subtype={subtype}"
        )

        # Read until we get the matching control_response
        while True:
            raw_line = await asyncio.wait_for(
                self.proc.stdout.readline(), timeout=10.0,
            )
            if not raw_line:
                raise RuntimeError("Process died waiting for control response")
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "control_response":
                resp = data.get("response", {})
                if resp.get("request_id") == request_id:
                    logger.info(
                        f"Persistent session {self.session_id}: "
                        f"control_response for {subtype}: "
                        f"{resp.get('subtype', 'unknown')}"
                    )
                    return resp
            # Skip other events (system init, etc.) while waiting

    async def send_control_response(
        self, request_id: str, approved: bool,
    ) -> None:
        """Answer a native permission prompt (can_use_tool control_request).

        Safe to call while send_message() is actively reading stdout —
        stdin and stdout are independent pipes.  Does NOT acquire the
        session lock.
        """
        if self.proc is None or self.proc.stdin is None:
            return
        if approved:
            permission = {"behavior": "allow"}
        else:
            permission = {"behavior": "deny", "message": "User denied"}
        msg = json.dumps({
            "type": "control_response",
            "response": {
                "request_id": request_id,
                "subtype": "success",
                "permission": permission,
            },
        })
        self.proc.stdin.write((msg + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        logger.info(
            f"Persistent session {self.session_id}: "
            f"sent permission {'allow' if approved else 'deny'} "
            f"for request {request_id[:8]}"
        )

    async def interrupt_turn(self) -> bool:
        """Gracefully interrupt the in-flight turn: fire-and-forget stdin write
        of ``control_request {subtype: "interrupt"}`` — the same frame the
        Agent SDK's ``interrupt()`` sends. Mirrors ``send_control_response``:
        deliberately never reads stdout (during a turn ``send_message``'s loop
        is the sole reader; the translator drops the control_response frame).
        The CLI closes the turn with a normal result event and KEEPS the
        partial turn in its own session history — so the caller must let the
        turn's consumer run to completion, and no cancelled-context injection
        is needed afterwards. Returns False when there is no live turn to
        interrupt or the pipe is dead (caller falls back to killpg).
        """
        if (self._closed or self.proc is None or self.proc.stdin is None
                or self.proc.returncode is not None or not self._turn_active):
            return False
        msg = json.dumps({
            "type": "control_request",
            "request_id": str(uuid.uuid4()),
            "request": {"subtype": "interrupt"},
        })
        try:
            self.proc.stdin.write((msg + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError):
            return False
        self._post_interrupt_watch = True
        logger.info(
            f"Persistent session {self.session_id}: sent graceful interrupt "
            f"(turn_seq={self._turn_seq})"
        )
        return True

    async def send_message(
        self, prompt: str, settle_after_result: float = 0,
        inject_time: bool = False,
    ) -> AsyncIterator[ClaudeStreamChunk]:
        """Write a message to stdin and yield response chunks from stdout.

        The caller MUST hold self.lock before calling this.
        Output format is identical to run_claude_stream (content_block_start,
        content_block_delta, etc.) so the same SSE logic works.

        settle_after_result: if > 0, don't return on the first result event.
            Instead, switch to timeout-based reading and continue yielding
            chunks from follow-up turns (e.g. background agent results).
            Returns when no new events arrive for this many seconds.
            Used for task execution where background agents may produce
            additional turns after the initial response.
        inject_time: if True, prepend current datetime to the message.
            Used for user messages in persistent sessions where the system
            prompt datetime becomes stale.
        """
        if self._closed or self.proc is None or self.proc.returncode is not None:
            raise RuntimeError(f"Persistent session {self.session_id} is not running")

        # Turn-active span for the graceful-interrupt machinery — the finally
        # covers every exit (result, EOF, cancellation via GeneratorExit).
        self._turn_seq += 1
        self._turn_active = True
        try:
            async for _chunk in self._drive_turn(
                prompt, settle_after_result, inject_time,
            ):
                yield _chunk
        finally:
            self._turn_active = False

    async def _drive_turn(
        self, prompt: str, settle_after_result: float,
        inject_time: bool,
    ) -> AsyncIterator[ClaudeStreamChunk]:
        """The actual turn drive — body of :meth:`send_message` (which wraps
        it only to maintain the turn-active span)."""
        self.last_activity = time.monotonic()

        # Drain any stale output left in the pipe from a previous interrupted
        # response (e.g. SSE broken by client disconnect / phone barge-in).
        # Without this, the next read would consume old data and desync.
        await self._drain_stale_output()

        # Optionally prepend current datetime so the agent has accurate time
        # (system prompt datetime becomes stale in long-lived persistent sessions)
        content = prompt
        if inject_time:
            user_tz = get_session_user_tz(self.session_id)
            content = f"[Current time: {config.format_current_time(user_tz)}]\n\n{prompt}"
            from core.session import sibling_awareness
            sibling_line = await sibling_awareness.prelude_line(self.session_id)
            if sibling_line:
                content = f"{sibling_line}\n\n{content}"

        # Write NDJSON message to stdin
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": content},
        })
        self.proc.stdin.write((msg + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

        logger.info(f"Persistent session {self.session_id}: sent message ({len(prompt)} chars)")

        # Parsing state + settle decisions are delegated to shared classes
        # that are reused by RemoteExecutionLayer. This guarantees identical
        # event semantics on local-sandboxed and remote-unsandboxed paths.
        # Fresh subagent registry per turn — spawn/finish tracking starts clean.
        # The background-command registry resets too, but PRESERVES still-running
        # commands so one spawned in a prior turn keeps its badge + completion
        # wait across a follow-up turn (mirror of reset_subagent_registry).
        reset_subagent_registry(self.session_id)
        reset_bg_command_registry(self.session_id)
        translator = ClaudeCLIEventTranslator(self.session_id)
        settle = SettleController(
            self.session_id, settle_after_result, translator,
        )

        # Foreign-result skip regime (see settle.ForeignSkipGate): resume
        # handshake / settlement results must not close the driven turn —
        # the valve, not a count, ends the regime.
        gate = ForeignSkipGate(self.session_id, translator)
        while True:
            # Choose readline timeout based on settle state. In pre-settle we
            # use a generous 60 s for heartbeat logging; in settle the
            # controller uses a short slice so it can re-check the
            # SubagentRegistry for pending background agents.
            timeout = gate.clamp_timeout(settle.effective_timeout())
            try:
                raw_line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                if gate.expired() and not settle.settling:
                    # Silence valve: nothing followed the skip regime — the
                    # mis-skipped result was probably legitimate. Close.
                    logger.warning(
                        f"Persistent session {self.session_id}: no stdout "
                        f"{FOREIGN_SKIP_SILENCE_S:.0f}s after a skipped "
                        f"foreign result — closing the turn"
                    )
                    yield ClaudeStreamChunk(
                        is_done=True,
                        session_id=translator.actual_session_id,
                    )
                    return
                if not settle.settling:
                    # Pre-settle stdout silence — emit heartbeat, bail if dead
                    proc_alive = self.proc and self.proc.returncode is None
                    settle.log_presettle_heartbeat(
                        agents_spawned=translator.agents_spawned,
                        proc_alive=bool(proc_alive),
                    )
                    if not proc_alive:
                        logger.error(
                            f"Persistent session {self.session_id}: process dead "
                            f"in pre-settle (rc={self.proc.returncode if self.proc else '?'})"
                        )
                        return
                    continue
                # In settle: decide whether to exit or keep waiting for hooks
                if settle.should_exit_on_silence(timeout):
                    return
                continue

            if not raw_line:
                # EOF — process died
                stderr_msg = ""
                if self.proc and self.proc.stderr:
                    with contextlib.suppress(Exception):
                        stderr_data = await asyncio.wait_for(
                            self.proc.stderr.read(), timeout=2,
                        )
                        stderr_msg = stderr_data.decode("utf-8", errors="replace").strip()
                rc = self.proc.returncode if self.proc else "?"
                if settle.settling:
                    logger.info(
                        f"Persistent session {self.session_id}: EOF during settle "
                        f"(rc={rc}, agents_spawned={translator.agents_spawned})"
                    )
                else:
                    logger.error(
                        f"Persistent session {self.session_id}: process died before "
                        f"result event (rc={rc}, agents_spawned="
                        f"{translator.agents_spawned}). "
                        f"stderr: {stderr_msg[:500] if stderr_msg else '(empty)'}"
                    )
                return

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Settle-mode bookkeeping: extend reaper, emit periodic heartbeat
            if settle.settling:
                self.last_activity = time.monotonic()
                logger.info(
                    f"Persistent session {self.session_id}: settle event: "
                    f"{data.get('type', '?')}"
                )
                settle.maybe_log_heartbeat(
                    proc_alive=bool(
                        self.proc and self.proc.returncode is None
                    ),
                )

            # A line arrived — slide the post-skip valve (never disarm:
            # noise must not remove the skip regime's only closer).
            gate.note_event()

            # Parse the event into one or more chunks and yield them.
            # The translator owns all per-turn state; we only observe the
            # raw event to handle the result-boundary book-keeping below.
            for chunk in translator.feed(data):
                if chunk_is_content(chunk):
                    gate.note_content()
                yield chunk

            if data.get("type") == "result":
                _record_session_use(translator.actual_session_id)
                self.last_activity = time.monotonic()

                # Preserve the detailed result-event log (for observability)
                result_text = data.get("result", "")
                is_error = data.get("is_error", False)
                cost = data.get("total_cost_usd", 0.0)
                duration = data.get("duration_ms", 0)
                num_turns = data.get("num_turns", "?")
                preview = (
                    result_text[:200] + "..."
                    if len(result_text) > 200
                    else result_text
                )
                logger.info(
                    f"Persistent session {self.session_id}: result event — "
                    f"is_error={is_error}, cost=${cost:.4f}, duration={duration}ms, "
                    f"turns={num_turns}, agents_spawned={translator.agents_spawned}, "
                    f"text_preview={preview!r}"
                )

                # Foreign results skip BEFORE the wedge watch below: a
                # settlement mini-turn's success must neither close the turn
                # nor disarm the watch (the driven turn hasn't run yet).
                # Error results are never foreign and still reach the repair.
                # Task mode must not enter settle on one either — the
                # job-done 5s fast-grace would close the turn just the same.
                if not settle.settling and is_foreign_result(
                    data, gate.content_since_skip,
                ):
                    gate.record_skip(data)
                    continue

                # #63943 wedge repair: a graceful interrupt mid-thinking can
                # persist an unsigned thinking block that 400s every later
                # turn. On the signature error after an interrupt: kill the
                # process group (next message auto-resumes via --resume) and
                # re-arm the cancelled-context injection the graceful path
                # suppressed. A clean result disarms the watch.
                if self._post_interrupt_watch:
                    if is_error and _THINKING_SIG_RE.search(str(result_text)):
                        self._post_interrupt_watch = False
                        logger.warning(
                            f"Persistent session {self.session_id}: thinking-"
                            f"signature 400 after graceful interrupt — killing "
                            f"process for --resume recovery"
                        )
                        _kill = asyncio.create_task(
                            interrupt_persistent_session(self.session_id)
                        )
                        _repair_tasks.add(_kill)
                        _kill.add_done_callback(_repair_tasks.discard)
                        try:
                            from storage import database as task_store
                            chat = task_store.get_chat_by_session(self.session_id)
                            if chat:
                                task_store.update_chat(
                                    chat["id"], last_turn_aborted=True,
                                    last_abort_graceful=False,
                                )
                        except Exception:
                            logger.exception(
                                f"Persistent session {self.session_id}: "
                                f"wedge-repair chat flag update failed"
                            )
                    elif not is_error:
                        self._post_interrupt_watch = False

                if settle.is_interactive_done():
                    yield ClaudeStreamChunk(
                        is_done=True,
                        session_id=translator.actual_session_id,
                    )
                    return  # Turn complete, process stays alive

                # Task mode: enter settle (clears parsing state; keeps counters)
                settle.enter_settle()

    async def close(self) -> None:
        """Gracefully close the persistent session."""
        if self._closed:
            return
        self._closed = True

        if self.proc and self.proc.returncode is None:
            logger.info(f"Closing persistent session {self.session_id} (pid={self.proc.pid})")
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=10)
                logger.info(f"Persistent session {self.session_id} exited cleanly")
            except asyncio.TimeoutError:
                logger.warning(f"Persistent session {self.session_id} didn't exit, killing")
                await _kill_process(self.proc, self.session_id)
        _active_processes.pop(self.session_id, None)
        # Clean up temp prompt file (only exists for new sessions, not resumes)
        if self._prompt_file:
            with contextlib.suppress(Exception):
                self._prompt_file.unlink(missing_ok=True)

    @property
    def is_alive(self) -> bool:
        return (
            self._started
            and not self._closed
            and self.proc is not None
            and self.proc.returncode is None
        )


# ---------------------------------------------------------------------------
# Session pool and lifecycle functions
# ---------------------------------------------------------------------------

# Session pool: session_id -> PersistentSession
_persistent_sessions: dict[str, PersistentSession] = {}
_persistent_sessions_lock = asyncio.Lock()


async def active_agent_names() -> set[str]:
    """Agent slugs of every live persistent CLI session.

    The auto-update in-use guard (services/mcp_updater.mcp_in_use) maps these to
    each agent's runtime MCP set to decide whether a docker MCP it's about to
    recreate is currently connected by a CLI session.
    """
    async with _persistent_sessions_lock:
        return {s.agent_name for s in _persistent_sessions.values()
                if getattr(s, "agent_name", "") and s.is_alive}


async def get_persistent_session(session_id: str) -> PersistentSession | None:
    """Return an existing alive persistent session, or None. Never creates."""
    async with _persistent_sessions_lock:
        session = _persistent_sessions.get(session_id)
        if session and session.is_alive:
            return session
        if session:
            _persistent_sessions.pop(session_id, None)
        return None


async def get_or_create_persistent_session(
    session_id: str,
    agent_prompt: str | None = None,
    mcp_config_path: Path | None = None,
    permission_mode: str = "auto",
    client_type: str = "",
    allow_resume: bool = False,
    use_native_permissions: bool = False,
    model: str = "",
    effort: str = "",
    extra_env: dict[str, str] | None = None,
    credential_env: dict[str, str] | None = None,
    sandbox_builder=None,
    agent_name: str = "",
) -> PersistentSession:
    """Get an existing persistent session or create a new one.

    If the session was previously used but the persistent process is dead
    (reaped by timeout), raises RuntimeError so the caller falls back to
    one-shot mode with ``--resume`` which restores full conversation context.

    When ``allow_resume`` is True (used by task sessions), a reaped session
    is recreated with ``--resume`` instead of raising — MCPs restart but
    conversation context is preserved from disk.

    When ``use_native_permissions`` is True (dashboard sessions), the CLI
    process is started with ``--permission-mode <mode>`` instead of
    ``--dangerously-skip-permissions``.  Permission prompts come via the
    control channel on stdout rather than through the hook system.
    """
    async with _persistent_sessions_lock:
        session = _persistent_sessions.get(session_id)
        if session and session.is_alive:
            logger.info(f"Reusing persistent session {session_id}")
            return session

        # Clean up dead session if exists
        if session:
            _persistent_sessions.pop(session_id, None)

        # If this session was used before (has message history on disk),
        # don't create a fresh persistent process — it would fail because
        # Claude CLI rejects --session-id for already-used IDs.
        # Instead, let the caller fall back to one-shot --resume.
        if session_exists(session_id):
            if allow_resume:
                # Task sessions: recreate with --resume (MCPs restart, context preserved)
                session = PersistentSession(
                    session_id=session_id,
                    agent_prompt=agent_prompt,
                    mcp_config_path=mcp_config_path,
                    permission_mode=permission_mode,
                    client_type=client_type,
                    resume=True,
                    use_native_permissions=use_native_permissions,
                    model=model,
                    effort=effort,
                    extra_env=extra_env,
                    credential_env=credential_env,
                    sandbox_builder=sandbox_builder,
                    agent_name=agent_name,
                )
                _persistent_sessions[session_id] = session
            else:
                raise RuntimeError(
                    f"Persistent session {session_id} was reaped; "
                    f"falling back to one-shot --resume"
                )
        else:
            # Create new (truly new session, never used before)
            session = PersistentSession(
                session_id=session_id,
                agent_prompt=agent_prompt,
                mcp_config_path=mcp_config_path,
                permission_mode=permission_mode,
                client_type=client_type,
                use_native_permissions=use_native_permissions,
                model=model,
                effort=effort,
                extra_env=extra_env,
                credential_env=credential_env,
                sandbox_builder=sandbox_builder,
                agent_name=agent_name,
            )
            _persistent_sessions[session_id] = session

    # Start outside the pool lock (slow: MCP init)
    await session.start()
    _record_session_use(session_id, client_type=client_type)
    return session


async def close_persistent_session(session_id: str) -> bool:
    """Close a persistent session by ID. Returns True if found."""
    async with _persistent_sessions_lock:
        session = _persistent_sessions.pop(session_id, None)
    if session:
        await session.close()
        # Any backgrounded subagents/commands died with the process group —
        # clear their badges (the dead CLI can never emit the clears itself).
        clear_session_liveness(session_id, reason="cli_close")
        logger.info(f"Closed persistent session {session_id}")
        return True
    return False


async def abort_persistent_session(session_id: str) -> bool:
    """Kill a persistent session's process (abort). Returns True if found."""
    async with _persistent_sessions_lock:
        session = _persistent_sessions.pop(session_id, None)
    if session and session.proc and session.proc.returncode is None:
        _aborted_sessions.add(session_id)
        logger.info(f"Aborting persistent session {session_id} (pid={session.proc.pid})")
        await _kill_process(session.proc, session_id)
        session._closed = True
        return True
    return False


async def interrupt_persistent_session(session_id: str) -> bool:
    """Kill the process for a persistent session but KEEP the session entry.

    Unlike abort_persistent_session, the session stays in _persistent_sessions
    with a dead process. The next send_message() detects proc.returncode != None
    and auto-resumes with --resume, preserving CLI conversation history.
    """
    # Release any pending permission waiter for this session (deny) so an abort
    # mid-prompt can't strand the hook coroutine (and its MCP pipe).
    resolve_session_permissions(session_id, approved=False)
    session = _persistent_sessions.get(session_id)
    if session and session.proc and session.proc.returncode is None:
        logger.info(f"Interrupting persistent session {session_id} (pid={session.proc.pid})")
        await _kill_process(session.proc, session_id)
        return True
    return False


async def reap_idle_sessions() -> None:
    """Background task: reap persistent sessions idle longer than timeout."""
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        to_reap: list[str] = []

        async with _persistent_sessions_lock:
            for sid, session in _persistent_sessions.items():
                idle = now - session.last_activity
                # Also check hook activity — background agents may be
                # working without producing stdout events or send_message
                # calls that update last_activity.
                last_hook = get_hook_activity(sid)
                if last_hook:
                    hook_idle = now - last_hook
                    idle = min(idle, hook_idle)
                if idle > config.get_idle_timeout():
                    to_reap.append(sid)
                elif not session.is_alive:
                    to_reap.append(sid)

        for sid in to_reap:
            logger.info(f"Reaping idle persistent session {sid}")
            await close_persistent_session(sid)
            # Release concurrency slot + subscription (bypasses layer.close_session)
            from core.concurrency import release_chat_slot
            release_chat_slot(sid)
            from services.engines.subscription_pool import release_subscription
            release_subscription(sid)



