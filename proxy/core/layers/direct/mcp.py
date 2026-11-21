"""MCP server manager for direct LLM sessions.

Manages MCP server lifecycles (stdio/SSE), discovers tools, namespaces them
as mcp__servername__toolname (matching Claude Code convention), and executes
tool calls in parallel.

Architecture:
- AgentMCPManager: manages all MCP servers for one agent session
- MCPPool: global singleton mapping session_id -> AgentMCPManager with idle reaping
"""

import asyncio
import contextlib
import functools
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, cast

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

if TYPE_CHECKING:
    from mcp.types import AudioContent, ImageContent

import config
from core import placement
from core.stdio_path_interceptor import STANDIN_ENV, marker_fingerprint

logger = logging.getLogger("mcp-manager")

#: This engine's id, as its capabilities name it (``layer.py``).
ENGINE_ID = "direct-llm"

# Timeout for individual tool calls (seconds). The default bounds an
# app-action click (a click must never wait longer); every Direct session,
# a call's too, uses the chat figure (Codex's 300 s) so a document render or
# a browser flow behind a sidecar MCP can finish.
TOOL_CALL_TIMEOUT = 60
TOOL_CALL_TIMEOUT_CHAT = 300


class TimedOut(str):
    """A call's text when the client stopped waiting at its cap: a model
    reads it as the text it is, and ``execute_tools`` marks the call
    ``timed_out`` for a caller that answers someone (an app's button)."""

# How long one MCP server may take to connect and list its tools before the
# session starts without it: the servers start together, so this bounds the
# whole start too. Above a cold start of a large stdio venv, below the app
# actions' 90 s backstop (``services/apps/headless_exec.py``).
MCP_START_TIMEOUT_S = 60.0
# On a call an HTTP server (a tool in its own container, a vendor's) gets a
# shorter start: one that does not answer is left out and the call starts.
PHONE_HTTP_START_S = 15.0

# The registry's Claude-JSON config format spells streamable HTTP as "http"
# (the CLI's alias for it) — both spellings are the same transport here. The
# two dispatch sites below must share this set: they have different type
# defaults ("stdio" vs "sse") and would drift apart with inline literals.
_REMOTE_STREAMABLE = {"streamable-http", "http"}

# --- Dedicated MCP I/O thread ---
# All MCP server subprocess I/O (start, tool calls, close) runs on this
# thread's event loop so it doesn't contend with the proxy's main HTTP loop.

_mcp_loop: asyncio.AbstractEventLoop | None = None
_mcp_thread: threading.Thread | None = None
_mcp_thread_lock = threading.Lock()


def session_gone(exc: BaseException) -> bool:
    """The HTTP transport's own error for a server that no longer knows the
    session — "Session terminated" on a 404, after the server evicted an
    idle session — as against a tool's failure a live server answered."""
    text = str(exc).lower()
    return "session terminated" in text or "session not found" in text


def _start_mcp_thread() -> asyncio.AbstractEventLoop:
    """Start the dedicated MCP I/O thread (idempotent, thread-safe)."""
    global _mcp_loop, _mcp_thread
    with _mcp_thread_lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return _mcp_loop

        _mcp_loop = asyncio.new_event_loop()
        _mcp_thread = threading.Thread(
            target=_mcp_loop.run_forever,
            name="mcp-io",
            daemon=True,
        )
        _mcp_thread.start()
        logger.info("MCP I/O thread started")
        return _mcp_loop


async def _run_on_mcp_loop(coro):
    """Dispatch a coroutine to the MCP I/O thread and await its result."""
    loop = _start_mcp_thread()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return await asyncio.wrap_future(future)


def stop_mcp_thread(join_timeout: float = 2.0) -> None:
    """Stop the MCP I/O loop at proxy shutdown (idempotent). The thread itself
    is daemon, but its lazily-created default-executor workers are NOT — left
    running they stall interpreter exit at the atexit thread join, after
    uvicorn already logged its shutdown. ``loop.close()`` is skipped when the
    thread doesn't stop within the timeout (closing a running loop raises)."""
    global _mcp_loop, _mcp_thread
    with _mcp_thread_lock:
        loop, thread = _mcp_loop, _mcp_thread
        _mcp_loop, _mcp_thread = None, None
    if loop is None:
        return
    with contextlib.suppress(Exception):
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None:
        thread.join(timeout=join_timeout)
    if thread is None or not thread.is_alive():
        with contextlib.suppress(Exception):
            loop.close()
        logger.info("MCP I/O thread stopped")
    else:
        logger.warning("MCP I/O thread did not stop in %.1fs", join_timeout)


# The MCP content block kinds that carry bytes (the MCP schema's words).
_MCP_FILE_BLOCKS = ("image", "resource")


def _marker_of(server_config: dict) -> dict | None:
    """The stand-in's marker on an entry (``OTO_TOOL_STANDIN``), or None."""
    raw = (server_config.get("env") or {}).get(STANDIN_ENV)
    try:
        marker = json.loads(raw) if raw else None
    except ValueError:
        return None
    return marker if isinstance(marker, dict) else None


def _log_warm_failure(conn: "MCPServerConnection", task: asyncio.Task) -> None:
    """A call's tool that did not start beside the model: one line (its first
    call tries again and says why). A session that ended meanwhile says
    nothing."""
    if task.cancelled() or conn.closed:
        return
    error = task.exception()
    refused = task.result() if error is None else str(error)
    if refused:
        logger.info("MCP server '%s' did not start with its call: %s", conn.name, refused)


def _defs_key(defs: list[dict]) -> str:
    """What a list says about each tool, as the model is given it."""
    return json.dumps(sorted([d.get("name"), d.get("description"), d.get("input_schema")] for d in defs),
                      sort_keys=True, default=str)


class MCPServerConnection:
    """Wraps a single MCP server connection (stdio or SSE)."""

    #: Whether the tools' own annotations are kept after a list. A program
    #: of the platform's own (``sandbox_program.SandboxProgram``) has none.
    records_hints = True

    def __init__(self, name: str, server_config: dict,
                 credential_env: dict[str, str] | None = None,
                 session_id: str = "",
                 sandbox_builder=None,
                 agent_name: str = "",
                 tool_timeout: int = TOOL_CALL_TIMEOUT,
                 start_timeout: float | None = None,
                 machine_id: str = "",
                 main_loop: asyncio.AbstractEventLoop | None = None):
        self.name = name
        self.config = server_config
        self.credential_env = credential_env
        self.session_id = session_id
        self.sandbox_builder = sandbox_builder  # SandboxBuilder or None
        self.agent_name = agent_name
        self.tool_timeout = tool_timeout  # per-call cap, set by the manager
        # A remote server's bound to connect and list, set by the manager
        # for a call; None (and every stdio server) is MCP_START_TIMEOUT_S.
        self.start_timeout = start_timeout
        # The compute machine a stdio server's process runs on ("": this
        # host) and the loop its link lives on (core/remote/tool_relay.py).
        self.machine_id = machine_id
        self.main_loop = main_loop
        # Its open failed because the machine's link was gone: the relay
        # keeps no child to say so (``AgentMCPManager.tools_lost``).
        self.link_lost = False
        # Why its open was refused before anything started (the machine has
        # not built the tool yet): the sentence its call answers.
        self.refused = ""
        # Set by start(), with _closing; what runs after it casts the reads.
        self.session: ClientSession | None = None
        self.tools: list[dict] = []  # Anthropic API format
        # Set when a call fails at the TRANSPORT level (closed pipe, dead
        # subprocess) — the tools stay listed (discovered at init) but every
        # call would fail. ``AgentMCPManager.has_tool`` reports a dead
        # server's tools as missing so the headless executor's self-heal
        # rebuilds instead of erroring forever (found live: uptime-kuma died
        # on startup auth while a firewall rule was missing, and the warm
        # pooled manager kept returning empty errors long after the rule was
        # fixed — the 60s dashboard auto-refresh never let it idle out).
        self.dead = False
        self._cm_stack: list = []  # context manager stack for cleanup
        # Single owner task that holds the (anyio-based) context managers open
        # for this connection's whole lifetime — see start() for why.
        self._closing: asyncio.Event | None = None
        self._owner_task: asyncio.Task | None = None
        # A program that waits for the agent's first call (the stand-in's
        # marker, ``services/mcp/tool_listing.py``): its tools come from the
        # answers saved for the session's scope, kept apart from ``tools`` (a
        # failed start's teardown clears that one) until ``ensure_started``.
        self.pending = False
        self.saved_tools: list[dict] = []
        # A call's tool (a warm marker): its program starts with the session,
        # beside the model, and the session's estimate counted it, so no
        # start of it is judged or counted again.
        self.warm = False
        self.warming: asyncio.Task | None = None
        self.closed = False
        self._start_lock: asyncio.Lock | None = None
        # Where a start keeps what the program answered: ``(scope, fp)``.
        self.capture: tuple[tuple[str, str], str] | None = None
        # What a start with the session kept, with its ``(scope, fp)``: the
        # program then rests until its first call (``rest_until_called``).
        self.kept: tuple[dict, tuple[str, str], str] | None = None

    async def start(self) -> None:
        """Connect to the MCP server and discover tools.

        The transport's client and ``ClientSession`` are anyio context managers, and
        anyio requires a cancel scope to be EXITED in the same task that
        entered it. So we run the whole lifecycle — enter → hold open → exit —
        inside one dedicated owner task (``_serve``), and only return here once
        that task reports the server ready (or failed). ``close()`` signals the
        owner task to tear down, so ``__aexit__`` runs in the entering task.

        Entering the contexts here and exiting them from ``close()``'s task (as
        the previous code did, via separate ``asyncio.gather`` children on the
        mcp-io loop) raised ``RuntimeError: Attempted to exit cancel scope in a
        different task`` — swallowed at debug level — which orphaned the stdio
        reader. That reader then busy-looped on the closed pipe's EOF, pegging
        the mcp-io event loop at 100% CPU (starving the main loop) and leaking
        the MCP subprocess. This task-consistent ownership is the fix.
        """
        loop = asyncio.get_running_loop()
        self._closing = asyncio.Event()
        ready: asyncio.Future = loop.create_future()
        self._owner_task = loop.create_task(self._serve(ready))
        await ready  # blocks until tools are discovered (or startup failed)

    async def _serve(self, ready: asyncio.Future) -> None:
        """Owner task: enter the contexts, discover tools, then hold them open
        until close() signals teardown. Enter and exit both happen here, in one
        task, satisfying anyio's same-task cancel-scope requirement."""
        server_type = self.config.get("type", "stdio")
        local = server_type == "stdio"
        bound = MCP_START_TIMEOUT_S if local or self.start_timeout is None else self.start_timeout
        try:
            try:
                async with asyncio.timeout(bound):
                    if local:
                        await self._start_stdio()
                    elif server_type == "sse" or server_type in _REMOTE_STREAMABLE:
                        await self._start_remote()
                    else:
                        logger.warning("Unknown MCP server type '%s' for %s", server_type, self.name)
                        return

                    # Initialize and discover tools
                    result = await cast("ClientSession", self.session).initialize()
                    logger.info(
                        "MCP server '%s' initialized "
                        "(protocol=%s)", self.name, result.protocolVersion
                    )

                    tools_result = await cast("ClientSession", self.session).list_tools()
            except TimeoutError:
                # A server that never answers (an address that does not
                # resolve, a process that never speaks) is left out like a
                # failed one; the contexts it entered are exited below.
                raise RuntimeError(
                    f"did not answer within {bound:.0f} s; the session starts without it"
                ) from None
            # Set at once: a start in flight leaves the list the model has
            # (a call's tool lists its saved answers while its program starts).
            self.tools = [{
                "name": f"mcp__{self.name}__{tool.name}",
                "description": tool.description or "",
                "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
                "_original_name": tool.name,
                "_server": self.name,
            } for tool in tools_result.tools]

            logger.info(
                "MCP server '%s': %s tools discovered",
                self.name, len(self.tools)
            )
            # The tools' own annotations: the permission tier of a tool no
            # manifest rule names (services/mcp/mcp_permissions).
            if self.records_hints:
                from services.mcp.mcp_permissions import record_tool_hints
                await asyncio.to_thread(record_tool_hints, self.name, [
                    (tool.name, tool.annotations.model_dump(exclude_none=True) if tool.annotations else None)
                    for tool in tools_result.tools])
            if self.capture is not None:
                await self._keep_answers(result, tools_result)
        except Exception as e:  # noqa: BLE001  # the mcp client raises anything at start: the server gets no tools
            # Error isolation: a failed server just has no tools (matches the
            # previous behaviour). Tear down whatever was entered, in THIS task.
            logger.error("Failed to start MCP server '%s': %s", self.name, e)
            await self._teardown()
            return
        finally:
            # start() waits on this: whatever ends the start (a cancel the
            # client's own task group leaves behind included) resolves it.
            if not ready.done():
                ready.set_result(None)

        # Ready (start() unblocked above): park until close() signals teardown.
        try:
            await cast("asyncio.Event", self._closing).wait()
        finally:
            await self._teardown()

    async def _keep_answers(self, init, tools_result) -> None:
        """What the program answered, kept for the session's scope (the
        platform's own client read it), when nothing was saved or the saved
        list differs. Best effort."""
        from services.mcp import tool_listing
        scope, fp = cast("tuple", self.capture)
        self.capture = None
        tools = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools_result.tools]
        if self.saved_tools and _defs_key(self.saved_tools) == _defs_key(self._defs(tools)):
            return
        listing = {"initialize": init.model_dump(mode="json", by_alias=True, exclude_none=True),
                   "lists": {"tools/list": tools}}
        try:
            if await asyncio.to_thread(tool_listing.keep, scope, self.name, fp, listing):
                self.kept = (listing, scope, fp)
        except Exception:  # noqa: BLE001  # a store that fails keeps nothing: the next session starts it again
            logger.warning("MCP server '%s': its answers were not kept", self.name, exc_info=True)

    async def rest_until_called(self) -> bool:
        """A program the session started only to answer its lists, whose
        answers were kept: stopped, its tools still listed from them, to start
        again at its first call (the CLI engines' stand-in stops it the same
        way, ``StandIn._maybe_stop``). False when it runs on: nothing was
        kept, or it is a call's tool."""
        kept, self.kept = self.kept, None
        if kept is None or self.warm or self.session is None or self.closed:
            return False
        await self._stop_owner()
        self.wait_for_first_call(*kept)
        logger.info("tool %s of %s stopped until its first call: its answers are kept",
                    self.name, (self.session_id or "")[:8])
        return True

    def _defs(self, tools: list[dict]) -> list[dict]:
        """MCP tool objects as this client lists them to the model."""
        return [{
            "name": f"mcp__{self.name}__{t.get('name')}",
            "description": t.get("description") or "",
            "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            "_original_name": t.get("name"),
            "_server": self.name,
        } for t in tools if isinstance(t, dict) and t.get("name")]

    def wait_for_first_call(self, saved: dict, scope: tuple[str, str], fp: str) -> None:
        """Answer from the saved listing and start at the first call."""
        self.saved_tools = self._defs(((saved.get("lists") or {}).get("tools/list")) or [])
        self.tools = list(self.saved_tools)
        self.pending = True
        self.capture = (scope, fp)

    async def ensure_started(self) -> str:
        """Start a program that waited for its first call: "" once it runs,
        else the sentence the call answers (the connection then keeps waiting,
        the next call tries again). The session's box judges the start."""
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if not self.pending:
                return ""
            label = str((_marker_of(self.config) or {}).get("label") or self.name)
            if self.closed:
                return f"The {label} tool did not start. Its session has ended."
            admission = await self._admission()
            if admission is not None and not admission:
                return str(admission.user_message or "")
            began = time.monotonic()
            await self.start()
            if self.session is None:
                await self._teardown()
                self.tools = list(self.saved_tools)
                if admission:
                    await self._give_back()
                return self.refused or f"The {label} tool did not start. Its program stopped before it answered."
            self.pending = False
            if self.closed:
                return f"The {label} tool did not start. Its session has ended."
            logger.info("tool %s of %s started on its first call in %d ms", self.name,
                        (self.session_id or "")[:8], int((time.monotonic() - began) * 1000))
            # The session keeps the list it was given; a changed one is kept
            # for its next session (``_keep_answers``).
            self.tools = list(self.saved_tools)
            return ""

    async def _admission(self):
        """The admission of a late start: judged on the session's box, or
        None for a call's tool (the session's estimate counted it)."""
        return None if self.warm else await self._admit()

    async def _stop_owner(self) -> None:
        """The owner task told to leave its contexts, and waited for: the
        program ends and its place on the machine is free."""
        if self._closing is not None:
            self._closing.set()
        task, self._owner_task = self._owner_task, None
        if task is not None:
            with contextlib.suppress(Exception):
                await task

    async def _admit(self, *, judged: bool = True):
        """The session's box asked for room for one more program, on the
        proxy's own loop (the admission's condition lives there): judged at
        a late start, counted for a program that starts with the session.
        None when no loop is there to ask."""
        if self.main_loop is None or not self.session_id:
            return None
        from core import concurrency
        fut = asyncio.run_coroutine_threadsafe(
            concurrency.grow(self.session_id, judged=judged, server=self.name), self.main_loop)
        return await asyncio.wrap_future(fut)

    async def _give_back(self) -> None:
        """A start the session's box counted that did not happen: its
        program given back (``concurrency.shrink``), on the proxy's loop."""
        if self.main_loop is None or not self.session_id:
            return
        from core import concurrency
        fut = asyncio.run_coroutine_threadsafe(concurrency.shrink(self.session_id, self.name), self.main_loop)
        await asyncio.wrap_future(fut)

    async def _teardown(self) -> None:
        """Exit all context managers, in reverse order. MUST run in the owner
        task (``_serve``) so anyio cancel scopes are exited where they began."""
        for cm in reversed(self._cm_stack):
            try:
                await cm.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001  # exiting an mcp client context of a server going away
                logger.debug("Error closing MCP server '%s': %s", self.name, e)
        self._cm_stack.clear()
        self.session = None
        self.tools.clear()

    def base_env(self) -> dict[str, str]:
        """The env a stdio program gets before its secrets: the session's
        own and the entry's (the stand-in's marker aside). Its fingerprint
        reads this one, so the exit's per-build placeholders never move it."""
        from core.sandbox.env_builder import build_session_env
        env = build_session_env(
            self.session_id or "", self.agent_name,
            credential_env=self.credential_env,
            username=self.sandbox_builder.cfg.username if self.sandbox_builder else "",
            user_role=self.sandbox_builder.cfg.role if self.sandbox_builder else "",
        )
        env.update(self.config.get("env", {}))
        env.pop(STANDIN_ENV, None)
        return env

    async def _start_stdio(self) -> None:
        """A stdio MCP server's process, on the session's compute machine."""
        command = self.config["command"]
        args = self.config.get("args", [])

        # Minimal env with the session-scoped auth token and the OTO_* vars;
        # username and role come from the sandbox config (matches CLI/Codex,
        # so OTO_* values are consistent), then the entry's own.
        env = self.base_env()

        # Credential broker: this MCP's brokered secrets, read straight from
        # the per-session store provisioned in _start_impl (the process has
        # no stdio interceptor to fetch them over HTTP).
        from core.credentials import credential_exit, mcp_broker
        _bundle = mcp_broker.get(self.session_id, self.name)
        if _bundle:
            env.update(_bundle.env)
        # The credential exit's variables: a placeholder in
        # the bundle is swapped for its value only through the exit. A tool
        # whose exit entry names a host the session keeps direct (an
        # engine's) starts with its own list, as the spawn fetch gives it.
        env.update(credential_exit.env_of(self.session_id or ""))
        if _bundle:
            env.update(credential_exit.own_direct(self.session_id or "", _bundle.exit, env))

        if not self.machine_id:
            # The platform starts no tool itself.
            self.refused = placement.NO_COMPUTE_SENTENCE
            raise RuntimeError(self.refused)
        await self._start_relayed(command, args, env)

    async def _start_relayed(self, command: str, args: list, env: dict[str, str]) -> None:
        """The stdio server's process on the session's compute machine,
        behind the same pair of streams ``stdio_client`` gives: the MCP
        client here does not change. The machine renders the session's
        sandbox description around the argv, so a server never runs bare."""
        if self.sandbox_builder is None or self.main_loop is None:
            raise RuntimeError(f"MCP server '{self.name}' has no sandbox to run in on a compute machine")
        from mcp.client.stdio import get_default_environment

        from core.remote import tool_relay
        from services.remote import machine_tools
        # A tool the machine builds itself opens only once it is built there
        # at the platform's version: otherwise the call answers one sentence
        # and the install is asked for.
        tool_dir, self.refused = machine_tools.relay_gate(self.machine_id, self.name)
        if self.refused:
            self.main_loop.call_soon_threadsafe(machine_tools.ask, self.machine_id)
            raise RuntimeError(self.refused)
        env.update(self.sandbox_builder.get_env_overrides())
        cm = tool_relay.relay_client(
            self.machine_id, self.session_id, self.name,
            description=self._relay_description(),
            argv=[command, *args],
            env={**get_default_environment(), **env},
            main_loop=self.main_loop,
            on_end=self._relay_ended,
            exit_block=self._relay_exit_block(),
            tool_dir=tool_dir,
        )
        try:
            streams = await cm.__aenter__()
        except tool_relay.LinkLost:
            self.link_lost = True
            raise
        except RuntimeError as e:
            # The machine's own answer when the folder is not there: the
            # memo said it was, so it forgets the tool and a run builds it.
            if machine_tools.INSTALLING_SENTENCE in str(e):
                self.refused = machine_tools.INSTALLING_SENTENCE
                machine_tools.note_not_there(self.machine_id, self.name)
                self.main_loop.call_soon_threadsafe(machine_tools.ask, self.machine_id)
            raise
        self._cm_stack.append(cm)
        session_cm = ClientSession(*streams)
        self.session = await session_cm.__aenter__()
        self._cm_stack.append(session_cm)

    def _relay_description(self) -> dict:
        """The sandbox the open is rendered in: the session's own."""
        return self.sandbox_builder.describe().to_dict()

    def _relay_exit_block(self) -> dict | None:
        """The session's exit door for the machine: the same
        block a start payload carries, on each tool's open."""
        from core.credentials import credential_exit, mcp_broker
        from core.remote import mcp_gateway_push
        secret = credential_exit.machine_secret(self.session_id)
        return (credential_exit.machine_block(self.session_id, mcp_broker.bundles_of(self.session_id),
                                              mcp_gateway_push.token_hash_of(secret)) if secret else None)

    def _relay_ended(self, _lost: bool) -> None:
        """The relayed process is gone. A call then fails with a ``McpError``
        that alone would not mark the server dead, and an app button heals
        on the mark."""
        self.dead = True

    async def _start_remote(self) -> None:
        """Connect to a remote MCP server (SSE or streamable HTTP)."""
        url = self.config.get("url", "")
        if not url:
            raise ValueError(f"Remote MCP server '{self.name}' has no URL")

        # Inject session_id for Docker MCPs that need sandbox path resolution
        if self.session_id and "session_id=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}session_id={self.session_id}"
        # A credential-gateway entry is dialled on the internal listener
        # (the only listener the route answers on), as the sandbox splice
        # lands sessions there; this layer runs in the proxy process and
        # has no splice.
        from core.credentials import mcp_gateway
        url = mcp_gateway.internal_entry_url(url)

        # Forward the entry's Authorization header to the remote MCP. CLI/Codex
        # already do this via their config files; the direct layer historically
        # dropped it (no `headers=`), which 401s vendor-bearer MCPs and — once
        # the master key is gone — breaks file-tools' proxy callbacks. For Docker
        # MCPs declaring server.proxy_callbacks, swap the session-JWT sentinel for
        # a real session JWT here (session_id is known); vendor bearers pass
        # through untouched.
        headers: dict | None
        headers = dict(self.config.get("headers") or {})
        from auth.session_token import swap_session_jwt_bearer
        _swapped = swap_session_jwt_bearer(
            headers.get("Authorization", ""), self.session_id, self.agent_name,
        )
        if _swapped is not None:
            headers["Authorization"] = _swapped
        headers = headers or None

        server_type = self.config.get("type", "sse")
        factory = self._client_factory(url)

        if server_type in _REMOTE_STREAMABLE:
            cm = streamablehttp_client(url, headers=headers, **factory)
            streams = await cm.__aenter__()
            self._cm_stack.append(cm)
            # streamablehttp_client returns (read, write, get_session_id) — 3-tuple
            session_cm = ClientSession(streams[0], streams[1])
        else:
            sse_cm = sse_client(url, headers=headers, **factory)
            sse_streams = await sse_cm.__aenter__()
            self._cm_stack.append(sse_cm)
            session_cm = ClientSession(*sse_streams)

        self.session = await session_cm.__aenter__()
        self._cm_stack.append(session_cm)

    def _client_factory(self, url: str) -> dict:
        """The credential gateway's entry on this proxy's listener keeps the
        SDK's client. A Docker sidecar (or, self-hosted, any loopback host)
        keeps it too, except on a hosted install, where it is dialled only
        inside the install's tool networks (``pinned_http.docker_tool``: a
        loopback URL a manifest names is refused there). Any other remote
        MCP, a manifest's URL, goes through the checked transport
        (``admin_host``: public only on a hosted install)."""
        from urllib.parse import urlsplit

        from core.config import deployment
        from core.credentials import mcp_gateway
        from services.infra import pinned_http
        from services.mcp import mcp_registry
        host = urlsplit(url).hostname or ""
        if deployment.is_proxy_local_mcp_host(host, mcp_registry.get_manifest(self.name)):
            policy = None if mcp_gateway.is_own_entry(url) else pinned_http.tool_policy()
            return {} if policy is None else {
                "httpx_client_factory": pinned_http.mcp_client_factory(policy)}
        return {"httpx_client_factory": pinned_http.mcp_client_factory(pinned_http.admin_host)}

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on this server. Returns the result as a string. A
        server that forgot the session is marked dead (``session_gone``)."""
        return (await self.call_tool_blocks(tool_name, arguments))[0]

    async def call_tool_blocks(self, tool_name: str, arguments: dict) -> tuple[str, list[dict]]:
        """``call_tool`` with the result's image blocks and embedded
        resources beside the text (MCP JSON shape), for the platform to keep
        when the MCP opts the tool in (``services/mcp/result_files.py``)."""
        if self.pending:
            refused = await self.ensure_started()
            if refused:
                return refused, []
        if not self.session:
            self.dead = True
            return f"Error: MCP server '{self.name}' is not connected", []

        try:
            result = await asyncio.wait_for(
                self.session.call_tool(tool_name, arguments),
                timeout=self.tool_timeout,
            )
            # Concatenate all text content from the result
            parts = []
            blocks: list[dict] = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                elif hasattr(content, "data"):
                    # The blocks with data (image, audio) carry its type.
                    parts.append(f"[binary data: {cast('ImageContent | AudioContent', content).mimeType}]")
                if getattr(content, "type", "") in _MCP_FILE_BLOCKS:
                    blocks.append(content.model_dump(mode="json", by_alias=True, exclude_none=True))
            return ("\n".join(parts) if parts else "(empty result)"), blocks
        except asyncio.TimeoutError:
            # A slow tool is NOT a dead server — timeouts never mark dead.
            return TimedOut(f"Error: Tool '{tool_name}' timed out after {self.tool_timeout}s"), []
        except McpError as e:
            # A JSON-RPC error answered by a LIVE server: some servers (the
            # github sidecar) report a tool's own failure this way instead of
            # as result.isError — a 404 for a repo with no releases marked
            # the whole server dead every ten minutes on the internal
            # install and cost a rebuild per wake. The server answered; only
            # the call failed. One McpError is the transport's own, not a
            # tool's: "Session terminated" is the HTTP client's word for a
            # server that no longer knows the session (the sidecar evicts an
            # idle one), after which every call fails until a rebuild.
            if session_gone(e):
                self.dead = True
                logger.warning(
                    "MCP server '%s' marked dead: the server forgot the session (%s)",
                    self.name, e
                )
            return f"Error calling tool '{tool_name}': {e}", []
        except Exception as e:  # noqa: BLE001  # the mcp client transport raises anything once a server dies
            # Tool-level failures come back as result.isError above; an
            # EXCEPTION here is transport/protocol trouble (closed stream,
            # dead subprocess — often with an empty message, e.g.
            # ClosedResourceError). Mark the server dead so has_tool stops
            # advertising it and the pooled executor self-heals.
            self.dead = True
            logger.warning(
                "MCP server '%s' marked dead after call failure: %r", self.name, e
            )
            return f"Error calling tool '{tool_name}': {e}", []

    async def close(self) -> None:
        """Signal the owner task to tear down its contexts, and wait for it.

        The actual ``__aexit__`` runs inside ``_serve`` (the task that entered
        the contexts), satisfying anyio's same-task cancel-scope rule. Awaiting
        the owner task guarantees the stdio reader is cancelled and the MCP
        subprocess is terminated before we return. Idempotent. A start in
        flight finishes first (its lock), so the program it opened is closed
        here too.
        """
        self.closed = True
        if self.warming is not None and not self.warming.done():
            with contextlib.suppress(Exception):
                await self.warming  # a call's start beside the model ends first
        if self._start_lock is not None and self._start_lock.locked():
            async with self._start_lock:
                pass
        await self._stop_owner()


class AgentMCPManager:
    """Manages all MCP servers for a single agent session."""

    def __init__(self, agent_name: str, phone_mode: bool = False,
                 credential_env: dict[str, str] | None = None,
                 excluded_mcps: set[str] | None = None,
                 session_id: str = "",
                 sandbox_builder=None,
                 prebuilt_config: tuple | None = None,
                 tool_timeout: int = TOOL_CALL_TIMEOUT,
                 included_mcps: set[str] | None = None,
                 machine_id: str = "",
                 lazy: bool = False,
                 programs: tuple = ()):
        self.agent_name = agent_name
        self.phone_mode = phone_mode
        # The programs of the platform's own this session carries, by name
        # (``sandbox_program.ProgramSpec`` each): held beside the servers,
        # never among them, so no tool list, index or catalog sees one.
        self.program_specs = tuple(programs)
        self.programs: dict = {}
        # A program whose entry carries the stand-in's marker waits for the
        # agent's first call when the session's scope has its answers saved
        # (every session the pool builds, a call's warm ones starting at
        # once beside the model; never an app button's, which starts what it
        # calls, and the listing job's build carries no marker).
        self.lazy = lazy
        # The pool's start future: a second caller for the same session
        # during the first start awaits it and shares the outcome (the pool
        # sets it when it registers the manager, so its readers cast).
        self.started: asyncio.Future | None = None
        # When set, ONLY these mcpServers keys start (the app-action executor
        # warms the MCPs an app's manifest names, not the agent's full set).
        self.included_mcps = included_mcps
        self.credential_env = credential_env
        # The ``mcpServers`` keys this session does not start although the
        # builder's config names them: what the builder left out by its kind
        # (by name, as a second filter) and what this engine has as builtins.
        self.excluded_mcps = excluded_mcps or set()
        self.session_id = session_id
        # The compute machine its stdio servers' processes run on, "" for this
        # host (``placement.compute_of`` of the session's placement).
        self.machine_id = machine_id
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # Per-call cap handed to every connection (see TOOL_CALL_TIMEOUT).
        self.tool_timeout = tool_timeout
        self.sandbox_builder = sandbox_builder  # SandboxBuilder or None
        # (config_path, secret_bundles) built by the CALLER, as the CLI
        # engines take theirs: the session's builder (its person, the chat's
        # choice of account, its kind's exclusions, the outside picks and the
        # audience gate) or the app-action executor. None starts nothing.
        self.prebuilt_config = prebuilt_config
        self.servers: dict[str, MCPServerConnection] = {}
        self._tool_index: dict[str, MCPServerConnection] = {}  # namespaced_name -> server
        self.last_activity: float = time.monotonic()

    async def start(self) -> None:
        """Load MCP config and start all servers (dispatched to MCP I/O thread)."""
        self._main_loop = asyncio.get_running_loop()
        await _run_on_mcp_loop(self._start_impl())

    def _read_config(self) -> tuple[dict, dict | None]:
        """The secret bundles and the ``mcpServers`` of the config the caller
        built, read back: a worker thread's job, never on a loop. The servers
        are None when there is no config to read."""
        if self.prebuilt_config is None:
            return {}, None
        mcp_config_path, secret_bundles = self.prebuilt_config
        secret_bundles = dict(secret_bundles or {})
        if not mcp_config_path or not mcp_config_path.exists():
            logger.info("No MCP config for agent '%s'", self.agent_name)
            return secret_bundles, None
        try:
            mcp_config = json.loads(mcp_config_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read MCP config for '%s': %s", self.agent_name, e)
            return secret_bundles, None
        return secret_bundles, mcp_config.get("mcpServers", {})

    async def _push_exit_values(self, secret_bundles: dict) -> None:
        """A session whose tools run on the compute machine: the values
        behind its placeholders pushed into that machine's table by the push
        registry before the first tool starts (the gateway's own channel,
        keyed by the session's exit secret: this layer mints a token per
        child, so no one child's token can key them)."""
        from core.credentials import credential_exit
        if not self.machine_id or not secret_bundles or self._main_loop is None:
            return
        secret = credential_exit.machine_secret(self.session_id)
        if not secret:
            return
        from core.remote import mcp_gateway_push
        fut = asyncio.run_coroutine_threadsafe(
            mcp_gateway_push.push_session(self.session_id, self.machine_id, mcp_gateway_push.token_hash_of(secret)),
            self._main_loop,
        )
        try:
            await asyncio.wrap_future(fut)
        except Exception:
            logger.exception("Direct session %s: the exit's values were not pushed", self.session_id[:8])

    async def _start_impl(self) -> None:
        """Internal: runs on the dedicated MCP I/O event loop; the config is
        read in a worker thread first (``_read_config``), so a session start
        holds neither loop."""
        from core.credentials import mcp_broker
        secret_bundles, servers_config = await asyncio.to_thread(self._read_config)
        # Credential broker: provision THIS session's per-MCP secret
        # bundles so _start_stdio can merge each server's secrets in-process.
        # Direct-LLM has no stdio interceptor / HTTP fetch — it reads the store
        # directly. Idempotent — a no-op when there are no secret bundles.
        mcp_broker.provision(self.session_id, secret_bundles)
        await self._push_exit_values(secret_bundles)
        self._make_programs()
        if servers_config is None:
            return
        if not servers_config:
            logger.info("Agent '%s' has no MCP servers", self.agent_name)
            return

        # Start servers in parallel, skipping the excluded ones.
        tasks = []
        counted: list[MCPServerConnection] = []
        for name, srv_config in servers_config.items():
            if name in self.excluded_mcps:
                logger.info("Skipping an excluded MCP: %s", name)
                continue
            if self.included_mcps is not None and name not in self.included_mcps:
                continue
            conn = MCPServerConnection(name, srv_config,
                                       credential_env=self.credential_env,
                                       session_id=self.session_id,
                                       sandbox_builder=self.sandbox_builder,
                                       agent_name=self.agent_name,
                                       tool_timeout=self.tool_timeout,
                                       start_timeout=PHONE_HTTP_START_S if self.phone_mode else None,
                                       machine_id=self.machine_id,
                                       main_loop=self._main_loop)
            self.servers[name] = conn
            marker = _marker_of(srv_config)
            conn.warm = bool(marker and marker.get("warm") is True)
            if self.lazy and await self._waits_for_first_call(conn):
                if conn.warm:
                    # A call's tool: listed from its answers now, its program
                    # started beside the model; a call meanwhile waits for it.
                    conn.warming = asyncio.ensure_future(conn.ensure_started())
                    conn.warming.add_done_callback(functools.partial(_log_warm_failure, conn))
                continue
            if marker is not None and not conn.warm:
                # The estimate counted it as waiting for its first call: a
                # program that starts with the session is counted here, as the
                # stand-in's start with its session is.
                if await conn._admit(judged=False):
                    counted.append(conn)
            tasks.append(self._start_server(conn))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("MCP server start failed: %s", result)
        for conn in counted:
            if conn.session is None:
                await conn._give_back()
        # A program started only for its answers rests until its first call.
        for conn in list(self.servers.values()):
            if await conn.rest_until_called() and conn in counted:
                await conn._give_back()

        # Build tool index
        for conn in self.servers.values():
            for tool in conn.tools:
                self._tool_index[tool["name"]] = conn

        total_tools = sum(len(c.tools) for c in self.servers.values())
        logger.info(
            "Agent '%s': %s MCP servers, "
            "%s tools ready", self.agent_name, len(self.servers), total_tools
        )

    def _make_programs(self) -> None:
        """The session's programs, each to start at its first call. On a
        call, one that asks for it starts now, beside the model."""
        from core.layers.direct.sandbox_program import SandboxProgram
        for spec in self.program_specs:
            program = SandboxProgram(spec, session_id=self.session_id, sandbox_builder=self.sandbox_builder,
                                     agent_name=self.agent_name, machine_id=self.machine_id,
                                     main_loop=self._main_loop)
            self.programs[spec.name] = program
            if self.phone_mode and spec.starts_with_call:
                program.start_soon()

    async def _waits_for_first_call(self, conn: MCPServerConnection) -> bool:
        """Whether ``conn`` answers from the answers saved for this session's
        scope and starts at its first call. A marked stdio entry with nothing
        saved starts now and its answers are kept for the next session."""
        marker = _marker_of(conn.config)
        from services.mcp.mcp_manifest_types import is_stdio_transport
        if marker is None or not is_stdio_transport(conn.config.get("type")):
            return False
        from core.session.session_state import get_session_security
        from services.mcp import tool_listing
        ctx = get_session_security(self.session_id) if self.session_id else None
        if ctx is None:
            return False
        scope = tool_listing.scope_of(ctx)
        argv = [conn.config["command"], *conn.config.get("args", [])]
        fp = marker_fingerprint(marker, conn.base_env(), argv)
        try:
            saved = await asyncio.to_thread(tool_listing.saved, scope, conn.name, fp)
        except Exception:  # noqa: BLE001  # no saved answer to read: the program starts with the session
            logger.warning("MCP server '%s': its saved answers could not be read", conn.name, exc_info=True)
            saved = None
        if saved is None:
            conn.capture = (scope, fp)
            return False
        conn.wait_for_first_call(saved, scope, fp)
        return True

    async def _start_server(self, conn: MCPServerConnection) -> None:
        """Start a single MCP server with error isolation."""
        try:
            await conn.start()
        except Exception as e:  # noqa: BLE001  # one MCP server's start must not stop the others from starting
            logger.error("Failed to start MCP server '%s': %s", conn.name, e)

    def tools_lost(self) -> bool:
        """Whether the session's relayed tools are gone: their machine's link
        went (also while one opened), or it no longer serves the port they
        reach the platform by. A tool the machine could not start is no loss."""
        if not self.machine_id:
            return False
        from core.remote import tool_relay
        return (any(c.link_lost for c in [*list(self.servers.values()), *list(self.programs.values())])
                or tool_relay.session_lost(self.machine_id, self.session_id))

    def has_tool(self, namespaced_name: str) -> bool:
        """Whether a namespaced tool was discovered on a started server that
        is still ALIVE. A dead server (transport failure after discovery)
        reports its tools missing so the headless executor's cooldown-gated
        self-heal rebuilds the manager instead of erroring forever."""
        conn = self._tool_index.get(namespaced_name)
        return conn is not None and not conn.dead

    def get_tools(self) -> list[dict]:
        """Return tool definitions in universal format (name, description, input_schema).

        This format is used by all providers: Anthropic, OpenAI, Groq, etc.
        """
        tools = []
        for conn in self.servers.values():
            for tool in conn.tools:
                tools.append({
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"],
                })
        return tools

    async def execute_tool(self, namespaced_name: str, arguments: dict) -> str:
        """Execute a single tool call by its namespaced name."""
        return (await self.execute_tool_blocks(namespaced_name, arguments))[0]

    async def execute_tool_blocks(self, namespaced_name: str, arguments: dict) -> tuple[str, list[dict]]:
        """``execute_tool`` with the result's image blocks and embedded
        resources (``MCPServerConnection.call_tool_blocks``)."""
        self.last_activity = time.monotonic()

        conn = self._tool_index.get(namespaced_name)
        if not conn:
            return f"Error: Unknown tool '{namespaced_name}'", []

        # Find the original tool name (strip mcp__servername__ prefix)
        original_name = None
        for tool in conn.tools:
            if tool["name"] == namespaced_name:
                original_name = tool["_original_name"]
                break

        if not original_name:
            return f"Error: Tool '{namespaced_name}' not found in server '{conn.name}'", []

        return await conn.call_tool_blocks(original_name, arguments)

    async def execute_tools(self, tool_calls: list[dict]) -> list[dict]:
        """Execute multiple tool calls in parallel (dispatched to MCP I/O thread).

        Args:
            tool_calls: list of {"id": str, "name": str, "input": dict}

        Returns:
            list of {"tool_use_id": str, "content": str}, with
            ``"file_blocks"`` (the result's image blocks and embedded
            resources, MCP JSON) when it carried any and ``"timed_out"``
            when the client stopped waiting at its cap
        """
        self.last_activity = time.monotonic()
        return await _run_on_mcp_loop(self._execute_tools_impl(tool_calls))

    async def _execute_tools_impl(self, tool_calls: list[dict]) -> list[dict]:
        """Internal: runs on the dedicated MCP I/O event loop."""
        async def _run_one(call: dict) -> dict:
            result, blocks = await self.execute_tool_blocks(call["name"], call["input"])
            out = {
                "tool_use_id": call["id"],
                "content": result,
            }
            if isinstance(result, TimedOut):
                out["timed_out"] = True
            if blocks:
                out["file_blocks"] = blocks
            return out

        results = await asyncio.gather(
            *[_run_one(tc) for tc in tool_calls],
            return_exceptions=True,
        )

        final = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                final.append({
                    "tool_use_id": tool_calls[i]["id"],
                    "content": f"Error: {result}",
                })
            else:
                final.append(cast("dict", result))
        return final

    async def close(self) -> None:
        """Close all MCP server connections (dispatched to MCP I/O thread)."""
        await _run_on_mcp_loop(self._close_impl())

    async def _close_impl(self) -> None:
        """Internal: runs on the dedicated MCP I/O event loop."""
        tasks = [conn.close() for conn in (*self.servers.values(), *self.programs.values())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.servers.clear()
        self.programs.clear()
        self._tool_index.clear()
        logger.info("Closed MCP manager for agent '%s'", self.agent_name)


class MCPPool:
    """Global pool of AgentMCPManagers, keyed by session_id."""

    def __init__(self):
        self._managers: dict[str, AgentMCPManager] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        session_id: str,
        agent_name: str,
        phone_mode: bool = False,
        credential_env: dict[str, str] | None = None,
        excluded_mcps: set[str] | None = None,
        sandbox_builder=None,
        tool_timeout: int = TOOL_CALL_TIMEOUT,
        prebuilt_config: tuple | None = None,
        machine_id: str = "",
        programs: tuple = (),
    ) -> AgentMCPManager:
        """Get an existing manager or create and start a new one.

        ``tool_timeout`` is the per-call cap (the chat's for every Direct
        session, the default for the app buttons). ``prebuilt_config`` is the
        session's builder's config and bundles, ``excluded_mcps`` the server
        keys it does not start. ``machine_id`` is the compute machine its
        stdio servers run on. ``programs`` are the programs of the
        platform's own the session carries. A manager whose relayed tools
        were lost is closed and never handed out again under its id.
        """
        lost: AgentMCPManager | None = None
        async with self._lock:
            mgr = self._managers.get(session_id)
            if mgr is not None and mgr.tools_lost():
                lost, mgr = mgr, None
                del self._managers[session_id]
            existing = mgr is not None
            if existing:
                cast("AgentMCPManager", mgr).last_activity = time.monotonic()
            else:
                mgr = AgentMCPManager(
                    agent_name, phone_mode=phone_mode,
                    credential_env=credential_env,
                    excluded_mcps=excluded_mcps,
                    session_id=session_id,
                    sandbox_builder=sandbox_builder,
                    tool_timeout=tool_timeout,
                    prebuilt_config=prebuilt_config,
                    machine_id=machine_id,
                    lazy=True,
                    programs=programs,
                )
                mgr.started = asyncio.get_running_loop().create_future()
                self._managers[session_id] = mgr
        mgr = cast("AgentMCPManager", mgr)  # the one found under the lock, or the one added
        if lost is not None:
            with contextlib.suppress(Exception):
                await lost.close()

        if existing:
            # A manager another caller is still starting: wait for that start
            # and share its outcome (a failed one raises here too); a started
            # one answers at once.
            if mgr.started is not None:
                await asyncio.shield(mgr.started)
            return mgr

        # Start outside the lock (slow: spawns MCP server subprocesses). A
        # start that raises or is cancelled leaves no manager behind (the next
        # warm of the session builds afresh instead of finding one with no
        # servers under its id) and every caller waiting on it gets the
        # failure.
        try:
            await mgr.start()
        except BaseException as exc:
            async with self._lock:
                if self._managers.get(session_id) is mgr:
                    del self._managers[session_id]
            with contextlib.suppress(Exception):
                await asyncio.shield(mgr.close())
            if not cast("asyncio.Future", mgr.started).done():
                failure = (RuntimeError("the session's MCP start was cancelled")
                           if isinstance(exc, asyncio.CancelledError) else exc)
                cast("asyncio.Future", mgr.started).set_exception(failure)
                # Read once, so a failure nobody waited on is not logged as
                # an exception never retrieved.
                cast("asyncio.Future", mgr.started).exception()
            raise
        if not cast("asyncio.Future", mgr.started).done():
            cast("asyncio.Future", mgr.started).set_result(None)
        return mgr

    async def close_session(self, session_id: str) -> bool:
        """Close and remove a session's MCP manager. Returns True if found."""
        async with self._lock:
            mgr = self._managers.pop(session_id, None)
        if mgr:
            await mgr.close()
            return True
        return False

    async def active_mcp_names(self) -> set[str]:
        """MCP names with a live connection in any active session.

        The auto-update in-use guard (services/mcp_updater.mcp_in_use): recreating
        a docker MCP's shared container while a session holds a connection would
        drop it, so the weekly job defers such MCPs. Precise — these are the
        servers actually started for each live session, not merely configured.
        """
        async with self._lock:
            names: set[str] = set()
            for mgr in self._managers.values():
                names |= {name for name, conn in mgr.servers.items() if not conn.pending}
            return names

    async def reap_idle(self, timeout: int = 0, *, keep: frozenset[str] = frozenset()) -> None:
        """Close managers idle for longer than timeout seconds. ``keep``
        are the sessions that live: a manager's own clock moves only at a
        tool's or a program's call, and a session that takes turns without
        one still holds its manager."""
        if timeout <= 0:
            timeout = config.get_idle_timeout()

        now = time.monotonic()
        to_reap = []

        async with self._lock:
            for sid, mgr in list(self._managers.items()):
                if sid not in keep and now - mgr.last_activity > timeout:
                    to_reap.append((sid, mgr))
                    del self._managers[sid]

        for sid, mgr in to_reap:
            logger.info("Reaping idle MCP manager: session=%s", sid)
            await mgr.close()


# Global singleton
mcp_pool = MCPPool()
