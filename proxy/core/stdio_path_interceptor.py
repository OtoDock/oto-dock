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

**The stand-in** (``OTO_TOOL_STANDIN`` in the env, set by the session's MCP
build): the interceptor answers the engine's questions (``initialize``, the
tool, prompt and resource lists, ``ping``) from the answers the platform saved
for this tool (``GET /v1/hooks/tool-listing``), and starts the real program at
the first request it cannot answer, normally the first ``tools/call``; from
then on it is the interceptor above. With no saved answers it starts the
program at the engine's ``initialize`` and saves what the program answered.
Until the start it loads nothing heavy: a session's stand-ins cost a few MB
each where the programs cost tens.

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
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any

# urllib's request module is loaded at its first use (``_no_proxy_urllib``):
# it doubles a waiting stand-in's memory.


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


_URLLIB = {"opener_owed": False}


def _no_proxy_urllib() -> None:
    """Install, once, urllib's opener with no proxy between, when the script
    runs as itself: PROXY_URL is a loopback address (the platform's own
    listener, or a satellite's tunnel). urllib's default opener takes the
    environment's proxy and, on Windows, Internet Options' (whose <local>
    exemption skips only dotless hosts, never 127.0.0.1). Never at import: a
    test that loads the script keeps its own opener."""
    if not _URLLIB["opener_owed"]:
        return
    import urllib.request
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    _URLLIB["opener_owed"] = False


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
    import urllib.error
    import urllib.request
    _no_proxy_urllib()
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


# While the platform cannot be reached (a restart, a dropped link: a
# machine's local link answers 502 or 503 at once while its socket is down)
# the fetch is held and asked again for up to _HOLD_S, then given up: the
# permission gate's numbers (``hooks/permission_gate.py``), so a tool started
# during a restart gets its secrets once the platform is back.
_HOLD_S = 120.0
_BACKOFF = (2.0, 4.0, 8.0, 15.0)  # then 15 s steps until the hold ends
# A refused token or no bundle right after a restart: a machine's sessions
# are taken back a moment after it signs in again. A short ladder, then the
# answer stands.
_AUTH_LADDER = (2.0, 4.0, 8.0)


def _fetch_mcp_credentials(token: str, timeout: float = 10.0, *, hold: bool = True) -> tuple[dict | None, bool]:
    """Fetch this MCP's secret bundle from the broker, authenticated by the
    per-(session, mcp) capability token — NOT ``PROXY_API_KEY`` (the broker
    endpoint rejects the session JWT, which the agent's own bash holds).
    Returns ``({"env": {...}} or None, waited)``: ``waited`` when the platform
    did not answer at the first ask.

    Fail-closed: on failure we inject nothing. With ``hold``, a platform
    that cannot be reached (no answer, a 502, a 503 or a 504, as the
    permission gate reads them) is asked again with the backoff for up to
    ``_HOLD_S``, and a 401 or 404 on the short ladder; any other answer is
    final. Without ``hold`` one ask is all."""
    import urllib.error
    import urllib.request
    _no_proxy_urllib()
    proxy_url = os.environ.get("PROXY_URL", "").rstrip("/")
    if not proxy_url or not token:
        return None, False
    url = f"{proxy_url}/v1/hooks/mcp-credentials"
    started: float | None = None
    away = ladder = 0
    while True:
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
                return json.loads(resp.read().decode("utf-8")), started is not None
        except urllib.error.HTTPError as e:
            if e.code in (401, 404):
                if not hold or ladder >= len(_AUTH_LADDER):
                    _log(f"mcp-credentials denied: HTTP {e.code} (terminal)")
                    return None, started is not None
                _log(f"mcp-credentials HTTP {e.code}, asked again in {_AUTH_LADDER[ladder]:.0f} s")
                started = time.monotonic() if started is None else started
                time.sleep(_AUTH_LADDER[ladder])
                ladder += 1
                continue
            if e.code not in (502, 503, 504):
                _log(f"mcp-credentials HTTP {e.code} (terminal)")
                return None, started is not None
            _log(f"mcp-credentials HTTP {e.code}: the platform is away")
        except (urllib.error.URLError, OSError) as e:
            _log(f"mcp-credentials unreachable: {e}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            _log(f"mcp-credentials malformed response: {e}")
            return None, started is not None
        if not hold:
            return None, False
        now = time.monotonic()
        started = now if started is None else started
        wait = _BACKOFF[min(away, len(_BACKOFF) - 1)]
        if now + wait > started + _HOLD_S:
            _log("mcp-credentials: the platform stayed away, nothing injected")
            return None, True
        time.sleep(wait)
        away += 1


def _apply_broker_credentials(child_env: dict, *, hold: bool = True) -> tuple[bool, bool]:
    """If a per-(session, mcp) capability token is present, fetch this MCP's
    secret env at spawn and merge it into ``child_env``; then strip ALL
    broker-only vars so the MCP child never carries the token or any HTTP bearer.

    Fail-closed: a failed fetch injects nothing. During the rollout window the
    secrets are still in the shipped config (idempotent re-inject); once they're
    stripped at the source the MCP simply surfaces a clean auth error — a secret
    we couldn't fetch is never injected. ``OTO_STRIP_KEYS`` (env_injection creds
    now broker-sourced) + ``OTO_BEARER_*`` are stripped case-insensitively; both
    are dormant until later phases inject them. Answers ``(fetched, waited)``:
    False first when a token's fetch got no answer (the stand-in then starts
    no tool that needs its secrets), and whether the fetch had to wait for
    the platform (``hold``: the fetch's own)."""
    token = _pop_ci(child_env, "OTO_MCP_FETCH_TOKEN")
    fetched, waited = True, False
    if token:
        creds, waited = _fetch_mcp_credentials(token, hold=hold)
        fetched_env = creds.get("env") if creds else None
        fetched = isinstance(fetched_env, dict)
        if isinstance(fetched_env, dict):
            child_env.update({str(k): str(v) for k, v in fetched_env.items()})
    strip_csv = _pop_ci(child_env, "OTO_STRIP_KEYS") or ""
    for name in (s.strip() for s in strip_csv.split(",") if s.strip()):
        _pop_ci(child_env, name)
    for key in [k for k in child_env if k.upper().startswith("OTO_BEARER_")]:
        child_env.pop(key, None)
    return fetched, waited


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
    for (parent, key, _decl), resolution in zip(triples, resolutions, strict=True):
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
        except Exception as e:  # noqa: BLE001  # the MCP pipe must keep flowing: a bad line passes through unchanged
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
# The stand-in: answer from the saved listing, start at the first call
# ---------------------------------------------------------------------------

#: The marker the session's MCP build sets on a tool that starts late: a JSON
#: object with the tool's version ``stamp``, the ``account`` it was built on (a
#: hash, "" for none), whether its start needs its ``fetch`` to answer (it has
#: secrets), and its ``label``.
STANDIN_ENV = "OTO_TOOL_STANDIN"

# The names whose values differ from one session to the next (or one start of
# the service manager to the next) while the tool's list does not: left out of
# the fingerprint, so two sessions of one person on one agent find one listing.
# A name missed here only makes every fingerprint new, and the tool then starts
# with the chat; it never serves a wrong list.
_VOLATILE_NAMES = frozenset({
    "OTO_SESSION_ID", "PROXY_API_KEY", "PROXY_URL", "OTO_MCP_FETCH_TOKEN", "OTO_TOKEN_RENEWAL",
    "OTO_CLI_TOKEN", "OTO_STRIP_KEYS", "OTO_SSH_KEY_DIR", "SSH_AUTH_SOCK", STANDIN_ENV,
    "OTO_TOOL_ARG_PATHS", "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "MANAGERPID",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO",
    "MCP_SERVER_CONNECTION_BATCH_SIZE",
})
_VOLATILE_SUFFIXES = ("_SESSION_ID", "_CHAT_ID")
_VOLATILE_PARTS = ("TOKEN", "SECRET", "PASSWORD")
_VOLATILE_PREFIXES = ("CLAUDE_CODE_MESSAGING_", "OTO_BEARER_")


def _volatile(name: str) -> bool:
    up = name.upper()
    return (up in _VOLATILE_NAMES or up.endswith(_VOLATILE_SUFFIXES)
            or any(part in up for part in _VOLATILE_PARTS) or up.startswith(_VOLATILE_PREFIXES))


# A PATH entry an engine makes per process: Codex puts its helper folder
# (``.../tmp/arg0-linux/codex-arg0<random>``) first in each tool's PATH.
_VOLATILE_PATH_PARTS = ("codex-arg0",)


def _stable_path(value: str) -> str:
    sep = ";" if ";" in value else ":"
    return sep.join(p for p in value.split(sep) if not any(v in p for v in _VOLATILE_PATH_PARTS))


def listing_fingerprint(env: dict, argv: list[str], stamp: str, account: str) -> str:
    """The key a tool's saved answers are kept under: its version, the account
    it runs on, its command line and the environment its program gets before
    its secrets, with the per-session names left out and the session's id
    blanked wherever a value carries it. Everything a tool's list can depend
    on (the role, the scopes, the settings, the machine's paths) is in it."""
    sid = str(env.get("OTO_SESSION_ID") or "")
    kept = {}
    for name, value in env.items():
        if _volatile(name):
            continue
        value = str(value)
        if name.upper() == "PATH":
            value = _stable_path(value)
        if len(sid) >= 8:
            value = value.replace(sid, "{session}")
        kept[name] = value
    doc = {"v": 1, "stamp": stamp, "account": account, "argv": [str(a) for a in argv], "env": kept}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()


def marker_fingerprint(marker: dict, env: dict, argv: list[str]) -> str:
    """A stand-in's fingerprint from its marker: the version stamp, and the
    account with whether the tool's start fetches secrets (a tool that gains
    its key, which names no account, is a new answer)."""
    account = f"{marker.get('account') or ''}|{int(bool(marker.get('fetch')))}"
    return listing_fingerprint(env, argv, str(marker.get("stamp") or ""), account)


# How long the platform's routes may take (the lookup, the admission): past it
# the stand-in goes on without the answer.
_ROUTE_TIMEOUT_S = 3.0
# How long the real start may take, from the spawn to the last list.
_START_TIMEOUT_S = 60.0
# A call held while the platform was away: its engine may have given it up
# meanwhile, so it is answered and never run late.
_RESTARTED = "OtoDock was restarting. Call the tool again."
_MAX_BODY = 4 * 1024 * 1024
_PRIVATE = "otodock-standin-"
# A client's probe before ``initialize`` for a newer handshake (Claude Code
# 2.1.322 sends ``server/discover`` first and falls back to ``initialize`` on
# an error, as the SDK servers answer it): the stand-in speaks the classic
# handshake, so it says it has no such method, with or without saved answers.
_PROBES = frozenset({"server/discover"})
_LISTS = {"tools/list": "tools", "prompts/list": "prompts", "resources/list": "resources",
          "resources/templates/list": "resourceTemplates"}
# Which capability a list needs (the lists of a capability the server lacks
# answer "method not found", as the server would).
_LIST_CAPABILITY = {"tools/list": "tools", "prompts/list": "prompts", "resources/list": "resources",
                    "resources/templates/list": "resources"}
# The engine's requests a program's own answers answer: a program that got
# only these since its start with nothing saved was never called.
_ANSWERABLE = frozenset({"initialize", "ping", "logging/setLevel", *_LISTS})
# How long a program stopped after its answers were kept may take to end on
# the end of its input before it is killed.
_STOP_WAIT_S = 3.0


def _route(method: str, path: str, token: str, body: dict | None = None,
           timeout: float = _ROUTE_TIMEOUT_S) -> tuple[int, Any]:
    """One request to the platform on PROXY_URL with the tool's fetch token,
    over a plain socket (no proxy may stand between, and urllib would double
    the stand-in's memory): ``(status, parsed body)``, ``(0, None)`` when it
    could not be asked. HTTP/1.0 with the connection closed by the answer; a
    chunked body (the satellite's tunnel streams one) is read too."""
    from urllib.parse import urlsplit
    proxy_url = os.environ.get("PROXY_URL", "")
    parts = urlsplit(proxy_url)
    if parts.scheme != "http" or not parts.hostname or not token:
        return 0, None
    # Compact and in UTF-8, never larger than what ``tool_listing.keep``
    # measures (compact, ASCII escapes): a list it accepts fits the bound.
    data = (json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if body is not None else b"")
    host_header = parts.netloc
    request = (f"{method} {path} HTTP/1.0\r\nHost: {host_header}\r\nAuthorization: Bearer {token}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n")
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout) as sock:
            sock.sendall(request.encode("latin-1") + data)
            chunks, size = [], 0
            while size <= _MAX_BODY:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
    except OSError as e:
        _log(f"route {path} unreachable: {e}")
        return 0, None
    raw = b"".join(chunks)
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return 0, None
    headers = {k.strip().lower(): v.strip() for k, _, v in
               (line.decode("latin-1").partition(":") for line in lines[1:])}
    if headers.get("transfer-encoding", "").lower() == "chunked":
        payload = _dechunk(payload)
    try:
        return status, json.loads(payload.decode("utf-8")) if payload else None
    except (ValueError, UnicodeDecodeError):
        return status, None


def _dechunk(data: bytes) -> bytes:
    out, pos = [], 0
    while True:
        end = data.find(b"\r\n", pos)
        if end < 0:
            break
        try:
            size = int(data[pos:end].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out.append(data[end + 2:end + 2 + size])
        pos = end + 2 + size + 2
    return b"".join(out)


def _version_told(requested: Any, saved: str) -> str:
    """The protocol version a server answers: the one asked for when it is
    not newer than the server's own (the SDKs' rule; versions are dates)."""
    if isinstance(requested, str) and requested and (not saved or requested <= saved):
        return requested
    return saved or (requested if isinstance(requested, str) else "")


def _tools_key(tools: list) -> str:
    """What a list says about each tool, its labels aside (the platform serves
    its own trusted labels in place of the server's)."""
    rows = sorted(
        [t.get("name"), t.get("description"), t.get("inputSchema"), t.get("outputSchema")]
        for t in tools if isinstance(t, dict))
    return json.dumps(rows, sort_keys=True, default=str)


def saved_answer(listing: dict, msg: dict, label: str) -> dict | None:
    """The answer a tool's saved listing gives the request ``msg`` (one with
    an ``id``), or None when only the tool's program can answer it: the
    handshake, a ping, a log level, the lists, the probe the engine sends
    first, and a call naming a tool the list does not have. The stand-in
    and a Docker tool's door both answer from it."""
    init = listing.get("initialize") or {}
    caps = _dict(init.get("capabilities"))
    method = msg.get("method")
    rid = msg.get("id")
    if method in _PROBES:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
    if method == "initialize":
        params = _dict(msg.get("params"))
        told = _version_told(params.get("protocolVersion"), str(init.get("protocolVersion") or ""))
        told_caps = dict(caps)
        if "tools" in caps:
            # The engine is told when the program's list differs.
            told_caps["tools"] = {**(caps.get("tools") or {}), "listChanged": True}
        result = {"protocolVersion": told, "capabilities": told_caps,
                  "serverInfo": init.get("serverInfo") or {"name": label, "version": ""}}
        if init.get("instructions"):
            result["instructions"] = init["instructions"]
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    if method in ("ping", "logging/setLevel"):
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method in _LISTS:
        if _LIST_CAPABILITY[method] not in caps:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": rid,
                "result": {_LISTS[method]: (listing.get("lists") or {}).get(method) or []}}
    if method == "tools/call":
        call = _dict(msg.get("params"))
        names = {t.get("name") for t in (listing.get("lists") or {}).get("tools/list") or []
                 if isinstance(t, dict)}
        if call.get("name") not in names:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"Unknown tool: {call.get('name')}"}}
    return None


class _StartFailed(Exception):
    """The real start did not happen; the message is the sentence."""


class StandIn:
    """One tool's stand-in, for the life of its session. The main thread is
    the only reader of the engine's lines (a reader thread queues them), so
    every decision is taken in one place: answered here, held for the start,
    or forwarded to the program once it runs."""

    def __init__(self, wrapped: list[str], declarations: list[dict], session_id: str,
                 marker: dict, *, stdin=None, stdout=None, stderr=None) -> None:
        self.wrapped = wrapped
        self.declarations = declarations
        self.session_id = session_id
        self.label = str(marker.get("label") or "this")
        self.needs_fetch = bool(marker.get("fetch"))
        # A call's tool: its program starts at the engine's ``initialize``,
        # beside the engine, and its session counted it at its own start.
        self.warm = marker.get("warm") is True
        self.token = os.environ.get("OTO_MCP_FETCH_TOKEN", "")
        self.fp = marker_fingerprint(marker, dict(os.environ), wrapped)
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.stdout = stdout if stdout is not None else sys.stdout.buffer
        self.stderr = stderr if stderr is not None else sys.stderr.buffer
        self.events: queue.Queue = queue.Queue()
        self.out_lock = threading.Lock()
        self.in_lock = threading.Lock()
        self.listing: dict | None = None
        self.init_msg: dict | None = None
        self.told = ""
        self.set_level: dict | None = None
        self.proc: subprocess.Popen | None = None
        self.diverting = False
        self.held: list[bytes] = []
        self.swallow_initialized = False
        self.next_private = 0
        self.counted = False
        # The start's fetch waited for the platform (``_apply_broker_credentials``).
        self.waited = False
        # The engine was told this server's tools once (the ``listed`` report).
        self.listed_told = False
        # Each start's program, counted: a killed program's last lines must
        # never be read as the next one's.
        self.generation = 0
        # A start with nothing saved: its program's answers, and until the
        # platform kept them the engine's requests in flight to it and
        # whether one called it (``_maybe_stop``). Main thread only, but
        # ``tracking``, which the stdout pump reads.
        self.own: dict | None = None
        self.tracking = False
        self.stop_wanted = False
        self.inflight: set = set()
        # The program's start whose list changed on its own (a list-changed
        # notice): its answers are no longer the ones kept, so it stays. Set
        # by the stdout pump before the notice reaches the engine.
        self.changed_generation = -1

    # --- the engine's side -------------------------------------------------

    def _read_engine(self) -> None:
        while True:
            line = self.stdin.readline()
            self.events.put(("engine", line))
            if not line:
                return

    def _send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self.out_lock:
            try:
                self.stdout.write(data)
                self.stdout.flush()
            except (BrokenPipeError, OSError):
                pass

    def _result(self, msg: dict, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": msg.get("id"), "result": result})

    def _error(self, msg: dict, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": code, "message": message}})

    def _refuse(self, line: bytes, sentence: str) -> None:
        """A request held for a start that did not happen: a tool call gets
        the sentence as its result, any other request as an error."""
        msg = _parse(line)
        if msg is None or "id" not in msg or "method" not in msg:
            return
        if msg.get("method") == "tools/call":
            self._result(msg, {"content": [{"type": "text", "text": sentence}], "isError": True})
        else:
            self._error(msg, -32603, sentence)

    # --- the answers it gives itself ---------------------------------------

    def _answer(self, msg: dict) -> bool:
        """Answer ``msg`` from the saved listing; False when only the program
        can (the start then follows)."""
        if "id" not in msg:
            return True  # a notification before the start says nothing to a program not running
        answer = saved_answer(self.listing or {}, msg, self.label)
        if answer is None:
            return False
        method = msg.get("method")
        if method == "initialize":
            self.init_msg = msg
            self.told = str(answer["result"]["protocolVersion"])
        elif method == "logging/setLevel":
            self.set_level = _dict(msg.get("params"))
        self._send(answer)
        if method == "tools/list" and not self.listed_told:
            # The engine has the list: a terminal's first message waits
            # for this (``tool_listing.pending_listed``). Fire and forget.
            self.listed_told = True
            threading.Thread(target=_route, args=("POST", "/v1/hooks/tool-start", self.token,
                                                  {"phase": "listed"}),
                             daemon=True, name="standin-listed").start()
        return True

    # --- the program's side ------------------------------------------------

    def _pump_out(self, src, generation: int) -> None:
        """The program's stdout to the engine. While the handshake runs, the
        answers to the stand-in's own requests are kept back; every other line
        (a request the program makes of the engine, a notice) goes on at once."""
        while True:
            line = src.readline()
            if not line:
                self.events.put(("exit", generation))
                return
            if generation != self.generation:
                return  # a killed program's last words
            msg = _parse(line) if self.diverting or self.tracking else None
            if (self.diverting and msg is not None and str(msg.get("id", "")).startswith(_PRIVATE)
                    and "method" not in msg):
                self.events.put(("private", (generation, msg)))
                continue
            if b"/list_changed" in line:
                notice = msg if msg is not None else _parse(line)
                if notice is not None and str(notice.get("method") or "").endswith("/list_changed"):
                    self.changed_generation = generation
            with self.out_lock:
                try:
                    self.stdout.write(line)
                    self.stdout.flush()
                except (BrokenPipeError, OSError):
                    return
            if self.tracking and msg is not None and "method" not in msg and "id" in msg:
                # Written to the engine: a stop waits for no answer in flight.
                self.events.put(("answered", (generation, msg["id"])))

    def _to_program(self, data: bytes) -> bool:
        with self.in_lock:
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError):
                return False

    def _private(self, method: str, params: dict | None, deadline: float) -> dict:
        """One request of the stand-in's own to the program, answered before
        ``deadline``. Meanwhile an answer the engine gives the program goes on
        at once and the engine's own requests wait for the start's end."""
        self.next_private += 1
        rid = f"{_PRIVATE}{self.next_private}"
        req: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        if not self._to_program((json.dumps(req) + "\n").encode("utf-8")):
            raise _StartFailed(self._stopped())
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _StartFailed(f"The {self.label} tool did not start. "
                                   "Its program did not answer within a minute.")
            try:
                kind, item = self.events.get(timeout=left)
            except queue.Empty:
                continue
            if kind == "private" and item[0] == self.generation and item[1].get("id") == rid:
                result = item[1].get("result")
                if not isinstance(result, dict):
                    raise _StartFailed(self._stopped())
                return result
            if kind == "exit" and item == self.generation:
                raise _StartFailed(self._stopped())
            if kind == "engine":
                if not item:
                    raise _StartFailed("")  # the engine left: nothing to answer
                msg = _parse(item)
                if msg is not None and "method" not in msg and "id" in msg:
                    self._to_program(item)  # the engine answers the program
                elif not self._answer_during_start(msg):
                    self.held.append(item)

    def _answer_during_start(self, msg: dict | None) -> bool:
        """During a start with saved answers, what they answer as before the
        start: a probe, ``ping`` and the lists. Everything else waits for the
        program, in order (a cancel follows its call, a log level reaches
        it). The engine's ``initialized`` after a handshake the stand-in
        answered goes nowhere: the program had its own."""
        if msg is None or self.listing is None:
            return False
        method = msg.get("method")
        if "id" not in msg:
            return method == "notifications/initialized"
        if method in _PROBES or method == "ping" or method in _LISTS:
            return self._answer(msg)
        return False

    def _stopped(self) -> str:
        return f"The {self.label} tool did not start. Its program stopped before it answered."

    def _lists(self, caps: dict, deadline: float) -> dict:
        lists: dict[str, list] = {}
        for method, key in _LISTS.items():
            if _LIST_CAPABILITY[method] not in caps:
                continue
            items: list = []
            cursor = None
            for _page in range(100):
                result = self._private(method, {"cursor": cursor} if cursor else {}, deadline)
                items.extend(result.get(key) or [])
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            lists[method] = items
        return lists

    def _start(self) -> None:
        """The real start, on the first request the stand-in cannot answer
        (held in ``self.held``), with nothing saved at the engine's
        ``initialize`` (it waits for the program's answer), and for a warm
        tool right after the stand-in answered that ``initialize``. Raises
        ``_StartFailed`` with the sentence."""
        nothing_saved = self.listing is None
        self.counted = False
        asked = 0
        if not self.warm:
            asked = self._ask_start(nothing_saved)
        child_env = dict(os.environ)
        child_env.pop("OTO_TOOL_ARG_PATHS", None)
        child_env.pop(STANDIN_ENV, None)
        fetched, self.waited = _apply_broker_credentials(child_env, hold=self.needs_fetch)
        if not fetched and self.needs_fetch:
            raise _StartFailed(f"The {self.label} tool cannot start in this chat any more. "
                               "Reload the chat or start a new one.")
        if self.waited and not self.warm and asked != 200:
            # The first ask found the platform away: the one that answers now
            # counts the program (a counted start is never asked twice, since
            # each ask grows the reserve).
            self._ask_start(nothing_saved)
        # The program's minute runs from its spawn: a wait for the platform
        # is not the program's.
        began = time.monotonic()
        try:
            self.proc = subprocess.Popen(self.wrapped, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=child_env, bufsize=0)
        except (OSError, ValueError) as e:
            sys.stderr.write(f"interceptor: failed to spawn {self.wrapped[0]}: {e}\n")
            raise _StartFailed(f"The {self.label} tool could not start on this machine.") from None
        self.diverting = True
        self.generation += 1
        threading.Thread(target=self._pump_out, args=(self.proc.stdout, self.generation), daemon=True,
                         name="standin-stdout").start()
        threading.Thread(target=_pump_outbound, args=(self.proc.stderr, self.stderr, threading.Event()),
                         daemon=True, name="standin-stderr").start()
        deadline = began + _START_TIMEOUT_S
        try:
            params = dict((self.init_msg or {}).get("params") or {})
            if self.told:
                params["protocolVersion"] = self.told
            params.setdefault("protocolVersion", str(((self.listing or {}).get("initialize") or {})
                                                     .get("protocolVersion") or "2025-06-18"))
            params.setdefault("capabilities", {})
            params.setdefault("clientInfo", {"name": "otodock", "version": "1"})
            init = self._private("initialize", params, deadline)
            self._to_program(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            if self.set_level is not None:
                self._private("logging/setLevel", self.set_level, deadline)
            caps = _dict(init.get("capabilities"))
            lists = self._lists(caps, deadline)
        except _StartFailed:
            self._kill()
            raise
        self.diverting = False
        real = {"initialize": init, "lists": lists}
        changed = nothing_saved or _tools_key(lists.get("tools/list") or []) != _tools_key(
            ((self.listing or {}).get("lists") or {}).get("tools/list") or [])
        if nothing_saved:
            if self.init_msg is not None:
                self.told = str(init.get("protocolVersion") or "")
                self._result(self.init_msg, init)
                self.swallow_initialized = True
            if not self.warm:
                # Started only for its answers: it stops once they are
                # kept, unless the engine calls it first. A call's stays.
                self.own = real
                self.tracking, self.stop_wanted = True, False
                self.inflight.clear()
        elif changed:
            self._send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        report = {"phase": "started", "fp": self.fp, "with_session": nothing_saved or self.warm,
                  "ms": int((time.monotonic() - began) * 1000), "listing": real if changed else None}
        threading.Thread(target=self._report, args=(report, self.generation),
                         daemon=True, name="standin-report").start()

    def _ask_start(self, nothing_saved: bool) -> int:
        """Whether the program may start now (``tool-start``): the status the
        platform answered, 0 when it could not be asked. Raises
        ``_StartFailed`` with the platform's sentence on a refusal."""
        status, answer = _route("POST", "/v1/hooks/tool-start", self.token,
                                {"phase": "start", "fp": self.fp, "with_session": nothing_saved})
        if status == 200 and isinstance(answer, dict) and answer.get("ok") is False:
            raise _StartFailed(str(answer.get("message") or ""))
        # The session's reserve grew by this program: a start that fails
        # from here gives it back (``failed``).
        self.counted = status == 200
        return status

    def _report(self, report: dict, generation: int) -> None:
        """The ``started`` report. Its answer says whether the platform kept
        the program's answers, which a program started for them waits for."""
        status, answer = _route("POST", "/v1/hooks/tool-start", self.token, report)
        if status == 200 and isinstance(answer, dict) and answer.get("kept") is True:
            self.events.put(("kept", generation))

    def _maybe_stop(self) -> None:
        """Stop a program started only for its answers once the platform
        kept them, the engine never called it and no request waits on it:
        from here the stand-in answers from them, and the program starts
        again at the first request it cannot answer."""
        if self.changed_generation == self.generation:
            self.tracking = False  # its list changed after the start: it stays
        if not (self.tracking and self.stop_wanted) or self.inflight or self.proc is None:
            return
        proc, self.proc = self.proc, None
        self.tracking = self.stop_wanted = self.diverting = self.swallow_initialized = False
        self.generation += 1  # its last lines never reach the engine
        self.listing = self.own
        self.listed_told = True  # its start's report said the engine has the list
        threading.Thread(target=_stop_gently, args=(proc,), daemon=True, name="standin-stop").start()
        # The platform gives back what its start added (``tool-start`` ``stopped``).
        self.counted = False
        threading.Thread(target=_route, args=("POST", "/v1/hooks/tool-start", self.token,
                                              {"phase": "stopped", "fp": self.fp}),
                         daemon=True, name="standin-report").start()

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        self.diverting = False
        self.generation += 1  # its last lines never reach the engine
        if proc is None:
            return
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    def _forward(self, line: bytes) -> bool:
        """An engine line to the running program, through today's path
        translation. False when the program's stdin is gone."""
        msg = _parse(line)
        if self.swallow_initialized and msg is not None and msg.get("method") == "notifications/initialized":
            self.swallow_initialized = False
            return True
        if msg is not None and msg.get("method") == "logging/setLevel":
            self.set_level = _dict(msg.get("params"))  # a later start sets it again
        if self.tracking and msg is not None and "method" in msg and "id" in msg:
            rid = msg.get("id")
            if msg.get("method") in _ANSWERABLE and isinstance(rid, (str, int)):
                self.inflight.add(rid)
            else:
                self.tracking = False  # called: the program stays for the session
        try:
            to_mcp, to_cli_err = _process_inbound_line(line, self.declarations, self.session_id)
        except Exception as e:  # noqa: BLE001  # the pipe must keep flowing: a bad line passes through unchanged
            _log(f"inbound exception, passing through: {e}")
            to_mcp, to_cli_err = line, b""
        if to_cli_err:
            with self.out_lock:
                with contextlib.suppress(BrokenPipeError, OSError):
                    self.stdout.write(to_cli_err)
                    self.stdout.flush()
            return True
        return self._to_program(to_mcp) if to_mcp else True

    def run(self) -> int:
        status, answer = _route("GET", f"/v1/hooks/tool-listing?fp={self.fp}", self.token)
        if status == 200 and isinstance(answer, dict) and isinstance(answer.get("listing"), dict):
            self.listing = answer["listing"]
        threading.Thread(target=self._read_engine, daemon=True, name="standin-stdin").start()
        while True:
            kind, item = self.events.get()
            if self.proc is not None:
                if kind == "exit" and item == self.generation:
                    return self._finish()
                if kind == "engine":
                    if not item:
                        return self._finish(close=True)
                    if not self._forward(item):
                        return self._finish()
                elif kind == "answered" and item[0] == self.generation:
                    self.inflight.discard(item[1])
                    self._maybe_stop()
                elif kind == "kept" and item == self.generation and self.tracking:
                    self.stop_wanted = True
                    self._maybe_stop()
                continue
            if kind != "engine":
                continue
            if not item:
                return 0  # the engine left before any call: nothing ever ran
            msg = _parse(item)
            if msg is None or "method" not in msg:
                continue  # an answer meant for a program that no longer runs
            if self.listing is None:
                if msg.get("method") in _PROBES and "id" in msg:
                    self._error(msg, -32601, "Method not found")
                    continue
                if msg.get("method") != "initialize":
                    if "id" in msg:
                        self.held.append(item)
                    continue
                self.init_msg = msg
            elif self._answer(msg):
                if not (self.warm and msg.get("method") == "initialize" and "id" in msg
                        and self.generation == 0):
                    continue
                # A call's tool: the engine has its answer, and the program
                # starts beside it now, before the first call needs it.
            else:
                self.held.append(item)
            if not self._try_start():
                return 0

    def _try_start(self) -> bool:
        """Start, then hand the held lines to the program; on a failure answer
        them with the sentence and stay. False when the engine has left."""
        try:
            self._start()
        except _StartFailed as e:
            held, self.held = self.held, []
            sentence = str(e)
            if not sentence:
                self._give_back(wait=True)  # the engine left: this process ends next
                return False
            _log(f"start refused: {sentence}")
            self._give_back(wait=False)
            for line in held:
                self._refuse(line, sentence)
            if self.listing is None and self.init_msg is not None:
                # Nothing saved, nothing started: the engine's handshake fails
                # as a program's would.
                self._error(self.init_msg, -32603, sentence)
                self.init_msg = None
            return True
        held, self.held = self.held, []
        for line in held:
            if self.waited and _is_call(line):
                self._refuse(line, _RESTARTED)
                continue
            if not self._forward(line):
                break
        self.waited = False
        return True

    def _give_back(self, *, wait: bool) -> None:
        """A start the platform counted that did not happen: its program
        given back (``tool-start`` ``failed``), once per start. ``wait``
        when this process ends next, so the report leaves before it does
        (the route's own timeout bounds the wait)."""
        if not self.counted:
            return
        self.counted = False
        args = ("POST", "/v1/hooks/tool-start", self.token, {"phase": "failed", "fp": self.fp})
        if wait:
            _route(*args)
        else:
            threading.Thread(target=_route, args=args, daemon=True, name="standin-report").start()

    def _finish(self, close: bool = False) -> int:
        proc = self.proc
        if proc is None:
            return 0
        if close:
            with self.in_lock, contextlib.suppress(BrokenPipeError, OSError):
                assert proc.stdin is not None
                proc.stdin.close()
        return proc.wait()


def _stop_gently(proc: Any) -> None:
    """A program's end (its ``Popen``) when nothing waits on it: its input
    closed (a stdio server ends there, and a wrapper's child reads the same
    pipe), a short wait, then a kill."""
    with contextlib.suppress(OSError, ValueError):
        if proc.stdin is not None:
            proc.stdin.close()
    try:
        proc.wait(timeout=_STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _is_call(line: bytes) -> bool:
    msg = _parse(line)
    return msg is not None and msg.get("method") == "tools/call" and "id" in msg


def _parse(line: bytes) -> dict | None:
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


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

    marker = os.environ.get(STANDIN_ENV, "")
    if marker:
        try:
            parsed_marker = json.loads(marker)
        except ValueError:
            parsed_marker = None
        if isinstance(parsed_marker, dict):
            return StandIn(wrapped, declarations, session_id, parsed_marker).run()
        _log("ignoring malformed OTO_TOOL_STANDIN")

    # Build the child env. Strip the interceptor-only path-decl var (our
    # contract, not the MCP's), then apply the credential broker: fetch this
    # MCP's secrets at spawn + strip every broker-only var before exec.
    child_env = dict(os.environ)
    child_env.pop("OTO_TOOL_ARG_PATHS", None)
    child_env.pop(STANDIN_ENV, None)
    _apply_broker_credentials(child_env)

    _log(f"spawning {wrapped!r} (decls={len(declarations)})")
    try:
        proc = subprocess.Popen(
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
    _URLLIB["opener_owed"] = True
    sys.exit(main())
