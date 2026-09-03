"""Stdio interceptor wrapping for MCP configs (JSON + Codex TOML).

The proxy marks stdio MCP servers that need the path-translation / credential-broker
interceptor (``OTO_TOOL_ARG_PATHS`` / ``OTO_MCP_FETCH_TOKEN`` in their env). These
helpers rewrite a server's ``command``/``args`` to spawn
``<python> stdio_path_interceptor.py -- <real cmd> <args...>`` instead, for both the
Claude CLI's JSON ``mcp-config.json`` and Codex's ``config.toml``. Consumed by
cli_session / pty_session (JSON) and codex_session / codex_pty_session (TOML); the
SessionManager itself never calls these.
"""
import json
import re
import sys
from pathlib import Path


_INTERCEPTOR_SCRIPT = str(
    (Path(__file__).resolve().parent.parent / "_vendored" / "stdio_path_interceptor.py")
).replace("\\", "/")


def wrap_interceptor_in_mcp_config(mcp_config: dict) -> None:
    """Wrap stdio servers that need the interceptor — either ``tool_arg_paths``
    declarations (path translation) OR a credential-broker token
    (``OTO_MCP_FETCH_TOKEN``, fetch-at-spawn). Mutates ``mcp_config`` in
    place.

    The proxy sets ``OTO_TOOL_ARG_PATHS`` (JSON of declarations) on
    each stdio server whose manifest opts in. This helper rewrites
    those entries so the CLI spawns ``<python> <interceptor> -- <cmd>
    <args...>`` instead of the raw MCP command. The interceptor:

      * Parses JSON-RPC tools/call lines from the CLI on stdin.
      * Calls the proxy's batched ``/v1/hooks/resolve-tool-arg-paths``
        to translate paths declared via ``tool_arg_paths``.
      * Forwards rewritten args to the real MCP; passes stdout / stderr
        through untouched.

    Servers with neither ``OTO_TOOL_ARG_PATHS`` nor ``OTO_MCP_FETCH_TOKEN``
    are left alone — zero overhead for MCPs that need neither. SSE / HTTP /
    streamable-http MCPs are also left alone (the interceptor only
    bridges stdio).
    """
    servers = mcp_config.get("mcpServers") if isinstance(mcp_config, dict) else None
    if not isinstance(servers, dict):
        return
    # Forward-slash form on Windows. The cli_session writer expands
    # `~/...` prefixes but does NOT touch other backslash paths; if we
    # emit a backslash-shaped sys.executable, json.dumps escapes it to
    # `\\` and Claude CLI's MCP config parser rejects the file (see the
    # comment at cli_session.py around the mcp-config.json write).
    interpreter = (sys.executable or "python3").replace("\\", "/")
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        env = server.get("env") or {}
        if not (env.get("OTO_TOOL_ARG_PATHS") or env.get("OTO_MCP_FETCH_TOKEN")):
            continue
        if "command" not in server:
            # SSE / HTTP MCPs — no command to wrap; these markers would be a
            # no-op (no stdio child). Strip them to avoid confusion downstream.
            env.pop("OTO_TOOL_ARG_PATHS", None)
            env.pop("OTO_MCP_FETCH_TOKEN", None)
            server["env"] = env
            continue
        original_cmd = server["command"]
        original_args = list(server.get("args") or [])
        server["command"] = interpreter
        server["args"] = [
            _INTERCEPTOR_SCRIPT, "--", original_cmd, *original_args,
        ]


_TOML_SECTION_RE = re.compile(r'^\[mcp_servers\.([^\]]+)\]\s*$')
_TOML_ANY_SECTION_RE = re.compile(r'^\[[^\]]+\]\s*$')
_TOML_COMMAND_RE = re.compile(r'^(\s*)command\s*=\s*"(.*)"\s*$')
_TOML_ARGS_RE = re.compile(r'^(\s*)args\s*=\s*(\[[^\]]*\])\s*$')
# Loose detector: tells "no args line" (wrap empty) apart from a malformed
# args line (leave the section alone).
_TOML_ARGS_LOOSE_RE = re.compile(r'^\s*args\s*=')


def _toml_escape(value: str) -> str:
    """Minimal TOML basic-string escape mirroring
    ``proxy/services/mcp/mcp_registry.py::_toml_escape``.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _maybe_wrap_codex_toml_section(
    section_lines: list[str], interpreter: str,
) -> list[str]:
    """Wrap a single [mcp_servers.<slug>] section's command/args with the
    interceptor IF its env carries ``OTO_TOOL_ARG_PATHS`` or
    ``OTO_MCP_FETCH_TOKEN`` (credential broker).
    """
    full = "\n".join(section_lines)
    if "OTO_TOOL_ARG_PATHS" not in full and "OTO_MCP_FETCH_TOKEN" not in full:
        return section_lines

    cmd_idx = -1
    args_idx = -1
    orig_cmd = ""
    orig_args_repr = ""
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
        # Non-stdio (SSE/streamable-http, no command) — nothing to wrap.
        return section_lines

    if args_idx >= 0:
        try:
            # TOML array of basic strings ≈ JSON array of strings for
            # typical command args (no special TOML-only escapes in
            # practice). json.loads is the safe roundtrip.
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

    new_args = [_INTERCEPTOR_SCRIPT, "--", orig_cmd, *args_list]
    new_args_repr = json.dumps(new_args)  # JSON ≈ TOML for these strings

    out = list(section_lines)
    out[cmd_idx] = f'command = "{_toml_escape(interpreter)}"'
    new_args_line = f'args = {new_args_repr}'
    if args_idx >= 0:
        out[args_idx] = new_args_line
    else:
        out.insert(cmd_idx + 1, new_args_line)  # add the args line after command
    return out


def wrap_interceptor_in_mcp_config_toml(toml_text: str) -> str:
    """TOML equivalent of ``wrap_interceptor_in_mcp_config`` (JSON).

    Walks the Codex config.toml section-by-section. For each
    ``[mcp_servers.<slug>]`` section that has ``OTO_TOOL_ARG_PATHS`` (path
    translation) or ``OTO_MCP_FETCH_TOKEN`` (credential broker) in
    its env block, rewrites ``command``/``args`` to invoke the stdio
    interceptor with the original command as the wrapped child. Non-stdio
    servers (SSE / streamable-http) and stdio servers with neither marker pass
    through unchanged.

    The TOML format is the deterministic shape emitted by
    ``proxy/services/mcp/mcp_registry.py::_servers_to_toml`` — basic-string
    paths in ``command``, inline-array ``args``, inline-table ``env``.
    We don't depend on a TOML parser library; line-based scanning is
    enough for this exact format.
    """
    if not toml_text or (
        "OTO_TOOL_ARG_PATHS" not in toml_text
        and "OTO_MCP_FETCH_TOKEN" not in toml_text
    ):
        # Fast path — nothing to wrap.
        return toml_text

    interpreter = sys.executable or "python3"
    out_lines: list[str] = []
    current_section: list[str] = []
    in_mcp_section = False

    def _flush() -> None:
        nonlocal current_section
        if current_section:
            out_lines.extend(
                _maybe_wrap_codex_toml_section(current_section, interpreter)
            )
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
    # Preserve trailing newline if original had one.
    suffix = "\n" if toml_text.endswith("\n") else ""
    return "\n".join(out_lines) + suffix
