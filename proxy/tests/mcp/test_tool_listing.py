"""The listing job: one server started for one agent, its labels recorded,
the server closed; nothing for a server placed on a machine or one that
does not start."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from auth.path_policy import SecurityContext
from services.apps import headless_exec as hx
from services.mcp import mcp_manifest_store, mcp_permissions, tool_listing
from services.mcp.mcp_registry import CredentialConfig, McpManifest, ServerConfig
from tests._compute import placed, tools_run_here

AGENT = "listing-agent"

# One tool labelled read-only, one labelled writing, one with no label.
FAKE_SERVER = """\
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("fake")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def look(q: str = "") -> str:
    return q


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def touch(q: str = "") -> str:
    return q


@mcp.tool()
def plain(q: str = "") -> str:
    return q


mcp.run()
"""


def _manifest(name: str, placement: str = "any") -> McpManifest:
    return McpManifest(
        name=name, label=name, description="", version="1.0.0", category="custom",
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], placement=placement,
        mcp_dir=Path(config.MCPS_DIR) / "custom" / name,
    )


@pytest.fixture
def fake_mcp(tmp_path, monkeypatch, temp_db):
    server = tmp_path / "fake_server.py"
    server.write_text(FAKE_SERVER)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "listing-mcp": {"type": "stdio", "command": sys.executable, "args": [str(server)]},
    }}))

    class _NoWrap:
        class cfg:
            username = ""
            role = "manager"
            # Every builder's config names its host config dir: the
            # credential exit's attach reads it.
            host_claude_dir = str(tmp_path)

        def get_env_overrides(self):
            return {}

        def describe(self):
            return SimpleNamespace(to_dict=lambda: {})

    seen: dict = {}

    def fake_build(agent, row, mcps, session_id="", phone=False, reach=hx.FULL):
        seen["prefix"] = session_id.split("-", 1)[0]
        seen["mcps"] = set(mcps)
        seen["phone"] = phone
        ctx = SecurityContext(role="manager", username="", agent=agent, is_admin_agent=False,
                              placement=placed())
        return cfg, {}, {}, _NoWrap(), ctx

    monkeypatch.setattr(hx, "_build_session_parts", fake_build)
    tools_run_here(monkeypatch)
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {
        "listing-mcp": _manifest("listing-mcp"),
        "on-a-machine": _manifest("on-a-machine", placement="satellite_only"),
    })
    mcp_permissions.reset_cache()
    yield seen
    mcp_permissions.reset_cache()


def test_the_listing_records_the_labels_and_closes_the_server(fake_mcp):
    async def main():
        assert not tool_listing.is_listed("listing-mcp")
        assert await tool_listing.list_tools(AGENT, "listing-mcp") is True
    asyncio.run(main())
    assert fake_mcp == {"prefix": "list", "mcps": {"listing-mcp"}, "phone": True}
    assert mcp_permissions.known_tools("listing-mcp") == ["look", "plain", "touch"]
    assert mcp_permissions.reads_only(mcp_permissions.tool_hints("listing-mcp", "look"))
    assert not mcp_permissions.reads_only(mcp_permissions.tool_hints("listing-mcp", "touch"))
    assert not mcp_permissions.reads_only(mcp_permissions.tool_hints("listing-mcp", "plain"))
    assert tool_listing.is_listed("listing-mcp")
    # Nothing stays in the app pool: the listing's manager is its own.
    assert not hx._pool


def test_a_server_on_a_machine_or_one_that_fails_is_left_unlisted(fake_mcp, monkeypatch):
    async def main():
        assert await tool_listing.list_tools(AGENT, "on-a-machine") is False
        assert await tool_listing.list_tools(AGENT, "unknown-mcp") is False

        def broken(agent, row, mcps, session_id=""):
            raise RuntimeError("no credentials")
        monkeypatch.setattr(hx, "_build_session_parts", broken)
        assert await tool_listing.list_tools(AGENT, "listing-mcp") is False
    asyncio.run(main())
    assert fake_mcp == {}
    assert not tool_listing.is_listed("listing-mcp")


def test_a_listing_refused_for_want_of_a_compute_machine_runs_at_its_sign_in(fake_mcp, monkeypatch):
    """A tool enabled while the compute machine is away is not left unlisted
    until somebody enables it again."""
    from core import placement
    monkeypatch.setattr(tool_listing, "_owed", set())
    ran: list[tuple[str, str]] = []

    async def main():
        def no_machine(agent, row, mcps, session_id="", phone=False, reach=hx.FULL):
            raise placement.NoComputeMachine()
        monkeypatch.setattr(hx, "_build_session_parts", no_machine)
        assert await tool_listing.list_tools(AGENT, "listing-mcp") is False

        def other(agent, row, mcps, session_id="", phone=False, reach=hx.FULL):
            raise RuntimeError("no credentials")
        monkeypatch.setattr(hx, "_build_session_parts", other)
        assert await tool_listing.list_tools("another-agent", "listing-mcp") is False
        assert tool_listing._owed == {(AGENT, "listing-mcp")}

        monkeypatch.setattr(tool_listing, "schedule", lambda agent, name: ran.append((agent, name)))
        assert tool_listing.retry_owed() == 1 and tool_listing.retry_owed() == 0
    asyncio.run(main())
    assert ran == [(AGENT, "listing-mcp")]


def test_schedule_after_install_runs_the_listing_in_the_background(fake_mcp, monkeypatch):
    ran: list[str] = []

    async def fake_after(name):
        ran.append(name)
    monkeypatch.setattr(tool_listing, "after_install", fake_after)
    tool_listing.schedule_after_install("listing-mcp")   # no loop: nothing, nothing raised
    assert ran == []

    async def main():
        tool_listing.schedule_after_install("listing-mcp")
        await asyncio.gather(*tool_listing._jobs)
    asyncio.run(main())
    assert ran == ["listing-mcp"]


def test_schedule_runs_on_the_loop_and_nowhere_else(fake_mcp, monkeypatch):
    ran: list[tuple[str, str]] = []

    async def fake_list(agent, mcp_name):
        ran.append((agent, mcp_name))
        return True
    monkeypatch.setattr(tool_listing, "list_tools", fake_list)
    # No loop: nothing is started, nothing raises.
    tool_listing.schedule(AGENT, "listing-mcp")
    assert ran == []

    async def main():
        tool_listing.schedule(AGENT, "listing-mcp")
        await asyncio.gather(*tool_listing._jobs)
    asyncio.run(main())
    assert ran == [(AGENT, "listing-mcp")]
    assert not tool_listing._jobs


def test_the_listing_builds_with_an_outside_sessions_contexts(fake_mcp, monkeypatch):
    """The listing's manager is built as an outside session is, not as a
    task: a server a manifest keeps out of tasks is still listed."""
    async def main():
        await tool_listing.list_tools(AGENT, "listing-mcp")
    asyncio.run(main())
    assert fake_mcp["phone"] is True


def test_the_managers_build_passes_the_phone_contexts(temp_db, monkeypatch):
    """``phone=True`` reaches the config build as ``phone_mode`` with
    ``task_mode`` off; an app button's build stays a task's."""
    from services.mcp import mcp_registry
    from storage.agents import agent_store
    if not agent_store.agent_exists(AGENT):
        agent_store.create_agent(AGENT, "Listing Agent")
    seen: list[dict] = []

    def fake_config(agent, sub, **kw):
        seen.append(kw)
        return None, {}, {}, {}, set()
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", fake_config)
    monkeypatch.setattr("core.sandbox.sandbox.resolve_sandbox_config",
                        lambda **kw: __import__("types").SimpleNamespace(**kw))
    monkeypatch.setattr("core.sandbox.sandbox.SandboxBuilder", lambda cfg: cfg)
    # A listing's tool starts on the compute machine that serves.
    from tests._compute import serve
    serve(monkeypatch)
    hx._build_session_parts(AGENT, {}, frozenset({"x"}), "list-1", phone=True)
    hx._build_session_parts(AGENT, {}, frozenset({"x"}), "appx-1")
    assert [(k["phone_mode"], k["task_mode"]) for k in seen] == [(True, False), (False, True)]


def test_after_install_lists_the_server_for_one_agent_that_has_it(fake_mcp, monkeypatch):
    """An update or a reinstall lists the server again through one agent
    that has it enabled (the labels are per server, so one listing serves
    every agent's picks); a skill package, an unknown name and a server no
    agent holds list nothing."""
    from services.mcp import mcp_registry
    from storage.agents import outside_store
    from storage.mcp import mcp_store
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(tool_listing, "schedule", lambda agent, mcp: scheduled.append((agent, mcp)))
    enabled = {"listing-mcp": [AGENT, "second-agent"]}
    monkeypatch.setattr(mcp_store, "get_mcp_agents", lambda mcp: list(enabled.get(mcp, [])))
    picking = {"listing-mcp": []}
    monkeypatch.setattr(outside_store, "agents_with_pick", lambda mcp: list(picking.get(mcp, [])))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements",
                        lambda agent: [_manifest("listing-mcp")] if agent in (AGENT, "second-agent") else [])
    mcp_manifest_store._manifests["skills-pack"] = _manifest("skills-pack")
    mcp_manifest_store._manifests["skills-pack"].category = "skill"

    async def main():
        await tool_listing.after_install("listing-mcp")
        await tool_listing.after_install("skills-pack")
        await tool_listing.after_install("unknown-mcp")
        # An agent whose picks name the tool comes first.
        picking["listing-mcp"] = ["second-agent"]
        await tool_listing.after_install("listing-mcp")
        # A pick on an agent that no longer holds the tool is skipped.
        picking["listing-mcp"] = ["gone-agent"]
        await tool_listing.after_install("listing-mcp")
        enabled["listing-mcp"] = []
        picking["listing-mcp"] = []
        await tool_listing.after_install("listing-mcp")
    asyncio.run(main())
    assert scheduled == [(AGENT, "listing-mcp"), ("second-agent", "listing-mcp"), (AGENT, "listing-mcp")]


def test_the_bundled_tools_a_pick_names_are_listed_again_when_their_version_moves(fake_mcp, monkeypatch, temp_db):
    """A platform upgrade changes the bundled tools with no install: at
    boot the ones a pick names are listed again once per version; an
    installed tool and an unpicked one are not."""
    from storage import database as task_store
    from storage.agents import outside_store
    installed = _manifest("installed-mcp")
    installed.mcp_dir = Path(config.MCPS_DIR) / "community" / "installed-mcp"
    mcp_manifest_store._manifests["installed-mcp"] = installed
    monkeypatch.setattr(outside_store, "picked_mcp_names", lambda: ["listing-mcp", "installed-mcp", "unknown-mcp"])
    listed: list[str] = []
    monkeypatch.setattr(tool_listing, "schedule_after_install", listed.append)

    async def main():
        assert await tool_listing.relist_bundled_after_upgrade() == "1 bundled tool(s) listed again"
        assert await tool_listing.relist_bundled_after_upgrade() is None
        mcp_manifest_store._manifests["listing-mcp"].version = "1.0.1"
        assert await tool_listing.relist_bundled_after_upgrade() == "1 bundled tool(s) listed again"
    asyncio.run(main())
    assert listed == ["listing-mcp", "listing-mcp"]
    assert json.loads(task_store.get_platform_setting(tool_listing.BUNDLED_VERSIONS_KEY)) == {"listing-mcp": "1.0.1"}


def test_an_update_lists_the_server_again(fake_mcp, monkeypatch):
    """The update path (the admin's button and the weekly job share it)
    ends with the listing, so a maker's relabel reaches the picks and the
    managers' notice without waiting for a Direct LLM or Codex session."""
    from services.mcp import mcp_updater

    async def fake_update(name, manifest, *, settled_only=False):
        return {"status": "updated", "name": name, "version": "1.0.1"}
    monkeypatch.setattr(mcp_updater, "_update_node_python_mcp", fake_update)
    monkeypatch.setattr(mcp_updater, "_forget_check_result", lambda name: None)
    monkeypatch.setattr(mcp_updater, "_set_phase", lambda *a, **k: None)
    listed: list[str] = []
    monkeypatch.setattr(tool_listing, "schedule_after_install", listed.append)

    async def main():
        result = await mcp_updater.update_one("listing-mcp")
        assert result["status"] == "updated"
        monkeypatch.setattr(mcp_updater, "_update_node_python_mcp",
                            lambda name, manifest, *, settled_only=False: _waiting(name))
        await mcp_updater.update_one("listing-mcp", settled_only=True)

    async def _waiting(name):
        return {"status": "waiting", "name": name}
    asyncio.run(main())
    # A release that still waits lists nothing: the server did not change.
    assert listed == ["listing-mcp"]


@pytest.mark.asyncio
async def test_the_admins_upload_lists_an_updated_tool(fake_mcp, monkeypatch, tmp_path):
    """The ZIP upload route replaces an installed MCP in place: the route
    schedules the listing after the install, so a relabel shipped this way
    reaches the picks (a first install has no agent to list through, which
    ``after_install`` answers with nothing)."""
    from types import SimpleNamespace

    from api.mcp import mcps as mcps_api
    from auth.providers import UserContext
    from services.community import community_installer
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"name": "weather-tools"}))

    async def fake_extract(file, tmp):
        return tmp_path
    monkeypatch.setattr(mcps_api, "_extract_upload_zip", fake_extract)
    outcomes = iter([{"status": "updated", "name": "weather-tools"},
                     {"status": "installed", "name": "weather-tools"}])

    async def fake_install(folder, **kw):
        assert folder == root
        return next(outcomes)
    monkeypatch.setattr(community_installer, "install_from_extracted_folder", fake_install)
    listed: list[str] = []
    monkeypatch.setattr(tool_listing, "schedule_after_install", listed.append)
    admin = UserContext(sub="user-admin", email="a@x", name="A", role="admin")
    upload = SimpleNamespace(filename="pkg.zip")
    assert (await mcps_api.install_mcp(file=upload, user=admin))["status"] == "updated"
    assert (await mcps_api.install_mcp(file=upload, user=admin))["status"] == "installed"
    assert listed == ["weather-tools", "weather-tools"]
