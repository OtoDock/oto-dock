"""Agent-read-deny of its OWN CLI config files.

The agent's per-session `.claude/*.json` + `.codex/{config.toml,auth.json}` carry
this session's secrets — the broker capability token, the swapped-in HTTP bearer
(local), the session JWT, the Codex model token. A prompt-injected agent
must not be able to Read / cat / grep its own config and paste the token in chat.

Gate is mirrored across all channels: `auth.path_policy._check_read_path` (native
Read + local bash cat/grep route here) and `services.path_policy_v2.
_protected_path_denial` (remote + MCP arg-paths). Matched ONLY at a session scope
root, so a repo that itself uses Claude Code / Codex stays readable.

NOT a malicious-MCP boundary — a native same-uid MCP can open() the file directly.
"""

from __future__ import annotations

from pathlib import Path

from services import path_roles
from services.path_policy_v2 import _protected_path_denial
from auth.path_policy import (
    SecurityContext,
    _AGENTS_DIR,
    _check_bash,
    _check_read_path,
    _check_write_path,
    check_tool_access,
)
from core import placement


# ---------------------------------------------------------------------------
# is_protected_agent_config_path — the core matcher
# ---------------------------------------------------------------------------

PROTECTED = [
    "/users/alice/.claude/personal-assistant-abc123.json",
    "/users/alice/.claude/mcp-config.json",
    "/users/alice/.claude/github-mcp-personal-assistant.json",  # instance config
    "/workspace/.claude/mcp-config.json",
    "/knowledge/.claude/ctx.json",
    "/users/alice/.codex/config.toml",
    "/users/alice/.codex/auth.json",
    "/workspace/.codex/config.toml",
    # host form (resolved local path)
    "/home/x/docker/oto-dock/agents/pa/users/alice/.claude/pa-abc.json",
    # satellite-absolute form
    "/home/frank/.oto-dock/agents/pa/users/alice/.codex/config.toml",
    # an external caller's tree (a phone caller who is not a platform user):
    # sandbox-virtual, host, and the ephemeral (withheld-number) host form
    "/caller/.codex/config.toml",
    "/caller/.codex/auth.json",
    "/caller/.claude/settings.json",
    "/caller/.claude/.credentials.json",
    "/home/x/agents/pa/externals/phone/302101234567/.codex/config.toml",
    "/home/x/agents/pa/externals/phone/302101234567/.claude/pa-abc.json",
    "/home/x/agents/pa/externals/phone/_ephemeral/11111111-2222/.codex/config.toml",
]

NOT_PROTECTED = [
    # repo nested under workspace/ — third-party config, secret-free → readable
    "/users/alice/workspace/myrepo/.claude/settings.json",
    "/workspace/myrepo/.codex/config.toml",
    "/home/x/agents/pa/users/alice/workspace/proj/.claude/foo.json",
    # a caller's own files, and a repo nested in a caller tree
    "/caller/workspace/notes.md",
    "/caller/workspace/proj/.claude/settings.json",
    "/home/x/agents/pa/externals/phone/302101234567/context/memory/topic.md",
    # not a config file
    "/users/alice/.codex/history.jsonl",
    "/users/alice/.claude/projects/transcript.jsonl",
    "/users/alice/.claude/projects/x.json",  # nested under .claude, not a config
    # not under a config dir at all
    "/users/alice/workspace/notes.json",
    "/users/alice/workspace/.env",
    # the dir itself (no filename)
    "/users/alice/.claude",
    "",
]


def test_protected_paths_matched():
    for p in PROTECTED:
        assert path_roles.is_protected_agent_config_path(p) is True, p


def test_non_protected_paths_not_matched():
    for p in NOT_PROTECTED:
        assert path_roles.is_protected_agent_config_path(p) is False, p


def test_handles_path_objects_and_none():
    assert path_roles.is_protected_agent_config_path(
        Path("/users/alice/.claude/x.json")
    ) is True
    assert path_roles.is_protected_agent_config_path(None) is False


# ---------------------------------------------------------------------------
# _check_read_path wiring (native Read + local bash) — universal (incl. admin)
# ---------------------------------------------------------------------------


def _admin_ctx() -> SecurityContext:
    return SecurityContext(
        role="admin", username="alice", agent="personal-assistant",
        is_admin_agent=True,
    )


def test_read_denies_own_claude_config_even_for_admin():
    p = (_AGENTS_DIR / "personal-assistant" / "users" / "alice"
         / ".claude" / "personal-assistant-abc.json").resolve()
    decision = _check_read_path(p, _admin_ctx())
    assert decision.allowed is False
    assert "agent CLI config" in decision.reason


def test_read_denies_own_codex_config_even_for_admin():
    p = (_AGENTS_DIR / "personal-assistant" / "users" / "alice"
         / ".codex" / "config.toml").resolve()
    decision = _check_read_path(p, _admin_ctx())
    assert decision.allowed is False
    assert "agent CLI config" in decision.reason


def test_read_allows_repo_nested_claude_config():
    """OSS regression — a repo that itself uses Claude Code stays readable."""
    p = (_AGENTS_DIR / "personal-assistant" / "users" / "alice"
         / "workspace" / "myrepo" / ".claude" / "settings.json").resolve()
    decision = _check_read_path(p, _admin_ctx())
    assert decision.allowed is True


def test_read_allows_normal_workspace_file():
    p = (_AGENTS_DIR / "personal-assistant" / "workspace" / "notes.md").resolve()
    decision = _check_read_path(p, _admin_ctx())
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# _check_bash wiring (cat/grep route through _check_read_path)
# ---------------------------------------------------------------------------


def test_bash_cat_of_own_config_denied():
    decision = _check_bash(
        "cat /users/alice/.claude/personal-assistant-abc.json", _admin_ctx(),
    )
    assert decision.allowed is False
    assert "agent CLI config" in decision.reason


def test_bash_grep_of_own_codex_config_denied():
    decision = _check_bash(
        "grep Bearer /users/alice/.codex/config.toml", _admin_ctx(),
    )
    assert decision.allowed is False


def test_bash_cat_repo_config_allowed():
    decision = _check_bash(
        "cat /users/alice/workspace/myrepo/.claude/settings.json", _admin_ctx(),
    )
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# _protected_path_denial wiring (remote + MCP arg-paths)
# ---------------------------------------------------------------------------


def test_protected_denial_blocks_own_config():
    assert _protected_path_denial(
        "/users/alice/.claude/mcp-config.json", writing=False
    ) != ""
    assert _protected_path_denial(
        "/users/alice/.codex/config.toml", writing=False
    ) != ""


def test_protected_denial_allows_repo_config():
    assert _protected_path_denial(
        "/users/alice/workspace/myrepo/.codex/config.toml", writing=False
    ) == ""
    assert _protected_path_denial(
        "/workspace/proj/.claude/settings.json", writing=False
    ) == ""


# ---------------------------------------------------------------------------
# command_references_protected_agent_config — raw-text backstop
# ---------------------------------------------------------------------------


def test_command_backstop_matches_scope_root_config():
    for cmd in [
        "cat /users/alice/.claude/personal-assistant-abc.json",
        "grep Bearer /workspace/.codex/config.toml",
        "cat ~/.oto-dock/agents/pa/users/alice/.codex/auth.json",
    ]:
        assert path_roles.command_references_protected_agent_config(cmd) is True, cmd


def test_command_backstop_skips_repo_and_unrelated():
    for cmd in [
        "cat /users/alice/workspace/repo/.claude/settings.json",  # repo nested
        "cat /workspace/proj/.codex/config.toml",                 # repo nested
        "cp x /users/alice/workspace/repo/.claude/permission_gate.py",
        "ls /workspace",
        "cat /users/alice/workspace/notes.md",
        "echo x > /users/alice/.claude/todos/x.json",
        "",
    ]:
        assert path_roles.command_references_protected_agent_config(cmd) is False, cmd


# ---------------------------------------------------------------------------
# The write-side set: the hook scripts and settings the gate runs from
# ---------------------------------------------------------------------------

WRITE_PROTECTED = [
    "/users/alice/.claude/permission_gate.py",
    "/users/alice/.claude/tool_result_forwarder.py",
    "/users/alice/.claude/subagent_tracker.py",
    "/users/alice/.claude/stop_tracker.py",
    "/users/alice/.claude/stdio_path_interceptor.py",
    "/users/alice/.codex/hooks.json",
    "/users/alice/.codex/permission_gate.py",
    "/workspace/.claude/subagent_tracker.py",
    "/knowledge/.codex/tool_result_forwarder.py",
    "/caller/.claude/permission_gate.py",
    "/home/frank/.oto-dock/agents/pa/users/alice/.claude/stop_tracker.py",
    # the sandbox HOME, in case a CLI ever falls back to it
    "/tmp/.claude/settings.json",
    "/tmp/.claude.json",
    "/tmp/.codex/config.toml",
    "/tmp/.claude/permission_gate.py",
    "/tmp/.codex/hooks.json",
]

# Readable (no secret in them), write-protected (the gate runs from them).
SCRIPTS = [
    "/users/alice/.claude/permission_gate.py",
    "/users/alice/.codex/hooks.json",
    "/tmp/.claude/permission_gate.py",
]

NOT_WRITE_PROTECTED = [
    "/users/alice/workspace/repo/.claude/permission_gate.py",  # a repo's own
    "/users/alice/.claude/todos/x.json",                        # not at the dir
    "/users/alice/.claude/plans/p.md",
    "/users/alice/.codex/sessions/2026/x.jsonl",
    "/tmp/x.py",
    "/tmp/claude-1000/slug/sid/scratchpad/permission_gate.py",
    "/var/tmp/.claude.json",
]


def test_write_protected_set():
    for p in WRITE_PROTECTED:
        assert path_roles.is_protected_agent_config_path(p, writing=True) is True, p


def test_hook_files_are_readable_but_not_writable():
    for p in SCRIPTS:
        assert path_roles.is_protected_agent_config_path(p) is False, p
        assert path_roles.is_protected_agent_config_path(p, writing=True) is True, p


def test_not_write_protected():
    for p in NOT_WRITE_PROTECTED:
        assert path_roles.is_protected_agent_config_path(p, writing=True) is False, p


def _mgr_ctx() -> SecurityContext:
    return SecurityContext(role="manager", username="alice", agent="personal-assistant",
                           is_admin_agent=False)


def test_write_path_gate_denies_the_set_for_every_role():
    p = (_AGENTS_DIR / "personal-assistant" / "users" / "alice"
         / ".claude" / "permission_gate.py").resolve()
    for ctx in (_mgr_ctx(), _admin_ctx()):
        decision = _check_write_path(p, ctx)
        assert decision.allowed is False and "agent CLI config" in decision.reason
    ok = (_AGENTS_DIR / "personal-assistant" / "users" / "alice"
          / ".claude" / "todos" / "x.json").resolve()
    assert _check_write_path(ok, _mgr_ctx()).allowed is True


def test_tool_gate_holds_for_an_admin_on_an_admin_agent():
    """The admin fast path skips the role matrix, never the protected set:
    the agent's own config is not readable, the hook scripts not writable."""
    ctx = _admin_ctx()
    for tool, path in [
        ("Write", "/users/alice/.claude/permission_gate.py"),
        ("Edit", "/users/alice/.codex/hooks.json"),
        ("Write", "/users/alice/.codex/config.toml"),
        ("Read", "/users/alice/.codex/auth.json"),
        ("Read", "/users/alice/.claude/personal-assistant-abc.json"),
    ]:
        decision, _ = check_tool_access(tool, {"file_path": path}, ctx)
        assert decision.allowed is False and "protected" in decision.reason, (tool, path)
    for tool, path in [
        ("Read", "/users/alice/.claude/permission_gate.py"),
        ("Write", "/users/alice/.claude/todos/x.json"),
        ("Write", "/users/alice/workspace/repo/.claude/permission_gate.py"),
        ("Write", "/etc/hosts"),
    ]:
        decision, _ = check_tool_access(tool, {"file_path": path}, ctx)
        assert decision.allowed is True, (tool, path, decision.reason)


def test_tool_gate_holds_for_an_admin_on_an_admin_agent_remote():
    ctx = SecurityContext(
        role="admin", username="alice", agent="personal-assistant", is_admin_agent=True,
        placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, home_dir="/home/svc", allow_full_fs=False, agents_dir="/home/svc/.oto-dock/agents", machine_id="m1"),
        )
    tree = "/home/svc/.oto-dock/agents/personal-assistant"
    d, _ = check_tool_access("Write", {"file_path": f"{tree}/users/alice/.claude/permission_gate.py"}, ctx)
    assert d.allowed is False and "protected" in d.reason
    d, _ = check_tool_access("Read", {"file_path": f"{tree}/users/alice/.codex/auth.json"}, ctx)
    assert d.allowed is False and "protected" in d.reason
    d, _ = check_tool_access("Write", {"file_path": "/etc/motd"}, ctx)  # no home band for admin
    assert d.allowed is True
    d, _ = check_tool_access("Read", {"file_path": f"{tree}/users/alice/.claude/permission_gate.py"}, ctx)
    assert d.allowed is True


# The gate runs as ``python3 <dir>/permission_gate.py``: ``<dir>`` is first on
# ``sys.path``, so any module planted beside it runs inside the gate, and a
# directive file there is read by every session the dir configures.
SHADOW_WRITES = [
    "/workspace/.claude/json.py",
    "/workspace/.claude/os.pyc",
    "/workspace/.claude/urllib/__init__.py",
    "/workspace/.codex/json.py",
    "/users/alice/.claude/json.py",
    "/workspace/.claude/CLAUDE.md",
    "/workspace/.codex/AGENTS.md",
    "/workspace/.claude",
    "/users/alice/.codex",
    "/home/frank/.oto-dock/agents/pa/workspace/.claude/json.py",
]


def test_nothing_can_be_planted_beside_the_hook_scripts():
    for p in SHADOW_WRITES:
        assert path_roles.is_protected_agent_config_path(p, writing=True) is True, p
        assert path_roles.is_protected_agent_config_path(p) is False, p
    ctx = SecurityContext(role="contributor", username="alice", agent="personal-assistant",
                          is_admin_agent=False)
    for tool, path in [("Write", "/workspace/.claude/json.py"),
                       ("Write", "/workspace/.claude/CLAUDE.md"),
                       ("Edit", "/users/alice/.claude/json.py")]:
        decision, _ = check_tool_access(tool, {"file_path": path}, ctx)
        assert decision.allowed is False and "protected" in decision.reason, (tool, path)


def test_session_state_dir_membership():
    for p in ("workspace/.claude", "workspace/.claude/.credentials.json",
              "users/bob/.codex/sessions/x.jsonl", "knowledge/.claude/projects/a/b.jsonl"):
        assert path_roles.in_session_state_dir(p) is True, p
    for p in ("workspace/repo/.claude/settings.json", "workspace/claude/x", "workspace",
              "users/bob/notes.md", ""):
        assert path_roles.in_session_state_dir(p) is False, p


def test_apply_patch_and_bash_inherit_the_write_set():
    ctx = _mgr_ctx()
    patch = ("*** Begin Patch\n*** Add File: /users/alice/.claude/permission_gate.py\n"
             "+x\n*** End Patch")
    assert check_tool_access("apply_patch", {"command": patch}, ctx)[0].allowed is False
    for cmd in [
        "cp x /users/alice/.claude/permission_gate.py",
        "echo x > /users/alice/.codex/hooks.json",
        "sed -i s/a/b/ /workspace/.claude/stop_tracker.py",
        "echo x > /tmp/.codex/config.toml",
    ]:
        assert _check_bash(cmd, ctx).allowed is False, cmd
        assert _check_bash(cmd, _admin_ctx()).allowed is False, cmd
    # `~` is the sandbox HOME (/tmp): the write-path check sees it; the raw
    # backstop (all an admin agent's Bash gets) has no scope root to match,
    # and nothing runs from /tmp/.codex.
    assert _check_bash("echo x > ~/.codex/hooks.json", ctx).allowed is False
    assert _check_bash("cp x /users/alice/workspace/repo/.claude/permission_gate.py", ctx).allowed


def test_copy_family_into_the_config_dir_is_judged_by_the_file_it_writes():
    """A directory destination (named last, or by -t / --target-directory)
    receives DEST/<basename>: that file is what the check judges."""
    for cmd in [
        "cp -t /workspace/.claude /workspace/foo/settings.json",
        "cp --target-directory=/workspace/.claude /workspace/foo/settings.json",
        "cp -rt /workspace/.claude /workspace/foo/settings.json",
        "install -t /workspace/.claude /workspace/foo/permission_gate.py",
        "install -m 755 /workspace/foo/permission_gate.py /workspace/.claude",
        "cp /workspace/foo/settings.json /workspace/.claude",
        "mv /workspace/foo/json.py /workspace/.claude/",
        "ln -s /workspace/foo/settings.json /workspace/.claude/",
        "rsync /workspace/foo/settings.json /workspace/.claude/",
        "cp /workspace/foo/settings.json /users/alice/.codex",
        "mv /workspace/.claude /workspace/old",
    ]:
        assert _check_bash(cmd, _mgr_ctx()).allowed is False, cmd
    assert _check_bash("cp /workspace/foo/a.md /workspace/docs/", _mgr_ctx()).allowed
    assert _check_bash("rsync -t /workspace/a.md /workspace/docs/", _mgr_ctx()).allowed


def test_mv_sources_are_writes():
    from auth.path_shell import _extract_path_args
    reads, writes = _extract_path_args("mv", "mv /workspace/a.txt /tmp/")
    assert "/workspace/a.txt" in reads and "/workspace/a.txt" in writes
    assert "/tmp/" in writes and "/tmp/a.txt" in writes
    reads, writes = _extract_path_args("cp", "cp -S .bak -t /workspace/d /workspace/a -- -b")
    assert reads == ["/workspace/a", "-b"]
    assert {"/workspace/d", "/workspace/d/a", "/workspace/d/-b"} <= set(writes)
    viewer = SecurityContext(role="viewer", username="alice", agent="personal-assistant",
                             is_admin_agent=False)
    assert _check_bash("mv /workspace/report.txt /tmp/", viewer).allowed is False
    assert _check_bash("cp /workspace/report.txt /tmp/", viewer).allowed


def test_protected_denial_write_side():
    assert _protected_path_denial("/users/alice/.claude/permission_gate.py", writing=True) != ""
    assert _protected_path_denial("/users/alice/.claude/permission_gate.py", writing=False) == ""
    assert _protected_path_denial("/workspace/.codex/hooks.json", writing=True) != ""
