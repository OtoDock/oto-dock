"""A manifest ``config`` row's default is in effect before an admin saves it,
in every env build: the stdio entry, the docker ``.env``, the per-user
resolver and a ``${config:<mcp>:<key>}`` template. A saved value wins (a
saved ``""`` too), ``_`` keys stay out, an empty default sets nothing, and
the network target keeps stored values only (its carve opens egress)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from services.mcp import docker_manager, mcp_registry
from services.mcp.mcp_manifest_types import ConfigField
from storage.mcp import mcp_store


def _manifest(tmp_path: Path, name: str = "cfg-mcp", runtime: str = "node") -> mcp_registry.McpManifest:
    mcp_dir = tmp_path / name
    mcp_dir.mkdir(parents=True, exist_ok=True)
    return mcp_registry.McpManifest(
        name=name, label=name, description="", version="1.0.0", category="community",
        server=mcp_registry.ServerConfig(runtime=runtime, transport="stdio", command="node",
                                         args=["server.js"], source="npm:x@1.0.0"),
        credentials=mcp_registry.CredentialConfig(type="none"),
        config=[
            ConfigField(key="EXPOSED", label="Exposed tools", default="read-only"),
            ConfigField(key="ALLOWED_IDS", label="Allowed ids", default=""),
            ConfigField(key="SAFE", label="Safe", default=True),       # a JSON bool
            ConfigField(key="PORT", label="Port", default=465),        # a JSON number
            ConfigField(key="_HIDDEN", label="internal", default="x"),
        ],
        env={"EXPOSED": "all"}, agent_env={}, exclude_from=[], skills=[], mcp_dir=mcp_dir,
    )


@pytest.fixture
def saved(monkeypatch):
    values: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(mcp_store, "get_mcp_config_values", lambda n: dict(values.get(n, {})))
    return values


class TestEffectiveConfig:
    def test_unsaved_rows_take_their_default(self, tmp_path, saved):
        assert mcp_registry.effective_config(_manifest(tmp_path)) == {
            "EXPOSED": "read-only", "SAFE": "true", "PORT": "465"}

    def test_a_saved_value_wins_an_empty_one_too(self, tmp_path, saved):
        saved["cfg-mcp"] = {"EXPOSED": "all", "SAFE": "", "_hosted_service_mode": "hosted",
                            "EXTRA": "kept"}
        assert mcp_registry.effective_config(_manifest(tmp_path)) == {
            "EXPOSED": "all", "SAFE": "", "PORT": "465", "EXTRA": "kept"}

    def test_no_manifest_no_config(self):
        assert mcp_registry.effective_config(None) == {}


def test_the_stdio_entry_runs_with_the_default(tmp_path, saved):
    env = mcp_registry.resolve_server_config(_manifest(tmp_path), "agent")["env"]
    # The config row wins over the manifest env, as a saved value always did.
    assert env["EXPOSED"] == "read-only"
    assert env["SAFE"] == "true" and env["PORT"] == "465"
    assert "ALLOWED_IDS" not in env and "_HIDDEN" not in env
    saved["cfg-mcp"] = {"EXPOSED": ""}
    env = mcp_registry.resolve_server_config(_manifest(tmp_path), "agent")["env"]
    assert env["EXPOSED"] == ""


def test_the_docker_env_carries_the_config_quoted(tmp_path, saved, monkeypatch):
    m = _manifest(tmp_path, runtime="docker")
    monkeypatch.setattr(mcp_registry, "get_tool_filter", lambda n: None)
    saved["cfg-mcp"] = {"ALLOWED_IDS": "1,2 $HOME", "BROKEN": "a\nb"}
    docker_manager._inject_mcp_env(m)
    lines = dict(line.split("=", 1) for line in (m.mcp_dir / ".env").read_text().splitlines())
    assert lines["EXPOSED"] == "'read-only'"           # the config row over the manifest env
    assert lines["ALLOWED_IDS"] == "'1,2 $HOME'"
    assert lines["SAFE"] == "'true'"
    assert "BROKEN" not in lines and "_HIDDEN" not in lines


def test_the_per_user_resolver_layers_person_over_admin_over_default(tmp_path, saved, monkeypatch):
    from services.oauth import credential_resolver
    from storage.identity import credential_store
    m = _manifest(tmp_path)
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda n: m if n == m.name else None)
    monkeypatch.setattr(credential_resolver, "pick_account",
                        lambda mcp, agent, user_sub=None: credential_resolver.AccountRef(
                            label="default", owner_sub="u1"))
    monkeypatch.setattr(credential_store, "get_user_credentials",
                        lambda sub, mcp, label: {"TOKEN": "t", "PORT": "587"})
    saved["cfg-mcp"] = {"SAFE": "false"}
    env = credential_resolver._resolve_per_user(
        "cfg-mcp", {"fields": [{"key": "TOKEN"}]}, "u1", "user", agent_name="agent")
    assert env == {"EXPOSED": "read-only", "SAFE": "false", "PORT": "587", "TOKEN": "t"}


def test_a_config_template_reads_the_value_in_effect(tmp_path, saved, monkeypatch):
    from services.mcp import mcp_templating
    other = _manifest(tmp_path, name="other")
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda n: other if n == "other" else None)
    me = _manifest(tmp_path, name="me")
    assert mcp_templating._resolve_template("${config:other:EXPOSED}", me) == "read-only"
    saved["other"] = {"EXPOSED": "all"}
    assert mcp_templating._resolve_template("${config:other:EXPOSED}", me) == "all"


def test_the_network_target_ignores_a_default(tmp_path, saved, monkeypatch):
    """The carve opens the session sandbox's egress to the host: an admin
    types it before the LAN opens."""
    m = _manifest(tmp_path)
    m.config.append(ConfigField(key="HOST_URL", label="Host", default="http://192.168.1.10:8123"))
    m.network_targets = [NS(source="config", host_key="HOST_URL", port_key="", port_default=None)]
    assert mcp_registry.enumerate_mcp_network_targets(m, "agent") == []
    saved["cfg-mcp"] = {"HOST_URL": "http://192.168.1.20:8123"}
    assert mcp_registry.enumerate_mcp_network_targets(m, "agent") == [("192.168.1.20", 8123)]


def test_an_unencodable_config_value_drops_the_old_line(tmp_path, saved, monkeypatch):
    m = _manifest(tmp_path, runtime="docker")
    monkeypatch.setattr(mcp_registry, "get_tool_filter", lambda n: None)
    saved["cfg-mcp"] = {"ALLOWED_IDS": "1,2"}
    docker_manager._inject_mcp_env(m)
    saved["cfg-mcp"] = {"ALLOWED_IDS": "1\n2"}
    docker_manager._inject_mcp_env(m)
    lines = dict(line.split("=", 1) for line in (m.mcp_dir / ".env").read_text().splitlines())
    assert "ALLOWED_IDS" not in lines
