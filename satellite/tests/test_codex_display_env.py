"""DISPLAY injection into codex MCP env tables (sat 0.5.99).

Codex spawns MCP children from the config.toml env tables ONLY, so on Linux
a GUI-needing MCP (browser-control) lost the satellite's DISPLAY and the
headed browser daemon could never launch from a codex session ("-32000
browser unavailable" — found live 2026-07-19; claude sessions were fine
because CLI children inherit the full env).
"""

from satellite.sessions.codex_session import _inject_display_env_toml

TOML = (
    '[mcp_servers.browser-control]\n'
    'command = "node"\n'
    'env = { "OTO_SESSION_ID" = "s1" }\n'
    '\n'
    '[mcp_servers.other]\n'
    'command = "python3"\n'
    'env = { }\n'
)


def test_appends_display_to_every_env_block(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.delenv("XAUTHORITY", raising=False)
    out = _inject_display_env_toml(TOML)
    assert out.count('"DISPLAY" = ":99"') == 2
    assert '"OTO_SESSION_ID" = "s1", "DISPLAY" = ":99"' in out


def test_includes_xauthority_when_set(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/home/u/.Xauthority")
    out = _inject_display_env_toml(TOML)
    assert '"XAUTHORITY" = "/home/u/.Xauthority"' in out


def test_noop_without_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _inject_display_env_toml(TOML) == TOML


def test_skips_blocks_that_declare_their_own(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    toml = 'env = { "DISPLAY" = ":7" }\n'
    assert _inject_display_env_toml(toml) == toml


def test_result_stays_valid_toml(monkeypatch):
    from satellite.tests._toml import toml_loads
    monkeypatch.setenv("DISPLAY", ":99")
    toml_loads(_inject_display_env_toml(TOML))


def test_a_value_with_braces_keeps_the_table_whole(monkeypatch):
    # The path marker is a JSON list of objects: its '}' are inside the
    # string, and DISPLAY belongs after the table's last pair, not inside
    # the value (a media MCP's session died at the config check before).
    import json

    from satellite.tests._toml import toml_loads
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.delenv("XAUTHORITY", raising=False)
    marker = json.dumps([{"tool": "edit_image", "json_path": "image_path", "mode": "read"}])
    escaped = marker.replace("\\", "\\\\").replace('"', '\\"')
    toml = (
        '[mcp_servers.image-gen-mcp]\ncommand = "python3"\n'
        f'env = {{ "OTO_TOOL_ARG_PATHS" = "{escaped}", "A" = "b" }}\n'
    )
    env = toml_loads(_inject_display_env_toml(toml))["mcp_servers"]["image-gen-mcp"]["env"]
    assert json.loads(env["OTO_TOOL_ARG_PATHS"]) == json.loads(marker)
    assert env["A"] == "b" and env["DISPLAY"] == ":99"
