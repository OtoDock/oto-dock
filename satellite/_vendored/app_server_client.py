"""AppServerClient — transport-only JSON-RPC client for ``codex app-server``.

The ``codex app-server`` subcommand runs Codex as a long-lived JSON-RPC daemon
over stdio (``--listen stdio://`` default), framed as **NDJSON** — one JSON-RPC
message per line, the same ``readline()`` → ``json.loads`` loop the Claude CLI
already uses. This client owns only the transport: spawn the daemon, run a
full-duplex background reader, correlate request/response by id, dispatch
server→client requests to a handler, and route notifications to a queue the
session drains per turn. All Codex semantics (turn lifecycle, event translation,
permissions) live in :class:`CodexAppServerSession` and the layer translator.

Three inbound message kinds the reader demuxes (verified live vs codex 0.120.0):
  * **Response** — ``{id, result}`` / ``{id, error}`` → resolves a pending request.
  * **ServerRequest** — ``{method, id, params}`` we must *answer* (approvals,
    token refresh, elicitation). Dispatched to a handler in its own task so a
    slow human-approval round-trip never blocks the reader / the turn stream.
  * **Notification** — ``{method, params}`` (no id) → pushed onto ``notif_queue``.

Mirrors the CLI persistent subprocess spawn (``core/layers/cli/session.py``):
200 MB stdout limit (big MCP-result lines), ``start_new_session`` for group-kill
on POSIX (Windows has no process groups → teardown tree-kills via taskkill),
optional sandbox command prefix for the local bwrap. The spawn itself is
injectable (``spawn``): the proxy starts the daemon off its event loop inside
a spawn slot; without one the client uses ``asyncio.create_subprocess_exec``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# Stdlib-only (no proxy ``config`` import) so this module is vendored verbatim
# into the satellite via scripts/sync-satellite-code.sh — one shared transport
# for both the local layer and the remote satellite. Edit here, then run the
# sync script + bump SHARED_APP_SERVER_CLIENT_HASH (the satellite drift-checks).

logger = logging.getLogger("codex-app-server")


def self_hash() -> str:
    """SHA256 of this module's source — used by the satellite drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""

# Default timeout (s) for control calls (thread/*, turn/interrupt, model/list).
# The turn stream itself has NO timeout — it ends on turn/completed.
_CONTROL_TIMEOUT = 30.0

# A server→client request handler: (method, params) -> result dict to send back.
ServerRequestHandler = Callable[[str, dict], Awaitable[dict]]

# Daemon-stderr signatures of the engine's INNER sandbox failing (codex 0.145.0
# vocabulary: "failed to start fs sandbox helper runtime", "bwrap: ...",
# "bubblewrap is unavailable: ...", landlock errors). Substring-matched
# case-insensitively; a rare benign hit merely logs one line at WARNING.
_SANDBOX_FAILURE_MARKERS = ("bwrap", "bubblewrap", "fs sandbox helper", "landlock")


def _looks_like_sandbox_failure(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _SANDBOX_FAILURE_MARKERS)


class AppServerError(Exception):
    """A JSON-RPC error response, or a transport failure."""


class AppServerClient:
    """Spawn + speak NDJSON JSON-RPC with one ``codex app-server`` daemon."""

    def __init__(
        self,
        *,
        env: dict[str, str],
        cwd: str | None = None,
        sandbox_cmd_prefix: list[str] | None = None,
        codex_bin: str | None = None,
        label: str = "",
        spawn: Callable[..., Awaitable[Any]] | None = None,
    ):
        self._env = env
        self._cwd = cwd
        self._sandbox_cmd_prefix = sandbox_cmd_prefix or []
        self._codex_bin = codex_bin or "codex"
        self._label = label or "codex-app-server"
        # ``spawn(argv, *, cwd, env, limit)`` → a process with the
        # ``asyncio.subprocess.Process`` surface the client uses.
        self._spawn = spawn

        self.proc: asyncio.subprocess.Process | None = None
        self._started = False
        self._closed = False

        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._sr_handler: ServerRequestHandler | None = None

        # One warning banner per daemon for inner-sandbox failures on stderr
        # (the raw bwrap error repeats per command; the pointer shouldn't).
        self._sandbox_stderr_warned = False

        # Notifications (method, params) for the session to drain per turn.
        self.notif_queue: "asyncio.Queue[tuple[str, dict]]" = asyncio.Queue()
        # Outstanding server-request ids we owe an answer for — lets the
        # session drop a pending answer if the daemon self-resolves it (the
        # `serverRequest/resolved` race).
        self._open_server_requests: set[Any] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        return (
            self._started
            and not self._closed
            and self.proc is not None
            and self.proc.returncode is None
        )

    def set_server_request_handler(self, handler: ServerRequestHandler) -> None:
        """Register the async handler that answers server→client requests."""
        self._sr_handler = handler

    async def start(self, init_params: dict) -> dict:
        """Spawn the daemon, start the reader, run the ``initialize`` handshake.

        Returns the ``initialize`` result. Raises on spawn / handshake failure.
        """
        if self._started:
            raise RuntimeError(f"{self._label}: already started")

        cmd = [*self._sandbox_cmd_prefix, self._codex_bin, "app-server"]
        logger.info(f"{self._label}: spawning {' '.join(cmd[:4])}... (cwd={self._cwd})")

        # 200 MB stdout buffer — matches the CLI/Codex subprocess. Default 64 KB
        # readline raises LimitOverrunError on large MCP-result lines and kills
        # the reader mid-turn (see core/layers/cli/session.py).
        limit = 200 * 1024 * 1024
        if self._spawn is not None:
            self.proc = await self._spawn(cmd, cwd=self._cwd, env=self._env, limit=limit)
        else:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
                start_new_session=True,
                limit=limit,
            )
        self._started = True
        logger.info(f"{self._label}: daemon pid={self.proc.pid}")

        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"{self._label}-reader",
        )
        asyncio.create_task(
            self._drain_stderr(), name=f"{self._label}-stderr",
        )

        result = await self.request("initialize", init_params)
        logger.info(
            f"{self._label}: initialized "
            f"(codexHome={result.get('codexHome')}, os={result.get('platformOs')})"
        )
        return result

    async def close(self) -> None:
        """Terminate the daemon and tear down the reader."""
        if self._closed:
            return
        self._closed = True
        # Fail every pending request so awaiters don't hang.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(AppServerError(f"{self._label}: closed"))
        self._pending.clear()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        await self._kill_proc()

    # ------------------------------------------------------------------
    # Requests / responses (client → server)
    # ------------------------------------------------------------------

    async def request(
        self, method: str, params: dict | None = None,
        *, timeout: float | None = _CONTROL_TIMEOUT,
    ) -> dict:
        """Send a ClientRequest and await its response.

        ``timeout=None`` for calls with no bound (none today — the turn stream
        is consumed via notifications, not a long request).
        """
        if not self.is_alive:
            raise AppServerError(f"{self._label}: daemon not alive")
        self._next_id += 1
        mid = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self._write({"jsonrpc": "2.0", "id": mid, "method": method,
                           "params": params if params is not None else {}})
        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise AppServerError(f"{self._label}: {method} timed out after {timeout}s")
        finally:
            self._pending.pop(mid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        """Send a client→server notification (no response expected)."""
        await self._write({"jsonrpc": "2.0", "method": method,
                           "params": params if params is not None else {}})

    async def respond(self, req_id: Any, result: dict) -> None:
        """Answer a server→client request by id.

        Best-effort: a held request (approval, ``request_user_input``) can wait
        on a human for minutes, and the daemon may be gone by the time the
        answer arrives — its transport is then closed and there is nobody left
        to receive the result. Log it and return; raising here would only kill
        the detached dispatch task with an unhandled traceback.
        """
        self._open_server_requests.discard(req_id)
        try:
            await self._write({"jsonrpc": "2.0", "id": req_id, "result": result})
        except AppServerError as e:
            logger.warning(
                f"{self._label}: answer to server-request {req_id} not "
                f"delivered — daemon gone ({e})"
            )

    async def respond_error(self, req_id: Any, code: int, message: str) -> None:
        """Answer a server→client request with a JSON-RPC error (best-effort —
        see :meth:`respond`)."""
        self._open_server_requests.discard(req_id)
        try:
            await self._write({"jsonrpc": "2.0", "id": req_id,
                               "error": {"code": code, "message": message}})
        except AppServerError as e:
            logger.warning(
                f"{self._label}: error answer to server-request {req_id} not "
                f"delivered — daemon gone ({e})"
            )

    def is_server_request_open(self, req_id: Any) -> bool:
        """True while we still owe an answer (false once we/the daemon resolved it)."""
        return req_id in self._open_server_requests

    # ------------------------------------------------------------------
    # Reader (server → client)
    # ------------------------------------------------------------------

    async def _write(self, obj: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise AppServerError(f"{self._label}: no stdin")
        line = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()
        except (RuntimeError, OSError) as e:
            # A dead/reaped daemon leaves a CLOSED transport: asyncio raises a
            # bare RuntimeError ("unable to perform operation on <WriteUnix
            # Transport closed=True…>"), the pipe a BrokenPipeError. Normalize
            # both to AppServerError so every caller sees one transport failure
            # type instead of a raw event-loop traceback.
            raise AppServerError(f"{self._label}: write failed: {e}") from e

    async def _reader_loop(self) -> None:
        """Background full-duplex reader: demux responses / requests / notifs."""
        assert self.proc and self.proc.stdout
        try:
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break  # EOF — daemon exited
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped)
                except json.JSONDecodeError:
                    # app-server keeps stderr separate, so non-JSON on stdout is
                    # rare; skip rather than crash the reader.
                    logger.debug(f"{self._label}: non-JSON stdout: {stripped[:200]!r}")
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{self._label}: reader loop error: {e}")
        finally:
            # Daemon died — fail pending requests + signal end-of-stream so a
            # turn consumer unblocks.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(AppServerError(f"{self._label}: daemon exited"))
            self._pending.clear()
            self.notif_queue.put_nowait(("__daemon_exit__", {}))

    def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        method = msg.get("method")
        if mid is not None and method is None:
            # Response to a request we sent.
            fut = self._pending.pop(mid, None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(AppServerError(
                    f"{method or 'request'} failed: {err.get('message', err)}"))
            else:
                fut.set_result(msg.get("result") or {})
        elif method is not None and mid is not None:
            # Server→client request — dispatch in its own task so a slow
            # approval never blocks the reader or the turn stream.
            self._open_server_requests.add(mid)
            asyncio.create_task(
                self._dispatch_server_request(method, mid, msg.get("params") or {}),
                name=f"{self._label}-sr",
            )
        elif method is not None:
            # Notification — route to the per-turn queue.
            self.notif_queue.put_nowait((method, msg.get("params") or {}))

    async def _dispatch_server_request(self, method: str, req_id: Any, params: dict) -> None:
        if self._sr_handler is None:
            logger.warning(f"{self._label}: no handler for server-request {method}; declining")
            await self.respond_error(req_id, -32601, "no handler registered")
            return
        try:
            result = await self._sr_handler(method, params)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{self._label}: server-request {method} handler error: {e}")
            if self.is_server_request_open(req_id):
                await self.respond_error(req_id, -32603, f"handler error: {e}")
            return
        # The daemon may have self-resolved it meanwhile
        # (serverRequest/resolved) — only answer if we still own it.
        if self.is_server_request_open(req_id):
            await self.respond(req_id, result)

    # ------------------------------------------------------------------
    # Process teardown
    # ------------------------------------------------------------------

    async def _drain_stderr(self) -> None:
        """Surface daemon stderr (auth errors, MCP startup failures) in logs.

        Inner-sandbox failures are promoted to WARNING, untruncated: codex's
        nested bwrap ("fs sandbox helper") dying on userns-restricted hosts
        used to drown at info[:400], leaving nothing operator-visible behind a
        model-paraphrased "the local sandbox could not start" (2026-08-12
        private-install incident). Best-effort — the failure text may also
        travel in-band in turn results; this catches the daemon-stderr copy.
        """
        if not self.proc or not self.proc.stderr:
            return
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                if _looks_like_sandbox_failure(text):
                    hint = "" if self._sandbox_stderr_warned else (
                        " — the Codex inner sandbox is failing; commands "
                        "degrade to unsandboxed retries behind permission "
                        "prompts. Run scripts/sandbox-doctor.sh on this host."
                    )
                    self._sandbox_stderr_warned = True
                    logger.warning(
                        f"{self._label}[stderr][sandbox]: {text}{hint}"
                    )
                else:
                    logger.info(f"{self._label}[stderr]: {text[:400]}")
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _kill_proc(self) -> None:
        proc = self.proc
        if not proc or proc.returncode is not None:
            return
        with contextlib.suppress(OSError, AttributeError):
            proc.stdin.close()  # type: ignore[union-attr]
        # POSIX: SIGTERM the whole process group — start_new_session=True put
        # the daemon + its MCP children in their own group, so the group kill
        # reaps the children too. Windows has neither os.killpg/os.getpgid (they
        # raise AttributeError, NOT OSError — so they must be guarded, not
        # caught) nor POSIX process groups here; terminate the daemon directly
        # and tree-kill on escalation so MCP children don't leak.
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                with contextlib.suppress(OSError, ProcessLookupError):
                    proc.terminate()
        else:
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        # Escalate to a hard kill.
        if hasattr(os, "killpg"):
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            # Windows: terminate()/kill() only hit the daemon itself, so walk
            # and force-kill the child tree via taskkill first.
            await self._taskkill_tree(proc.pid)
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.kill()

    @staticmethod
    async def _taskkill_tree(pid: int) -> None:
        """Windows-only: force-kill ``pid`` and its child tree via taskkill."""
        try:
            tk = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(tk.wait(), timeout=5)
        except (OSError, asyncio.TimeoutError):
            pass


# ---------------------------------------------------------------------------
# Pre-turn MCP warm gate — one implementation for the proxy session and the
# satellite twin (both drain ``notif_queue`` before their first turn).
# ---------------------------------------------------------------------------

# app-server's per-server startup notification (v2 ``McpServerStatusUpdated``):
# params ``{threadId, name, status, error?, failureReason?}``; ``status`` is
# ``starting`` first, then ``ready`` / ``failed`` / ``cancelled`` — Codex fails
# a server itself at its ``startup_timeout_sec``. Identical on 0.149.1 and
# 0.153.4 (0.153.x adds ``runtimeStatus`` next to it).
MCP_STARTUP_METHOD = "mcpServer/startupStatus/updated"
MCP_TERMINAL_STATUSES = frozenset({"ready", "failed", "cancelled"})

# ``[mcp_servers.<name>]`` table headers (bare or quoted key); sub-tables such
# as ``[mcp_servers.x.env]`` / ``[mcp_servers.x.http_headers]`` do not match.
_MCP_TABLE_RE = re.compile(
    r'^\s*\[mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\]\s*$', re.MULTILINE,
)


def mcp_server_names_from_toml(toml_text: str) -> list[str]:
    """The MCP server names a Codex config.toml declares, in order — the
    servers the warm gate waits for."""
    names: list[str] = []
    for m in _MCP_TABLE_RE.finditer(toml_text or ""):
        name = m.group(1) or m.group(2)
        if name and name not in names:
            names.append(name)
    return names


@dataclass
class McpWarmResult:
    """What the gate saw: every reported status, the expected servers still
    without a terminal status when it returned, the reporters it had not
    expected (Codex's own hosted MCPs, e.g. the ChatGPT apps connector — never
    waited for), the wall time, and whether the daemon died meanwhile."""
    statuses: dict[str, str] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    daemon_exited: bool = False

    @property
    def ready(self) -> list[str]:
        return sorted(n for n, s in self.statuses.items() if s == "ready")

    @property
    def failed(self) -> list[str]:
        return sorted(n for n, s in self.statuses.items() if s in ("failed", "cancelled"))


async def wait_for_mcp_startup(
    client: "AppServerClient",
    expected: "list[str] | set[str] | tuple[str, ...]",
    *,
    cap_s: float,
    poll_s: float = 0.5,
    no_status_grace_s: float = 5.0,
) -> McpWarmResult:
    """Drain ``client.notif_queue`` until every EXPECTED server (the
    ``[mcp_servers.*]`` the session wrote) has a terminal startup status, or
    ``cap_s`` passes.

    Codex sends the model the servers that are CONNECTED when a turn starts,
    so a gate that leaves while servers are still ``starting`` costs the first
    turn its tools and changes the tool list between turns — every prefix
    cache misses, minutes of re-prefill on a local model (a Windows desktop
    with 21 stdio servers on Ollama, 2026-09-08: turn 1 left with 4 servers,
    73 → 323 tools, +66k tokens on turn 2). The previous gate stopped at the
    first ``poll_s`` silence, which the burst-then-pause pattern of many stdio
    servers satisfied long before they were up.

    ``poll_s`` is the queue poll (and the quick exit for a session with no
    expected servers). A daemon that reports nothing for the expected servers
    within ``no_status_grace_s`` ends the wait early (a renamed notification on
    a Codex bump, or only Codex's own hosted MCPs reporting) instead of burning
    the cap. Anything else on the queue is pre-turn noise (thread/started,
    status changes) and is dropped — the caller must be the queue's sole
    consumer until it returns.
    """
    q = client.notif_queue
    expected_set = {str(n) for n in expected if n}
    statuses: dict[str, str] = {}
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + cap_s
    daemon_exited = False

    def _pending() -> set[str]:
        return {
            n for n in expected_set
            if statuses.get(n, "starting") not in MCP_TERMINAL_STATUSES
        }

    def _expected_reported() -> bool:
        return any(n in expected_set for n in statuses)

    while True:
        now = loop.time()
        if now >= deadline:
            break
        try:
            method, params = await asyncio.wait_for(
                q.get(), timeout=min(poll_s, deadline - now),
            )
        except asyncio.TimeoutError:
            if not _pending():
                break  # quiet and nothing outstanding → warm
            if not _expected_reported() and loop.time() - started >= no_status_grace_s:
                break  # the daemon reports nothing for what we configured
            continue
        if method == "__daemon_exit__":
            daemon_exited = True
            break
        if method == MCP_STARTUP_METHOD:
            statuses[str(params.get("name") or "?")] = str(params.get("status") or "?")
            if not _pending():
                break
    return McpWarmResult(
        statuses=statuses,
        pending=sorted(_pending()),
        unexpected=sorted(set(statuses) - expected_set),
        elapsed=loop.time() - started,
        daemon_exited=daemon_exited,
    )
