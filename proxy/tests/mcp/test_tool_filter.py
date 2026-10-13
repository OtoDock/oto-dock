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

import json
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


def _manifest_file(tmp_path, extra: dict):
    d = tmp_path / "x-mcp"
    d.mkdir(parents=True)
    data = {
        "name": "x-mcp", "label": "X", "description": "X tools.", "version": "1.0.0",
        "category": "community",
        "server": {"runtime": "python", "transport": "stdio", "command": "python", "args": ["s.py"]},
        "credentials": {"type": "none"},
        **extra,
    }
    (d / "manifest.json").write_text(json.dumps(data))
    return d / "manifest.json"


@pytest.mark.parametrize("declared", [
    {"config": [{"key": "ENABLED_TOOLS_FLAG", "label": "flag"}]},
    {"env": {"ENABLED_TOOLS_REGEX": "^x"}},
    {"agent_env": {"ENABLED_TOOLS_FLAG": "x"}},
    {"credentials": {"type": "infra", "fields": [{"key": "ENABLED_TOOLS_REGEX", "label": "r"}]}},
    {"instances": {"fields": [{"key": "ENABLED_TOOLS_FLAG", "label": "f"}]}},
])
def test_a_filter_variable_that_is_a_declared_key_is_refused(tmp_path, declared):
    """The two variables land in the MCP's env beside every declared key; a
    shared name would replace that key's value silently, so the manifest is
    refused at parse."""
    from services.mcp.mcp_manifest_parse import _parse_manifest
    with pytest.raises(ValueError, match="invalid tool_filter block.*is also a declared"):
        _parse_manifest(_manifest_file(tmp_path, {
            "tool_filter": {"arg_name": "--enabled-tools"}, **declared}))


def test_distinct_filter_variables_parse_beside_the_declared_keys(tmp_path):
    from services.mcp.mcp_manifest_parse import _parse_manifest
    m = _parse_manifest(_manifest_file(tmp_path, {
        "tool_filter": {"arg_name": "--enabled-tools", "env_var_name": "MS_TOOLS_FLAG",
                        "regex_env_var_name": "MS_TOOLS_RE"},
        "config": [{"key": "ENABLED_TOOLS_FLAG", "label": "an unrelated key"}],
        "env": {"ENABLED_TOOLS_REGEX": "x"}}))
    assert (m.tool_filter.env_var_name, m.tool_filter.regex_env_var_name) == (
        "MS_TOOLS_FLAG", "MS_TOOLS_RE")


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


# ---------------------------------------------------------------------------
# The bare regex variable: it reaches a server's argv intact
# ---------------------------------------------------------------------------

import os
import subprocess


def _compose_dotenv_value(raw: str) -> str:
    """What Compose's dotenv reader hands the container for one value of the
    form ``_inject_mcp_env`` writes (probed with Compose 5.3, the default
    ``env_file`` format): a single-quoted value is literal but for ``\\'``,
    an unquoted one is taken as written (no ``$`` in the cases below)."""
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("\\'", "'")
    return raw


def _env_from(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        key, _, raw = line.partition("=")
        out[key] = _compose_dotenv_value(raw)
    return out


def _argv(script: str, env: dict[str, str]) -> list[str]:
    """Run ``script`` under ``sh -c`` the way a compose entrypoint does and
    return the argv its ``exec`` hands the program (``printf`` stands in)."""
    out = subprocess.run(
        ["sh", "-c", script], env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True, check=True,
    ).stdout
    return out.decode().split("\0")[:-1]


class TestBareRegexVariable:
    def _inject(self, tmp_path: Path, regex: str | None, **tf_kw) -> Path:
        from services.mcp import docker_manager
        tf = mcp_registry.ToolFilterConfig(arg_name="--enabled-tools", **tf_kw)
        manifest = TestDockerManagerToolFilterInjection()._stub_manifest(tmp_path, tool_filter=tf)
        value = ("--enabled-tools", regex) if regex else None
        with patch("services.mcp.mcp_registry.get_tool_filter", return_value=value):
            docker_manager._inject_mcp_env(manifest)
        return tmp_path / ".env"

    @pytest.mark.parametrize("regex", [
        "^(mail|calendar)_.*$",
        "^(?!sharepoint_)",
        "a b|c*d",
        "x`id`$HOME",
        "it's|they're",
        "^a#b\\d+",
        "a\\\\'b",
        "x\\\\",
    ])
    def test_the_regex_reaches_the_program_byte_for_byte(self, tmp_path, regex):
        env = _env_from(self._inject(tmp_path, regex))
        assert env["ENABLED_TOOLS_REGEX"] == regex
        argv = _argv('exec printf "%s\\0" --enabled-tools "$ENABLED_TOOLS_REGEX"', env)
        assert argv == ["--enabled-tools", regex]

    def test_the_flag_form_keeps_its_quotes_when_expanded(self, tmp_path):
        """Why the bare variable exists: an unquoted expansion of the flag
        hands the program the quotes and splits on a space."""
        env = _env_from(self._inject(tmp_path, "^(mail|calendar)_"))
        argv = _argv('exec printf "%s\\0" ${ENABLED_TOOLS_FLAG}', env)
        assert argv == ["--enabled-tools", "'^(mail|calendar)_'"]

    def test_no_filter_writes_both_empty(self, tmp_path):
        env = _env_from(self._inject(tmp_path, None))
        assert env["ENABLED_TOOLS_REGEX"] == "" and env["ENABLED_TOOLS_FLAG"] == ""
        assert _argv('exec printf "%s\\0" ok ${ENABLED_TOOLS_FLAG}', env) == ["ok"]

    def test_a_custom_variable_name(self, tmp_path):
        env = _env_from(self._inject(tmp_path, "^mail_", regex_env_var_name="ENABLED_TOOLS"))
        assert env["ENABLED_TOOLS"] == "^mail_" and "ENABLED_TOOLS_REGEX" not in env

    @pytest.mark.parametrize("regex", ["a\nb", "a\\", "tab\there", "a\\'b"])
    def test_a_regex_the_env_cannot_carry_empties_both(self, tmp_path, regex, caplog):
        env = _env_from(self._inject(tmp_path, regex))
        assert env["ENABLED_TOOLS_REGEX"] == "" and env["ENABLED_TOOLS_FLAG"] == ""
        assert "runs unfiltered" in caplog.text

    def test_a_second_write_changes_nothing(self, tmp_path):
        from services.mcp import docker_manager
        tf = mcp_registry.ToolFilterConfig(arg_name="--enabled-tools")
        manifest = TestDockerManagerToolFilterInjection()._stub_manifest(tmp_path, tool_filter=tf)
        with patch("services.mcp.mcp_registry.get_tool_filter",
                   return_value=("--enabled-tools", "it's$")):
            assert docker_manager._inject_mcp_env(manifest) is True
            assert docker_manager._inject_mcp_env(manifest) is False


class TestRegexVariableManifestField:
    def test_default(self):
        cfg = mcp_registry._parse_tool_filter({"arg_name": "--enabled-tools"}, "m")
        assert cfg.regex_env_var_name == "ENABLED_TOOLS_REGEX"

    def test_custom(self):
        cfg = mcp_registry._parse_tool_filter(
            {"arg_name": "--enabled-tools", "regex_env_var_name": "ENABLED_TOOLS"}, "m")
        assert cfg.regex_env_var_name == "ENABLED_TOOLS"

    @pytest.mark.parametrize("bad", ["lower", "", 3, "ENABLED_TOOLS_FLAG"])
    def test_refused(self, bad):
        with pytest.raises(ValueError):
            mcp_registry._parse_tool_filter(
                {"arg_name": "--enabled-tools", "regex_env_var_name": bad}, "m")


class TestToolFilterSave:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.mcp import mcps as mcps_api
        from auth.providers import get_current_user
        from storage.mcp import mcp_store
        manifest = MagicMock()
        manifest.tool_filter = mcp_registry.ToolFilterConfig(arg_name="--enabled-tools")
        manifest.server.runtime = "python"
        monkeypatch.setattr(mcp_registry, "get_manifest", lambda n: manifest)
        saved = []
        monkeypatch.setattr(mcp_store, "set_tool_filter_regex", lambda n, r: saved.append(r))
        app = FastAPI()
        app.include_router(mcps_api.router)
        app.dependency_overrides[get_current_user] = lambda: MagicMock(is_admin=True, role="admin")
        monkeypatch.setattr(mcps_api, "require_admin", lambda u: None)
        return TestClient(app), saved

    @pytest.mark.parametrize("regex", ["a\nb", "ends\\"])
    def test_a_regex_the_env_cannot_carry_is_refused(self, client, regex):
        c, saved = client
        r = c.put("/v1/admin/mcps/m365-mcp/tool-filter", json={"regex": regex})
        assert r.status_code == 400 and saved == []

    def test_a_regex_with_quotes_and_dollars_is_saved(self, client):
        c, saved = client
        r = c.put("/v1/admin/mcps/m365-mcp/tool-filter", json={"regex": "it's$|^a b"})
        assert r.status_code == 200 and saved == ["it's$|^a b"]


class TestDotenvQuote:
    """``dotenv_quote``: the single-quoted form Compose hands over literally,
    or None for a value that form cannot carry."""

    def test_quotes_dollars_and_even_backslash_runs_are_carried(self):
        from services.mcp import docker_manager
        assert docker_manager.dotenv_quote("^(mail)_.*$") == "'^(mail)_.*$'"
        assert docker_manager.dotenv_quote("it's") == "'it\\'s'"
        assert docker_manager.dotenv_quote("a\\\\b") == "'a\\\\b'"
        assert docker_manager.dotenv_quote("a\\\\'b") == "'a\\\\\\'b'"

    @pytest.mark.parametrize("value", ["ends\\", "a\\'b", "odd\\\\\\", "tab\there", "nl\n", "\x7f"])
    def test_a_value_the_form_cannot_carry_is_refused(self, value):
        from services.mcp import docker_manager
        assert docker_manager.dotenv_quote(value) is None

    def test_a_long_backslash_run_costs_one_pass(self):
        from services.mcp import docker_manager
        assert docker_manager.dotenv_quote("\\" * 200_001) is None
        assert docker_manager.dotenv_quote("\\" * 200_000) == "'" + "\\" * 200_000 + "'"
