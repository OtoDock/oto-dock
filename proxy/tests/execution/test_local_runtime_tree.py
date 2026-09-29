"""The sandbox's /tmp on LOCAL targets.

The CLI keeps its scratchpad + background-task outputs under
``/tmp/claude-<proxy uid>/<cwd-slug>/<session id>/`` inside the sandbox's
private tmpfs, which is also the agent's HOME. The local path gate admits the
whole of that tmpfs read + write (``auth/path_policy._is_local_tmp_path``):
nothing of another session or user is there. Remote targets keep the
session-scoped predicate (``services/path_roles.is_session_runtime_path``,
used by ``path_policy_v2`` with the satellite-reported root). The
load-bearing part is what stays denied: the CLI state that would live under
/tmp if a CLI fell back to $HOME, ``.env`` files, every deny that runs before
the carve, and every remote path outside the session's own tree.

Run standalone:
    proxy/venv/bin/python -m pytest tests/execution/test_local_runtime_tree.py -x
"""

import os
import sys
from pathlib import Path

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

import config
from auth.path_policy import (
    SecurityContext,
    _check_read_path,
    _check_write_path,
    check_tool_access,
)
from core.sandbox.sandbox import claude_runtime_root
from services import path_roles
from services.path_policy_v2 import PathPolicyContext, _is_session_runtime_path
from core import placement

_AGENTS = config.AGENTS_DIR.resolve()
SID = "76fe15d2-9b5b-495c-9c6c-42e6626f88f6"
OTHER_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ROOT = claude_runtime_root()
TREE = f"{ROOT}/-users-alice/{SID}"


def _ctx(role="manager", username="alice", agent="personal-assistant",
         sid=SID, **over):
    kwargs = dict(role=role, username=username, agent=agent,
                  is_admin_agent=False, cli_session_id=sid)
    kwargs.update(over)
    return SecurityContext(**kwargs)


# --------------------------------------------------------------------------- #
# The root + the shared predicate
# --------------------------------------------------------------------------- #

def test_local_root_is_tmp_claude_uid():
    assert ROOT == f"/tmp/claude-{os.getuid()}"


def test_predicate_matches_tree_and_rejects_root_and_others():
    ok = path_roles.is_session_runtime_path
    assert ok(f"{TREE}/scratchpad/notes.txt", ROOT, SID)
    assert ok(Path(f"{TREE}/tasks/abc.output"), ROOT, SID)
    assert ok(TREE, ROOT, SID)
    assert not ok(ROOT, ROOT, SID)
    assert not ok(f"{ROOT}/-users-alice/{OTHER_SID}/x", ROOT, SID)
    assert not ok(f"{ROOT}/-users-alice/no-sid/x", ROOT, SID)
    assert not ok("/tmp/evil.txt", ROOT, SID)
    assert not ok(f"/tmp/claude-9999/-users-alice/{SID}/x", ROOT, SID)
    assert not ok(f"{TREE}/x", "", SID)
    assert not ok(f"{TREE}/x", ROOT, "")


def test_predicate_case_insensitive_only_when_asked():
    ok = path_roles.is_session_runtime_path
    win_root = "c:/Users/f/AppData/Local/Temp/claude"
    p = f"c:/Users/f/AppData/Local/Temp/Claude/-c-proj/{SID}/scratchpad/x"
    assert ok(p, win_root, SID, case_insensitive=True)
    assert not ok(p, win_root, SID)
    assert ok(p.replace("/", "\\"), win_root, SID, case_insensitive=True)


def test_v2_wrapper_delegates_to_the_shared_predicate():
    ctx = PathPolicyContext(
        agent_slug="demo", role="manager",
        cli_session_id=SID,
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m1", home_dir="/home/dave", os_user="dave", allow_full_fs=False, agents_dir="/home/dave/.oto-dock/agents", os="linux", claude_runtime_root="/tmp/claude-1000"),
    )
    assert _is_session_runtime_path(f"/tmp/claude-1000/-home-dave/{SID}/x", ctx)
    assert not _is_session_runtime_path(f"/tmp/claude-1000/-home-dave/{OTHER_SID}/x", ctx)


# --------------------------------------------------------------------------- #
# The local gate — allow inside the own tree
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("username", ["alice", ""])
def test_read_and_write_allowed_in_own_tree(username):
    ctx = _ctx(username=username)
    p = Path(f"{TREE}/scratchpad/probe.py")
    assert _check_read_path(p, ctx).allowed
    assert _check_write_path(p, ctx).allowed


@pytest.mark.parametrize("role", ["viewer", "editor", "manager", "admin"])
def test_every_role_gets_its_own_tree(role):
    p = Path(f"{TREE}/scratchpad/out.txt")
    assert _check_write_path(p, _ctx(role=role)).allowed
    assert _check_read_path(p, _ctx(role=role)).allowed


def test_tool_gate_write_read_glob_and_bash_redirect():
    ctx = _ctx()
    d, _ = check_tool_access("Write", {"file_path": f"{TREE}/scratchpad/probe.py",
                                       "content": "x"}, ctx)
    assert d.allowed, d.reason
    d, _ = check_tool_access("Read", {"file_path": f"{TREE}/scratchpad/probe.py"}, ctx)
    assert d.allowed, d.reason
    d, _ = check_tool_access("Glob", {"pattern": "*.py", "path": f"{TREE}/scratchpad"}, ctx)
    assert d.allowed, d.reason
    d, _ = check_tool_access("Bash", {"command": f"echo hi > {TREE}/scratchpad/out.txt"}, ctx)
    assert d.allowed and d.permission_tier == "edit", d.reason
    d, _ = check_tool_access("Bash", {"command": f"cat {TREE}/scratchpad/out.txt"}, ctx)
    assert d.allowed and d.permission_tier == "read", d.reason


def test_bg_output_read_still_works_without_a_session_id():
    # The read-only *.output rule predates the carve and must survive it.
    p = Path(f"{ROOT}/-users-alice/{SID}/tasks/abc.output")
    assert _check_read_path(p, _ctx(sid="")).allowed


# --------------------------------------------------------------------------- #
# The local gate — what stays denied
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    "/tmp/n.xml",
    f"{ROOT}",
    f"{ROOT}/-users-alice",
    f"{ROOT}/-users-alice/{OTHER_SID}/scratchpad/x",
    f"/tmp/claude-99999/-users-alice/{SID}/scratchpad/x",
])
def test_rest_of_tmp_is_the_sandboxes_own(path):
    # A private per-sandbox tmpfs: another session's tree or another uid's
    # root cannot exist in it, so there is nothing to scope.
    p = Path(path)
    assert _check_read_path(p, _ctx()).allowed
    assert _check_write_path(p, _ctx()).allowed


@pytest.mark.parametrize("path", [
    "/tmp/.claude.json",
    "/tmp/.claude/settings.json",
    "/tmp/.codex/config.toml",
    "/tmp/.codex/auth.json",
])
def test_cli_state_under_tmp_stays_protected(path):
    p = Path(path)
    assert not _check_read_path(p, _ctx()).allowed
    assert not _check_write_path(p, _ctx()).allowed


@pytest.mark.parametrize("path", [
    "/tmp/.claude/permission_gate.py",
    "/tmp/.codex/hooks.json",
    "/tmp/.codex/stdio_path_interceptor.py",
])
def test_hook_files_under_tmp_are_write_protected(path):
    p = Path(path)
    assert _check_read_path(p, _ctx()).allowed
    assert not _check_write_path(p, _ctx()).allowed


def test_no_session_id_still_gets_tmp():
    p = Path(f"{TREE}/scratchpad/x")
    assert _check_read_path(p, _ctx(sid="")).allowed
    assert _check_write_path(p, _ctx(sid="")).allowed


def test_tilde_is_the_sandbox_home():
    ctx = _ctx()
    d, _ = check_tool_access("Bash", {"command": "echo hi > ~/x.txt"}, ctx)
    assert d.allowed and d.permission_tier == "edit", d.reason
    d, _ = check_tool_access("Bash", {"command": "cat ~/.claude.json"}, ctx)
    assert not d.allowed and "protected" in d.reason


def test_remote_context_does_not_take_the_local_rule():
    # Remote gets the carve from path_policy_v2 (satellite-reported root);
    # the local rule asks the placement's isolates_with_bwrap.
    p = Path(f"{TREE}/scratchpad/x")
    ctx = _ctx(placement=placement.PlacementCapabilities(
        kind=placement.KIND_USER_REMOTE, home_dir="/home/dave",
        agents_dir="/home/dave/.oto-dock/agents"))
    assert not _check_read_path(p, ctx).allowed
    assert not _check_write_path(p, ctx).allowed


def test_env_write_inside_tree_stays_denied():
    d = _check_write_path(Path(f"{TREE}/scratchpad/.env"), _ctx())
    assert not d.allowed and ".env" in d.reason


def test_traversal_collapses_before_matching():
    # Five levels up from the scratchpad is the filesystem root: outside /tmp.
    d, _ = check_tool_access(
        "Write", {"file_path": f"{TREE}/scratchpad/../../../../../evil", "content": "x"},
        _ctx())
    assert not d.allowed


def test_agent_tree_under_tmp_keeps_the_role_matrix(monkeypatch, tmp_path):
    # The sandbox's /tmp shares only its name with the host's: a data dir
    # placed under /tmp (the test suite's is) must not open the agent tree.
    from auth import path_policy
    agents = (tmp_path / "agents").resolve()
    monkeypatch.setattr(path_policy, "_AGENTS_DIR", agents)
    assert str(agents).startswith(str(path_policy._SANDBOX_TMP))
    viewer = _ctx(role="viewer")
    assert not _check_write_path(agents / "personal-assistant" / "knowledge" / "x.md", viewer).allowed
    assert not _check_read_path(agents / "personal-assistant" / "config" / "p.md", viewer).allowed
    assert _check_write_path(Path("/tmp/scratch.txt"), viewer).allowed


def test_cross_user_deny_runs_before_the_carve():
    # A path under another user's dir that happens to name this session id.
    p = (_AGENTS / "personal-assistant" / "users" / "bob" / "claude-1" / SID / "x")
    assert not _check_read_path(p, _ctx(username="alice")).allowed
    assert not _check_write_path(p, _ctx(username="alice")).allowed


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
