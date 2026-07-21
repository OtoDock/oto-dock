"""Integration tests for the un-short-circuit of
``check_tool_access`` and the per-tool target revocation check.

These verify the wiring between ``auth.path_policy.check_tool_access``
and ``services.path_policy_v2.resolve_path_for_session``: native tools
(Read / Edit / Glob) on remote sessions now go through the same path
policy as MCP tool args.
"""

import sys
from unittest.mock import patch


from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from auth.path_policy import SecurityContext, check_tool_access  # noqa: E402
from services.path_policy_v2 import check_target_still_valid  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _user_remote_ctx(
    *,
    allow_full_fs: bool = False,
    home_dir: str = "/home/dave",
    target_agents_dir: str = "/home/dave/.oto-dock/agents",
    machine_id: str = "machine-abc",
    agent: str = "my-agent",
    role: str = "manager",
) -> SecurityContext:
    return SecurityContext(
        role=role,
        username="dave",
        agent=agent,
        is_admin_agent=False,
        target_kind="user_remote",
        target_label="MacBook Pro",
        target_agents_dir=target_agents_dir,
        target_machine_id=machine_id,
        target_home_dir=home_dir,
        target_allow_full_fs=allow_full_fs,
    )


def _user_remote_ctx_windows(
    *,
    allow_full_fs: bool = False,
    role: str = "manager",
    agent: str = "my-agent",
) -> SecurityContext:
    """Windows satellite ctx — agents_dir/home are drive-rooted so
    ``_infer_target_os`` resolves ``target_os == "windows"`` and the
    Windows-only native-tools nudge is exercised."""
    return SecurityContext(
        role=role,
        username="dave",
        agent=agent,
        is_admin_agent=False,
        target_kind="user_remote",
        target_label="Windows laptop",
        target_agents_dir="C:/Users/frank/OtoDock/agents",
        target_machine_id="machine-win",
        target_home_dir="C:/Users/frank",
        target_allow_full_fs=allow_full_fs,
    )


def _admin_remote_ctx(
    *,
    allow_full_fs: bool = True,
    role: str = "admin",
) -> SecurityContext:
    return SecurityContext(
        role=role,
        username="dave",
        agent="ops-bot",
        is_admin_agent=True,
        target_kind="admin_remote",
        target_label="ops-vm",
        target_agents_dir="/home/svc/.oto-dock/agents",
        target_machine_id="machine-xyz",
        target_home_dir="/home/svc",
        target_allow_full_fs=allow_full_fs,
    )


# ---------------------------------------------------------------------------
# check_tool_access delegates to path_policy_v2 on remote
# ---------------------------------------------------------------------------

class TestCheckToolAccessRemoteDelegation:
    def test_home_path_allowed_user_remote(self):
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "Read", {"file_path": "/home/dave/Desktop/foo.png"}, ctx,
        )
        assert decision.allowed

    def test_etc_rejected_home_only(self):
        ctx = _user_remote_ctx(allow_full_fs=False)
        decision, _ = check_tool_access(
            "Read", {"file_path": "/etc/hosts"}, ctx,
        )
        assert not decision.allowed
        assert "home" in decision.reason.lower()

    def test_etc_allowed_with_full_fs(self):
        ctx = _user_remote_ctx(allow_full_fs=True)
        decision, _ = check_tool_access(
            "Read", {"file_path": "/etc/hosts"}, ctx,
        )
        assert decision.allowed

    def test_admin_remote_system_path_allowed(self):
        # The early-return for is_admin_agent + admin role is REACHED
        # BEFORE the remote branch, so let's pick a manager on
        # admin_remote to exercise the new branch.
        ctx = _admin_remote_ctx(role="manager")
        # is_admin_agent stays True, but role!=admin → falls through
        # to remote branch.
        decision, _ = check_tool_access(
            "Read", {"file_path": "/var/log/syslog"}, ctx,
        )
        assert decision.allowed

    def test_admin_remote_no_full_fs_rejects_etc(self):
        ctx = _admin_remote_ctx(allow_full_fs=False, role="manager")
        decision, _ = check_tool_access(
            "Read", {"file_path": "/etc/sudoers"}, ctx,
        )
        assert not decision.allowed

    def test_empty_path_allows(self):
        # Empty path = tool will error naturally on its own; we don't
        # reject upstream.
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access("Read", {"file_path": ""}, ctx)
        assert decision.allowed

    def test_bash_on_remote_skipped(self):
        # Bash has its own checks earlier in check_tool_access; the
        # remote branch shouldn't second-guess.
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "Bash", {"command": "ls /home/dave"}, ctx,
        )
        # Bash's own check is what matters here — the test asserts the
        # remote branch didn't reject simply for being remote.
        # (Bash check may pass or deny based on its own rules; we just
        # verify the remote branch doesn't crash.)
        assert decision is not None

    def test_mcp_tool_on_remote_allowed(self):
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "mcp__display__display_images", {"images": []}, ctx,
        )
        assert decision.allowed

    def test_sandbox_virtual_inside_tree_still_rbac_checked(self):
        # Viewer asking to WRITE workspace should still be denied — the
        # role-based RBAC still applies inside the synced tree.
        ctx = _user_remote_ctx(role="viewer")
        decision, _ = check_tool_access(
            "Write",
            {"file_path": "/workspace/foo.md"},
            ctx,
        )
        # Viewer cannot write to /workspace/
        assert not decision.allowed

    def test_sandbox_virtual_workspace_read_allowed_viewer(self):
        ctx = _user_remote_ctx(role="viewer")
        decision, _ = check_tool_access(
            "Read",
            {"file_path": "/workspace/foo.md"},
            ctx,
        )
        # Viewer can read /workspace/
        assert decision.allowed

    def test_satellite_absolute_inside_synced_tree_uses_sandbox_rbac(self):
        # The LLM passes the satellite-host equivalent of /users/dave/...
        # The translator brings it back to sandbox-virtual; role-based
        # RBAC then applies.
        ctx = _user_remote_ctx(role="viewer")
        absolute = (
            "/home/dave/.oto-dock/agents/my-agent/"
            "users/dave/workspace/foo.md"
        )
        decision, _ = check_tool_access(
            "Read", {"file_path": absolute}, ctx,
        )
        # Viewer can read their own user dir
        assert decision.allowed


# ---------------------------------------------------------------------------
# Windows satellite — host paths pass through untouched (regression: the old
# deny-nudge fired on EVERY in-tree path, looping plan-mode writes with an
# unsatisfiable "use C:/x instead of C:/x" message). Sandbox-virtual paths are
# now REWRITTEN (updated_input) instead of denied — see
# TestNativeToolPathRewrite below.
# ---------------------------------------------------------------------------

class TestWindowsNativeToolsNudge:
    def test_host_plan_write_backslash_allowed(self):
        # Plan-mode plan file, satellite-host path with BACKSLASHES (the
        # form the CLI actually passes on Windows). Must be allowed.
        ctx = _user_remote_ctx_windows()
        path = (
            "C:\\Users\\frank\\OtoDock\\agents\\my-agent\\"
            "users\\dave\\.claude\\plans\\humming-weaving-gosling.md"
        )
        decision, _ = check_tool_access("Write", {"file_path": path}, ctx)
        assert decision.allowed, decision.reason

    def test_host_plan_write_forwardslash_allowed(self):
        ctx = _user_remote_ctx_windows()
        path = (
            "C:/Users/frank/OtoDock/agents/my-agent/"
            "users/dave/.claude/plans/humming-weaving-gosling.md"
        )
        decision, _ = check_tool_access("Write", {"file_path": path}, ctx)
        assert decision.allowed, decision.reason

    def test_host_workspace_write_allowed(self):
        # General in-tree host write (not just plans) is allowed on Windows.
        ctx = _user_remote_ctx_windows()
        path = "C:/Users/frank/OtoDock/agents/my-agent/workspace/out.txt"
        decision, _ = check_tool_access("Write", {"file_path": path}, ctx)
        assert decision.allowed, decision.reason

    def test_sandbox_virtual_rewritten_not_denied(self):
        # A sandbox-virtual path on Windows used to deny with an OS-native
        # steer (it would silently miswrite to a drive-rooted C:\users\...).
        # Now it's ALLOWED with the input rewritten to the satellite-host
        # form — the hook returns it as PreToolUse updatedInput.
        ctx = _user_remote_ctx_windows()
        decision, _ = check_tool_access(
            "Write",
            {"file_path": "/users/dave/.claude/plans/x.md", "content": "p"},
            ctx,
        )
        assert decision.allowed, decision.reason
        assert decision.updated_input == {
            "file_path": (
                "C:/Users/frank/OtoDock/agents/my-agent/"
                "users/dave/.claude/plans/x.md"
            ),
            "content": "p",
        }

    def test_host_write_viewer_still_rbac_denied(self):
        # Removing the spurious nudge must not bypass RBAC: a viewer writing
        # to /workspace/ (host form) is still denied by role policy.
        ctx = _user_remote_ctx_windows(role="viewer")
        path = "C:/Users/frank/OtoDock/agents/my-agent/workspace/out.txt"
        decision, _ = check_tool_access("Write", {"file_path": path}, ctx)
        assert not decision.allowed


# ---------------------------------------------------------------------------
# Native-tool path rewrite (sandbox-virtual / ~ → satellite-host) — the
# permission hook returns PathDecision.updated_input as PreToolUse
# updatedInput so the tool executes against the real path on the satellite.
# ---------------------------------------------------------------------------

class TestNativeToolPathRewrite:
    def test_workspace_read_rewritten_linux(self):
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "Read", {"file_path": "/workspace/notes.md"}, ctx,
        )
        assert decision.allowed, decision.reason
        assert decision.updated_input == {
            "file_path": "/home/dave/.oto-dock/agents/my-agent/workspace/notes.md",
        }

    def test_knowledge_read_rewritten(self):
        # The reported field bug: `Read /knowledge/memory/dev-environment.md`
        # ENOENT'd on a Linux satellite and the agent had to guess the
        # OS-native tree. Now the input is rewritten to the synced path.
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "Read", {"file_path": "/knowledge/memory/dev-environment.md"}, ctx,
        )
        assert decision.allowed, decision.reason
        assert decision.updated_input == {
            "file_path": (
                "/home/dave/.oto-dock/agents/my-agent/"
                "knowledge/memory/dev-environment.md"
            ),
        }

    def test_other_input_keys_preserved(self):
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "Read",
            {"file_path": "/workspace/big.log", "offset": 100, "limit": 50},
            ctx,
        )
        assert decision.allowed
        assert decision.updated_input == {
            "file_path": "/home/dave/.oto-dock/agents/my-agent/workspace/big.log",
            "offset": 100,
            "limit": 50,
        }

    def test_glob_path_key_rewritten(self):
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "Glob", {"pattern": "**/*.md", "path": "/workspace/docs"}, ctx,
        )
        assert decision.allowed
        assert decision.updated_input == {
            "pattern": "**/*.md",
            "path": "/home/dave/.oto-dock/agents/my-agent/workspace/docs",
        }

    def test_tilde_expanded_for_file_tools(self):
        # `~` is shell syntax the native file tools don't expand — the
        # rewrite hands them the real home-rooted path.
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "Read", {"file_path": "~/Desktop/shot.png"}, ctx,
        )
        assert decision.allowed, decision.reason
        assert decision.updated_input == {
            "file_path": "/home/dave/Desktop/shot.png",
        }

    def test_satellite_host_path_not_rewritten(self):
        # An already-native path passes through untouched — no gratuitous
        # updatedInput echoes.
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "Read", {"file_path": "/home/dave/Desktop/foo.png"}, ctx,
        )
        assert decision.allowed
        assert decision.updated_input is None

    def test_denied_paths_carry_no_rewrite(self):
        # RBAC deny still wins — a viewer writing /knowledge gets a deny
        # with no updated_input attached.
        ctx = _user_remote_ctx(role="viewer")
        decision, _ = check_tool_access(
            "Write", {"file_path": "/knowledge/notes.md", "content": "x"}, ctx,
        )
        assert not decision.allowed
        assert decision.updated_input is None

    def test_notebook_path_policed_and_rewritten(self):
        # NotebookEdit names its path arg `notebook_path` — it now gets the
        # same remote policy + rewrite as Write/Edit.
        ctx = _user_remote_ctx()
        decision, _ = check_tool_access(
            "NotebookEdit",
            {"notebook_path": "/workspace/nb.ipynb", "new_source": "x"},
            ctx,
        )
        assert decision.allowed, decision.reason
        assert decision.updated_input == {
            "notebook_path": "/home/dave/.oto-dock/agents/my-agent/workspace/nb.ipynb",
            "new_source": "x",
        }

    def test_notebook_write_viewer_denied(self):
        ctx = _user_remote_ctx(role="viewer")
        decision, _ = check_tool_access(
            "NotebookEdit",
            {"notebook_path": "/workspace/nb.ipynb", "new_source": "x"},
            ctx,
        )
        assert not decision.allowed

    def test_local_sessions_never_rewrite(self):
        # Local sandbox mounts the virtual roots for real — the input must
        # pass through untouched.
        ctx = SecurityContext(
            role="manager", username="dave", agent="my-agent",
            is_admin_agent=False,
        )
        decision, _ = check_tool_access(
            "Read", {"file_path": "/workspace/notes.md"}, ctx,
        )
        assert decision.allowed
        assert decision.updated_input is None


# ---------------------------------------------------------------------------
# Per-tool target revocation
# ---------------------------------------------------------------------------

class TestCheckTargetStillValid:
    def test_local_session_short_circuits(self):
        ctx = SecurityContext(
            role="manager", username="alice", agent="a", is_admin_agent=False,
        )
        # No DB access needed; local always valid.
        assert check_target_still_valid(ctx) == ""

    def test_remote_no_machine_id_short_circuits(self):
        # A session with target_kind set but no machine_id field
        # (transition state). Treat as valid to avoid false positives.
        ctx = SecurityContext(
            role="manager", username="a", agent="b", is_admin_agent=False,
            target_kind="user_remote",
            target_machine_id="",
        )
        assert check_target_still_valid(ctx) == ""

    def test_remote_machine_exists_valid(self):
        ctx = _user_remote_ctx()
        with patch(
            "storage.remote_store.get_remote_machine",
            return_value={"id": "machine-abc", "name": "MacBook"},
        ):
            assert check_target_still_valid(ctx) == ""

    def test_remote_machine_missing_revoked(self):
        ctx = _user_remote_ctx()
        with patch(
            "storage.remote_store.get_remote_machine",
            return_value=None,
        ):
            reason = check_target_still_valid(ctx)
            assert reason
            assert "unpaired" in reason.lower() or "remote" in reason.lower()


class TestRemoteBashHonorsPolicy:
    """Remote Bash path args route through path_policy_v2 (home /
    full-FS for satellite-host, RBAC for in-tree) — NOT the proxy-local
    agent-tree check. So ``cat ~/Desktop/x`` behaves like ``Read ~/Desktop/x``.
    """

    def _bash(self, ctx, command):
        return check_tool_access("Bash", {"command": command}, ctx)[0]

    def test_home_path_allowed_when_full_fs_off(self):
        ctx = _user_remote_ctx(allow_full_fs=False)  # home=/home/dave
        assert self._bash(ctx, "cat /home/dave/Desktop/notes.txt").allowed

    def test_outside_home_denied_when_full_fs_off(self):
        ctx = _user_remote_ctx(allow_full_fs=False)
        d = self._bash(ctx, "cat /etc/passwd")
        assert not d.allowed
        assert "home" in d.reason.lower() or "full" in d.reason.lower()

    def test_outside_home_allowed_when_full_fs_on(self):
        ctx = _user_remote_ctx(allow_full_fs=True)
        assert self._bash(ctx, "cat /etc/passwd").allowed

    def test_workspace_path_allowed(self):
        ctx = _user_remote_ctx(allow_full_fs=False)
        assert self._bash(ctx, "cat /workspace/report.txt").allowed

    def test_windows_desktop_allowed_when_full_fs_off(self):
        # The exact bug from the user's log: C:/Users/frank/Desktop under home.
        ctx = _user_remote_ctx_windows(allow_full_fs=False)
        assert self._bash(
            ctx, 'cat "C:/Users/frank/Desktop/test-document.docx"',
        ).allowed

    def test_windows_outside_home_denied_when_full_fs_off(self):
        ctx = _user_remote_ctx_windows(allow_full_fs=False)
        assert not self._bash(
            ctx, "cat C:/Windows/System32/drivers/etc/hosts",
        ).allowed

    def test_write_redirect_outside_home_denied_when_full_fs_off(self):
        ctx = _user_remote_ctx(allow_full_fs=False)
        assert not self._bash(ctx, "echo hi > /etc/evil.conf").allowed

    def test_viewer_rbac_still_enforced_in_tree(self):
        # In-tree paths keep per-role RBAC even on remote: a viewer can't
        # write /knowledge (owner-only).
        ctx = _user_remote_ctx(allow_full_fs=False, role="viewer")
        assert not self._bash(
            ctx, "cp /workspace/a.txt /knowledge/b.txt",
        ).allowed


class TestLiveAllowFullFsRefresh:
    """Flipping allow_full_fs refreshes the cached SecurityContext of
    live sessions on that machine (frozen dataclass → dataclasses.replace)."""

    def test_refresh_updates_only_matching_machine(self):
        from core.session import session_state
        on_machine = _user_remote_ctx(allow_full_fs=False, machine_id="m-live")
        other_machine = _user_remote_ctx(allow_full_fs=False, machine_id="m-other")
        session_state.set_session_security("sess-live", on_machine)
        session_state.set_session_security("sess-other", other_machine)
        try:
            n = session_state.refresh_target_allow_full_fs("m-live", True)
            assert n == 1
            assert session_state.get_session_security("sess-live").target_allow_full_fs is True
            # Untouched: different machine.
            assert session_state.get_session_security("sess-other").target_allow_full_fs is False
            # Idempotent: re-applying the same value updates nothing.
            assert session_state.refresh_target_allow_full_fs("m-live", True) == 0
        finally:
            session_state._session_security.pop("sess-live", None)
            session_state._session_security.pop("sess-other", None)
