"""Snapshot-style smoke tests for the 4 ``# Execution
Environment`` prompt variants.

Each variant is exercised with a minimal SecurityContext fixture and the
generated section is asserted against expected substrings (not full
byte-for-byte snapshots — wording can drift slightly without breaking
LLM comprehension).
"""

import sys


from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from auth.path_policy import (  # noqa: E402
    SecurityContext,
    _build_execution_environment_section,
)


def _ctx(
    target_kind: str,
    *,
    target_label: str = "the-machine",
    allow_full_fs: bool = False,
    home_dir: str = "/home/alice",
    os_user: str = "alice",
    user_dirs: dict | None = None,
) -> SecurityContext:
    return SecurityContext(
        role="manager", username="alice", agent="my-agent",
        is_admin_agent=False,
        target_kind=target_kind,
        target_label=target_label,
        target_machine_id="m1" if target_kind != "local" else "",
        target_home_dir=home_dir if target_kind != "local" else "",
        target_allow_full_fs=allow_full_fs,
        target_os_user=os_user if target_kind != "local" else "",
        target_user_dirs=user_dirs or {},
    )


class TestLocalSandbox:
    def test_contains_bwrap_sandbox(self):
        text = _build_execution_environment_section(_ctx("local"))
        assert "local bwrap kernel sandbox" in text
        # Local should NOT mention home dirs or allow_full_fs.
        assert "Filesystem access" not in text
        assert "Full filesystem access" not in text


class TestUserRemoteHomeOnly:
    def test_mentions_home_dir(self):
        ctx = _ctx("user_remote", allow_full_fs=False,
                   home_dir="/home/alice", os_user="alice")
        text = _build_execution_environment_section(ctx)
        assert "/home/alice" in text
        assert "alice" in text  # os_user surfaced
        assert "limited to the agent's synced tree" in text

    def test_lists_user_dirs(self):
        ctx = _ctx("user_remote", allow_full_fs=False,
                   user_dirs={
                       "desktop": "/home/alice/Desktop",
                       "downloads": "/home/alice/Downloads",
                       "documents": "/home/alice/Documents",
                   })
        text = _build_execution_environment_section(ctx)
        assert "Desktop: `/home/alice/Desktop`" in text
        assert "Downloads: `/home/alice/Downloads`" in text
        assert "Documents: `/home/alice/Documents`" in text

    def test_mentions_opt_in_path(self):
        ctx = _ctx("user_remote", allow_full_fs=False)
        text = _build_execution_environment_section(ctx)
        assert "Full filesystem access" in text
        assert "Settings" in text


class TestUserRemoteFullFs:
    def test_admits_full_fs_wording(self):
        ctx = _ctx("user_remote", allow_full_fs=True)
        text = _build_execution_environment_section(ctx)
        assert "full filesystem access" in text.lower()
        # Should NOT contain the home-only "limited to" wording.
        assert "limited to the agent's synced tree" not in text


class TestAdminRemoteFullFs:
    def test_default_admin_path(self):
        ctx = _ctx("admin_remote", allow_full_fs=True,
                   target_label="ops-vm", home_dir="/home/svc",
                   os_user="svc")
        text = _build_execution_environment_section(ctx)
        assert "admin-paired" in text or "admin paired" in text.lower()
        assert "Filesystem access**: full" in text
        # Admin-tier bash mentioned.
        assert "admin tier" in text.lower() or "systemctl" in text


class TestAdminRemoteOptedOut:
    def test_explicit_opt_out_messaging(self):
        ctx = _ctx("admin_remote", allow_full_fs=False)
        text = _build_execution_environment_section(ctx)
        assert "explicitly opted this machine out" in text.lower()
        # Admin-pairing still grants admin-tier bash even when not full FS.
        assert "admin tier" in text.lower()


class TestMcpTranslationGuidance:
    def test_remote_mentions_framework_translation(self):
        ctx = _ctx("user_remote", allow_full_fs=False)
        text = _build_execution_environment_section(ctx)
        # The contract is: framework translates declared
        # tool_arg_paths automatically. No per-MCP enumeration — a
        # hardcoded list goes stale on every manifest change.
        assert "translates them automatically" in text
        assert "display-mcp" not in text

    def test_local_no_translation_guidance(self):
        # Local sandbox has bwrap mapping — no framework translation
        # is needed (or mentioned).
        text = _build_execution_environment_section(_ctx("local"))
        assert "translates them automatically" not in text

    def test_remote_native_tool_path_guidance(self):
        # Read/Write/Glob/Grep accept sandbox-virtual / ~ paths on remote
        # satellites (the permission hook rewrites them) — the prompt says
        # so, while carving out Edit/NotebookEdit (their validation runs
        # before hooks, so no rewrite can reach them) and steering shell
        # COMMANDS to OS-native paths (nothing rewrites inside a Bash
        # command string).
        ctx = _ctx("user_remote", allow_full_fs=False)
        text = _build_execution_environment_section(ctx)
        assert "Native file tools" in text
        assert "rewrites the path to its satellite location" in text
        assert "`Edit` and `NotebookEdit` do NOT" in text
        assert "OS-native paths ONLY" in text
