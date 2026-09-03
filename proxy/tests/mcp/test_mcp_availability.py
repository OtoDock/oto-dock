"""Tests for the MCP availability tiers + instance-aware request flow.

Covers the 2026-08-16 batch:

- ``mcp_registry.get_unauthorized_explicit_mcps_for_agent`` — the discovery
  complement: platform-enabled explicit-mode MCPs with no instance covering
  the agent (auto-mode and platform-disabled MCPs never appear).
- ``GET /v1/agents/{name}/mcps?include_unauthorized=true`` — appends those
  rows flagged ``authorized: false``; the default response keeps its shape.
- ``POST /v1/agents/{slug}/mcp-requests`` accepts MCPs installed on the
  platform even when absent from the community catalog (zip/manual
  installs), checking the local manifest BEFORE the catalog so a catalog
  outage can't 502 a request for an on-disk MCP.
- ``approve_request(instance_id=...)`` — admin-selected instance attach,
  validated BEFORE any status transition (a bad id must not strand the row
  in ``installing``).
- The derived request-row display fields (``assignment_mode`` /
  ``needs_instance`` / ``instance_count``) — computed live, never persisted.
- Community-agent installs: an ADMIN installing a template that needs an
  installed explicit-mode MCP with zero instances now terminates in the
  same guided ``install_failed`` state as the not-installed variant
  (previously it queued a plain ``pending`` row at the admin themself).

Run: cd proxy && python -m pytest tests/mcp/test_mcp_availability.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


ADMIN_SUB = "user-admin"
REQUESTER_SUB = "user-manager"


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


def _stub_manifest(name: str, mode: str = "auto", category: str = "community"):
    return SimpleNamespace(
        name=name,
        label=name.title(),
        description=f"{name} description",
        category=category,
        assignment_mode=mode,
        requires_capability=None,
        server=SimpleNamespace(runtime="node"),
        skills=[],
    )


def _admin_ctx():
    from auth.providers import UserContext
    return UserContext(
        sub=ADMIN_SUB, email="admin@test.com", name="Admin User",
        role="admin", agents=[],
    )


def _manager_ctx(agent: str = "test-agent"):
    from auth.providers import UserContext
    return UserContext(
        sub=REQUESTER_SUB, email="manager@test.com", name="Manager User",
        role="creator", agents=[agent],
        agent_roles={agent: "manager"},
    )


# ───────────────────────────────────────────────────────────────────────────
# Registry: the unauthorized-explicit complement
# ───────────────────────────────────────────────────────────────────────────


class TestUnauthorizedRegistryListing:
    def _patched_manifests(self, manifests: dict):
        from services.mcp import mcp_registry
        return patch.object(mcp_registry, "_manifests", manifests)

    def test_explicit_without_instance_is_listed(self, temp_db):
        from services.mcp import mcp_registry
        from storage import mcp_store
        mcp_store.set_mcp_enabled("prometheus", True)
        with self._patched_manifests({"prometheus": _stub_manifest("prometheus", "explicit")}):
            names = [m.name for m in mcp_registry.get_unauthorized_explicit_mcps_for_agent("a1")]
        assert names == ["prometheus"]

    def test_auto_mode_never_listed(self, temp_db):
        from services.mcp import mcp_registry
        from storage import mcp_store
        mcp_store.set_mcp_enabled("bluesky-mcp", True)
        with self._patched_manifests({"bluesky-mcp": _stub_manifest("bluesky-mcp", "auto")}):
            assert mcp_registry.get_unauthorized_explicit_mcps_for_agent("a1") == []

    def test_platform_disabled_is_hidden(self, temp_db):
        from services.mcp import mcp_registry
        from storage import mcp_store
        mcp_store.set_mcp_enabled("prometheus", False)
        with self._patched_manifests({"prometheus": _stub_manifest("prometheus", "explicit")}):
            assert mcp_registry.get_unauthorized_explicit_mcps_for_agent("a1") == []

    def test_covered_agent_not_listed_but_others_are(self, temp_db):
        from services.mcp import mcp_registry
        from storage import mcp_store
        mcp_store.set_mcp_enabled("prometheus", True)
        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default", "field_values": {},
            "agents": ["covered-agent"], "assigned_to_all": False,
        })
        with self._patched_manifests({"prometheus": _stub_manifest("prometheus", "explicit")}):
            assert mcp_registry.get_unauthorized_explicit_mcps_for_agent("covered-agent") == []
            names = [m.name for m in mcp_registry.get_unauthorized_explicit_mcps_for_agent("other-agent")]
        assert names == ["prometheus"]

    def test_assigned_to_all_covers_everyone(self, temp_db):
        from services.mcp import mcp_registry
        from storage import mcp_store
        mcp_store.set_mcp_enabled("prometheus", True)
        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default", "field_values": {},
            "agents": [], "assigned_to_all": True,
        })
        with self._patched_manifests({"prometheus": _stub_manifest("prometheus", "explicit")}):
            assert mcp_registry.get_unauthorized_explicit_mcps_for_agent("anyone") == []


# ───────────────────────────────────────────────────────────────────────────
# Endpoint: include_unauthorized param
# ───────────────────────────────────────────────────────────────────────────


class TestIncludeUnauthorizedEndpoint:
    def _run(self, include_unauthorized: bool):
        from api.mcp import mcps as mcps_api
        with patch.object(
            mcps_api.mcp_registry, "get_visible_mcps_for_agent",
            return_value=[_stub_manifest("bluesky-mcp", "auto")],
        ), patch.object(
            mcps_api.mcp_registry, "get_unauthorized_explicit_mcps_for_agent",
            return_value=[_stub_manifest("prometheus", "explicit")],
        ), patch.object(
            mcps_api.mcp_registry, "get_credential_schema",
            return_value={"type": "per_user"},
        ), patch.object(
            mcps_api.mcp_store, "get_manager_enabled_mcps",
            return_value=["bluesky-mcp"],
        ):
            return asyncio.run(mcps_api.get_agent_mcps(
                "test-agent",
                include_unauthorized=include_unauthorized,
                user=_admin_ctx(),
            ))

    def test_default_shape_has_no_unauthorized_rows(self, temp_db):
        out = self._run(include_unauthorized=False)
        names = [m["name"] for m in out["mcps"]]
        assert names == ["bluesky-mcp"]
        # Every default row is explicitly authorized — additive field only.
        assert all(m["authorized"] is True for m in out["mcps"])
        assert out["mcps"][0]["enabled"] is True

    def test_param_appends_flagged_rows(self, temp_db):
        out = self._run(include_unauthorized=True)
        by_name = {m["name"]: m for m in out["mcps"]}
        assert set(by_name) == {"bluesky-mcp", "prometheus"}
        prom = by_name["prometheus"]
        assert prom["authorized"] is False
        # Unauthorized ⇒ not deliverable at runtime, so never "enabled" —
        # even if a stale agent_mcps row lingers from a revoked instance.
        assert prom["enabled"] is False
        assert prom["assignment_mode"] == "explicit"


# ───────────────────────────────────────────────────────────────────────────
# Requests for installed-but-uncatalogued MCPs
# ───────────────────────────────────────────────────────────────────────────


class TestRequestInstalledMcp:
    def test_installed_zip_mcp_is_requestable_without_catalog(self, temp_db):
        """The catalog must not even be consulted for an installed MCP —
        a zip install is requestable during a catalog outage."""
        _seed_agent()
        from api.mcp.community import create_mcp_request, CreateRequestBody
        from services.community import community_catalog

        with patch(
            "services.mcp.mcp_registry.get_manifest",
            side_effect=lambda n: _stub_manifest("zip-mcp", "explicit") if n == "zip-mcp" else None,
        ), patch.object(
            community_catalog, "fetch_registry",
            new=AsyncMock(side_effect=RuntimeError("catalog down")),
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            row = asyncio.run(create_mcp_request(
                "test-agent",
                CreateRequestBody(mcp_name="zip-mcp", reason="needed"),
                user=_manager_ctx(),
            ))
        assert row["status"] == "pending"
        assert row["mcp_name"] == "zip-mcp"
        # Derived fields ride every request response.
        assert row["assignment_mode"] == "explicit"
        assert row["needs_instance"] is True
        assert row["instance_count"] == 0

    def test_unknown_everywhere_404s(self, temp_db):
        _seed_agent()
        from api.mcp.community import create_mcp_request, CreateRequestBody
        from services.community import community_catalog

        with patch(
            "services.mcp.mcp_registry.get_manifest", return_value=None,
        ), patch.object(
            community_catalog, "fetch_registry",
            new=AsyncMock(return_value={"mcps": [{"name": "something-else"}]}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(create_mcp_request(
                    "test-agent",
                    CreateRequestBody(mcp_name="ghost-mcp", reason="hm"),
                    user=_manager_ctx(),
                ))
        assert exc.value.status_code == 404

    def test_catalog_outage_still_502s_for_uninstalled(self, temp_db):
        _seed_agent()
        from api.mcp.community import create_mcp_request, CreateRequestBody
        from services.community import community_catalog

        with patch(
            "services.mcp.mcp_registry.get_manifest", return_value=None,
        ), patch.object(
            community_catalog, "fetch_registry",
            new=AsyncMock(side_effect=RuntimeError("catalog down")),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(create_mcp_request(
                    "test-agent",
                    CreateRequestBody(mcp_name="ghost-mcp", reason="hm"),
                    user=_manager_ctx(),
                ))
        assert exc.value.status_code == 502


# ───────────────────────────────────────────────────────────────────────────
# approve_request(instance_id=...)
# ───────────────────────────────────────────────────────────────────────────


class TestApproveWithInstanceId:
    def _explicit_patch(self, name: str = "prometheus"):
        return patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_stub_manifest(name, "explicit"),
        )

    def _notify_patch(self):
        return patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        )

    def test_selected_instance_wins_over_lowest_id(self, temp_db):
        """Two instances; the admin picks the SECOND — the agent must land
        on that one, not the lowest-id automatic choice."""
        _seed_agent()
        from services.community import community_installer
        from storage import mcp_request_store, mcp_store

        first = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "first", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        second = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "second", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        row = mcp_request_store.create_request("prometheus", "test-agent", REQUESTER_SUB)

        with self._explicit_patch(), self._notify_patch():
            updated = asyncio.run(community_installer.approve_request(
                row["id"], ADMIN_SUB, instance_id=second,
            ))

        assert updated["status"] == "installed"
        by_id = {i["id"]: i for i in mcp_store.get_mcp_instances("prometheus")}
        assert "test-agent" in by_id[second]["agents"]
        assert "test-agent" not in by_id[first]["agents"]
        assert "admin-selected" in (updated["install_log"] or "")

    def test_wrong_mcp_instance_id_400s_before_any_transition(self, temp_db):
        """An instance belonging to ANOTHER MCP is rejected — and the row
        must still be pending (a mid-cascade 400 would strand it in
        ``installing``, which only exits via installed/install_failed)."""
        _seed_agent()
        from services.community import community_installer
        from storage import mcp_request_store, mcp_store

        alien = mcp_store.upsert_mcp_instance("other-mcp", {
            "instance_name": "alien", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        row = mcp_request_store.create_request("prometheus", "test-agent", REQUESTER_SUB)

        with self._explicit_patch(), self._notify_patch():
            with pytest.raises(HTTPException) as exc:
                asyncio.run(community_installer.approve_request(
                    row["id"], ADMIN_SUB, instance_id=alien,
                ))
        assert exc.value.status_code == 400
        assert mcp_request_store.get_request(row["id"])["status"] == "pending"

    def test_auto_mode_instance_id_400s(self, temp_db):
        _seed_agent()
        from services.community import community_installer
        from storage import mcp_request_store

        row = mcp_request_store.create_request("bluesky-mcp", "test-agent", REQUESTER_SUB)
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=_stub_manifest("bluesky-mcp", "auto"),
        ), self._notify_patch():
            with pytest.raises(HTTPException) as exc:
                asyncio.run(community_installer.approve_request(
                    row["id"], ADMIN_SUB, instance_id=1,
                ))
        assert exc.value.status_code == 400
        assert "auto-mode" in str(exc.value.detail)

    def test_uninstalled_instance_id_400s(self, temp_db):
        _seed_agent()
        from services.community import community_installer
        from storage import mcp_request_store

        row = mcp_request_store.create_request("ghost-mcp", "test-agent", REQUESTER_SUB)
        with patch(
            "services.community.community_installer.mcp_registry.get_manifest",
            return_value=None,
        ), self._notify_patch():
            with pytest.raises(HTTPException) as exc:
                asyncio.run(community_installer.approve_request(
                    row["id"], ADMIN_SUB, instance_id=1,
                ))
        assert exc.value.status_code == 400
        assert "not installed" in str(exc.value.detail)

    def test_omitted_instance_id_keeps_lowest_id_auto_attach(self, temp_db):
        """Backward compatibility: no instance_id → today's behavior, which
        the instance-save auto-retry hook depends on."""
        _seed_agent()
        from services.community import community_installer
        from storage import mcp_request_store, mcp_store

        first = mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "first", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "second", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        row = mcp_request_store.create_request("prometheus", "test-agent", REQUESTER_SUB)

        with self._explicit_patch(), self._notify_patch():
            updated = asyncio.run(community_installer.approve_request(row["id"], ADMIN_SUB))

        assert updated["status"] == "installed"
        by_name = {i["instance_name"]: i for i in mcp_store.get_mcp_instances("prometheus")}
        assert "test-agent" in by_name["first"]["agents"]
        assert first == by_name["first"]["id"]

    def test_helper_vanished_selected_instance_falls_back(self, temp_db):
        """The id passed validation but the instance was deleted mid-approve
        → the helper silently takes the automatic path instead of failing."""
        _seed_agent()
        from services.community import community_installer
        from storage import mcp_store

        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "survivor", "field_values": {},
            "agents": [], "assigned_to_all": False,
        })
        with self._explicit_patch():
            status, _log = community_installer._ensure_agent_authorized_for_instance_mcp(
                "prometheus", "test-agent", instance_id=999_999,
            )
        assert status == "added_to_first"


# ───────────────────────────────────────────────────────────────────────────
# Derived display fields on request rows
# ───────────────────────────────────────────────────────────────────────────


class TestDerivedDisplayFields:
    def _augment(self, row: dict) -> dict:
        from api.mcp.community import _augmented_row
        return asyncio.run(_augmented_row(row))

    def _row(self, **over) -> dict:
        base = {
            "id": 1, "mcp_name": "prometheus", "agent_slug": "test-agent",
            "status": "install_failed", "kind": "mcp",
        }
        base.update(over)
        return base

    def test_open_explicit_uncovered_needs_instance(self, temp_db):
        with patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=_stub_manifest("prometheus", "explicit"),
        ):
            out = self._augment(self._row())
        assert out["assignment_mode"] == "explicit"
        assert out["needs_instance"] is True
        assert out["instance_count"] == 0

    def test_covering_instance_clears_the_flag(self, temp_db):
        from storage import mcp_store
        mcp_store.upsert_mcp_instance("prometheus", {
            "instance_name": "default", "field_values": {},
            "agents": ["test-agent"], "assigned_to_all": False,
        })
        with patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=_stub_manifest("prometheus", "explicit"),
        ):
            out = self._augment(self._row())
        assert out["needs_instance"] is False
        assert out["instance_count"] == 1

    def test_terminal_status_never_needs_instance(self, temp_db):
        with patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=_stub_manifest("prometheus", "explicit"),
        ):
            out = self._augment(self._row(status="installed"))
        assert out["needs_instance"] is False

    def test_auto_mode_defaults(self, temp_db):
        with patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=_stub_manifest("bluesky-mcp", "auto"),
        ):
            out = self._augment(self._row(mcp_name="bluesky-mcp"))
        assert out["assignment_mode"] == "auto"
        assert out["needs_instance"] is False

    def test_uninstalled_mcp_is_null_mode(self, temp_db):
        with patch("services.mcp.mcp_registry.get_manifest", return_value=None):
            out = self._augment(self._row(mcp_name="ghost-mcp"))
        assert out["assignment_mode"] is None
        assert out["needs_instance"] is False

    def test_skill_kind_untouched(self, temp_db):
        out = self._augment(self._row(kind="skill", mcp_name="theme-pack"))
        assert out["assignment_mode"] is None
        assert out["needs_instance"] is False
        assert out["instance_count"] == 0


# ───────────────────────────────────────────────────────────────────────────
# Community-agent install: the zero-instances admin path is now guided
# ───────────────────────────────────────────────────────────────────────────


class TestAdminAgentInstallZeroInstances:
    def _write_template(self, tmp_path: Path, mcp_name: str) -> Path:
        template_dir = tmp_path / "demo-template"
        template_dir.mkdir(parents=True, exist_ok=True)
        (template_dir / "agent.json").write_text(json.dumps({
            "schema_version": "1", "slug": "demo-template",
            "display_name": "Demo Template", "description": "t",
            "color": "#10B981", "version": "1.0.0",
        }))
        (template_dir / "agent.md").write_text("# Prompt\n")
        (template_dir / "mcps.json").write_text(json.dumps({"required": [{"name": mcp_name}]}))
        (template_dir / "README.md").write_text("# README\n")
        return template_dir

    def test_installed_explicit_no_instances_lands_guided_not_pending(self, tmp_path, temp_db):
        """Change-4 consistency: previously this queued a plain ``pending``
        row at the admin themself, while the not-installed variant of the
        identical situation landed as a guided failure. Both now terminate
        in ``install_failed`` + the create-instance guidance."""
        from services.community.community_agent_installer import (
            install_from_extracted_template,
        )
        from storage.community_agent_template_store import load_template_from_dir

        tdir = self._write_template(tmp_path, "prometheus")
        template = load_template_from_dir(tdir)

        stub = _stub_manifest("prometheus", "explicit")
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={"prometheus": stub},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            side_effect=lambda n: stub if n == "prometheus" else None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = asyncio.run(install_from_extracted_template(
                template=template, target_slug="demo-agent",
                installer_user_sub=ADMIN_SUB, installer_role="admin",
                source_label="test",
            ))

        assert len(result["created_requests"]) == 1
        req = result["created_requests"][0]
        assert req["status"] == "install_failed"
        assert "Create an instance" in (req["install_log"] or "")
        assert result["ready_mcps"] == []
        # The enable was rolled back — no non-functional row for the UI.
        from storage import mcp_store
        assert "prometheus" not in mcp_store.get_manager_enabled_mcps("demo-agent")
