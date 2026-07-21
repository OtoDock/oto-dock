"""Tests for MCP config rewriting for remote execution.

Covers `_rewrite_mcp_json_for_remote`, `_rewrite_mcp_toml_for_remote`,
`_rewrite_stdio_paths`, and `_rewrite_env_for_remote`. The rewriters
must produce satellite-side paths that match the satellite's actual
on-disk layout — including the OS-aware bits (Windows uses
`~/OtoDock/...venv/Scripts/<bin>.exe` while Unix uses
`~/.oto-dock/...venv/bin/<bin>`).
"""

from unittest.mock import patch


SAT_PORT = 49152  # arbitrary tunnel port for tests


class TestRewriteMcpJson:
    def test_rewrites_sse_url_to_satellite_tunnel(self):
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote

        mcp_config = {
            "mcpServers": {
                "file-tools": {
                    "url": "http://localhost:8932/sse",
                    "env": {"SESSION_ID": "abc"},
                }
            }
        }

        result = _rewrite_mcp_json_for_remote(mcp_config, SAT_PORT)
        assert result["mcpServers"]["file-tools"]["url"] == (
            f"http://127.0.0.1:{SAT_PORT}/mcp/file-tools/sse"
        )

    def test_rewrites_proxy_url_in_env(self):
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote

        mcp_config = {
            "mcpServers": {
                "schedules-mcp": {
                    "command": "node",
                    "args": ["index.js"],
                    "env": {
                        "PROXY_URL": "http://localhost:8400",
                        "OTHER": "unchanged",
                    },
                }
            }
        }

        result = _rewrite_mcp_json_for_remote(mcp_config, SAT_PORT)
        env = result["mcpServers"]["schedules-mcp"]["env"]
        assert env["PROXY_URL"] == f"http://127.0.0.1:{SAT_PORT}"
        assert env["OTHER"] == "unchanged"

    def test_external_hosted_urls_are_not_tunneled(self):
        """C1: external hosted MCPs (Linear/Slack/Notion/Zoom) carry a real
        public host and must reach the internet directly — only LOOPBACK URLs
        (Docker MCPs on the platform) are tunneled. Tunneling an external URL
        to the satellite loopback would 404.
        """
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote

        mcp_config = {
            "mcpServers": {
                "linear": {"url": "https://mcp.linear.app/mcp"},
                "file-tools": {"url": "http://127.0.0.1:8932/mcp"},
            }
        }

        result = _rewrite_mcp_json_for_remote(mcp_config, SAT_PORT)
        # External host: left untouched.
        assert result["mcpServers"]["linear"]["url"] == "https://mcp.linear.app/mcp"
        # Loopback Docker MCP: tunneled.
        assert result["mcpServers"]["file-tools"]["url"] == (
            f"http://127.0.0.1:{SAT_PORT}/mcp/file-tools/mcp"
        )

    def test_injects_session_id_into_http_mcp(self):
        """Docker MCPs (file-tools) need the OtoDock session_id in the URL to
        call back to /v1/hooks/* (resolve-path, document-preview). The remote
        rewrite must inject it for loopback (tunneled) URLs."""
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote

        cfg = {"mcpServers": {"file-tools": {"type": "http", "url": "http://localhost:8932/mcp/"}}}
        result = _rewrite_mcp_json_for_remote(cfg, SAT_PORT, session_id="sess-xyz")
        url = result["mcpServers"]["file-tools"]["url"]
        assert "session_id=sess-xyz" in url
        assert f"127.0.0.1:{SAT_PORT}/mcp/file-tools/mcp/" in url

    def test_external_hosted_url_gets_no_session_id(self):
        """External MCPs aren't tunneled and must NOT receive the OtoDock
        session_id (no leak of the session to third-party servers)."""
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote

        cfg = {"mcpServers": {"linear": {"url": "https://mcp.linear.app/mcp"}}}
        result = _rewrite_mcp_json_for_remote(cfg, SAT_PORT, session_id="sess-xyz")
        assert "session_id" not in result["mcpServers"]["linear"]["url"]


class TestStripExcludedTomlSections:
    """C2: line-based removal of excluded `[mcp_servers.<key>]` blocks. The old
    `[^\\[]*` regex stopped at the `[` inside `args = [...]`, leaving a corrupt
    half-block."""

    def test_strips_whole_block_with_args_bracket(self):
        from core.remote.remote_execution import _strip_toml_mcp_sections

        toml = (
            '[mcp_servers.keep-me]\n'
            'command = "node"\n'
            'args = ["a.js", "b.js"]\n'
            '\n'
            '[mcp_servers.drop-me]\n'
            'command = "python"\n'
            'args = ["x.py", "y.py"]\n'
            '\n'
            '[mcp_servers.also-keep]\n'
            'url = "http://127.0.0.1:9/mcp"\n'
        )
        result = _strip_toml_mcp_sections(toml, {"drop-me"})
        assert "[mcp_servers.drop-me]" not in result
        assert "x.py" not in result  # body gone (old regex left it)
        assert "[mcp_servers.keep-me]" in result
        assert '"a.js", "b.js"' in result  # neighbor's args intact
        assert "[mcp_servers.also-keep]" in result
        assert "http://127.0.0.1:9/mcp" in result

    def test_drops_env_subtable_of_dropped_server(self):
        from core.remote.remote_execution import _strip_toml_mcp_sections

        toml = (
            '[mcp_servers.drop-me]\n'
            'command = "python"\n'
            '[mcp_servers.drop-me.env]\n'
            'SECRET = "shh"\n'
            '[mcp_servers.keep-me]\n'
            'command = "node"\n'
        )
        result = _strip_toml_mcp_sections(toml, {"drop-me"})
        assert "drop-me" not in result
        assert "SECRET" not in result  # the dropped server's env went too
        assert "[mcp_servers.keep-me]" in result


class TestRewriteMcpToml:
    def test_rewrites_localhost_urls_to_satellite_tunnel(self):
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote

        toml = (
            '[mcp_servers.file-tools]\n'
            'url = "http://localhost:8932/mcp"\n'
            '\n'
            '[mcp_servers.schedules-mcp]\n'
            'command = "node"\n'
            'args = ["index.js"]\n'
            '\n'
            '[mcp_servers.schedules-mcp.env]\n'
            'PROXY_URL = "http://127.0.0.1:8400"\n'
        )
        result = _rewrite_mcp_toml_for_remote(toml, SAT_PORT)
        assert f"127.0.0.1:{SAT_PORT}/mcp/file-tools/mcp" in result

    def test_injects_session_id_into_toml_url(self):
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote

        toml = '[mcp_servers.file-tools]\nurl = "http://localhost:8932/mcp"\n'
        result = _rewrite_mcp_toml_for_remote(toml, SAT_PORT, session_id="sess-xyz")
        assert "session_id=sess-xyz" in result
        assert f"127.0.0.1:{SAT_PORT}/mcp/file-tools/mcp" in result
        assert f"127.0.0.1:{SAT_PORT}" in result
        # The slug-scoped tunnel URL should have replaced the bare
        # localhost; the only `127.0.0.1` survivor is `:<SAT_PORT>`.
        assert ":8932" not in result
        assert ":8400" not in result

    def test_startup_timeout_raised_to_remote_floor(self):
        from core.remote.remote_execution import (
            _rewrite_mcp_toml_for_remote,
            _REMOTE_MCP_STARTUP_FLOOR,
            _REMOTE_MCP_STARTUP_OVERRIDES,
        )

        toml = (
            '[mcp_servers.schedules-mcp]\n'
            'startup_timeout_sec = 10\n'
            'command = "node"\n'
            '\n'
            '[mcp_servers.google-workspace]\n'
            'startup_timeout_sec = 10\n'
            'command = "node"\n'
        )
        result = _rewrite_mcp_toml_for_remote(toml, SAT_PORT)
        # The local 10s warm-gate value must never survive into a remote config.
        assert "startup_timeout_sec = 10" not in result
        # A light server is raised to the remote floor.
        assert f"startup_timeout_sec = {_REMOTE_MCP_STARTUP_FLOOR}" in result
        # A heavy server gets its per-MCP override (> floor).
        gw = _REMOTE_MCP_STARTUP_OVERRIDES["google-workspace"]
        assert gw > _REMOTE_MCP_STARTUP_FLOOR
        assert f"startup_timeout_sec = {gw}" in result

    def test_startup_timeout_never_lowered(self):
        """A value already above the floor is preserved, not clamped down."""
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote

        toml = (
            '[mcp_servers.schedules-mcp]\n'
            'startup_timeout_sec = 300\n'
            'command = "node"\n'
        )
        result = _rewrite_mcp_toml_for_remote(toml, SAT_PORT)
        assert "startup_timeout_sec = 300" in result

    def test_toml_rewrite_uses_manifest_category_not_on_disk_dir(self):
        """REGRESSION: the Codex TOML rewrite must resolve the satellite path
        via the manifest category — NOT a naive ``mcps/<on-disk-dir>`` swap.

        schedules-mcp lives on disk under ``mcps/custom/`` but its manifest declares
        ``category: core``, so the satellite installs it at ``mcps/core/schedules-mcp``.
        A naive prefix swap produced ``mcps/custom/schedules-mcp`` → a path that
        doesn't exist on the satellite → the MCP failed to start under Codex
        (Claude's JSON twin was immune; it always resolved via the manifest)."""
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        toml = (
            '[mcp_servers.schedules-mcp]\n'
            f'command = "{plat}/custom/schedules-mcp/venv/bin/python"\n'
            f'args = ["{plat}/custom/schedules-mcp/server.py"]\n'
        )
        with patch(
            "core.remote.remote_mcp_rewrite._resolve_satellite_mcp_path_info",
            return_value=("core", "schedules-mcp", f"{plat}/custom/schedules-mcp", None),
        ):
            result = _rewrite_mcp_toml_for_remote(toml, SAT_PORT)
        assert "~/.oto-dock/mcps/core/schedules-mcp/venv/bin/python" in result
        assert "~/.oto-dock/mcps/core/schedules-mcp/server.py" in result
        # The on-disk dir-group must NOT leak into the satellite path.
        assert "mcps/custom/schedules-mcp" not in result

    def test_toml_rewrite_windows_category_aware_venv_layout(self):
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        toml = (
            '[mcp_servers.schedules-mcp]\n'
            f'command = "{plat}/custom/schedules-mcp/venv/bin/python"\n'
            f'args = ["{plat}/custom/schedules-mcp/server.py"]\n'
        )
        with patch(
            "core.remote.remote_mcp_rewrite._resolve_satellite_mcp_path_info",
            return_value=("core", "schedules-mcp", f"{plat}/custom/schedules-mcp", None),
        ):
            result = _rewrite_mcp_toml_for_remote(
                toml, SAT_PORT, target_os="windows",
            )
        assert "~/OtoDock/mcps/core/schedules-mcp/venv/Scripts/python.exe" in result
        assert "~/OtoDock/mcps/core/schedules-mcp/server.py" in result
        assert ".oto-dock" not in result
        assert "mcps/custom/schedules-mcp" not in result

    def test_toml_rewrite_name_differs_from_dir(self):
        """workspace-mcp dir / google-workspace manifest name → the satellite
        path must use the manifest NAME (google-workspace)."""
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        toml = (
            '[mcp_servers.google-workspace]\n'
            f'command = "{plat}/community/workspace-mcp/venv/bin/python"\n'
            f'args = ["{plat}/community/workspace-mcp/main.py"]\n'
        )
        with patch(
            "core.remote.remote_mcp_rewrite._resolve_satellite_mcp_path_info",
            return_value=(
                "community", "google-workspace",
                f"{plat}/community/workspace-mcp", None,
            ),
        ):
            result = _rewrite_mcp_toml_for_remote(toml, SAT_PORT)
        assert "~/.oto-dock/mcps/community/google-workspace/venv/bin/python" in result
        assert "mcps/community/workspace-mcp" not in result

    def test_toml_rewrite_preserves_http_headers_block(self):
        """A bearer MCP's ``http_headers`` sub-table must survive the rewrite
        while its loopback url is tunneled (github-mcp)."""
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote

        toml = (
            '[mcp_servers.github-mcp]\n'
            'url = "http://localhost:8935/mcp"\n'
            '[mcp_servers.github-mcp.http_headers]\n'
            '"Authorization" = "Bearer ghp_secret"\n'
        )
        result = _rewrite_mcp_toml_for_remote(toml, SAT_PORT, session_id="s1")
        assert f"127.0.0.1:{SAT_PORT}/mcp/github-mcp/mcp" in result
        assert "[mcp_servers.github-mcp.http_headers]" in result
        assert '"Authorization" = "Bearer ghp_secret"' in result

    def test_toml_rewrite_injects_proxy_callback_env(self):
        """Codex does NOT propagate the daemon env to MCP subprocesses, so each
        stdio MCP's env table must get PROXY_URL (tunnel) + PROXY_API_KEY (JWT)
        injected — else callback MCPs (task/memory/notifications/...) send an
        empty ``Authorization: Bearer ``. HTTP MCPs have no env block and must
        stay untouched (they auth via http_headers / the session_id URL)."""
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote

        toml = (
            '[mcp_servers.schedules-mcp]\n'
            'command = "node"\n'
            'args = ["x.js"]\n'
            'env = { "OTO_SESSION_ID" = "s1" }\n'
            '\n'
            '[mcp_servers.github-mcp]\n'
            'url = "http://localhost:8935/mcp"\n'
            '[mcp_servers.github-mcp.http_headers]\n'
            '"Authorization" = "Bearer ghp_x"\n'
        )
        result = _rewrite_mcp_toml_for_remote(
            toml, SAT_PORT, session_id="s1", proxy_api_key="JWT.tok_123",
        )
        assert f'"PROXY_URL" = "http://127.0.0.1:{SAT_PORT}"' in result
        assert '"PROXY_API_KEY" = "JWT.tok_123"' in result
        # schedules-mcp's existing env survived the append.
        assert '"OTO_SESSION_ID" = "s1"' in result
        # The HTTP MCP (github) got NO injection — exactly one PROXY_API_KEY.
        assert result.count("PROXY_API_KEY") == 1

    def test_toml_rewrite_no_proxy_env_without_key(self):
        """Without ``proxy_api_key`` (defensive default) nothing is injected."""
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote

        toml = '[mcp_servers.schedules-mcp]\ncommand = "node"\nenv = { "A" = "1" }\n'
        result = _rewrite_mcp_toml_for_remote(toml, SAT_PORT)
        assert "PROXY_API_KEY" not in result
        assert "PROXY_URL" not in result


class TestRewriteStdioPaths:
    def test_linux_default_keeps_unix_layout(self):
        from core.remote.remote_execution import _rewrite_stdio_paths
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        cmd, args = _rewrite_stdio_paths(
            f"{plat}/custom/notifications-mcp/venv/bin/python",
            [f"{plat}/custom/notifications-mcp/server.py"],
            mcp_name="notifications-mcp",
            satellite_category="core",
            platform_dir=f"{plat}/custom/notifications-mcp",
        )
        assert cmd == "~/.oto-dock/mcps/core/notifications-mcp/venv/bin/python"
        assert args == ["~/.oto-dock/mcps/core/notifications-mcp/server.py"]

    def test_windows_swaps_dir_name_and_venv_layout(self):
        from core.remote.remote_execution import _rewrite_stdio_paths
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        cmd, args = _rewrite_stdio_paths(
            f"{plat}/custom/notifications-mcp/venv/bin/python",
            [f"{plat}/custom/notifications-mcp/server.py"],
            mcp_name="notifications-mcp",
            satellite_category="core",
            platform_dir=f"{plat}/custom/notifications-mcp",
            target_os="windows",
        )
        assert cmd == (
            "~/OtoDock/mcps/core/notifications-mcp/venv/Scripts/python.exe"
        )
        assert args == ["~/OtoDock/mcps/core/notifications-mcp/server.py"]

    def test_windows_collapses_python3_to_python(self):
        from core.remote.remote_execution import _rewrite_stdio_paths
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        cmd, _ = _rewrite_stdio_paths(
            f"{plat}/community/workspace-mcp/venv/bin/python3",
            [],
            mcp_name="workspace-mcp",
            satellite_category="community",
            platform_dir=f"{plat}/community/workspace-mcp",
            target_os="windows",
        )
        # Windows ships only python.exe, never python3.exe.
        assert cmd == (
            "~/OtoDock/mcps/community/workspace-mcp/venv/Scripts/python.exe"
        )

    def test_windows_handles_pip_installed_entrypoints(self):
        from core.remote.remote_execution import _rewrite_stdio_paths
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        cmd, args = _rewrite_stdio_paths(
            f"{plat}/community/workspace-mcp/venv/bin/workspace-mcp",
            ["--single-user", "--transport", "stdio"],
            mcp_name="workspace-mcp",
            satellite_category="community",
            platform_dir=f"{plat}/community/workspace-mcp",
            target_os="windows",
        )
        assert cmd == (
            "~/OtoDock/mcps/community/workspace-mcp/venv/Scripts/workspace-mcp.exe"
        )
        # Non-path args pass through untouched.
        assert args == ["--single-user", "--transport", "stdio"]

    def test_manifest_name_differs_from_platform_dir_name(self):
        """Real-world bug: workspace-mcp dir / google-workspace manifest name.

        Caught in prod on a Windows satellite — the regex-based variant of
        the rewriter (which assumed the dir segment was the manifest name)
        silently left the Linux platform path in the shipped MCP config,
        causing the CLI to fail to spawn the MCP. The exact-prefix substitution
        with ``platform_dir`` fixes this.
        """
        from core.remote.remote_execution import _rewrite_stdio_paths
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        cmd, args = _rewrite_stdio_paths(
            f"{plat}/community/workspace-mcp/venv/bin/workspace-mcp",
            ["--single-user", "--transport", "stdio"],
            mcp_name="google-workspace",  # manifest name
            satellite_category="community",
            platform_dir=f"{plat}/community/workspace-mcp",  # dir name
            target_os="windows",
        )
        # Both segments swapped correctly: dir → name; bin → Scripts/.exe.
        assert cmd == (
            "~/OtoDock/mcps/community/google-workspace/venv/Scripts/workspace-mcp.exe"
        )
        assert args == ["--single-user", "--transport", "stdio"]

    def test_windows_leaves_bare_commands_alone(self):
        """`node`, `npx`, plain commands resolved via PATH on both OSes."""
        from core.remote.remote_execution import _rewrite_stdio_paths
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        cmd, args = _rewrite_stdio_paths(
            "node",
            [f"{plat}/community/nextcloud/node_modules/foo/index.js"],
            mcp_name="nextcloud",
            satellite_category="community",
            platform_dir=f"{plat}/community/nextcloud",
            target_os="windows",
        )
        assert cmd == "node"
        # node_modules path is layout-identical on Windows; only dir name swaps.
        assert args == [
            "~/OtoDock/mcps/community/nextcloud/node_modules/foo/index.js"
        ]

    def test_fallback_prefix_swap_when_no_manifest_info(self):
        """Without manifest info, we do a plain prefix swap."""
        from core.remote.remote_execution import _rewrite_stdio_paths
        import config as app_config

        plat = str(app_config.MCPS_DIR.resolve())
        cmd, _ = _rewrite_stdio_paths(
            f"{plat}/custom/foo/venv/bin/python", [],
        )
        assert cmd == "~/.oto-dock/mcps/custom/foo/venv/bin/python"

        cmd, _ = _rewrite_stdio_paths(
            f"{plat}/custom/foo/venv/bin/python", [],
            target_os="windows",
        )
        assert cmd == "~/OtoDock/mcps/custom/foo/venv/Scripts/python.exe"


class TestTranslateVenvForWindows:
    def test_idempotent_on_scripts_layout(self):
        from core.remote.remote_execution import _translate_venv_for_windows

        s = "C:/foo/venv/Scripts/python.exe"
        assert _translate_venv_for_windows(s) == s

    def test_does_not_double_suffix(self):
        from core.remote.remote_execution import _translate_venv_for_windows

        # An input that already has .exe should not become .exe.exe.
        s = "/home/foo/venv/bin/workspace-mcp.exe"
        assert _translate_venv_for_windows(s) == (
            "/home/foo/venv/Scripts/workspace-mcp.exe"
        )


class TestRewriteEnv:
    def test_rewrites_proxy_url_to_tunnel(self):
        from core.remote.remote_execution import _rewrite_env_for_remote

        env = {
            "PROXY_URL": "http://localhost:8400",
            "API_KEY": "secret",
            "HOME": "/home/user",
        }

        result = _rewrite_env_for_remote(env, SAT_PORT)
        assert result["PROXY_URL"] == f"http://127.0.0.1:{SAT_PORT}"
        assert result["API_KEY"] == "secret"
        assert result["HOME"] == "/home/user"
