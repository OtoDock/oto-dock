"""Tests for the Claude Code CLI background-command output read allowance.

Covers the structural matcher (services/path_roles.is_claude_bg_output_path) and
the local read-path gate (auth/path_policy._check_read_path) — confirming the
allow-rule admits the agent's own ``*.output`` files WITHOUT weakening the
OAuth-credential, agent-config, or cross-user denies that precede it.
"""

import sys
from pathlib import Path

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

import config
from auth.path_policy import SecurityContext, _check_read_path
from services import path_roles

_AGENTS = config.AGENTS_DIR.resolve()


def _ctx(role="manager", username="", agent="personal-assistant", is_admin_agent=False):
    return SecurityContext(role=role, username=username, agent=agent,
                           is_admin_agent=is_admin_agent)


# --- the structural matcher ------------------------------------------------

SID = "83089e74-1234"


def test_matcher_linux_sandbox_path():
    assert path_roles.is_claude_bg_output_path(
        "/tmp/claude-1000/-home-dave/83089e74-1234/tasks/b6v8ayxb6.output", SID)


def test_matcher_rejects_another_sessions_output():
    # The session segment is the capability: a foreign session's output,
    # however well its path is guessed, never matches the caller's id.
    assert not path_roles.is_claude_bg_output_path(
        "/tmp/claude-1000/-home-dave/83089e74-1234/tasks/b6v8ayxb6.output", "0ther-sess")


def test_matcher_empty_session_matches_nothing():
    assert not path_roles.is_claude_bg_output_path(
        "/tmp/claude-1000/-home-dave/83089e74-1234/tasks/b6v8ayxb6.output", "")


def test_matcher_session_id_case_insensitive():
    assert path_roles.is_claude_bg_output_path(
        "/tmp/claude-1000/-home-dave/83089E74-1234/tasks/x.output", SID)


def test_matcher_windows_path():
    assert path_roles.is_claude_bg_output_path(
        r"C:\Users\frank\AppData\Local\Temp\claude\abc\tasks\bzsw24mbr.output", "abc")


def test_matcher_macos_tmpdir_path():
    assert path_roles.is_claude_bg_output_path(
        "/var/folders/xy/claude-501/proj/sess/tasks/q1w2e3.output", "sess")


def test_matcher_rejects_tasks_without_a_session_segment():
    assert not path_roles.is_claude_bg_output_path("/tmp/claude-1000/tasks/x.output", "sess")


def test_matcher_rejects_non_output_suffix():
    assert not path_roles.is_claude_bg_output_path(
        "/tmp/claude-1000/x/sess/tasks/notes.txt", "sess")


def test_matcher_rejects_without_claude_segment():
    assert not path_roles.is_claude_bg_output_path("/tmp/foo/sess/tasks/x.output", "sess")


def test_matcher_rejects_without_tasks_segment():
    assert not path_roles.is_claude_bg_output_path("/tmp/claude-1000/sess/x.output", "sess")


def test_matcher_rejects_claude_after_tasks():
    # "claude-*" must come BEFORE "tasks"; this ordering must not match.
    assert not path_roles.is_claude_bg_output_path("/tmp/tasks/claude-1000/x.output", "x")


def test_matcher_accepts_pathlib_input():
    assert path_roles.is_claude_bg_output_path(
        Path("/tmp/claude-1000/p/s/tasks/abc.output"), "s")


# --- the local read-path gate ----------------------------------------------

def test_read_allows_own_bg_output_agent_scope():
    # Agent-scope session (username="") — the common case for tasks/phone.
    p = Path("/tmp/claude-1000/-home-dave/sess/tasks/abc.output")
    assert _check_read_path(p, _ctx(username="")).allowed


def test_read_allows_own_bg_output_user_scope():
    p = Path("/tmp/claude-1001/proj/sess/tasks/abc.output")
    assert _check_read_path(p, _ctx(role="viewer", username="alice")).allowed


def test_read_allows_plain_tmp_file_locally():
    # The sandbox's /tmp is a private tmpfs: the local gate admits all of it,
    # so the bg-output rule only matters for remote targets now.
    p = Path("/tmp/claude-1000/sess/secrets.txt")
    assert _check_read_path(p, _ctx(username="")).allowed


def test_bg_allow_does_not_bypass_cross_user_deny():
    # A .output file that ALSO sits under another user's dir must stay denied —
    # the cross-user check runs before the bg-output allow.
    p = (_AGENTS / "personal-assistant" / "users" / "alice"
         / "claude-1" / "tasks" / "x.output")
    # current session is a DIFFERENT user (bob) — alice's dir is off-limits
    assert not _check_read_path(p, _ctx(role="manager", username="bob")).allowed
    # and an agent-scope session (username="") also can't read any users/* dir
    assert not _check_read_path(p, _ctx(username="")).allowed


def test_credential_and_config_paths_are_structurally_exclusive():
    # Sanity: credential (*-tokens json) and agent-config (.json/.toml) paths
    # can never end in ".output", so the bg-output allow can't shadow those
    # denies by construction.
    assert not path_roles.is_claude_bg_output_path(
        "/tmp/claude-1000/sess/tasks/anthropic-tokens.json", "sess")
    assert not path_roles.is_claude_bg_output_path(
        str(_AGENTS / "pa" / "workspace" / ".claude" / "settings.json"), "sess")
