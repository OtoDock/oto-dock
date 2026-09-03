"""Session manager for satellite daemon.

Dispatches platform commands to CLI or Codex sessions based on execution_path.
Manages session registry and lifecycle.
"""

import asyncio
import base64
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Union

from ..transport import file_sync
from .cli_session import CLISession, _write_cli_hooks
from .codex_session import CodexSession, _write_codex_hooks
from ..config import atomic_replace, force_rmtree, kill_process_tree
from ..host.host_probe import (
    _detect_display,
    _detect_os_user_and_home,
    _detect_user_dirs,
    _interactive_pty_supported,
    _machine_resources,
    _recommended_max_sessions,
)
from .mcp_install_support import (
    _fix_shebangs_after_swap,
    _fix_windows_exe_paths_after_swap,
    _force_remove_or_quarantine,
    _is_transient_lock,
    _scan_installed_mcps,
    _uv_bin_if_present,
    _warm_one_mcp,
)
import contextlib


if TYPE_CHECKING:
    from ..config import SatelliteConfig
    from ..transport.ws_client import SatelliteWSClient

logger = logging.getLogger("satellite")

# Category directories the platform ships MCPs under (``mcps/<category>/<name>``
# on both sides). ``sync_mcps`` takes the category from the wire spec and uses
# it as a path component, so it is allow-listed rather than slug-checked: an
# unknown value must never create a fresh top-level directory under mcps_dir.
# Every platform MCP is ``core``; ``custom`` and ``community`` are the
# operator-installed and catalog trees.
MCP_CATEGORIES = frozenset({"core", "custom", "community"})


def _validate_satellite_host_path(raw: str) -> None:
    """Defensive validation before writing/reading a satellite-host path.

    Structural checks only — absolute form, no NUL, no `..`. The
    semantic policy check (home-only vs full-FS) lives in
    `_check_satellite_host_policy` and runs in addition to this one.
    """
    if not raw:
        raise ValueError("empty satellite-host path")
    if "\x00" in raw:
        raise ValueError("path contains NUL byte")
    # Permit absolute Unix paths and Windows drive-rooted paths (forward
    # or backward slash variants).
    is_unix_abs = raw.startswith("/")
    is_win_abs = (
        len(raw) >= 3 and raw[1] == ":" and raw[2] in ("/", "\\")
    )
    if not (is_unix_abs or is_win_abs):
        raise ValueError(f"satellite-host path must be absolute: {raw!r}")
    # Reject ``..`` segments (post-normalize they'd resolve to a
    # different path than the policy admitted).
    normalized = raw.replace("\\", "/")
    if any(seg == ".." for seg in normalized.split("/")):
        raise ValueError(f"satellite-host path contains '..': {raw!r}")


def _claude_runtime_root() -> str:
    """The Claude Code CLI's per-user runtime root on this machine.

    POSIX: ``<tempdir>/claude-<uid>`` (the CLI namespaces by uid there);
    Windows: ``<tempdir>/claude`` (temp already sits under the user
    profile). gettempdir() honors the same TMPDIR/TEMP env the spawned
    CLI inherits, so both sides agree on the root. Forward-slash form.
    """
    base = Path(tempfile.gettempdir())
    leaf = f"claude-{os.getuid()}" if hasattr(os, "getuid") else "claude"
    return str(base / leaf).replace("\\", "/")


def _is_claude_runtime_path(raw: str) -> bool:
    """True when ``raw`` lies inside THIS machine's own Claude-CLI runtime
    root — with the tightenings the proxy's lexical check cannot do:

    * realpath re-check: the RESOLVED path must still sit under the
      resolved root (``/tmp`` is sticky/world-writable, so a symlink
      planted inside the tree must not escape it — unlike the home band,
      which is user-owned);
    * ownership: when the root exists it must belong to this uid and not
      be writable by others (anti-squatting by another local user).

    Deliberately NOT bound to live session ids: media pulls of scratchpad
    artifacts legitimately arrive after the session is reaped, and the
    proxy (the policy authority) already scoped the path to the session's
    own tree.
    """
    root = _claude_runtime_root()
    is_ci = platform.system().lower() in ("windows", "darwin")

    def _under(child: str, parent: str) -> bool:
        c, p = child.replace("\\", "/"), parent.replace("\\", "/")
        if is_ci:
            c, p = c.lower(), p.lower()
        c, p = c.rstrip("/"), p.rstrip("/")
        return c == p or c.startswith(p + "/")

    if not _under(raw, root):
        return False
    try:
        real = os.path.realpath(raw)
        real_root = os.path.realpath(root)
        if not _under(real, real_root):
            return False
        if os.path.isdir(real_root) and hasattr(os, "getuid"):
            st = os.stat(real_root)
            if st.st_uid != os.getuid() or (st.st_mode & 0o002):
                return False
    except OSError:
        return False
    return True


def _check_satellite_host_policy(raw: str) -> None:
    """Local policy re-check before a satellite-host
    write or read. Defense in depth: the proxy already policy-gated the
    call via ``path_policy_v2``, but a compromised proxy could lie. We
    independently verify against the satellite's own cached
    ``allow_full_fs`` flag (received at WS auth or via ``policy_update``).

    When ``allow_full_fs`` is True: admit anything that passes structural
    validation (proxy is trusted on path scope when the toggle was
    explicitly enabled).

    When ``allow_full_fs`` is False: require the path to be under the OS
    user's home directory, OR inside this machine's own Claude-CLI
    runtime root (the proxy scopes that admission to the session's own
    tree; see ``_is_claude_runtime_path`` for the local tightenings).
    Anything else gets rejected with a clear error so the proxy operator
    sees the boundary they tried to cross.

    Raises ``ValueError`` on policy violation.
    """
    from ..host.satellite_policy import is_full_fs_allowed
    if is_full_fs_allowed():
        return
    if _is_claude_runtime_path(raw):
        return
    home_dir = str(Path.home().resolve()).replace("\\", "/")
    if not home_dir:
        raise ValueError(
            "satellite cannot determine its OS home directory — "
            "rejecting satellite-host I/O until allow_full_fs is enabled"
        )
    # Resolve symlinks BEFORE the home comparison — a symlink under $HOME
    # pointing outside would otherwise pass this lexical check while the
    # actual I/O lands outside home (the exact escape this band exists to
    # block). realpath keeps non-existent tails, so pushes that CREATE a
    # new file still validate against the real parent chain.
    normalized = os.path.realpath(raw).replace("\\", "/")
    # Case-fold for Windows / macOS-HFS+ (matches the proxy's
    # `_normalize_for_compare`).
    is_case_insensitive = (
        platform.system().lower() in ("windows", "darwin")
    )
    if is_case_insensitive:
        c = normalized.lower().rstrip("/")
        p = home_dir.lower().rstrip("/")
    else:
        c = normalized.rstrip("/")
        p = home_dir.rstrip("/")
    if c == p or c.startswith(p + "/"):
        return
    raise ValueError(
        f"satellite-host path {raw!r} is outside the OS user's home "
        f"directory ({home_dir}) and allow_full_fs is disabled — "
        "ask an admin to enable full-FS access for this machine if "
        "broader scope is needed"
    )


async def _apply_satellite_host_push(
    msg: dict, ws: "SatelliteWSClient | None",
) -> None:
    """Apply a file_push targeting an absolute satellite-host path.

    Mirrors ``file_sync.apply_file_push`` semantics (delete / write /
    write_chunk) but writes to the OS filesystem at the absolute path
    instead of joining under the agent tree. Chunked writes use a
    sibling ``.partial`` staging file and atomic ``os.replace``.
    """
    import base64
    import hashlib

    abs_path = msg.get("path", "")
    action = msg.get("action", "write")
    command_id = msg.get("command_id", "")
    error_msg = ""

    try:
        target = Path(abs_path)
        if action == "delete":
            target.unlink(missing_ok=True)
        elif action == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            content = base64.b64decode(msg.get("content_b64", ""))
            # Optional integrity check.
            expected_hash = msg.get("hash", "")
            if expected_hash:
                actual = "sha256:" + hashlib.sha256(content).hexdigest()
                if actual != expected_hash:
                    raise ValueError(
                        f"hash mismatch: got {actual} expected {expected_hash}"
                    )
            partial = target.with_suffix(target.suffix + ".partial")
            partial.write_bytes(content)
            os.replace(partial, target)
        elif action == "write_chunk":
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".partial")
            chunk = base64.b64decode(msg.get("content_b64", ""))
            chunk_idx = msg.get("chunk_index", 0)
            mode = "wb" if chunk_idx == 0 else "ab"
            with open(partial, mode) as f:
                f.write(chunk)
            total = msg.get("total_chunks", 0)
            if chunk_idx + 1 >= total:
                # Last chunk — verify hash and finalize.
                expected_hash = msg.get("hash", "")
                if expected_hash:
                    with open(partial, "rb") as f:
                        actual = "sha256:" + hashlib.sha256(f.read()).hexdigest()
                    if actual != expected_hash:
                        partial.unlink(missing_ok=True)
                        raise ValueError(
                            f"chunked hash mismatch: got {actual} expected {expected_hash}"
                        )
                os.replace(partial, target)
        else:
            raise ValueError(f"unknown action: {action!r}")
    except (OSError, ValueError) as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.warning(
            "_apply_satellite_host_push %s on %r: %s",
            action, abs_path, error_msg,
        )

    if command_id and ws is not None:
        await ws.enqueue_send({
            "type": "ack",
            "command_id": command_id,
            "status": "error" if error_msg else "ok",
            "error": error_msg,
        })


class SessionManager:
    """Routes commands to CLI or Codex sessions."""

    def __init__(self, config: "SatelliteConfig"):
        self.config = config
        self.sessions: dict[str, Union[CLISession, CodexSession]] = {}
        # Interactive PTY sessions — a separate lifecycle
        # from the -p `sessions` above (no send_message/stop_turn; driven by
        # pty_input/pty_resize/pty_close from the proxy). session_id -> PtySession.
        self.pty_sessions: dict = {}
        # otodock-CLI: local control-socket clients (`otodock` terminals on this
        # machine) awaiting/attached to a PTY session, keyed by session_id. Duck-typed
        # (local_socket._LocalConn) to avoid an import cycle. Wired to the PTY's
        # output/exit tee + input/resize once both exist (either ordering).
        self._otodock_conns: dict = {}
        # Set by __main__.py after LocalTunnelServer.start(). Reported in
        # capabilities so the platform can rewrite PROXY_URL to
        # http://127.0.0.1:<port> for satellite subprocesses.
        self.local_tunnel_port: int = 0
        # Proxy-restart re-attach (Mode C): per-CLI-session retention of the
        # CURRENT turn's forwarded events, replayable to a restarted proxy.
        # turn_state carries {command_id, active, seq, start_seq}; the buffer
        # holds the seq/command-tagged event dicts (deque caps memory — a
        # dropped head is reported as `truncated` on replay). forward_paused
        # parks the live forwarder while a replay is enqueued so the wire
        # order is strictly [begin][replay][live].
        self.turn_buffers: dict[str, deque] = {}
        self.turn_state: dict[str, dict] = {}
        self.forward_paused: set[str] = set()

    def _live_session_count(self, exclude: str = "") -> int:
        """Live agent sessions on this satellite: headless (-p/codex) + interactive
        PTY (which also holds native-CLI/otodock sessions). ``_otodock_conns`` is a
        relay-attachment map (not a session) so it is NOT counted. ``exclude`` drops
        a session_id being re-opened so a re-adopt never false-rejects."""
        n = len(self.sessions) + len(self.pty_sessions)
        if exclude and (exclude in self.sessions or exclude in self.pty_sessions):
            n -= 1
        return n

    def _check_capacity(self, session_id: str = "") -> None:
        """Raise if this satellite is at its session ceiling — the physical-safety
        hard cap, enforced for BOTH proxy-commanded and satellite-initiated spawns."""
        cap = _recommended_max_sessions()
        live = self._live_session_count(exclude=session_id)
        if live >= cap:
            raise RuntimeError(f"machine at capacity ({live}/{cap} active sessions)")

    def detect_capabilities(self) -> dict:
        """Detect installed CLIs, MCPs, and system info."""
        os_user, home_dir = _detect_os_user_and_home()
        user_dirs = _detect_user_dirs(home_dir)
        caps = {
            "os": platform.system().lower(),
            "arch": platform.machine(),
            # Absolute path to the satellite's agents root. The proxy uses
            # this to set sandbox-equivalent path env vars (IMAGE_SAVE_DIR,
            # credential_dir vars) on remote sessions and to strip the
            # prefix when MCPs send absolute paths back via /v1/hooks/*.
            "agents_dir": str(self.config.agents_dir.resolve()),
            # Local tunnel server port (HTTP-over-WS). The platform reads
            # this and uses it as PROXY_URL when injecting env for new
            # subprocesses on this machine. 0 = tunnel not started (e.g.
            # tests).
            "local_tunnel_port": self.local_tunnel_port,
            # OS user identity + home dir + well-known folders.
            # Drives the path-policy framework's home-directory admission
            # rules and populates the agent prompt's # Execution
            # Environment block with shortcut paths. Forward-slash form
            # on all platforms (including Windows) for consistent prefix
            # matching across the wire.
            "os_user": os_user,
            "home_dir": home_dir,
            "user_dirs": user_dirs,
            # Device-local MCP gate: whether this
            # machine has an interactive GUI session. The proxy reads
            # display.has_display to filter requires_display device MCPs
            # (computer / browser control) off headless satellites.
            "display": _detect_display(),
            # The Claude Code CLI's per-user runtime root on THIS machine
            # (scratchpad, background-task outputs live under
            # <root>/<cwd-slug>/<session-id>/). Reported concretely so the
            # proxy's path policy can admit a session's OWN runtime tree
            # without structural guessing (uid is unknown proxy-side).
            # gettempdir() follows the same env the spawned CLI inherits
            # (env curation never touches TMPDIR/TEMP). Forward-slash form.
            "claude_runtime_root": _claude_runtime_root(),
        }

        # Probe through the same pin-verified resolution spawns use — the
        # configured hint alone can point at a binary spawns no longer pick.
        # Sync variant only: this runs on the event loop during auth.
        from ..host.cli_versions import resolve_spawn_bin
        installed_clis = []
        if shutil.which(resolve_spawn_bin("claude", self.config.claude_bin)):
            installed_clis.append("claude-code")
        if shutil.which(resolve_spawn_bin("codex", self.config.codex_bin)):
            installed_clis.append("codex")
        caps["installed_clis"] = installed_clis
        caps["installed_mcps"] = _scan_installed_mcps(self.config.mcps_dir)
        # Interactive PTY: the proxy lets Claude run its
        # native TUI on this satellite (streamed over the WS) only when this is
        # advertised. Unix uses pty_relay (pty.openpty); Windows uses winpty_relay
        # (ConPTY via pywinpty) — advertised only when the pywinpty wheel is
        # actually importable so a Windows box that missed the dep falls back to
        # headless -p instead of opening a PTY that can't spawn.
        caps["interactive_pty"] = _interactive_pty_supported()
        # Per-satellite budget: raw resources + this host's own
        # physical-safety session ceiling. The proxy displays these and may apply
        # a lower admin override (remote_machines.max_sessions) on top.
        cpus, mem_bytes = _machine_resources()
        caps["cpu_count"] = cpus
        caps["mem_total_bytes"] = mem_bytes
        caps["recommended_max_sessions"] = _recommended_max_sessions()

        return caps

    def cleanup_orphans(self) -> None:
        """Kill orphan process trees + GC stale install scratch dirs from a
        previous satellite run.

        Scans for PID files and tree-kills any still-running processes.
        Tree-kill covers ``cmd.exe`` → ``node.exe`` on Windows and
        MCP/child forks on Unix. Then sweeps leftover MCP install scratch
        directories so they don't accumulate.
        """
        pid_dir = self.config.agents_dir / ".satellite-pids"
        if pid_dir.is_dir():
            for pid_file in pid_dir.glob("*.pid"):
                try:
                    pid = int(pid_file.read_text().strip())
                    kill_process_tree(pid, timeout=3.0)
                    logger.info(f"Killed orphan process tree: pid={pid}")
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
                finally:
                    pid_file.unlink(missing_ok=True)

        # GC stale MCP-install scratch dirs left by a Windows quarantine-on-lock
        # swap (``.quarantine-<mcp>-<uuid>``) or an interrupted install
        # (``<mcp>.new``). The handles that blocked deletion mid-install are
        # released after this restart, so force_rmtree succeeds now. Fulfils the
        # "GC'd by the startup sweep" contract that _force_remove_or_quarantine
        # and the sync_mcps swap rely on. Best-effort — a still-held dir is
        # retried on the next restart.
        mcps_dir = self.config.mcps_dir
        if mcps_dir.is_dir():
            for category_dir in mcps_dir.iterdir():
                if not category_dir.is_dir():
                    continue
                stale = list(category_dir.glob(".quarantine-*")) + list(
                    category_dir.glob("*.new")
                )
                for d in stale:
                    try:
                        force_rmtree(d)
                        logger.info("Swept stale MCP install scratch dir: %s", d.name)
                    except OSError:
                        pass  # still held — retry next restart

    # -----------------------------------------------------------------
    # Command handlers
    # -----------------------------------------------------------------

    async def start_session(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Start a CLI or Codex session."""
        session_id = msg["session_id"]
        execution_path = msg["execution_path"]
        config_payload = msg["config"]
        command_id = msg["command_id"]
        agent_slug = msg["agent_slug"]

        try:
            # Same slug gate as file_push/file_pull — the slug becomes a
            # path component of the whole config-tree write below.
            from ..host import auth_paths
            if not auth_paths.is_safe_slug(agent_slug):
                raise ValueError(f"unsafe agent_slug: {agent_slug!r}")
            agent_dir = self.config.agents_dir / agent_slug
            agent_dir.mkdir(parents=True, exist_ok=True)
            self._check_capacity(session_id)  # physical-safety cap → ack error below

            # A start for a session id we still track live means the platform
            # lost its handle (e.g. proxy restart mid-turn) and is replacing
            # the process — close the old one first or it lingers satellite-
            # parented forever. Must happen BEFORE the new spawn: close()
            # flushes the old CLI's transcript for --resume, and its
            # session_files.wipe() would otherwise clobber the replacement's
            # freshly materialized secret files (same session id).
            stale = self.sessions.pop(session_id, None)
            if stale is not None:
                logger.warning(
                    f"start_session: session {session_id} already live "
                    f"(pid={stale.pid}) — closing stale process before respawn"
                )
                try:
                    await stale.close()
                except Exception:
                    logger.exception(f"Failed to close stale session {session_id}")

            if execution_path == "claude-code-cli":
                session = CLISession(session_id, agent_dir, config_payload, self.config)
                await session.start()
            elif execution_path == "codex-cli":
                session = CodexSession(session_id, agent_dir, config_payload, self.config)
                await session.start()
            else:
                raise ValueError(f"Unsupported execution_path: {execution_path}")

            self.sessions[session_id] = session

            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "ok",
            })
            await ws.enqueue_send({
                "type": "session_started",
                "session_id": session_id,
                "command_id": command_id,
                "pid": session.pid,
                "execution_path": execution_path,
            })

            # Codex app-server captures the thread_id at start (the thread/start
            # response), not per-turn — report it now so the proxy persists it
            # to chats.codex_thread_id for resume after a proxy restart.
            if execution_path == "codex-cli" and getattr(session, "thread_id", None):
                await ws.enqueue_send({
                    "type": "codex_thread_id",
                    "session_id": session_id,
                    "thread_id": session.thread_id,
                })

        except Exception as e:
            logger.exception(f"Failed to start session {session_id}")
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "error",
                "error": str(e),
            })

    async def send_message(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Forward user message to session, stream events, scan files, signal turn_ended."""
        session_id = msg["session_id"]
        command_id = msg["command_id"]
        session = self.sessions.get(session_id)

        if not session:
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "error",
                "error": "Session not found",
            })
            return

        # CLI sessions hold a persistent subprocess; if it died (crash, or a
        # prior abort) fail the send as an ACK error so the proxy's cli_dead
        # path fires and the dashboard auto-resumes a fresh session. Without
        # this, the dead-proc RuntimeError raised inside session.send_message
        # is caught below and emitted as a session_event error — which the
        # proxy does NOT map to cli_dead, leaving the session hung.
        if session.execution_path == "claude-code-cli" and not session.is_alive:
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "error",
                "error": "CLI process not running",
            })
            return

        await ws.enqueue_send({"type": "ack", "command_id": command_id, "status": "ok"})

        try:
            if session.execution_path == "codex-cli":
                # Codex: a persistent forwarder (started at session start) streams
                # EVERY app-server notification to the proxy as a session_event —
                # including a BACKGROUND sub-agent's events AFTER the main turn
                # ends, so the proxy (which holds the translator) can supervise
                # bg sub-agents centrally. run_turn just drives turn/start + waits
                # for the MAIN thread's completion (the satellite stays dumb).
                session.set_event_forwarder(lambda ev: ws.enqueue_send({
                    "type": "session_event",
                    "session_id": session_id,
                    "execution_path": "codex-cli",
                    "event": ev,
                }))
                await session.run_turn(
                    msg["message"],
                    inject_time=msg.get("inject_time", False),
                )
            else:
                # Fresh per-turn retention buffer (Mode C re-attach): a proxy
                # restart mid-turn replays these to close or resume the turn.
                state = self.turn_state.setdefault(
                    session_id, {"seq": 0},
                )
                state.update({"command_id": command_id, "active": True,
                              "start_seq": state["seq"] + 1})
                buffer = deque(maxlen=2000)
                self.turn_buffers[session_id] = buffer
                async for event in session.send_message(
                    msg["message"],
                    inject_time=msg.get("inject_time", False),
                ):
                    # Tag each CLI turn event with the command it streamed
                    # under, so a replaced process's dying flush racing into
                    # the NEXT turn's queue can't terminate that turn (the
                    # proxy drops mismatched result/error events), plus a
                    # per-session seq so a replay overlap dedupes. Codex
                    # events stay untagged — its persistent forwarder crosses
                    # turns by design (bg sub-agent supervision).
                    event["_command_id"] = command_id
                    state["seq"] += 1
                    event["_seq"] = state["seq"]
                    buffer.append(event)
                    await self._wait_forward_unpaused(session_id)
                    await ws.enqueue_send({
                        "type": "session_event",
                        "session_id": session_id,
                        "execution_path": session.execution_path,
                        "event": event,
                    })
                # (CLI sessions forward raw NDJSON; Codex thread_id is reported
                # once at start_session from the thread/start response.)

        except Exception as e:
            logger.exception(f"Error streaming session {session_id}")
            await ws.enqueue_send({
                "type": "session_event",
                "session_id": session_id,
                "execution_path": session.execution_path,
                "event": {"type": "error", "message": str(e),
                          "_command_id": command_id},
            })

        # Close the retention buffer: a `_turn_sentinel` marks the buffered
        # turn as FINISHED, so a proxy that restarts after this point replays
        # to a clean turn close instead of waiting on a live stream.
        state = self.turn_state.get(session_id)
        buffer = self.turn_buffers.get(session_id)
        if state is not None and state.get("command_id") == command_id:
            state["active"] = False
            if buffer is not None:
                state["seq"] += 1
                buffer.append({"type": "_turn_sentinel",
                               "command_id": command_id,
                               "_seq": state["seq"]})

        # Signal end-of-turn so the proxy can yield DONE and release the turn.
        # Echo ``command_id`` so the proxy can correlate this marker with the
        # originating send_message. This MUST precede the file-change scan
        # below: on a large workspace the scan takes far longer than the
        # proxy's post-stop drain budget, so a marker sent after it arrives
        # once the turn already concluded — it then sits in the queue as a
        # stale leftover, and once an autonomous turn (e.g. a background-
        # agent nudge) interleaves, the marker accounting goes permanently
        # one turn behind: every response persists one prompt late and the
        # final turn's response is lost (live repro 2026-07-21, delegated
        # worker chat). The proxy's RemoteExecutionLayer filters on this id.
        await ws.enqueue_send({
            "type": "turn_ended",
            "session_id": session_id,
            "command_id": command_id,
        })

        # Scan for file changes after the marker: file_changed frames are
        # applied by the proxy independently of turn state (they carry
        # agent_slug so the write routes into the right per-agent dir with
        # no session lookup), so delivering them after DONE only defers the
        # sync by the scan duration — exactly what already happened whenever
        # the scan overran the old drain window.
        try:
            changes = session.detect_file_changes()
            for change in changes:
                await ws.enqueue_send({
                    "type": "file_changed",
                    "session_id": session_id,
                    "agent_slug": session.agent_slug,
                    **change,
                })
        except Exception:
            logger.exception(f"Error detecting file changes for {session_id}")

        # CLI: if the proxy flagged a still-pending backgrounded command on its
        # stop_turn, keep draining + forwarding stdout post-turn so the command's
        # completion frame (claude emits it idle, after the turn) reaches the
        # proxy's bg-command monitor. No-op unless drain_bg was requested; the
        # next send_message cancels it. (Codex uses its own persistent forwarder.)
        if session.execution_path == "claude-code-cli" and hasattr(session, "start_bg_drain"):
            async def _fwd_bg(ev):
                # Tagged with THIS (finished) turn's command id: the proxy's
                # stale filter only drops mismatched result/error events, so
                # the drain's task_updated completion frames still reach the
                # bg-command registry even mid-next-turn.
                ev["_command_id"] = command_id
                await ws.enqueue_send({
                    "type": "session_event",
                    "session_id": session_id,
                    "execution_path": session.execution_path,
                    "event": ev,
                })
            session.start_bg_drain(_fwd_bg)

    async def stop_turn(self, msg: dict) -> None:
        """Proxy is asking the current send_message loop to exit (turn over).
        ``drain_bg`` (set when a backgrounded command is still pending) arms the
        session's post-turn stdout drainer once this turn's loop exits."""
        session_id = msg.get("session_id", "")
        session = self.sessions.get(session_id)
        if session and hasattr(session, "request_stop_turn"):
            session.request_stop_turn(drain_bg=msg.get("drain_bg", False))

    async def control_response(self, msg: dict) -> None:
        """Forward a native permission answer to the CLI's control channel."""
        session_id = msg.get("session_id", "")
        session = self.sessions.get(session_id)
        if not session:
            return
        if not hasattr(session, "send_permission_response"):
            return
        request_id = msg.get("request_id", "")
        approved = bool(msg.get("approved", False))
        await session.send_permission_response(request_id, approved)

    async def interrupt_turn(self, msg: dict) -> None:
        """Soft-stop the current turn: write ``control_request {interrupt}``
        into the CLI's stdin — the same frame the platform's local layer uses.

        The CLI closes the turn with a normal result event that flows back
        through the running read loop, so the subprocess and its MCP sidecars
        SURVIVE for the next prompt (no re-warm, on every OS — a stdin frame
        has no signal/console semantics). No ack frame: the proxy watches the
        turn close itself and escalates to the hard ``abort`` if it doesn't
        (dead process, dropped frame).
        """
        session_id = msg.get("session_id", "")
        session = self.sessions.get(session_id)
        if session is None or not hasattr(session, "send_control_request"):
            return
        proc = getattr(session, "proc", None)
        if proc is None or proc.returncode is not None:
            logger.warning(
                "interrupt_turn: CLI process not running for %s — proxy "
                "watchdog will escalate to abort", session_id,
            )
            return
        try:
            await session.send_control_request("interrupt")
            logger.info("interrupt_turn: sent graceful interrupt to %s", session_id)
        except Exception as e:
            logger.warning(
                "interrupt_turn: control write failed for %s: %s", session_id, e,
            )

    async def abort(self, msg: dict, ws: "SatelliteWSClient | None" = None) -> None:
        """Abort a running session, then ack.

        The ack tells the platform the graceful kill (SIGINT → flush →
        tree-kill) is fully complete, so it can drain any events the dying CLI
        flushed during the abort window before auto-resuming — without it those
        stale events (e.g. a buffered image) leak into the next turn, and the
        next message can race the still-dying process.
        """
        session_id = msg.get("session_id", "")
        session = self.sessions.get(session_id)
        if session:
            await session.abort()
        if ws is not None:
            try:
                await ws.enqueue_send({"type": "session_aborted", "session_id": session_id})
            except Exception as e:
                logger.warning(
                    "abort: failed to send session_aborted ack for %s: %s",
                    session_id, e,
                )

    async def close_session(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Clean shutdown of a session."""
        session_id = msg.get("session_id", "")
        command_id = msg.get("command_id", "")
        session = self.sessions.pop(session_id, None)
        # A proxy-commanded close is deliberate — drop the turn retention.
        self.turn_buffers.pop(session_id, None)
        self.turn_state.pop(session_id, None)
        self.forward_paused.discard(session_id)

        if session:
            await session.close()
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "ok",
            })
            await ws.enqueue_send({
                "type": "session_ended",
                "session_id": session_id,
                "exit_code": 0,
            })
        else:
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "ok",
            })

    async def control_request(self, msg: dict) -> None:
        """Forward control request (model/mode change) to CLI via stdin."""
        session_id = msg.get("session_id", "")
        session = self.sessions.get(session_id)
        if not session:
            return
        if not hasattr(session, "send_control_request"):
            return  # Codex has no mid-session control channel
        subtype = msg.get("subtype", "")
        kwargs = msg.get("kwargs", {})
        await session.send_control_request(subtype, **kwargs)

    async def credentials_update(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Rotation fan-out: rewrite a scope config dir's CLI credential file
        with a freshly rotated OAuth token (the platform's token fan-out —
        these files are excluded from the generic file sync, this push is
        their only delivery channel). The live CLIs pick the rewrite up on
        their own: Claude via its ``.credentials.json`` mtime-watch /
        401-recovery, Codex via its guarded ``auth.json`` reload. No process
        is touched.
        """
        command_id = msg.get("command_id", "")
        agent_slug = msg.get("agent_slug", "")
        dir_relative = str(msg.get("dir_relative", ""))
        kind = msg.get("kind", "")
        content = msg.get("content")
        error_msg = ""
        try:
            # The target is constrained to exactly what the platform writes at
            # session start: a .claude/.codex scope dir inside the agent tree.
            parts = dir_relative.replace("\\", "/").split("/")
            if (
                not agent_slug or "/" in agent_slug or "\\" in agent_slug
                or agent_slug in (".", "..")
                or not isinstance(content, dict)
                or kind not in ("claude", "codex")
                # RELATIVE only: an absolute dir_relative would win the join
                # (`agents_dir / slug / "/x"` IS "/x") and relocate the
                # credential write anywhere — reject leading separators and
                # drive-qualified forms alongside traversal.
                or Path(dir_relative).is_absolute()
                or parts[0] == "" or ":" in parts[0]
                or ".." in parts
                or parts[-1] != (".claude" if kind == "claude" else ".codex")
            ):
                raise ValueError(f"invalid credentials_update target: "
                                 f"{agent_slug!r}/{dir_relative!r} kind={kind!r}")
            filename = ".credentials.json" if kind == "claude" else "auth.json"
            target_dir = self.config.agents_dir / agent_slug / dir_relative
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / filename
            path.write_text(json.dumps(content))
            path.chmod(0o600)
            logger.info("credentials_update: rewrote %s/%s/%s",
                        agent_slug, dir_relative, filename)
        except Exception as e:
            logger.exception("credentials_update failed")
            error_msg = str(e)
        await ws.enqueue_send({
            "type": "ack",
            "command_id": command_id,
            "status": "error" if error_msg else "ok",
            **({"error": error_msg} if error_msg else {}),
        })

    # -----------------------------------------------------------------
    # Interactive PTY handlers
    # -----------------------------------------------------------------

    async def pty_open(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Spawn an interactive TUI under a PTY (the remote analogue of
        start_session for the -p path). Acks with the satellite pid so the
        proxy's RemotePtyProcess can log parity."""
        session_id = msg["session_id"]
        command_id = msg["command_id"]
        agent_slug = msg["agent_slug"]
        config_payload = msg["config"]
        execution_path = msg.get("execution_path", "claude-code-cli")
        rows = int(msg.get("rows", 24))
        cols = int(msg.get("cols", 80))

        try:
            # Same slug gate as start_session — the slug is a path component.
            from ..host import auth_paths
            if not auth_paths.is_safe_slug(agent_slug):
                raise ValueError(f"unsafe agent_slug: {agent_slug!r}")
            agent_dir = self.config.agents_dir / agent_slug
            agent_dir.mkdir(parents=True, exist_ok=True)
            self._check_capacity(session_id)  # physical-safety cap → ack error below
            # Codex runs the Ratatui TUI (rollout JSONL); Claude runs
            # the Ink TUI (<id>.jsonl). Both share BasePtySession (spawn + transcript
            # forwarding); only the config tree / argv / rollout-locate differ.
            if execution_path == "codex-cli":
                from ..terminal.codex_pty_session import CodexPtySession as _PtySessionCls
            else:
                from ..terminal.pty_session import PtySession as _PtySessionCls
            session = _PtySessionCls(
                session_id, agent_dir, config_payload, self.config, ws,
                rows=rows, cols=cols,
            )
            # When the child exits on its own, drop it from pty_sessions
            # so a later pty_alive reconcile is accurate (no dead-session re-adopt).
            session._on_dead = lambda sid=session_id: self._on_pty_dead(sid)
            await session.start()
            self.pty_sessions[session_id] = session
            # otodock-CLI: if a local terminal is already waiting for this
            # session (the common ordering — start_session awaits the pty_open ack
            # before the proxy sends local_session_opened, so the conn attaches
            # right after; this also covers the reverse race), wire its relay now.
            conn = self._otodock_conns.get(session_id)
            if conn is not None:
                self._wire_local_conn(session, conn)
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "ok",
                "pid": session.pid,
            })
        except Exception as e:
            logger.exception(f"Failed to open PTY session {session_id}")
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "error",
                "error": str(e),
            })

    async def pty_input(self, msg: dict) -> None:
        session = self.pty_sessions.get(msg.get("session_id", ""))
        if session is not None:
            try:
                session.write(base64.b64decode(msg.get("data_b64", "") or ""))
            except Exception:
                logger.debug("pty_input decode/write failed", exc_info=True)

    async def pty_resize(self, msg: dict) -> None:
        session = self.pty_sessions.get(msg.get("session_id", ""))
        if session is not None:
            session.resize(int(msg.get("rows", 24)), int(msg.get("cols", 80)))

    async def pty_inject(self, msg: dict, ws) -> None:
        """Server-prompt injection into an otodock-attached CLI's stdin (the
        proxy's delegation-delivery PTY rung). Unknown session → NACK so the
        proxy falls back instead of waiting out its result timeout."""
        sid = msg.get("session_id", "")
        inject_id = str(msg.get("inject_id", ""))
        session = self.pty_sessions.get(sid)
        if session is None:
            await ws.enqueue_send({
                "type": "pty_inject_result",
                "session_id": sid,
                "inject_id": inject_id,
                "ok": False,
                "reason": "session_gone",
            })
            return
        await session.inject_prompt(
            str(msg.get("text", "")), inject_id, str(msg.get("source", "")),
        )

    async def pty_close(self, msg: dict) -> None:
        sid = msg.get("session_id", "")
        session = self.pty_sessions.pop(sid, None)
        conn = self._otodock_conns.pop(sid, None)
        if session is not None:
            await session.close()
            logger.info(f"PTY session closed: {sid}")
        if conn is not None:
            # Proxy-driven close (e.g. take-over) → tell the local terminal
            # to exit. Idempotent with the _on_exit feed if the child also died.
            conn.feed_exit(None)

    # --- otodock-CLI local control-socket relay ------------------------------
    def _on_pty_dead(self, session_id: str) -> None:
        """Child PTY exited on its own — drop bookkeeping. The local terminal was
        already notified via the PTY's `_on_exit` → `conn.feed_exit` tee (which
        fires before this), so we just clear the maps."""
        self.pty_sessions.pop(session_id, None)
        self._otodock_conns.pop(session_id, None)

    def _wire_local_conn(self, session, conn, force_repaint: bool = False) -> None:
        """Cross-wire a local `otodock` terminal to a live PTY session: the conn's
        input/resize drive the PTY, and the PTY's output/exit tee to the conn.

        Replays the PTY's current scrollback to the new conn so the terminal sees
        the current screen (an ATTACH-to-live terminal sees the running session;
        the fresh-spawn flow catches up on bytes emitted between spawn and wire).
        The replay is QUERY-SANITIZED (``terminal_queries.strip_queries``): the
        ring holds every stale terminal query the TUI ever emitted (Ink sends a
        ``CSI 6n`` per render frame), and a real terminal auto-answers each one
        it parses — a raw replay fires that whole backlog into the PTY as input,
        the TUI re-renders and emits fresh queries, and the loop self-sustains
        (the dual-attach "growing garbage" storm). Live tee bytes stay RAW — the
        attached terminal is the controlling one and must answer live queries.
        ``force_repaint`` (attach-to-live only) then nudges the winsize so an
        alt-screen TUI redraws cleanly even when the new terminal's size equals the
        PTY's current size — skipped on a fresh spawn (the TUI renders fresh
        anyway, and a nudge mid-startup is pointless). Both the relay attach + the
        scrollback replay run SYNCHRONOUSLY (no awaits between), so a live-tee frame
        can't interleave and duplicate the replayed scrollback."""
        from ..terminal.terminal_queries import strip_clipboard_writes, strip_queries

        conn.bind_session(session)
        session.attach_local_relay(conn.feed_output, conn.feed_exit)
        pty = getattr(session, "pty", None)
        if pty is not None:
            try:
                # Replay boundary: queries would be auto-answered; a buffered
                # OSC 52 copy would overwrite the attaching terminal's system
                # clipboard. Both are replay-only strips (live output passes).
                sb = strip_clipboard_writes(strip_queries(pty.scrollback()))
                if sb:
                    conn.feed_output(sb)
            except Exception:
                logger.debug("otodock scrollback replay failed", exc_info=True)
        if force_repaint:
            try:
                session.force_repaint()
            except Exception:
                logger.debug("otodock force_repaint failed", exc_info=True)
        logger.info(
            "otodock local terminal attached to PTY session %s", session.session_id
        )

    def attach_local_conn(self, session_id: str, conn, attach: bool = False) -> None:
        """Register a local terminal for ``session_id`` (called once the proxy has
        confirmed the session). Wires immediately if the PTY already exists, else
        ``pty_open`` wires it when the PTY spawns.

        ``attach`` (dual-control attach-to-live) is informational — the PTY's
        presence is what decides wiring; the proxy/_handle_conn already guarantee
        an ATTACH only arrives when ``pty_sessions[session_id]`` exists.

        otodock-vs-otodock take-over: if ANOTHER terminal already holds this
        session, evict it FIRST (tell its client + close its conn) but do NOT
        detach the relay — we immediately re-attach it to the new ``conn`` below.
        The evicted conn's own EOF cleanup is conn-identity-suppressed
        (``detach_local_conn`` returns False for it), so it can't tear down the new
        controller."""
        old = self._otodock_conns.get(session_id)
        if old is not None and old is not conn:
            from ..terminal.otodock_proto import SUPERSEDE_EXIT_CODE
            old.feed_exit(SUPERSEDE_EXIT_CODE, "taken over by another terminal")
            asyncio.create_task(old.close())
        self._otodock_conns[session_id] = conn
        session = self.pty_sessions.get(session_id)
        if session is not None:
            self._wire_local_conn(session, conn, force_repaint=attach)

    def detach_local_conn(self, session_id: str, conn=None) -> bool:
        """The local terminal disconnected — stop teeing to it. The PTY + proxy
        mirror keep running (the chat stays dashboard-resumable; the session
        continues as an orphan).

        CONN-IDENTITY (dual-control): returns True iff ``conn`` is still the
        live conn for this session and we actually detached it (so the caller
        notifies the proxy via ``local_session_detached``). Returns False when
        ``conn`` was already SUPERSEDED (a newer otodock terminal attached, or the
        dashboard kicked it) — then we must NOT detach the new controller's relay
        nor clear its ``otodock_attached``. ``conn=None`` keeps the legacy
        unconditional behavior (no live caller relies on it)."""
        cur = self._otodock_conns.get(session_id)
        if conn is not None and cur is not conn:
            return False
        self._otodock_conns.pop(session_id, None)
        session = self.pty_sessions.get(session_id)
        if session is not None:
            session.detach_local_relay()
        return True

    async def evict_local_conn(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Dual-control: the proxy (a dashboard take-over) asked us to detach
        the local `otodock` terminal from a session WITHOUT killing the PTY. Detach
        the relay, tell the client to exit (with the reason → it prints "taken over
        by …"), and CONFIRM back to the proxy (``local_session_detached``) so it
        flips ``otodock_attached`` off → the dashboard takes control. We pop the
        conn here, so the client's own EOF cleanup is conn-identity-suppressed (no
        double confirm). The PTY + proxy mirror keep running."""
        session_id = msg.get("session_id", "")
        reason = msg.get("reason", "") or "taken over by the dashboard"
        conn = self._otodock_conns.pop(session_id, None)
        session = self.pty_sessions.get(session_id)
        if session is not None:
            session.detach_local_relay()
        if conn is not None:
            from ..terminal.otodock_proto import SUPERSEDE_EXIT_CODE
            conn.feed_exit(SUPERSEDE_EXIT_CODE, reason)
        # Confirm even if conn/session were already gone, so the proxy never gets
        # stuck with otodock_attached=True (→ dashboard gated out forever).
        try:
            await ws.send_local_detached(session_id)
        except Exception:
            logger.debug("evict_local_conn: confirm send failed", exc_info=True)

    async def check_session_resumable(
        self, msg: dict, ws: "SatelliteWSClient",
    ) -> None:
        """Stat the CLI session JSONL on disk; reply whether ``--resume`` would work.

        After an abort the satellite's CLI subprocess exits but the
        ``.claude/projects/<hash>/<session_id>.jsonl`` file claude-code
        wrote during the turn remains on disk. The proxy's
        ``RemoteExecutionLayer.can_resume_session`` calls this RPC to
        decide between (a) issuing ``start_session(resume=True)`` so the
        new subprocess loads conversation history, and (b) creating a
        fresh session (which would drop chat memory entirely).

        Returns ``resumable=True`` iff the JSONL exists AND contains at
        least one user message — matches the contract in
        ``proxy/core/layers/cli/layer.py::can_resume_session``.
        """
        command_id = msg.get("command_id", "")
        session_id = msg.get("session_id", "")
        agent_slug = msg.get("agent_slug", "")
        username = msg.get("username", "")

        resumable = False
        try:
            if agent_slug and session_id:
                # Mirror the proxy-side path derivation: per-user agent
                # dirs win over the shared workspace .claude/ for sessions
                # that ran under a logged-in user.
                if username:
                    claude_dir = (
                        self.config.agents_dir / agent_slug
                        / "users" / username / ".claude"
                    )
                else:
                    claude_dir = (
                        self.config.agents_dir / agent_slug
                        / "workspace" / ".claude"
                    )
                projects_dir = claude_dir / "projects"
                if projects_dir.is_dir():
                    # Project-hash dir name varies by CWD; search all.
                    for project_dir in projects_dir.iterdir():
                        if not project_dir.is_dir():
                            continue
                        candidate = project_dir / f"{session_id}.jsonl"
                        if candidate.exists():
                            try:
                                content = candidate.read_text(
                                    encoding="utf-8", errors="replace",
                                )
                                resumable = (
                                    '"type":"user"' in content
                                    or '"type": "user"' in content
                                )
                            except OSError:
                                resumable = False
                            break
        except Exception as e:
            logger.warning(
                "check_session_resumable failed for %s: %s",
                session_id[:8] if session_id else "?", e,
            )

        await ws.enqueue_send({
            "type": "ack",
            "command_id": command_id,
            "status": "ok",
            "resumable": resumable,
        })

    async def codex_compact(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Manual Codex thread compaction RPC (proxy
        ``RemoteExecutionLayer.compact`` → ``CodexSession.compact``). Always
        acks — ``{ok, post_tokens|reason}`` — so the proxy never waits out the
        timeout on a refusal."""
        command_id = msg.get("command_id", "")
        session_id = msg.get("session_id", "")
        session = self.sessions.get(session_id)
        compact = getattr(session, "compact", None)
        result = None
        reason = ""
        if compact is None:
            reason = ("session not found" if session is None
                      else "not a codex app-server session")
        else:
            try:
                result = await compact()
                if result is None:
                    reason = "compaction refused (turn active?) or failed"
            except Exception as e:
                logger.exception("codex_compact failed for %s", session_id[:8])
                reason = str(e)
        ack = {
            "type": "ack", "command_id": command_id, "status": "ok",
            "ok": result is not None,
            "post_tokens": (result or {}).get("post_tokens"),
        }
        if result is None:
            ack["reason"] = reason
        await ws.enqueue_send(ack)

    async def codex_steer(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Mid-turn steering RPC (proxy ``RemoteExecutionLayer.steer`` →
        ``CodexSession.steer``). ``steered`` is STRICT — True only on daemon
        accept: the proxy's caller queues the message otherwise, so a false
        positive would lose user input."""
        command_id = msg.get("command_id", "")
        session_id = msg.get("session_id", "")
        text = msg.get("text", "") or ""
        session = self.sessions.get(session_id)
        steer = getattr(session, "steer", None)
        steered = False
        if steer is not None and text:
            try:
                steered = bool(await steer(text))
            except Exception:
                logger.exception("codex_steer failed for %s", session_id[:8])
                steered = False
        await ws.enqueue_send({
            "type": "ack", "command_id": command_id, "status": "ok",
            "steered": steered,
        })

    async def codex_bg_terminals(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Background-terminal list RPC (proxy bg-command drain →
        ``CodexSession.list_background_terminals``). Always acks —
        ``{ok, terminals|reason}`` — so the proxy never waits out the timeout.
        ``ok`` False means NO list was obtained; the proxy resolves nothing
        (fail-closed), so a soft failure here must never claim an empty list."""
        command_id = msg.get("command_id", "")
        session_id = msg.get("session_id", "")
        session = self.sessions.get(session_id)
        lister = getattr(session, "list_background_terminals", None)
        terminals = None
        reason = ""
        if lister is None:
            reason = ("session not found" if session is None
                      else "not a codex app-server session")
        else:
            try:
                terminals = await lister()
                if terminals is None:
                    reason = "list failed (daemon busy/dead?)"
            except Exception as e:
                logger.exception("codex_bg_terminals failed for %s", session_id[:8])
                reason = str(e)
                terminals = None
        ack = {
            "type": "ack", "command_id": command_id, "status": "ok",
            "ok": terminals is not None,
            "terminals": terminals,
        }
        if terminals is None:
            ack["reason"] = reason
        await ws.enqueue_send(ack)

    async def _wait_forward_unpaused(self, session_id: str) -> None:
        """Park the live CLI forwarder while a replay is being enqueued so the
        wire order stays strictly [replay][live]. Events keep landing in the
        retention buffer meanwhile (append happens before this wait), so the
        replay snapshot covers them; the proxy dedupes any overlap by _seq."""
        while session_id in self.forward_paused:
            await asyncio.sleep(0.05)

    def headless_sessions_alive(self) -> list[dict]:
        """Live headless CLI sessions + their turn state, reported post-auth
        (mirrors pty_alive) so a RESTARTED proxy can re-adopt in-flight turns
        instead of failing them blind. Codex sessions are not reported —
        thread-id resume already covers their next-turn continuity."""
        out = []
        for sid, session in self.sessions.items():
            if session.execution_path != "claude-code-cli":
                continue
            if not session.is_alive:
                continue
            state = self.turn_state.get(sid) or {}
            buffer = self.turn_buffers.get(sid)
            out.append({
                "session_id": sid,
                "execution_path": session.execution_path,
                "agent_slug": getattr(session, "agent_slug", ""),
                "turn_active": bool(state.get("active")),
                "command_id": state.get("command_id", ""),
                "buffered_events": len(buffer) if buffer is not None else 0,
                "use_native_permissions": bool(
                    getattr(session, "config", {}).get(
                        "use_native_permissions", False)
                ),
            })
        return out

    async def resume_session_stream(
        self, msg: dict, ws: "SatelliteWSClient",
    ) -> None:
        """Replay the retained turn buffer to a re-adopting (restarted) proxy.

        The live forwarder is paused for the duration so the frames arrive
        strictly [begin][replay][live]; a buffered `_turn_sentinel` closes a
        finished-during-outage turn via a trailing turn_ended frame."""
        session_id = msg.get("session_id", "")
        state = self.turn_state.get(session_id) or {}
        buffer = self.turn_buffers.get(session_id)
        self.forward_paused.add(session_id)
        try:
            events = list(buffer) if buffer is not None else []
            truncated = bool(
                events and state.get("start_seq")
                and events[0].get("_seq", 0) > state["start_seq"]
            )
            await ws.enqueue_send({
                "type": "session_event",
                "session_id": session_id,
                "execution_path": "claude-code-cli",
                "event": {"type": "_resume_replay_begin",
                          "truncated": truncated,
                          "count": len(events)},
            })
            ended_command = ""
            for i, ev in enumerate(events):
                if ev.get("type") == "_turn_sentinel":
                    ended_command = ev.get("command_id", "")
                    continue
                await ws.enqueue_send({
                    "type": "session_event",
                    "session_id": session_id,
                    "execution_path": "claude-code-cli",
                    "event": ev,
                })
                if i % 100 == 99:
                    # Let the writer drain — a 2000-event burst must not
                    # starve acks/heartbeats (control-first mitigates, but
                    # yield anyway).
                    await asyncio.sleep(0)
            if ended_command:
                await ws.enqueue_send({
                    "type": "turn_ended",
                    "session_id": session_id,
                    "command_id": ended_command,
                })
            logger.info(
                "Replayed %d buffered event(s) for session %s "
                "(truncated=%s, ended=%s)",
                len(events), session_id[:8], truncated, bool(ended_command),
            )
        finally:
            self.forward_paused.discard(session_id)

    async def check_session_process(
        self, msg: dict, ws: "SatelliteWSClient",
    ) -> None:
        """Liveness probe for a headless session's subprocess.

        The proxy's stall-reap consults this before killing a turn that has
        gone event-silent: a live CLI with a quiet stream is a network stall
        or a long quiet tool run, not a death (the Mode D incident). Absent
        session → alive=False (nothing to preserve)."""
        session = self.sessions.get(msg.get("session_id", ""))
        await ws.enqueue_send({
            "type": "ack",
            "command_id": msg.get("command_id", ""),
            "status": "ok",
            "alive": bool(session is not None and session.is_alive),
        })

    # -----------------------------------------------------------------
    # File sync command handlers
    # -----------------------------------------------------------------

    def compute_agent_fingerprints(self) -> dict[str, str]:
        """Cheap STAT-only tree fingerprint per agent dir this satellite holds, for
        the proxy's idle change-detection. Walks each ``agents_dir/<slug>``
        and returns ``{slug: fingerprint}``; skips non-dirs + internal dot-dirs
        (e.g. ``.satellite-pids``). Synchronous — the heartbeat loop calls it via
        ``asyncio.to_thread`` so a large tree never blocks the loop. Best-effort: a
        dir that errors is omitted."""
        out: dict[str, str] = {}
        base = self.config.agents_dir
        try:
            if not base.is_dir():
                return out
            children = list(base.iterdir())
        except (OSError, PermissionError):
            return out
        for child in children:
            try:
                if not child.is_dir() or child.name.startswith("."):
                    continue
                out[child.name] = file_sync.compute_fingerprint(child)
            except (OSError, PermissionError):
                continue
        return out

    async def request_manifest(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Compute and send file manifest for an agent directory."""
        agent_slug = msg.get("agent_slug", "")
        command_id = msg.get("command_id", "")
        agent_dir = self.config.agents_dir / agent_slug

        # Off-thread: a big tree's manifest (walk + hash, even cache-warm)
        # would otherwise stall the event loop — heartbeats and PTY frames
        # share it.
        manifest = await asyncio.to_thread(file_sync.compute_manifest, agent_dir)
        await ws.enqueue_send({
            "type": "file_manifest",
            "agent_slug": agent_slug,
            "command_id": command_id,
            "files": manifest,
        })

    async def file_push(self, msg: dict, ws: "SatelliteWSClient | None" = None) -> None:
        """Apply a file pushed from the platform.

        If the message carries a `command_id`, replies with an ack once the
        write is complete so the proxy's `push_file` helper can await the
        flush (write-back barrier in remote_file_flow).

        If a session for this agent_slug is currently running, its
        ``_file_snapshot`` is refreshed with the new file's hash so the
        end-of-turn ``detect_file_changes`` scan doesn't emit a phantom
        ``file_changed`` event echoing the same content back to the
        platform.

        ``path_kind``:
          * ``"agent_tree"`` (default) — agents-relative path. Allowlist
            enforced via ``auth_paths.assert_agent_path_safe``.
          * ``"satellite_host"`` — absolute path on the satellite. The
            satellite re-validates ``..`` / NUL / non-absolute defensively
            before writing; the proxy is the policy authority (decides
            home-only vs full-FS via ``allow_full_fs``).
        """
        from ..host import auth_paths

        path_kind = msg.get("path_kind", "agent_tree")
        agent_slug = msg.get("agent_slug", "")
        rel_path = msg.get("path", "")
        error_msg = ""

        # Path-form validation. agent_tree → allowlist + slug-safe.
        # satellite_host → absolute + no traversal + no NUL.
        try:
            if path_kind == "agent_tree":
                if msg.get("action") != "delete":
                    auth_paths.assert_agent_path_safe(
                        rel_path, agent_slug, self.config.agents_dir,
                    )
                elif not auth_paths.is_safe_slug(agent_slug):
                    raise ValueError(f"unsafe agent_slug: {agent_slug!r}")
            elif path_kind == "satellite_host":
                _validate_satellite_host_path(rel_path)
                # Re-check the satellite's local
                # allow_full_fs policy before accepting the write.
                # Deletes also go through this check.
                _check_satellite_host_policy(rel_path)
            else:
                raise ValueError(f"unknown path_kind: {path_kind!r}")
        except ValueError as e:
            logger.warning(
                "file_push rejected: %s (kind=%r slug=%r path=%r)",
                e, path_kind, agent_slug, rel_path,
            )
            command_id = msg.get("command_id", "")
            if command_id and ws is not None:
                await ws.enqueue_send({
                    "type": "ack",
                    "command_id": command_id,
                    "status": "rejected",
                    "error": f"path not authorized: {e}",
                })
            return

        # satellite_host: dispatch to the absolute-path handler and ack
        # there. Agent-tree path falls through to the existing flow.
        if path_kind == "satellite_host":
            await _apply_satellite_host_push(msg, ws)
            return

        agent_dir = self.config.agents_dir / agent_slug
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Collect every active session's snapshot under this agent_slug so
        # apply_file_push can refresh them all post-write — multiple sessions
        # share an agent_dir; without this they'd each emit a phantom
        # file_changed for the just-pushed file at end-of-turn. PTY sessions
        # scan too (quiescence scan in pty_session_base) — refreshing theirs
        # is what keeps a platform push from echoing back as a write-back.
        snapshots_to_refresh = [
            s._file_snapshot
            for s in list(self.sessions.values()) + list(self.pty_sessions.values())
            if getattr(s, "agent_slug", "") == agent_slug
            and hasattr(s, "_file_snapshot")
        ]

        try:
            file_sync.apply_file_push(
                agent_dir, msg, snapshots=snapshots_to_refresh,
            )
        except Exception as e:  # pragma: no cover — defensive
            error_msg = f"{type(e).__name__}: {e}"

        # A push of a hooks-settings file (.claude/settings.json /
        # .codex/hooks.json) just overwrote a LIVE session's spawn-time REAL-path
        # hook commands with the proxy's bwrap-VIRTUAL paths (the reconnect
        # re-sync re-pushes push-only .claude/.codex). A live CLI re-reads settings
        # on change, so its PreToolUse gate + Stop hook would then resolve to
        # non-existent /users/<u>/… paths. Re-assert real paths.
        if not error_msg and msg.get("action", "write") == "write":
            self._reapply_hooks_after_push(agent_slug, rel_path)

        command_id = msg.get("command_id", "")
        if command_id and ws is not None:
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "error" if error_msg else "ok",
                "error": error_msg,
            })

    def _reapply_hooks_after_push(self, agent_slug: str, rel_path: str) -> None:
        """Re-assert REAL-path hook commands for any LIVE session whose hooks-
        settings file was just clobbered by a platform push.

        The proxy ships ``.claude/settings.json`` / ``.codex/hooks.json`` with
        bwrap-VIRTUAL hook paths (``/users/<u>/.claude/…``) — correct for the
        local sandbox, meaningless on a satellite, where ``start()`` rewrites them
        to REAL ``sys.executable`` paths. A reconnect re-sync (or any platform
        push of these push-only files) overwrites that with the virtual version;
        a running CLI re-reads settings on change → its PreToolUse gate + Stop
        hook break. We regenerate via the SAME writer ``start()`` uses (single
        source of truth — no path-translator, forward-compatible with arbitrary
        cwd), then refresh the session snapshot so no phantom ``file_changed``
        echoes the rewrite. Hook SCRIPTS are path-agnostic + identical content →
        not clobbered, not rewritten. Non-live agents need nothing (next spawn
        rewrites). Best-effort; never raises into the push path."""
        base = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
        if base not in ("settings.json", "hooks.json"):
            return
        agent_dir = self.config.agents_dir / agent_slug
        try:
            pushed = (agent_dir / rel_path).resolve()
        except Exception:
            return
        for session in (*self.sessions.values(), *self.pty_sessions.values()):
            if getattr(session, "agent_slug", "") != agent_slug:
                continue
            claude_dir = getattr(session, "_claude_dir", None)
            codex_dir = getattr(session, "_codex_dir", None)
            try:
                if base == "settings.json" and claude_dir is not None \
                        and (claude_dir / "settings.json").resolve() == pushed:
                    _write_cli_hooks(
                        claude_dir,
                        getattr(session, "config", {}).get("disallowed_tools"),
                    )
                    self._refresh_push_snapshot(session, claude_dir / "settings.json", agent_dir)
                    logger.info(
                        "re-applied real-path Claude hooks after settings push (slug=%s)",
                        agent_slug,
                    )
                elif base == "hooks.json" and codex_dir is not None \
                        and (codex_dir / "hooks.json").resolve() == pushed:
                    _write_codex_hooks(codex_dir)
                    self._refresh_push_snapshot(session, codex_dir / "hooks.json", agent_dir)
                    logger.info(
                        "re-applied real-path Codex hooks after hooks push (slug=%s)",
                        agent_slug,
                    )
            except Exception:
                logger.exception(
                    "hook re-apply failed (slug=%s path=%s)", agent_slug, rel_path,
                )

    @staticmethod
    def _refresh_push_snapshot(session, file_path: Path, agent_dir: Path) -> None:
        """Update a session's ``_file_snapshot`` for ``file_path`` to its on-disk
        hash after we rewrote it, so the next ``detect_changes`` doesn't emit a
        phantom ``file_changed`` (the ``-p`` per-turn scan; harmless for PTY
        sessions that don't scan). Mirrors ``apply_file_push``'s own refresh."""
        snap = getattr(session, "_file_snapshot", None)
        if not isinstance(snap, dict):
            return
        try:
            rel = file_path.relative_to(agent_dir).as_posix()
            snap[rel] = file_sync._hash_file(file_path)
        except Exception:
            pass

    async def file_pull(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Read and send a file back to the platform.

        ``path_kind``:
          * ``"agent_tree"`` (default) — agents-relative path; allowlist
            enforced via ``auth_paths.assert_agent_path_safe`` so a
            compromised proxy can't fish ``~/.ssh/id_rsa``.
          * ``"satellite_host"`` — absolute path; the proxy already
            policy-gated home-vs-full-FS, the satellite re-validates
            absoluteness + traversal defensively.
        """
        from ..host import auth_paths

        path_kind = msg.get("path_kind", "agent_tree")
        agent_slug = msg.get("agent_slug", "")
        request_id = msg.get("request_id", "")
        rel_path = msg.get("path", "")

        try:
            if path_kind == "agent_tree":
                target = auth_paths.assert_agent_path_safe(
                    rel_path, agent_slug, self.config.agents_dir,
                )
            elif path_kind == "satellite_host":
                _validate_satellite_host_path(rel_path)
                # Same local-policy re-check on the
                # read side. A compromised proxy can't exfiltrate
                # ~/.ssh/id_rsa from a home-only machine where the user
                # paired with allow_full_fs=False then disabled the
                # toggle from the dashboard.
                _check_satellite_host_policy(rel_path)
                target = Path(rel_path)
            else:
                raise ValueError(f"unknown path_kind: {path_kind!r}")
        except ValueError as e:
            logger.warning(
                "file_pull rejected: %s (kind=%r slug=%r path=%r)",
                e, path_kind, agent_slug, rel_path,
            )
            await ws.enqueue_send({
                "type": "file_content",
                "request_id": request_id,
                "path": rel_path,
                "error": f"path not authorized: {e}",
            })
            return

        if not target.exists():
            await ws.enqueue_send({
                "type": "file_content",
                "request_id": request_id,
                "path": rel_path,
                "error": "File not found",
            })
            return

        # Stream the file back in CHUNK_SIZE blocks so a large file never
        # becomes a single oversized WS frame — the proxy's 16MB ws_max_size
        # would reject it (close 1009), and the reconnect → initial-sync →
        # pull loop would brick the chat. The final chunk carries the sha256
        # as the eof marker (mirrors the write_chunk push convention). Reads
        # one block at a time so the satellite never holds the whole file.
        import hashlib

        try:
            size = target.stat().st_size
        except OSError as e:
            await ws.enqueue_send({
                "type": "file_content",
                "request_id": request_id,
                "path": rel_path,
                "error": f"stat failed: {e}",
            })
            return
        if size > file_sync.max_file_size():
            await ws.enqueue_send({
                "type": "file_content",
                "request_id": request_id,
                "path": rel_path,
                "error": f"file exceeds {file_sync.max_file_size() // (1024 * 1024)}MB limit",
            })
            return

        chunk_size = file_sync.CHUNK_SIZE
        total_chunks = max(1, (size + chunk_size - 1) // chunk_size)
        hasher = hashlib.sha256()
        try:
            with open(target, "rb") as fh:
                for chunk_index in range(total_chunks):
                    block = fh.read(chunk_size)
                    hasher.update(block)
                    is_last = chunk_index == total_chunks - 1
                    await ws.enqueue_send({
                        "type": "file_content",
                        "request_id": request_id,
                        "path": rel_path,
                        "chunk_index": chunk_index,
                        "total_chunks": total_chunks,
                        "content_b64": base64.b64encode(block).decode(),
                        "hash": f"sha256:{hasher.hexdigest()}" if is_last else "",
                    })
        except OSError as e:
            await ws.enqueue_send({
                "type": "file_content",
                "request_id": request_id,
                "path": rel_path,
                "error": f"read failed: {e}",
            })
            return

    async def file_stat(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Report a file's (size, mtime_ns) so the proxy can revalidate its
        pull-through cache without re-transferring the bytes (0.5.95+).

        Same path validation as ``file_pull`` — a stat leaks existence, so
        it is gated exactly as hard as a read. Replies as a command ``ack``
        (the proxy correlates via ``send_command``): ``exists`` False for a
        missing file; ``status: error`` for a policy reject (the proxy falls
        back to a full pull, which surfaces the real policy error).
        """
        from ..host import auth_paths

        path_kind = msg.get("path_kind", "agent_tree")
        agent_slug = msg.get("agent_slug", "")
        command_id = msg.get("command_id", "")
        rel_path = msg.get("path", "")

        try:
            if path_kind == "agent_tree":
                target = auth_paths.assert_agent_path_safe(
                    rel_path, agent_slug, self.config.agents_dir,
                )
            elif path_kind == "satellite_host":
                _validate_satellite_host_path(rel_path)
                _check_satellite_host_policy(rel_path)
                target = Path(rel_path)
            else:
                raise ValueError(f"unknown path_kind: {path_kind!r}")
        except ValueError as e:
            await ws.enqueue_send({
                "type": "ack",
                "command_id": command_id,
                "status": "error",
                "error": f"path not authorized: {e}",
            })
            return

        reply = {
            "type": "ack",
            "command_id": command_id,
            "status": "ok",
            "error": "",
            "exists": False,
            "size": 0,
            "mtime_ns": 0,
        }
        try:
            st = target.stat()
            if target.is_file():
                reply["exists"] = True
                reply["size"] = st.st_size
                reply["mtime_ns"] = st.st_mtime_ns
        except OSError:
            pass  # exists stays False — the proxy re-pulls and surfaces it
        await ws.enqueue_send(reply)

    # ------------------------------------------------------------------
    # MCP lifecycle (install / update / remove)
    # ------------------------------------------------------------------

    async def sync_mcps(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Install / update / remove MCPs per a batched platform directive.

        Payload shape (built by proxy/core/remote/remote_execution.py):
            {
              command_id: str,
              mcps_to_install: [
                {name, category, source, manifest_data,
                 tarball_b64, version_hash}
              ],
              mcps_to_remove: [name, …],
            }

        For each install:
          1. Write `.install-in-progress-{name}` marker.
          2. Extract tarball into `{mcps_dir}/{category}/{name}.new/`.
          3. Run the shared `mcp_installer.install_mcp` with progress
             callbacks streamed to the platform over WS.
          4. Verify `compute_version_hash` matches the expected hash.
          5. Atomic `os.replace` over existing dir, preserving
             `keys/`, `config/`, `screenshots/` (platform-agnostic user data).
          6. Remove marker.
        Failure: drop `.new`, emit `phase=failed`, keep old version.

        Removes: delete `{mcps_dir}/{category}/{name}/`.
        """
        import base64 as _b64
        import io
        import tarfile

        from .._vendored import mcp_installer

        command_id = msg.get("command_id", "")
        to_install = msg.get("mcps_to_install", []) or []
        to_remove = msg.get("mcps_to_remove", []) or []

        results: dict[str, dict] = {}

        # name + category become path components under mcps_dir (install
        # extract, atomic replace, and rmtree on removal) — the same slug
        # gate every file_push path applies, or a crafted spec gets a
        # write/delete primitive outside the MCPs tree that auth_paths
        # deliberately withholds from the proxy. The category must be one
        # the platform actually ships (MCP_CATEGORIES): rejecting a real one
        # silently drops the MCP from every session on this machine.
        from ..host import auth_paths

        def _bad_mcp_ref(name: str, category: str = "custom") -> str | None:
            if not auth_paths.is_safe_slug(name):
                return f"unsafe mcp name: {name!r}"
            if category not in MCP_CATEGORIES:
                return f"unsafe mcp category: {category!r}"
            return None

        async def _progress(ev: dict) -> None:
            # Forward progress events upstream so the pump can surface them
            # as SYSTEM events in the chat.
            try:
                await ws.enqueue_send({
                    "type": "mcp_install_progress",
                    "command_id": command_id,
                    **ev,
                })
            except Exception:
                logger.exception("progress forward failed")

        preserve_subdirs = {"keys", "config", "screenshots"}

        # Pre-warm freshly-installed MCPs (cold-start mitigation). Kicked as
        # background tasks right after each successful install so an MCP warms
        # while the NEXT
        # one installs (pipelined), then gathered before the ack. Warm-ups are
        # import/IO-bound (not CPU-bound), so concurrency is a flat 8 — with the
        # short per-MCP timeout this keeps the gather to ~1-2 waves (~10-20s)
        # instead of the old min(4,cpu) × 20s serialization (~4 min on a laptop).
        warm_tasks: list[asyncio.Task] = []
        warm_sem = asyncio.Semaphore(8)

        # --- Installs / updates ---
        for spec in to_install:
            name = spec.get("name", "")
            category = spec.get("category") or "custom"
            if err := _bad_mcp_ref(name, category):
                logger.warning("sync_mcps: rejected install spec — %s", err)
                results[name or "?"] = {"status": "error", "error": err}
                continue
            tarball_b64 = spec.get("tarball_b64", "")
            expected_hash = spec.get("version_hash", "")
            source = spec.get("source", "")
            target_root = self.config.mcps_dir / category / name
            new_root = self.config.mcps_dir / category / (name + ".new")
            marker = self.config.mcps_dir / category / (f".install-in-progress-{name}")
            marker.parent.mkdir(parents=True, exist_ok=True)

            try:
                marker.write_text(expected_hash or "pending")
                # Wipe any stale ``.new/`` from a previous failed install.
                # Critical on Windows where AV-held handles cause silent
                # rmtree failure with ignore_errors=True, leaving a broken
                # venv that the next install would extract over.
                #
                # Windows quirk: a zombie python.exe from a previous crashed
                # `uv venv` / `python -m venv` keeps an exclusive handle on
                # ``venv\Scripts\python.exe``, blocking rmtree even with
                # cmd.exe rmdir fallback. Workaround: if rmtree raises, RENAME
                # the held dir to a quarantine path with a uuid suffix —
                # Windows allows renaming open files even when delete is
                # blocked — then proceed with a fresh ``.new/``. The
                # quarantine dir is GC'd by the satellite startup sweep
                # (next reboot releases the handles).
                _force_remove_or_quarantine(
                    new_root,
                    self.config.mcps_dir / category /
                    f".quarantine-{name}-{uuid.uuid4().hex[:8]}",
                    label=name,
                )
                new_root.mkdir(parents=True, exist_ok=True)

                # Extract the tarball into .new/
                tarbytes = _b64.b64decode(tarball_b64)
                with tarfile.open(fileobj=io.BytesIO(tarbytes), mode="r:gz") as tar:
                    # Safe extract: reject absolute paths + path traversal.
                    for member in tar.getmembers():
                        mpath = (new_root / member.name).resolve()
                        if not str(mpath).startswith(str(new_root.resolve())):
                            raise ValueError(f"tarball escaped: {member.name}")
                    # filter="data" additionally blocks what the pre-check
                    # can't see: symlink members that retarget later writes
                    # outside new_root, device/fifo members, and setuid
                    # bits. Same pattern as the auto-update extract
                    # (transport/lifecycle_update.py).
                    tar.extractall(new_root, filter="data")

                # Sanity-check the extract — manifest.json is required for
                # every MCP, so missing it after extract means the tarball
                # was empty / corrupted / silently filtered by the OS
                # (Defender quarantine on Windows). Catching it here surfaces
                # a clear root-cause error instead of failing two steps
                # later with cryptic "requirements.txt not found" from uv.
                if not (new_root / "manifest.json").is_file():
                    raise RuntimeError(
                        f"tarball extracted but manifest.json missing at "
                        f"{new_root / 'manifest.json'} — tarball may be "
                        f"corrupt or Windows Defender quarantined extracted "
                        f"files. tarball size: {len(tarbytes)} bytes."
                    )

                # Run pip/npm in the .new dir. Emit progress as phase steps.
                await _progress({"mcp": name, "phase": "deps", "pct": 10, "message": "starting install"})
                # Pass sys.executable as python_bin — the default "python3"
                # doesn't exist on Windows (only python.exe / py.exe do).
                # sys.executable is the satellite venv's python, available
                # on every platform we run on.
                r = await mcp_installer.install_mcp(
                    new_root, spec.get("runtime", ""), source,
                    progress_cb=_progress,
                    uv_bin=_uv_bin_if_present(),
                    python_bin=sys.executable,
                )
                # Retry past a TRANSIENT Windows file lock: a real-time AV
                # scanner holding a freshly-written file open blocks uv/pip's
                # rename → "Access is denied" (os error 5). It's timing-
                # dependent (the same build succeeds on another run), so wipe
                # the partial venv and retry past the scan window. Mirrors
                # force_rmtree / atomic_replace, which already retry the delete
                # and swap for the same reason. Heavy MCPs (many native .pyd
                # files = lots of scanning) are the ones that hit it.
                _lock_retry = 0
                while not r.ok and _lock_retry < 3 and _is_transient_lock(r.log or ""):
                    _lock_retry += 1
                    logger.warning(
                        "sync_mcps install %s: transient file lock (likely AV) "
                        "— retry %d/3", name, _lock_retry,
                    )
                    await _progress({
                        "mcp": name, "phase": "deps", "pct": 30,
                        "message": (
                            "file locked by another process (likely antivirus) "
                            f"— retry {_lock_retry}/3"
                        ),
                    })
                    with contextlib.suppress(OSError):
                        force_rmtree(new_root / "venv")
                    await asyncio.sleep(2.0 * _lock_retry)
                    r = await mcp_installer.install_mcp(
                        new_root, spec.get("runtime", ""), source,
                        progress_cb=_progress,
                        uv_bin=_uv_bin_if_present(),
                        python_bin=sys.executable,
                    )
                if not r.ok:
                    raise RuntimeError(r.log[-500:] if r.log else "install failed")

                # Verify hash. Soft-warn on mismatch; satellite still keeps
                # the install (it works — the hash only keys the sync diff).
                # A persistent mismatch here means the platform will re-ship
                # this MCP on EVERY session (the diff compares the platform's
                # hash against ours), so it must be investigated, not ignored:
                # it indicates the install mutated a hashed file in a way the
                # platform's own install didn't (the canonical package.json
                # rewrite exists precisely to prevent that).
                if expected_hash and r.version_hash != expected_hash:
                    logger.warning(
                        "hash mismatch for %s: expected=%s actual=%s — the "
                        "platform will re-ship this MCP every session until "
                        "the hashes converge",
                        name, expected_hash, r.version_hash,
                    )

                # Preserve per-MCP user data (keys, config, screenshots) by
                # moving the preserved subdirs from the OLD dir into .new before
                # the atomic replace. Track what we moved so we can put it back
                # if the swap is blocked and we keep the old version.
                moved_subdirs: list[str] = []
                try:
                    if target_root.exists():
                        for sub in preserve_subdirs:
                            src = target_root / sub
                            if src.exists():
                                dst = new_root / sub
                                if dst.exists():
                                    shutil.rmtree(dst, ignore_errors=True)
                                shutil.move(str(src), str(dst))
                                moved_subdirs.append(sub)
                        # Robustly clear the OLD dir before the swap. A plain
                        # ignore_errors rmtree SILENTLY leaves it behind on
                        # Windows when a previous session's MCP still holds its
                        # venv python.exe; quarantine-aside on lock so the swap
                        # has a clear target. (preserve_subdirs already moved.)
                        _force_remove_or_quarantine(
                            target_root,
                            self.config.mcps_dir / category /
                            f".quarantine-{name}-{uuid.uuid4().hex[:8]}",
                            label=name,
                        )

                    # Atomic swap (.new → final). atomic_replace retries
                    # os.replace to ride out the brief AV/Defender handle window
                    # on Windows; the old dir was just cleared above.
                    atomic_replace(new_root, target_root)
                    # Post-swap path fixes for venv entry-points whose absolute
                    # interpreter path was baked at install time (Linux shebangs
                    # in venv/bin; Windows distlib .exe wrappers in
                    # venv/Scripts). Without these the entry-point fails on every
                    # spawn (Linux `bad interpreter` / Windows WinError 2).
                    _fix_shebangs_after_swap(new_root, target_root)
                    _fix_windows_exe_paths_after_swap(new_root, target_root)
                except Exception as swap_err:
                    # The swap could not replace the live install — almost always
                    # because the OLD version's MCP process is still running and
                    # holds its files (Windows blocks BOTH delete and rename →
                    # WinError 5). If a healthy old version is still in place,
                    # KEEP it: the session uses the working old version and we
                    # DEFER the update, instead of excluding the MCP and
                    # rebuilding it (~70s) on every later session. Restore any
                    # preserved subdirs we moved into .new, then drop .new. A
                    # genuine failure (no usable old version) re-raises to the
                    # outer handler → reported as an error/exclusion.
                    if not (target_root.exists() and (target_root / "manifest.json").is_file()):
                        raise
                    for sub in moved_subdirs:
                        try:
                            src = new_root / sub
                            dst = target_root / sub
                            if src.exists() and not dst.exists():
                                shutil.move(str(src), str(dst))
                        except OSError:
                            logger.warning(
                                "%s: could not restore preserved '%s' after "
                                "deferred update", name, sub,
                            )
                    with contextlib.suppress(OSError):
                        force_rmtree(new_root)
                    with contextlib.suppress(OSError):
                        marker.unlink()
                    try:
                        old_hash = mcp_installer.compute_version_hash(target_root)
                    except Exception:
                        old_hash = ""
                    logger.warning(
                        "sync_mcps: update swap for %s blocked (in use) — keeping "
                        "existing version, deferring update: %s", name, swap_err,
                    )
                    await _progress({
                        "mcp": name, "phase": "done", "pct": 100,
                        "message": "kept existing version (update deferred — in use)",
                    })
                    results[name] = {
                        "status": "deferred",
                        "version_hash": old_hash,
                        "error": f"{type(swap_err).__name__}: {swap_err}",
                    }
                    continue

                with contextlib.suppress(OSError):
                    marker.unlink()
                await _progress({"mcp": name, "phase": "done", "pct": 100, "message": "installed"})
                results[name] = {"status": "ok", "version_hash": r.version_hash}
                # Pre-warm in the background so it overlaps the next install.
                warm_tasks.append(
                    asyncio.create_task(_warm_one_mcp(target_root, name, warm_sem))
                )
            except Exception as e:
                logger.exception("sync_mcps install %s failed", name)
                # Clean up .new and marker; leave any old version intact.
                # Use force_rmtree (not ignore_errors=True) so a Windows
                # AV-held handle is retried — otherwise a partial .new
                # survives and corrupts the next install attempt.
                try:
                    force_rmtree(new_root)
                except OSError:
                    logger.warning(
                        "%s: failed to wipe stale .new dir at %s — "
                        "next install attempt may fail with broken state",
                        name, new_root,
                    )
                with contextlib.suppress(OSError):
                    marker.unlink()
                err = f"{type(e).__name__}: {e}"
                await _progress({"mcp": name, "phase": "failed", "pct": 100, "message": err, "error": err})
                results[name] = {"status": "error", "error": err}

        # --- Removals ---
        for name in to_remove:
            if err := _bad_mcp_ref(name):
                logger.warning("sync_mcps: rejected removal — %s", err)
                results[name or "?"] = {"status": "error", "error": err}
                continue
            removed = False
            for cat in ("custom", "community"):
                d = self.config.mcps_dir / cat / name
                if d.is_dir():
                    try:
                        shutil.rmtree(d)
                        removed = True
                        break
                    except OSError as e:
                        logger.warning("failed to remove %s: %s", d, e)
            results[name] = {"status": "removed" if removed else "not_found"}

        # --- Pre-warm gather ---
        # Wait for the pipelined warm-ups to finish before acking, so the
        # proxy's install_done (which awaits this ack) only fires once every
        # freshly-installed MCP has been booted + checked. One aggregate
        # "verifying" progress event lets the dashboard show "Checking MCPs…"
        # for the tail; per-MCP warm-up stays silent (status rides in the ack).
        if warm_tasks:
            await _progress({"phase": "verifying", "pct": 100, "message": "Checking MCPs…"})
            warm_outcomes = await asyncio.gather(*warm_tasks, return_exceptions=True)
            for outcome in warm_outcomes:
                if isinstance(outcome, BaseException):
                    logger.warning("warm-up task crashed: %r", outcome)
                    continue
                warm_name, warm_status = outcome
                if warm_name in results:
                    results[warm_name]["warmup"] = warm_status

        installed = _scan_installed_mcps(self.config.mcps_dir)
        await ws.enqueue_send({
            "type": "ack",
            "command_id": command_id,
            "status": "ok",
            "results": results,
            "installed_mcps": installed,
        })

    async def sync_mcps_verify(self, msg: dict, ws: "SatelliteWSClient") -> None:
        """Compute version hashes for all installed MCPs and return them.

        Called by the platform on reconnect to detect MCPs left in
        half-installed state (marker present) or that have drifted from the
        last-known version_hash. The platform queues re-install for any
        mismatches on the next session warmup.
        """
        from .._vendored import mcp_installer

        command_id = msg.get("command_id", "")
        out: dict[str, dict] = {}
        for cat_dir in self.config.mcps_dir.iterdir():
            if not cat_dir.is_dir():
                continue
            for mcp_dir in cat_dir.iterdir():
                if not mcp_dir.is_dir() or not (mcp_dir / "manifest.json").is_file():
                    continue
                name = mcp_dir.name
                marker = cat_dir / f".install-in-progress-{name}"
                healthy = not marker.exists()
                try:
                    vh = mcp_installer.compute_version_hash(mcp_dir)
                except Exception:
                    vh = ""
                    healthy = False
                out[name] = {"version_hash": vh, "healthy": healthy, "category": cat_dir.name}
        await ws.enqueue_send({
            "type": "ack",
            "command_id": command_id,
            "status": "ok",
            "mcps": out,
        })
