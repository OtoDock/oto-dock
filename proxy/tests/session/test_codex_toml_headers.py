"""Codex TOML bearer-header serialization — CRITICAL fix.

Without this, bearer-injecting remote MCPs (github-mcp/Slack/Linear/Notion/
Zoom) would silently 401 on the Codex execution layer because TOML wouldn't
carry the Authorization header. Codex's MCP-server config field for custom
headers is ``http_headers`` (an inline table) — an unknown ``headers``
sub-table is silently ignored (verified on codex-cli 0.120.0).
"""

from __future__ import annotations

from services.mcp.mcp_registry import _servers_to_toml


def test_bearer_mcp_emits_http_headers_table():
    """When a remote MCP entry has `headers`, the TOML output includes
    a `[mcp_servers.<name>.http_headers]` block with each header K-V."""
    servers = {
        "linear-mcp": {
            "type": "streamable-http",
            "url": "https://mcp.linear.app/mcp",
            "headers": {"Authorization": "Bearer xoxb-abc123"},
        },
    }
    toml = _servers_to_toml(servers)
    assert "[mcp_servers.linear-mcp]" in toml
    assert 'url = "https://mcp.linear.app/mcp"' in toml
    assert "[mcp_servers.linear-mcp.http_headers]" in toml
    # The wrong (ignored-by-Codex) bare `headers` table must NOT be emitted.
    assert "[mcp_servers.linear-mcp.headers]" not in toml
    assert '"Authorization" = "Bearer xoxb-abc123"' in toml


def test_stdio_mcp_does_not_emit_headers_block():
    """Stdio MCPs never have headers — no http_headers block must appear."""
    servers = {
        "file-tools-mcp": {
            "type": "stdio",
            "command": "venv/bin/file-tools-mcp",
            "args": ["--port", "8932"],
            "env": {"FOO": "bar"},
        },
    }
    toml = _servers_to_toml(servers)
    assert "[mcp_servers.file-tools-mcp]" in toml
    assert "command =" in toml
    assert "headers" not in toml.lower()


def test_multi_mcp_headers_are_isolated():
    """Each MCP's headers go under its own section — no leakage."""
    servers = {
        "slack-mcp": {
            "type": "streamable-http",
            "url": "https://mcp.slack.com/mcp",
            "headers": {"Authorization": "Bearer xoxb-slack"},
        },
        "linear-mcp": {
            "type": "sse",
            "url": "https://mcp.linear.app/mcp",
            "headers": {"Authorization": "Bearer lnr-secret"},
        },
        "file-tools-mcp": {
            "type": "stdio",
            "command": "venv/bin/file-tools-mcp",
        },
    }
    toml = _servers_to_toml(servers)
    assert "[mcp_servers.slack-mcp.http_headers]" in toml
    assert "[mcp_servers.linear-mcp.http_headers]" in toml
    assert "[mcp_servers.file-tools-mcp.http_headers]" not in toml
    # Bearer values are not cross-leaked.
    assert '"Authorization" = "Bearer xoxb-slack"' in toml
    assert '"Authorization" = "Bearer lnr-secret"' in toml


def test_remote_mcp_without_headers_skips_block():
    """Bearer is optional — remote MCPs may not need it (no allowlist
    entry, no token bound). No headers table should appear when the
    headers dict is missing or empty."""
    servers = {
        "anon-remote": {
            "type": "streamable-http",
            "url": "https://example.com/mcp",
            # no headers key
        },
        "empty-headers": {
            "type": "sse",
            "url": "https://example.com/sse",
            "headers": {},
        },
    }
    toml = _servers_to_toml(servers)
    assert "[mcp_servers.anon-remote.http_headers]" not in toml
    assert "[mcp_servers.empty-headers.http_headers]" not in toml


# ---------------------------------------------------------------------------
# _write_config_toml — generated per-session config.toml keys
# ---------------------------------------------------------------------------

import tomllib

from core.layers.codex.layer import CodexCLIExecutionLayer as _Layer


def _written_config(tmp_path, **kwargs) -> dict:
    _Layer._write_config_toml(tmp_path, "prompt", **kwargs)
    return tomllib.loads((tmp_path / "config.toml").read_text())


def test_config_toml_root_keys_always_present(tmp_path):
    # check_for_update guards the version pin (the TUI's update prompt runs
    # `npm install -g` on Enter); the suppress key pairs with the interactive
    # feature flag. Both are ROOT keys — parsing proves they didn't land under
    # an open [table] header.
    cfg = _written_config(tmp_path)
    assert cfg["check_for_update_on_startup"] is False
    assert cfg["suppress_unstable_features_warning"] is True
    assert cfg["features"]["plugins"] is False


def test_config_toml_question_flag_dashboard_and_interactive(tmp_path):
    # request_user_input is exposed to interactive-USER sessions: the bare TUI
    # AND the headless -p dashboard (which now HOLDS the request and surfaces a
    # question card). OFF for autonomous runs (task/phone/meeting) and for a
    # session with no client_type — nobody answers.
    headless_no_client = _written_config(tmp_path)
    assert "default_mode_request_user_input" not in headless_no_client["features"]

    headless_dashboard = _written_config(tmp_path, client_type="dashboard")
    assert headless_dashboard["features"]["default_mode_request_user_input"] is True
    # Headless dashboard must NOT enable the TUI-only hooks flag.
    assert "hooks" not in headless_dashboard["features"]

    headless_task = _written_config(tmp_path, client_type="task")
    assert "default_mode_request_user_input" not in headless_task["features"]

    interactive = _written_config(tmp_path, interactive=True)
    assert interactive["features"]["default_mode_request_user_input"] is True
    assert interactive["features"]["hooks"] is True


def test_config_toml_is_owner_only(tmp_path):
    # config.toml can carry an inline MCP bearer — must be locked 0600 like
    # auth.json, not world/group readable.
    _Layer._write_config_toml(
        tmp_path, "prompt",
        mcp_toml='[mcp_servers.x.http_headers]\n"Authorization" = "Bearer secret"',
    )
    mode = (tmp_path / "config.toml").stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)
