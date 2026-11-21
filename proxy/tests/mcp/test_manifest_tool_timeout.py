"""Manifest ``server.tool_timeout_sec``: the per-call limit a server's tools
ask for, read as whole seconds within an hour, dropped with a log otherwise,
carried on the server entry in every format and written as each engine's own
key: Codex's ``tool_timeout_sec`` (its own default is 60 s), Claude Code's
per-server ``timeout`` in milliseconds where it raises the engine's own limit
on a silent call (30 minutes over stdio, 5 minutes over HTTP)."""

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


def test_the_entry_carries_the_limit_in_every_format(tmp_path):
    m = _parse_manifest(_write(tmp_path, {"tool_timeout_sec": 600}))
    for fmt in ("toml", "json", None):
        entry = mcp_registry.resolve_server_config(m, "agent", mcp_config_format=fmt, session_ctx={})
        assert entry["tool_timeout_sec"] == 600
    plain = _parse_manifest(_write(tmp_path, {}, name="plain-mcp"))
    for fmt in ("toml", "json", None):
        assert "tool_timeout_sec" not in mcp_registry.resolve_server_config(
            plain, "agent", mcp_config_format=fmt, session_ctx={})


# --- the bound each engine's config carries ----------------------------------

_STANDIN = {"OTO_TOOL_STANDIN": "{}"}
_HTTP = {"type": "http", "url": "https://mcp.example.com/mcp/"}
_STDIO = {"type": "stdio", "command": "x", "args": []}


def _docker(monkeypatch, name: str = "video-tools") -> dict:
    """A Docker tool's entry as the build writes it: the gateway's address."""
    import config
    from services.mcp import mcp_session_config
    monkeypatch.setattr(mcp_session_config, "_is_container_entry", lambda key, entry: key == name)
    return {"type": "http", "url": f"http://127.0.0.1:{config.PORT}/v1/mcp-gateway/{name}/mcp/"}


def test_a_calls_bound_is_its_limit_plus_the_start_it_carries(monkeypatch):
    from services.containers import door
    from services.mcp.mcp_session_config import call_limit_s
    docker = _docker(monkeypatch)
    assert call_limit_s("vendor", {**_HTTP, "tool_timeout_sec": 600}, 60) == 600
    assert call_limit_s("x", {**_STDIO, "env": _STANDIN, "tool_timeout_sec": 600}, 60) == 660
    assert call_limit_s("video-tools", {**docker, "tool_timeout_sec": 3600}, 60) == 3600 + int(door.WAKE_BUDGET_S)
    # A start with no limit named: the engine's default plus the start.
    assert call_limit_s("x", {**_STDIO, "env": _STANDIN}, 60) == 120
    # Nothing named and nothing starting: the engine keeps its own.
    assert call_limit_s("vendor", dict(_HTTP), 60) == 0
    # The wake's budget is the door's own, read at the call.
    monkeypatch.setattr(door, "WAKE_BUDGET_S", 45.0)
    assert call_limit_s("video-tools", {**docker, "tool_timeout_sec": 600}, 60) == 645


@pytest.mark.parametrize("entry, timeout_ms", [
    # Over HTTP Claude Code allows 5 silent minutes: a longer limit is written.
    ({**_HTTP, "tool_timeout_sec": 600}, 600_000),
    ({**_HTTP, "type": "sse", "tool_timeout_sec": 900}, 900_000),
    # A shorter one is not: the engine's own 5 minutes are already longer.
    ({**_HTTP, "tool_timeout_sec": 120}, None),
    # Over stdio it allows 30: a ten-minute tool keeps the engine's own.
    ({**_STDIO, "env": _STANDIN, "tool_timeout_sec": 600}, None),
    ({"command": "x", "tool_timeout_sec": 600}, None),
    ({**_STDIO, "env": _STANDIN, "tool_timeout_sec": 3600}, 3_660_000),
    # A tool that names no limit keeps the engine's own on every transport.
    (dict(_HTTP), None),
    ({**_STDIO, "env": _STANDIN}, None),
])
def test_claude_code_gets_a_limit_only_where_it_raises_its_own(entry, timeout_ms):
    from services.mcp.mcp_session_config import claude_entry
    written = claude_entry("x", entry)
    assert "tool_timeout_sec" not in written
    assert written.get("timeout") == timeout_ms
    assert {k: v for k, v in written.items() if k != "timeout"} == {
        k: v for k, v in entry.items() if k != "tool_timeout_sec"}


def test_a_docker_tool_gets_its_limit_and_the_wake_on_claude_code(monkeypatch):
    from services.containers import door
    from services.mcp.mcp_session_config import claude_entry
    docker = _docker(monkeypatch, "file-tools")
    assert claude_entry("file-tools", {**docker, "tool_timeout_sec": 600})["timeout"] == (
        (600 + int(door.WAKE_BUDGET_S)) * 1000)
    # One that names none keeps the engine's own five minutes, its wake inside them.
    assert "timeout" not in claude_entry("file-tools", dict(docker))


def test_the_json_file_carries_each_engines_own_key(tmp_path, monkeypatch):
    """Claude Code's file gets ``timeout`` and never the manifest's field; the
    file an in-process Direct LLM session reads gets neither. The build's own
    entries keep the field (the TOML regeneration reads it)."""
    import config
    from services.mcp import mcp_session_config
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    docker = _docker(monkeypatch, "file-tools")
    servers = {"file-tools": {**docker, "tool_timeout_sec": 600},
               "media": {**_STDIO, "env": dict(_STANDIN), "tool_timeout_sec": 600},
               "plain": dict(_HTTP)}
    claude = json.loads(mcp_session_config._write_session_mcp_config(
        servers, "agent", "sub", "session-claude", "json").read_text())["mcpServers"]
    assert claude["file-tools"]["timeout"] == 660_000
    assert "timeout" not in claude["media"] and "timeout" not in claude["plain"]
    assert not any("tool_timeout_sec" in e for e in claude.values())
    direct = json.loads(mcp_session_config._write_session_mcp_config(
        servers, "agent", "sub", "session-direct", None).read_text())["mcpServers"]
    assert not any({"timeout", "tool_timeout_sec"} & set(e) for e in direct.values())
    assert servers["file-tools"]["tool_timeout_sec"] == 600 and "timeout" not in servers["file-tools"]


def _claude_silence_s(written: dict) -> float:
    """How long Claude Code lets a call of this written entry stay silent: its
    per-server ``timeout``, else its own limit for the transport."""
    if "timeout" in written:
        return written["timeout"] / 1000
    return 1800 if written.get("type", "stdio") == "stdio" else 300


def test_every_bundled_limit_reaches_every_engine(monkeypatch):
    """A tool's declared limit is honoured by each engine's config, whatever
    its transport: Codex's ``tool_timeout_sec`` and Claude Code's silence."""
    from services.mcp.mcp_session_config import (
        _CODEX_TOOL_TIMEOUT_S,
        call_limit_s,
        claude_entry,
    )
    from tests._paths import CUSTOM_MCPS
    monkeypatch.setattr("services.mcp.mcp_session_config._is_container_entry", lambda key, entry: False)
    checked = 0
    for path in sorted(CUSTOM_MCPS.glob("*/manifest.json")):
        m = _parse_manifest(path)
        declared = m.server.tool_timeout_sec if m is not None else 0
        if not declared:
            continue
        key = m.server_name or m.name
        claude = mcp_registry.resolve_server_config(m, "agent", mcp_config_format="json", session_ctx={})
        codex = mcp_registry.resolve_server_config(m, "agent", mcp_config_format="toml", session_ctx={})
        assert _claude_silence_s(claude_entry(key, claude)) >= declared, m.name
        assert call_limit_s(key, codex, _CODEX_TOOL_TIMEOUT_S) >= declared, m.name
        checked += 1
    assert checked == 9


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


# --- server.starts: when a session starts the program ------------------------


def test_starts_defaults_to_the_first_call(tmp_path):
    assert _parse_manifest(_write(tmp_path, {})).server.starts == "first_call"


@pytest.mark.parametrize("value", ["first_call", "with_chat"])
def test_starts_reads_its_two_words(tmp_path, value):
    assert _parse_manifest(_write(tmp_path, {"starts": value})).server.starts == value


@pytest.mark.parametrize("value", ["eager", "", 1, True, ["with_chat"]])
def test_a_bad_starts_is_logged_and_read_as_the_first_call(tmp_path, value, caplog):
    with caplog.at_level(logging.WARNING, logger="otodock"):
        m = _parse_manifest(_write(tmp_path, {"starts": value}))
    assert m is not None and m.server.starts == "first_call"
    assert any("x-mcp: server.starts" in r.getMessage() for r in caplog.records)


def test_with_platform_is_read_only_from_the_platform_s_own_tools(tmp_path, monkeypatch, caplog):
    """A catalog or uploaded package that says ``with_platform`` starts at its
    first call (one line says why); one under ``mcps/custom`` keeps the word."""
    import config
    monkeypatch.setattr(config, "MCPS_DIR", tmp_path)
    with caplog.at_level(logging.WARNING, logger="otodock"):
        m = _parse_manifest(_write(tmp_path / "community", {"starts": "with_platform"}))
    assert m is not None and m.server.starts == "first_call"
    assert any("with_platform is for the platform's own tools" in r.getMessage() for r in caplog.records)
    own = _parse_manifest(_write(tmp_path / "custom", {"starts": "with_platform"}))
    assert own is not None and own.server.starts == "with_platform"


def test_the_bundled_servers_that_list_live_data_start_with_the_chat():
    """agent-config-mcp lists the platform's engines and conversations-mcp the
    session's conversation kind when asked for their tools: a saved answer
    could be stale, and a failed read gives a short list."""
    from tests._paths import CUSTOM_MCPS
    declared = {
        p.parent.name: json.loads(p.read_text()).get("server", {}).get("starts")
        for p in CUSTOM_MCPS.glob("*/manifest.json")
    }
    assert {n for n, v in declared.items() if v == "with_chat"} == {"agent-config-mcp", "conversations-mcp"}
    # The file tools' container runs while the platform runs: the app renderer
    # and the document pane's downloads call it.
    assert {n for n, v in declared.items() if v and v != "with_chat"} == {"file-tools-mcp"}
    assert declared["file-tools-mcp"] == "with_platform"
