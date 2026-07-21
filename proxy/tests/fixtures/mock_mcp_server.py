"""In-process FastMCP HTTP server for builder-executor integration tests.

Spins up a real ``mcp.server.Server`` behind ``StreamableHTTPSessionManager``
served by uvicorn on a free port for the lifetime of one test. Tests
register tools dynamically, then exercise code paths (e.g.
``builder_executor.execute_builder``) that connect to the URL via
``streamablehttp_client`` — same wire protocol as production MCPs.

Why a real server: the builder pipeline relies on the streaming-HTTP
protocol contract (initialize → list_tools → call_tool → content list).
Mocking ``streamablehttp_client`` directly would test the orchestration
but skip the wire format; a real fixture catches both.
"""

from __future__ import annotations

import asyncio
import gc
import json
import socket
from contextlib import asynccontextmanager, suppress
from typing import Any, Awaitable, Callable

import pytest_asyncio
import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from sse_starlette.sse import AppStatus
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


ToolHandler = Callable[..., Awaitable[str]]


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MockMCPHandle:
    """Test-side handle to the running mock MCP server.

    ``mcp_url`` is the streamable-HTTP endpoint to pass to
    ``streamablehttp_client``. Use ``register(name, handler, schema=...)``
    to wire a tool name to an async handler that returns a string
    (JSON-encoded if the caller expects ``${result.*}`` flattening).
    """

    def __init__(self, port: int):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.mcp_url = f"{self.url}/mcp"
        self._tools: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        description: str = "",
        schema: dict[str, Any] | None = None,
    ) -> None:
        self._tools[name] = {
            "tool": Tool(
                name=name,
                description=description or handler.__doc__ or name,
                inputSchema=schema or {"type": "object", "properties": {}},
            ),
            "handler": handler,
        }

    def clear(self) -> None:
        self._tools.clear()


@pytest_asyncio.fixture
async def mock_mcp():
    """Yield a running mock MCP server. Tests register tools, run code that
    hits ``handle.mcp_url``, then the fixture tears the server down."""
    # sse-starlette's AppStatus.should_exit is PROCESS-GLOBAL: its shutdown
    # watcher finds a uvicorn Server via the SIGTERM handler and latches the
    # flag True when that server shuts down. Once latched, every later
    # in-process EventSourceResponse — any test, any event loop — cancels
    # itself before sending headers, so streamable-http requests hang until
    # timeout. Clear any latch a previous server left behind.
    AppStatus.should_exit = False

    port = _find_free_port()
    handle = MockMCPHandle(port)

    server = Server("mock-mcp")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [v["tool"] for v in handle._tools.values()]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        entry = handle._tools.get(name)
        if entry is None:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"no such tool: {name}"}),
            )]
        result = await entry["handler"](**(arguments or {}))
        return [TextContent(type="text", text=result)]

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    @asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    async def _health(_req):
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/health", endpoint=_health),
            Mount("/mcp", app=session_manager.handle_request),
        ],
        lifespan=lifespan,
    )

    cfg = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        access_log=False,
    )
    uv_server = uvicorn.Server(cfg)
    task = asyncio.create_task(uv_server.serve())

    # Wait until the server is accepting connections (uvicorn flips
    # ``started`` once the socket binds + lifespan startup finishes).
    for _ in range(100):
        if uv_server.started:
            break
        await asyncio.sleep(0.02)
    else:
        task.cancel()
        raise RuntimeError("mock_mcp uvicorn server did not start in 2s")

    try:
        yield handle
    finally:
        # A test that abandons an in-flight streamable-HTTP call (client-side
        # timeout cancelling mid-protocol) leaves the client's async
        # generators suspended in their cleanup. Collect + tick HERE so they
        # finalize on the loop they were created on, while the server is
        # still up, instead of on a later test's event loop (where the anyio
        # teardown would run in a foreign task context and log errors).
        gc.collect()
        await asyncio.sleep(0.1)

        uv_server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError, BaseException):
                await task

        # This shutdown is exactly what latches AppStatus.should_exit (the
        # watcher polls every 0.5s and a drained-but-lingering connection
        # from an abandoned call keeps uvicorn shutting down long enough for
        # it to notice). Clear it again so this test can't poison the next.
        AppStatus.should_exit = False
