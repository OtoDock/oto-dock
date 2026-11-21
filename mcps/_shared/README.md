# `mcps/_shared`: reusable Docker-MCP building blocks

Shared, **MCP-agnostic** components. Each consumer keeps its own copy in its folder, byte for byte the same as
the one here. Edit the copy here first, then every consumer's copy.

## `stream_sidecar.py`: session-lifecycle sidecar

A thin streaming reverse-proxy that sits in front of a **streamable-HTTP** MCP
server (which runs on an internal port). Fully env-driven. It knows nothing about
the MCP behind it. It adds the session lifecycle the OtoDock layer needs:

- It maps the OtoDock `?session_id=<oto>` (injected by the proxy on every
  request) to the server's `mcp-session-id`.
- It **idle-GCs** abandoned sessions (`SESSION_IDLE_S`) via a `DELETE`. That is
  the backstop for a CLI that exits without the session-terminating `DELETE`.
  Such an exit otherwise leaks the server's per-session state, e.g. a browser
  context.
- It exposes **`POST /internal/close-session {"session_id"}`**, so the OtoDock
  proxy (`proxy/services/infra/browser_session.py`) tears a session down the
  instant it kills the agent session.
- It exposes **`GET /internal/sessions?except_prefix=<p>`**, which answers
  `{"open": n}`: the sessions whose OtoDock id does not start with `<p>`.
  Camoufox's health checker leaves its own sessions out this way to tell when
  the browser is idle. No session ids leave the container.

Everything else **streams straight through** (transparent proxy). The
long-lived server-to-client SSE `GET` has no read timeout. A request/response
call is bounded by `OTO_MCP_CALL_TIMEOUT_S`.

An idle SSE stream gets periodic keepalive comments, and every write to an
attached client refreshes its session. So a quiet client that stays connected
is never idle-GC'd. A request whose session the server no longer knows (a 404
upstream) drops it from the map.

It deliberately does **not** keep a session alive on a client's behalf. A
session lives while its client holds the standalone `GET` and answers the
server's heartbeat pings. There is a ping every 3 s from the session's first
tool call. The session is closed when one goes unanswered for 5 s.

### Env contract

| var | default | meaning |
|---|---|---|
| `MCP_UPSTREAM` | `http://127.0.0.1:8930` | the internal MCP server URL |
| `ROUTER_PORT` | `8931` | the public port the sidecar listens on |
| `SESSION_IDLE_S` | `600` | idle-GC: tear down a session idle this long |
| `SSE_KEEPALIVE_S` | `20` | client-SSE idle keepalive-comment interval |
| `OTO_MCP_CALL_TIMEOUT_S` | `90` | read limit of a request/response call (a wedged tool call answers 502) |
| `SCREENSHOTS_DIR` | `/screenshots` | the shared screenshots folder the GC pass keeps small (nothing happens when it does not exist) |
| `SCREENSHOTS_MAX_AGE_S` | `2400` | a screenshot older than this is removed |
| `SCREENSHOTS_MAX_COUNT` | `200` | past the newest this many, a screenshot older than the grace is removed |
| `SCREENSHOTS_GRACE_S` | `180` | the count limit never removes a screenshot younger than this |

### How to use it (vendored copy)

Consumer MCPs are **self-contained**. Vendor a copy of `stream_sidecar.py` into
the MCP's own folder, byte for byte the same as this reference copy. The
community catalog's `camoufox/stream_sidecar.py` is such a copy. Then copy it
in the MCP's `Dockerfile`:

```dockerfile
RUN pip install --no-cache-dir aiohttp
COPY stream_sidecar.py /app/stream_sidecar.py
```

(The old BuildKit `additional_contexts` mount of `../../_shared` was dropped:
catalog installs build from the MCP folder alone.)

Then run it from the entrypoint (after the MCP server is up on the internal port):

```bash
MCP_UPSTREAM=http://127.0.0.1:8930 ROUTER_PORT=8931 python3 /app/stream_sidecar.py &
```

The first consumer is the community catalog's `camoufox`.

### Tests

Unit tests for the pure session-map logic live in the proxy's test suite
rather than in this tree. The file is
**`proxy/tests/mcp/test_stream_sidecar.py`**, and it points its import back
here. The tests cover:

- open/refresh
- the oto-to-mcp mapping and back
- the oto-reuse remap guard
- the attached-stream refresh
- the session count
- one GC pass

aiohttp is imported lazily, so the test file runs with no deps:

```bash
python3 proxy/tests/mcp/test_stream_sidecar.py    # standalone, no pytest / aiohttp / DB
```

The aiohttp-backed handlers are verified live (E2E over a real MCP), not in unit
tests.

## `tool_args.py`: a tool call's arguments, checked with the schema it was listed with

The MCP SDK checks a call's arguments by default and answers the first
problem without naming the argument. It also refuses a value that misses an
enum only by its case. A custom MCP registers its handler with
`@server.call_tool(validate_input=False)` and checks the arguments itself:

```python
from _tool_args import ToolArgs

TOOL_ARGS = ToolArgs()

@server.list_tools()
async def list_tools():
    return TOOL_ARGS.listed([...])          # records each tool's inputSchema

@server.call_tool(validate_input=False)
async def call_tool(name, arguments):
    arguments, refusal = TOOL_ARGS.check(name, arguments)
    if refusal is not None:
        return refusal                      # CallToolResult, isError=True
    ...
```

`check` takes a string that equals exactly one member of its enum, once both
are folded, as that member. Folding covers case, and `-` or a space read as
`_`. `check` walks nested objects and array items. It never guesses between
two members and never coerces a type.

What is still invalid is refused with the SDK's own validation steps (the
same draft, `check_schema`, no format checker). The refusal has:

- every problem named by its argument (`operations[2].op`, `notify.severity`)
- an enum's allowed values listed
- the SDK's own first problem first
- five at most
- the tool to call again

A tool that was never listed passes unchecked, as with the SDK. A schema
jsonschema cannot read raises, which the SDK answers as an error result.

### How to use it (vendored copy)

Copy this file into the MCP's folder as `_tool_args.py`, byte for byte. An MCP
imports nothing outside its own folder, and a Docker MCP's `COPY *.py` takes
the file along. `proxy/tests/mcp/test_tool_args.py` holds every copy equal to
this one. It also calls each server through the SDK with an argument it must
refuse. The file holds no module-level state: the copies share one module
name.
