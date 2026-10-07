"""Shared stdio-interceptor wrapping for MCP configs (credential broker
+ tool-arg-path translation).

Rewrites each stdio MCP whose env carries ``OTO_MCP_FETCH_TOKEN`` (credential
broker) or ``OTO_TOOL_ARG_PATHS`` (path translation) so the CLI spawns
``<interpreter> <interceptor_path> -- <cmd> <args...>`` instead of the raw MCP
command. The interceptor then fetches that MCP's secrets at spawn (and/or
translates declared tool-arg paths), passing stdio through untouched.

Parameterized by ``interpreter`` + ``interceptor_path`` because the two callers
launch the interceptor differently:

* **Local proxy (bwrap sandbox)** — ``interpreter="python3"`` (resolved on the
  sandbox's restricted PATH) with ``interpreter_args=("-I",)`` (isolated: no
  ``PYTHON*`` variable, no user site, no script directory on the path) + the
  sandbox-internal copy of the script (``/users/<u>/.claude/
  stdio_path_interceptor.py`` etc.). The interceptor is stdlib-only, so the
  system ``python3`` runs it.
* **Satellite (native, no bwrap)** — keeps its own copy of this logic in
  ``satellite/session_manager.py`` for now (``sys.executable`` + the vendored
  script path); a later slice can collapse it onto this module.

Pure text/dict transforms — no I/O, no ``mcp_broker`` dependency. The caller
injects the per-(session, mcp) ``OTO_MCP_FETCH_TOKEN`` first; this module only
rewrites the command of servers that already carry a marker.
"""

from __future__ import annotations

import json
import re

# Markers whose presence on a stdio server means "wrap it".
_OTO_TOOL_ARG_PATHS = "OTO_TOOL_ARG_PATHS"
_OTO_MCP_FETCH_TOKEN = "OTO_MCP_FETCH_TOKEN"


def wrap_servers_json(
    mcp_config: dict, *, interpreter: str, interceptor_path: str,
    interpreter_args: tuple[str, ...] = (),
) -> None:
    """Claude JSON config: rewrite stdio servers carrying a marker to run via
    the interceptor. Mutates ``mcp_config`` in place.

    SSE / HTTP / streamable-http servers (no ``command``) are left alone — the
    markers would be a no-op there, so they're stripped to avoid confusion.
    """
    servers = mcp_config.get("mcpServers") if isinstance(mcp_config, dict) else None
    if not isinstance(servers, dict):
        return
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        env = server.get("env") or {}
        if not (env.get(_OTO_TOOL_ARG_PATHS) or env.get(_OTO_MCP_FETCH_TOKEN)):
            continue
        if "command" not in server:
            env.pop(_OTO_TOOL_ARG_PATHS, None)
            env.pop(_OTO_MCP_FETCH_TOKEN, None)
            server["env"] = env
            continue
        original_cmd = server["command"]
        original_args = list(server.get("args") or [])
        server["command"] = interpreter
        server["args"] = [*interpreter_args, interceptor_path, "--", original_cmd, *original_args]


# --- Codex TOML twin ---------------------------------------------------------
# The TOML is the deterministic shape emitted by
# ``services/mcp/mcp_registry.py::_servers_to_toml`` — basic-string ``command``,
# inline-array ``args``, inline-table ``env`` — so line-based scanning is enough
# (no TOML parser dependency).

_TOML_SECTION_RE = re.compile(r'^\[mcp_servers\.([^\]]+)\]\s*$')
_TOML_ANY_SECTION_RE = re.compile(r'^\[[^\]]+\]\s*$')
_TOML_COMMAND_RE = re.compile(r'^(\s*)command\s*=\s*"(.*)"\s*$')
_TOML_ARGS_RE = re.compile(r'^(\s*)args\s*=\s*(\[[^\]]*\])\s*$')
# Loose detector: an args line exists but may not match the strict form above
# (malformed). Used to tell "no args line" (wrap with empty) apart from
# "unparseable args line" (leave the section alone).
_TOML_ARGS_LOOSE_RE = re.compile(r'^\s*args\s*=')


def _toml_escape(value: str) -> str:
    """Minimal TOML basic-string escape (mirrors ``mcp_registry._toml_escape``)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _maybe_wrap_toml_section(
    section_lines: list[str], *, interpreter: str, interceptor_path: str,
    interpreter_args: tuple[str, ...] = (),
) -> list[str]:
    """Wrap one ``[mcp_servers.<slug>]`` section's command/args IF its body
    carries a marker. A stdio server is identified by its ``command`` line; the
    ``args`` line is OPTIONAL — an MCP that takes no args has none (e.g.
    google-maps), and must still be wrapped. Non-stdio sections (``url``, no
    ``command``) pass through unchanged."""
    full = "\n".join(section_lines)
    if _OTO_TOOL_ARG_PATHS not in full and _OTO_MCP_FETCH_TOKEN not in full:
        return section_lines

    cmd_idx = args_idx = -1
    orig_cmd = orig_args_repr = ""
    for i, ln in enumerate(section_lines):
        m_cmd = _TOML_COMMAND_RE.match(ln)
        if m_cmd:
            cmd_idx = i
            orig_cmd = m_cmd.group(2)
            continue
        m_args = _TOML_ARGS_RE.match(ln)
        if m_args:
            args_idx = i
            orig_args_repr = m_args.group(2)
    if cmd_idx < 0:
        return section_lines  # SSE / streamable-http (no command) — nothing to wrap.

    if args_idx >= 0:
        try:
            # JSON array of strings ≈ TOML inline array for these command args.
            args_list = json.loads(orig_args_repr)
            if not isinstance(args_list, list) or not all(
                isinstance(a, str) for a in args_list
            ):
                return section_lines
        except (json.JSONDecodeError, ValueError):
            return section_lines
    elif any(_TOML_ARGS_LOOSE_RE.match(ln) for ln in section_lines):
        return section_lines  # an args line is present but malformed — leave it.
    else:
        args_list = []  # stdio MCP with no args line — wrap with empty args.

    new_args = [*interpreter_args, interceptor_path, "--", orig_cmd, *args_list]
    out = list(section_lines)
    out[cmd_idx] = f'command = "{_toml_escape(interpreter)}"'
    new_args_line = f'args = {json.dumps(new_args)}'
    if args_idx >= 0:
        out[args_idx] = new_args_line
    else:
        out.insert(cmd_idx + 1, new_args_line)  # add the args line after command
    return out


def wrap_toml_text(
    toml_text: str, *, interpreter: str, interceptor_path: str,
    interpreter_args: tuple[str, ...] = (),
) -> str:
    """Codex TOML twin of :func:`wrap_servers_json`. Walks the config
    section-by-section; wraps each ``[mcp_servers.<slug>]`` carrying a marker."""
    if not toml_text or (
        _OTO_TOOL_ARG_PATHS not in toml_text and _OTO_MCP_FETCH_TOKEN not in toml_text
    ):
        return toml_text  # fast path — nothing to wrap.

    out_lines: list[str] = []
    current_section: list[str] = []
    in_mcp_section = False

    def _flush() -> None:
        nonlocal current_section
        if current_section:
            out_lines.extend(_maybe_wrap_toml_section(
                current_section,
                interpreter=interpreter, interceptor_path=interceptor_path,
                interpreter_args=interpreter_args,
            ))
            current_section = []

    for line in toml_text.splitlines():
        if _TOML_SECTION_RE.match(line):
            _flush()
            in_mcp_section = True
            current_section.append(line)
            continue
        if _TOML_ANY_SECTION_RE.match(line):
            _flush()
            in_mcp_section = False
            out_lines.append(line)
            continue
        if in_mcp_section:
            current_section.append(line)
        else:
            out_lines.append(line)
    _flush()

    suffix = "\n" if toml_text.endswith("\n") else ""
    return "\n".join(out_lines) + suffix
