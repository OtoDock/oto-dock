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
        McpManifest, ServerConfig, CredentialConfig,
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
