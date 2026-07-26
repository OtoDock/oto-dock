"""memory-mcp unit tests — tool exposure rules, tolerant argument coercion,
op-body mapping, and result formatting. The server is a thin relay; the
command semantics themselves are covered by the proxy's test_memory_api.py.

Run with the proxy venv (has mcp + httpx + pytest):
    proxy/venv/bin/python -m pytest mcps/custom/memory-mcp/tests/ -q
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

import server


def _reload_with_env(monkeypatch, **env):
    for k in (
        "OTO_MEMORY_USER_ENABLED",
        "OTO_MEMORY_AGENT_ENABLED", "OTO_USER_SUB", "PROXY_TASK_OWNER",
        "OTO_DEFAULT_SCOPE", "PROXY_TASK_SCOPE", "OTO_SCOPE",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(server)


# ---------------------------------------------------------------------------
# Tool exposure rules
# ---------------------------------------------------------------------------

def test_tool_exposed_with_both_scopes(monkeypatch):
    srv = _reload_with_env(monkeypatch, OTO_USER_SUB="user-1")
    tools = asyncio.run(srv.list_tools())
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "memory"
    assert "/memories/agent/" in t.description
    assert "/memories/user/" in t.description
    cmds = t.inputSchema["properties"]["command"]["enum"]
    assert cmds == ["view", "create", "str_replace", "insert", "delete", "rename"]


def test_no_user_hides_user_scope(monkeypatch):
    srv = _reload_with_env(monkeypatch)  # no USER_SUB
    tools = asyncio.run(srv.list_tools())
    assert len(tools) == 1
    assert "/memories/user/" not in tools[0].description
    assert "Default scope: /memories/agent/" in tools[0].description


def test_both_toggles_off_gets_no_tools(monkeypatch):
    srv = _reload_with_env(
        monkeypatch,
        OTO_MEMORY_USER_ENABLED="false",
        OTO_MEMORY_AGENT_ENABLED="false",
        OTO_USER_SUB="user-1",
    )
    assert asyncio.run(srv.list_tools()) == []


def test_default_scope_env_respected(monkeypatch):
    srv = _reload_with_env(
        monkeypatch, OTO_USER_SUB="user-1", OTO_DEFAULT_SCOPE="user",
    )
    tools = asyncio.run(srv.list_tools())
    assert "Default scope: /memories/user/" in tools[0].description


def test_disabled_default_falls_back_to_available(monkeypatch):
    srv = _reload_with_env(
        monkeypatch, OTO_USER_SUB="user-1", OTO_DEFAULT_SCOPE="user",
        OTO_MEMORY_USER_ENABLED="false",
    )
    tools = asyncio.run(srv.list_tools())
    assert "Default scope: /memories/agent/" in tools[0].description


# ---------------------------------------------------------------------------
# Tolerant coercion (deferred-schema fragility: agents may pass JSON strings)
# ---------------------------------------------------------------------------

def test_view_range_string_coerced():
    assert server._coerce_view_range("[2, 5]") == [2, 5]
    assert server._coerce_view_range([2, 5]) == [2, 5]
    assert server._coerce_view_range(["2", "5"]) == [2, 5]
    assert server._coerce_view_range("garbage") is None
    assert server._coerce_view_range(None) is None


def test_insert_line_string_coerced():
    assert server._coerce_int("3") == 3
    assert server._coerce_int(3) == 3
    assert server._coerce_int(" 7 ") == 7
    assert server._coerce_int("x") is None
    assert server._coerce_int(True) is None


# ---------------------------------------------------------------------------
# Op-body mapping
# ---------------------------------------------------------------------------

def test_build_op_body_view_with_range():
    body = server.build_op_body("memory", {
        "command": "view", "path": "/memories/agent/a.md",
        "view_range": "[1, 4]",
    })
    assert body == {
        "command": "view", "path": "/memories/agent/a.md",
        "view_range": [1, 4],
    }


def test_build_op_body_create():
    body = server.build_op_body("memory", {
        "command": "create", "path": "/memories/user/p.md", "file_text": "# X\n",
    })
    assert body["file_text"] == "# X\n"


def test_build_op_body_rename_falls_back_to_path():
    """Models sometimes pass `path` instead of `old_path` for rename."""
    body = server.build_op_body("memory", {
        "command": "rename", "path": "/memories/agent/a.md",
        "new_path": "/memories/agent/b.md",
    })
    assert body["old_path"] == "/memories/agent/a.md"
    assert body["new_path"] == "/memories/agent/b.md"
    assert "path" not in body


def test_build_op_body_str_replace_accepts_edit_tool_aliases():
    """Models habitually pass the built-in Edit tool's old_string/new_string."""
    body = server.build_op_body("memory", {
        "command": "str_replace", "path": "/memories/agent/a.md",
        "old_string": "was", "new_string": "now",
    })
    assert body["old_str"] == "was"
    assert body["new_str"] == "now"


def test_build_op_body_str_replace_canonical_names_win():
    body = server.build_op_body("memory", {
        "command": "str_replace", "path": "/memories/agent/a.md",
        "old_str": "canonical", "old_string": "alias", "new_str": "x",
    })
    assert body["old_str"] == "canonical"


def test_build_op_body_create_accepts_write_tool_content_alias():
    body = server.build_op_body("memory", {
        "command": "create", "path": "/memories/user/p.md", "content": "# X\n",
    })
    assert body["file_text"] == "# X\n"


def test_build_op_body_insert_coerces_line():
    body = server.build_op_body("memory", {
        "command": "insert", "path": "/memories/agent/a.md",
        "insert_line": "2", "insert_text": "x",
    })
    assert body["insert_line"] == 2


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def test_format_result_verbatim_output():
    out = server.format_result({
        "output": "File created successfully at: a.md",
        "is_error": False, "warnings": [],
    })
    assert out == "File created successfully at: a.md"


def test_format_result_appends_warnings():
    out = server.format_result({
        "output": "The memory file has been edited.",
        "is_error": False,
        "warnings": ["WARN: a.md is big"],
    })
    assert out.endswith("\nWARN: a.md is big")


def test_format_result_error_output_verbatim():
    out = server.format_result({
        "output": "Error: File a.md already exists",
        "is_error": True, "warnings": [],
    })
    assert out == "Error: File a.md already exists"


# ---------------------------------------------------------------------------
# Command normalization (near-miss command names)
# ---------------------------------------------------------------------------

def test_normalize_command_editor_variant_maps_to_str_replace():
    assert server._normalize_command(
        {"command": "str_replace_based_edit_20250124"}) == "str_replace"


def test_normalize_command_destructive_needs_exact_token():
    assert server._normalize_command({"command": "remove_topic"}) == "delete"
    assert server._normalize_command({"command": "move_topic"}) == "rename"
    # Substring matches must NOT trigger: "reformat" contains "rm",
    # "truncate" contains "cat" — both pass through for the proxy's
    # self-correcting error instead of becoming delete/view.
    assert server._normalize_command(
        {"command": "reformat_memory"}) == "reformat_memory"
    assert server._normalize_command({"command": "truncate"}) == "truncate"


def test_normalize_command_infers_view_from_path_only():
    assert server._normalize_command({"path": "/memories/agent"}) == "view"
    # A write payload means the intent was NOT a read — don't guess.
    assert server._normalize_command({"path": "x.md", "file_text": "y"}) is None


def test_normalize_path_view_root_sentinels():
    assert server._normalize_path("view", None) == "/memories"
    assert server._normalize_path("view", "") == "/memories"
    assert server._normalize_path("view", "/") == "/memories"
    # Regression: these already work server-side — keep passing them through.
    assert server._normalize_path("view", "memories") == "memories"
    assert server._normalize_path("view", "/memories/") == "/memories/"


def test_normalize_path_bare_filename_view_uses_default_scope(monkeypatch):
    srv = _reload_with_env(
        monkeypatch, OTO_USER_SUB="u1", OTO_DEFAULT_SCOPE="user")
    assert srv._normalize_path("view", "prefs.md") == "/memories/user/prefs.md"


def test_bare_filename_write_refused_names_both_scopes(monkeypatch):
    # Agent-only default: the fixed scope order would land a personal note in
    # SHARED agent memory — the refusal must name both scopes so the model
    # can self-correct with a full path.
    srv = _reload_with_env(monkeypatch)
    with pytest.raises(srv.ToolArgError) as ei:
        srv.build_op_body("memory", {
            "command": "create", "path": "n.md", "file_text": "x",
        })
    msg = str(ei.value)
    assert "/memories/agent/n.md" in msg
    assert "/memories/user/n.md" in msg


# ---------------------------------------------------------------------------
# call_tool dispatch (validate_input=False tolerance)
# ---------------------------------------------------------------------------

def _call(monkeypatch, arguments, srv=None):
    srv = srv or _reload_with_env(monkeypatch, OTO_USER_SUB="u1")

    async def fake_post(body):
        fake_post.body = body
        return {"output": "ok", "is_error": False, "warnings": []}

    monkeypatch.setattr(srv, "_post_op", fake_post)
    result = asyncio.run(srv.call_tool("memory", arguments))
    return result[0].text, getattr(fake_post, "body", None)


def test_call_tool_accepts_json_string_arguments(monkeypatch):
    text, body = _call(monkeypatch, '{"command": "view", "path": "/memories"}')
    assert text == "ok"
    assert body["command"] == "view"


def test_call_tool_junk_arguments_ask_for_command(monkeypatch):
    text, body = _call(monkeypatch, "///not json")
    assert text == "command required"
    assert body is None


def test_call_tool_missing_command_with_path_infers_view(monkeypatch):
    text, body = _call(monkeypatch, {"path": "/memories/agent"})
    assert text == "ok"
    assert body["command"] == "view"


def test_call_tool_bare_filename_write_refusal(monkeypatch):
    text, body = _call(monkeypatch, {
        "command": "create", "path": "n.md", "file_text": "x",
    })
    assert "/memories/agent/n.md" in text
    assert "/memories/user/n.md" in text
    assert body is None
