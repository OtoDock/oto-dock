"""The loop a program of the platform's own starts with, inside a session's sandbox.

This text never runs in the proxy. ``sandbox_program.program_text`` joins it
with one program's own text (its ``OPERATIONS`` table) and a last line that
calls ``_serve``, and the compute machine starts the whole inside the
session's sandbox (``sandbox_program.SandboxProgram``). So it imports nothing
of the proxy, uses the standard library only and runs on Python 3.10, the
oldest ``python3`` a sandbox holds. It knows nothing of what a program does.

It speaks the MCP stdio protocol in its smallest form, one JSON line a
message: ``initialize``, ``tools/list``, ``tools/call``, ``ping``. An
operation is a function of ``(facts, args)`` that answers a dict. The facts
are the program's first argument, as JSON, from the platform. An answer goes
back as one text block of JSON in ASCII, so the streams' encoding never
matters. An operation that raises answers ``{"ok": false, "code": "failed"}``:
the loop never ends on one call.
"""

import json
import sys

#: What an operation that raised, or answered no dict, is answered with.
_FAILED = "failed"
#: What a call of a name the program does not have is answered with.
_UNKNOWN = "unknown_operation"
#: The protocol version answered to a client that names none.
_PROTOCOL = "2024-11-05"


def _answer(operations, facts, name, args):
    """One operation's answer, always a dict with ``ok``."""
    operation = operations.get(name) if isinstance(name, str) else None
    if operation is None:
        return {"ok": False, "code": _UNKNOWN}
    try:
        out = operation(facts, args if isinstance(args, dict) else {})
    except Exception as e:  # noqa: BLE001  # an operation's own fault is an answer, never the loop's end
        return {"ok": False, "code": _FAILED, "detail": f"{type(e).__name__}: {e}"}
    if not isinstance(out, dict) or "ok" not in out:
        return {"ok": False, "code": _FAILED, "detail": "the operation gave no answer"}
    return out


def _handle(operations, facts, message):
    """The reply to one message, or None for a notice that takes none."""
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None or not isinstance(method, str):
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or _PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "oto-program", "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [
            {"name": name, "description": "", "inputSchema": {"type": "object"}}
            for name in sorted(operations)
        ]}
    elif method == "tools/call":
        answer = _answer(operations, facts, params.get("name"), params.get("arguments"))
        result = {"content": [{"type": "text", "text": json.dumps(answer)}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": "unknown method"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _serve(operations, argv=None, stdin=None, stdout=None):
    """Answer every line of ``stdin`` until it ends. A line that is no JSON
    object is passed over, as a notice is."""
    argv = sys.argv if argv is None else argv
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    try:
        facts = json.loads(argv[1]) if len(argv) > 1 else {}
    except ValueError:
        facts = {}
    if not isinstance(facts, dict):
        facts = {}
    for raw in stdin:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        reply = _handle(operations, facts, message)
        if reply is not None:
            stdout.write(json.dumps(reply).encode("ascii") + b"\n")
            stdout.flush()
