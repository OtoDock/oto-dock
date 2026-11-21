"""Claude Code CLI session management for satellite daemon.

Manages persistent CLI subprocesses (no bwrap). Mirrors the platform's
PersistentSession but runs directly on the remote host filesystem.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config
from .._vendored import layout
from ..config import (
    hook_command,
    kill_process_tree,
    otodock_dir,
    reap_descendants,
    session_tools_env,
    snapshot_descendants,
    write_command_wrappers,
    write_if_changed,
)
from ..host import env_hygiene, path_translator
from ..transport import file_sync
from . import sandbox_host, session_homes, start_slots

if TYPE_CHECKING:
    from ..config import SatelliteConfig

logger = logging.getLogger("satellite")

# How long the post-turn stdout drain forwards when the proxy names no bound
# (a proxy before 1.9.1 reads a sub-agent's lines for ten minutes at most; a
# command's end it reads at any time, at the next turn).
DEFAULT_BG_DRAIN_S = 600.0

# A turn's reader checks its stop flag every second (``_read_stdout_until_stop``).
_READER_HANDOFF_S = 10.0
# A process that exited inside a turn: how long each read of what its pipe
# still holds may take (a child that kept the pipe open would hold the read
# for good), how long its exit code is waited for, and the most of its last
# stderr line the turn's end carries.
_EXIT_DRAIN_S = 0.5
_EXIT_CODE_WAIT_S = 2.0
_LAST_LINE_MAX = 300
# The inputs of the CLI's open ``can_use_tool`` requests kept for their
# answers; a request never answered ages out past this many.
_PROMPT_INPUTS_MAX = 64

# Claude Code's built-in plugins (2.1.287+) but the four the platform keeps
# on (agents-md, diff, telemetry, the managed-only sec-default), switched off
# by name in settings.json: an empty map leaves them on. The proxy's list
# (core/layers/cli/config_dir.DISABLED_BUILTIN_PLUGINS) is the source; the
# proxy suite holds the two equal.
DISABLED_BUILTIN_PLUGINS = (
    "cc-plugin-plugin-authoring@builtin",
    "cc-plugin-you-should-know@builtin",
    "cc-plugin-claude-test@builtin",
    "cc-plugin-mods-guide@builtin",
    "cc-plugin-mermaid@builtin",
    "cc-plugin-responsive-mode@builtin",
    "cc-plugin-tips@builtin",
    # 2.1.306: charts drawn in the transcript, a chart tool and charts under
    # MCP results. 2.1.312: the developer docs of platform.claude.com as an
    # HTTP MCP server, with a claude-api skill. Both dark behind remote flags.
    "cc-plugin-easel@builtin",
    "cc-plugin-claude-platform@builtin",
    # 2.1.322: the account-email check, on under these settings, refuses a
    # shell command holding the signed-in email and a User-Agent without
    # asking. The platform's gate judges commands; a refusal of its own ends
    # an unattended run's call with no card to answer.
    "cc-plugin-privacy@builtin",
)

# The events that carry a finished call's result to the forwarder: Claude
# Code runs PostToolUse for a call that succeeded and PostToolUseFailure for
# one that failed (a non-zero exit, a stopped command, a missing file, an MCP
# error). The proxy's list (core/layers/cli/config_dir.TOOL_RESULT_EVENTS) is
# the source; the proxy suite holds the two equal.
TOOL_RESULT_EVENTS = ("PostToolUse", "PostToolUseFailure")


def _format_time() -> str:
    return datetime.now(timezone.utc).strftime("%A, %B %d, %Y %H:%M")


def _write_cli_hooks(claude_dir: Path, disallowed_tools: list | None = None,
                     start_parts: int = 0, scripts_dir: Path | None = None) -> None:
    """Write settings.json with hook config pointing to scripts in the .claude/ dir.

    Also disables Claude Code's built-in auto-memory subsystem
    (``autoMemoryEnabled: false``) so satellite-side sessions match the
    proxy-side behaviour — otodock memory-mcp is the only memory store.
    Belt-and-braces with the env-var injection in ``CLISession.start``.
    ``claude_dir`` is the folder the engine reads (a session's home when it
    has one, so its settings are its own session's); the scripts stay in
    ``scripts_dir``, the store.
    """
    scripts = scripts_dir or claude_dir
    gate = hook_command(scripts / "permission_gate.py")
    forwarder = hook_command(scripts / "tool_result_forwarder.py")
    subagent = hook_command(scripts / "subagent_tracker.py")
    stop = hook_command(scripts / "stop_tracker.py")
    usage_script = scripts / "usage_report.py"
    start = hook_command(scripts / "start_context.py")

    settings = {
        "autoMemoryEnabled": False,
        # Pin/freeze the CLI version: disable Claude Code's own auto-updater so a
        # satellite install can't drift off the platform pin (the satellite
        # reconciles the pinned version). Belt-and-braces with env
        # DISABLE_AUTOUPDATER=1 set in CLISession/PtySession.
        "autoUpdates": False,
        # Platform is the only skill source: no marketplace plugin is enabled
        # and every built-in but the four the platform keeps is off by name
        # (mirrors the proxy-side settings builder).
        "enabledPlugins": {plugin: False for plugin in DISABLED_BUILTIN_PLUGINS},
        # Claude Code ≥ 2.1.275 syncs the signed-in claude.ai account's skills
        # and plugins into the session; sessions run on pool accounts, so
        # both stay off (mirrors core/layers/cli/config_dir.build_settings).
        "syncClaudeAiSkills": False,
        "syncClaudeAiPlugins": False,
        # The account's claude.ai connectors (Claude Docs, Google Drive, …)
        # would load as MCP servers too and act on the pool owner's account;
        # off (mirrors the proxy builder).
        "disableClaudeAiConnectors": True,
        # Plan keeps Claude Code's plain plan semantics, not its auto mode's
        # reviewer: a call it does not read as read-only becomes its ask,
        # which the proxy answers from the gate's record (mirrors the proxy
        # builder, which carries the rationale).
        "useAutoModeDuringPlan": False,
        # Built-in-tool deny list, shipped by the proxy (single source of truth:
        # core/layers/cli/config_dir.DISALLOWED_BUILTIN_TOOLS alone: a session's own
        # denials ride the gate over the tunnel, since this file is the scope's and
        # the CLI reloads it live) so a REMOTE session enforces the
        # SAME permissions.deny as the local sandbox (the claude.ai
        # Cron/Trigger/Push/Artifact/integration tools; Skill is ALLOWED since 2026-07 —
        # platform-managed skills activate through it). Omitted → no deny block.
        **(
            {"permissions": {"deny": list(disallowed_tools)}}
            if disallowed_tools else {}
        ),
        # A terminal's status line report of its account's windows (mirrors
        # the proxy builder, which carries the rationale). Only when the
        # platform shipped the script: an older platform has no route for it.
        **(
            {"statusLine": {"type": "command", "command": hook_command(usage_script), "padding": 0}}
            if usage_script.is_file() else {}
        ),
        "hooks": {
            "PreToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": gate,
                    "timeout": config.HOOK_WAIT_S,
                    # Claude Code 2.1.295+: a gate that cannot start, times
                    # out or exits non-zero blocks the call (the proxy's
                    # builder writes the same; older CLIs ignore the key).
                    # The gate alone, never the trackers.
                    "onFailure": "block",
                }],
            }],
            **{event: [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": forwarder,
                    "timeout": 10,
                }],
            }] for event in TOOL_RESULT_EVENTS},
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
            # Turn end (satellite 0.5.121; the same four events as the local
            # sandbox — proxy docs/architecture/HOOKS.md): the interactive
            # TUI's transcript pointer and the platform's turn_end verdict,
            # which may hold the turn open. The week-long timeout is the
            # gate's; the proxy bounds the wait. The script reaches
            # /v1/hooks/stop over the loopback tunnel (allowlisted both sides).
            "Stop": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": stop,
                    "timeout": config.HOOK_WAIT_S,
                }],
            }],
        }
    }
    if start_parts > 0:
        # A terminal's start asks the proxy for the start's riding part
        # (proxy core/session/start_context.py): one entry per part,
        # since Claude Code inlines a hook's context only up to 10,000
        # characters. In a headless session, which gets the start on its
        # first turn, the script answers only a compaction inside a turn:
        # the file is the scope's, so it names the entries whatever kind of
        # session wrote it last.
        settings["hooks"]["SessionStart"] = [{
            "matcher": "",
            "hooks": [{"type": "command", "command": f"{start} {i} {start_parts}", "timeout": 20}
                      for i in range(1, start_parts + 1)],
        }]
    write_if_changed(claude_dir / "settings.json", (json.dumps(settings, indent=2) + "\n").encode("utf-8"))


# A session's prompt file, named as the platform names its own
# (``core/layers/cli/session._prompt_filename``): the shape the never-sync rule
# on both sides already matches (``_CLAUDE_HOST_LOCAL_RE``). One per session,
# so a start never replaces the file another session of the scope reads.
_PROMPT_STEM = "system-prompt"
_PROMPT_GLOB = f"{_PROMPT_STEM}-{'[0-9a-f]' * 12}.json"
# The one file every session of a scope shared before 0.5.171.
_SHARED_PROMPT_FILENAME = "system-prompt.md"
_PROMPT_SWEEP_S = 24 * 3600


def rewrite_cli_hooks(session) -> None:
    """A live session's settings again, as its start wrote them, after a
    push of the store's file (``SessionManager._reapply_hooks_after_push``):
    in the folder the engine reads (its home when it has one), naming the
    store's scripts."""
    cfg = getattr(session, "config", {})
    claude_dir = session._claude_dir
    _write_cli_hooks(getattr(session, "_engine_dir", None) or claude_dir, cfg.get("disallowed_tools"),
                     int(cfg.get("start_context_parts") or 0), scripts_dir=claude_dir)


def prompt_filename(session_id: str) -> str:
    return f"{_PROMPT_STEM}-{hashlib.sha256(session_id.encode()).hexdigest()[:12]}.json"


def write_prompt_file(claude_dir: Path, session_id: str, content: str) -> Path:
    """Write the session's own prompt file. The shared file an earlier
    version wrote goes, and prompt files older than a day are swept (a
    close removes its own; a session that died with the machine leaves one)."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    shared = claude_dir / _SHARED_PROMPT_FILENAME
    if shared.is_file() or shared.is_symlink():
        shared.unlink(missing_ok=True)
    cutoff = time.time() - _PROMPT_SWEEP_S
    for stale in claude_dir.glob(_PROMPT_GLOB):
        try:
            if stale.is_file() and stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass
    path = claude_dir / prompt_filename(session_id)
    path.write_text(content)
    return path


def remove_prompt_file(claude_dir: Path | None, session_id: str) -> None:
    if claude_dir is None:
        return
    with contextlib.suppress(OSError):
        (claude_dir / prompt_filename(session_id)).unlink(missing_ok=True)


def _write_hook_scripts(claude_dir: Path, scripts: dict[str, str]) -> None:
    """Write hook scripts sent by the proxy into the .claude/ dir.

    Executable, and refreshed on every session start so satellites always
    run the current proxy version of each hook: an unchanged script is left
    alone, a changed one replaced whole (``config.write_if_changed``; a
    sibling session may launch a hook from this dir at that moment). Hooks
    run via ``"python.exe" "<path>"`` on all platforms, so the exec bit only
    serves Unix shebang dispatch.
    """
    if not scripts:
        return
    for filename, content in scripts.items():
        if not content:
            continue
        write_if_changed(claude_dir / filename, content.encode("utf-8"), 0o755)


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
        # The folder the engine reads: the session's home in ``_claude_dir``
        # when the start asks for one (``session_homes``), else that dir.
        self._engine_dir: Path | None = None
        # Set by SessionManager when the proxy sends `stop_turn` — ends
        # the current send_message iteration cleanly.
        self.stop_turn_event: asyncio.Event = asyncio.Event()
        # One reader per stdout: a turn's loop holds this until it exits.
        self._reader_lock: asyncio.Lock = asyncio.Lock()
        # request id -> the input of an open ``can_use_tool`` request (the
        # CLI's own copy, for the allow that answers it).
        self._prompt_inputs: dict[str, dict] = {}
        # Post-turn background-command drain: when the proxy's stop_turn carries
        # drain_bg (a bg command still pending), keep reading + forwarding stdout
        # AFTER the turn so the command's completion frame (claude emits it idle,
        # post-turn) reaches the proxy. Cancelled when the next turn starts (it
        # reclaims stdout — single reader).
        self._drain_bg: bool = False
        # How long the drain forwards: the proxy's background-work ceiling
        # (``drain_bg_s``, a 1.9.1 proxy); an older proxy never reads the
        # forwarded lines while only sub-agents run, so it keeps ten minutes.
        self._drain_bg_s: float = DEFAULT_BG_DRAIN_S
        self._bg_drain_task: asyncio.Task | None = None
        # True from the prompt's stdin write until the turn's read loop
        # exits: the window a steer may write into (a user frame between
        # turns would start a turn nobody reads).
        self._turn_active: bool = False
        # A turn the engine started by itself is running, as the reader after
        # the turn sees it (from its opening line to its result): a steer may
        # write into it too, since that reader forwards what follows.
        self._engine_turn_open: bool = False
        # The process ended inside the turn that just ran: its exit code and
        # the last line it said (``take_turn_end``).
        self._turn_end: dict = {}
        # Awaited once with the exit code when the process ends with no turn
        # reading it, for an end the session did not make itself (the session
        # manager sets it): nothing else notices before the next message.
        self.on_exit: Callable[[int | None], Awaitable[None]] | None = None
        self._own_end = False
        self._exit_told = False
        self._exit_task: asyncio.Task | None = None
        # A compute session (``sandbox_host``): the platform's files, the
        # platform's sandbox, nothing written, translated or scanned here.
        # On a compute machine with its own copy (``_own_copy``, set at the
        # start) the prepared files are written and the changes scanned.
        self._compute = sandbox_host.is_compute(config)
        self._own_copy = False

    async def _start_compute(self) -> None:
        """A session on a compute machine: the platform wrote every file of
        the session into the shared folder and planned the spawn; this
        machine renders the sandbox from the plan's description and runs
        the plan's argv with the plan's environment. No cwd (bubblewrap
        changes into the sandbox's), no config dir of this machine's."""
        # The session's SSH agent socket before the render: the description
        # names it as a bind source. The plan's environment carries the
        # sandbox's own SSH_AUTH_SOCK and OTO_SSH_KEY_DIR, so they win here.
        from . import credential_exit, ssh_agent
        self._own_copy = await sandbox_host.ready_own_copy(self.config, self.sat_config,
                                                           self.session_id, self.agent_dir)
        if self._own_copy:
            self._file_snapshot = await asyncio.to_thread(file_sync.snapshot_agent_dir, self.agent_dir)
        agent_env = await asyncio.to_thread(ssh_agent.start, self.session_id, self.config)
        await asyncio.to_thread(credential_exit.start, self.session_id, self.config)
        cmd = await sandbox_host.spawn_command(self.config, self.sat_config, "claude")
        env = sandbox_host.spawn_env(self.config)
        ssh_agent.merge_env(env, agent_env, self.config)
        async with start_slots.slot():
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=200 * 1024 * 1024,
            )
        self.pid = self.proc.pid
        logger.info("CLI compute session started: pid=%s, session=%s", self.pid, self.session_id)
        self._stderr_buf: list[bytes] = []
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self.watch_exit()

    async def start(self) -> None:
        """Write config files and spawn CLI subprocess."""
        if self._compute:
            await self._start_compute()
            return
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
        self._engine_dir = session_homes.open_home(
            self._claude_dir, self.session_id, self.config.get("session_home"))
        self._cwd.mkdir(parents=True, exist_ok=True)

        # Defensive cleanup: wipe Claude Code's built-in auto-memory dirs
        # (``.claude/projects/{cwd}/memory/``). The platform's otodock
        # memory-mcp is the single source of memory truth; running both
        # in parallel splits the agent's view. Mirrors the proxy-side
        # cleanup in the proxy's ``core/layers/cli/config_dir.ensure_persistent_claude_dir``. Session JSONLs
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
        write_command_wrappers(self._claude_dir, self.config.get("command_wrappers", {}))

        # Write system prompt
        prompt_file = write_prompt_file(
            self._claude_dir, self.session_id, self.config.get("system_prompt", ""))

        # Write MCP config (expand ~ to actual home dir). Use forward
        # slashes in substituted paths — embedding raw Windows
        # backslashes into JSON produces illegal escape sequences like
        # `\U`, `\d` and Claude Code rejects the file with "MCP config
        # is not a valid JSON". Forward slashes are valid Python paths
        # on Windows and don't need JSON escaping.
        mcp_config = self.config.get("mcp_config")
        # Named per session: two concurrent sessions of one person on one
        # agent never read each other's token; the shared-named copy an
        # earlier satellite wrote is removed.
        mcp_file = self._claude_dir / f"mcp-config-{self.session_id[:12]}.json"
        (self._claude_dir / "mcp-config.json").unlink(missing_ok=True)
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
        _write_cli_hooks(self._engine_dir, self.config.get("disallowed_tools"),
                         int(self.config.get("start_context_parts") or 0), scripts_dir=self._claude_dir)

        # OAuth file delivery: the CLI authenticates from .credentials.json in
        # CLAUDE_CONFIG_DIR (never env — env is frozen at exec, so the pool's
        # rotation fan-out could never reach a live process). Rewritten on
        # rotation by ``credentials_update`` pushes (session_manager).
        creds = self.config.get("credentials_json")
        if creds:
            session_homes.write_sign_in(self._engine_dir, ".credentials.json", creds)

        # Build command. The binary comes from pin-verified resolution, not
        # PATH — see host/cli_versions.py.
        from ..host.cli_versions import resolve_spawn_bin_async
        claude_bin = await resolve_spawn_bin_async(
            "claude", self.sat_config.bin_hint("claude"),
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
        # A 1.9 proxy ships Claude Code's own word for the mode
        # (``cli_permission_mode``): its auto mode for Auto, where a call the
        # hook leaves undecided goes to the CLI's classifier. An older proxy
        # ships the platform word alone.
        permission_mode = self.config.get("permission_mode", "")
        cli_word = self.config.get("cli_permission_mode") or ""
        if self.config.get("use_native_permissions"):
            cmd += ["--permission-mode", cli_word or permission_mode]
        elif permission_mode == "plan":
            # Mirrors the local CLI layer: Claude Code 2.1.299+ asks for a
            # call the hook allowed in plan mode, so the prompt tool carries
            # that ask, and the proxy answers it from the gate's record. It
            # also exposes ExitPlanMode and AskUserQuestion in Plan.
            cmd += ["--permission-mode", "plan", "--permission-prompt-tool", "stdio"]
        else:
            cmd += (["--permission-mode", "auto"] if cli_word == "auto"
                    else ["--dangerously-skip-permissions"])
            # Mirrors the local CLI layer: the prompt-tool's PRESENCE makes
            # the CLI expose AskUserQuestion headless (absent from every -p
            # roster since at least 2.1.193). The PreToolUse hook
            # denies-and-surfaces the question before the tool runs; the CLI
            # still asks over this channel for some calls under bypass (a
            # heredoc writing a file, a wide ``rm -rf``) and, in auto, after
            # its reviewer's decline limit, which the proxy answers like a
            # card or refuses (core/session/engine_prompts). Not added to
            # the native branch (a real prompt could route to stdio, which
            # nothing answers).
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
        # Claude Code ≥ 2.1.267 would otherwise re-send the prompt it recorded
        # on the conversation's first request on every --resume, discarding
        # the fresh file above; and only the platform's settings.json is read
        # (a plugin enabled from a terminal at the local scope lands in
        # settings.local.json). Mirrors the local CLI layer; both flags exist
        # on the previous pin 2.1.281 too.
        cmd += ["--system-prompt-snapshot", "off", "--setting-sources", "user"]
        cmd += ["--output-format", "stream-json"]
        cmd += ["--input-format", "stream-json"]
        cmd += ["--verbose", "--include-partial-messages"]

        if _is_resume:
            cmd += ["--resume", self.config["session_id_for_resume"]]
        else:
            cmd += ["--session-id", self.session_id]

        # Build env. Curate the operator's AMBIENT env (drop their personal shell
        # secrets) BEFORE overlaying the platform-shipped config["env"] (which
        # carries the intentional PROXY_API_KEY / ANTHROPIC_API_KEY
        # and survives curation). Defense-in-depth — see env_hygiene.py.
        env = env_hygiene.curate_satellite_env(os.environ)
        env.update(self.config.get("env", {}))
        env["CLAUDE_CONFIG_DIR"] = str(self._engine_dir)
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
        # The advisor tool is a model call the platform can neither meter nor
        # show. Mirrors proxy env_builder.
        env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] = "1"
        # Tools that really start connect together, not three at a time.
        # Mirrors proxy env_builder.
        env["MCP_SERVER_CONNECTION_BATCH_SIZE"] = str(min(16, max(3, 2 * (os.cpu_count() or 1))))
        env.pop("CLAUDECODE", None)
        # Force UTF-8 IO — Windows console default is cp1252 and breaks
        # tool-result JSON parse on non-ASCII content.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")

        # The session's SSH agent socket and public keys (the proxy signs),
        # and the credential exit's door (the payload's ``exit`` block).
        from . import credential_exit, ssh_agent
        ssh_agent.merge_env(
            env, await asyncio.to_thread(ssh_agent.start, self.session_id, self.config),
            self.config,
        )
        await asyncio.to_thread(credential_exit.start, self.session_id, self.config)

        # Translate sandbox-style virtual paths (e.g. /users/{u}/workspace,
        # /workspace, /.claude) to satellite-absolute paths. The proxy ships
        # sandbox-style values for cross-target uniformity; the satellite has
        # no bwrap so it has to do the mapping that bwrap would. Also expands
        # the literal {session_id} token wherever a path value contains it (no
        # built-in role emits it now). The ``multi_value_envs`` map (built proxy-side
        # from manifest path_env decls + OTO_ALLOWED_ROOTS) tells the
        # translator which env vars are separator-joined sandbox-path lists
        # so it can split-translate-rejoin them. See path_translator.py.
        username = layout.user_of(
            self.config.get("cwd_relative", ""),
        )
        env = path_translator.translate_env(
            env,
            agent_dir=self.agent_dir,
            username=username,
            session_id=self.session_id,
            multi_value_envs=self.config.get("multi_value_envs") or {},
        )
        env = session_tools_env(env, self._claude_dir)

        # Spawn. Bump StreamReader buffer to 10MB (default is 64KB) so the
        # CLI's `--output-format stream-json` lines don't get truncated. A
        # single MCP tool-result line can blow past 64KB easily — e.g.
        # unifi-network's `list_devices` or `get_dashboard` JSON for a
        # network with 30+ clients. Hitting the default limit raises
        # asyncio.LimitOverrunError and silently kills the stream reader,
        # ending the pump mid-turn with no `done` ever reaching the
        # dashboard (UI freezes; nothing persisted to DB).
        async with start_slots.slot():
            self.proc = await asyncio.create_subprocess_exec(
                *sandbox_host.paired_command(cmd),
                cwd=str(self._cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=200 * 1024 * 1024,
            )
        self.pid = self.proc.pid
        logger.info("CLI session started: pid=%s, session=%s", self.pid, self.session_id)

        # Drain stderr in the background so a crashed CLI's error is
        # surfaced to the satellite log instead of vanishing into a
        # full pipe buffer. Cap collected bytes at ~64KB so a chatty
        # process doesn't balloon memory.
        self._stderr_buf: list[bytes] = []
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self.watch_exit()

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
        start_context: str = "",
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

        if inject_time:
            prompt = f"[Current time: {_format_time()}]\n{prompt}"

        # Translate sandbox-virtual paths (`/users/{u}/...`, `/workspace/...`)
        # the proxy injects for chat-attached photos and files into satellite-
        # absolute paths. Mirrors `translate_env` for env vars; without this
        # the CLI subprocess would try to read sandbox paths literally and
        # fail (no bwrap on satellite). See proxy/ws/dashboard.py::_handle_chat.
        if not self._compute:
            username = layout.user_of(
                self.config.get("cwd_relative", "")
            )
            prompt = path_translator.translate_paths_in_text(
                prompt, agent_dir=self.agent_dir, username=username,
            )
            if start_context:
                start_context = path_translator.translate_paths_in_text(
                    start_context, agent_dir=self.agent_dir, username=username,
                )

        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prompt},
        })
        if start_context:
            # The start's data as its own message, marked as not typed by a
            # person: the CLI stores it as a meta message, its terminal never
            # draws it, and shouldQuery false runs no model call for it (its
            # empty result is one the proxy's turn loop skips).
            msg = json.dumps({
                "type": "user", "message": {"role": "user", "content": start_context},
                "isSynthetic": True, "shouldQuery": False,
            }) + "\n" + msg

        # A new turn reclaims stdout — stop any post-turn bg-command drainer
        # first (single reader on the CLI's stdout).
        await self.cancel_bg_drain()
        # A previous turn's loop still reading (its stop_turn was lost, or
        # arrives after this send) is told to stop and waited for BEFORE the
        # stop flag is cleared: two loops on one stdout raise readuntil().
        if self._reader_lock.locked():
            self.stop_turn_event.set()
        try:
            await asyncio.wait_for(self._reader_lock.acquire(), timeout=_READER_HANDOFF_S)
        except asyncio.TimeoutError:
            raise RuntimeError("the previous turn is still reading this session") from None
        # Clear any residual stop_turn signal from a previous turn
        self.stop_turn_event.clear()
        self._drain_bg = False
        self._engine_turn_open = False
        try:
            # The previous turn's handler runs on, without yielding, when its
            # reader lets go: a drainer it started meanwhile stops here.
            await self.cancel_bg_drain()
            self.proc.stdin.write((msg + "\n").encode())
            await self.proc.stdin.drain()
        except BaseException as exc:
            self._reader_lock.release()
            if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
                await asyncio.sleep(0.2)  # the stderr drain reads what the process left
                self._log_early_exit()
            raise

        # Read every line until stop_turn or EOF. No filtering.
        self._turn_active = True
        self._turn_end = {}
        try:
            async for line in self._read_stdout_until_stop():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._note_control_request(event)
                yield event
        finally:
            self._turn_active = False
            self._reader_lock.release()

    async def steer(self, text: str) -> bool:
        """Write a user frame into the RUNNING turn's stdin — the satellite
        half of the proxy's ``PersistentSession.steer``: the CLI takes it at
        its next tool boundary, and a frame the turn does not consume becomes
        the CLI's next turn, which the proxy reads through. True only when
        the frame reached the pipe (the proxy queues the message otherwise).
        The proxy owns the result-seen guard — it alone can tell a foreign
        result from the driven one — so this side refuses only without a live
        turn or a live process. A turn the engine started by itself counts
        while the reader after the turn forwards it (``_engine_turn_open``)."""
        if (self.proc is None or self.proc.returncode is not None
                or self.proc.stdin is None or not text
                or not (self._turn_active or self._engine_turn_open)):
            return False
        if not self._compute:
            username = layout.user_of(self.config.get("cwd_relative", ""))
            text = path_translator.translate_paths_in_text(
                text, agent_dir=self.agent_dir, username=username,
            )
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": text},
        })
        try:
            self.proc.stdin.write((msg + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError):
            return False
        logger.info(
            "CLI session %s: steered the live turn (%d chars)",
            self.session_id[:8], len(text),
        )
        return True

    async def run_turn(self, prompt: str, *, inject_time: bool = False, forward,
                       start_context: str = "") -> None:
        """Drive ONE turn: write the prompt, hand every raw NDJSON event to
        ``await forward(event)`` until the proxy's ``stop_turn`` or EOF. The
        session manager's ``forward`` tags, retains (Mode C) and ships the
        event; the same entry point every engine's session exposes."""
        async for event in self.send_message(prompt, inject_time=inject_time,
                                             start_context=start_context):
            await forward(event)

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
        except (asyncio.CancelledError, Exception):  # noqa: BLE001  # the stderr drain task's top: nobody awaits it
            return

    async def _read_stdout_until_stop(self):
        """Yield decoded stdout lines until ``stop_turn`` fires or the
        process is gone (EOF, or its exit code is known).

        Uses a short timeout on each readline so we can periodically check
        the stop_turn event without missing quick succession of lines. A
        process that exited is read to the end of what it printed first:
        its last lines often say why (an error result).
        """
        assert self.proc and self.proc.stdout
        while True:
            if self.stop_turn_event.is_set():
                return
            exited = self.proc.returncode is not None
            try:
                line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=_EXIT_DRAIN_S if exited else 1.0,
                )
            except asyncio.TimeoutError:
                if exited:
                    break  # a child of the process holds the pipe open
                continue
            if not line:
                break
            decoded = line.decode(errors="replace").strip()
            if decoded:
                yield decoded
        await self._note_exit()

    def watch_exit(self) -> None:
        """Watch the process that just started for its end."""
        assert self.proc is not None
        self._exit_task = asyncio.create_task(self._watch_exit(self.proc))

    async def _watch_exit(self, proc: asyncio.subprocess.Process) -> None:
        """Tell an end of the process that no turn's end says (``on_exit``,
        once). A turn in flight ends first: its reader may have read the
        exit, and then the turn's end says it (``_note_exit``). An end
        ``abort`` or ``close`` made is the session's own."""
        code = await proc.wait()
        async with self._reader_lock:
            pass
        if self._own_end or self._exit_told or self.on_exit is None:
            return
        self._exit_told = True
        logger.warning("CLI session %s: the process ended with no turn reading it (returncode=%s); "
                       "stderr tail: %s", self.session_id[:8], code, self._stderr_tail()[-1500:] or "(empty)")
        await self.on_exit(code if isinstance(code, int) else None)

    async def _note_exit(self) -> None:
        """The process ended inside a turn: what the turn's end says of it
        (``take_turn_end``), and one log line with its stderr tail."""
        assert self.proc is not None
        self._exit_told = True
        if self.proc.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), _EXIT_CODE_WAIT_S)
        await asyncio.sleep(0.2)  # the stderr drain reads what the process left
        self._log_early_exit()
        code = self.proc.returncode
        said = [ln.strip() for ln in self._stderr_tail().splitlines() if ln.strip()]
        self._turn_end = {"reason": "exited",
                          **({"exit_code": code} if isinstance(code, int) else {}),
                          **({"last_line": said[-1][:_LAST_LINE_MAX]} if said else {})}

    def take_turn_end(self) -> dict:
        """What the turn that just ended adds to its end frame, once: the
        engine's exit inside it, with its exit code and the last line it said."""
        ended, self._turn_end = self._turn_end, {}
        return ended

    def _stderr_tail(self) -> str:
        return b"".join(getattr(self, "_stderr_buf", []) or []).decode("utf-8", errors="replace").strip()

    def _log_early_exit(self) -> None:
        """The process ended inside a turn, or before its prompt reached it:
        its stderr tail is the only account of why (a compute session's
        render, a refused flag)."""
        logger.warning("CLI session %s: the process ended inside a turn (returncode=%s); stderr tail: %s",
                       self.session_id[:8], self.proc.returncode if self.proc else None,
                       self._stderr_tail()[-1500:] or "(empty)")

    def request_stop_turn(self, drain_bg: bool = False,
                          drain_bg_s: float | None = None, *,
                          stream: bool = False) -> None:
        """Signal the current send_message() loop to exit gracefully. ``drain_bg``
        (set by the proxy when background work is still pending) arms a
        post-turn stdout drainer once this turn's loop exits, forwarding for
        ``drain_bg_s`` seconds (ten minutes when the proxy sends none).
        ``stream`` (a platform that reads between turns asks for it) forwards
        with no time bound: until the next turn takes stdout back, the process
        ends or the session closes."""
        self._drain_bg = drain_bg or stream
        if stream:
            self._drain_bg_s = math.inf
        else:
            self._drain_bg_s = (float(drain_bg_s) if isinstance(drain_bg_s, (int, float))
                                and drain_bg_s > 0 else DEFAULT_BG_DRAIN_S)
        self.stop_turn_event.set()

    def start_bg_drain(self, forward) -> None:
        """If the last stop_turn requested it (``drain_bg``), start a post-turn
        stdout drainer that forwards each NDJSON event via ``await forward(ev)``
        until the next turn cancels it (or EOF / the proxy's bound). This is how
        a backgrounded command's completion frame and a background sub-agent's
        own lines — claude emits them idle, after the turn — reach the proxy's
        monitors on the remote path."""
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
        bound. No idle exit — a sleeping bg command emits nothing for a while,
        then its task_updated{completed}. A process that exited is read to
        the end of what it printed first, as the turn's reader does."""
        if self.proc is None or self.proc.stdout is None:
            return
        deadline = asyncio.get_running_loop().time() + self._drain_bg_s
        try:
            while asyncio.get_running_loop().time() < deadline:
                exited = self.proc.returncode is not None
                try:
                    line = await asyncio.wait_for(
                        self.proc.stdout.readline(), timeout=_EXIT_DRAIN_S if exited else 1.0,
                    )
                except asyncio.TimeoutError:
                    if exited:
                        return  # a child of the process holds the pipe open
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
                self._note_control_request(event)
                self._note_engine_turn(event)
                await forward(event)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("bg-command post-turn drain failed")

    def _note_engine_turn(self, event: dict) -> None:
        """A line the reader after the turn forwards: a main-thread opening
        line (its ``init``, or its first message where a CLI sends no
        ``init``) opens a turn the engine started by itself, and its result
        closes it. A sub-agent's lines carry ``parent_tool_use_id``."""
        if event.get("parent_tool_use_id"):
            return
        kind = event.get("type")
        if kind == "result":
            self._engine_turn_open = False
        elif kind in ("stream_event", "assistant") or (
                kind == "system" and event.get("subtype") == "init"):
            self._engine_turn_open = True

    async def abort(self) -> None:
        """SIGINT (Unix) / terminate (Windows), then tree-kill on timeout.

        SIGINT lets Claude Code flush in-progress output on Unix; Windows
        has no equivalent so we go straight to terminate. The tree-kill
        backstop catches ``cmd.exe → node.exe`` grandchildren on Windows
        and MCP forks on Unix.
        """
        if not (self.proc and self.proc.returncode is None):
            return
        self._own_end = True
        pid = self.proc.pid
        try:
            if not config.HOST.posix:
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
        logger.info("CLI session aborted: %s", self.session_id)

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
        self._own_end = True
        from . import credential_exit, session_files, ssh_agent
        try:
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
                logger.info("CLI session closed: %s", self.session_id)
            # The session's SSH agent socket and public keys die with the session.
            await asyncio.to_thread(ssh_agent.stop, self.session_id)
            credential_exit.stop(self.session_id)
        finally:
            # Whatever a step above raised: the home with its sign-in and the
            # session's files go here or nowhere while this satellite runs.
            if not self._compute:
                remove_prompt_file(self._claude_dir, self.session_id)
                session_homes.close_home(self._claude_dir, self.session_id, self.config.get("session_home"))
                # Per-session secret files (SSH keys) die with the session.
                session_files.wipe(self.session_id)
            elif self._own_copy:
                # The prompt file is the platform's (its close removes it);
                # a machine with its own copy wrote the prepared files.
                await asyncio.to_thread(session_files.wipe_agent_files, self.session_id)
                session_homes.close_home(self.agent_dir / self.config.get("claude_dir_relative", ""),
                                         self.session_id, self.config.get("session_home"))

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

    def _note_control_request(self, event: dict) -> None:
        """Remember the input of a ``can_use_tool`` request the CLI printed,
        for the allow that answers it (bounded: the oldest go first)."""
        if event.get("type") != "control_request":
            return
        request = event.get("request") or {}
        rid = str(event.get("request_id") or "")
        if not rid or not isinstance(request, dict) or request.get("subtype") != "can_use_tool":
            return
        tool_input = request.get("input")
        if isinstance(tool_input, dict):
            self._prompt_inputs[rid] = tool_input
            while len(self._prompt_inputs) > _PROMPT_INPUTS_MAX:
                self._prompt_inputs.pop(next(iter(self._prompt_inputs)))

    async def send_permission_response(
        self, request_id: str, approved: bool, response: dict | None = None,
    ) -> None:
        """Answer a ``can_use_tool`` request on the CLI's control channel in
        the shape it parses (the proxy's ``engine_prompts.answer_frame``):
        ``response.response`` carries ``behavior``, an allow the request's
        input, a deny a message. The decision is the proxy's ``response``
        (its ``behavior`` and a deny's ``message``) when it names this
        request, else ``approved`` (a proxy before 1.8.0 sends only that).
        An allow carries the input the CLI printed, never a copy that crossed
        the wire twice (a changed input is refused), or none when it is not
        known (the CLI keeps its own); nothing else the frame holds reaches
        the CLI."""
        if self.proc is None or self.proc.returncode is not None:
            return
        decision: dict = {}
        if isinstance(response, dict) and str(response.get("request_id") or "") == request_id:
            inner = response.get("response")
            if isinstance(inner, dict) and inner.get("behavior") in ("allow", "deny"):
                decision = inner
        allow = decision.get("behavior") == "allow" if decision else bool(approved)
        tool_input = self._prompt_inputs.pop(request_id, None)
        if allow:
            answer: dict = {"behavior": "allow"}
            if tool_input is not None:
                answer["updatedInput"] = tool_input
            # The platform's own additions to an allow (a plan's exit carries
            # the mode the person chose as ``updatedPermissions``): passed on
            # as they are, the only other key an allow may carry.
            extra = decision.get("updatedPermissions")
            if isinstance(extra, list):
                answer["updatedPermissions"] = extra
        else:
            answer = {"behavior": "deny",
                      "message": str(decision.get("message") or "The request was not approved.")}
        msg = json.dumps({
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": answer},
        })
        self.proc.stdin.write((msg + "\n").encode())
        await self.proc.stdin.drain()

    def detect_file_changes(self) -> list[dict]:
        """Compare current files against session-start snapshot. A compute
        session beside the platform writes its folder itself: nothing to
        report."""
        if self._compute and not self._own_copy:
            return []
        return file_sync.detect_changes(self.agent_dir, self._file_snapshot)

    @property
    def mcp_server_names(self) -> list[str]:
        """The MCP server keys of the config this session was started with
        (the connect report carries them, so an adoption knows its tools)."""
        mcp_config = self.config.get("mcp_config")
        servers = mcp_config.get("mcpServers") if isinstance(mcp_config, dict) else None
        return sorted(servers) if isinstance(servers, dict) else []

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None
