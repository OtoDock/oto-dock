#!/usr/bin/env python3
"""Stdin interceptor for stdio MCP servers (shared proxy + satellite).

Wraps an MCP subprocess: parses JSON-RPC messages from the agent on stdin,
intercepts ``tools/call`` requests, and applies path translation before
forwarding the (possibly rewritten) message to the real MCP's stdin:

  * **Path translation** — for tools declared in
    ``OTO_TOOL_ARG_PATHS``, path args are resolved/translated via
    ``/v1/hooks/resolve-tool-arg-paths`` (a denied path synthesizes a JSON-RPC
    error).

Stdout + stderr stream byte-for-byte from the MCP back to the agent; the
interceptor never parses outbound traffic.

NOTE: per-MCP-tool *permission* gating is NOT done here. Codex gates configured
MCP tool calls natively via ``mcpServer/elicitation/request`` (handled by
``core/layers/codex/codex_approvals.py`` → ``decide_tool_permission``); the
Claude CLI gates MCP via its PreToolUse hook. This interceptor is purely a
path-translation choke point for remote stdio MCPs.

Usage::

    python stdio_path_interceptor.py [--manifest <path>] -- <real-command...>

The ``--manifest`` arg is optional and currently unused. Config is via env.

Env (set by the proxy mcp-config builder / the satellite wrap):
  * ``PROXY_URL`` — base URL (local tunnel) e.g. ``http://127.0.0.1:8473``
  * ``PROXY_API_KEY`` — bearer token for hook auth (path translation only)
  * ``OTO_SESSION_ID`` — for the hook's session lookup
  * ``OTO_TOOL_ARG_PATHS`` — JSON-serialized path declarations (or empty)
  * ``OTO_MCP_FETCH_TOKEN`` — credential-broker capability token: if
    set, this MCP's secrets are fetched from ``/v1/hooks/mcp-credentials`` at
    spawn and merged into the child env; the token + ``OTO_STRIP_KEYS`` +
    ``OTO_BEARER_*`` are stripped before exec so the MCP never carries them.

With no declarations and no fetch token set, the interceptor is pure passthrough.

Stdlib-only — no httpx / requests dependency so the interceptor runs
on any Python 3.10+ regardless of which venv invoked it. Canonical copy lives
at ``proxy/core/stdio_path_interceptor.py`` and is vendored to the satellite via
``scripts/sync-satellite-code.sh`` (the satellite drift-checks ``self_hash()``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any


def self_hash() -> str:
    """SHA256 of this module's source — used by the satellite drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Diagnostics (stderr only — never write to stdout, that's the JSON-RPC pipe)
# ---------------------------------------------------------------------------


_DEBUG = os.environ.get("OTO_INTERCEPTOR_DEBUG", "").lower() in ("1", "true")


def _log(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[interceptor] {msg}\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# JSONPath subset walker (mirrors mcp_registry's validator)
# ---------------------------------------------------------------------------


_JSONPATH_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tokenize_jsonpath(path: str) -> list[tuple[str, str]]:
    """Tokenize a JSONPath expression into (kind, value) tuples.

    Kinds: ``"field"`` (value is the name) or ``"wildcard"`` (value
    is empty).
    """
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            i += 1
            continue
        if path[i:i + 3] == "[*]":
            tokens.append(("wildcard", ""))
            i += 3
            continue
        m = _JSONPATH_IDENT.match(path, i)
        if not m:
            raise ValueError(
                f"interceptor: cannot tokenize JSONPath {path!r} at {i}"
            )
        tokens.append(("field", m.group(0)))
        i = m.end()
    return tokens


def _walk(
    obj: Any, tokens: list[tuple[str, str]],
) -> list[tuple[Any, Any]]:
    """Walk an object tree, returning ``(parent, key)`` pairs where
    ``parent[key]`` is a matched value. The caller can read or write
    ``parent[key]`` to rewrite the value in-place.
    """
    if not tokens:
        return []
    cur_kind, cur_name = tokens[0]
    rest = tokens[1:]
    out: list[tuple[Any, Any]] = []
    if cur_kind == "field":
        if not isinstance(obj, dict) or cur_name not in obj:
            return []
        if not rest:
            out.append((obj, cur_name))
        else:
            out.extend(_walk(obj[cur_name], rest))
    elif cur_kind == "wildcard":
        if not isinstance(obj, list):
            return []
        for idx in range(len(obj)):
            if not rest:
                out.append((obj, idx))
            else:
                out.extend(_walk(obj[idx], rest))
    return out


# ---------------------------------------------------------------------------
# Path type detection (mirrors path_policy_v2.is_path_string)
# ---------------------------------------------------------------------------


_NON_PATH_PREFIXES = (
    "http://", "https://", "ftp://", "ftps://", "file://",
    "data:", "base64:", "blob:",
)
_TEMPLATE_MARKERS = ("${", "{{")


def _looks_like_path(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    lo = value.lower()
    for prefix in _NON_PATH_PREFIXES:
        if lo.startswith(prefix):
            return False
    for marker in _TEMPLATE_MARKERS:
        if marker in value:
            return False
    if len(value) > 500 and "/" not in value and "\\" not in value:
        return False
    return True


# ---------------------------------------------------------------------------
# Hook client
# ---------------------------------------------------------------------------


class HookError(Exception):
    pass


def _hook_post(endpoint: str, body: dict, timeout: float = 10.0) -> dict:
    """Synchronous POST to a proxy hook. Returns parsed JSON.

    Raises HookError on any non-200 or transport failure — caller
    treats this as fail-closed. ``timeout`` is short for path resolution but
    long (a human-approval window) for the permission gate.
    """
    proxy_url = os.environ.get("PROXY_URL", "").rstrip("/")
    api_key = os.environ.get("PROXY_API_KEY", "")
    if not proxy_url:
        raise HookError("PROXY_URL not set")
    url = f"{proxy_url}{endpoint}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else "",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except OSError:
            body_text = ""
        raise HookError(
            f"hook {endpoint} returned HTTP {e.code}: {body_text[:200]}"
        ) from None
    except (urllib.error.URLError, OSError) as e:
        raise HookError(f"hook {endpoint} unreachable: {e}") from None
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HookError(f"hook {endpoint} returned malformed JSON: {e}") from None


# ---------------------------------------------------------------------------
# Credential broker — fetch this MCP's secrets at spawn
# ---------------------------------------------------------------------------


def _pop_ci(env: dict, name: str) -> str | None:
    """Case-insensitive pop (Windows env var names are case-insensitive, but a
    plain ``dict(os.environ)`` copy compares case-sensitively)."""
    if name in env:
        return env.pop(name)
    lower = name.lower()
    for key in list(env):
        if key.lower() == lower:
            return env.pop(key)
    return None


def _fetch_mcp_credentials(
    token: str, timeout: float = 10.0, attempts: int = 4,
) -> dict | None:
    """Fetch this MCP's secret bundle from the broker, authenticated by the
    per-(session, mcp) capability token — NOT ``PROXY_API_KEY`` (the broker
    endpoint rejects the session JWT, which the agent's own bash holds). Returns
    ``{"env": {...}, "http_bearer": ...}`` or ``None`` on failure.

    Fail-closed: on failure we inject nothing. The loopback tunnel can answer 503
    right after a (re)connect (before the satellite WS re-auths), so retry with
    backoff; 401/404 are terminal (bad/expired token, or no bundle for this
    session) and are not retried."""
    proxy_url = os.environ.get("PROXY_URL", "").rstrip("/")
    if not proxy_url or not token:
        return None
    url = f"{proxy_url}/v1/hooks/mcp-credentials"
    backoff = 0.5
    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            data=b"",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 404):
                _log(f"mcp-credentials denied: HTTP {e.code} (terminal)")
                return None
            _log(f"mcp-credentials HTTP {e.code} (attempt {attempt + 1}/{attempts})")
        except (urllib.error.URLError, OSError) as e:
            _log(f"mcp-credentials unreachable: {e} (attempt {attempt + 1}/{attempts})")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            _log(f"mcp-credentials malformed response: {e}")
            return None
        if attempt < attempts - 1:
            time.sleep(backoff)
            backoff *= 2
    return None


def _apply_broker_credentials(child_env: dict) -> None:
    """If a per-(session, mcp) capability token is present, fetch this MCP's
    secret env at spawn and merge it into ``child_env``; then strip ALL
    broker-only vars so the MCP child never carries the token or any HTTP bearer.

    Fail-closed: a failed fetch injects nothing. During the rollout window the
    secrets are still in the shipped config (idempotent re-inject); once they're
    stripped at the source the MCP simply surfaces a clean auth error — a secret
    we couldn't fetch is never injected. ``OTO_STRIP_KEYS`` (env_injection creds
    now broker-sourced) + ``OTO_BEARER_*`` are stripped case-insensitively; both
    are dormant until later phases inject them."""
    token = _pop_ci(child_env, "OTO_MCP_FETCH_TOKEN")
    if token:
        creds = _fetch_mcp_credentials(token)
        if creds and isinstance(creds.get("env"), dict):
            child_env.update({str(k): str(v) for k, v in creds["env"].items()})
    strip_csv = _pop_ci(child_env, "OTO_STRIP_KEYS") or ""
    for name in (s.strip() for s in strip_csv.split(",") if s.strip()):
        _pop_ci(child_env, name)
    for key in [k for k in child_env if k.upper().startswith("OTO_BEARER_")]:
        child_env.pop(key, None)


# ---------------------------------------------------------------------------
# Tool-call interception
# ---------------------------------------------------------------------------


def _per_tool_declarations(
    all_decls: list[dict], tool_name: str,
) -> list[dict]:
    return [d for d in all_decls if d.get("tool") == tool_name]


def _build_resolve_items(
    tool_args: Any, tool_decls: list[dict],
) -> list[tuple[Any, Any, dict]]:
    """For each declaration, walk the args and collect (parent, key, decl)
    triples where the value at parent[key] looks like a path.
    """
    out: list[tuple[Any, Any, dict]] = []
    if not isinstance(tool_args, dict):
        return out
    for decl in tool_decls:
        json_path = decl.get("json_path") or ""
        try:
            tokens = _tokenize_jsonpath(json_path)
        except ValueError:
            _log(f"skipping malformed json_path {json_path!r}")
            continue
        for parent, key in _walk(tool_args, tokens):
            value = parent[key]
            if _looks_like_path(value):
                out.append((parent, key, decl))
    return out


def _synthesize_error_response(
    request_msg: dict, summary: str,
) -> bytes:
    """Build a JSON-RPC error response for a denied tool call."""
    err = {
        "jsonrpc": "2.0",
        "id": request_msg.get("id"),
        "error": {
            "code": -32603,
            "message": summary,
        },
    }
    return (json.dumps(err) + "\n").encode("utf-8")


def _process_inbound_line(
    line: bytes,
    declarations: list[dict],
    session_id: str,
) -> tuple[bytes, bytes]:
    """Process one JSON-RPC line from the agent.

    Returns ``(to_mcp, to_cli_error)``:
      * ``to_mcp`` — bytes to forward to the MCP's stdin (the rewritten
        line, or the original line if no rewrite was needed).
      * ``to_cli_error`` — bytes to write to the agent's stdout when the
        request is rejected (synthesized JSON-RPC error). When empty,
        the call is forwarded normally.
    """
    stripped = line.rstrip(b"\r\n")
    if not stripped:
        return line, b""
    try:
        msg = json.loads(stripped)
    except json.JSONDecodeError:
        return line, b""
    if not isinstance(msg, dict):
        return line, b""
    if msg.get("method") != "tools/call":
        return line, b""

    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return line, b""
    tool_name = params.get("name") or ""
    if not tool_name:
        return line, b""
    tool_args = params.get("arguments")

    tool_decls = _per_tool_declarations(declarations, tool_name)
    if not tool_decls:
        return line, b""

    triples = _build_resolve_items(tool_args, tool_decls)
    if not triples:
        return line, b""

    body = {
        "session_id": session_id,
        "tool": tool_name,
        "items": [
            {
                "value": parent[key],
                "write": (decl.get("mode") == "write"),
                "json_path": decl.get("json_path", ""),
            }
            for parent, key, decl in triples
        ],
    }
    try:
        resp = _hook_post("/v1/hooks/resolve-tool-arg-paths", body)
    except HookError as e:
        summary = f"path policy unavailable: {e}"
        _log(summary)
        return b"", _synthesize_error_response(msg, summary)

    resolutions = resp.get("items") or []
    if len(resolutions) != len(triples):
        summary = (
            f"path policy returned {len(resolutions)} items for "
            f"{len(triples)} requested"
        )
        _log(summary)
        return b"", _synthesize_error_response(msg, summary)

    denials: list[str] = []
    for (parent, key, _decl), resolution in zip(triples, resolutions):
        if not resolution.get("allowed", False):
            denials.append(
                f"{parent[key]!r}: {resolution.get('error', 'rejected')}"
            )
        else:
            parent[key] = resolution.get("access_path", parent[key])
    if denials:
        summary = "Path policy rejected: " + "; ".join(denials)
        _log(summary)
        return b"", _synthesize_error_response(msg, summary)

    rewritten = (json.dumps(msg) + "\n").encode("utf-8")
    return rewritten, b""


# ---------------------------------------------------------------------------
# Pump threads
# ---------------------------------------------------------------------------


def _pump_inbound(
    src,
    mcp_stdin,
    cli_stdout,
    declarations: list[dict],
    session_id: str,
    stop_event: threading.Event,
) -> None:
    """CLI stdin → MCP stdin (with interception)."""
    while not stop_event.is_set():
        line = src.readline()
        if not line:
            break
        try:
            to_mcp, to_cli_err = _process_inbound_line(
                line, declarations, session_id,
            )
        except Exception as e:  # noqa: BLE001
            _log(f"inbound exception, passing through: {e}")
            to_mcp, to_cli_err = line, b""
        if to_cli_err:
            try:
                cli_stdout.write(to_cli_err)
                cli_stdout.flush()
            except (BrokenPipeError, OSError):
                break
            continue
        if to_mcp:
            try:
                mcp_stdin.write(to_mcp)
                mcp_stdin.flush()
            except (BrokenPipeError, OSError):
                break
    with contextlib.suppress(BrokenPipeError, OSError):
        mcp_stdin.close()


def _pump_outbound(
    src, dst, stop_event: threading.Event,
) -> None:
    """MCP stdout (or stderr) → CLI stdout (or stderr). Pure passthrough."""
    while not stop_event.is_set():
        chunk = src.readline()
        if not chunk:
            break
        try:
            dst.write(chunk)
            dst.flush()
        except (BrokenPipeError, OSError):
            break


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _split_argv(argv: list[str]) -> list[str]:
    """Split argv at ``--`` and return the wrapped command. Drops any
    interceptor-level flags before the separator (currently
    ``--manifest <path>`` is accepted-but-unused; reserved for a future
    unknown-tool-field warning).
    """
    if "--" in argv:
        idx = argv.index("--")
        return argv[idx + 1:]
    # Fallback: whole argv is the wrapped command.
    return argv


def main() -> int:
    wrapped = _split_argv(sys.argv[1:])
    if not wrapped:
        sys.stderr.write(
            "interceptor: no wrapped command (expected `-- <cmd> [args...]`)\n"
        )
        return 2

    raw_decls = os.environ.get("OTO_TOOL_ARG_PATHS", "")
    declarations: list[dict] = []
    if raw_decls:
        try:
            parsed = json.loads(raw_decls)
            if isinstance(parsed, list):
                declarations = [d for d in parsed if isinstance(d, dict)]
        except json.JSONDecodeError:
            _log("ignoring malformed OTO_TOOL_ARG_PATHS")

    session_id = os.environ.get("OTO_SESSION_ID", "")
    if declarations and not session_id:
        _log("no OTO_SESSION_ID set; declarations will fail-closed")

    # Build the child env. Strip the interceptor-only path-decl var (our
    # contract, not the MCP's), then apply the credential broker: fetch this
    # MCP's secrets at spawn + strip every broker-only var before exec.
    child_env = dict(os.environ)
    child_env.pop("OTO_TOOL_ARG_PATHS", None)
    _apply_broker_credentials(child_env)

    _log(f"spawning {wrapped!r} (decls={len(declarations)})")
    try:
        proc = subprocess.Popen(  # noqa: S603 — wrapping a trusted MCP cmd
            wrapped,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            bufsize=0,
        )
    except (OSError, ValueError) as e:
        sys.stderr.write(f"interceptor: failed to spawn {wrapped[0]}: {e}\n")
        return 127

    stop = threading.Event()
    cli_stdin = sys.stdin.buffer
    cli_stdout = sys.stdout.buffer
    cli_stderr = sys.stderr.buffer

    t_in = threading.Thread(
        target=_pump_inbound,
        args=(cli_stdin, proc.stdin, cli_stdout, declarations, session_id, stop),
        name="interceptor-stdin",
        daemon=True,
    )
    t_out = threading.Thread(
        target=_pump_outbound,
        args=(proc.stdout, cli_stdout, stop),
        name="interceptor-stdout",
        daemon=True,
    )
    t_err = threading.Thread(
        target=_pump_outbound,
        args=(proc.stderr, cli_stderr, stop),
        name="interceptor-stderr",
        daemon=True,
    )
    t_in.start()
    t_out.start()
    t_err.start()

    rc = proc.wait()
    stop.set()
    # Brief join — pumps are daemons so a stuck reader doesn't block exit.
    for t in (t_in, t_out, t_err):
        t.join(timeout=2.0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
