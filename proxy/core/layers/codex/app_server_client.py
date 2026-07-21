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
optional sandbox command prefix for the local bwrap.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
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
    ):
        self._env = env
        self._cwd = cwd
        self._sandbox_cmd_prefix = sandbox_cmd_prefix or []
        self._codex_bin = codex_bin or "codex"
        self._label = label or "codex-app-server"

        self.proc: asyncio.subprocess.Process | None = None
        self._started = False
        self._closed = False

        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._sr_handler: ServerRequestHandler | None = None

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
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,
            limit=200 * 1024 * 1024,
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
        """Answer a server→client request by id."""
        self._open_server_requests.discard(req_id)
        await self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def respond_error(self, req_id: Any, code: int, message: str) -> None:
        """Answer a server→client request with a JSON-RPC error."""
        self._open_server_requests.discard(req_id)
        await self._write({"jsonrpc": "2.0", "id": req_id,
                           "error": {"code": code, "message": message}})

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
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

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
        """Surface daemon stderr (auth errors, MCP startup failures) in logs."""
        if not self.proc or not self.proc.stderr:
            return
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
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
