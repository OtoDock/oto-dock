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
    # Codex 0.152.0 made update_plan opt-in: every session keeps the plan
    # tool (the dashboard TODO checklist rides turn/plan/updated).
    assert cfg["tools"]["update_plan"]["enabled"] is True
    for kwargs in ({"interactive": True}, {"client_type": "task"},
                   {"local_endpoint": "http://127.0.0.1:11434/v1"}):
        assert _written_config(tmp_path, **kwargs)["tools"]["update_plan"]["enabled"] is True


def test_config_toml_question_flag_dashboard_and_interactive(tmp_path):
    # request_user_input is exposed to interactive-USER sessions: the bare TUI
    # AND the headless -p dashboard (which now HOLDS the request and surfaces a
    # question card). OFF for autonomous runs (task/phone/meeting) and for a
    # session with no client_type — nobody answers.
    headless_no_client = _written_config(tmp_path)
    assert "default_mode_request_user_input" not in headless_no_client["features"]

    headless_dashboard = _written_config(tmp_path, client_type="dashboard")
    assert headless_dashboard["features"]["default_mode_request_user_input"] is True
    # The headless dashboard chat runs the hook too (its gate judges the
    # Don't Ask and auto turns).
    assert headless_dashboard["features"]["hooks"] is True

    headless_task = _written_config(tmp_path, client_type="task")
    assert "default_mode_request_user_input" not in headless_task["features"]

    interactive = _written_config(tmp_path, interactive=True)
    assert interactive["features"]["default_mode_request_user_input"] is True
    assert interactive["features"]["hooks"] is True


def test_config_toml_interactive_puts_codex_worktrees_beside_the_config_dir(tmp_path):
    # Codex 0.162's managed worktrees (TUI tools) default to $CODEX_HOME/
    # worktrees, the protected .codex dir; the root moves to a hidden sibling
    # at the scope root, with the SANDBOX path Codex reads inside bwrap. The
    # tools exist in the TUI only, so a headless config carries no table.
    cfg = _written_config(tmp_path, interactive=True, trusted_cwd="/users/alice",
                          sandbox_codex_dir="/users/alice/.codex")
    assert cfg["desktop"]["git-worktree-root"] == "/users/alice/.codex-worktrees"
    assert cfg["projects"]["/users/alice"]["trust_level"] == "trusted"
    cfg = _written_config(tmp_path, interactive=True, sandbox_codex_dir="/workspace/.codex/")
    assert cfg["desktop"]["git-worktree-root"] == "/workspace/.codex-worktrees"
    for kwargs in ({"client_type": "dashboard", "sandbox_codex_dir": "/workspace/.codex"},
                   {"interactive": True}):
        assert "desktop" not in _written_config(tmp_path, **kwargs)


def test_config_toml_local_endpoint_wire_api_responses(tmp_path):
    # codex-rs removed wire_api="chat" (deserializing it is a hard serde error
    # since <=0.144.1) — a "chat" value makes the daemon reject the entire
    # config.toml, breaking every local-endpoint session. Pin "responses".
    cfg = _written_config(tmp_path, local_endpoint="http://127.0.0.1:11434/v1")
    prov = cfg["model_providers"]["oto_local"]
    assert prov["base_url"] == "http://127.0.0.1:11434/v1"
    assert prov["wire_api"] == "responses"
    # Keyless endpoint → NO env_key: codex-rs refuses to start a provider whose
    # env_key names an unset variable.
    assert "env_key" not in prov


def test_config_toml_local_endpoint_raises_the_stream_idle_timeout(tmp_path):
    # Codex's 300000 ms default (counted to the FIRST event) killed a local
    # model's first turn mid-prefill; every local endpoint gets the long budget,
    # a hosted session keeps Codex's default.
    from core.layers.codex.helpers import LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS
    cfg = _written_config(tmp_path, local_endpoint="http://127.0.0.1:11434/v1")
    assert cfg["model_providers"]["oto_local"]["stream_idle_timeout_ms"] == LOCAL_ENDPOINT_STREAM_IDLE_TIMEOUT_MS
    assert "model_providers" not in _written_config(tmp_path)


def test_config_toml_local_catalog_written_with_the_sandbox_path(tmp_path):
    import json
    from core.layers.codex.local_model_catalog import local_model_catalog_json
    catalog = local_model_catalog_json("qwen3.6-35b-a3b", "ollama", [("qwen3.6-35b-a3b", 131_072)])
    text_cfg = _written_config(
        tmp_path, local_endpoint="http://127.0.0.1:11434/v1",
        local_catalog_json=catalog, sandbox_codex_dir="/users/alice/.codex",
    )
    # Root key (parsed at root, above every table) with the path Codex sees
    # INSIDE the sandbox, not the host tmp_path.
    assert text_cfg["model_catalog_json"] == "/users/alice/.codex/models.json"
    raw = (tmp_path / "config.toml").read_text()
    assert raw.index("model_catalog_json") < raw.index("[memories]")
    # The file itself lands next to config.toml, owner-only, as generated.
    models = tmp_path / "models.json"
    assert json.loads(models.read_text())["models"][0]["slug"] == "qwen3.6-35b-a3b"
    assert models.stat().st_mode & 0o777 == 0o600


def test_config_toml_without_catalog_has_no_key_and_drops_a_stale_file(tmp_path):
    from core.layers.codex.local_model_catalog import local_model_catalog_json
    stale = tmp_path / "models.json"
    stale.write_text('{"models": []}')
    # openai_compatible → no catalog (the builder returns ""), key absent,
    # the previous session's file gone.
    cfg = _written_config(
        tmp_path, local_endpoint="http://127.0.0.1:8080/v1",
        local_catalog_json=local_model_catalog_json("m", "openai_compatible"),
        sandbox_codex_dir="/workspace/.codex",
    )
    assert "model_catalog_json" not in cfg
    assert not stale.exists()
    # A hosted session never gets one either, even if a caller passes text.
    stale.write_text('{"models": []}')
    cfg = _written_config(
        tmp_path, local_catalog_json=local_model_catalog_json("m", "ollama"),
        sandbox_codex_dir="/workspace/.codex",
    )
    assert "model_catalog_json" not in cfg
    assert "model_provider" not in cfg
    assert not stale.exists()


def test_config_toml_local_endpoint_env_key_only_when_keyed(tmp_path):
    from core.layers.codex.layer import _LOCAL_ENDPOINT_KEY_ENV
    cfg = _written_config(
        tmp_path, local_endpoint="http://192.168.1.8:8080/v1", local_endpoint_keyed=True,
    )
    assert cfg["model_providers"]["oto_local"]["env_key"] == _LOCAL_ENDPOINT_KEY_ENV
    assert _LOCAL_ENDPOINT_KEY_ENV != "CODEX_API_KEY"


def test_config_toml_is_owner_only(tmp_path):
    # config.toml can carry an inline MCP bearer — must be locked 0600 like
    # auth.json, not world/group readable.
    _Layer._write_config_toml(
        tmp_path, "prompt",
        mcp_toml='[mcp_servers.x.http_headers]\n"Authorization" = "Bearer secret"',
    )
    mode = (tmp_path / "config.toml").stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_agents_md_gets_the_tools_note_only_on_a_local_endpoint(tmp_path):
    # Codex sends MCP tools as Responses-API namespace tools that only some
    # local servers unpack (and no tool_search on a custom provider); the note
    # tells the local model the truth for its server, hosted models need
    # nothing. An openai_compatible endpoint gets the server list.
    from core.layers.codex.helpers import LOCAL_PROVIDER_TOOL_NOTE
    _Layer._write_config_toml(
        tmp_path, "You are X.", local_endpoint="http://127.0.0.1:8080/v1",
        local_provider="openai_compatible",
    )
    text = (tmp_path / "AGENTS.md").read_text()
    assert text.startswith("You are X.")
    assert text.endswith(LOCAL_PROVIDER_TOOL_NOTE + "\n")
    assert text.count("# Tools on this model provider") == 1
    assert "llama.cpp and LM Studio drop it" in text
    assert "tool_search" not in LOCAL_PROVIDER_TOOL_NOTE  # no such function there

    hosted = tmp_path / "hosted"
    _Layer._write_config_toml(hosted, "You are X.")
    assert (hosted / "AGENTS.md").read_text() == "You are X."


def test_agents_md_note_follows_the_provider(tmp_path):
    # Ollama 0.34+ serves Codex's MCP tools natively: the note expects them and
    # blames an older server when they are missing. Unknown provider → the
    # generic note.
    from core.layers.codex.helpers import (
        LOCAL_PROVIDER_TOOL_NOTE, OLLAMA_PROVIDER_TOOL_NOTE, local_provider_note,
    )
    _Layer._write_config_toml(
        tmp_path, "You are X.", local_endpoint="http://127.0.0.1:11434/v1",
        local_provider="ollama",
    )
    text = (tmp_path / "AGENTS.md").read_text()
    assert text.endswith(OLLAMA_PROVIDER_TOOL_NOTE + "\n")
    assert "older than 0.34" in text
    assert local_provider_note("") == LOCAL_PROVIDER_TOOL_NOTE
    assert local_provider_note("Ollama") == OLLAMA_PROVIDER_TOOL_NOTE
    for note in (LOCAL_PROVIDER_TOOL_NOTE, OLLAMA_PROVIDER_TOOL_NOTE):
        assert note.startswith("# Tools on this model provider")
        assert "Never work around a missing tool" in note


def test_with_local_provider_note_is_idempotent_and_handles_empty():
    from core.layers.codex.helpers import (
        LOCAL_PROVIDER_TOOL_NOTE, OLLAMA_PROVIDER_TOOL_NOTE, with_local_provider_note,
    )
    once = with_local_provider_note("Persona.")
    assert once == "Persona.\n\n" + LOCAL_PROVIDER_TOOL_NOTE + "\n"
    assert with_local_provider_note(once) == once
    assert with_local_provider_note(once, "ollama") == once  # one note per prompt
    assert with_local_provider_note("") == LOCAL_PROVIDER_TOOL_NOTE + "\n"
    assert with_local_provider_note("P.", "ollama") == "P.\n\n" + OLLAMA_PROVIDER_TOOL_NOTE + "\n"


def test_config_toml_hook_floor_and_shell_knobs(tmp_path):
    # Every session runs the PreToolUse permission floor: the app-server's
    # trust rides thread/start.config (a dashboard chat's gate judges the
    # turns the bridge does not ask in); the TUI keeps its flag.
    for client_type in ("task", "phone", "meeting", "trigger", "internal", "dashboard", ""):
        assert _written_config(tmp_path, client_type=client_type)["features"]["hooks"] is True
    assert _written_config(tmp_path, interactive=True)["features"]["hooks"] is True
    # An external caller loses the shell tool; every other session keeps it.
    external = _written_config(tmp_path, client_type="phone", no_shell=True)["features"]
    assert external["shell_tool"] is False and external["hooks"] is True
    assert "shell_tool" not in _written_config(tmp_path, client_type="phone")["features"]


def test_session_without_oauth_drops_a_stale_auth_json(tmp_path):
    # An API-key / local-endpoint session must not load a previous ChatGPT
    # session's auth.json from the persistent CODEX_HOME (Codex loops on
    # refreshing the stale, neutralized token).
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    _Layer._drop_stale_auth_json(tmp_path)
    assert not (tmp_path / "auth.json").exists()
    _Layer._drop_stale_auth_json(tmp_path)              # already gone → no error
    _Layer._drop_stale_auth_json(tmp_path / "absent")   # no dir → no error
