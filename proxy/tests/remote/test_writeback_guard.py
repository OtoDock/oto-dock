"""Tests for the role-aware satellite→platform write-back guard.

`core.remote.file_sync.can_write_back` is the single source of truth for whether a
satellite session (by role + username) may write an agent-tree path BACK to the
platform. It gates BOTH the per-turn `file_changed` applier
(`core.remote.satellite_connection._apply_file_changed`) and the initial-sync
`diff_manifests` to_pull decision. It is the ONLY filesystem-write gate for
Codex remote sessions (no permission hooks), so the matrix below is a security
contract.
"""

import base64
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.remote.file_sync import can_write_back  # noqa: E402


# ---------------------------------------------------------------------------
# The write matrix (role × dir × outcome)
# ---------------------------------------------------------------------------

class TestCanWriteBackMatrix:
    # --- knowledge/ + config/ : owner-tier only ---
    @pytest.mark.parametrize("role,expected", [
        ("viewer", False), ("editor", False), ("manager", True), ("admin", True),
    ])
    def test_knowledge(self, role, expected):
        assert can_write_back("knowledge/ref.md", role, "alice") is expected

    @pytest.mark.parametrize("role,expected", [
        ("viewer", False), ("editor", False), ("manager", True), ("admin", True),
    ])
    def test_config_prompt(self, role, expected):
        assert can_write_back("config/prompt.md", role, "alice") is expected

    @pytest.mark.parametrize("role,expected", [
        ("viewer", False), ("editor", False), ("manager", True), ("admin", True),
    ])
    def test_config_context(self, role, expected):
        assert can_write_back("config/context/doc.md", role, "alice") is expected

    # --- workspace/ : editor tier and up ---
    @pytest.mark.parametrize("role,expected", [
        ("viewer", False), ("editor", True), ("manager", True), ("admin", True),
    ])
    def test_shared_workspace(self, role, expected):
        assert can_write_back("workspace/out.md", role, "alice") is expected

    # --- users/{own}/ : any role on own dir ---
    @pytest.mark.parametrize("role", ["viewer", "editor", "manager", "admin"])
    def test_own_user_dir(self, role):
        assert can_write_back("users/alice/workspace/a.md", role, "alice") is True
        assert can_write_back("users/alice/context/c.md", role, "alice") is True

    # --- users/{other}/ : never (even admin — stricter on remote) ---
    @pytest.mark.parametrize("role", ["viewer", "editor", "manager", "admin"])
    def test_other_user_dir_denied(self, role):
        assert can_write_back("users/bob/workspace/b.md", role, "alice") is False

    # --- Shared-only human chat: username set (attribution) but the MOUNT
    #     identity is blank — its mode has NO per-user dirs, so even the
    #     "own" users/ dir is denied, while owner config/knowledge curation
    #     (keyed on the REAL username) keeps working. ---
    def test_shared_only_mount_denies_own_user_dir_keeps_curation(self):
        assert can_write_back("users/alice/workspace/a.md", "manager", "alice",
                              mount_username="") is False
        assert can_write_back("config/prompt.md", "manager", "alice",
                              mount_username="") is True
        assert can_write_back("knowledge/ref.md", "manager", "alice",
                              mount_username="") is True
        assert can_write_back("workspace/out.md", "editor", "alice",
                              mount_username="") is True
        # User-scope session: mount == attribution → unchanged behavior.
        assert can_write_back("users/alice/workspace/a.md", "viewer", "alice",
                              mount_username="alice") is True

    # --- .claude / .codex machinery : never, any role, anywhere in path ---
    @pytest.mark.parametrize("path", [
        "users/alice/.claude/projects/h/sid.jsonl",
        "users/alice/.claude/settings.json",
        "workspace/.claude/settings.json",
        "users/alice/.codex/sessions/t.jsonl",
        ".claude/settings.json",
    ])
    @pytest.mark.parametrize("role", ["viewer", "editor", "manager", "admin"])
    def test_claude_codex_machinery_never(self, path, role):
        assert can_write_back(path, role, "alice") is False

    # --- agent-scoped sessions (username="") : role is manager/admin but they
    #     must NOT curate knowledge/config (parity with local bwrap: knowledge RO,
    #     config unmounted, workspace RW). ---
    @pytest.mark.parametrize("role", ["manager", "admin"])
    def test_agent_scope_cannot_write_knowledge_or_config(self, role):
        assert can_write_back("knowledge/ref.md", role, "") is False
        assert can_write_back("config/prompt.md", role, "") is False

    @pytest.mark.parametrize("role", ["manager", "admin"])
    def test_agent_scope_can_write_shared_workspace(self, role):
        # Agent-scoped tasks DO own the shared workspace (their CWD).
        assert can_write_back("workspace/out.md", role, "") is True

    # --- fail-closed: empty / unknown / root-level / missing username ---
    def test_fail_closed(self):
        assert can_write_back("", "manager", "alice") is False
        assert can_write_back("junk.md", "manager", "alice") is False
        assert can_write_back("randomdir/x", "admin", "alice") is False
        # users/ path with no session username → drop
        assert can_write_back("users/alice/workspace/a.md", "manager", "") is False
        # unknown role on an owner dir → not owner-tier → drop
        assert can_write_back("knowledge/x.md", "", "alice") is False


# ---------------------------------------------------------------------------
# Canonical rel-path gate (shared with pull_through / push_back)
# ---------------------------------------------------------------------------

class TestIsCanonicalRelPath:
    """`is_canonical_rel_path` is the syntactic front door for every
    wire/hook-supplied agent-tree path — non-canonical forms (backslash,
    dot segments, absolute-ish drive-letter junk) must be rejected before
    any caller mkdirs or resolves them."""

    @pytest.mark.parametrize("path", [
        "workspace/out.md",
        "users/alice/workspace/a.md",
        "config/context/doc.md",
        "knowledge/ref.md",
    ])
    def test_canonical_accepted(self, path):
        from core.remote.file_sync import is_canonical_rel_path
        assert is_canonical_rel_path(path) is True

    @pytest.mark.parametrize("path", [
        "",
        "/workspace/out.md",                 # leading slash
        "workspace/../config/x",             # dot segment
        "workspace/./x",
        "workspace\\out.md",                 # backslash
        "workspace/a\x00b",                  # NUL
        # Mistranslated Windows satellite-host absolute — stays in-tree on
        # Linux ("C:" is a plain dir name), so relative_to can't catch it;
        # this gate is what prevents the junk C:/ dir chain (2026-06-04).
        "C:/Users/alice/OtoDock/agents/personal-assistant/users/alice/workspace/x.png",
        "junk.md",                           # root-level file
        "randomdir/x",                       # unknown top-level scope
    ])
    def test_non_canonical_rejected(self, path):
        from core.remote.file_sync import is_canonical_rel_path
        assert is_canonical_rel_path(path) is False


# ---------------------------------------------------------------------------
# Wire-level: the per-turn applier actually drops denied write-backs
# ---------------------------------------------------------------------------

class TestApplierGuard:
    async def _run(self, role, username, path, action="write",
                   mount_username=None):
        """Drive _apply_file_changed with a stubbed session security ctx and
        return whether the inner apply_incoming_file was invoked."""
        from core.remote.satellite_connection import SatelliteConnectionManager

        cm = SatelliteConnectionManager()
        msg = {
            "agent_slug": "my-agent",
            "path": path,
            "action": action,
            "session_id": "sess-1",
            "content_b64": base64.b64encode(b"x").decode() if action == "write" else "",
        }
        sec = SimpleNamespace(role=role, username=username)
        if mount_username is not None:
            sec.mount_username = mount_username
        called = {"n": 0}

        def _fake_apply(*a, **kw):
            called["n"] += 1

        with patch("core.session.session_state.get_session_security", return_value=sec), \
             patch("core.remote.file_sync.apply_incoming_file", _fake_apply):
            await cm._apply_file_changed("machine-1", msg)
        return called["n"] > 0

    @pytest.mark.asyncio
    async def test_viewer_knowledge_write_dropped(self):
        assert await self._run("viewer", "alice", "knowledge/x.md") is False

    @pytest.mark.asyncio
    async def test_editor_knowledge_write_dropped(self):
        assert await self._run("editor", "alice", "knowledge/x.md") is False

    @pytest.mark.asyncio
    async def test_manager_knowledge_write_applied(self):
        assert await self._run("manager", "alice", "knowledge/x.md") is True

    @pytest.mark.asyncio
    async def test_viewer_own_userdir_applied(self):
        assert await self._run("viewer", "alice", "users/alice/workspace/a.md") is True

    @pytest.mark.asyncio
    async def test_shared_only_session_userdir_dropped(self):
        """A Shared-only chat's ctx carries mount_username == "" — its
        users/ write-backs are dropped even for the "own" dir."""
        assert await self._run(
            "manager", "alice", "users/alice/workspace/a.md",
            mount_username="",
        ) is False

    @pytest.mark.asyncio
    async def test_viewer_shared_workspace_dropped(self):
        assert await self._run("viewer", "alice", "workspace/out.md") is False

    @pytest.mark.asyncio
    async def test_viewer_knowledge_delete_dropped(self):
        # A non-owner must not be able to DELETE a platform knowledge file.
        assert await self._run("viewer", "alice", "knowledge/x.md", action="delete") is False

    @pytest.mark.asyncio
    async def test_missing_session_ctx_dropped(self):
        # Fail-closed: no authenticated security context → drop.
        from core.remote.satellite_connection import SatelliteConnectionManager
        cm = SatelliteConnectionManager()
        msg = {
            "agent_slug": "my-agent", "path": "knowledge/x.md",
            "action": "write", "session_id": "sess-x",
            "content_b64": base64.b64encode(b"x").decode(),
        }
        called = {"n": 0}
        with patch("core.session.session_state.get_session_security", return_value=None), \
             patch("core.remote.file_sync.apply_incoming_file", lambda *a, **k: called.__setitem__("n", called["n"] + 1)):
            await cm._apply_file_changed("machine-1", msg)
        assert called["n"] == 0


class TestDenialLogLevel:
    """Engine-machinery denials are routine (engines rewrite their own
    runtime state every turn — codex's models_cache.json tripped a WARNING
    per turn, observed live 2026-07-09) and log at DEBUG; every other
    denial — role/scope violations, missing SecurityContext — stays WARNING
    because it's the actual signal this log exists for."""

    async def _run_capturing(self, caplog, path, sec):
        import logging

        from core.remote.satellite_connection import SatelliteConnectionManager

        cm = SatelliteConnectionManager()
        msg = {
            "agent_slug": "my-agent", "path": path,
            "action": "write", "session_id": "sess-1",
            "content_b64": base64.b64encode(b"x").decode(),
        }
        called = {"n": 0}
        with caplog.at_level(logging.DEBUG, logger="claude-proxy.satellite"), \
             patch("core.session.session_state.get_session_security", return_value=sec), \
             patch("core.remote.file_sync.apply_incoming_file",
                   lambda *a, **k: called.__setitem__("n", called["n"] + 1)):
            await cm._apply_file_changed("machine-1", msg)
        assert called["n"] == 0  # denied either way
        return [(r.levelname, r.getMessage()) for r in caplog.records
                if "write-back" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_engine_machinery_denial_is_debug(self, caplog):
        sec = SimpleNamespace(role="admin", username="alice")
        records = await self._run_capturing(
            caplog, "users/alice/.codex/models_cache.json", sec)
        assert records and all(lvl == "DEBUG" for lvl, _ in records)

    @pytest.mark.asyncio
    async def test_role_denial_stays_warning(self, caplog):
        sec = SimpleNamespace(role="viewer", username="alice")
        records = await self._run_capturing(caplog, "knowledge/x.md", sec)
        assert records and all(lvl == "WARNING" for lvl, _ in records)

    @pytest.mark.asyncio
    async def test_missing_ctx_on_engine_path_stays_warning(self, caplog):
        # No SecurityContext is anomalous even on a machinery path — the
        # quiet lane is only for authenticated sessions' routine engine noise.
        records = await self._run_capturing(
            caplog, "users/alice/.codex/models_cache.json", None)
        assert records and all(lvl == "WARNING" for lvl, _ in records)
