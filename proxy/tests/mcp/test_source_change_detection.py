"""A community MCP's catalog source changed: how the check sees it, what it
tells the admin, and what it persists.

The identity on both sides (``mcp_source_swap.entry_identity`` from the
registry's fields, the catalog manifest as the fallback), the credential
plan (carry, rename, reconnect, OAuth), the ``replaces`` declaration judged
by identity, the detection pass inside ``detect_available_updates`` (a
source change replaces the ordinary axes, a revert drops the pending row,
a switching row is left alone), and the two tables the page reads back.
"""

from __future__ import annotations

import json

import pytest

from services.community import community_catalog, mcp_source_swap as swap
from services.mcp import mcp_registry, mcp_updater
from storage.mcp import mcp_update_state_store as store


# ── fixtures ───────────────────────────────────────────────────────

def _manifest(name="drill-mcp", runtime="node", source="npm:drill", image="",
              url_template="", version="1.0.0", **extra) -> dict:
    server = {"runtime": runtime, "transport": "stdio", "command": "node",
              "source": source}
    if image:
        server["image"] = image
    if url_template:
        server["url_template"] = url_template
        server["transport"] = "streamable_http"
    if runtime == "docker":
        server["transport"] = "http"
        server["docker_compose"] = "docker-compose.yml"
        server["port"] = 8999
    if runtime == "remote":
        server.pop("runtime")
        server.pop("command")
    data = {"name": name, "label": name, "description": "d", "version": version,
            "category": "community", "server": server}
    data.update(extra)
    return data


def _installed(tmp_path, data: dict):
    """An installed manifest: the raw file on disk plus the registry object."""
    folder = tmp_path / data["name"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(data))
    s = data["server"]
    return mcp_registry.McpManifest(
        name=data["name"], label=data["name"], description="d",
        version=data["version"], category="community",
        server=mcp_registry.ServerConfig(
            runtime=s.get("runtime", ""), transport=s.get("transport", "stdio"),
            source=s.get("source", ""), image=s.get("image", ""),
            url_template=s.get("url_template", ""),
        ),
        credentials=mcp_registry.CredentialConfig(type="none"),
        config=[], env={}, agent_env={}, exclude_from=[], skills=[], mcp_dir=folder,
    )


def _entry(data: dict, **fields) -> dict:
    """The registry entry the generator would write for ``data``."""
    s = data["server"]
    runtime = s.get("runtime") or ("remote" if str(s.get("source", "")).startswith("remote:") else "")
    entry = {
        "name": data["name"], "version": data["version"], "runtime": runtime,
        "source": s.get("source", ""),
        "manifest_hash": community_catalog.normalized_manifest_hash(data),
        "manifest_url": f"./{data['name']}/manifest.json",
    }
    entry.update(fields)
    return entry


@pytest.fixture
def fake_catalog(monkeypatch):
    """``registry`` entries and ``manifests`` by folder, served to the
    detection as the catalog; the skills registry empty."""
    state = {"registry": [], "manifests": {}, "fetched": []}

    async def _registry():
        return {"mcps": list(state["registry"])}

    async def _manifest(folder):
        state["fetched"].append(folder)
        if folder not in state["manifests"]:
            raise RuntimeError(f"no manifest for {folder}")
        return state["manifests"][folder]

    async def _skills():
        return {"skills": []}

    async def _latest(registry, package, constraint):
        return None

    monkeypatch.setattr(community_catalog, "fetch_registry", _registry)
    monkeypatch.setattr(community_catalog, "fetch_manifest", _manifest)
    monkeypatch.setattr(community_catalog, "fetch_skills_registry", _skills)
    monkeypatch.setattr(mcp_updater, "resolve_latest_in_bound", _latest)
    return state


# ── identity from the registry ──────────────────────────────────────

def test_entry_identity_reads_the_registry_fields():
    assert swap.entry_identity({"runtime": "node", "source": "npm:drill"}) == ("npm", "drill")
    assert swap.entry_identity({"runtime": "python", "source": "pypi:Drill_MCP"}) == ("pypi", "drill-mcp")
    assert swap.entry_identity({
        "runtime": "python", "source": "git+https://host/r.git@v1#subdirectory=mcp",
    }) == ("git", "git+https://host/r#mcp")
    assert swap.entry_identity({
        "runtime": "docker", "source": "docker:drill", "image": "ghcr.io/otodock/drill:1.0.0",
    }) == ("image", "ghcr.io/otodock/drill")
    assert swap.entry_identity({"runtime": "docker", "source": "docker:drill", "image": ""}) == ("image", "")
    assert swap.entry_identity({
        "runtime": "remote", "source": "remote:mcp.linear.app", "url_host": "MCP.linear.app",
    }) == ("remote", "mcp.linear.app")


def test_entry_identity_is_none_without_the_field_its_runtime_needs():
    # A registry generated before the fields existed: judged from the manifest.
    assert swap.entry_identity({"runtime": "docker", "source": "docker:drill"}) is None
    assert swap.entry_identity({"runtime": "remote", "source": "remote:x"}) is None


def test_entry_folder_comes_from_the_manifest_url():
    assert swap.entry_folder({"name": "google-workspace",
                              "manifest_url": "./workspace-mcp/manifest.json"}) == "workspace-mcp"
    assert swap.entry_folder({"name": "drill-mcp", "manifest_url": "https://x/y"}) == "drill-mcp"
    assert swap.entry_folder({"name": "drill-mcp", "manifest_url": "./../etc/manifest.json"}) == "drill-mcp"


def test_source_urls():
    assert swap.source_url("npm", "@scope/pkg") == "https://www.npmjs.com/package/@scope/pkg"
    assert swap.source_url("pypi", "drill-mcp") == "https://pypi.org/project/drill-mcp/"
    assert swap.source_url("git", "git+https://host/r#mcp") == "https://host/r#subdirectory=mcp"
    assert swap.source_url("git", "git+ssh://host/r#") == "ssh://host/r"
    assert swap.source_url("image", "ghcr.io/otodock/drill") == "ghcr.io/otodock/drill"
    assert swap.source_url("image", "") == "(built from the folder)"
    assert swap.source_url("remote", "mcp.linear.app") == "mcp.linear.app"


# ── the declaration and the plan ────────────────────────────────────

def test_a_replaces_entry_is_matched_by_identity_with_the_installed_runtime():
    catalog = _manifest(source="npm:drill-next", replaces=[
        {"source": "npm:drill@1.0.0", "credentials": {"A": "B"}},
    ])
    assert swap._declared_by(catalog, "node", ("npm", "drill")) == {
        "source": "npm:drill@1.0.0", "credentials": {"A": "B"},
    }
    assert swap._declared_by(catalog, "node", ("npm", "other")) is None
    # A container entry names the previous image reference; a tag is not identity.
    catalog = _manifest(runtime="docker", source="docker:drill", image="ghcr.io/new/drill:2",
                        replaces=[{"source": "ghcr.io/otodock/drill:1.0.0"}])
    assert swap._declared_by(catalog, "docker", ("image", "ghcr.io/otodock/drill")) is not None
    # A hosted entry names the previous host or URL.
    catalog = _manifest(runtime="remote", source="remote:new.example", url_template="https://new.example/mcp",
                        replaces=[{"source": "mcp.linear.app"}, {"source": "https://old.example/mcp"}])
    assert swap._declared_by(catalog, "remote", ("remote", "mcp.linear.app")) is not None
    assert swap._declared_by(catalog, "remote", ("remote", "old.example")) is not None
    assert swap._declared_by(catalog, "remote", ("remote", "third.example")) is None


def _with_keys(data, *, fields=(), instance_fields=(), config=(), oauth=None):
    if fields:
        data["credentials"] = {"type": "infra", "fields": [{"key": k} for k in fields]}
    if oauth:
        data.setdefault("credentials", {"type": "per_user", "fields": []})
        data["credentials"]["oauth"] = {"provider_id": oauth}
    if instance_fields:
        data["instances"] = {"delivery": "env", "fields": [{"key": k} for k in instance_fields]}
    if config:
        data["config"] = [{"key": k} for k in config]
    return data


def test_credential_plan_per_block():
    installed = _with_keys(_manifest(), fields=("API_KEY", "USER", "OLD_TOKEN"),
                           instance_fields=("URL", "PASS"), config=("MODE", "USER"))
    catalog = _with_keys(_manifest(source="npm:drill-next"),
                         fields=("API_KEY", "USERNAME"), instance_fields=("URL", "PASSWORD"),
                         config=("MODE",))
    stored = {"credentials": {"API_KEY", "USER", "OLD_TOKEN"},
              "instances": {"URL", "PASS"}, "config": {"MODE", "USER"}}
    plan = swap.credential_plan(
        installed, catalog, {"USER": "USERNAME", "PASS": "PASSWORD", "_MODE": "X"}, stored,
    )
    assert plan["credentials"] == {
        "carry": ["API_KEY", "MODE", "URL"],
        "rename": {"USER": "USERNAME", "PASS": "PASSWORD"},
        "reconnect": ["OLD_TOKEN", "USER"],
        "oauth": "none",
    }
    # USER is renamed in the credentials block and reconnects in the config
    # block: a rename applies where both manifests declare it, nowhere else.
    assert plan["blocks"]["credentials"]["rename"] == {"USER": "USERNAME"}
    assert plan["blocks"]["config"]["rename"] == {}
    assert plan["blocks"]["config"]["reconnect"] == ["USER"]
    assert plan["runtime"] == {"from": "node", "to": "node"}


def test_credential_plan_lists_only_stored_keys_for_reconnect():
    installed = _with_keys(_manifest(), fields=("A", "B"))
    catalog = _with_keys(_manifest(source="npm:x"), fields=("C",))
    plan = swap.credential_plan(installed, catalog, {}, {"credentials": {"A"}})
    assert plan["credentials"]["reconnect"] == ["A"]


def test_credential_plan_judges_oauth_by_provider():
    installed = _with_keys(_manifest(), oauth="notion")
    same = _with_keys(_manifest(source="npm:x"), oauth="notion")
    other = _with_keys(_manifest(source="npm:x"), oauth="linear")
    gone = _manifest(source="npm:x")
    stored = {"credentials": {"NOTION_EMAIL", "NOTION_SERVICES"}}
    assert swap.credential_plan(installed, same, {}, stored)["credentials"]["oauth"] == "carry"
    # The account keys an OAuth MCP writes beside its fields carry with it.
    assert swap.credential_plan(installed, same, {}, stored)["credentials"]["reconnect"] == []
    assert swap.credential_plan(installed, other, {}, stored)["credentials"]["oauth"] == "reconnect"
    assert swap.credential_plan(installed, gone, {}, stored)["credentials"]["oauth"] == "reconnect"
    assert swap.credential_plan(gone, same, {}, {})["credentials"]["oauth"] == "none"


def test_credential_plan_reconnects_when_the_issuing_mechanism_changes():
    """The same provider id that starts (or stops) naming the MCP server's
    own authorization server: the old grants came from another issuer."""
    def _with(block: bool):
        oauth = {"provider_id": "notion", "flows": ["authorization_code_pkce"],
                 "bearer_required": True, "proposed_hosts": ["mcp.notion.com"]}
        if block:
            oauth["authorization_server"] = {"registration": "dynamic"}
        return _manifest(credentials={"type": "per_user", "oauth": oauth},
                         runtime="remote", source="remote:mcp.notion.com",
                         url_template="https://mcp.notion.com/mcp")
    stored = {"credentials": set()}
    assert swap.credential_plan(_with(False), _with(True), {}, stored)["credentials"]["oauth"] == "reconnect"
    assert swap.credential_plan(_with(True), _with(False), {}, stored)["credentials"]["oauth"] == "reconnect"
    assert swap.credential_plan(_with(True), _with(True), {}, stored)["credentials"]["oauth"] == "carry"


def test_credential_plan_runtime_change_and_bearer_host():
    installed = _manifest(runtime="remote", source="remote:old.example", url_template="https://old.example/mcp")
    installed["credentials"] = {"type": "per_user", "oauth": {"provider_id": "x", "bearer_required": True}}
    catalog = _manifest(runtime="remote", source="remote:new.example", url_template="https://new.example/mcp")
    plan = swap.credential_plan(installed, catalog, {}, {})
    assert plan["runtime"] == {"from": "remote", "to": "remote"}
    assert plan["bearer_host_change"] is True
    plan = swap.credential_plan(_manifest(), _manifest(runtime="docker", image="ghcr.io/x/y:1"), {}, {})
    assert plan["runtime"] == {"from": "node", "to": "docker"}
    assert plan["bearer_host_change"] is False


# ── the detection pass ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_changed_source_is_reported_and_persisted(temp_db, tmp_path, monkeypatch, fake_catalog):
    installed = _installed(tmp_path, _with_keys(_manifest(), fields=("USER",)))
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"drill-mcp": installed})
    catalog = _with_keys(_manifest(source="npm:drill-next", version="", replaces=[
        {"source": "npm:drill", "credentials": {"USER": "USERNAME"}},
    ]), fields=("USERNAME",))
    fake_catalog["registry"] = [_entry(catalog)]
    fake_catalog["manifests"] = {"drill-mcp": catalog}

    out = await mcp_updater.detect_available_updates()
    info = out["updates"]["drill-mcp"]
    assert info["reason"] == "source"
    assert info["registry"] == "npm"
    change = info["source_change"]
    assert change["status"] == "pending"
    assert change["from"]["url"] == "https://www.npmjs.com/package/drill"
    assert change["to"]["url"] == "https://www.npmjs.com/package/drill-next"
    assert change["declared"] is True
    assert change["plan"]["credentials"]["rename"] == {"USER": "USERNAME"}
    assert out["checked"] == 1
    assert out["checked_at"]

    row = store.get_source_change("drill-mcp")
    assert row["status"] == "pending"
    assert row["to_manifest_hash"] == community_catalog.normalized_manifest_hash(catalog)
    assert row["notified_at"] is None
    # The page reads the same thing back without a new check.
    state = swap.build_update_state()
    assert state["updates"]["drill-mcp"]["reason"] == "source"
    assert state["checked_at"] == out["checked_at"]


@pytest.mark.asyncio
async def test_an_undeclared_change_is_reported_as_such(temp_db, tmp_path, monkeypatch, fake_catalog):
    installed = _installed(tmp_path, _manifest(runtime="docker", source="docker:drill",
                                               image="ghcr.io/otodock/drill:1.0.0"))
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"drill-mcp": installed})
    catalog = _manifest(runtime="docker", source="docker:drill", image="ghcr.io/other/drill:1.0.0")
    fake_catalog["registry"] = [_entry(catalog, image="ghcr.io/other/drill:1.0.0")]
    fake_catalog["manifests"] = {"drill-mcp": catalog}
    out = await mcp_updater.detect_available_updates()
    change = out["updates"]["drill-mcp"]["source_change"]
    assert change["declared"] is False
    assert change["from"]["url"] == "ghcr.io/otodock/drill"
    assert change["to"]["url"] == "ghcr.io/other/drill"
    assert change["from"]["runtime"] == "docker" and change["to"]["runtime"] == "docker"
    # No manifest-axis entry for the same MCP: the source change replaces it.
    assert out["updates"]["drill-mcp"]["reason"] == "source"


@pytest.mark.asyncio
async def test_an_older_registry_is_judged_from_the_catalog_manifest(temp_db, tmp_path, monkeypatch, fake_catalog):
    installed = _installed(tmp_path, _manifest(runtime="remote", source="remote:old.example",
                                               url_template="https://old.example/mcp"))
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"drill-mcp": installed})
    catalog = _manifest(runtime="remote", source="remote:new.example", url_template="https://new.example/mcp")
    entry = _entry(catalog)
    assert "url_host" not in entry
    fake_catalog["registry"] = [entry]
    fake_catalog["manifests"] = {"drill-mcp": catalog}
    out = await mcp_updater.detect_available_updates()
    assert out["updates"]["drill-mcp"]["source_change"]["to"]["url"] == "new.example"
    assert fake_catalog["fetched"] == ["drill-mcp"]


@pytest.mark.asyncio
async def test_an_unreadable_catalog_manifest_is_not_judged(temp_db, tmp_path, monkeypatch, fake_catalog):
    installed = _installed(tmp_path, _manifest())
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"drill-mcp": installed})
    fake_catalog["registry"] = [_entry(_manifest(source="npm:drill-next"))]
    out = await mcp_updater.detect_available_updates()
    assert "drill-mcp" not in out["updates"]
    assert store.get_source_change("drill-mcp") is None


@pytest.mark.asyncio
async def test_a_revert_drops_the_pending_row_and_a_switching_row_stays(temp_db, tmp_path, monkeypatch, fake_catalog):
    installed = _installed(tmp_path, _manifest())
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"drill-mcp": installed})
    moved = _manifest(source="npm:drill-next", version="")
    fake_catalog["registry"] = [_entry(moved)]
    fake_catalog["manifests"] = {"drill-mcp": moved}
    await mcp_updater.detect_available_updates()
    assert store.get_source_change("drill-mcp")["status"] == "pending"

    # The catalog reverts to the installed source.
    fake_catalog["registry"] = [_entry(_manifest(version=""))]
    out = await mcp_updater.detect_available_updates()
    assert "drill-mcp" not in out["updates"]
    assert store.get_source_change("drill-mcp") is None

    # A switch in flight is never touched by a check.
    fake_catalog["registry"] = [_entry(moved)]
    await mcp_updater.detect_available_updates()
    store.set_source_change_status("drill-mcp", store.STATUS_SWITCHING)
    fake_catalog["registry"] = [_entry(_manifest(source="npm:third", version=""))]
    fake_catalog["manifests"] = {"drill-mcp": _manifest(source="npm:third", version="")}
    await mcp_updater.detect_available_updates()
    row = store.get_source_change("drill-mcp")
    assert row["status"] == "switching" and row["to_identity"] == "drill-next"


@pytest.mark.asyncio
async def test_a_failed_registry_fetch_keeps_the_rows(temp_db, tmp_path, monkeypatch, fake_catalog):
    installed = _installed(tmp_path, _manifest())
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"drill-mcp": installed})
    moved = _manifest(source="npm:drill-next", version="")
    fake_catalog["registry"] = [_entry(moved)]
    fake_catalog["manifests"] = {"drill-mcp": moved}
    await mcp_updater.detect_available_updates()

    async def _down():
        raise RuntimeError("github is down")
    monkeypatch.setattr(community_catalog, "fetch_registry", _down)
    persisted_before = store.get_check_results()
    checked_before = store.last_checked_at()
    out = await mcp_updater.detect_available_updates()
    assert out["updates"]["drill-mcp"]["reason"] == "source"
    assert store.get_source_change("drill-mcp")["status"] == "pending"
    # The previous check stands: nothing persisted, its time unchanged.
    assert store.get_check_results() == persisted_before
    assert store.last_checked_at() == checked_before
    assert out["checked_at"] == checked_before


@pytest.mark.asyncio
async def test_a_pending_change_whose_entry_left_the_catalog_is_dropped(temp_db, tmp_path, monkeypatch, fake_catalog):
    """An entry removed from the catalog (moved as a new entry) leaves the
    old install nothing to switch to: its pending row goes at the next check."""
    installed = _installed(tmp_path, _manifest())
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"drill-mcp": installed})
    moved = _manifest(source="npm:drill-next", version="")
    fake_catalog["registry"] = [_entry(moved)]
    fake_catalog["manifests"] = {"drill-mcp": moved}
    await mcp_updater.detect_available_updates()
    assert store.get_source_change("drill-mcp")["status"] == "pending"
    fake_catalog["registry"] = []
    fake_catalog["manifests"] = {}
    out = await mcp_updater.detect_available_updates()
    assert store.get_source_change("drill-mcp") is None
    assert "drill-mcp" not in out["updates"]


def test_the_projection_carries_the_catalog_manifest_hash(temp_db):
    store.upsert_pending_source_change("drill-mcp", {
        "from_kind": "npm", "from_identity": "drill", "from_url": "u", "to_kind": "npm",
        "to_identity": "drill-next", "to_url": "v", "to_manifest_hash": "h-catalog",
    })
    row = store.get_source_change("drill-mcp")
    assert swap._projection(row)["to_manifest_hash"] == "h-catalog"


@pytest.mark.asyncio
async def test_the_ordinary_results_persist_and_a_switched_row_rides_beside_them(temp_db, tmp_path, monkeypatch, fake_catalog):
    installed = _installed(tmp_path, _manifest(runtime="docker", source="docker:drill",
                                               image="ghcr.io/otodock/drill:1.0.0"))
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"drill-mcp": installed})
    newer = _manifest(runtime="docker", source="docker:drill", image="ghcr.io/otodock/drill:1.1.0", version="1.1.0")
    fake_catalog["registry"] = [_entry(newer, image="ghcr.io/otodock/drill:1.1.0")]
    store.upsert_pending_source_change("drill-mcp", {
        "from_kind": "image", "from_identity": "ghcr.io/old/drill", "from_url": "ghcr.io/old/drill",
        "to_kind": "image", "to_identity": "ghcr.io/otodock/drill", "to_url": "ghcr.io/otodock/drill",
    })
    store.set_source_change_status("drill-mcp", store.STATUS_SWITCHED, result={"version": "1.0.0"})

    out = await mcp_updater.detect_available_updates()
    info = out["updates"]["drill-mcp"]
    assert info["reason"] == "package" and info["latest"] == "1.1.0"
    assert info["source_change"]["status"] == "switched"
    assert swap.build_update_state()["updates"]["drill-mcp"]["reason"] == "package"
    # A switched row alone shows as such.
    store.replace_check_results({})
    assert swap.build_update_state()["updates"]["drill-mcp"]["reason"] == "switched"


# ── the store ───────────────────────────────────────────────────────

def _change(**over) -> dict:
    change = {
        "from_kind": "npm", "from_identity": "a", "from_url": "https://www.npmjs.com/package/a",
        "from_runtime": "node", "to_kind": "npm", "to_identity": "b",
        "to_url": "https://www.npmjs.com/package/b", "to_runtime": "node",
        "to_version": "", "to_manifest_hash": "h1", "declared": False, "plan": {"x": 1},
    }
    change.update(over)
    return change


def test_the_same_pair_keeps_its_row_and_a_new_pair_replaces_it(temp_db):
    row = store.upsert_pending_source_change("m", _change())
    assert row["status"] == "pending" and row["plan"] == {"x": 1}
    store.mark_source_change_notified("m")
    store.set_source_change_status("m", store.STATUS_PENDING, result={"error": "boom"})
    row = store.upsert_pending_source_change("m", _change(to_manifest_hash="h2", plan={"x": 2}, declared=True))
    assert row["notified_at"] is not None
    assert row["result"] == {"error": "boom"}
    assert row["to_manifest_hash"] == "h2" and row["plan"] == {"x": 2} and row["declared"] is True
    row = store.upsert_pending_source_change("m", _change(to_identity="c"))
    assert row["notified_at"] is None and row["result"] == {} and row["to_identity"] == "c"


def test_delete_rules(temp_db):
    store.upsert_pending_source_change("m", _change())
    assert store.delete_pending_source_change("m") is True
    store.upsert_pending_source_change("m", _change())
    store.set_source_change_status("m", store.STATUS_SWITCHED)
    assert store.delete_pending_source_change("m") is False
    store.replace_check_results({"m": {"reason": "package"}})
    store.delete_mcp_rows("m")
    assert store.get_source_change("m") is None
    assert store.get_check_results() == {}
    assert store.interrupted_switches() == []
