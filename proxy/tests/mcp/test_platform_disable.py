"""Platform-level MCP disable: core is locked EXCEPT the parallelism pair.

meetings-mcp and delegation-mcp are the only core MCPs an admin may disable —
their backends gate on mcp_state, so the toggle is a real kill-switch. The
admin listing exposes the same policy as ``can_disable`` per row.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api.mcp import mcps as mcps_api
from auth.providers import UserContext
from storage.mcp import mcp_store


def _admin() -> UserContext:
    return UserContext(sub="u-admin", email="a@x.test", name="A", role="admin")


def _make_manifest(name, category="core"):
    from services.mcp.mcp_registry import (
        CredentialConfig,
        McpManifest,
        ServerConfig,
    )
    return McpManifest(
        name=name,
        label=name.replace("-", " ").title(),
        description="",
        version="1.0.0",
        category=category,
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(type="none"),
        config=[],
        env={},
        agent_env={},
        exclude_from=[],
        skills=[],
        assignment_mode="auto",
    )


@pytest.fixture
def core_manifests(temp_db, monkeypatch):
    from services.mcp import mcp_registry
    manifests = {
        name: _make_manifest(name)
        for name in ("meetings-mcp", "delegation-mcp", "schedules-mcp", "memory-mcp")
    }
    monkeypatch.setattr(mcp_registry, "_manifests", manifests)
    for name in manifests:
        mcp_store.set_mcp_enabled(name, True)
    return manifests


def test_parallelism_core_pair_is_disableable(core_manifests):
    for name in ("meetings-mcp", "delegation-mcp"):
        out = asyncio.run(mcps_api.disable_mcp(name, user=_admin()))
        assert out == {"status": "disabled", "name": name}
        state = mcp_store.get_mcp_state(name)
        assert state and not state.get("enabled")


def test_other_core_mcps_stay_locked(core_manifests):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mcps_api.disable_mcp("schedules-mcp", user=_admin()))
    assert ei.value.status_code == 400
    state = mcp_store.get_mcp_state("schedules-mcp")
    assert state and state.get("enabled")


def test_listing_reports_can_disable(core_manifests):
    result = asyncio.run(mcps_api.list_mcps(user=_admin()))
    by_name = {m["name"]: m for m in result["mcps"]}
    assert by_name["meetings-mcp"]["can_disable"] is True
    assert by_name["delegation-mcp"]["can_disable"] is True
    assert by_name["schedules-mcp"]["can_disable"] is False
    assert by_name["memory-mcp"]["can_disable"] is False
    # Provenance rides every row: empty on bundled manifests, and no icon.png
    # in the fixture's mcp_dir.
    assert by_name["memory-mcp"]["author"] == ""
    assert by_name["memory-mcp"]["author_url"] == ""
    assert by_name["memory-mcp"]["icon"] is False


def test_a_switch_registers_the_mcps_relay_providers_again(core_manifests, monkeypatch):
    from services.webhooks import relay_events
    asked: list[str] = []
    monkeypatch.setattr(relay_events, "sync_providers_of_mcp_soon", asked.append)
    asyncio.run(mcps_api.disable_mcp("meetings-mcp", user=_admin()))
    asyncio.run(mcps_api.enable_mcp("meetings-mcp", user=_admin()))
    assert asked == ["meetings-mcp", "meetings-mcp"]


@pytest.mark.parametrize("in_use", [True, False])
def test_an_uninstall_deletes_its_subscriptions_first(temp_db, monkeypatch, tmp_path, in_use):
    """Vendor delete and row delete before anything of the MCP goes; after
    the rescan the relay endpoint registered again, only for a provider the
    install used (one nothing used never reaches the relay)."""
    from types import SimpleNamespace

    from services.mcp import mcp_registry
    from services.webhooks import relay_events, subscription_manager
    from storage.identity import credential_store
    from storage.mcp import mcp_update_state_store
    manifest = _make_manifest("slack-mcp", category="community")
    manifest.mcp_dir = tmp_path / "slack-mcp"
    manifest.credentials = SimpleNamespace(webhooks={"provider_id": "slack"}, oauth={"provider_id": "slack"})
    monkeypatch.setattr(mcp_registry, "_manifests", {"slack-mcp": manifest})
    order: list[str] = []

    async def cleanup(name):
        order.append(f"subscriptions:{name}")
        return 1

    async def rescan():
        order.append("rescan")
        return {}
    monkeypatch.setattr(subscription_manager, "cleanup_mcp_subscriptions", cleanup)
    monkeypatch.setattr(mcp_store, "delete_mcp_all_data", lambda *a: order.append("data"))
    monkeypatch.setattr(credential_store, "delete_all_mcp_credentials", lambda n: order.append("credentials"))
    monkeypatch.setattr(mcp_update_state_store, "delete_mcp_rows", lambda n: None)
    monkeypatch.setattr(mcp_registry, "rescan", rescan)
    monkeypatch.setattr(relay_events, "sync_provider_soon", lambda p: order.append(f"sync:{p}"))
    monkeypatch.setattr(relay_events, "providers_in_use", lambda: ["github", "slack"] if in_use else ["github"])
    assert asyncio.run(mcps_api._delete_mcp_locked("slack-mcp")) == {"status": "deleted", "name": "slack-mcp"}
    assert order == ["subscriptions:slack-mcp", "data", "credentials", "rescan",
                     *(["sync:slack"] if in_use else [])]
