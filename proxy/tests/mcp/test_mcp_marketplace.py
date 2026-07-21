"""Tests for the community MCP marketplace.

Covers:
- Storage: CRUD + state machine + partial unique index + user-name JOIN
- Service orchestration: approve / reject / cancel paths (with the installer
  mocked — the real one would hit npm/pypi/GitHub which we don't want in unit
  tests)
- Auto-enable for the requesting agent on approval
- Notification dispatch (per admin, with the requester's name in the body)
- Catalog augmentation (``pending_request`` per agent, ``pending_request_count``
  for the admin view)

Run: cd proxy && python -m pytest tests/mcp/test_mcp_marketplace.py -v
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

REQUESTER_SUB = "user-manager"
ADMIN_SUB = "user-admin"


def _seed_extra_admin(sub: str = "user-admin-2", email: str = "admin2@test.com"):
    """Seed a second admin (the default conftest seeds only one)."""
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login) "
            "VALUES (%s, %s, %s, 'admin', %s, %s) ON CONFLICT (sub) DO NOTHING",
            (sub, email, "Second Admin", now, now),
        )
        conn.commit()


def _seed_agent(slug: str = "test-agent"):
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agents (slug, display_name, created_at, updated_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (slug) DO NOTHING""",
            (slug, slug.replace("-", " ").title(), now, now),
        )
        conn.commit()


def _create_request(mcp_name: str = "nextcloud", agent_slug: str = "test-agent",
                    requested_by: str = REQUESTER_SUB, reason: str = "") -> dict:
    from storage import mcp_request_store
    return mcp_request_store.create_request(mcp_name, agent_slug, requested_by, reason)


# A fake install_from_catalog result that the orchestrator expects.
FAKE_INSTALL_RESULT = {
    "status": "installed",
    "name": "nextcloud",
    "version": "1.1.0",
    "old_version": None,
    "runtime": "node",
    "install_log": "npm install ok",
}


# ───────────────────────────────────────────────────────────────────────────
# Storage — create + read + JOIN with users
# ───────────────────────────────────────────────────────────────────────────


class TestCreateAndRead:
    def test_create_returns_row_with_requester_name(self, temp_db):
        _seed_agent()
        row = _create_request()
        assert row["id"] > 0
        assert row["status"] == "pending"
        assert row["requested_by_name"] == "Manager User"
        assert row["requested_by_email"] == "manager@test.com"
        assert row["resolved_by_name"] is None
        assert row["resolved_by_email"] is None

    def test_create_persists_reason_when_provided(self, temp_db):
        _seed_agent()
        row = _create_request(reason="user asked to send email")
        assert row["reason"] == "user asked to send email"
        from storage import mcp_request_store
        fetched = mcp_request_store.get_request(row["id"])
        assert fetched is not None
        assert fetched["reason"] == "user asked to send email"

    def test_create_defaults_reason_to_empty_string(self, temp_db):
        """NOT NULL DEFAULT '' — never NULL, always a string."""
        _seed_agent()
        row = _create_request()
        assert row["reason"] == ""

    def test_get_request_includes_join(self, temp_db):
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        fetched = mcp_request_store.get_request(row["id"])
        assert fetched is not None
        assert fetched["requested_by_name"] == "Manager User"

    def test_get_request_survives_user_deletion(self, temp_db):
        """LEFT JOIN — deleted user → name columns are NULL but row stays."""
        _seed_agent()
        row = _create_request()
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM users WHERE sub=%s", (REQUESTER_SUB,))
            conn.commit()
        from storage import mcp_request_store
        fetched = mcp_request_store.get_request(row["id"])
        assert fetched is not None
        assert fetched["requested_by"] == REQUESTER_SUB
        assert fetched["requested_by_name"] is None
        assert fetched["requested_by_email"] is None


class TestDuplicateGuard:
    def test_duplicate_open_request_rejected(self, temp_db):
        _seed_agent()
        _create_request()
        with pytest.raises(ValueError) as exc:
            _create_request()
        assert "already exists" in str(exc.value)

    def test_can_recreate_after_rejected(self, temp_db):
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        mcp_request_store.update_status(row["id"], "rejected", resolved_by=ADMIN_SUB)
        # Same (mcp, agent) pair is now requestable again.
        new_row = _create_request()
        assert new_row["id"] != row["id"]
        assert new_row["status"] == "pending"

    def test_can_recreate_after_cancelled(self, temp_db):
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        mcp_request_store.update_status(row["id"], "cancelled", resolved_by=REQUESTER_SUB)
        new_row = _create_request()
        assert new_row["id"] != row["id"]

    def test_can_recreate_after_installed(self, temp_db):
        """After a successful install lifecycle, a fresh request is allowed
        (managers may want it again later — e.g. after an admin uninstall)."""
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        mcp_request_store.update_status(row["id"], "approved", resolved_by=ADMIN_SUB)
        mcp_request_store.update_status(row["id"], "installing")
        mcp_request_store.update_status(row["id"], "installed", resolved_by=ADMIN_SUB)
        new_row = _create_request()
        assert new_row["status"] == "pending"


# ───────────────────────────────────────────────────────────────────────────
# Storage — state machine
# ───────────────────────────────────────────────────────────────────────────


class TestStateMachine:
    def test_invalid_transition_raises(self, temp_db):
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        with pytest.raises(ValueError) as exc:
            mcp_request_store.update_status(row["id"], "installing")
        assert "Cannot transition from 'pending' to 'installing'" in str(exc.value)

    def test_terminal_transition_sets_resolved_fields(self, temp_db):
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        updated = mcp_request_store.update_status(
            row["id"], "rejected", resolved_by=ADMIN_SUB, admin_note="too risky",
        )
        assert updated["status"] == "rejected"
        assert updated["resolved_by"] == ADMIN_SUB
        assert updated["resolved_at"] is not None
        assert updated["admin_note"] == "too risky"
        # Resolver name joined in.
        assert updated["resolved_by_name"] == "Admin User"

    def test_intermediate_transition_does_not_set_resolved_at(self, temp_db):
        """``approved`` is intermediate, not terminal — resolved_at must stay null
        until the install lands."""
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        mid = mcp_request_store.update_status(
            row["id"], "approved", resolved_by=ADMIN_SUB,
        )
        assert mid["status"] == "approved"
        assert mid["resolved_at"] is None
        assert mid["resolved_by"] is None  # only persisted on terminal

    def test_install_failed_can_retry_to_installing(self, temp_db):
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        mcp_request_store.update_status(row["id"], "approved", resolved_by=ADMIN_SUB)
        mcp_request_store.update_status(row["id"], "installing")
        mcp_request_store.update_status(row["id"], "install_failed", install_log="boom")
        # Retry path: install_failed → installing.
        retried = mcp_request_store.update_status(row["id"], "installing")
        assert retried["status"] == "installing"


class TestAggregateQueries:
    def test_count_pending_excludes_other_open_states(self, temp_db):
        _seed_agent("agent-1")
        _seed_agent("agent-2")
        _seed_agent("agent-3")
        from storage import mcp_request_store

        # Three requests, distinct (mcp, agent) pairs.
        mcp_request_store.create_request("nextcloud", "agent-1", REQUESTER_SUB)
        r2 = mcp_request_store.create_request("nextcloud", "agent-2", REQUESTER_SUB)
        r3 = mcp_request_store.create_request("nextcloud", "agent-3", REQUESTER_SUB)

        # r1 stays pending, r2 → approved (still open but not pending), r3 → rejected (terminal).
        mcp_request_store.update_status(r2["id"], "approved", resolved_by=ADMIN_SUB)
        mcp_request_store.update_status(r3["id"], "rejected", resolved_by=ADMIN_SUB)

        assert mcp_request_store.count_pending() == 1

    def test_open_requests_by_pair_returns_open_only(self, temp_db):
        _seed_agent("a1")
        _seed_agent("a2")
        from storage import mcp_request_store

        r_open = mcp_request_store.create_request("nextcloud", "a1", REQUESTER_SUB)
        r_terminal = mcp_request_store.create_request("nextcloud", "a2", REQUESTER_SUB)
        mcp_request_store.update_status(
            r_terminal["id"], "rejected", resolved_by=ADMIN_SUB,
        )

        pairs = mcp_request_store.open_requests_by_pair()
        assert ("nextcloud", "a1") in pairs
        assert pairs[("nextcloud", "a1")] == r_open["id"]
        assert ("nextcloud", "a2") not in pairs


# ───────────────────────────────────────────────────────────────────────────
# Service — approve / reject / cancel orchestration
# ───────────────────────────────────────────────────────────────────────────


class TestApprove:
    def _patch_install(self, *, missing: bool = True, fails: bool = False):
        """Set up a context manager that patches the install side-effects."""
        from services.community import community_installer

        mock_manifest = None if missing else object()  # truthy = "already installed"
        if fails:
            install_mock = AsyncMock(side_effect=RuntimeError("simulated install failure"))
        else:
            install_mock = AsyncMock(return_value=FAKE_INSTALL_RESULT)

        # patch.multiple needs the underlying module attributes
        return patch.multiple(
            community_installer,
            install_from_catalog=install_mock,
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=mock_manifest,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new_callable=AsyncMock,
        )

    def test_approve_resolved_by_set_on_installed(self, temp_db):
        """The bug we just shipped a fix for — `resolved_by` must persist on
        the final installed transition."""
        _seed_agent()
        row = _create_request()
        from services.community import community_installer

        with patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value=FAKE_INSTALL_RESULT),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            updated = asyncio.run(
                community_installer.approve_request(row["id"], ADMIN_SUB, admin_note=""),
            )
        assert updated["status"] == "installed"
        assert updated["resolved_by"] == ADMIN_SUB
        assert updated["resolved_at"] is not None
        assert updated["resolved_by_name"] == "Admin User"

    def test_approve_enables_mcp_for_agent(self, temp_db):
        _seed_agent()
        row = _create_request()
        from services.community import community_installer
        from storage import mcp_store

        # Confirm agent has no MCP enabled yet.
        assert mcp_store.get_manager_enabled_mcps("test-agent") == []

        with patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value=FAKE_INSTALL_RESULT),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            asyncio.run(
                community_installer.approve_request(row["id"], ADMIN_SUB),
            )
        enabled = mcp_store.get_manager_enabled_mcps("test-agent")
        assert "nextcloud" in enabled

    def test_approve_skips_install_when_already_installed(self, temp_db):
        """When the local manifest registry already has the MCP, we shouldn't
        re-fetch from GitHub — just flip the request through to installed."""
        _seed_agent()
        row = _create_request()
        from services.community import community_installer

        install_mock = AsyncMock(return_value=FAKE_INSTALL_RESULT)
        # Truthy manifest = "already installed"
        with patch.object(
            community_installer, "install_from_catalog", new=install_mock,
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=object(),
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            updated = asyncio.run(
                community_installer.approve_request(row["id"], ADMIN_SUB),
            )
        install_mock.assert_not_awaited()
        assert updated["status"] == "installed"
        assert "Already installed" in (updated["install_log"] or "")

    def test_approve_install_failure_lands_in_install_failed(self, temp_db):
        _seed_agent()
        row = _create_request()
        from services.community import community_installer

        with patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(side_effect=RuntimeError("npm boom")),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            updated = asyncio.run(
                community_installer.approve_request(row["id"], ADMIN_SUB),
            )
        assert updated["status"] == "install_failed"
        assert "npm boom" in updated["install_log"]
        # MCP must NOT be enabled when install fails.
        from storage import mcp_store
        assert mcp_store.get_manager_enabled_mcps("test-agent") == []

    def test_retry_install_after_failure(self, temp_db):
        """First approval fails, second approval (acts as retry) succeeds."""
        _seed_agent()
        row = _create_request()
        from services.community import community_installer

        # First attempt fails.
        with patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(side_effect=RuntimeError("first attempt fail")),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            updated = asyncio.run(
                community_installer.approve_request(row["id"], ADMIN_SUB),
            )
        assert updated["status"] == "install_failed"

        # Second attempt succeeds — retry path: install_failed → installing → installed.
        with patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value=FAKE_INSTALL_RESULT),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            updated = asyncio.run(
                community_installer.approve_request(row["id"], ADMIN_SUB),
            )
        assert updated["status"] == "installed"


class TestReject:
    def test_reject_pending(self, temp_db):
        _seed_agent()
        row = _create_request()
        from services.community import community_installer

        with patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            updated = asyncio.run(
                community_installer.reject_request(row["id"], ADMIN_SUB, admin_note="No."),
            )
        assert updated["status"] == "rejected"
        assert updated["resolved_by"] == ADMIN_SUB
        assert updated["admin_note"] == "No."

    def test_reject_after_approved_raises_409(self, temp_db):
        from fastapi import HTTPException
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        mcp_request_store.update_status(row["id"], "approved", resolved_by=ADMIN_SUB)
        from services.community import community_installer
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                community_installer.reject_request(row["id"], ADMIN_SUB),
            )
        assert exc.value.status_code == 409


class TestCancel:
    def test_cancel_by_requester(self, temp_db):
        _seed_agent()
        row = _create_request()
        from services.community import community_installer
        updated = asyncio.run(
            community_installer.cancel_request(row["id"], REQUESTER_SUB),
        )
        assert updated["status"] == "cancelled"
        assert updated["resolved_by"] == REQUESTER_SUB

    def test_cancel_by_other_user_forbidden(self, temp_db):
        from fastapi import HTTPException
        _seed_agent()
        row = _create_request()
        from services.community import community_installer
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                community_installer.cancel_request(row["id"], "user-viewer"),
            )
        assert exc.value.status_code == 403

    def test_cancel_after_approval_forbidden(self, temp_db):
        from fastapi import HTTPException
        _seed_agent()
        row = _create_request()
        from storage import mcp_request_store
        mcp_request_store.update_status(row["id"], "approved", resolved_by=ADMIN_SUB)
        from services.community import community_installer
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                community_installer.cancel_request(row["id"], REQUESTER_SUB),
            )
        assert exc.value.status_code == 409


# ───────────────────────────────────────────────────────────────────────────
# Explicit-mode MCP instance authorization (community_installer +
# mcp_store.add_agent_to_instance) — fix for the bug where approving a
# request for prometheus/ssh-server/unifi/etc. flipped ``agent_mcps`` but
# left ``mcp_instances.agents`` untouched, so the runtime delivered no env.
# ───────────────────────────────────────────────────────────────────────────


class _StubExplicitManifest:
    assignment_mode = "explicit"


class _StubAutoManifest:
    assignment_mode = "auto"


class TestExplicitInstanceAuthorization:
    def test_add_agent_to_instance_inserts(self, temp_db):
        from storage import mcp_store
        _seed_agent()
        iid = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default",
            "field_values": {"PROM_URL": "http://prom:9090"},
            "agents": [],
            "assigned_to_all": False,
        })
        assert mcp_store.add_agent_to_instance(iid, "test-agent") is True
        inst = mcp_store.get_mcp_instances("prometheus")[0]
        assert inst["agents"] == ["test-agent"]
        # Credentials are preserved (no double-encrypt drift).
        assert inst["field_values"] == {"PROM_URL": "http://prom:9090"}

    def test_add_agent_to_instance_idempotent(self, temp_db):
        from storage import mcp_store
        iid = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default",
            "field_values": {},
            "agents": ["test-agent"],
            "assigned_to_all": False,
        })
        assert mcp_store.add_agent_to_instance(iid, "test-agent") is False

    def test_add_agent_to_instance_missing_returns_false(self, temp_db):
        from storage import mcp_store
        assert mcp_store.add_agent_to_instance(999, "test-agent") is False

    def test_helper_not_applicable_for_auto_mode(self, temp_db):
        from services.community import community_installer
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_StubAutoManifest(),
        ):
            status, _log = community_installer._ensure_agent_authorized_for_instance_mcp(
                "nextcloud", "test-agent",
            )
        assert status == "not_applicable"

    def test_helper_no_instances_signals_failure(self, temp_db):
        from services.community import community_installer
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_StubExplicitManifest(),
        ):
            status, _log = community_installer._ensure_agent_authorized_for_instance_mcp(
                "prometheus", "test-agent",
            )
        assert status == "no_instances"

    def test_helper_assigned_to_all_short_circuits(self, temp_db):
        from services.community import community_installer
        from storage import mcp_store
        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "catchall",
            "field_values": {},
            "agents": [],
            "assigned_to_all": True,
        })
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_StubExplicitManifest(),
        ):
            status, log = community_installer._ensure_agent_authorized_for_instance_mcp(
                "prometheus", "test-agent",
            )
        assert status == "assigned_to_all"
        assert "catch-all" in log

    def test_helper_already_in_instance_is_no_op(self, temp_db):
        from services.community import community_installer
        from storage import mcp_store
        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default",
            "field_values": {},
            "agents": ["test-agent"],
            "assigned_to_all": False,
        })
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_StubExplicitManifest(),
        ):
            status, _log = community_installer._ensure_agent_authorized_for_instance_mcp(
                "prometheus", "test-agent",
            )
        assert status == "already_authorized"

    def test_helper_single_instance_attaches_agent(self, temp_db):
        from services.community import community_installer
        from storage import mcp_store
        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default",
            "field_values": {"PROM_URL": "http://prom:9090"},
            "agents": [],
            "assigned_to_all": False,
        })
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_StubExplicitManifest(),
        ):
            status, log = community_installer._ensure_agent_authorized_for_instance_mcp(
                "prometheus", "test-agent",
            )
        assert status == "added_to_first"
        assert "default" in log
        attached = mcp_store.get_mcp_instances("prometheus")[0]
        assert "test-agent" in attached["agents"]
        # Credentials survive the JSON-only update path.
        assert attached["field_values"]["PROM_URL"] == "http://prom:9090"

    def test_helper_multi_instance_picks_lowest_id(self, temp_db):
        """The same precedence rule the runtime uses in
        ``get_instance_for_agent_env_delivery`` — lowest id wins."""
        from services.community import community_installer
        from storage import mcp_store
        first = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "primary",
            "field_values": {},
            "agents": [],
            "assigned_to_all": False,
        })
        second = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "secondary",
            "field_values": {},
            "agents": [],
            "assigned_to_all": False,
        })
        # Sanity: rows ordered by id.
        assert first < second
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_StubExplicitManifest(),
        ):
            community_installer._ensure_agent_authorized_for_instance_mcp(
                "prometheus", "test-agent",
            )
        primary = next(
            i for i in mcp_store.get_mcp_instances("prometheus")
            if i["id"] == first
        )
        secondary = next(
            i for i in mcp_store.get_mcp_instances("prometheus")
            if i["id"] == second
        )
        assert "test-agent" in primary["agents"]
        assert "test-agent" not in secondary["agents"]

    def test_approve_explicit_mcp_no_instances_fails_with_guidance(self, temp_db):
        """End-to-end: admin approves a request for prometheus without
        configuring an instance → request lands in install_failed with a
        clear admin_note. The agent_mcps row must NOT linger."""
        _seed_agent()
        row = _create_request(mcp_name="prometheus")
        from services.community import community_installer
        from storage import mcp_store

        with patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value={**FAKE_INSTALL_RESULT, "name": "prometheus"}),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_StubExplicitManifest(),
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            updated = asyncio.run(
                community_installer.approve_request(row["id"], ADMIN_SUB),
            )
        assert updated["status"] == "install_failed"
        assert "Create an instance" in (updated["install_log"] or "")
        # Critical: agent_mcps must be rolled back so the manager UI
        # doesn't show a non-functional row.
        assert "prometheus" not in mcp_store.get_manager_enabled_mcps("test-agent")

    def test_approve_explicit_mcp_with_instance_attaches_agent(self, temp_db):
        """End-to-end happy path for the bug fix: an instance exists, admin
        approves → agent ends up both in agent_mcps AND in the instance's
        agents list, so the runtime can deliver env."""
        _seed_agent()
        row = _create_request(mcp_name="prometheus")
        from services.community import community_installer
        from storage import mcp_store

        iid = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default",
            "field_values": {"PROM_URL": "http://prom:9090"},
            "agents": [],
            "assigned_to_all": False,
        })

        with patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value={**FAKE_INSTALL_RESULT, "name": "prometheus"}),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_StubExplicitManifest(),
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            updated = asyncio.run(
                community_installer.approve_request(row["id"], ADMIN_SUB),
            )
        assert updated["status"] == "installed"
        assert "prometheus" in mcp_store.get_manager_enabled_mcps("test-agent")
        attached = next(i for i in mcp_store.get_mcp_instances("prometheus") if i["id"] == iid)
        assert "test-agent" in attached["agents"]
        # Helper log appears in the install log for traceability.
        assert "Attached agent to instance" in (updated["install_log"] or "")


# ───────────────────────────────────────────────────────────────────────────
# Notification dispatch
# ───────────────────────────────────────────────────────────────────────────


class TestNotifications:
    def test_notify_request_created_fires_per_admin(self, temp_db):
        """Each admin gets their own delivery — confirms the for-loop in
        ``notify_request_created`` iterates all admin rows."""
        _seed_agent()
        _seed_extra_admin()  # second admin row
        row = _create_request()

        from services.community import community_installer
        captured: list[dict] = []

        async def fake_fire(**kwargs):
            captured.append(kwargs)

        with patch(
            "services.notifications.notification_manager.fire_notification",
            new=fake_fire,
        ):
            asyncio.run(community_installer.notify_request_created(row))

        assert len(captured) == 2
        targets = {c["target"] for c in captured}
        assert targets == {ADMIN_SUB, "user-admin-2"}

    def test_notify_uses_requester_name_in_body(self, temp_db):
        _seed_agent()
        row = _create_request()
        from services.community import community_installer
        captured: list[dict] = []

        async def fake_fire(**kwargs):
            captured.append(kwargs)

        with patch(
            "services.notifications.notification_manager.fire_notification",
            new=fake_fire,
        ):
            asyncio.run(community_installer.notify_request_created(row))

        assert captured, "expected at least one notification fired"
        body = captured[0]["body"]
        assert "Manager User" in body
        assert "manager@test.com" in body
        assert "nextcloud" in body
        assert "test-agent" in body
        # No reason on the row → no "Reason:" line.
        assert "Reason:" not in body

    def test_notify_includes_reason_when_present(self, temp_db):
        _seed_agent()
        row = _create_request(reason="user wants to send invoice emails")
        from services.community import community_installer
        captured: list[dict] = []

        async def fake_fire(**kwargs):
            captured.append(kwargs)

        with patch(
            "services.notifications.notification_manager.fire_notification",
            new=fake_fire,
        ):
            asyncio.run(community_installer.notify_request_created(row))

        assert captured
        body = captured[0]["body"]
        assert "Reason:" in body
        assert "user wants to send invoice emails" in body

    def test_notify_falls_back_to_email_only_when_name_missing(self, temp_db):
        """If the user's ``name`` is empty (rare but possible), the formatter
        falls back to email."""
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute("UPDATE users SET name='' WHERE sub=%s", (REQUESTER_SUB,))
            conn.commit()
        _seed_agent()
        row = _create_request()
        # Re-fetch to pick up the updated JOIN values.
        from storage import mcp_request_store
        row = mcp_request_store.get_request(row["id"])

        from services.community import community_installer
        formatted = community_installer._format_requester(row)
        assert formatted == "**manager@test.com**"

    def test_notify_falls_back_to_generic_when_user_deleted(self, temp_db):
        _seed_agent()
        row = _create_request()
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM users WHERE sub=%s", (REQUESTER_SUB,))
            conn.commit()
        from storage import mcp_request_store
        row = mcp_request_store.get_request(row["id"])
        from services.community import community_installer
        formatted = community_installer._format_requester(row)
        assert formatted == "A manager"


# ───────────────────────────────────────────────────────────────────────────
# Catalog augmentation — pending_request fields
# ───────────────────────────────────────────────────────────────────────────


class TestCatalogAugmentation:
    def test_augment_entry_pending_request_for_agent(self, temp_db):
        _seed_agent()
        row = _create_request()
        from services.community import community_catalog
        entry = {
            "name": "nextcloud",
            "label": "Nextcloud",
            "description": "...",
            "version": "1.1.0",
        }
        augmented = community_catalog.augment_entry(
            entry,
            installed_versions={},
            enabled_for_agents={},
            pending_requests={("nextcloud", "test-agent"): row["id"]},
            agent_slug="test-agent",
        )
        assert augmented["pending_request"] == row["id"]
        assert augmented["pending_request_count"] == 1

    def test_augment_entry_pending_request_null_for_other_agent(self, temp_db):
        from services.community import community_catalog
        entry = {"name": "nextcloud", "label": "X", "description": "", "version": "1"}
        augmented = community_catalog.augment_entry(
            entry,
            installed_versions={},
            enabled_for_agents={},
            pending_requests={("nextcloud", "agent-1"): 5},
            agent_slug="agent-2",
        )
        assert augmented["pending_request"] is None
        # But the count still reflects open requests across all agents.
        assert augmented["pending_request_count"] == 1

    def test_augment_entry_installed_and_update_available(self, temp_db):
        from services.community import community_catalog
        entry = {"name": "nextcloud", "label": "X", "description": "", "version": "1.2.0"}
        augmented = community_catalog.augment_entry(
            entry,
            installed_versions={"nextcloud": "1.1.0"},
            enabled_for_agents={"nextcloud": ["a", "b"]},
            pending_requests={},
        )
        assert augmented["installed"] is True
        assert augmented["installed_version"] == "1.1.0"
        assert augmented["update_available"] is True
        assert augmented["enabled_for_agents"] == ["a", "b"]

    def test_augment_entry_no_update_when_installed_newer_than_catalog(self, temp_db):
        """An npm/pypi MCP auto-updated past the catalog's pinned seed version must
        NOT advertise the older catalog version as an available update (the
        downgrade bug: installed 2.4.1, catalog 2.4.0 → no update)."""
        from services.community import community_catalog
        entry = {"name": "notion-mcp", "label": "X", "description": "", "version": "2.4.0"}
        augmented = community_catalog.augment_entry(
            entry, installed_versions={"notion-mcp": "2.4.1"},
            enabled_for_agents={}, pending_requests={},
        )
        assert augmented["installed"] is True
        assert augmented["installed_version"] == "2.4.1"
        assert augmented["update_available"] is False

    def test_augment_entry_no_update_when_versions_equal(self, temp_db):
        from services.community import community_catalog
        entry = {"name": "notion-mcp", "label": "X", "description": "", "version": "2.4.1"}
        augmented = community_catalog.augment_entry(
            entry, installed_versions={"notion-mcp": "2.4.1"},
            enabled_for_agents={}, pending_requests={},
        )
        assert augmented["update_available"] is False

    def test_augment_entry_no_update_when_catalog_version_empty(self, temp_db):
        """node/python catalog entries are unpinned (version ""); the catalog must
        never advertise an update for them (they update via the registry probe,
        not the catalog). Empty catalog version → update_available False."""
        from services.community import community_catalog
        entry = {"name": "notion-mcp", "label": "X", "description": "", "version": ""}
        augmented = community_catalog.augment_entry(
            entry, installed_versions={"notion-mcp": "2.4.0"},
            enabled_for_agents={}, pending_requests={},
        )
        assert augmented["installed"] is True
        assert augmented["installed_version"] == "2.4.0"
        assert augmented["update_available"] is False


# ───────────────────────────────────────────────────────────────────────────
# End-to-end happy path — mirrors the real-world user flow
# ───────────────────────────────────────────────────────────────────────────


class TestAdminAutoApprove:
    """``POST /v1/agents/{slug}/mcp-requests`` short-circuits the queue for
    admin requesters. The same approve_request orchestration runs inline
    so the response row is already resolved (installed | install_failed)."""

    def _admin_ctx(self):
        from auth.providers import UserContext
        return UserContext(
            sub=ADMIN_SUB, email="admin@test.com", name="Admin User",
            role="admin", agents=[],
        )

    def _manager_ctx(self):
        from auth.providers import UserContext
        return UserContext(
            sub=REQUESTER_SUB, email="manager@test.com", name="Manager User",
            role="creator", agents=["test-agent"],
            agent_roles={"test-agent": "manager"},
        )

    def _mock_catalog(self, mcp_names=("nextcloud", "prometheus")):
        from services.community import community_catalog
        registry = {"mcps": [{"name": n} for n in mcp_names]}
        return patch.object(
            community_catalog, "fetch_registry",
            new=AsyncMock(return_value=registry),
        )

    def test_admin_request_auto_approves_inline(self, temp_db):
        """Admin POSTs request → returned row is already ``installed``,
        no notification dispatched (admin doesn't bug themselves)."""
        _seed_agent()
        from api.mcp.community import create_mcp_request, CreateRequestBody
        from services.community import community_installer
        from storage import mcp_store, mcp_request_store

        fire_calls: list[dict] = []
        async def fake_fire(**kwargs):
            fire_calls.append(kwargs)

        with self._mock_catalog(), patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value=FAKE_INSTALL_RESULT),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=None,  # auto-mode (no instance auth needed)
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=fake_fire,
        ):
            row = asyncio.run(create_mcp_request(
                "test-agent",
                CreateRequestBody(mcp_name="nextcloud", reason=""),
                user=self._admin_ctx(),
            ))

        assert row["status"] == "installed"
        assert row["resolved_by"] == ADMIN_SUB
        assert "Auto-approved" in row["admin_note"]
        assert "nextcloud" in mcp_store.get_manager_enabled_mcps("test-agent")
        # Admin should NOT be pinged about their own request.
        assert not any(
            c.get("title") == "MCP request pending" for c in fire_calls
        )
        # No pending rows hanging around.
        assert mcp_request_store.count_pending() == 0

    def test_manager_request_still_queues_for_approval(self, temp_db):
        """Sanity: the auto-approve branch is admin-only — managers still
        land in pending + every admin gets pinged."""
        _seed_agent()
        from api.mcp.community import create_mcp_request, CreateRequestBody
        from services.community import community_installer
        from storage import mcp_request_store

        fire_calls: list[dict] = []
        async def fake_fire(**kwargs):
            fire_calls.append(kwargs)

        with self._mock_catalog(), patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value=FAKE_INSTALL_RESULT),
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=fake_fire,
        ):
            row = asyncio.run(create_mcp_request(
                "test-agent",
                CreateRequestBody(mcp_name="nextcloud", reason="for invoices"),
                user=self._manager_ctx(),
            ))

        assert row["status"] == "pending"
        # The admin notification fires; auto-approve doesn't.
        assert any(
            c.get("title") == "MCP request pending" for c in fire_calls
        )
        assert mcp_request_store.count_pending() == 1

    def test_admin_request_explicit_no_instance_lands_install_failed(self, temp_db):
        """Admin can still hit the no-instance wall on an explicit-mode
        MCP — auto-approve doesn't paper over the missing config, it
        surfaces the same admin_note the manager flow would."""
        _seed_agent()
        from api.mcp.community import create_mcp_request, CreateRequestBody
        from services.community import community_installer

        class _Explicit:
            assignment_mode = "explicit"

        with self._mock_catalog(("prometheus",)), patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value={**FAKE_INSTALL_RESULT, "name": "prometheus"}),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_Explicit(),  # explicit-mode without instances
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            row = asyncio.run(create_mcp_request(
                "test-agent",
                CreateRequestBody(mcp_name="prometheus", reason=""),
                user=self._admin_ctx(),
            ))

        assert row["status"] == "install_failed"
        assert "Create an instance" in (row["install_log"] or "")


class TestInstanceUpdateById:
    """``mcp_store.update_mcp_instance_by_id`` — PUT semantics, distinct
    from the upsert path used by POST. Targets the URL ``{instance_id}``
    directly so rename works without leaving the old row orphaned."""

    def test_update_renames_in_place(self, temp_db):
        from storage import mcp_store
        iid = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "primary",
            "field_values": {"PROMETHEUS_URL": "http://a:9090"},
            "agents": ["agent-a"],
            "assigned_to_all": False,
        })
        ok = mcp_store.update_mcp_instance_by_id(iid, "prometheus", {
            "instance_name": "production",
            "field_values": {"PROMETHEUS_URL": "http://a:9090"},
            "agents": ["agent-a"],
            "assigned_to_all": False,
        })
        assert ok is True
        rows = mcp_store.get_mcp_instances("prometheus")
        # Exactly one row — the rename was in-place, no orphan.
        assert len(rows) == 1
        assert rows[0]["id"] == iid
        assert rows[0]["instance_name"] == "production"
        assert rows[0]["field_values"]["PROMETHEUS_URL"] == "http://a:9090"

    def test_update_missing_id_returns_false(self, temp_db):
        from storage import mcp_store
        ok = mcp_store.update_mcp_instance_by_id(99999, "prometheus", {
            "instance_name": "ghost", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        assert ok is False

    def test_update_name_collision_raises(self, temp_db):
        """Renaming to a name that's already taken by another instance of
        the same MCP must raise — surfaces a 409 at the API layer."""
        from storage import mcp_store
        first = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "prod", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        second = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "staging", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        with pytest.raises(ValueError, match="already named"):
            mcp_store.update_mcp_instance_by_id(second, "prometheus", {
                "instance_name": "prod",  # taken by ``first``
                "field_values": {}, "agents": [], "assigned_to_all": False,
            })
        # No mutation happened — second row keeps its original name.
        rows = {r["id"]: r["instance_name"] for r in mcp_store.get_mcp_instances("prometheus")}
        assert rows[first] == "prod"
        assert rows[second] == "staging"

    def test_update_same_name_not_a_collision(self, temp_db):
        """Updating credentials without renaming is fine — the self-row
        is excluded from the collision check via ``id != %s``."""
        from storage import mcp_store
        iid = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "prod",
            "field_values": {"PROMETHEUS_URL": "http://old:9090"},
            "agents": [], "assigned_to_all": False,
        })
        ok = mcp_store.update_mcp_instance_by_id(iid, "prometheus", {
            "instance_name": "prod",
            "field_values": {"PROMETHEUS_URL": "http://new:9090"},
            "agents": [], "assigned_to_all": False,
        })
        assert ok is True
        rows = mcp_store.get_mcp_instances("prometheus")
        assert len(rows) == 1
        assert rows[0]["field_values"]["PROMETHEUS_URL"] == "http://new:9090"


class TestInstanceSaveAutoRetry:
    """``POST/PUT /v1/admin/mcps/{name}/instances`` sweeps install_failed
    requests for this MCP and auto-retries the ones whose agent is now
    authorized — eliminates the manual Retry click after admin configures
    an instance for a pending explicit-mode request."""

    def _admin_ctx(self):
        from auth.providers import UserContext
        return UserContext(
            sub=ADMIN_SUB, email="admin@test.com", name="Admin User",
            role="admin", agents=[],
        )

    def test_auto_retry_when_agent_now_in_instance(self, temp_db):
        """install_failed → instance created with agent in agents[] →
        request auto-flips to installed without a manual Retry."""
        _seed_agent()
        # Manually plant an install_failed row (the request that an
        # admin would have hit before they configured an instance).
        from storage.pg import get_conn
        from storage import mcp_request_store
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO mcp_assignment_requests
                   (mcp_name, agent_slug, requested_by, status, admin_note,
                    install_log, created_at, updated_at, reason)
                   VALUES (%s, %s, %s, 'install_failed', '',
                           'No instance configured.', %s, %s, '')""",
                ("prometheus", "test-agent", REQUESTER_SUB, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM mcp_assignment_requests WHERE mcp_name='prometheus'"
            ).fetchone()
            request_id = row["id"]

        # Helper: mimic the registry returning an explicit-mode prometheus
        # so approve_request's _ensure_agent_authorized_for_instance_mcp
        # branches correctly.
        class _Explicit:
            assignment_mode = "explicit"

        # Configure an instance with the agent already attached.
        from api.mcp.mcps import _retry_install_failed_for_instance
        from storage import mcp_store
        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default",
            "field_values": {"PROMETHEUS_URL": "http://localhost:9090"},
            "agents": ["test-agent"],
            "assigned_to_all": False,
        })

        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_Explicit(),
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            retried = asyncio.run(_retry_install_failed_for_instance(
                "prometheus", ["test-agent"], False, ADMIN_SUB,
            ))

        assert retried == [request_id]
        updated = mcp_request_store.get_request(request_id)
        assert updated["status"] == "installed"
        # Agent is on agent_mcps now (full cascade ran).
        assert "prometheus" in mcp_store.get_manager_enabled_mcps("test-agent")

    def test_auto_retry_via_assigned_to_all_catchall(self, temp_db):
        """Same sweep should trigger when admin saves an instance with
        ``assigned_to_all=True`` even without naming the agent — the
        catch-all covers them."""
        _seed_agent()
        from storage.pg import get_conn
        from storage import mcp_request_store, mcp_store
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO mcp_assignment_requests
                   (mcp_name, agent_slug, requested_by, status, admin_note,
                    install_log, created_at, updated_at, reason)
                   VALUES (%s, %s, %s, 'install_failed', '', '', %s, %s, '')""",
                ("prometheus", "test-agent", REQUESTER_SUB, now, now),
            )
            conn.commit()
            request_id = conn.execute(
                "SELECT id FROM mcp_assignment_requests WHERE mcp_name='prometheus'"
            ).fetchone()["id"]

        class _Explicit:
            assignment_mode = "explicit"

        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "shared",
            "field_values": {"PROMETHEUS_URL": "http://prom:9090"},
            "agents": [],
            "assigned_to_all": True,
        })

        from api.mcp.mcps import _retry_install_failed_for_instance
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_Explicit(),
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            retried = asyncio.run(_retry_install_failed_for_instance(
                "prometheus", [], True, ADMIN_SUB,
            ))

        assert retried == [request_id]
        assert mcp_request_store.get_request(request_id)["status"] == "installed"

    def test_auto_retry_skips_unrelated_agent(self, temp_db):
        """An install_failed request for agent A should NOT auto-retry
        when admin saves an instance authorizing only agent B."""
        _seed_agent("agent-a")
        _seed_agent("agent-b")
        from storage.pg import get_conn
        from storage import mcp_request_store
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO mcp_assignment_requests
                   (mcp_name, agent_slug, requested_by, status, admin_note,
                    install_log, created_at, updated_at, reason)
                   VALUES (%s, %s, %s, 'install_failed', '', '', %s, %s, '')""",
                ("prometheus", "agent-a", REQUESTER_SUB, now, now),
            )
            conn.commit()
            request_id = conn.execute(
                "SELECT id FROM mcp_assignment_requests WHERE mcp_name='prometheus'"
            ).fetchone()["id"]

        from api.mcp.mcps import _retry_install_failed_for_instance
        retried = asyncio.run(_retry_install_failed_for_instance(
            "prometheus", ["agent-b"], False, ADMIN_SUB,
        ))

        assert retried == []
        assert mcp_request_store.get_request(request_id)["status"] == "install_failed"


class TestEndToEnd:
    def test_full_lifecycle_pending_to_installed(self, temp_db):
        """Manager requests → admin approves → install runs (mocked) →
        MCP is enabled for the agent → request is in `installed` state."""
        _seed_agent()
        row = _create_request()
        from services.community import community_installer
        from storage import mcp_store, mcp_request_store

        # Sanity: nothing enabled yet, request is pending.
        assert row["status"] == "pending"
        assert mcp_store.get_manager_enabled_mcps("test-agent") == []

        # Admin approves with a note.
        with patch.object(
            community_installer, "install_from_catalog",
            new=AsyncMock(return_value=FAKE_INSTALL_RESULT),
        ), patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            final = asyncio.run(
                community_installer.approve_request(
                    row["id"], ADMIN_SUB,
                    admin_note="Approved — API key already configured.",
                ),
            )

        # Request is terminal.
        assert final["status"] == "installed"
        assert final["resolved_by"] == ADMIN_SUB
        assert "API key" in final["admin_note"]
        # Agent has the MCP enabled.
        assert "nextcloud" in mcp_store.get_manager_enabled_mcps("test-agent")
        # Listings reflect the resolved state.
        assert mcp_request_store.count_pending() == 0
        all_rows = mcp_request_store.list_all_requests()
        assert len(all_rows) == 1
        assert all_rows[0]["status"] == "installed"


# ───────────────────────────────────────────────────────────────────────────
# Batch ID grouping on mcp_assignment_requests
# ───────────────────────────────────────────────────────────────────────────


class TestBatchIdGrouping:
    """Verify that ``batch_id`` populates correctly and the helper queries
    used by the notification batcher work as expected."""

    def test_create_with_batch_id_persists(self, temp_db):
        from storage import mcp_request_store
        _seed_agent()
        row = mcp_request_store.create_request(
            "nextcloud", "test-agent", REQUESTER_SUB,
            batch_id="batch-abc",
        )
        assert row["batch_id"] == "batch-abc"

    def test_create_without_batch_id_defaults_null(self, temp_db):
        from storage import mcp_request_store
        _seed_agent()
        row = mcp_request_store.create_request(
            "nextcloud", "test-agent", REQUESTER_SUB,
        )
        assert row["batch_id"] is None

    def test_list_requests_by_batch_returns_all(self, temp_db):
        from storage import mcp_request_store
        _seed_agent("agent-a")
        _seed_agent("agent-b")
        mcp_request_store.create_request(
            "nextcloud", "agent-a", REQUESTER_SUB, batch_id="batch-xyz",
        )
        mcp_request_store.create_request(
            "google-maps", "agent-a", REQUESTER_SUB, batch_id="batch-xyz",
        )
        # Different batch — should not be returned.
        mcp_request_store.create_request(
            "nextcloud", "agent-b", REQUESTER_SUB, batch_id="batch-other",
        )
        rows = mcp_request_store.list_requests_by_batch("batch-xyz")
        assert len(rows) == 2
        assert {r["mcp_name"] for r in rows} == {"nextcloud", "google-maps"}

    def test_all_in_batch_terminal_false_when_pending(self, temp_db):
        from storage import mcp_request_store
        _seed_agent()
        mcp_request_store.create_request(
            "nextcloud", "test-agent", REQUESTER_SUB, batch_id="batch-1",
        )
        assert mcp_request_store.all_in_batch_terminal("batch-1") is False

    def test_all_in_batch_terminal_true_when_all_resolved(self, temp_db):
        from storage import mcp_request_store
        _seed_agent("agent-a")
        _seed_agent("agent-b")
        r1 = mcp_request_store.create_request(
            "nextcloud", "agent-a", REQUESTER_SUB, batch_id="batch-done",
        )
        r2 = mcp_request_store.create_request(
            "google-maps", "agent-b", REQUESTER_SUB, batch_id="batch-done",
        )
        # Manually cycle through the state machine to a terminal state.
        mcp_request_store.update_status(r1["id"], "approved")
        mcp_request_store.update_status(r1["id"], "installing")
        mcp_request_store.update_status(r1["id"], "installed", resolved_by=ADMIN_SUB)
        mcp_request_store.update_status(r2["id"], "rejected", resolved_by=ADMIN_SUB)
        assert mcp_request_store.all_in_batch_terminal("batch-done") is True

    def test_all_in_batch_terminal_false_when_install_failed(self, temp_db):
        """``install_failed`` is NOT terminal — admin may retry."""
        from storage import mcp_request_store
        _seed_agent()
        r = mcp_request_store.create_request(
            "nextcloud", "test-agent", REQUESTER_SUB, batch_id="batch-mid",
        )
        mcp_request_store.update_status(r["id"], "approved")
        mcp_request_store.update_status(r["id"], "installing")
        mcp_request_store.update_status(
            r["id"], "install_failed", admin_note="npm err"
        )
        assert mcp_request_store.all_in_batch_terminal("batch-mid") is False

    def test_all_in_batch_terminal_empty_batch_is_false(self, temp_db):
        """No rows for batch_id — return False (caller must skip)."""
        from storage import mcp_request_store
        assert mcp_request_store.all_in_batch_terminal("never-existed") is False


# ───────────────────────────────────────────────────────────────────────────
# Agent deletion + user-removal cascade cleanup
# ───────────────────────────────────────────────────────────────────────────


def _insert_dynamic_task(agent: str, *, scope: str = "user", created_by: str = REQUESTER_SUB,
                         item_slug: str | None = None) -> str:
    from storage import database as db
    task_id = f"task-{agent}-{scope}-{created_by}-{item_slug or 'manual'}"
    db.create_dynamic_task(
        task_id=task_id, agent=agent, name=f"test-{task_id}",
        prompt="echo", llm_mode="cli", task_type="cron",
        schedule="0 8 * * *", run_at=None, delay_seconds=None,
        interval_seconds=None, timeout_seconds=600,
        created_by=created_by, scope=scope,
        on_complete_agent=None, on_complete_prompt=None,
        on_complete_session_id=None, on_complete_chat_id=None,
        continue_session=None, use_persistent=False,
        notification_mode="manual", notify_severity="info",
        user_tz="UTC",
        community_template=None,
        community_template_item_slug=item_slug,
    )
    return task_id


def _insert_trigger(agent: str, *, slug: str, scope: str = "user", created_by: str = REQUESTER_SUB,
                    item_slug: str | None = None) -> str:
    from storage import trigger_store
    trigger_id = f"trig-{agent}-{slug}-{created_by}"
    trigger_store.create_trigger(
        trigger_id=trigger_id, slug=slug, name=f"test-{slug}",
        scope=scope, agent=agent, created_by=created_by,
        task_id=None,
        notify_enabled=False, notify_severity="info",
        notify_title=None, notify_body=None,
        notify_target_scope=None, notify_target=None,
        debounce_seconds=0,
        community_template=None,
        community_template_item_slug=item_slug,
    )
    return trigger_id


def _insert_notification(agent: str, *, scope: str = "user", target: str = REQUESTER_SUB,
                          item_slug: str | None = None) -> str:
    from storage import notification_store
    notif_id = f"notif-{agent}-{scope}-{target}-{item_slug or 'manual'}"
    notification_store.create_notification(
        notification_id=notif_id, title="test", body="test",
        severity="info", scope=scope, target=target,
        source="template", source_id=None,
        notification_type="recurring",
        schedule="0 9 * * *", run_at=None, interval_seconds=None,
        created_by=ADMIN_SUB,
        agent_slug=agent, chat_id=None, user_tz="UTC",
        community_template=None,
        community_template_item_slug=item_slug,
    )
    return notif_id


class TestAgentDeleteCascade:
    """``agent_store.delete_agent`` must hard-delete all related scheduled
    items (tasks/triggers/notifications + their delivery rows), which were
    previously orphaned."""

    def test_delete_agent_removes_dynamic_tasks(self, temp_db):
        from storage import agent_store, database as db
        agent_store.create_agent("doomed", "Doomed")
        _insert_dynamic_task("doomed", scope="agent", created_by=ADMIN_SUB)
        _insert_dynamic_task("doomed", scope="user", created_by=REQUESTER_SUB)
        assert len(db.list_dynamic_tasks(agent="doomed")) == 2

        agent_store.delete_agent("doomed")
        assert db.list_dynamic_tasks(agent="doomed") == []

    def test_delete_agent_removes_triggers(self, temp_db):
        from storage import agent_store, trigger_store
        agent_store.create_agent("doomed", "Doomed")
        _insert_trigger("doomed", slug="t1", scope="agent", created_by=ADMIN_SUB)
        _insert_trigger("doomed", slug="t2", scope="user", created_by=REQUESTER_SUB)
        assert len(trigger_store.list_triggers(agent="doomed")) == 2

        agent_store.delete_agent("doomed")
        assert trigger_store.list_triggers(agent="doomed") == []

    def test_delete_agent_removes_notifications_and_deliveries(self, temp_db):
        from storage import agent_store
        from storage.pg import get_conn

        agent_store.create_agent("doomed", "Doomed")
        notif_id = _insert_notification("doomed", scope="user", target=REQUESTER_SUB)
        # Create a delivery row pointing at the notification.
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO notification_deliveries
                   (id, notification_id, user_sub, title, body, severity, scope, source, delivered_at, agent_slug)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                ("delivery-1", notif_id, REQUESTER_SUB, "x", "x", "info",
                 "user", "template", now, "doomed"),
            )
            conn.commit()

        agent_store.delete_agent("doomed")

        with get_conn() as conn:
            notifs = conn.execute(
                "SELECT 1 FROM notifications WHERE agent_slug = 'doomed'"
            ).fetchall()
            deliveries = conn.execute(
                "SELECT 1 FROM notification_deliveries WHERE notification_id = %s",
                (notif_id,),
            ).fetchall()
        assert notifs == []
        assert deliveries == []

    def test_delete_agent_keeps_unrelated_agent_data(self, temp_db):
        """Deleting one agent must not touch another agent's rows."""
        from storage import agent_store, database as db, trigger_store
        agent_store.create_agent("keeper", "Keeper")
        agent_store.create_agent("doomed", "Doomed")
        _insert_dynamic_task("keeper", scope="agent", created_by=ADMIN_SUB)
        _insert_trigger("keeper", slug="k1", scope="agent", created_by=ADMIN_SUB)
        _insert_dynamic_task("doomed", scope="agent", created_by=ADMIN_SUB)

        agent_store.delete_agent("doomed")

        assert len(db.list_dynamic_tasks(agent="keeper")) == 1
        assert len(trigger_store.list_triggers(agent="keeper")) == 1


class TestUserRemovalCascade:
    """``set_user_agents`` must cascade-delete user-scope tasks/triggers/
    notifications belonging to the (sub, agent) pair when the user loses
    access. Agent-scope items stay."""

    def test_removing_user_from_agent_cleans_user_scope_tasks(self, temp_db):
        from storage import agent_store, database as db
        agent_store.create_agent("a-test", "A Test")
        db.set_user_agents(REQUESTER_SUB, ["a-test"], assigned_by=ADMIN_SUB,
                           agent_roles={"a-test": "manager"})
        _insert_dynamic_task("a-test", scope="user", created_by=REQUESTER_SUB)
        _insert_dynamic_task("a-test", scope="agent", created_by=REQUESTER_SUB)
        assert len(db.list_dynamic_tasks(agent="a-test")) == 2

        # Remove user from agent (set to empty list).
        db.set_user_agents(REQUESTER_SUB, [], assigned_by=ADMIN_SUB)

        remaining = db.list_dynamic_tasks(agent="a-test")
        assert len(remaining) == 1
        assert remaining[0]["scope"] == "agent"

    def test_removing_user_cleans_user_scope_triggers(self, temp_db):
        from storage import agent_store, database as db, trigger_store
        agent_store.create_agent("a-test", "A Test")
        db.set_user_agents(REQUESTER_SUB, ["a-test"], assigned_by=ADMIN_SUB,
                           agent_roles={"a-test": "manager"})
        _insert_trigger("a-test", slug="t1", scope="user", created_by=REQUESTER_SUB)
        _insert_trigger("a-test", slug="t2", scope="agent", created_by=REQUESTER_SUB)

        db.set_user_agents(REQUESTER_SUB, [], assigned_by=ADMIN_SUB)

        remaining = trigger_store.list_triggers(agent="a-test")
        assert len(remaining) == 1
        assert remaining[0]["scope"] == "agent"

    def test_removing_user_cleans_user_scope_notifications(self, temp_db):
        from storage import agent_store, database as db, notification_store
        agent_store.create_agent("a-test", "A Test")
        db.set_user_agents(REQUESTER_SUB, ["a-test"], assigned_by=ADMIN_SUB,
                           agent_roles={"a-test": "manager"})
        _insert_notification("a-test", scope="user", target=REQUESTER_SUB)
        _insert_notification("a-test", scope="agent", target=None)

        db.set_user_agents(REQUESTER_SUB, [], assigned_by=ADMIN_SUB)

        all_notifs = notification_store.list_notifications()
        remaining = [n for n in all_notifs if n["agent_slug"] == "a-test"]
        assert len(remaining) == 1
        assert remaining[0]["scope"] == "agent"

    def test_other_user_scope_items_survive(self, temp_db):
        """Removing user A from agent X must NOT touch user B's items on X."""
        from storage import agent_store, database as db
        agent_store.create_agent("a-test", "A Test")
        db.set_user_agents(REQUESTER_SUB, ["a-test"], assigned_by=ADMIN_SUB,
                           agent_roles={"a-test": "manager"})
        db.set_user_agents("user-viewer", ["a-test"], assigned_by=ADMIN_SUB,
                           agent_roles={"a-test": "viewer"})
        _insert_dynamic_task("a-test", scope="user", created_by=REQUESTER_SUB)
        _insert_dynamic_task("a-test", scope="user", created_by="user-viewer")

        # Remove REQUESTER from the agent; viewer keeps access.
        db.set_user_agents(REQUESTER_SUB, [], assigned_by=ADMIN_SUB)

        remaining = db.list_dynamic_tasks(agent="a-test")
        assert len(remaining) == 1
        assert remaining[0]["created_by"] == "user-viewer"
