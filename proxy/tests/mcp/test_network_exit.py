"""The platform browser's switch and its container's own exit.

A Docker MCP that declares ``network_exit`` (the platform browser) closes its
own local network when the platform asks: the local-network approval is the
switch (off by its manifest, off for good on a hosted install), the compose
the platform runs carries the variable and, while closed, the one capability
the container's entrypoint closes the exit with, and no session of an
outside person or of a session kind's own gets the browser while it is open.
"""

import asyncio
import types

import pytest
import yaml
from fastapi.testclient import TestClient

import config
from app import app
from auth.providers import create_session_jwt
from core.config import deployment
from services.mcp import compose_rewrite as cr, mcp_egress, mcp_session_filter
from services.mcp.mcp_registry import McpManifest, ServerConfig

BROWSER_COMPOSE = {"services": {"camoufox": {
    "build": {"context": "."}, "environment": ["OTO_MCP_SUPPRESS_SERVER_REQUESTS=1"],
    "cap_add": ["SYS_ADMIN"], "security_opt": ["seccomp:unconfined"],
    "ports": ["127.0.0.1:8931:8931"],
}}}


def _browser(tmp_path, *, default=False) -> McpManifest:
    m = McpManifest.__new__(McpManifest)
    m.name, m.mcp_dir, m.network_targets, m.placement = "camoufox", tmp_path, [], "any"
    m.network_exit, m.network_access_default = True, default
    m.requires_capability = None
    m.server = ServerConfig(runtime="docker", transport="http", port=8931,
                            docker_compose="docker-compose.yml", service_name="camoufox",
                            image="ghcr.io/otodock/camoufox:0.0.78")
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(BROWSER_COMPOSE))
    return m


@pytest.fixture(autouse=True)
def _values(monkeypatch):
    stored: dict = {}
    monkeypatch.setattr(mcp_egress.mcp_store, "get_mcp_config_values", lambda n: dict(stored))
    mcp_session_filter.note_open_exits(())
    yield stored
    mcp_session_filter.note_open_exits(())


def test_the_switch_is_the_approval_closed_by_default_and_on_a_hosted_install(tmp_path, monkeypatch, _values):
    m = _browser(tmp_path)
    assert mcp_egress.network_access_enabled(m) is False
    _values["_network_access"] = "true"
    assert mcp_egress.network_access_enabled(m) is True
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    assert mcp_egress.network_access_enabled(m) is False


def test_the_t2_compose_carries_the_exit_and_the_capability_only_while_closed(tmp_path, monkeypatch, _values):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    m = _browser(tmp_path)
    _path, _data, planned = cr._plan_pull_compose(m)
    svc = planned["services"]["camoufox"]
    assert "OTODOCK_LOCAL_NETWORK=closed" in svc["environment"]
    assert "OTO_MCP_SUPPRESS_SERVER_REQUESTS=1" in svc["environment"]
    assert svc["cap_add"] == ["NET_ADMIN"], "the catalog's own capability is gone"
    assert svc["security_opt"] == ["no-new-privileges:true"]
    _values["_network_access"] = "true"
    _path, _data, planned = cr._plan_pull_compose(m)
    svc = planned["services"]["camoufox"]
    assert "OTODOCK_LOCAL_NETWORK=open" in svc["environment"]
    assert "cap_add" not in svc and "security_opt" not in svc


def test_a_rewrite_of_its_own_closed_compose_warns_about_nothing(tmp_path, monkeypatch, _values,
                                                                 caplog):
    """The file the rewrite wrote carries the platform's own keys: the next
    start strips and sets them again without a security warning; a key the
    catalog added beside them still warns."""
    import logging
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    m = _browser(tmp_path)
    path, _data, planned = cr._plan_pull_compose(m)
    path.write_text(yaml.safe_dump(planned))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=cr.logger.name):
        _path, _data, again = cr._plan_pull_compose(m)
    assert "stripped unsafe keys" not in caplog.text
    assert again["services"]["camoufox"]["cap_add"] == ["NET_ADMIN"]
    planned["services"]["camoufox"]["cap_add"] = ["NET_ADMIN", "SYS_ADMIN"]
    path.write_text(yaml.safe_dump(planned))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=cr.logger.name):
        _path, _data, again = cr._plan_pull_compose(m)
    assert "stripped unsafe keys" in caplog.text
    assert again["services"]["camoufox"]["cap_add"] == ["NET_ADMIN"]


def test_an_mcp_without_the_field_gets_none_of_it(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    m = _browser(tmp_path)
    m.network_exit = False
    _path, _data, planned = cr._plan_pull_compose(m)
    svc = planned["services"]["camoufox"]
    assert not any("OTODOCK_LOCAL_NETWORK" in e for e in svc["environment"])
    assert "cap_add" not in svc


def test_the_t1_override_carries_the_exit(tmp_path, monkeypatch, _values):
    monkeypatch.setattr(config, "INSTALL_ID", "testid")
    monkeypatch.setattr(config, "OTODOCK_MCP_ADDRESS_POOL", "10.201.0.0/16")
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    monkeypatch.setattr(cr, "_collect_used_subnets", lambda exclude_network=None: [])
    monkeypatch.setattr(cr, "_picked_subnets", {})
    m = _browser(tmp_path)
    override = yaml.safe_load(cr.ensure_t1_override(m).read_text())
    svc = override["services"]["camoufox"]
    assert svc["environment"] == {"OTODOCK_LOCAL_NETWORK": "closed"}
    assert svc["cap_add"] == ["NET_ADMIN"] and svc["security_opt"] == ["no-new-privileges:true"]


def test_no_outside_session_gets_an_open_browser():
    m = types.SimpleNamespace(name="camoufox", exclude_from=[])
    picks = types.SimpleNamespace(admits=lambda name: True)
    rule = mcp_session_filter.KindRule(denied=frozenset(), admitted=frozenset(), reason="x")
    assert mcp_session_filter.session_exclusion_reason(
        m, contexts=set(), external=True, picks=picks) is None
    mcp_session_filter.note_open_exit("camoufox", True)
    assert mcp_session_filter.session_exclusion_reason(
        m, contexts=set(), external=True, picks=picks) == mcp_session_filter.OPEN_EXIT_REASON
    assert mcp_session_filter.session_exclusion_reason(
        m, contexts=set(), external=False, kind_rule=rule) == mcp_session_filter.OPEN_EXIT_REASON
    assert mcp_session_filter.session_exclusion_reason(m, contexts=set(), external=False) is None
    mcp_session_filter.note_open_exit("camoufox", False)
    assert mcp_session_filter.session_exclusion_reason(
        m, contexts=set(), external=True, picks=picks) is None


def test_the_route_opens_the_exit_and_recreates_a_running_browser(tmp_path, monkeypatch, temp_db):
    from api.mcp import mcps
    m = _browser(tmp_path)
    monkeypatch.setattr(mcps.mcp_registry, "get_manifest", lambda name: m if name == "camoufox" else None)
    monkeypatch.setattr(mcps.mcp_store, "set_mcp_config_value", lambda *a: None)
    monkeypatch.setattr(mcps.docker_manager, "get_container_status", lambda man: "running")
    recreated: list = []
    monkeypatch.setattr(mcps.docker_manager, "start_container",
                        lambda man, force_recreate=False: recreated.append(force_recreate) or True)
    admin = TestClient(app, cookies={"session": create_session_jwt(
        "user-admin", "admin@test.com", "Admin User", "admin")})
    r = admin.put("/v1/admin/mcps/camoufox/network-access", json={"enabled": True})
    assert r.status_code == 200 and r.json()["restarted"] is True
    assert recreated == [True]
    assert "camoufox" in mcp_session_filter._open_exits
    from services.mcp import mcp_updater
    monkeypatch.setattr(mcp_updater, "update_running", lambda name: True)
    r = admin.put("/v1/admin/mcps/camoufox/network-access", json={"enabled": False})
    assert r.status_code == 409 and recreated == [True]
    assert "camoufox" in mcp_session_filter._open_exits
    monkeypatch.setattr(mcp_updater, "update_running", lambda name: False)
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    assert admin.put("/v1/admin/mcps/camoufox/network-access",
                     json={"enabled": True}).status_code == 400


def test_the_manifest_field_is_parsed_strictly(tmp_path):
    import json

    from services.mcp import mcp_manifest_parse
    for raw, want in ((True, True), ("true", False), (1, False), (None, False)):
        d = tmp_path / f"m{want}{raw!r}".replace(" ", "")
        d.mkdir()
        body = {"name": "b", "server": {"runtime": "docker", "transport": "http", "port": 1}}
        if raw is not None:
            body["network_exit"] = raw
        (d / "manifest.json").write_text(json.dumps(body))
        parsed = mcp_manifest_parse._parse_manifest(d / "manifest.json")
        assert parsed is not None and parsed.network_exit is want, raw


def test_a_failed_recreate_keeps_the_setting(tmp_path, monkeypatch):
    from api.mcp import mcps
    m = _browser(tmp_path)
    monkeypatch.setattr(mcps.docker_manager, "get_container_status", lambda man: "running")

    def boom(man, force_recreate=False):
        raise RuntimeError("no daemon")
    monkeypatch.setattr(mcps.docker_manager, "start_container", boom)
    assert asyncio.run(mcps._recreate_if_running(m)) is False


def test_a_browser_that_is_up_is_recreated_even_disabled_or_starting(tmp_path, monkeypatch):
    """Enabling a disabled MCP recreates nothing, and a second change while
    the first recreate still starts would be lost: any container that is up
    takes the new exit when the switch changes, not at some later start."""
    from api.mcp import mcps
    m = _browser(tmp_path)
    monkeypatch.setattr(mcps.mcp_store, "get_mcp_state", lambda name: {"enabled": False})
    recreated: list = []
    monkeypatch.setattr(mcps.docker_manager, "start_container",
                        lambda man, force_recreate=False: recreated.append(force_recreate) or True)
    for up in ("running", "starting", "unhealthy"):
        monkeypatch.setattr(mcps.docker_manager, "get_container_status", lambda man, up=up: up)
        assert asyncio.run(mcps._recreate_if_running(m)) is True, up
    for down in ("stopped", "not_found", "error"):
        monkeypatch.setattr(mcps.docker_manager, "get_container_status", lambda man, down=down: down)
        assert asyncio.run(mcps._recreate_if_running(m)) is False, down
    assert recreated == [True, True, True]


def test_a_second_change_of_the_browser_meets_the_409_while_the_first_runs(tmp_path, monkeypatch):
    """Two compose runs on one project collide: the store, the exit set and
    the recreate hold the MCP's install lock, and a change that finds it
    held is refused, so the container never keeps one exit while the
    switch reads the other."""
    from fastapi import HTTPException

    from api.mcp import mcps
    from core.credentials import catalog_install_registry
    m = _browser(tmp_path)
    monkeypatch.setattr(catalog_install_registry, "_name_locks", {})
    stored: list = []
    monkeypatch.setattr(mcps.mcp_store, "set_mcp_config_value", lambda *a: stored.append(a[2]))

    async def go():
        gate = asyncio.Event()

        async def slow(man):
            await gate.wait()
            return True
        monkeypatch.setattr(mcps, "_recreate_if_running", slow)
        first = asyncio.create_task(mcps._apply_network_access(m, True))
        await asyncio.sleep(0.05)
        with pytest.raises(HTTPException) as refused:
            await mcps._apply_network_access(m, False)
        assert refused.value.status_code == 409
        gate.set()
        assert await first is True
    asyncio.run(go())
    assert stored == ["true"] and "camoufox" in mcp_session_filter._open_exits


def test_the_config_route_writes_the_approval_through_the_switch(tmp_path, monkeypatch):
    """``PUT …/config`` with ``_network_access`` takes the switch's path: the
    exit set follows, the browser is recreated, the hosted refusal holds,
    and the other keys are stored as before."""
    from api.mcp import mcps
    m = _browser(tmp_path)
    prom = McpManifest.__new__(McpManifest)
    prom.name, prom.network_targets, prom.network_exit = "plain", [], False
    manifests = {"camoufox": m, "plain": prom}
    monkeypatch.setattr(mcps.mcp_registry, "get_manifest", manifests.get)
    one: list = []
    many: list = []
    monkeypatch.setattr(mcps.mcp_store, "set_mcp_config_value", lambda *a: one.append(a[1:]))
    monkeypatch.setattr(mcps.mcp_store, "set_mcp_config_values", lambda n, v: many.append(v))
    recreated: list = []

    async def recreate(man):
        recreated.append(man.name)
        return True
    monkeypatch.setattr(mcps, "_recreate_if_running", recreate)
    admin = TestClient(app, cookies={"session": create_session_jwt(
        "user-admin", "admin@test.com", "Admin User", "admin")})
    url = "/v1/admin/mcps/camoufox/config"
    r = admin.put(url, json={"values": {"_network_access": "true", "OTHER": "x"}})
    assert r.status_code == 200
    assert one == [("_network_access", "true")] and many == [{"OTHER": "x"}]
    assert recreated == ["camoufox"] and "camoufox" in mcp_session_filter._open_exits
    assert admin.put(url, json={"values": {"_network_access": "yes"}}).status_code == 400
    assert admin.put("/v1/admin/mcps/plain/config",
                     json={"values": {"_network_access": "true"}}).status_code == 400
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    assert admin.put(url, json={"values": {"_network_access": "true"}}).status_code == 400
    assert len(one) == 1
