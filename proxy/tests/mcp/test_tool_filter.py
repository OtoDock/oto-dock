"""Generic tool-filter framework tests.

Covers:
  * Manifest validator accepts/rejects the ``tool_filter`` block.
  * ``mcp_registry.get_tool_filter`` reader returns tuple only when both
    manifest AND admin regex are set.
  * ``docker_manager._inject_mcp_env`` writes ENABLED_TOOLS_FLAG with
    the composed CLI flag when manifest declares + admin regex set.
  * ``docker_manager._inject_mcp_env`` writes empty env var when
    manifest declares but admin hasn't set a regex (so stale flags from
    a prior run don't survive).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.mcp import mcp_registry


# ---------------------------------------------------------------------------
# Manifest validator
# ---------------------------------------------------------------------------


class TestToolFilterValidator:
    def test_accepts_arg_name_only(self):
        cfg = mcp_registry._parse_tool_filter(
            {"arg_name": "--enabled-tools"}, "test-mcp",
        )
        assert cfg is not None
        assert cfg.arg_name == "--enabled-tools"
        # Default env var name when omitted.
        assert cfg.env_var_name == "ENABLED_TOOLS_FLAG"

    def test_accepts_custom_env_var_name(self):
        cfg = mcp_registry._parse_tool_filter(
            {"arg_name": "--tools", "env_var_name": "MS365_ENABLED_TOOLS"},
            "test-mcp",
        )
        assert cfg is not None
        assert cfg.env_var_name == "MS365_ENABLED_TOOLS"

    def test_none_when_block_absent(self):
        assert mcp_registry._parse_tool_filter(None, "test-mcp") is None

    def test_rejects_missing_arg_name(self):
        with pytest.raises(ValueError, match="arg_name is required"):
            mcp_registry._parse_tool_filter({}, "test-mcp")

    def test_rejects_short_flag(self):
        # Short flags like `-t` are typo-prone; spec requires `--long-form`.
        with pytest.raises(ValueError, match="must be a long-form CLI flag"):
            mcp_registry._parse_tool_filter(
                {"arg_name": "-t"}, "test-mcp",
            )

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="tool_filter must be an object"):
            mcp_registry._parse_tool_filter("--enabled-tools", "test-mcp")

    def test_rejects_invalid_env_var_name(self):
        with pytest.raises(ValueError, match="POSIX env var name pattern"):
            mcp_registry._parse_tool_filter(
                {"arg_name": "--tools", "env_var_name": "lower-case"},
                "test-mcp",
            )


# ---------------------------------------------------------------------------
# get_tool_filter reader
# ---------------------------------------------------------------------------


class TestGetToolFilter:
    def test_returns_none_when_manifest_has_no_block(self):
        # Manifest with no tool_filter → reader returns None even when
        # admin has set a regex (no point — runtime would ignore it).
        manifest = MagicMock()
        manifest.tool_filter = None
        with patch.dict(
            mcp_registry._manifests, {"some-mcp": manifest}, clear=False,
        ), patch(
            "storage.mcp.mcp_store.get_tool_filter_regex", return_value="^x_.*",
        ):
            assert mcp_registry.get_tool_filter("some-mcp") is None

    def test_returns_none_when_regex_empty(self):
        manifest = MagicMock()
        manifest.tool_filter = mcp_registry.ToolFilterConfig(
            arg_name="--enabled-tools",
        )
        with patch.dict(
            mcp_registry._manifests, {"some-mcp": manifest}, clear=False,
        ), patch(
            "storage.mcp.mcp_store.get_tool_filter_regex", return_value="",
        ):
            assert mcp_registry.get_tool_filter("some-mcp") is None

    def test_returns_tuple_when_both_set(self):
        manifest = MagicMock()
        manifest.tool_filter = mcp_registry.ToolFilterConfig(
            arg_name="--enabled-tools",
        )
        with patch.dict(
            mcp_registry._manifests, {"some-mcp": manifest}, clear=False,
        ), patch(
            "storage.mcp.mcp_store.get_tool_filter_regex",
            return_value="^(mail|calendar)_.*",
        ):
            tf = mcp_registry.get_tool_filter("some-mcp")
        assert tf == ("--enabled-tools", "^(mail|calendar)_.*")


# ---------------------------------------------------------------------------
# docker_manager._inject_mcp_env — writes ENABLED_TOOLS_FLAG correctly
# ---------------------------------------------------------------------------


class TestDockerManagerToolFilterInjection:
    def _stub_manifest(
        self, tmp_path: Path,
        *,
        tool_filter: mcp_registry.ToolFilterConfig | None,
    ) -> MagicMock:
        manifest = MagicMock()
        manifest.name = "test-mcp"
        manifest.mcp_dir = tmp_path
        manifest.env = {}
        manifest.agent_env = {}
        manifest.credentials.type = "none"
        manifest.tool_filter = tool_filter
        return manifest

    def test_writes_flag_when_manifest_and_regex_both_set(self, tmp_path):
        from services.mcp import docker_manager
        tf = mcp_registry.ToolFilterConfig(
            arg_name="--enabled-tools",
            env_var_name="ENABLED_TOOLS_FLAG",
        )
        manifest = self._stub_manifest(tmp_path, tool_filter=tf)

        with patch(
            "services.mcp.mcp_registry.get_tool_filter",
            return_value=("--enabled-tools", "^(mail|calendar)_.*"),
        ):
            docker_manager._inject_mcp_env(manifest)

        env = (tmp_path / ".env").read_text()
        # Single-quoted regex preserves shell-special chars.
        assert (
            "ENABLED_TOOLS_FLAG=--enabled-tools '^(mail|calendar)_.*'" in env
        )

    def test_writes_empty_flag_when_manifest_declared_but_no_regex(
        self, tmp_path,
    ):
        """Stale ENABLED_TOOLS_FLAG must be cleared when admin clears
        the regex — otherwise the container would keep applying the
        old filter."""
        from services.mcp import docker_manager
        tf = mcp_registry.ToolFilterConfig(
            arg_name="--enabled-tools",
            env_var_name="ENABLED_TOOLS_FLAG",
        )
        manifest = self._stub_manifest(tmp_path, tool_filter=tf)

        with patch(
            "services.mcp.mcp_registry.get_tool_filter", return_value=None,
        ):
            docker_manager._inject_mcp_env(manifest)

        env = (tmp_path / ".env").read_text()
        # Var present but empty — ENTRYPOINT shell expansion is a no-op.
        assert "ENABLED_TOOLS_FLAG=" in env
        assert "ENABLED_TOOLS_FLAG=--enabled-tools" not in env

    def test_no_env_var_for_mcps_without_tool_filter_block(self, tmp_path):
        from services.mcp import docker_manager
        manifest = self._stub_manifest(tmp_path, tool_filter=None)

        with patch(
            "services.mcp.mcp_registry.get_tool_filter", return_value=None,
        ):
            docker_manager._inject_mcp_env(manifest)

        # No env to write at all → no .env file should exist (manifests
        # without env vars don't litter the MCP folder).
        assert not (tmp_path / ".env").exists()
