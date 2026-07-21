"""Shared stdio-interceptor wrap module.

`core/sandbox/interceptor_wrap.py` rewrites stdio MCPs carrying OTO_MCP_FETCH_TOKEN or
OTO_TOOL_ARG_PATHS to run via `<interpreter> <interceptor_path> -- <cmd> <args>`.
Pure transforms — no broker / I/O. Mirrors the satellite wrap, parameterized by
interpreter + path so the proxy's local bwrap paths can reuse it.
"""

import sys

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.sandbox.interceptor_wrap import wrap_servers_json, wrap_toml_text  # noqa: E402


# --- JSON (Claude) -----------------------------------------------------------

def test_wraps_fetch_token_server():
    cfg = {"mcpServers": {"maps": {
        "command": "node", "args": ["s.js", "--port", "1"],
        "env": {"OTO_MCP_FETCH_TOKEN": "t"},
    }}}
    wrap_servers_json(cfg, interpreter="python3", interceptor_path="/x/i.py")
    srv = cfg["mcpServers"]["maps"]
    assert srv["command"] == "python3"
    assert srv["args"] == ["/x/i.py", "--", "node", "s.js", "--port", "1"]
    # The token is left in env — the interceptor strips it at runtime.
    assert srv["env"]["OTO_MCP_FETCH_TOKEN"] == "t"


def test_wraps_tool_arg_paths_server():
    cfg = {"mcpServers": {"fs": {
        "command": "python3", "args": ["fs.py"],
        "env": {"OTO_TOOL_ARG_PATHS": "[]"},
    }}}
    wrap_servers_json(cfg, interpreter="PY", interceptor_path="/i.py")
    srv = cfg["mcpServers"]["fs"]
    assert srv["command"] == "PY"
    assert srv["args"] == ["/i.py", "--", "python3", "fs.py"]


def test_leaves_unmarked_server_untouched():
    cfg = {"mcpServers": {"plain": {
        "command": "python3", "args": ["p.py"], "env": {"OTO_USERNAME": "a"},
    }}}
    wrap_servers_json(cfg, interpreter="python3", interceptor_path="/i.py")
    srv = cfg["mcpServers"]["plain"]
    assert srv["command"] == "python3" and srv["args"] == ["p.py"]


def test_strips_markers_on_http_server():
    cfg = {"mcpServers": {"http": {
        "url": "http://x", "env": {"OTO_MCP_FETCH_TOKEN": "t"},
    }}}
    wrap_servers_json(cfg, interpreter="python3", interceptor_path="/i.py")
    srv = cfg["mcpServers"]["http"]
    assert "command" not in srv
    assert "OTO_MCP_FETCH_TOKEN" not in srv["env"]


def test_json_no_servers_is_noop():
    cfg = {"other": 1}
    wrap_servers_json(cfg, interpreter="python3", interceptor_path="/i.py")
    assert cfg == {"other": 1}


# --- TOML (Codex) ------------------------------------------------------------

def test_toml_wraps_marked_section():
    toml = (
        '[mcp_servers.maps]\n'
        'command = "node"\n'
        'args = ["s.js"]\n'
        'env = { "OTO_MCP_FETCH_TOKEN" = "t" }\n'
    )
    out = wrap_toml_text(toml, interpreter="python3", interceptor_path="/x/i.py")
    assert 'command = "python3"' in out
    assert 'args = ["/x/i.py", "--", "node", "s.js"]' in out


def test_toml_fast_path_no_marker_returns_input():
    toml = (
        '[mcp_servers.maps]\ncommand = "node"\nargs = ["s.js"]\n'
        'env = { "OTO_USERNAME" = "a" }\n'
    )
    assert wrap_toml_text(toml, interpreter="python3", interceptor_path="/i.py") == toml


def test_toml_leaves_non_stdio_section():
    toml = (
        '[mcp_servers.web]\nurl = "http://x"\n'
        'env = { "OTO_MCP_FETCH_TOKEN" = "t" }\n'
    )
    out = wrap_toml_text(toml, interpreter="python3", interceptor_path="/i.py")
    assert 'url = "http://x"' in out
    assert 'command =' not in out


def test_toml_only_marked_section_wrapped():
    toml = (
        '[mcp_servers.maps]\ncommand = "node"\nargs = ["s.js"]\n'
        'env = { "OTO_MCP_FETCH_TOKEN" = "t" }\n'
        '\n'
        '[mcp_servers.plain]\ncommand = "node"\nargs = ["p.js"]\n'
        'env = { "OTO_USERNAME" = "a" }\n'
    )
    out = wrap_toml_text(toml, interpreter="PY", interceptor_path="/i.py")
    plain_idx = out.index("[mcp_servers.plain]")
    # plain section keeps its original command (not wrapped)
    assert 'command = "node"' in out[plain_idx:]
    # maps section is wrapped
    assert 'args = ["/i.py", "--", "node", "s.js"]' in out[:plain_idx]


def test_toml_wraps_command_only_section_no_args_line():
    """An MCP that takes no args has no `args` line in the TOML (e.g.
    google-maps-mcp-server). It must STILL be wrapped — an `args` line is
    inserted. Regression for the broker-never-ran-on-codex bug."""
    toml = (
        '[mcp_servers.maps]\n'
        'command = "/path/google-maps-mcp-server"\n'
        'env = { "OTO_MCP_FETCH_TOKEN" = "t" }\n'
    )
    out = wrap_toml_text(toml, interpreter="python3", interceptor_path="/x/i.py")
    assert 'command = "python3"' in out
    assert 'args = ["/x/i.py", "--", "/path/google-maps-mcp-server"]' in out
    # the original command is preserved as the wrapped child, env untouched
    assert "/path/google-maps-mcp-server" in out and "OTO_MCP_FETCH_TOKEN" in out


def test_toml_preserves_trailing_newline():
    base = (
        '[mcp_servers.m]\ncommand = "n"\nargs = ["a"]\n'
        'env = { "OTO_MCP_FETCH_TOKEN" = "t" }'
    )
    assert wrap_toml_text(base + "\n", interpreter="P", interceptor_path="/i.py").endswith("\n")
    assert not wrap_toml_text(base, interpreter="P", interceptor_path="/i.py").endswith("\n")
