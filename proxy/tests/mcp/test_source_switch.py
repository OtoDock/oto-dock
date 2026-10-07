"""The admin's switch of a community MCP to its new catalog source
(``mcp_source_swap.switch`` and the routes around it).

The install gate lets exactly the accepted pair through and pins the catalog
manifest the admin saw; a switch drops the runtime dirs and stops a container
the new runtime no longer has, and a failure restores both; the switch keeps
the MCP's rows and applies the declared renames where the new key holds
nothing; a failed, an interrupted and a running switch each answer as the
plan says; the routes take an admin at the keyboard only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import config
from services.community import community_catalog
from services.community import community_installer as ci
from services.community import mcp_source_swap as swap
from services.mcp import mcp_registry
from storage.identity import credential_store
from storage.mcp import mcp_store
from storage.mcp import mcp_update_state_store as store


# ── fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def mcps_dir(temp_db, tmp_path, monkeypatch):
    root = tmp_path / "opt" / "otodock" / "mcps"
    (root / "community").mkdir(parents=True)
    monkeypatch.setattr(config, "MCPS_DIR", root)
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    return root


@pytest.fixture
def fake_package_install(monkeypatch):
    """The package step replaced by a recorder; ``fail`` makes it fail."""
    state = {"calls": [], "fail": False}

    async def _install(mcp_dir, runtime, source, **kw):
        state["calls"].append((runtime, source))
        if state["fail"]:
            return ci.mcp_installer.InstallResult(ok=False, log="npm said no", version_hash="",
                                                  resolved_version="")
        return ci.mcp_installer.InstallResult(
            ok=True, log="", version_hash="h", resolved_version="1.0.0",
        )

    monkeypatch.setattr(ci.mcp_installer, "install_mcp", _install)
    return state


def _packaged(source: str, *, runtime: str = "node", name: str = "pkg-mcp",
              image: str = "", fields: tuple = (), **extra) -> dict:
    server = {"runtime": runtime, "transport": "stdio", "command": "x", "source": source}
    if runtime == "docker":
        server.update({"transport": "http", "port": 8999, "docker_compose": "docker-compose.yml",
                       "url_template": "http://${docker_mcp_host}:${port}"})
        if image:
            server["image"] = image
    data = {"name": name, "label": "Pkg", "description": "d", "version": "",
            "category": "community", "server": server, "skills": []}
    if fields:
        data["credentials"] = {"type": "infra", "fields": [{"key": k} for k in fields]}
    data.update(extra)
    return data


def _folder(tmp_path: Path, manifest: dict, tag: str) -> Path:
    src = tmp_path / "upload" / f"{manifest['name']}-{tag}"
    src.mkdir(parents=True)
    (src / "README.md").write_text(f"Pkg {tag}.\n")
    (src / "manifest.json").write_text(json.dumps(manifest))
    if manifest["server"].get("runtime") == "docker":
        (src / "docker-compose.yml").write_text("services:\n  pkg:\n    image: x\n")
    return src


async def _install(tmp_path, manifest, tag, **kw):
    return await ci.install_from_extracted_folder(_folder(tmp_path, manifest, tag), **kw)


def _target(mcps_dir, name="pkg-mcp") -> Path:
    return mcps_dir / "community" / name


def _plant_runtime_dirs(target: Path) -> None:
    for d in ("node_modules", "venv"):
        (target / d).mkdir(exist_ok=True)
        (target / d / "marker").write_text("x")


# ── the gate ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_accepted_pair_lets_exactly_that_source_through(mcps_dir, tmp_path, fake_package_install):
    await _install(tmp_path, _packaged("npm:pkg"), "a")
    result = await _install(tmp_path, _packaged("npm:other"), "b", accepted_source=("npm", "other"))
    assert result["status"] == "updated"
    assert mcp_registry.get_manifest("pkg-mcp").server.source == "npm:other@1.0.0"
    with pytest.raises(HTTPException) as ei:
        await _install(tmp_path, _packaged("npm:third"), "c", accepted_source=("npm", "other"))
    assert ei.value.status_code == 409 and "changed again" in ei.value.detail
    assert mcp_registry.get_manifest("pkg-mcp").server.source == "npm:other@1.0.0"


@pytest.mark.asyncio
async def test_the_accepted_hash_pins_the_catalog_manifest(mcps_dir, tmp_path, fake_package_install):
    await _install(tmp_path, _packaged("npm:pkg"), "a")
    incoming = _packaged("npm:other")
    good = community_catalog.normalized_manifest_hash(incoming)
    with pytest.raises(HTTPException) as ei:
        await _install(tmp_path, incoming, "b", accepted_source=("npm", "other"),
                       accepted_manifest_hash="0000000000000000")
    assert ei.value.status_code == 409 and "changed since the check" in ei.value.detail
    assert (_target(mcps_dir) / "README.md").read_text() == "Pkg a.\n"
    result = await _install(tmp_path, incoming, "c", accepted_source=("npm", "other"),
                            accepted_manifest_hash=good)
    assert result["status"] == "updated"


@pytest.mark.asyncio
async def test_a_switch_drops_the_runtime_dirs_and_an_update_keeps_them(mcps_dir, tmp_path, fake_package_install):
    await _install(tmp_path, _packaged("npm:pkg"), "a")
    target = _target(mcps_dir)
    _plant_runtime_dirs(target)
    await _install(tmp_path, _packaged("npm:pkg"), "b")
    assert (target / "node_modules" / "marker").is_file() and (target / "venv" / "marker").is_file()
    await _install(tmp_path, _packaged("npm:other"), "c", accepted_source=("npm", "other"))
    assert not (target / "node_modules").exists() and not (target / "venv").exists()
    assert not target.with_suffix(".bak").exists()


@pytest.mark.asyncio
async def test_a_failed_switch_restores_the_folder_and_its_runtime_dirs(mcps_dir, tmp_path, fake_package_install):
    await _install(tmp_path, _packaged("npm:pkg"), "a")
    target = _target(mcps_dir)
    _plant_runtime_dirs(target)
    fake_package_install["fail"] = True
    with pytest.raises(HTTPException) as ei:
        await _install(tmp_path, _packaged("npm:other"), "b", accepted_source=("npm", "other"))
    assert ei.value.status_code == 500
    assert (target / "README.md").read_text() == "Pkg a.\n"
    assert (target / "node_modules" / "marker").is_file() and (target / "venv" / "marker").is_file()
    assert not target.with_suffix(".bak").exists()
    assert mcp_registry.get_manifest("pkg-mcp").server.source == "npm:pkg@1.0.0"


@pytest.mark.asyncio
async def test_a_switch_away_from_a_container_stops_it_and_a_failure_restarts_it(
        mcps_dir, tmp_path, fake_package_install, monkeypatch):
    from services.mcp import compose_rewrite, docker_manager
    calls: list[str] = []
    monkeypatch.setattr(docker_manager, "stop_container", lambda m: calls.append("stop") or True)
    monkeypatch.setattr(docker_manager, "start_container",
                        lambda m, **kw: calls.append("start") or True)
    monkeypatch.setattr(docker_manager, "_inject_mcp_env", lambda m: True)
    monkeypatch.setattr(docker_manager, "get_container_status", lambda m: "running")
    monkeypatch.setattr(compose_rewrite, "ensure_pull_compose", lambda m: None)
    await _install(tmp_path, _packaged("docker:pkg", runtime="docker", image="ghcr.io/o/pkg:1"), "a")
    calls.clear()
    fake_package_install["fail"] = True
    with pytest.raises(HTTPException):
        await _install(tmp_path, _packaged("npm:other"), "b", accepted_source=("npm", "other"))
    assert calls == ["stop", "start"]
    calls.clear()
    fake_package_install["fail"] = False
    await _install(tmp_path, _packaged("npm:other"), "c", accepted_source=("npm", "other"))
    assert calls == ["stop"]
    # A plain update of a container never stops it ahead of the gate.
    calls.clear()
    await _install(tmp_path, _packaged("npm:other"), "d")
    assert calls == []


# ── the switch ─────────────────────────────────────────────────────

@pytest.fixture
def rig(mcps_dir, tmp_path, fake_package_install, monkeypatch):
    """``pkg-mcp`` installed from ``npm:pkg`` with the catalog moved to
    ``npm:other``; ``install_from_catalog`` builds the incoming folder from
    ``rig["catalog"]`` so the real gate and installer run."""
    state = {"catalog": None, "installed_calls": []}

    async def _from_catalog(name, *, progress_cb=None, install_version=None,
                            accepted_source=None, accepted_manifest_hash=""):
        state["installed_calls"].append((name, accepted_source, accepted_manifest_hash))
        folder = _folder(tmp_path, state["catalog"], f"catalog{len(state['installed_calls'])}")
        return await ci.install_from_extracted_folder(
            folder, accepted_source=accepted_source,
            accepted_manifest_hash=accepted_manifest_hash,
        )

    monkeypatch.setattr(ci, "install_from_catalog", _from_catalog)

    async def _run():
        await _install(tmp_path, _packaged("npm:pkg", fields=("USER", "API_KEY")), "a")
        state["catalog"] = _packaged("npm:other", fields=("USERNAME", "API_KEY"), replaces=[
            {"source": "npm:pkg", "credentials": {"USER": "USERNAME"}},
        ])
        plan = swap.credential_plan(
            _packaged("npm:pkg", fields=("USER", "API_KEY")), state["catalog"],
            {"USER": "USERNAME"}, {"credentials": {"USER", "API_KEY"}},
        )
        state["row"] = store.upsert_pending_source_change("pkg-mcp", {
            "from_kind": "npm", "from_identity": "pkg", "from_url": "https://www.npmjs.com/package/pkg",
            "from_runtime": "node", "to_kind": "npm", "to_identity": "other",
            "to_url": "https://www.npmjs.com/package/other", "to_runtime": "node",
            "to_version": "", "declared": True, "plan": plan,
            "to_manifest_hash": community_catalog.normalized_manifest_hash(state["catalog"]),
        })
        return state
    return _run


def _switch_args(state):
    return {"from_url": state["row"]["from_url"], "to_url": state["row"]["to_url"],
            "manifest_hash": state["row"]["to_manifest_hash"], "admin_sub": "user-admin"}


@pytest.mark.asyncio
async def test_a_switch_needs_a_pending_row_and_the_exact_pair(rig):
    with pytest.raises(HTTPException) as ei:
        await swap.switch("pkg-mcp", from_url="a", to_url="b", manifest_hash="h", admin_sub="user-admin")
    assert ei.value.status_code == 409 and "No source change is pending" in ei.value.detail
    state = await rig()
    with pytest.raises(HTTPException) as ei:
        await swap.switch("pkg-mcp", from_url=state["row"]["from_url"], to_url="https://elsewhere",
                          manifest_hash=state["row"]["to_manifest_hash"], admin_sub="user-admin")
    assert ei.value.status_code == 409 and "does not match" in ei.value.detail
    assert state["installed_calls"] == []


@pytest.mark.asyncio
async def test_the_body_hash_must_be_the_catalog_manifest_the_card_showed(rig):
    """The row's catalog side is refreshed by every check: the Switch sends
    the hash the card described, and a refreshed row refuses it."""
    state = await rig()
    with pytest.raises(HTTPException) as ei:
        await swap.switch("pkg-mcp", **{**_switch_args(state), "manifest_hash": "stale"})
    assert ei.value.status_code == 409 and "changed since the page" in ei.value.detail
    assert state["installed_calls"] == []
    state["row"] = store.upsert_pending_source_change("pkg-mcp", {
        **{k: state["row"][k] for k in ("from_kind", "from_identity", "from_url", "from_runtime",
                                        "to_kind", "to_identity", "to_url", "to_runtime", "to_version")},
        "declared": True, "plan": state["row"]["plan"], "to_manifest_hash": "",
    })
    with pytest.raises(HTTPException) as ei:
        await swap.switch("pkg-mcp", **{**_switch_args(state), "manifest_hash": ""})
    assert ei.value.status_code == 409 and "carries no catalog manifest" in ei.value.detail


@pytest.mark.asyncio
async def test_a_switch_keeps_the_rows_and_applies_the_declared_renames(rig, mcps_dir):
    state = await rig()
    mcp_store.set_mcp_enabled("pkg-mcp", True)
    mcp_store.add_agent_mcp("alice-agent", "pkg-mcp")
    mcp_store.set_mcp_config_values("pkg-mcp", {"MODE": "fast", "USER": "cfg"})
    credential_store.set_infra_credentials("pkg-mcp", {"USER": "infra-u", "API_KEY": "k"})
    credential_store.set_user_credentials("user-admin", "pkg-mcp", {"USER": "admin-u"}, "default")

    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert out["status"] == "switched"
    assert state["installed_calls"] == [("pkg-mcp", ("npm", "other"), state["row"]["to_manifest_hash"])]
    assert out["result"]["renamed"] == {"USER": "USERNAME"}
    assert out["result"]["kept"] == []
    assert out["result"]["reconnect_needed"] == []
    assert credential_store.get_infra_credentials("pkg-mcp") == {"USERNAME": "infra-u", "API_KEY": "k"}
    assert credential_store.get_user_credentials("user-admin", "pkg-mcp", "default") == {"USERNAME": "admin-u"}
    # The config block declared no rename: its USER value stays as it was.
    assert mcp_store.get_mcp_config_values("pkg-mcp") == {"MODE": "fast", "USER": "cfg"}
    assert mcp_store.get_all_mcp_states()["pkg-mcp"] is True
    assert "pkg-mcp" in mcp_store.get_manager_enabled_mcps("alice-agent")
    assert mcp_registry.get_manifest("pkg-mcp").server.source == "npm:other@1.0.0"
    row = store.get_source_change("pkg-mcp")
    assert row["status"] == "switched" and row["accepted_by"] == "user-admin"
    assert row["result"]["renamed"] == {"USER": "USERNAME"}
    assert not _target(mcps_dir).with_suffix(".bak").exists()


@pytest.mark.asyncio
async def test_a_rename_that_collides_keeps_the_old_key_and_lists_it(rig):
    state = await rig()
    credential_store.set_infra_credentials("pkg-mcp", {"USER": "old", "USERNAME": "already"})
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert credential_store.get_infra_credentials("pkg-mcp") == {"USER": "old", "USERNAME": "already"}
    assert out["result"]["renamed"] == {}
    assert out["result"]["kept"] == ["USER"]
    assert out["result"]["reconnect_needed"] == ["USER"]


@pytest.mark.asyncio
async def test_instance_values_are_renamed_row_by_row(rig, tmp_path):
    state = await rig()
    state["catalog"]["instances"] = {"delivery": "env", "fields": [{"key": "PASSWORD"}]}
    state["row"] = store.upsert_pending_source_change("pkg-mcp", {
        **{k: state["row"][k] for k in ("from_kind", "from_identity", "from_url", "from_runtime",
                                        "to_kind", "to_identity", "to_url", "to_runtime", "to_version")},
        "declared": True,
        "to_manifest_hash": community_catalog.normalized_manifest_hash(state["catalog"]),
        "plan": {"blocks": {"instances": {"rename": {"PASS": "PASSWORD"}}}},
    })
    a = mcp_store.upsert_mcp_instance("pkg-mcp", {"instance_name": "a", "field_values": {"PASS": "p1"},
                                                  "agents": [], "assigned_to_all": False})
    b = mcp_store.upsert_mcp_instance("pkg-mcp", {"instance_name": "b",
                                                  "field_values": {"PASS": "p2", "PASSWORD": "keep"},
                                                  "agents": [], "assigned_to_all": True})
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    rows = {i["id"]: i for i in mcp_store.get_mcp_instances("pkg-mcp")}
    assert rows[a]["field_values"] == {"PASSWORD": "p1"}
    assert rows[b]["field_values"] == {"PASS": "p2", "PASSWORD": "keep"}
    assert rows[b]["assigned_to_all"] is True
    assert out["result"]["renamed"] == {"PASS": "PASSWORD"} and out["result"]["kept"] == ["PASS"]


@pytest.mark.asyncio
async def test_a_failed_switch_leaves_the_row_pending_with_the_error_and_retries(rig, fake_package_install):
    state = await rig()
    fake_package_install["fail"] = True
    with pytest.raises(HTTPException) as ei:
        await swap.switch("pkg-mcp", **_switch_args(state))
    assert ei.value.status_code == 500
    row = store.get_source_change("pkg-mcp")
    assert row["status"] == "pending" and "npm said no" in row["result"]["error"]
    assert mcp_registry.get_manifest("pkg-mcp").server.source == "npm:pkg@1.0.0"
    fake_package_install["fail"] = False
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert out["status"] == "switched"


@pytest.mark.asyncio
async def test_a_failure_after_the_install_ends_switched_with_the_error(rig, monkeypatch):
    """Past the install the switch is irreversible: an error in the renames
    is recorded on the switched row, never left as a stuck switching one."""
    state = await rig()
    credential_store.set_infra_credentials("pkg-mcp", {"USER": "infra-u"})

    def _boom(name, blocks):
        raise RuntimeError("db gone")
    monkeypatch.setattr(swap, "apply_renames", _boom)
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert out["status"] == "switched"
    row = store.get_source_change("pkg-mcp")
    assert row["status"] == "switched" and "db gone" in row["result"]["error"]
    assert row["result"]["renamed"] == {}
    assert mcp_registry.get_manifest("pkg-mcp").server.source == "npm:other@1.0.0"
    assert credential_store.get_infra_credentials("pkg-mcp") == {"USER": "infra-u"}
    with pytest.raises(HTTPException) as ei:
        await swap.switch("pkg-mcp", **_switch_args(state))
    assert ei.value.status_code == 409 and "not pending" in ei.value.detail


@pytest.mark.asyncio
async def test_an_installer_error_past_the_point_of_no_return_ends_switched(rig, monkeypatch):
    """An installer error after the folder is replaced and its backup gone
    (a T2 compose refusal, a failed container start): the MCP runs from the
    new source, so the switch finishes on it with the error noted."""
    state = await rig()
    credential_store.set_infra_credentials("pkg-mcp", {"USER": "infra-u"})
    real = ci.install_from_catalog

    async def _after_the_files(name, **kw):
        await real(name, **kw)
        raise HTTPException(400, "the container could not be started")
    monkeypatch.setattr(ci, "install_from_catalog", _after_the_files)
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert out["status"] == "switched"
    row = store.get_source_change("pkg-mcp")
    assert row["status"] == "switched"
    assert "could not be started" in row["result"]["error"]
    assert row["result"]["renamed"] == {"USER": "USERNAME"}
    assert credential_store.get_infra_credentials("pkg-mcp") == {"USERNAME": "infra-u"}


@pytest.mark.asyncio
async def test_a_folder_already_on_the_new_source_is_finished_at_accept(rig, mcps_dir):
    """A crash after the install left the folder on the new source and the
    row pending: the accept finishes the switch instead of installing again."""
    state = await rig()
    credential_store.set_infra_credentials("pkg-mcp", {"USER": "infra-u"})
    (_target(mcps_dir) / "manifest.json").write_text(json.dumps(state["catalog"]))
    mcp_registry.scan_manifests()
    store.set_source_change_status("pkg-mcp", store.STATUS_PENDING, result=swap._interrupted())
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert out["status"] == "switched" and out["recovered"] is True
    assert state["installed_calls"] == []
    row = store.get_source_change("pkg-mcp")
    assert row["status"] == "switched" and row["result"]["renamed"] == {"USER": "USERNAME"}
    assert row["result"]["error"] == swap.INTERRUPTED
    assert credential_store.get_infra_credentials("pkg-mcp") == {"USERNAME": "infra-u"}


@pytest.mark.asyncio
async def test_a_catalog_that_moved_again_is_refused_at_the_gate(rig):
    state = await rig()
    state["catalog"] = _packaged("npm:third")
    with pytest.raises(HTTPException) as ei:
        await swap.switch("pkg-mcp", **_switch_args(state))
    assert ei.value.status_code == 409 and "changed again" in ei.value.detail
    assert store.get_source_change("pkg-mcp")["status"] == "pending"


@pytest.mark.asyncio
async def test_an_interrupted_switch_is_taken_up_and_a_running_one_is_refused(rig):
    from core.credentials import catalog_install_registry
    state = await rig()
    store.set_source_change_status("pkg-mcp", store.STATUS_SWITCHING)
    lock = catalog_install_registry.lock_for("pkg-mcp")
    async with lock:
        with pytest.raises(HTTPException) as ei:
            await swap.switch("pkg-mcp", **_switch_args(state))
        assert ei.value.status_code == 409 and "already running" in ei.value.detail
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert out["status"] == "switched"


@pytest.mark.asyncio
async def test_the_installed_source_must_still_be_the_row_s(rig, mcps_dir):
    state = await rig()
    path = _target(mcps_dir) / "manifest.json"
    data = json.loads(path.read_text())
    data["server"]["source"] = "npm:moved"
    path.write_text(json.dumps(data))
    with pytest.raises(HTTPException) as ei:
        await swap.switch("pkg-mcp", **_switch_args(state))
    assert ei.value.status_code == 409 and "installed source moved" in ei.value.detail


@pytest.mark.asyncio
async def test_the_old_image_goes_only_after_a_confirmed_start(rig, monkeypatch, tmp_path):
    from services.mcp import docker_manager
    removed: list[str] = []
    started = {"ok": True}
    monkeypatch.setattr(docker_manager, "remove_image", lambda img: removed.append(img) or True)
    monkeypatch.setattr(docker_manager, "start_container", lambda m, **kw: started["ok"])
    monkeypatch.setattr(docker_manager, "_inject_mcp_env", lambda m: True)
    monkeypatch.setattr(docker_manager, "stop_container", lambda m: True)
    from services.mcp import compose_rewrite
    monkeypatch.setattr(compose_rewrite, "ensure_pull_compose", lambda m: None)
    # Reinstall the rig's MCP as a container and move the catalog to another image.
    state = await rig()
    await _install(tmp_path, _packaged("docker:pkg", runtime="docker", image="ghcr.io/old/pkg:1"), "d",
                   accepted_source=("image", "ghcr.io/old/pkg"))
    state["catalog"] = _packaged("docker:pkg", runtime="docker", image="ghcr.io/new/pkg:1")
    state["row"] = store.upsert_pending_source_change("pkg-mcp", {
        "from_kind": "image", "from_identity": "ghcr.io/old/pkg", "from_url": "ghcr.io/old/pkg",
        "from_runtime": "docker", "to_kind": "image", "to_identity": "ghcr.io/new/pkg",
        "to_url": "ghcr.io/new/pkg", "to_runtime": "docker", "to_version": "",
        "to_manifest_hash": community_catalog.normalized_manifest_hash(state["catalog"]),
        "declared": False, "plan": {},
    })
    started["ok"] = False
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert out["result"]["container_started"] is False
    assert removed == []
    # Back to pending for a second try with a start that succeeds.
    state["row"] = store.upsert_pending_source_change("pkg-mcp", {
        "from_kind": "image", "from_identity": "ghcr.io/new/pkg", "from_url": "ghcr.io/new/pkg",
        "from_runtime": "docker", "to_kind": "image", "to_identity": "ghcr.io/newer/pkg",
        "to_url": "ghcr.io/newer/pkg", "to_runtime": "docker", "to_version": "",
        "to_manifest_hash": community_catalog.normalized_manifest_hash(
            _packaged("docker:pkg", runtime="docker", image="ghcr.io/newer/pkg:1")),
        "declared": False, "plan": {},
    })
    state["catalog"] = _packaged("docker:pkg", runtime="docker", image="ghcr.io/newer/pkg:1")
    started["ok"] = True
    out = await swap.switch("pkg-mcp", **_switch_args(state))
    assert out["result"]["container_started"] is True
    assert removed == ["ghcr.io/new/pkg:1"]


@pytest.mark.asyncio
async def test_dismiss_rules(rig):
    state = await rig()
    with pytest.raises(HTTPException) as ei:
        await swap.dismiss("pkg-mcp")
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        await swap.dismiss("nothing-here")
    assert ei.value.status_code == 404
    await swap.switch("pkg-mcp", **_switch_args(state))
    await swap.dismiss("pkg-mcp")
    assert store.get_source_change("pkg-mcp") is None


@pytest.mark.asyncio
async def test_reconcile_restores_the_backup_of_an_interrupted_switch(rig, mcps_dir):
    """A restart during the install: the backup is the old source, runtime
    dirs included, and it comes back; the row goes back to pending."""
    state = await rig()
    target = _target(mcps_dir)
    _plant_runtime_dirs(target)
    bak = target.with_suffix(".bak")
    import shutil
    shutil.move(str(target), str(bak))
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(state["catalog"]))
    store.set_source_change_status("pkg-mcp", store.STATUS_SWITCHING)
    moved = await swap.reconcile_interrupted()
    assert moved == ["pkg-mcp"]
    row = store.get_source_change("pkg-mcp")
    assert row["status"] == "pending" and row["result"]["error"] == swap.INTERRUPTED
    assert not bak.exists()
    assert (target / "README.md").read_text() == "Pkg a.\n"
    assert (target / "node_modules" / "marker").is_file()
    assert mcp_registry.get_manifest("pkg-mcp").server.source == "npm:pkg@1.0.0"
    assert state["installed_calls"] == []


@pytest.mark.asyncio
async def test_reconcile_finishes_an_interrupted_switch_past_its_install(rig, mcps_dir):
    """A restart after the install (no backup left, the folder on the new
    source): the switch is finished on it, renames applied, error noted."""
    state = await rig()
    credential_store.set_infra_credentials("pkg-mcp", {"USER": "infra-u"})
    (_target(mcps_dir) / "manifest.json").write_text(json.dumps(state["catalog"]))
    mcp_registry.scan_manifests()
    store.set_source_change_status("pkg-mcp", store.STATUS_SWITCHING)
    moved = await swap.reconcile_interrupted()
    assert moved == []
    row = store.get_source_change("pkg-mcp")
    assert row["status"] == "switched" and row["result"]["error"] == swap.INTERRUPTED
    assert row["result"]["renamed"] == {"USER": "USERNAME"}
    assert credential_store.get_infra_credentials("pkg-mcp") == {"USERNAME": "infra-u"}


@pytest.mark.asyncio
async def test_reconcile_moves_an_interrupted_switch_back_and_removes_a_stray_backup(rig, mcps_dir):
    """A restart before any file moved: the row goes back to pending; and a
    backup a crashed ordinary update left beside a complete folder is swept."""
    state = await rig()
    store.set_source_change_status("pkg-mcp", store.STATUS_SWITCHING)
    moved = await swap.reconcile_interrupted()
    assert moved == ["pkg-mcp"]
    assert store.get_source_change("pkg-mcp")["status"] == "pending"
    bak = _target(mcps_dir).with_suffix(".bak")
    bak.mkdir()
    (bak / "manifest.json").write_text("{}")
    assert await swap.reconcile_interrupted() == []
    assert not bak.exists()
    assert mcp_registry.get_manifest("pkg-mcp").server.source == "npm:pkg@1.0.0"
    assert state["installed_calls"] == []


@pytest.mark.asyncio
async def test_an_unexpected_error_during_the_install_rolls_back_and_restarts(
        mcps_dir, tmp_path, fake_package_install, monkeypatch):
    """What the explicit rollback paths do not catch (a copy error) still
    restores the folder and the container the switch stopped."""
    from services.mcp import compose_rewrite, docker_manager
    calls: list[str] = []
    monkeypatch.setattr(docker_manager, "stop_container", lambda m: calls.append("stop") or True)
    monkeypatch.setattr(docker_manager, "start_container",
                        lambda m, **kw: calls.append("start") or True)
    monkeypatch.setattr(docker_manager, "_inject_mcp_env", lambda m: True)
    monkeypatch.setattr(docker_manager, "get_container_status", lambda m: "running")
    monkeypatch.setattr(compose_rewrite, "ensure_pull_compose", lambda m: None)
    await _install(tmp_path, _packaged("docker:pkg", runtime="docker", image="ghcr.io/o/pkg:1"), "a")
    calls.clear()
    real_apply = ci._apply_extracted_files

    def _half_copy(src, target, is_update, backup, **kw):
        real_apply(src, target, is_update, backup, **kw)
        raise OSError("disk full")
    monkeypatch.setattr(ci, "_apply_extracted_files", _half_copy)
    with pytest.raises(OSError):
        await _install(tmp_path, _packaged("npm:other"), "b", accepted_source=("npm", "other"))
    assert calls == ["stop", "start"]
    target = _target(mcps_dir)
    assert (target / "README.md").read_text() == "Pkg a.\n"
    assert not target.with_suffix(".bak").exists()


@pytest.mark.asyncio
async def test_an_applied_update_retires_the_persisted_offer(rig, monkeypatch):
    from services.mcp import mcp_updater
    await rig()
    before = store.replace_check_results({"pkg-mcp": {"current": "1.0.0", "latest": "1.1.0",
                                                      "registry": "npm", "package": "pkg",
                                                      "reason": "package"}})

    async def _updated(name, manifest):
        return {"status": "updated"}
    monkeypatch.setattr(mcp_updater, "_update_node_python_mcp", _updated)
    await mcp_updater.update_one("pkg-mcp")
    assert store.get_check_results() == {}
    assert store.last_checked_at() == before


@pytest.mark.asyncio
async def test_browse_flags_a_moved_source_instead_of_offering_it(mcps_dir, tmp_path, fake_package_install):
    await _install(tmp_path, _packaged("npm:pkg"), "a")
    entry = {"name": "pkg-mcp", "version": "", "runtime": "node", "source": "npm:other",
             "manifest_hash": "catalog-hash", "manifest_url": "./pkg-mcp/manifest.json"}
    out = community_catalog.augment_entry(
        entry, {"pkg-mcp": "1.0.0"}, {}, installed_manifest_hashes={"pkg-mcp": "installed-hash"},
    )
    assert out["update_available"] is False and out["source_changed"] is True
    same = community_catalog.augment_entry(
        {**entry, "source": "npm:pkg"}, {"pkg-mcp": "1.0.0"}, {},
        installed_manifest_hashes={"pkg-mcp": "installed-hash"},
    )
    assert same["update_available"] is True and same["source_changed"] is False


# ── the routes ─────────────────────────────────────────────────────

def _client(principal):
    from api.mcp import mcps as mcps_api
    from auth.providers import get_current_user

    async def _stub():
        return principal

    app = FastAPI()
    app.include_router(mcps_api.router)
    app.dependency_overrides[get_current_user] = _stub
    return TestClient(app)


def _principal(role="admin", *, is_api_key=False):
    from auth.providers import UserContext
    return UserContext(sub="user-admin", email="admin@test.com", name="A", role=role,
                       agents=[], agent_roles={}, is_api_key=is_api_key)


@pytest.mark.parametrize("principal, status", [
    (_principal("admin"), 200),
    (_principal("admin", is_api_key=True), 403),   # a session token of an admin-owned session
    (_principal("member"), 403),
])
def test_accept_source_takes_an_admin_at_the_keyboard_only(temp_db, monkeypatch, principal, status):
    seen = []

    async def _switch(name, *, from_url, to_url, manifest_hash, admin_sub):
        seen.append((name, from_url, to_url, manifest_hash, admin_sub))
        return {"status": "switched"}

    monkeypatch.setattr(swap, "switch", _switch)
    r = _client(principal).post("/v1/admin/mcps/pkg-mcp/accept-source",
                                json={"from": "https://a", "to": "https://b", "manifest_hash": "h"})
    assert r.status_code == status, r.text
    assert seen == ([("pkg-mcp", "https://a", "https://b", "h", "user-admin")] if status == 200 else [])
    if status == 200:
        r = _client(principal).post("/v1/admin/mcps/pkg-mcp/accept-source",
                                    json={"from": "https://a", "to": "https://b"})
        assert r.status_code == 422


@pytest.mark.parametrize("principal, status", [
    (_principal("admin"), 200),
    (_principal("admin", is_api_key=True), 403),
])
def test_dismiss_takes_an_admin_at_the_keyboard_only(temp_db, monkeypatch, principal, status):
    async def _dismiss(name):
        return None
    monkeypatch.setattr(swap, "dismiss", _dismiss)
    r = _client(principal).delete("/v1/admin/mcps/pkg-mcp/source-change")
    assert r.status_code == status, r.text


def test_update_check_and_delete_refuse_a_session_token(temp_db, monkeypatch):
    client = _client(_principal("admin", is_api_key=True))
    assert client.post("/v1/admin/mcps/pkg-mcp/update").status_code == 403
    assert client.get("/v1/admin/mcps/check-updates").status_code == 403
    assert client.delete("/v1/admin/mcps/pkg-mcp").status_code == 403


def test_delete_takes_an_interrupted_switch_s_row_with_the_install(mcps_dir, tmp_path, monkeypatch):
    """Delete holds the install lock, so a running switch finishes first; an
    interrupted one (its row still switching) goes with the rest."""
    folder = mcps_dir / "community" / "pkg-mcp"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps(_packaged("npm:pkg")))
    mcp_registry.scan_manifests()
    store.upsert_pending_source_change("pkg-mcp", {
        "from_kind": "npm", "from_identity": "pkg", "from_url": "u", "to_kind": "npm",
        "to_identity": "other", "to_url": "v",
    })
    store.set_source_change_status("pkg-mcp", store.STATUS_SWITCHING)
    r = _client(_principal("admin")).delete("/v1/admin/mcps/pkg-mcp")
    assert r.status_code == 200, r.text
    assert store.get_source_change("pkg-mcp") is None
    assert not folder.exists()


def test_update_state_reads_the_persisted_check(temp_db, monkeypatch):
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    r = _client(_principal("admin")).get("/v1/admin/mcps/update-state")
    assert r.status_code == 200
    assert r.json() == {"updates": {}, "checked": 0, "checked_at": ""}
    store.replace_check_results({"x-mcp": {"current": "1", "latest": "2", "registry": "npm",
                                           "package": "x", "reason": "package"}})
    body = _client(_principal("admin")).get("/v1/admin/mcps/update-state").json()
    assert body["updates"]["x-mcp"]["reason"] == "package" and body["checked"] == 1
    assert body["checked_at"]
