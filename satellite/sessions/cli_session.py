"""Claude Code CLI session management for satellite daemon.

Manages persistent CLI subprocesses (no bwrap). Mirrors the platform's
PersistentSession but runs directly on the remote host filesystem.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..host import env_hygiene, path_translator
from ..transport import file_sync
from ..config import (
    hook_command,
    kill_process_tree,
    otodock_dir,
    reap_descendants,
    snapshot_descendants,
)
import contextlib

if TYPE_CHECKING:
    from ..config import SatelliteConfig

logger = logging.getLogger("satellite")


def _format_time() -> str:
    return datetime.now(timezone.utc).strftime("%A, %B %d, %Y %H:%M")


def _write_cli_hooks(claude_dir: Path, disallowed_tools: list | None = None) -> None:
    """Write settings.json with hook config pointing to scripts in the .claude/ dir.

    Also disables Claude Code's built-in auto-memory subsystem
    (``autoMemoryEnabled: false``) so satellite-side sessions match the
    proxy-side behaviour — otodock memory-mcp is the only memory store.
    Belt-and-braces with the env-var injection in ``CLISession.start``.
    """
    gate = hook_command(claude_dir / "permission_gate.py")
    forwarder = hook_command(claude_dir / "tool_result_forwarder.py")
    subagent = hook_command(claude_dir / "subagent_tracker.py")

    settings = {
        "autoMemoryEnabled": False,
        # Pin/freeze the CLI version: disable Claude Code's own auto-updater so a
        # satellite install can't drift off the platform pin (the satellite
        # reconciles the pinned version). Belt-and-braces with env
        # DISABLE_AUTOUPDATER=1 set in CLISession/PtySession.
        "autoUpdates": False,
        # Platform is the only skill source: plugin skills must not activate
        # outside install/approval (mirrors the proxy-side settings builder).
        "enabledPlugins": {},
        # Built-in-tool deny list, shipped by the proxy (single source of truth:
        # core.sandbox._DISALLOWED_BUILTIN_TOOLS) so a REMOTE session enforces the
        # SAME permissions.deny as the local sandbox (the claude.ai
        # Cron/Trigger/Push/integration tools; Skill is ALLOWED since 2026-07 —
        # platform-managed skills activate through it). Omitted → no deny block.
        **(
            {"permissions": {"deny": list(disallowed_tools)}}
            if disallowed_tools else {}
        ),
        "hooks": {
            "PreToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": gate,
                    "timeout": 604800,
                }],
            }],
            "PostToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": forwarder,
                    "timeout": 10,
                }],
            }],
            # Deterministic, idle-safe subagent completion (fg + bg). The
            # script is shipped by the proxy via hook_scripts and written by
            # _write_hook_scripts before this settings.json. The SubagentStop
            # subprocess hits the loopback tunnel → /v1/hooks/subagent.
            "SubagentStop": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": subagent,
                    "timeout": 10,
                }],
            }],
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")


def _write_hook_scripts(claude_dir: Path, scripts: dict[str, str]) -> None:
    """Write hook scripts sent by the proxy into the .claude/ dir.

    Makes them executable. Refreshed on every session start so satellites
    always run the current proxy version of each hook.
    """
    if not scripts:
        return
    for filename, content in scripts.items():
        if not content:
            continue
        path = claude_dir / filename
        path.write_text(content)
        # Hooks run via `"python.exe" "<path>"` on all platforms, so the
        # exec bit is only useful for Unix shebang dispatch (and a no-op
        # on Windows).
        if sys.platform != "win32":
            path.chmod(0o755)


class CLISession:
    """Persistent Claude Code CLI subprocess (direct, no bwrap).

    The satellite is a dumb pipe: it spawns the CLI process on ``start()``,
    writes user messages to stdin on ``send_message()``, forwards every raw
    NDJSON line as a ``session_event`` over the WS, and exits its read
    loop when the proxy sends ``stop_turn`` (or EOF). No settle logic, no
    turn-end decisions — those live on the proxy.
    """

    execution_path = "claude-code-cli"

    def __init__(
        self,
        session_id: str,
        agent_dir: Path,
        config: dict,
        sat_config: "SatelliteConfig",
    ):
        self.session_id = session_id
        self.agent_dir = agent_dir
        self.agent_slug = agent_dir.name
        self.config = config
        self.sat_config = sat_config
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self._file_snapshot: dict[str, str] = {}
        self._init_done = asyncio.Event()
        self._cwd: Path | None = None
        self._claude_dir: Path | None = None
        # Set by SessionManager when the proxy sends `stop_turn` — ends
        # the current send_message iteration cleanly.
        self.stop_turn_event: asyncio.Event = asyncio.Event()
        # Post-turn background-command drain: when the proxy's stop_turn carries
        # drain_bg (a bg command still pending), keep reading + forwarding stdout
        # AFTER the turn so the command's completion frame (claude emits it idle,
        # post-turn) reaches the proxy. Cancelled when the next turn starts (it
        # reclaims stdout — single reader).
        self._drain_bg: bool = False
        self._bg_drain_task: "asyncio.Task | None" = None

    async def start(self) -> None:
        """Write config files and spawn CLI subprocess."""
        # Resolve paths. Honor an absolute `work_cwd` the SAME
        # way the interactive PTY sessions do — a dashboard `-p` resume of an
        # otodock chat must run in the chat's original folder, else Claude (which
        # keys its session JSONL by cwd) can't find it and `--resume` 404s. A bad
        # work_cwd never breaks a normal `-p` session — fall back to the in-tree
        # default (an otodock resume whose folder is gone then hits the
        # DB-fallback path rather than crashing here).
        from ..terminal.pty_session_base import resolve_work_cwd
        try:
            self._cwd = resolve_work_cwd(
                self.config.get("work_cwd") or "",
                self.agent_dir,
                self.config["cwd_relative"],
            )
        except ValueError:
            logger.warning(
                "invalid work_cwd for -p session %s; using the agent dir",
                self.session_id, exc_info=True,
            )
            self._cwd = self.agent_dir / self.config["cwd_relative"]
        self._claude_dir = self.agent_dir / self.config["claude_dir_relative"]
        self._claude_dir.mkdir(parents=True, exist_ok=True)
        self._cwd.mkdir(parents=True, exist_ok=True)

        # Defensive cleanup: wipe Claude Code's built-in auto-memory dirs
        # (``.claude/projects/{cwd}/memory/``). The platform's otodock
        # memory-mcp is the single source of memory truth; running both
        # in parallel splits the agent's view. Mirrors the proxy-side
        # cleanup in ``ensure_persistent_claude_dir``. Session JSONLs
        # in ``projects/{id}/`` (used for ``--resume``) are left
        # untouched — only the ``memory/`` subdir is removed.
        projects_dir = self._claude_dir / "projects"
        if projects_dir.exists():
            import shutil as _shutil
            for proj in projects_dir.iterdir():
                if not proj.is_dir():
                    continue
                mem_dir = proj / "memory"
                if mem_dir.exists():
                    _shutil.rmtree(mem_dir, ignore_errors=True)

        # Write hook scripts shipped from the proxy. These must land before
        # settings.json is written, because settings.json points at them.
        _write_hook_scripts(self._claude_dir, self.config.get("hook_scripts", {}))

        # Write system prompt
        prompt_file = self._claude_dir / "system-prompt.md"
        prompt_file.write_text(self.config.get("system_prompt", ""))

        # Write MCP config (expand ~ to actual home dir). Use forward
        # slashes in substituted paths — embedding raw Windows
        # backslashes into JSON produces illegal escape sequences like
        # `\U`, `\d` and Claude Code rejects the file with "MCP config
        # is not a valid JSON". Forward slashes are valid Python paths
        # on Windows and don't need JSON escaping.
        mcp_config = self.config.get("mcp_config")
        mcp_file = self._claude_dir / "mcp-config.json"
        if mcp_config and mcp_config.get("mcpServers"):
            # Wrap stdio MCPs that declared `tool_arg_paths`
            # with the path interceptor BEFORE serialization, so the
            # disk-resident config the CLI reads is already wrapped.
            from .mcp_interceptor import wrap_interceptor_in_mcp_config
            wrap_interceptor_in_mcp_config(mcp_config)
            mcp_raw = json.dumps(mcp_config, indent=2)
            otodock_fwd = str(otodock_dir()).replace("\\", "/")
            home_fwd = str(Path.home()).replace("\\", "/")
            mcp_raw = mcp_raw.replace("~/.oto-dock", otodock_fwd)
            mcp_raw = mcp_raw.replace("~/", home_fwd + "/")
            mcp_file.write_text(mcp_raw)
        else:
            mcp_config = None  # signal to skip --mcp-config flag
            mcp_file.write_text(json.dumps({"mcpServers": {}}, indent=2))

        # Write hooks
        _write_cli_hooks(self._claude_dir, self.config.get("disallowed_tools"))

        # OAuth file delivery: the CLI authenticates from .credentials.json in
        # CLAUDE_CONFIG_DIR (never env — env is frozen at exec, so the pool's
        # rotation fan-out could never reach a live process). Rewritten on
        # rotation by ``credentials_update`` pushes (session_manager).
        creds = self.config.get("credentials_json")
        if creds:
            creds_path = self._claude_dir / ".credentials.json"
            creds_path.write_text(json.dumps(creds))
            creds_path.chmod(0o600)

        # Build command. The binary comes from pin-verified resolution, not
        # PATH — see host/cli_versions.py.
        from ..host.cli_versions import resolve_spawn_bin_async
        claude_bin = await resolve_spawn_bin_async(
            "claude", self.sat_config.claude_bin,
        )
        cmd = [claude_bin, "-p"]
        model = self.config.get("model", "")
        if model:
            cmd += ["--model", model]
        effort = self.config.get("effort", "")
        if effort:
            cmd += ["--effort", effort]
        cmd += ["--max-thinking-tokens", str(self.config.get("max_thinking_tokens", 100000))]
        # Permission mode mirrors the local CLI layer (proxy ships both fields).
        permission_mode = self.config.get("permission_mode", "default")
        if self.config.get("use_native_permissions"):
            cmd += ["--permission-mode", permission_mode]
        elif permission_mode == "plan":
            cmd += ["--permission-mode", "plan"]
        else:
            cmd += ["--dangerously-skip-permissions"]
            # Mirrors the local CLI layer: the prompt-tool's PRESENCE makes
            # the CLI expose AskUserQuestion headless (absent from every -p
            # roster since at least 2.1.193). Bypass never raises permission
            # prompts and the PreToolUse hook denies-and-surfaces the
            # question, so the stdio channel stays silent. Not added to the
            # plan/native branches (a real prompt could route to stdio,
            # which nothing answers).
            cmd += ["--permission-prompt-tool", "stdio"]
        if mcp_config is not None:
            cmd += ["--mcp-config", str(mcp_file)]
        _is_resume = bool(
            self.config.get("resume") and self.config.get("session_id_for_resume")
        )
        # System prompt on fresh AND --resume starts (mirrors the local CLI
        # layer): the CLI rebuilds its system prompt from each invocation's
        # flags — transcripts persist messages only — so skipping this on
        # resume would re-warm the session with the stock SDK prompt (no
        # agent identity). The proxy ships system_prompt on resume too.
        cmd += ["--append-system-prompt-file", str(prompt_file)]
        cmd += ["--output-format", "stream-json"]
        cmd += ["--input-format", "stream-json"]
        cmd += ["--verbose", "--include-partial-messages"]

        if _is_resume:
            cmd += ["--resume", self.config["session_id_for_resume"]]
        else:
            cmd += ["--session-id", self.session_id]

        # Build env. Curate the operator's AMBIENT env (drop their personal shell
        # secrets) BEFORE overlaying the platform-shipped config["env"] (which
        # carries the intentional GH_TOKEN / PROXY_API_KEY / ANTHROPIC_API_KEY
        # and survives curation). Defense-in-depth — see env_hygiene.py.
        env = env_hygiene.curate_satellite_env(os.environ)
        env.update(self.config.get("env", {}))
        env["CLAUDE_CONFIG_DIR"] = str(self._claude_dir)
        env["OTO_SESSION_ID"] = self.session_id
        # Belt-and-braces disable of Claude Code's auto-memory subsystem
        # (paired with ``autoMemoryEnabled: false`` in settings.json and
        # the session-start memory-dir wipe above). See proxy-side
        # ``env_builder.py`` for the matching local-session injection.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["DISABLE_AUTOUPDATER"] = "1"  # version pin/freeze (see VERSIONS.md)
        # Platform is the only skill source — bundled CLI skills stay off;
        # platform skills arrive as synced .claude/skills/ folders. Mirrors
        # proxy env_builder.
        env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"
        # Nested subagents (depth-3 default since CC 2.1.219) are invisible to
        # the platform — freeze at depth 1. Mirrors proxy env_builder.
        env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] = "1"
        # CC 2.1.233 removed Todo/Task tools on newer models; the dashboard
        # todo checklist depends on their frames. Mirrors proxy env_builder.
        env["CLAUDE_CODE_ENABLE_TODO_TOOLS"] = "1"
        env.pop("CLAUDECODE", None)
        # Force UTF-8 IO — Windows console default is cp1252 and breaks
        # tool-result JSON parse on non-ASCII content.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")

        # Per-session secret files: fetch from the platform's session-file
        # broker over the tunnel BEFORE spawn — SSH keys (admin-paired
        # machines; injects OTO_SSH_KEY_DIR) + OAuth token files landed at
        # their virtual credentials_dir target inside the agent tree. No
        # token in the payload → no-op. The fetch token never enters the
        # CLI env; everything is wiped at session close.
        from . import session_files
        env.update(await asyncio.to_thread(
            session_files.materialize, self.config, self.session_id,
            self.agent_dir,
        ))

        # Translate sandbox-style virtual paths (e.g. /users/{u}/workspace,
        # /workspace, /.claude) to satellite-absolute paths. The proxy ships
        # sandbox-style values for cross-target uniformity; the satellite has
        # no bwrap so it has to do the mapping that bwrap would. Also expands
        # the literal {session_id} token wherever a path value contains it (no
        # built-in role emits it now). The ``multi_value_envs`` map (built proxy-side
        # from manifest path_env decls + OTO_ALLOWED_ROOTS) tells the
        # translator which env vars are separator-joined sandbox-path lists
        # so it can split-translate-rejoin them. See path_translator.py.
        username = path_translator.derive_username_from_cwd_relative(
            self.config.get("cwd_relative", ""),
        )
        env = path_translator.translate_env(
            env,
            agent_dir=self.agent_dir,
            username=username,
            session_id=self.session_id,
            multi_value_envs=self.config.get("multi_value_envs") or {},
        )

        # Spawn. Bump StreamReader buffer to 10MB (default is 64KB) so the
        # CLI's `--output-format stream-json` lines don't get truncated. A
        # single MCP tool-result line can blow past 64KB easily — e.g.
        # unifi-network's `list_devices` or `get_dashboard` JSON for a
        # network with 30+ clients. Hitting the default limit raises
        # asyncio.LimitOverrunError and silently kills the stream reader,
        # ending the pump mid-turn with no `done` ever reaching the
        # dashboard (UI freezes; nothing persisted to DB).
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self._cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=200 * 1024 * 1024,
        )
        self.pid = self.proc.pid
        logger.info(f"CLI session started: pid={self.pid}, session={self.session_id}")

        # Drain stderr in the background so a crashed CLI's error is
        # surfaced to the satellite log instead of vanishing into a
        # full pipe buffer. Cap collected bytes at ~64KB so a chatty
        # process doesn't balloon memory.
        self._stderr_buf: list[bytes] = []
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Don't wait for init — the CLI emits system.init on stdout only after
        # the first message is written to stdin. The init event will be captured
        # during the first send_message() call, same as the local proxy does.

        # Snapshot files for change detection. Off-thread — a big agent tree's
        # hash walk must not stall the event loop at session start (cache-cold
        # it can be seconds; heartbeats/PTY frames share the loop).
        self._file_snapshot = await asyncio.to_thread(
            file_sync.snapshot_agent_dir, self.agent_dir,
        )

    async def send_message(
        self,
        prompt: str,
        *,
        inject_time: bool = False,
    ):
        """Write user message to stdin, yield every raw NDJSON line from
        stdout until the proxy sends ``stop_turn`` or the process exits.

        The proxy owns all turn-end decisions (see
        ``core/layers/cli/settle.py:SettleController``). This generator
        simply pumps events verbatim.
        """
        if self.proc is None or self.proc.returncode is not None:
            stderr_text = b"".join(self._stderr_buf).decode(
                "utf-8", errors="replace"
            ).strip() if getattr(self, "_stderr_buf", None) else ""
            rc = self.proc.returncode if self.proc else None
            if stderr_text:
                logger.error(
                    "CLI process exited (returncode=%s) before send_message. "
                    "stderr tail:\n%s",
                    rc, stderr_text[-2000:],
                )
            else:
                logger.error(
                    "CLI process exited (returncode=%s) before send_message "
                    "with empty stderr.",
                    rc,
                )
            raise RuntimeError(
                f"CLI process not running (exit={rc}); "
                f"stderr: {stderr_text[-300:] if stderr_text else '(empty)'}"
            )

        # A new turn reclaims stdout — stop any post-turn bg-command drainer
        # first (single reader on the CLI's stdout).
        await self.cancel_bg_drain()
        # Clear any residual stop_turn signal from a previous turn
        self.stop_turn_event.clear()
        self._drain_bg = False

        if inject_time:
            prompt = f"[Current time: {_format_time()}]\n{prompt}"

        # Translate sandbox-virtual paths (`/users/{u}/...`, `/workspace/...`)
        # the proxy injects for chat-attached photos and files into satellite-
        # absolute paths. Mirrors `translate_env` for env vars; without this
        # the CLI subprocess would try to read sandbox paths literally and
        # fail (no bwrap on satellite). See proxy/ws/dashboard.py::_handle_chat.
        username = path_translator.derive_username_from_cwd_relative(
            self.config.get("cwd_relative", "")
        )
        prompt = path_translator.translate_paths_in_text(
            prompt, agent_dir=self.agent_dir, username=username,
        )

        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prompt},
        })
        self.proc.stdin.write((msg + "\n").encode())
        await self.proc.stdin.drain()

        # Read every line until stop_turn or EOF. No filtering.
        async for line in self._read_stdout_until_stop():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield event

    async def _drain_stderr(self) -> None:
        """Continuously read stderr into a bounded buffer.

        Without this, a chatty CLI fills the OS pipe buffer and either
        deadlocks (Linux) or crashes immediately (Windows). The collected
        bytes are surfaced if ``send_message`` finds the process dead.
        """
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            while True:
                chunk = await self.proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr_buf.append(chunk)
                # Keep only the last ~64KB so a runaway process doesn't
                # balloon memory.
                if sum(len(c) for c in self._stderr_buf) > 65536:
                    joined = b"".join(self._stderr_buf)[-65536:]
                    self._stderr_buf = [joined]
        except (asyncio.CancelledError, Exception):
            return

    async def _read_stdout_until_stop(self):
        """Yield decoded stdout lines until EOF or ``stop_turn`` fires.

        Uses a short timeout on each readline so we can periodically check
        the stop_turn event without missing quick succession of lines.
        """
        assert self.proc and self.proc.stdout
        while True:
            if self.proc.returncode is not None:
                return
            if self.stop_turn_event.is_set():
                return
            try:
                line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue
            if not line:
                return
            decoded = line.decode(errors="replace").strip()
            if decoded:
                yield decoded

    def request_stop_turn(self, drain_bg: bool = False) -> None:
        """Signal the current send_message() loop to exit gracefully. ``drain_bg``
        (set by the proxy when a backgrounded command is still pending) arms a
        post-turn stdout drainer once this turn's loop exits."""
        self._drain_bg = drain_bg
        self.stop_turn_event.set()

    def start_bg_drain(self, forward) -> None:
        """If the last stop_turn requested it (``drain_bg``), start a post-turn
        stdout drainer that forwards each NDJSON event via ``await forward(ev)``
        until the next turn cancels it (or EOF / a 10-min ceiling). This is how a
        backgrounded command's completion frame — claude emits it idle, after the
        turn — reaches the proxy's bg-command monitor on the remote path."""
        if not self._drain_bg or self._bg_drain_task is not None:
            return
        self._bg_drain_task = asyncio.create_task(self._bg_drain_loop(forward))

    async def cancel_bg_drain(self) -> None:
        """Stop the post-turn drainer (the next turn reclaims stdout)."""
        task, self._bg_drain_task = self._bg_drain_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _bg_drain_loop(self, forward) -> None:
        """Read + forward stdout frames post-turn until cancelled, EOF, or the
        ceiling. No idle exit — a sleeping bg command emits nothing for a while,
        then its task_updated{completed}."""
        if self.proc is None or self.proc.stdout is None:
            return
        deadline = asyncio.get_running_loop().time() + 600.0
        try:
            while asyncio.get_running_loop().time() < deadline:
                if self.proc.returncode is not None:
                    return
                try:
                    line = await asyncio.wait_for(
                        self.proc.stdout.readline(), timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue
                if not line:
                    return  # EOF — process exited
                decoded = line.decode(errors="replace").strip()
                if not decoded:
                    continue
                try:
                    event = json.loads(decoded)
                except json.JSONDecodeError:
                    continue
                await forward(event)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("bg-command post-turn drain failed")

    async def abort(self) -> None:
        """SIGINT (Unix) / terminate (Windows), then tree-kill on timeout.

        SIGINT lets Claude Code flush in-progress output on Unix; Windows
        has no equivalent so we go straight to terminate. The tree-kill
        backstop catches ``cmd.exe → node.exe`` grandchildren on Windows
        and MCP forks on Unix.
        """
        if not (self.proc and self.proc.returncode is None):
            return
        pid = self.proc.pid
        try:
            if sys.platform == "win32":
                self.proc.terminate()
            else:
                self.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            await asyncio.to_thread(kill_process_tree, pid, 5.0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=5)
        logger.info(f"CLI session aborted: {self.session_id}")

    async def close(self) -> None:
        """Close stdin, let the CLI exit cleanly, then reap leaked MCP children.

        Closing stdin gives claude EOF so it exits gracefully and persists its
        session for ``--resume``. But on Windows a cleanly-exited CLI ORPHANS
        its MCP children (they are not in a job/process group that dies with the
        parent), and a still-running ``.exe``/``.pyd`` keeps its venv directory
        locked — which then blocks every future MCP-update swap with WinError 5
        (see sync_mcps). So snapshot the child tree BEFORE the CLI exits (once
        it's gone its orphans can't be enumerated) and reap the survivors after.
        ``abort()`` already tree-kills; this brings normal teardown in line.
        """
        if self.proc and self.proc.returncode is None:
            children = await asyncio.to_thread(snapshot_descendants, self.proc.pid)
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
            await asyncio.to_thread(reap_descendants, children, 5.0)
            logger.info(f"CLI session closed: {self.session_id}")
        # Per-session secret files (SSH keys) die with the session.
        from . import session_files
        session_files.wipe(self.session_id)

    async def send_control_request(self, subtype: str, **kwargs) -> None:
        """Send a control request (set_model / set_permission_mode) to CLI via stdin.

        Wire format matches proxy/core/layers/cli/session.py:send_control_request.
        """
        if self.proc is None or self.proc.returncode is not None:
            return
        import uuid as _uuid
        msg = json.dumps({
            "type": "control_request",
            "request_id": str(_uuid.uuid4()),
            "request": {"subtype": subtype, **kwargs},
        })
        self.proc.stdin.write((msg + "\n").encode())
        await self.proc.stdin.drain()

    async def send_permission_response(
        self, request_id: str, approved: bool,
    ) -> None:
        """Answer a native can_use_tool permission prompt on the CLI's control channel.

        Wire format matches proxy/core/layers/cli/session.py:send_control_response.
        """
        if self.proc is None or self.proc.returncode is not None:
            return
        permission = (
            {"behavior": "allow"}
            if approved
            else {"behavior": "deny", "message": "User denied"}
        )
        msg = json.dumps({
            "type": "control_response",
            "response": {
                "request_id": request_id,
                "subtype": "success",
                "permission": permission,
            },
        })
        self.proc.stdin.write((msg + "\n").encode())
        await self.proc.stdin.drain()

    def detect_file_changes(self) -> list[dict]:
        """Compare current files against session-start snapshot."""
        return file_sync.detect_changes(self.agent_dir, self._file_snapshot)

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None
