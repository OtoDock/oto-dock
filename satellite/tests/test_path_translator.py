"""Tests for satellite-side sandbox-path translation.

The proxy emits sandbox-style virtual paths in MCP env vars (the same paths
local-bwrap-sandboxed agents see). The satellite has no bwrap, so it
rewrites them to its own ``{agent_dir}/...`` paths before subprocess spawn.
"""

from pathlib import Path

import pytest

from satellite.host import path_translator


@pytest.fixture
def agent_dir(tmp_path):
    """A throwaway satellite agent dir."""
    return tmp_path / "agents" / "my-agent"


# ---------------------------------------------------------------------------
# translate_path: workspace
# ---------------------------------------------------------------------------


def test_workspace_user_scoped(agent_dir):
    result = path_translator.translate_path(
        "/users/alice/workspace", agent_dir, username="alice",
    )
    assert result == f"{agent_dir}/users/alice/workspace"


def test_workspace_user_subpath(agent_dir):
    result = path_translator.translate_path(
        "/users/alice/workspace/foo.png", agent_dir, username="alice",
    )
    assert result == f"{agent_dir}/users/alice/workspace/foo.png"


def test_workspace_agent_scoped(agent_dir):
    result = path_translator.translate_path(
        "/workspace", agent_dir, username="",
    )
    assert result == f"{agent_dir}/workspace"


def test_workspace_agent_subpath(agent_dir):
    result = path_translator.translate_path(
        "/workspace/.screenshots/sid-xyz/foo.png", agent_dir, username="",
    )
    assert result == f"{agent_dir}/workspace/.screenshots/sid-xyz/foo.png"


# ---------------------------------------------------------------------------
# translate_path: config dir
# ---------------------------------------------------------------------------


def test_config_user_scoped(agent_dir):
    result = path_translator.translate_path(
        "/config", agent_dir, username="alice",
    )
    assert result == f"{agent_dir}/config"


def test_config_subpath(agent_dir):
    result = path_translator.translate_path(
        "/config/prompt.md", agent_dir, username="alice",
    )
    assert result == f"{agent_dir}/config/prompt.md"


# ---------------------------------------------------------------------------
# translate_path: knowledge dir
# ---------------------------------------------------------------------------


def test_knowledge_user_scoped(agent_dir):
    """/knowledge translates to {agent_dir}/knowledge for user-scope."""
    result = path_translator.translate_path(
        "/knowledge", agent_dir, username="alice",
    )
    assert result == f"{agent_dir}/knowledge"


def test_knowledge_agent_scoped(agent_dir):
    """/knowledge translates the same way for agent-scope sessions
    (knowledge is universal — both user-scope and agent-scope read from
    the same {agent_dir}/knowledge dir on the satellite)."""
    result = path_translator.translate_path(
        "/knowledge", agent_dir, username="",
    )
    assert result == f"{agent_dir}/knowledge"


def test_knowledge_subpath(agent_dir):
    result = path_translator.translate_path(
        "/knowledge/refs/template.md", agent_dir, username="alice",
    )
    assert result == f"{agent_dir}/knowledge/refs/template.md"


def test_knowledge_credentials_subpath(agent_dir):
    """/knowledge/.credentials/google-tokens — used for agent-scope
    OAuth token reads on the satellite."""
    result = path_translator.translate_path(
        "/knowledge/.credentials/google-tokens",
        agent_dir,
        username="",
    )
    assert result == f"{agent_dir}/knowledge/.credentials/google-tokens"


# ---------------------------------------------------------------------------
# translate_path: .claude / .codex
# ---------------------------------------------------------------------------


def test_claude_dir_user_scoped(agent_dir):
    result = path_translator.translate_path(
        "/.claude/settings.json", agent_dir, username="alice",
    )
    assert result == f"{agent_dir}/users/alice/.claude/settings.json"


def test_claude_dir_agent_scoped(agent_dir):
    result = path_translator.translate_path(
        "/.claude/settings.json", agent_dir, username="",
    )
    assert result == f"{agent_dir}/workspace/.claude/settings.json"


def test_codex_dir_user_scoped(agent_dir):
    result = path_translator.translate_path(
        "/.codex/config.toml", agent_dir, username="alice",
    )
    assert result == f"{agent_dir}/users/alice/.codex/config.toml"


def test_codex_dir_agent_scoped(agent_dir):
    result = path_translator.translate_path(
        "/.codex/config.toml", agent_dir, username="",
    )
    assert result == f"{agent_dir}/workspace/.codex/config.toml"


# ---------------------------------------------------------------------------
# translate_path: passthrough cases
# ---------------------------------------------------------------------------


def test_url_passes_through(agent_dir):
    """URLs aren't paths — leave alone."""
    val = "https://example.com/foo"
    assert path_translator.translate_path(val, agent_dir, username="alice") == val


def test_proxy_url_passes_through(agent_dir):
    """PROXY_URL value should not be rewritten."""
    val = "http://100.64.5.10:8400"
    assert path_translator.translate_path(val, agent_dir, username="alice") == val


def test_arbitrary_string_passes_through(agent_dir):
    val = "some-api-key-XXXX"
    assert path_translator.translate_path(val, agent_dir, username="alice") == val


def test_empty_string_passes_through(agent_dir):
    assert path_translator.translate_path("", agent_dir, username="alice") == ""


def test_unrelated_absolute_path_passes_through(agent_dir):
    """Random absolute paths the MCP might use directly aren't translated."""
    val = "/tmp/scratch.txt"
    assert path_translator.translate_path(val, agent_dir, username="alice") == val


# ---------------------------------------------------------------------------
# translate_path: tricky/edge cases
# ---------------------------------------------------------------------------


def test_users_alone_translates(agent_dir):
    """Bare /users (no trailing slash) should still translate."""
    result = path_translator.translate_path("/users", agent_dir, username="alice")
    assert result == f"{agent_dir}/users"


def test_workspace_alone_translates(agent_dir):
    result = path_translator.translate_path("/workspace", agent_dir, username="")
    assert result == f"{agent_dir}/workspace"


def test_path_lookalike_no_leading_slash(agent_dir):
    """No leading slash means no sandbox prefix — not rewritten."""
    val = "users/alice/workspace"
    assert path_translator.translate_path(val, agent_dir, username="alice") == val


# ---------------------------------------------------------------------------
# expand_session_id
# ---------------------------------------------------------------------------


def test_expand_session_id_replaces_token():
    val = "/users/alice/workspace/.screenshots/{session_id}"
    assert path_translator.expand_session_id(val, "abc-123") == "/users/alice/workspace/.screenshots/abc-123"


def test_expand_session_id_idempotent_when_no_token():
    val = "/users/alice/workspace"
    assert path_translator.expand_session_id(val, "abc-123") == "/users/alice/workspace"


# ---------------------------------------------------------------------------
# translate_env
# ---------------------------------------------------------------------------


def test_translate_env_full_round_trip(agent_dir):
    env = {
        "PROXY_URL": "http://100.64.5.10:8400",
        "PROXY_API_KEY": "session-token-xyz",
        "IMAGE_SAVE_DIR": "/users/alice/workspace",
        "SCREENSHOTS_SESSION": "/users/alice/workspace/.screenshots/{session_id}",
        "MY_CREDS": "/users/alice/.my_creds",
        "RANDOM_KEY": "no-path-here",
        "AN_INT": 42,
    }
    result = path_translator.translate_env(
        env, agent_dir=agent_dir, username="alice", session_id="sid-abc",
    )
    # Path values rewritten
    assert result["IMAGE_SAVE_DIR"] == f"{agent_dir}/users/alice/workspace"
    assert result["SCREENSHOTS_SESSION"] == f"{agent_dir}/users/alice/workspace/.screenshots/sid-abc"
    assert result["MY_CREDS"] == f"{agent_dir}/users/alice/.my_creds"
    # Non-path values unchanged
    assert result["PROXY_URL"] == "http://100.64.5.10:8400"
    assert result["PROXY_API_KEY"] == "session-token-xyz"
    assert result["RANDOM_KEY"] == "no-path-here"
    assert result["AN_INT"] == 42


def test_translate_env_does_not_mutate_input(agent_dir):
    env = {"IMAGE_SAVE_DIR": "/users/alice/workspace"}
    path_translator.translate_env(
        env, agent_dir=agent_dir, username="alice", session_id="sid",
    )
    assert env == {"IMAGE_SAVE_DIR": "/users/alice/workspace"}


def test_translate_env_agent_scoped(agent_dir):
    env = {
        "IMAGE_SAVE_DIR": "/workspace",
        "SCREENSHOTS": "/workspace/.screenshots/{session_id}",
    }
    result = path_translator.translate_env(
        env, agent_dir=agent_dir, username="", session_id="sid-1",
    )
    assert result["IMAGE_SAVE_DIR"] == f"{agent_dir}/workspace"
    assert result["SCREENSHOTS"] == f"{agent_dir}/workspace/.screenshots/sid-1"


# ---------------------------------------------------------------------------
# derive_username_from_cwd_relative
# ---------------------------------------------------------------------------


def test_derive_username_user_scoped():
    assert path_translator.derive_username_from_cwd_relative("users/alice") == "alice"


def test_derive_username_user_scoped_with_subpath():
    assert path_translator.derive_username_from_cwd_relative("users/alice/sub") == "alice"


def test_derive_username_agent_scoped():
    assert path_translator.derive_username_from_cwd_relative("workspace") == ""


def test_derive_username_empty():
    assert path_translator.derive_username_from_cwd_relative("") == ""


def test_derive_username_with_leading_slash():
    """Defensive: strip leading slash."""
    assert path_translator.derive_username_from_cwd_relative("/users/alice") == "alice"


def test_derive_username_random():
    """Anything not starting with 'users/' is agent-scoped."""
    assert path_translator.derive_username_from_cwd_relative("foo/bar") == ""


# ---------------------------------------------------------------------------
# translate_env: multi-value (separator-joined sandbox-path lists)
# ---------------------------------------------------------------------------


def test_translate_env_multivalue_allowed_file_dirs(agent_dir):
    """ALLOWED_FILE_DIRS: split, translate each, rejoin."""
    env = {
        "ALLOWED_FILE_DIRS": "/users/alice:/workspace:/config",
    }
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="alice",
        session_id="s",
        multi_value_envs={"ALLOWED_FILE_DIRS": ":"},
    )
    expected = (
        f"{agent_dir}/users/alice:{agent_dir}/workspace:{agent_dir}/config"
    )
    assert result["ALLOWED_FILE_DIRS"] == expected


def test_translate_env_multivalue_oto_allowed_roots(agent_dir):
    """OTO_ALLOWED_ROOTS uses the same splitter."""
    env = {
        "OTO_ALLOWED_ROOTS": "/users/alice:/workspace:/config",
    }
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="alice",
        session_id="s",
        multi_value_envs={"OTO_ALLOWED_ROOTS": ":"},
    )
    expected = (
        f"{agent_dir}/users/alice:{agent_dir}/workspace:{agent_dir}/config"
    )
    assert result["OTO_ALLOWED_ROOTS"] == expected


def test_translate_env_multivalue_drops_empty_segments(agent_dir):
    """Empty segments (e.g. trailing separator) are dropped."""
    env = {"X": "/users/alice::/workspace"}
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="alice",
        session_id="s",
        multi_value_envs={"X": ":"},
    )
    assert result["X"] == f"{agent_dir}/users/alice:{agent_dir}/workspace"


def test_translate_env_multivalue_custom_separator(agent_dir):
    env = {"PATHS_LIST": "/users/alice,/workspace,/config"}
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="alice",
        session_id="s",
        multi_value_envs={"PATHS_LIST": ","},
    )
    assert (
        result["PATHS_LIST"]
        == f"{agent_dir}/users/alice,{agent_dir}/workspace,{agent_dir}/config"
    )


def test_translate_env_multivalue_pass_through_segment_unchanged(agent_dir):
    """Segments that don't match a sandbox prefix pass through unchanged."""
    env = {"X": "/users/alice:/some/random/abs/path"}
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="alice",
        session_id="s",
        multi_value_envs={"X": ":"},
    )
    assert (
        result["X"] == f"{agent_dir}/users/alice:/some/random/abs/path"
    )


def test_translate_env_non_multi_with_colon_passes_through(agent_dir):
    """Env vars NOT listed in multi_value_envs that contain `:` aren't split."""
    env = {"PATH": "/usr/bin:/usr/local/bin"}
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="alice",
        session_id="s",
        multi_value_envs={},
    )
    # PATH is treated as a single value; doesn't match any sandbox prefix → passes through.
    assert result["PATH"] == "/usr/bin:/usr/local/bin"


def test_translate_env_multivalue_with_session_id_token(agent_dir):
    """{session_id} expansion happens before split (idempotent across segments)."""
    env = {"X": "/users/alice/workspace/.screenshots/{session_id}:/workspace"}
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="alice",
        session_id="sid-42",
        multi_value_envs={"X": ":"},
    )
    expected = (
        f"{agent_dir}/users/alice/workspace/.screenshots/sid-42:"
        f"{agent_dir}/workspace"
    )
    assert result["X"] == expected


def test_translate_env_multivalue_viewer_only_user_root(agent_dir):
    """Viewer's OTO_ALLOWED_ROOTS is just /users/{u}."""
    env = {"OTO_ALLOWED_ROOTS": "/users/viewer1"}
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="viewer1",
        session_id="s",
        multi_value_envs={"OTO_ALLOWED_ROOTS": ":"},
    )
    assert result["OTO_ALLOWED_ROOTS"] == f"{agent_dir}/users/viewer1"


def test_translate_env_multivalue_agent_scoped(agent_dir):
    """Agent-scoped: only /workspace in the joined list."""
    env = {"OTO_ALLOWED_ROOTS": "/workspace"}
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="",
        session_id="s",
        multi_value_envs={"OTO_ALLOWED_ROOTS": ":"},
    )
    assert result["OTO_ALLOWED_ROOTS"] == f"{agent_dir}/workspace"


def test_translate_env_multivalue_empty_value(agent_dir):
    """An empty multi-value env (no segments) stays empty."""
    env = {"X": ""}
    result = path_translator.translate_env(
        env,
        agent_dir=agent_dir,
        username="alice",
        session_id="s",
        multi_value_envs={"X": ":"},
    )
    assert result["X"] == ""


def test_translate_env_no_multi_value_arg_defaults_empty_dict(agent_dir):
    """Backwards-compat: omitting multi_value_envs treats all envs as single-value."""
    env = {"IMAGE_SAVE_DIR": "/users/alice/workspace"}
    result = path_translator.translate_env(
        env, agent_dir=agent_dir, username="alice", session_id="s",
    )
    assert result["IMAGE_SAVE_DIR"] == f"{agent_dir}/users/alice/workspace"


# ---------------------------------------------------------------------------
# translate_paths_in_text
# ---------------------------------------------------------------------------


def test_text_user_scoped_photo_path(agent_dir):
    """Sandbox-virtual photo path inside an injected attachment line is
    rewritten to satellite-absolute. This is the primary use case — the
    proxy ships `/users/{u}/workspace/uploads/photos/img.jpg` for chat-
    attached photos and the satellite must resolve it to a real path
    before the CLI subprocess tries to read it."""
    text = (
        "Hello\n\nThe user has attached 1 image(s). "
        "Read and analyze them using the Read tool:\n"
        "- /users/alice/workspace/uploads/photos/img_abc.jpg\n"
    )
    result = path_translator.translate_paths_in_text(
        text, agent_dir=agent_dir, username="alice",
    )
    assert (
        f"- {agent_dir}/users/alice/workspace/uploads/photos/img_abc.jpg"
        in result
    )
    # Plain text untouched
    assert "Hello" in result


def test_text_agent_scoped_workspace_path(agent_dir):
    """Agent-scoped sessions inject `/workspace/...` paths."""
    text = (
        "The user has attached 1 file(s):\n"
        "- /workspace/uploads/files/report.pdf (PDF document)\n"
    )
    result = path_translator.translate_paths_in_text(
        text, agent_dir=agent_dir, username="",
    )
    assert f"- {agent_dir}/workspace/uploads/files/report.pdf" in result


def test_text_multiple_paths(agent_dir):
    """Every sandbox-virtual path in the prompt is translated independently."""
    text = (
        "Read /users/alice/workspace/a.txt and "
        "/users/alice/workspace/b.txt then write to /workspace/out.md"
    )
    result = path_translator.translate_paths_in_text(
        text, agent_dir=agent_dir, username="alice",
    )
    assert f"{agent_dir}/users/alice/workspace/a.txt" in result
    assert f"{agent_dir}/users/alice/workspace/b.txt" in result
    assert f"{agent_dir}/workspace/out.md" in result


def test_text_url_with_users_segment_not_translated(agent_dir):
    """A URL containing `/users/...` must not be rewritten — the negative
    lookbehind on `[\\w./-]` excludes paths embedded after a domain."""
    text = "See https://github.com/users/alice/repo for details"
    result = path_translator.translate_paths_in_text(
        text, agent_dir=agent_dir, username="alice",
    )
    assert "https://github.com/users/alice/repo" in result


def test_text_no_paths_passes_through(agent_dir):
    """Strings without sandbox paths are returned unchanged."""
    text = "Just a regular chat message with no paths."
    result = path_translator.translate_paths_in_text(
        text, agent_dir=agent_dir, username="alice",
    )
    assert result == text


def test_text_empty_string(agent_dir):
    """Empty input returns empty (no regex search needed)."""
    result = path_translator.translate_paths_in_text(
        "", agent_dir=agent_dir, username="alice",
    )
    assert result == ""


def test_text_config_path_translated(agent_dir):
    """`/config/...` paths translate the same as `/users/...` and `/workspace/...`."""
    text = "Read /config/context/spec.md"
    result = path_translator.translate_paths_in_text(
        text, agent_dir=agent_dir, username="alice",
    )
    assert f"{agent_dir}/config/context/spec.md" in result


def test_text_claude_dir_path_translated(agent_dir):
    """`/.claude/...` paths translate to per-user (or workspace for agent-scoped)."""
    text = "Plan saved to /.claude/plans/myplan.md"
    result = path_translator.translate_paths_in_text(
        text, agent_dir=agent_dir, username="alice",
    )
    assert f"{agent_dir}/users/alice/.claude/plans/myplan.md" in result


def test_text_path_followed_by_punctuation(agent_dir):
    """Trailing punctuation (period, comma, parenthesis) breaks the path
    match cleanly so it doesn't get included in the translated path."""
    text = (
        "I read /users/alice/workspace/foo.txt, then wrote "
        "/users/alice/workspace/bar.md."
    )
    result = path_translator.translate_paths_in_text(
        text, agent_dir=agent_dir, username="alice",
    )
    # Period at end of first path — currently part of `foo.txt` (file ext)
    assert f"{agent_dir}/users/alice/workspace/foo.txt" in result
    # Comma terminates — `,` not in body chars, so `foo.txt` is the match
    # and the comma stays in the surrounding text.
    assert ", then wrote " in result
    assert f"{agent_dir}/users/alice/workspace/bar.md" in result


# ---------------------------------------------------------------------------
# translate_satellite_to_virtual_in_text (REVERSE: in-tree satellite-host
# absolute → sandbox-virtual). Used by the HTTP tunnel on outgoing Docker-MCP
# request bodies so a proxy-hosted MCP (file-tools, camoufox) receives the
# sandbox-virtual form it understands. These lock the "file inside the agent
# folder, addressed by its real absolute path, must resolve on the FIRST try"
# behavior. Inputs use consistent case so the assertions are platform-stable
# (os.path.normcase only loosens matching on case-insensitive FS).
# ---------------------------------------------------------------------------


def test_sat_to_virtual_windows_forward_slash_in_tree():
    """The exact reported case: a Windows in-tree file passed as a
    forward-slash absolute path folds back to /users/<u>/workspace/..."""
    agents_dir = Path("C:/Users/alice/OtoDock/agents")
    body = (
        '{"path": "C:/Users/alice/OtoDock/agents/personal-assistant/'
        'users/bob/workspace/test-document.docx"}'
    )
    out = path_translator.translate_satellite_to_virtual_in_text(
        body, agents_dir=agents_dir,
    )
    assert out == '{"path": "/users/bob/workspace/test-document.docx"}'


def test_sat_to_virtual_windows_escaped_backslash_in_tree():
    """Same file with Windows escaped-backslash JSON separators (\\\\)."""
    agents_dir = Path("C:/Users/alice/OtoDock/agents")
    body = (
        '{"path": "C:\\\\Users\\\\alice\\\\OtoDock\\\\agents\\\\'
        'personal-assistant\\\\users\\\\bob\\\\workspace\\\\'
        'test-document.docx"}'
    )
    out = path_translator.translate_satellite_to_virtual_in_text(
        body, agents_dir=agents_dir,
    )
    assert out == '{"path": "/users/bob/workspace/test-document.docx"}'


def test_sat_to_virtual_workspace_subtree():
    """Agent-scoped /workspace files fold back too (not just /users)."""
    agents_dir = Path("C:/Users/alice/OtoDock/agents")
    body = '{"source": "C:/Users/alice/OtoDock/agents/pa/workspace/screenshots/x.png"}'
    out = path_translator.translate_satellite_to_virtual_in_text(
        body, agents_dir=agents_dir,
    )
    assert out == '{"source": "/workspace/screenshots/x.png"}'


def test_sat_to_virtual_outside_tree_passes_through():
    """A Desktop file OUTSIDE the agent tree is left unchanged — it takes the
    full-FS host-pull path on the proxy, not the agent-tree fold."""
    agents_dir = Path("C:/Users/alice/OtoDock/agents")
    body = '{"source": "C:/Users/alice/Desktop/logo.png"}'
    out = path_translator.translate_satellite_to_virtual_in_text(
        body, agents_dir=agents_dir,
    )
    assert out == body


def test_sat_to_virtual_posix_in_tree():
    """A POSIX (Linux/macOS) satellite folds its in-tree absolute back too."""
    agents_dir = Path("/home/alice/.oto-dock/agents")
    body = "Read /home/alice/.oto-dock/agents/pa/users/bob/workspace/a.txt please"
    out = path_translator.translate_satellite_to_virtual_in_text(
        body, agents_dir=agents_dir,
    )
    assert "/users/bob/workspace/a.txt" in out
    assert "/home/alice/.oto-dock/agents" not in out


def test_sat_to_virtual_empty_inputs():
    """Empty text or missing agents_dir is a no-op (defensive)."""
    agents_dir = Path("C:/Users/alice/OtoDock/agents")
    assert path_translator.translate_satellite_to_virtual_in_text(
        "", agents_dir=agents_dir,
    ) == ""
