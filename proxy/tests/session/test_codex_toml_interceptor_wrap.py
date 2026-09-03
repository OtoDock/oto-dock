"""Tests for Gap 2 — Codex TOML interceptor wrap on the satellite.

Mirrors `test_stdio_path_interceptor.py::TestWrapInterceptor` but for
the TOML format Codex CLI consumes.
"""

import sys

import pytest

from tests._paths import REPO_ROOT as _REPO_ROOT
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from satellite.sessions.mcp_interceptor import (  # noqa: E402
    wrap_interceptor_in_mcp_config_toml,
)


class TestWrapsStdioWithDeclarations:
    def test_basic_stdio_with_oto_tool_arg_paths(self):
        toml = '\n'.join([
            '[mcp_servers.display]',
            'type = "stdio"',
            'command = "venv/bin/python"',
            'args = ["display_server.py"]',
            'env = { "PROXY_URL" = "http://127.0.0.1:8473", '
            '"OTO_TOOL_ARG_PATHS" = "[{\\"tool\\":\\"X\\"}]" }',
            '',
        ])
        out = wrap_interceptor_in_mcp_config_toml(toml)
        # Command should now be Python.
        assert 'command = "' in out
        # Args contain the interceptor + original cmd + original args.
        assert 'stdio_path_interceptor.py' in out
        assert '"--"' in out
        assert '"venv/bin/python"' in out
        assert '"display_server.py"' in out
        # Env preserved including OTO_TOOL_ARG_PATHS.
        assert "OTO_TOOL_ARG_PATHS" in out

    def test_multiple_args_preserved_in_order(self):
        toml = '\n'.join([
            '[mcp_servers.foo]',
            'type = "stdio"',
            'command = "node"',
            'args = ["--max-old-space-size=8192", "server.js", "--port", "9000"]',
            'env = { "OTO_TOOL_ARG_PATHS" = "[]" }',
            '',
        ])
        out = wrap_interceptor_in_mcp_config_toml(toml)
        # Check the new args list contains everything in order.
        import json
        for line in out.splitlines():
            if line.startswith("args = "):
                parsed = json.loads(line[len("args = "):])
                # interceptor, --, node, then the 4 original args
                assert parsed[1] == "--"
                assert parsed[2] == "node"
                assert parsed[3:] == [
                    "--max-old-space-size=8192", "server.js",
                    "--port", "9000",
                ]
                break
        else:
            pytest.fail("args line not found in output")

class TestPassesThroughUnchanged:
    def test_stdio_without_declarations_left_alone(self):
        toml = '\n'.join([
            '[mcp_servers.other]',
            'type = "stdio"',
            'command = "python"',
            'args = ["server.py"]',
            'env = { "FOO" = "bar" }',
            '',
        ])
        out = wrap_interceptor_in_mcp_config_toml(toml)
        assert out == toml
        assert 'stdio_path_interceptor.py' not in out

    def test_sse_server_left_alone(self):
        toml = '\n'.join([
            '[mcp_servers.linear-mcp]',
            'type = "streamable-http"',
            'url = "https://mcp.linear.app/mcp"',
            '',
            '[mcp_servers.linear-mcp.http_headers]',
            '"Authorization" = "Bearer xoxb-abc"',
            '',
        ])
        out = wrap_interceptor_in_mcp_config_toml(toml)
        assert out == toml

    def test_empty_input_returns_unchanged(self):
        assert wrap_interceptor_in_mcp_config_toml("") == ""

    def test_no_mcp_servers_section(self):
        toml = 'project_doc_max_bytes = 100000\n'
        assert wrap_interceptor_in_mcp_config_toml(toml) == toml


class TestMixedSections:
    def test_only_declared_servers_wrapped(self):
        toml = '\n'.join([
            '# Header comment',
            'project_doc_max_bytes = 100000',
            '',
            '[mcp_servers.display]',
            'type = "stdio"',
            'command = "venv/bin/python"',
            'args = ["display_server.py"]',
            'env = { "OTO_TOOL_ARG_PATHS" = "[]" }',
            '',
            '[mcp_servers.plain-mcp]',
            'type = "stdio"',
            'command = "python"',
            'args = ["plain.py"]',
            'env = { "FOO" = "bar" }',
            '',
            '[mcp_servers.linear-mcp]',
            'type = "streamable-http"',
            'url = "https://mcp.linear.app/mcp"',
            '',
        ])
        out = wrap_interceptor_in_mcp_config_toml(toml)
        # `display` section wrapped.
        assert 'stdio_path_interceptor.py' in out
        # `plain-mcp` section NOT wrapped.
        plain_section_start = out.index('[mcp_servers.plain-mcp]')
        # Anything between plain-mcp section and the next section header should
        # NOT contain the interceptor path.
        post_plain = out[plain_section_start:out.index('[mcp_servers.linear-mcp]')]
        assert 'stdio_path_interceptor.py' not in post_plain
        # linear-mcp untouched.
        assert 'streamable-http' in out

    def test_top_level_keys_preserved(self):
        toml = '\n'.join([
            'project_doc_max_bytes = 100000',
            '',
            '[mcp_servers.display]',
            'type = "stdio"',
            'command = "python"',
            'args = ["server.py"]',
            'env = { "OTO_TOOL_ARG_PATHS" = "[]" }',
        ])
        out = wrap_interceptor_in_mcp_config_toml(toml)
        assert "project_doc_max_bytes = 100000" in out


class TestEdgeCases:
    def test_non_stdio_with_declarations_left_alone(self):
        # An MCP that declares OTO_TOOL_ARG_PATHS in env but uses SSE
        # (unusual but legal) shouldn't crash — there's nothing to wrap.
        toml = '\n'.join([
            '[mcp_servers.weird]',
            'type = "sse"',
            'url = "http://localhost:9000/sse"',
            'env = { "OTO_TOOL_ARG_PATHS" = "[]" }',
            '',
        ])
        out = wrap_interceptor_in_mcp_config_toml(toml)
        # Pass-through expected (no command/args to rewrite).
        assert out == toml

    def test_malformed_args_list_left_alone(self):
        toml = '\n'.join([
            '[mcp_servers.broken]',
            'type = "stdio"',
            'command = "python"',
            'args = ["unclosed',  # broken
            'env = { "OTO_TOOL_ARG_PATHS" = "[]" }',
            '',
        ])
        # Should not crash; passes the section through.
        out = wrap_interceptor_in_mcp_config_toml(toml)
        assert 'stdio_path_interceptor.py' not in out

    def test_trailing_newline_preserved(self):
        toml_with_nl = '[mcp_servers.a]\ntype = "stdio"\ncommand = "x"\nargs = []\nenv = {}\n'
        out = wrap_interceptor_in_mcp_config_toml(toml_with_nl)
        assert out.endswith("\n")

    def test_no_trailing_newline_preserved(self):
        toml_no_nl = '[mcp_servers.a]\ntype = "stdio"\ncommand = "x"\nargs = []\nenv = {}'
        out = wrap_interceptor_in_mcp_config_toml(toml_no_nl)
        assert not out.endswith("\n")
