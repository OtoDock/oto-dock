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
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

if TYPE_CHECKING:
    from mcp.types import AudioContent, ImageContent

import config
from core import placement

logger = logging.getLogger("mcp-manager")

# Timeout for individual tool calls (seconds). The default bounds phone
# calls and app-action clicks (a caller / a click must never wait longer);
# chat, task and meeting sessions use the chat figure (Codex's 300 s) so a
# document render or a browser flow behind a sidecar MCP can finish.
TOOL_CALL_TIMEOUT = 60
TOOL_CALL_TIMEOUT_CHAT = 300

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


# --- The config build's lock ---
# One build at a time per build identity, which is what names the file
# ``build_session_mcp_config`` writes and the manager reads back
# (``user-mcp-configs/[sessions/<sha(session_id)>/]<agent>-<sha("agent")>.json``),
# so a second build of the same identity waits for the first and reads back
# a whole file. Taken in worker threads only, around a job that awaits
# nothing, so no loop ever waits on a thread that waits on the loop; an
# entry leaves the map with its last holder.

class _BuildLock:
    __slots__ = ("lock", "holders")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.holders = 0


_build_locks: dict[tuple, _BuildLock] = {}
_build_locks_guard = threading.Lock()


@contextlib.contextmanager
def _build_lock(key: tuple):
    with _build_locks_guard:
        entry = _build_locks.get(key)
        if entry is None:
            entry = _build_locks[key] = _BuildLock()
        entry.holders += 1
    try:
        with entry.lock:
            yield
    finally:
        with _build_locks_guard:
            entry.holders -= 1
            if entry.holders == 0 and _build_locks.get(key) is entry:
                del _build_locks[key]


class MCPServerConnection:
    """Wraps a single MCP server connection (stdio or SSE)."""

    def __init__(self, name: str, server_config: dict,
                 credential_env: dict[str, str] | None = None,
                 session_id: str = "",
                 sandbox_builder=None,
                 agent_name: str = "",
                 tool_timeout: int = TOOL_CALL_TIMEOUT):
        self.name = name
        self.config = server_config
        self.credential_env = credential_env
        self.session_id = session_id
        self.sandbox_builder = sandbox_builder  # SandboxBuilder or None
        self.agent_name = agent_name
        self.tool_timeout = tool_timeout  # per-call cap, set by the manager
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

    async def start(self) -> None:
        """Connect to the MCP server and discover tools.

        ``stdio_client`` / ``ClientSession`` are anyio context managers, and
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
        try:
            if server_type == "stdio":
                await self._start_stdio()
            elif server_type == "sse" or server_type in _REMOTE_STREAMABLE:
                await self._start_remote()
            else:
                logger.warning("Unknown MCP server type '%s' for %s", server_type, self.name)
                if not ready.done():
                    ready.set_result(None)
                return

            # Initialize and discover tools
            result = await cast("ClientSession", self.session).initialize()
            logger.info(
                "MCP server '%s' initialized "
                "(protocol=%s)", self.name, result.protocolVersion
            )

            tools_result = await cast("ClientSession", self.session).list_tools()
            for tool in tools_result.tools:
                namespaced = f"mcp__{self.name}__{tool.name}"
                self.tools.append({
                    "name": namespaced,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
                    "_original_name": tool.name,
                    "_server": self.name,
                })

            logger.info(
                "MCP server '%s': %s tools discovered",
                self.name, len(self.tools)
            )
            # The tools' own annotations: the permission tier of a tool no
            # manifest rule names (services/mcp/mcp_permissions).
            from services.mcp.mcp_permissions import record_tool_hints
            await asyncio.to_thread(record_tool_hints, self.name, [
                (tool.name, tool.annotations.model_dump(exclude_none=True) if tool.annotations else None)
                for tool in tools_result.tools])
        except Exception as e:  # noqa: BLE001  # the mcp client raises anything at start: the server gets no tools
            # Error isolation: a failed server just has no tools (matches the
            # previous behaviour). Tear down whatever was entered, in THIS task.
            logger.error("Failed to start MCP server '%s': %s", self.name, e)
            await self._teardown()
            if not ready.done():
                ready.set_result(None)
            return

        # Ready — unblock start(), then park until close() signals teardown.
        if not ready.done():
            ready.set_result(None)
        try:
            await cast("asyncio.Event", self._closing).wait()
        finally:
            await self._teardown()

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

    async def _start_stdio(self) -> None:
        """Start a stdio MCP server subprocess."""
        command = self.config["command"]
        args = self.config.get("args", [])

        # Build minimal env with session-scoped auth token + OTO_* vars.
        # Username + role come from the sandbox config (matches CLI/Codex
        # — same source of truth, so OTO_* values are consistent).
        from core.sandbox.env_builder import build_session_env
        _username = (
            self.sandbox_builder.cfg.username if self.sandbox_builder else ""
        )
        _user_role = (
            self.sandbox_builder.cfg.role if self.sandbox_builder else ""
        )
        env = build_session_env(
            self.session_id or "", self.agent_name,
            credential_env=self.credential_env,
            username=_username,
            user_role=_user_role,
        )

        # Merge server-specific env vars from MCP config
        server_env = self.config.get("env", {})
        env.update(server_env)

        # Credential broker: merge this MCP's brokered secrets in-process.
        # Direct-LLM spawns MCPs inside the proxy, so there's no stdio interceptor
        # to fetch over HTTP — read straight from the per-session store
        # provisioned in _start_impl. No-op while secrets remain in server_env;
        # after the source-strip this becomes the sole delivery path.
        from core.credentials import mcp_broker
        _bundle = mcp_broker.get(self.session_id, self.name)
        if _bundle:
            env.update(_bundle.env)

        # Wrap in bwrap if sandbox is enabled for this session.
        # Each stdio MCP subprocess gets its own bwrap mount namespace
        # with the same mounts as CLI MCPs (system, workspace, mcps dir).
        if self.sandbox_builder:
            inner_cmd = [command] + args
            wrapped = self.sandbox_builder.build_command_prefix(inner_cmd)
            command = wrapped[0]   # "bwrap"
            args = wrapped[1:]     # bwrap flags + "--" + original command + args
            # Apply sandbox env restrictions (PATH, HOME)
            env.update(self.sandbox_builder.get_env_overrides())

        # Below the proxy's CPU priority, like every session process.
        from core.sandbox import pty_relay
        command, *args = pty_relay.niced([command, *args])

        params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        # stdio_client is an async context manager that returns (read, write) streams
        cm = stdio_client(params)
        streams = await cm.__aenter__()
        self._cm_stack.append(cm)

        # ClientSession wraps the streams
        session_cm = ClientSession(*streams)
        self.session = await session_cm.__aenter__()
        self._cm_stack.append(session_cm)

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

        if server_type in _REMOTE_STREAMABLE:
            cm = streamablehttp_client(url, headers=headers)
            streams = await cm.__aenter__()
            self._cm_stack.append(cm)
            # streamablehttp_client returns (read, write, get_session_id) — 3-tuple
            session_cm = ClientSession(streams[0], streams[1])
        else:
            sse_cm = sse_client(url, headers=headers)
            sse_streams = await sse_cm.__aenter__()
            self._cm_stack.append(sse_cm)
            session_cm = ClientSession(*sse_streams)

        self.session = await session_cm.__aenter__()
        self._cm_stack.append(session_cm)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on this server. Returns the result as a string. A
        server that forgot the session is marked dead (``session_gone``)."""
        return (await self.call_tool_blocks(tool_name, arguments))[0]

    async def call_tool_blocks(self, tool_name: str, arguments: dict) -> tuple[str, list[dict]]:
        """``call_tool`` with the result's image blocks and embedded
        resources beside the text (MCP JSON shape), for the platform to keep
        when the MCP opts the tool in (``services/mcp/result_files.py``)."""
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
            return f"Error: Tool '{tool_name}' timed out after {self.tool_timeout}s", []
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
        subprocess is terminated before we return. Idempotent.
        """
        if self._closing is not None:
            self._closing.set()
        task = self._owner_task
        self._owner_task = None
        if task is not None:
            with contextlib.suppress(Exception):
                await task


class AgentMCPManager:
    """Manages all MCP servers for a single agent session."""

    def __init__(self, agent_name: str, phone_mode: bool = False,
                 credential_env: dict[str, str] | None = None,
                 excluded_mcps: set[str] | None = None,
                 session_id: str = "",
                 sandbox_builder=None,
                 prebuilt_config: tuple | None = None,
                 enable_http_transport: bool = False,
                 tool_timeout: int = TOOL_CALL_TIMEOUT,
                 external: bool = False,
                 included_mcps: set[str] | None = None,
                 user_role: str = "",
                 scope: str = "user"):
        self.agent_name = agent_name
        self.phone_mode = phone_mode
        # The person's effective role on the agent and the scope the build
        # judges the manifests' audience with, as the session's builder did
        # (``mcp_registry.audience_refusal``): a user-scope session of a
        # viewer never gets an editor-audience MCP's tools on this engine
        # either. An empty role is "no person known" and filters nothing.
        self.user_role = user_role
        self.scope = scope
        # The pool's start future: a second caller for the same session
        # during the first start awaits it and shares the outcome (the pool
        # sets it when it registers the manager, so its readers cast).
        self.started: asyncio.Future | None = None
        # When set, ONLY these mcpServers keys start (the app-action executor
        # warms the MCPs an app's manifest names, not the agent's full set).
        self.included_mcps = included_mcps
        # An EXTERNAL session (a phone caller who is not a platform user)
        # never attaches the platform-management MCPs — the rebuild below
        # applies the same exclusion rule the phone builder used, so the
        # tools cannot exist here when the prompt says they do not.
        self.external = external
        self.credential_env = credential_env
        self.excluded_mcps = excluded_mcps or set()
        self.session_id = session_id
        # Sidecar HTTP MCPs (registry Claude-JSON ``type: "http"``) are opt-in
        # per manager: the app-action executor and Direct-LLM chat / task /
        # meeting sessions enable them (their schemas ride the deferred-tools
        # catalog, and the chat call cap is 300 s); phone calls keep them off
        # — a document render must never hang a caller.
        self.enable_http_transport = enable_http_transport
        # Per-call cap handed to every connection (see TOOL_CALL_TIMEOUT).
        self.tool_timeout = tool_timeout
        self.sandbox_builder = sandbox_builder  # SandboxBuilder or None
        # Optional (config_path, secret_bundles) built by the CALLER — used by
        # the headless app-action executor, whose identity (personal-app owner
        # creds, task-context exclusions) differs from the agent-scope
        # dashboard build _start_impl does by default. When set, _start_impl
        # provisions THESE bundles instead of rebuilding.
        self.prebuilt_config = prebuilt_config
        self.servers: dict[str, MCPServerConnection] = {}
        self._tool_index: dict[str, MCPServerConnection] = {}  # namespaced_name -> server
        self.last_activity: float = time.monotonic()

    async def start(self) -> None:
        """Load MCP config and start all servers (dispatched to MCP I/O thread)."""
        await _run_on_mcp_loop(self._start_impl())

    def _build_config(self) -> tuple[object, dict, dict | None]:
        """The session's MCP config (built, or the caller's prebuilt one),
        its secret bundles with the token files delivered, and the
        ``mcpServers`` read back: a worker thread's job, one build at a time
        per build identity (``_build_lock``). Never on a loop: the build
        reads the store and the manifests, writes the file, and an
        installation binding mints its token only where no loop runs
        (``github_apps.ensure_installation_token``). The servers are None
        when there is no config to read."""
        from core.credentials import credential_files
        if self.prebuilt_config is not None:
            key: tuple[str, ...] = ("prebuilt", str(self.prebuilt_config[0]))
        else:
            key = ("build", self.agent_name, self.session_id)
        with _build_lock(key):
            # is_remote=False (fail-closed default, explicit here): Direct-LLM
            # starts MCP subprocesses IN the proxy process and is ALWAYS
            # local, so a satellite_only device MCP must never reach this
            # path — it would drive the SERVER's screen/input.
            if self.prebuilt_config is not None:
                mcp_config_path, secret_bundles = self.prebuilt_config
            else:
                from services.mcp import mcp_registry
                mcp_config_path, _, _, secret_bundles, _ = mcp_registry.build_session_mcp_config(
                    self.agent_name, None,
                    phone_mode=self.phone_mode,
                    placement=placement.LOCAL_PLACEMENT,
                    external=self.external,
                    session_id=self.session_id,
                    user_role=self.user_role,
                    task_scope=self.scope,
                )
            secret_bundles = dict(secret_bundles or {})
            if self.prebuilt_config is None:
                # A builder that hands in its bundles (an app button) has
                # delivered the files for ITS identity; re-delivering the
                # agent scope here would replace a personal owner's file.
                credential_files.deliver_token_files(
                    secret_bundles, self.agent_name, user_sub="", session_scope="agent",
                )
            if not mcp_config_path or not mcp_config_path.exists():
                logger.info("No MCP config for agent '%s'", self.agent_name)
                return mcp_config_path, secret_bundles, None
            try:
                mcp_config = json.loads(mcp_config_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to read MCP config for '%s': %s", self.agent_name, e)
                return mcp_config_path, secret_bundles, None
            return mcp_config_path, secret_bundles, mcp_config.get("mcpServers", {})

    async def _start_impl(self) -> None:
        """Internal: runs on the dedicated MCP I/O event loop; the config is
        built in a worker thread first (``_build_config``), so a session
        start holds neither loop."""
        from core.credentials import mcp_broker
        _path, secret_bundles, servers_config = await asyncio.to_thread(self._build_config)
        # Credential broker: provision THIS session's per-MCP secret
        # bundles so _start_stdio can merge each server's secrets in-process.
        # Direct-LLM has no stdio interceptor / HTTP fetch — it reads the store
        # directly. Provisioned from THIS build (agent scope) so the bundle keys
        # match the mcpServers keys read below. Idempotent — a no-op when there
        # are no secret bundles.
        mcp_broker.provision(self.session_id, secret_bundles)
        if servers_config is None:
            return
        if not servers_config:
            logger.info("Agent '%s' has no MCP servers", self.agent_name)
            return

        # Start servers in parallel, skipping excluded ones
        # (phone/context exclusions already handled by build_session_mcp_config)
        tasks = []
        for name, srv_config in servers_config.items():
            if name in self.excluded_mcps:
                logger.info("Skipping credential-excluded MCP: %s", name)
                continue
            if self.included_mcps is not None and name not in self.included_mcps:
                continue
            if srv_config.get("type") == "http" and not self.enable_http_transport:
                logger.info(
                    "Skipping http-transport MCP '%s' "
                    "(not enabled for this manager)", name
                )
                continue

            conn = MCPServerConnection(name, srv_config,
                                       credential_env=self.credential_env,
                                       session_id=self.session_id,
                                       sandbox_builder=self.sandbox_builder,
                                       agent_name=self.agent_name,
                                       tool_timeout=self.tool_timeout)
            self.servers[name] = conn
            tasks.append(self._start_server(conn))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("MCP server start failed: %s", result)

        # Build tool index
        for conn in self.servers.values():
            for tool in conn.tools:
                self._tool_index[tool["name"]] = conn

        total_tools = sum(len(c.tools) for c in self.servers.values())
        logger.info(
            "Agent '%s': %s MCP servers, "
            "%s tools ready", self.agent_name, len(self.servers), total_tools
        )

    async def _start_server(self, conn: MCPServerConnection) -> None:
        """Start a single MCP server with error isolation."""
        try:
            await conn.start()
        except Exception as e:  # noqa: BLE001  # one MCP server's start must not stop the others from starting
            logger.error("Failed to start MCP server '%s': %s", conn.name, e)

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
            resources, MCP JSON) when it carried any
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
        tasks = [conn.close() for conn in self.servers.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.servers.clear()
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
        enable_http_transport: bool = False,
        tool_timeout: int = TOOL_CALL_TIMEOUT,
        external: bool = False,
        user_role: str = "",
        scope: str = "user",
    ) -> AgentMCPManager:
        """Get an existing manager or create and start a new one.

        ``enable_http_transport`` / ``tool_timeout`` are the session policy
        (``layer.direct_mcp_policy``): sidecar HTTP MCPs + the chat call cap
        for chats, tasks and meetings; stdio-only + 60 s for phone calls.
        ``external`` marks a session whose caller is not a platform user
        (the management MCPs are never started for it). ``user_role`` and
        ``scope`` are what the build judges the manifests' audience with
        (``AgentMCPManager``).
        """
        async with self._lock:
            mgr = self._managers.get(session_id)
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
                    enable_http_transport=enable_http_transport,
                    tool_timeout=tool_timeout,
                    external=external,
                    user_role=user_role,
                    scope=scope,
                )
                mgr.started = asyncio.get_running_loop().create_future()
                self._managers[session_id] = mgr
        mgr = cast("AgentMCPManager", mgr)  # the one found under the lock, or the one added

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
                names |= set(mgr.servers.keys())
            return names

    async def reap_idle(self, timeout: int = 0) -> None:
        """Close managers idle for longer than timeout seconds."""
        if timeout <= 0:
            timeout = config.get_idle_timeout()

        now = time.monotonic()
        to_reap = []

        async with self._lock:
            for sid, mgr in list(self._managers.items()):
                if now - mgr.last_activity > timeout:
                    to_reap.append((sid, mgr))
                    del self._managers[sid]

        for sid, mgr in to_reap:
            logger.info("Reaping idle MCP manager: session=%s", sid)
            await mgr.close()


# Global singleton
mcp_pool = MCPPool()
