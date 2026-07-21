"""MCP server manager for direct LLM sessions.

Manages MCP server lifecycles (stdio/SSE), discovers tools, namespaces them
as mcp__servername__toolname (matching Claude Code convention), and executes
tool calls in parallel.

Architecture:
- AgentMCPManager: manages all MCP servers for one agent session
- MCPPool: global singleton mapping session_id -> AgentMCPManager with idle reaping
"""

import asyncio
import json
import logging
import threading
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

import config
import contextlib

logger = logging.getLogger("mcp-manager")

# Timeout for individual tool calls (seconds)
TOOL_CALL_TIMEOUT = 60

# --- Dedicated MCP I/O thread ---
# All MCP server subprocess I/O (start, tool calls, close) runs on this
# thread's event loop so it doesn't contend with the proxy's main HTTP loop.

_mcp_loop: asyncio.AbstractEventLoop | None = None
_mcp_thread: threading.Thread | None = None
_mcp_thread_lock = threading.Lock()


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


class MCPServerConnection:
    """Wraps a single MCP server connection (stdio or SSE)."""

    def __init__(self, name: str, server_config: dict,
                 credential_env: dict[str, str] | None = None,
                 session_id: str = "",
                 sandbox_builder=None,
                 agent_name: str = ""):
        self.name = name
        self.config = server_config
        self.credential_env = credential_env
        self.session_id = session_id
        self.sandbox_builder = sandbox_builder  # SandboxBuilder or None
        self.agent_name = agent_name
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
            elif server_type in ("sse", "streamable-http"):
                await self._start_remote()
            else:
                logger.warning(f"Unknown MCP server type '{server_type}' for {self.name}")
                if not ready.done():
                    ready.set_result(None)
                return

            # Initialize and discover tools
            result = await self.session.initialize()
            logger.info(
                f"MCP server '{self.name}' initialized "
                f"(protocol={result.protocolVersion})"
            )

            tools_result = await self.session.list_tools()
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
                f"MCP server '{self.name}': {len(self.tools)} tools discovered"
            )
        except Exception as e:
            # Error isolation: a failed server just has no tools (matches the
            # previous behaviour). Tear down whatever was entered, in THIS task.
            logger.error(f"Failed to start MCP server '{self.name}': {e}")
            await self._teardown()
            if not ready.done():
                ready.set_result(None)
            return

        # Ready — unblock start(), then park until close() signals teardown.
        if not ready.done():
            ready.set_result(None)
        try:
            await self._closing.wait()
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        """Exit all context managers, in reverse order. MUST run in the owner
        task (``_serve``) so anyio cancel scopes are exited where they began."""
        for cm in reversed(self._cm_stack):
            try:
                await cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing MCP server '{self.name}': {e}")
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

        # Forward the entry's Authorization header to the remote MCP. CLI/Codex
        # already do this via their config files; the direct layer historically
        # dropped it (no `headers=`), which 401s vendor-bearer MCPs and — once
        # the master key is gone — breaks file-tools' proxy callbacks. For Docker
        # MCPs declaring server.proxy_callbacks, swap the session-JWT sentinel for
        # a real session JWT here (session_id is known); vendor bearers pass
        # through untouched.
        headers = dict(self.config.get("headers") or {})
        from auth.session_token import swap_session_jwt_bearer
        _swapped = swap_session_jwt_bearer(
            headers.get("Authorization", ""), self.session_id, self.agent_name,
        )
        if _swapped is not None:
            headers["Authorization"] = _swapped
        headers = headers or None

        server_type = self.config.get("type", "sse")

        if server_type == "streamable-http":
            cm = streamablehttp_client(url, headers=headers)
            streams = await cm.__aenter__()
            self._cm_stack.append(cm)
            # streamablehttp_client returns (read, write, get_session_id) — 3-tuple
            session_cm = ClientSession(streams[0], streams[1])
        else:
            cm = sse_client(url, headers=headers)
            streams = await cm.__aenter__()
            self._cm_stack.append(cm)
            session_cm = ClientSession(*streams)

        self.session = await session_cm.__aenter__()
        self._cm_stack.append(session_cm)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on this server. Returns the result as a string."""
        if not self.session:
            self.dead = True
            return f"Error: MCP server '{self.name}' is not connected"

        try:
            result = await asyncio.wait_for(
                self.session.call_tool(tool_name, arguments),
                timeout=TOOL_CALL_TIMEOUT,
            )
            # Concatenate all text content from the result
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                elif hasattr(content, "data"):
                    parts.append(f"[binary data: {content.mimeType}]")
            return "\n".join(parts) if parts else "(empty result)"
        except asyncio.TimeoutError:
            # A slow tool is NOT a dead server — timeouts never mark dead.
            return f"Error: Tool '{tool_name}' timed out after {TOOL_CALL_TIMEOUT}s"
        except Exception as e:
            # Tool-level failures come back as result.isError above; an
            # EXCEPTION here is transport/protocol trouble (closed stream,
            # dead subprocess — often with an empty message, e.g.
            # ClosedResourceError). Mark the server dead so has_tool stops
            # advertising it and the pooled executor self-heals.
            self.dead = True
            logger.warning(
                f"MCP server '{self.name}' marked dead after call failure: {e!r}"
            )
            return f"Error calling tool '{tool_name}': {e}"

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
                 prebuilt_config: tuple | None = None):
        self.agent_name = agent_name
        self.phone_mode = phone_mode
        self.credential_env = credential_env
        self.excluded_mcps = excluded_mcps or set()
        self.session_id = session_id
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

    async def _start_impl(self) -> None:
        """Internal: runs on the dedicated MCP I/O event loop."""
        # Try runtime-generated config first, fall back to static per-agent config.
        # is_remote=False (fail-closed default, explicit here): Direct-LLM starts
        # MCP subprocesses IN the proxy process and is ALWAYS local, so a
        # satellite_only device MCP must never reach this path — it would drive
        # the SERVER's screen/input.
        if self.prebuilt_config is not None:
            mcp_config_path, secret_bundles = self.prebuilt_config
        else:
            from services.mcp import mcp_registry
            mcp_config_path, _, _, secret_bundles, _ = mcp_registry.build_session_mcp_config(
                self.agent_name, None,
                phone_mode=self.phone_mode,
                is_remote=False,
            )
        # Credential broker: provision THIS session's per-MCP secret
        # bundles so _start_stdio can merge each server's secrets in-process.
        # Direct-LLM has no stdio interceptor / HTTP fetch — it reads the store
        # directly. Provisioned from THIS build (agent scope) so the bundle keys
        # match the mcpServers keys read below. Idempotent — a no-op when there
        # are no secret bundles.
        from core.credentials import mcp_broker
        mcp_broker.provision(self.session_id, secret_bundles)
        if not mcp_config_path or not mcp_config_path.exists():
            logger.info(f"No MCP config for agent '{self.agent_name}'")
            return

        try:
            mcp_config = json.loads(mcp_config_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to read MCP config for '{self.agent_name}': {e}")
            return

        servers_config = mcp_config.get("mcpServers", {})
        if not servers_config:
            logger.info(f"Agent '{self.agent_name}' has no MCP servers")
            return

        # Start servers in parallel, skipping excluded ones
        # (phone/context exclusions already handled by build_session_mcp_config)
        tasks = []
        for name, srv_config in servers_config.items():
            if name in self.excluded_mcps:
                logger.info(f"Skipping credential-excluded MCP: {name}")
                continue

            conn = MCPServerConnection(name, srv_config,
                                       credential_env=self.credential_env,
                                       session_id=self.session_id,
                                       sandbox_builder=self.sandbox_builder,
                                       agent_name=self.agent_name)
            self.servers[name] = conn
            tasks.append(self._start_server(conn))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"MCP server start failed: {result}")

        # Build tool index
        for conn in self.servers.values():
            for tool in conn.tools:
                self._tool_index[tool["name"]] = conn

        total_tools = sum(len(c.tools) for c in self.servers.values())
        logger.info(
            f"Agent '{self.agent_name}': {len(self.servers)} MCP servers, "
            f"{total_tools} tools ready"
        )

    async def _start_server(self, conn: MCPServerConnection) -> None:
        """Start a single MCP server with error isolation."""
        try:
            await conn.start()
        except Exception as e:
            logger.error(f"Failed to start MCP server '{conn.name}': {e}")

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
        self.last_activity = time.monotonic()

        conn = self._tool_index.get(namespaced_name)
        if not conn:
            return f"Error: Unknown tool '{namespaced_name}'"

        # Find the original tool name (strip mcp__servername__ prefix)
        original_name = None
        for tool in conn.tools:
            if tool["name"] == namespaced_name:
                original_name = tool["_original_name"]
                break

        if not original_name:
            return f"Error: Tool '{namespaced_name}' not found in server '{conn.name}'"

        return await conn.call_tool(original_name, arguments)

    async def execute_tools(self, tool_calls: list[dict]) -> list[dict]:
        """Execute multiple tool calls in parallel (dispatched to MCP I/O thread).

        Args:
            tool_calls: list of {"id": str, "name": str, "input": dict}

        Returns:
            list of {"tool_use_id": str, "content": str}
        """
        self.last_activity = time.monotonic()
        return await _run_on_mcp_loop(self._execute_tools_impl(tool_calls))

    async def _execute_tools_impl(self, tool_calls: list[dict]) -> list[dict]:
        """Internal: runs on the dedicated MCP I/O event loop."""
        async def _run_one(call: dict) -> dict:
            result = await self.execute_tool(call["name"], call["input"])
            return {
                "tool_use_id": call["id"],
                "content": result,
            }

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
                final.append(result)
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
        logger.info(f"Closed MCP manager for agent '{self.agent_name}'")


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
    ) -> AgentMCPManager:
        """Get an existing manager or create and start a new one."""
        async with self._lock:
            mgr = self._managers.get(session_id)
            if mgr:
                mgr.last_activity = time.monotonic()
                return mgr

            mgr = AgentMCPManager(
                agent_name, phone_mode=phone_mode,
                credential_env=credential_env,
                excluded_mcps=excluded_mcps,
                session_id=session_id,
                sandbox_builder=sandbox_builder,
            )
            self._managers[session_id] = mgr

        # Start outside lock (slow: spawns MCP server subprocesses)
        await mgr.start()
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
            logger.info(f"Reaping idle MCP manager: session={sid}")
            await mgr.close()


# Global singleton
mcp_pool = MCPPool()
