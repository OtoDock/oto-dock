# `mcps/_shared` — reusable Docker-MCP building blocks

Shared, **MCP-agnostic** components mounted into individual Docker-MCP images at
build time. Single source of truth — edit here, every consumer rebuilds against it.

## `stream_sidecar.py` — session-lifecycle sidecar

A thin streaming reverse-proxy that sits in front of a **streamable-HTTP** MCP
server (which runs on an internal port). Fully env-driven; knows nothing about
the MCP behind it. It adds the session lifecycle the OtoDock layer needs:

- maps the OtoDock `?session_id=<oto>` (injected by the proxy on every request)
  → the server's `mcp-session-id`;
- **idle-GCs** abandoned sessions (`SESSION_IDLE_S`) via a `DELETE` — the backstop
  for a CLI that exits without the session-terminating `DELETE` (which otherwise
  leaks the server's per-session state, e.g. a browser context);
- exposes **`POST /internal/close-session {"session_id"}`** so the OtoDock proxy
  (`proxy/services/infra/browser_session.py`) tears a session down the instant it kills
  the agent session.

Everything else **streams straight through** (transparent proxy) with no
read-timeout, so long browser actions and the long-lived server→client SSE `GET`
both pass untouched. An idle SSE stream gets periodic keepalive comments.

It deliberately does **not** try to keep a session alive across a client GET drop
or a think-gap: `@playwright/mcp@0.0.55` evicts an idle isolated session on its
own (~10s, verified directly against the server with no sidecar in path), so a
re-`initialize` on the next call is unavoidable either way — and it's sub-second
given the proxy streaming fix. (A stream "broker" that keeps sessions alive
across drops was evaluated and rejected as not worth the complexity.)

### Env contract

| var | default | meaning |
|---|---|---|
| `MCP_UPSTREAM` | `http://127.0.0.1:8930` | the internal MCP server URL |
| `ROUTER_PORT` | `8931` | the public port the sidecar listens on |
| `SESSION_IDLE_S` | `600` | idle-GC: tear down a session idle this long |
| `SSE_KEEPALIVE_S` | `20` | client-SSE idle keepalive-comment interval |
| `OTO_MCP_SUPPRESS_SERVER_REQUESTS` | `0` | **temporary workaround** (see below) — strip the client's `roots` capability + reject the standalone SSE `GET` so a buggy MCP can't issue server→client requests (`roots/list`, `ping`) it then fails to correlate |

### Temporary workaround: `OTO_MCP_SUPPRESS_SERVER_REQUESTS`

Some streamable-HTTP MCP *server* builds issue server→client JSON-RPC requests
(`roots/list` when the client advertises the `roots` capability — claude-code
does — and periodic `ping`) but **fail to correlate the client's POSTed response**
back to the pending request. The server then blocks every first tool call until
its 60s server-request timeout fires, so a `browser_navigate` "hangs ~60s then
times out with a blank page."

This is confirmed on **`@playwright/mcp@0.0.55`** (the version `camoufox` pins for
patched-Firefox connect compatibility) — reproduced driving the MCP *directly*
(no sidecar, no satellite tunnel). `@playwright/mcp@0.0.68` fixes it but is **not
camoufox-compatible** (hard-pinned to an npm playwright *alpha* that can't be
matched to camoufox 0.4.11's PyPI playwright). So until camoufox can move to a
fixed, connect-compatible build, set `OTO_MCP_SUPPRESS_SERVER_REQUESTS=1` for that
MCP. The sidecar then prevents the server from ever issuing those requests by
(a) stripping `capabilities.roots` from `initialize`, and (b) rejecting the
optional standalone SSE `GET` with `405` (spec-allowed — a compliant client
receives results on its POST responses instead). A browser MCP needs neither.

**To remove:** once the MCP runs a build that correlates server→client responses
correctly, unset the flag in that MCP's compose env and confirm a
`browser_navigate` returns sub-second. The gated code in `stream_sidecar.py` is
self-contained and can then be deleted.

### How to use it (vendored copy)

Consumer MCPs are **self-contained**: vendor a copy of `stream_sidecar.py` into
the MCP's own folder (kept in sync with this reference copy) and copy it in the
MCP's `Dockerfile`:

```dockerfile
RUN pip install --no-cache-dir aiohttp
COPY stream_sidecar.py /app/stream_sidecar.py
```

(The old BuildKit `additional_contexts` mount of `../../_shared` was dropped —
catalog installs build from the MCP folder alone.)

Then run it from the entrypoint (after the MCP server is up on the internal port):

```bash
MCP_UPSTREAM=http://127.0.0.1:8930 ROUTER_PORT=8931 python3 /app/stream_sidecar.py &
```

The first consumer is the community catalog's `camoufox`.

### Tests

Unit tests for the pure session-map logic (open/refresh, the oto↔mcp mapping, the
oto-reuse remap guard) live in the proxy's test suite rather than in this tree:
**`proxy/tests/mcp/test_stream_sidecar.py`** (it points its import back here).
aiohttp is imported lazily, so it runs with no deps:

```bash
python3 proxy/tests/mcp/test_stream_sidecar.py    # standalone, no pytest / aiohttp / DB
```

The aiohttp-backed handlers are verified live (E2E over a real MCP), not in unit
tests.

## `tool_args.py` — a tool call's arguments, checked with the schema it was listed with

The MCP SDK checks a call's arguments by default and answers the first
problem without naming the argument; it also refuses a value that misses an
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

`check` takes a string that equals exactly one member of its enum once both
are folded (case, and `-` or a space read as `_`) as that member, walking
nested objects and array items; it never guesses between two members and
never coerces a type. What is still invalid is refused with the SDK's own
validation steps (the same draft, `check_schema`, no format checker): every
problem named by its argument (`operations[2].op`, `notify.severity`), an
enum's allowed values listed, the SDK's own first problem first, five at
most, and the tool to call again. A tool that was never listed passes
unchecked, as with the SDK; a schema jsonschema cannot read raises, which the
SDK answers as an error result.

### How to use it (vendored copy)

Copy this file into the MCP's folder as `_tool_args.py`, byte for byte (an MCP
imports nothing outside its own folder; a Docker MCP's `COPY *.py` takes it
along). `proxy/tests/mcp/test_tool_args.py` holds every copy equal to this one
and calls each server through the SDK with an argument it must refuse. The
file holds no module-level state: the copies share one module name.
