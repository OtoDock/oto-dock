"""Manifest ``server.tool_timeout_sec`` — the per-call limit Codex gives a
server's tools (its own default is 60 s): read as whole seconds within an
hour, dropped with a log otherwise, carried on the server entry in the TOML
format only, written into the server's own table."""

from __future__ import annotations

import json
import logging

import pytest

from services.mcp import mcp_registry
from services.mcp.mcp_manifest_parse import _parse_manifest


def _write(tmp_path, server_extra: dict, name: str = "x-mcp"):
    d = tmp_path / name
    d.mkdir(parents=True)
    data = {
        "name": name, "label": "X", "description": "X tools.", "version": "1.0.0",
        "category": "community",
        "server": {"runtime": "python", "transport": "stdio", "command": "python",
                   "args": ["s.py"], **server_extra},
        "credentials": {"type": "none"},
    }
    p = d / "manifest.json"
    p.write_text(json.dumps(data))
    return p


def test_absent_leaves_codex_its_default(tmp_path):
    assert _parse_manifest(_write(tmp_path, {})).server.tool_timeout_sec == 0


@pytest.mark.parametrize("value", [1, 600, 3600])
def test_whole_seconds_within_an_hour_are_read(tmp_path, value):
    assert _parse_manifest(_write(tmp_path, {"tool_timeout_sec": value})).server.tool_timeout_sec == value


@pytest.mark.parametrize("value", [0, -5, 3601, "600", 60.0, True, [600], {"s": 600}])
def test_a_malformed_value_is_dropped_with_a_log_naming_the_key(tmp_path, value, caplog):
    with caplog.at_level(logging.WARNING, logger="otodock"):
        m = _parse_manifest(_write(tmp_path, {"tool_timeout_sec": value}))
    assert m is not None and m.server.tool_timeout_sec == 0
    assert any("x-mcp: server.tool_timeout_sec" in r.getMessage() for r in caplog.records)


def test_the_entry_carries_the_limit_in_the_toml_format_only(tmp_path):
    m = _parse_manifest(_write(tmp_path, {"tool_timeout_sec": 600}))
    toml_entry = mcp_registry.resolve_server_config(m, "agent", mcp_config_format="toml", session_ctx={})
    assert toml_entry["tool_timeout_sec"] == 600
    json_entry = mcp_registry.resolve_server_config(m, "agent", mcp_config_format="json", session_ctx={})
    assert "tool_timeout_sec" not in json_entry
    plain = _parse_manifest(_write(tmp_path, {}, name="plain-mcp"))
    assert "tool_timeout_sec" not in mcp_registry.resolve_server_config(
        plain, "agent", mcp_config_format="toml", session_ctx={})


def test_the_bundled_long_running_servers_declare_ten_minutes():
    from tests._paths import CUSTOM_MCPS
    declared = {
        p.parent.name: json.loads(p.read_text()).get("server", {}).get("tool_timeout_sec")
        for p in CUSTOM_MCPS.glob("*/manifest.json")
    }
    long_running = {"image-gen-mcp", "video-gen-mcp", "music-gen-mcp", "tts-mcp", "transcribe-mcp",
                    "file-tools-mcp", "browser-mcp", "agent-creator-mcp", "mcps-mcp"}
    assert {n for n, v in declared.items() if v} == long_running
    assert {declared[n] for n in long_running} == {600}
