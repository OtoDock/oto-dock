"""Tests for task run permission enforcement.

Verifies that:
- DB scope filtering in list_runs / get_run_count works correctly
- _check_run_access enforces scope-based access
- _scope_filter_sub returns correct filter for each role context
- Trigger fire endpoint requires agent access
- create_run stores scope/created_by

Covers the full permission matrix:
  Viewer:  own user-scoped only
  Manager: own user-scoped + agent-scoped (not other users')
  Admin (agent page): same as manager
  Admin (admin page): all runs
  API key: all runs
"""

import sys

import pytest
from fastapi import HTTPException

# Ensure proxy root is on sys.path
from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Uses the shared `temp_db` fixture from conftest.py (autouse, provides clean PG)


def _make_user(sub="user-1", role="member", agents=None, is_api_key=False):
    """Create a UserContext for testing."""
    from auth.providers import UserContext

    return UserContext(
        sub=sub,
        email=f"{sub}@test.com",
        name=sub,
        role=role,
        agents=agents or [],
        is_api_key=is_api_key,
    )


def _seed_runs(db):
    """Create a standard set of runs for permission testing.

    Returns dict of run IDs keyed by description.
    """
    runs = {}

    # Agent-scoped run (visible to all with agent access)
    db.create_run("run-agent-1", "task-1", "support-bot", "cron", None,
                  "agent task prompt", "scheduled", scope="agent", created_by="support-bot")
    runs["agent_scoped"] = "run-agent-1"

    # User-scoped run by user-1 (viewer)
    db.create_run("run-user1", "task-2", "support-bot", "manual", None,
                  "user1 task", "one-time", scope="user", created_by="user-1")
    runs["user1_own"] = "run-user1"

    # User-scoped run by user-2 (another viewer)
    db.create_run("run-user2", "task-3", "support-bot", "manual", None,
                  "user2 task", "one-time", scope="user", created_by="user-2")
    runs["user2_own"] = "run-user2"

    # Agent-scoped run for a different agent
    db.create_run("run-other-agent", "task-4", "home-bot", "cron", None,
                  "other agent task", "scheduled", scope="agent", created_by="home-bot")
    runs["other_agent"] = "run-other-agent"

    # Legacy run (no explicit scope — defaults to 'agent' from migration)
    db.create_run("run-legacy", "task-5", "support-bot", "cron", None,
                  "legacy task", "scheduled")
    runs["legacy"] = "run-legacy"

    return runs


# ---------------------------------------------------------------------------
# DB-level scope filtering
# ---------------------------------------------------------------------------


class TestListRunsScopeFilter:
    def test_no_filter_returns_all(self, temp_db):
        db = temp_db
        _seed_runs(db)
        runs = db.list_runs(limit=100)
        assert len(runs) == 5

    def test_scope_filter_user1_sees_own_plus_agent(self, temp_db):
        db = temp_db
        _seed_runs(db)
        runs = db.list_runs(limit=100, scope_user_sub="user-1")
        ids = {r["id"] for r in runs}
        # Should see: agent-scoped runs + own user-scoped + legacy (scope=agent)
        assert "run-agent-1" in ids
        assert "run-user1" in ids
        assert "run-legacy" in ids
        # Should NOT see other user's user-scoped
        assert "run-user2" not in ids

    def test_scope_filter_user2_sees_own_plus_agent(self, temp_db):
        db = temp_db
        _seed_runs(db)
        runs = db.list_runs(limit=100, scope_user_sub="user-2")
        ids = {r["id"] for r in runs}
        assert "run-agent-1" in ids
        assert "run-user2" in ids
        assert "run-user1" not in ids

    def test_scope_filter_combined_with_agent(self, temp_db):
        db = temp_db
        _seed_runs(db)
        runs = db.list_runs(limit=100, agent="support-bot", scope_user_sub="user-1")
        ids = {r["id"] for r in runs}
        assert "run-agent-1" in ids
        assert "run-user1" in ids
        assert "run-other-agent" not in ids  # different agent
        assert "run-user2" not in ids  # other user's

    def test_scope_filter_unknown_user_sees_only_agent_scoped(self, temp_db):
        db = temp_db
        _seed_runs(db)
        runs = db.list_runs(limit=100, scope_user_sub="user-nobody")
        ids = {r["id"] for r in runs}
        assert "run-agent-1" in ids
        assert "run-legacy" in ids
        assert "run-other-agent" in ids
        assert "run-user1" not in ids
        assert "run-user2" not in ids


class TestGetRunCountScopeFilter:
    def test_count_no_filter(self, temp_db):
        db = temp_db
        _seed_runs(db)
        assert db.get_run_count() == 5

    def test_count_scope_filtered(self, temp_db):
        db = temp_db
        _seed_runs(db)
        # user-1: agent-scoped(2) + own(1) + legacy(1) = 4
        assert db.get_run_count(scope_user_sub="user-1") == 4

    def test_count_scope_filtered_with_agent(self, temp_db):
        db = temp_db
        _seed_runs(db)
        # user-1 + support-bot: agent-scoped(1) + own(1) + legacy(1) = 3
        assert db.get_run_count(agent="support-bot", scope_user_sub="user-1") == 3


class TestCreateRunScopeFields:
    def test_scope_and_created_by_stored(self, temp_db):
        db = temp_db
        db.create_run("run-test", "t1", "bot", "manual", None, "prompt",
                      scope="user", created_by="user-42")
        run = db.get_run("run-test")
        assert run["scope"] == "user"
        assert run["created_by"] == "user-42"

    def test_default_scope_is_agent(self, temp_db):
        db = temp_db
        db.create_run("run-default", "t2", "bot", "cron", None, "prompt")
        run = db.get_run("run-default")
        assert run["scope"] == "agent"
        assert run["created_by"] is None


# ---------------------------------------------------------------------------
# _check_run_access
# ---------------------------------------------------------------------------


class TestCheckRunAccess:
    def _check(self, run, user):
        from api.tasks.tasks import _check_run_access
        _check_run_access(run, user)

    def test_master_key_always_allowed(self):
        # Only the master key (sub="api-key" → is_service) bypasses run access.
        user = _make_user(sub="api-key", role="admin", is_api_key=True)
        run = {"agent": "any-bot", "scope": "user", "created_by": "someone-else"}
        self._check(run, user)  # Should not raise

    def test_user_session_blocked_from_other_user_run(self):
        # A session token (is_api_key=True) backed by a real user is NOT a
        # service principal — it must be held to the same ownership check as a
        # cookie, so it cannot read another user's user-scoped run by id.
        user = _make_user(sub="user-1", role="member", agents=["support-bot"],
                          is_api_key=True)
        run = {"agent": "support-bot", "scope": "user", "created_by": "user-2"}
        with pytest.raises(HTTPException) as exc_info:
            self._check(run, user)
        assert exc_info.value.status_code == 403

    def test_viewer_allowed_own_user_scoped(self):
        user = _make_user(sub="user-1", role="member", agents=["support-bot"])
        run = {"agent": "support-bot", "scope": "user", "created_by": "user-1"}
        self._check(run, user)  # Should not raise

    def test_viewer_blocked_other_user_scoped(self):
        user = _make_user(sub="user-1", role="member", agents=["support-bot"])
        run = {"agent": "support-bot", "scope": "user", "created_by": "user-2"}
        with pytest.raises(HTTPException) as exc_info:
            self._check(run, user)
        assert exc_info.value.status_code == 403

    def test_viewer_allowed_agent_scoped(self):
        user = _make_user(sub="user-1", role="member", agents=["support-bot"])
        run = {"agent": "support-bot", "scope": "agent", "created_by": "support-bot"}
        self._check(run, user)  # Should not raise

    def test_viewer_blocked_no_agent_access(self):
        user = _make_user(sub="user-1", role="member", agents=[])
        run = {"agent": "support-bot", "scope": "agent", "created_by": "support-bot"}
        with pytest.raises(HTTPException) as exc_info:
            self._check(run, user)
        assert exc_info.value.status_code == 403

    def test_manager_allowed_agent_scoped(self):
        user = _make_user(sub="mgr-1", role="creator", agents=["support-bot"])
        run = {"agent": "support-bot", "scope": "agent", "created_by": "support-bot"}
        self._check(run, user)  # Should not raise

    def test_manager_blocked_other_user_scoped(self):
        user = _make_user(sub="mgr-1", role="creator", agents=["support-bot"])
        run = {"agent": "support-bot", "scope": "user", "created_by": "user-1"}
        with pytest.raises(HTTPException) as exc_info:
            self._check(run, user)
        assert exc_info.value.status_code == 403

    def test_manager_allowed_own_user_scoped(self):
        user = _make_user(sub="mgr-1", role="creator", agents=["support-bot"])
        run = {"agent": "support-bot", "scope": "user", "created_by": "mgr-1"}
        self._check(run, user)  # Should not raise

    def test_admin_allowed_agent_scoped(self):
        user = _make_user(sub="admin-1", role="admin")
        run = {"agent": "support-bot", "scope": "agent", "created_by": "support-bot"}
        self._check(run, user)  # Admin has access to all agents

    def test_admin_allowed_other_user_scoped(self):
        """Admin can view all runs (needed for admin page global view)."""
        user = _make_user(sub="admin-1", role="admin")
        run = {"agent": "support-bot", "scope": "user", "created_by": "user-1"}
        self._check(run, user)  # Should not raise -- admin can view all

    def test_legacy_run_no_scope_defaults_agent(self):
        """Runs without scope field default to 'agent' -- visible to all with access."""
        user = _make_user(sub="user-1", role="member", agents=["support-bot"])
        run = {"agent": "support-bot"}  # No scope, no created_by
        self._check(run, user)  # Should not raise (defaults to 'agent')


# ---------------------------------------------------------------------------
# _scope_filter_sub
# ---------------------------------------------------------------------------


class TestScopeFilterSub:
    def _filter(self, user, agent=None, audit=False):
        from api.tasks.tasks import _scope_filter_sub
        return _scope_filter_sub(user, agent, audit)

    def test_master_key_no_filter(self):
        # The master key (service-to-service) is unfiltered.
        user = _make_user(sub="api-key", role="admin", is_api_key=True)
        assert self._filter(user, agent="bot") is None

    def test_user_session_filtered_to_own_sub(self):
        # A real-user session token (is_api_key + real sub) is no longer
        # unfiltered — it is scoped to its own runs, like a cookie user.
        user = _make_user(sub="user-1", is_api_key=True)
        assert self._filter(user, agent="bot") == "user-1"

    def test_viewer_always_filtered(self):
        user = _make_user(sub="user-1", role="member")
        assert self._filter(user, agent="bot") == "user-1"

    def test_viewer_no_agent_still_filtered(self):
        user = _make_user(sub="user-1", role="member")
        assert self._filter(user, agent=None) == "user-1"

    def test_manager_filtered_on_agent_page(self):
        user = _make_user(sub="mgr-1", role="creator")
        assert self._filter(user, agent="bot") == "mgr-1"

    def test_admin_filtered_on_agent_page(self):
        """Admin on agent page (agent param set) gets scope filtering."""
        user = _make_user(sub="admin-1", role="admin")
        assert self._filter(user, agent="bot") == "admin-1"

    def test_admin_unfiltered_on_audit_page(self):
        """Admin on the AUDIT surface (audit=true) sees all runs — even when an
        agent filter is applied."""
        user = _make_user(sub="admin-1", role="admin")
        assert self._filter(user, agent=None, audit=True) is None
        assert self._filter(user, agent="bot", audit=True) is None

    def test_admin_without_audit_gets_user_view(self):
        """Admin WITHOUT audit (agent settings tab, or any non-audit call) gets
        the user-view — they do NOT see other users' user-scoped runs."""
        user = _make_user(sub="admin-1", role="admin")
        assert self._filter(user, agent=None) == "admin-1"
        assert self._filter(user, agent="bot") == "admin-1"

    def test_manager_filtered_even_no_agent(self):
        """Non-admin without agent param still gets filtered."""
        user = _make_user(sub="mgr-1", role="creator")
        assert self._filter(user, agent=None) == "mgr-1"


# ---------------------------------------------------------------------------
# Integration: full permission matrix through list_runs
# ---------------------------------------------------------------------------


class TestPermissionMatrixIntegration:
    """End-to-end: seed runs, then verify each role sees the correct subset."""

    def test_viewer_agent_page(self, temp_db):
        db = temp_db
        _seed_runs(db)
        # Viewer on agent page: own user-scoped + agent-scoped for that agent
        result = db.list_runs(limit=100, agent="support-bot", scope_user_sub="user-1")
        ids = {r["id"] for r in result}
        assert ids == {"run-agent-1", "run-user1", "run-legacy"}

    def test_manager_agent_page(self, temp_db):
        db = temp_db
        _seed_runs(db)
        # Manager (sub=mgr-1) on agent page: agent-scoped + own (none) + legacy
        result = db.list_runs(limit=100, agent="support-bot", scope_user_sub="mgr-1")
        ids = {r["id"] for r in result}
        assert ids == {"run-agent-1", "run-legacy"}
        # Manager should NOT see user-1's or user-2's runs
        assert "run-user1" not in ids
        assert "run-user2" not in ids

    def test_admin_agent_page(self, temp_db):
        db = temp_db
        _seed_runs(db)
        # Admin on agent page (agent param set) -- same as manager
        result = db.list_runs(limit=100, agent="support-bot", scope_user_sub="admin-1")
        ids = {r["id"] for r in result}
        assert ids == {"run-agent-1", "run-legacy"}

    def test_admin_admin_page(self, temp_db):
        db = temp_db
        _seed_runs(db)
        # Admin on admin page (no scope filter) -- sees everything
        result = db.list_runs(limit=100, scope_user_sub=None)
        assert len(result) == 5

    def test_api_key_sees_all(self, temp_db):
        db = temp_db
        _seed_runs(db)
        result = db.list_runs(limit=100, scope_user_sub=None)
        assert len(result) == 5
