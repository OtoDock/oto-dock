"""An MCP's update as a job (services/mcp/mcp_updater.py, the routes in
api/mcp/mcps.py): the button's request answers at once, the MCP Servers page
reads the phase and the end from update-state, a second update and a
container control wait while one runs, and a Docker update starts its
container once and keeps the offer when that start failed."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.mcp import compose_progress, mcp_updater as mu


@pytest.fixture(autouse=True)
def _no_records():
    compose_progress._records.clear()
    yield
    compose_progress._records.clear()


def _manifest(name="camoufox", *, runtime="docker", image="ghcr.io/otodock/camoufox:0.0.76",
              category="community"):
    return SimpleNamespace(name=name, category=category, version="0.0.76",
                           server=SimpleNamespace(runtime=runtime, image=image, source="docker:x"),
                           mcp_dir="/nonexistent")


def _known(monkeypatch, *manifests):
    by_name = {m.name: m for m in manifests}
    monkeypatch.setattr(mu.mcp_registry, "get_manifest", by_name.get)
    return by_name


class _Gate:
    """An update_one stand-in that holds until released."""

    def __init__(self, result=None, exc=None):
        self.release = asyncio.Event()
        self.calls: list[tuple[str, dict]] = []
        self.result = result or {"status": "updated", "version": "1.1.0", "runtime": "node",
                                 "install_log": "npm ok"}
        self.exc = exc

    async def __call__(self, name, **kw):
        self.calls.append((name, kw))
        mu._set_phase(name, mu.PHASE_PREPARING, "Installing the new version.",
                      status=mu.JOB_RUNNING)
        await self.release.wait()
        if self.exc:
            raise self.exc
        return self.result


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ── the job ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_answers_at_once_and_the_job_runs_to_its_log(monkeypatch):
    _known(monkeypatch, _manifest("pkg", runtime="node"))
    gate = _Gate()
    monkeypatch.setattr(mu, "update_one", gate)

    assert mu.start_update("pkg") == {"status": "started", "name": "pkg"}
    state = mu.job_state()
    assert state["running"]["pkg"]["phase"] == "queued"
    await _settle()
    assert mu.job_state()["running"]["pkg"]["detail"] == "Installing the new version."
    gate.release.set()
    await _settle()
    state = mu.job_state()
    assert state["running"] == {}
    assert state["finished"]["pkg"]["status"] == "done"
    assert state["finished"]["pkg"]["log"] == "npm ok"
    assert gate.calls == [("pkg", {"settled_only": False})]


@pytest.mark.asyncio
async def test_a_second_update_while_one_runs_is_refused(monkeypatch):
    _known(monkeypatch, _manifest("pkg", runtime="node"))
    gate = _Gate()
    monkeypatch.setattr(mu, "update_one", gate)
    mu.start_update("pkg")
    with pytest.raises(mu.UpdateAlreadyRunning) as refused:
        mu.start_update("pkg")
    assert refused.value.status_code == 409
    assert refused.value.detail == "An update of this MCP is already running."
    with pytest.raises(mu.UpdateAlreadyRunning):
        await mu.run_update("pkg", settled_only=True)
    gate.release.set()
    await _settle()
    # Once it ended, the next one starts.
    gate.release = asyncio.Event()
    assert mu.start_update("pkg")["status"] == "started"
    gate.release.set()
    await _settle()
    assert len(gate.calls) == 2


def test_an_unknown_mcp_is_404_and_registers_nothing(monkeypatch):
    _known(monkeypatch)
    with pytest.raises(HTTPException) as nf:
        mu.start_update("ghost")
    assert nf.value.status_code == 404
    assert mu._jobs == {}


@pytest.mark.asyncio
async def test_a_failed_update_keeps_its_sentence(monkeypatch):
    _known(monkeypatch, _manifest("pkg", runtime="node"))
    gate = _Gate(exc=HTTPException(502, "The registry did not answer."))
    monkeypatch.setattr(mu, "update_one", gate)
    mu.start_update("pkg")
    gate.release.set()
    await _settle()
    assert mu.job_state()["finished"]["pkg"] == {
        "status": "failed", "log": "The registry did not answer.",
        "ended_at": mu._jobs["pkg"].ended_at}


@pytest.mark.asyncio
async def test_run_update_raises_what_the_update_raised_and_records_it(monkeypatch):
    _known(monkeypatch, _manifest("pkg", runtime="node"))
    gate = _Gate(exc=HTTPException(409, "builds from a git source"))
    gate.release.set()
    monkeypatch.setattr(mu, "update_one", gate)
    with pytest.raises(HTTPException) as exc:
        await mu.run_update("pkg", settled_only=True)
    # update_one's own 409 is a failure, never the job's "already running".
    assert not isinstance(exc.value, mu.UpdateAlreadyRunning)
    assert mu._jobs["pkg"].status == "failed"


@pytest.mark.asyncio
async def test_a_running_docker_job_shows_its_compose_progress(monkeypatch):
    _known(monkeypatch, _manifest())
    gate = _Gate()
    monkeypatch.setattr(mu, "update_one", gate)
    mu.start_update("camoufox")
    tracker = compose_progress.Tracker("camoufox")
    tracker.feed("aaaaaaaaaaaa Downloading 1.3GB")
    # Queued: the record of another run is not this job's.
    assert mu.job_state()["running"]["camoufox"]["phase"] == "queued"
    await _settle()
    running = mu.job_state()["running"]["camoufox"]
    assert running["phase"] == "downloading"
    assert running["detail"] == "Downloading the new image, 1.3 GB so far."
    tracker.close()
    assert mu.job_state()["running"]["camoufox"]["phase"] == "preparing"
    gate.release.set()
    await _settle()


@pytest.mark.asyncio
async def test_the_real_update_turns_a_package_job_running_under_the_lock(monkeypatch):
    """update_one itself (no stand-in) moves the job from queued to running
    once it holds the lock; a job left queued would never show its compose
    progress."""
    _known(monkeypatch, _manifest("pkg-real", runtime="node"))
    seen: dict = {}
    release = asyncio.Event()

    async def _converge(name, manifest, *, settled_only=False):
        seen.update(mu.job_state()["running"][name])
        await release.wait()
        return {"status": "updated", "version": "1.1.0", "runtime": "node", "install_log": "npm ok"}

    monkeypatch.setattr(mu, "_update_node_python_mcp", _converge)
    forgot: list[str] = []
    monkeypatch.setattr(mu, "_forget_check_result", forgot.append)
    mu.start_update("pkg-real")
    await _settle()
    assert seen["phase"] == "preparing" and seen["detail"] == "Installing the new version."
    assert mu._jobs["pkg-real"].status == mu.JOB_RUNNING
    release.set()
    await _settle()
    assert mu.job_state()["finished"]["pkg-real"]["log"] == "npm ok"
    assert forgot == ["pkg-real"]


@pytest.mark.asyncio
async def test_a_job_cancelled_with_the_server_ends_failed(monkeypatch):
    from core import background

    _known(monkeypatch, _manifest("pkg-stop", runtime="node"))
    monkeypatch.setattr(mu, "update_one", _Gate())
    mu.start_update("pkg-stop")
    await _settle()
    [task] = background.running("mcp-updater")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finished = mu.job_state()["finished"]["pkg-stop"]
    assert finished["status"] == "failed"
    assert finished["log"] == "The update stopped with the server."


def test_a_finished_job_is_read_for_thirty_seconds_and_kept_at_most_ten_minutes(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(mu, "_clock", lambda: clock["t"])
    for name in ("a", "b"):
        job = mu._register(name)
        mu._finish(job, mu.JOB_DONE, "ok")
    clock["t"] += 10
    assert set(mu.job_state()["finished"]) == {"a", "b"}   # the first read
    clock["t"] += 29
    assert set(mu.job_state()["finished"]) == {"a", "b"}   # a second tab
    clock["t"] += 2
    assert mu.job_state()["finished"] == {}
    late = mu._register("c")
    mu._finish(late, mu.JOB_DONE, "ok")
    clock["t"] += 601
    assert mu.job_state()["finished"] == {}
    assert mu._jobs == {}


# ── the Docker update ──────────────────────────────────────────────

def _docker_rig(monkeypatch, *, started, new_image="ghcr.io/otodock/camoufox:0.0.77"):
    from core.config import deployment
    from services.community import community_installer
    from services.mcp import docker_manager

    current = {"m": _manifest()}
    monkeypatch.setattr(mu.mcp_registry, "get_manifest", lambda n: current["m"])
    calls = {"events": [], "removed": [], "starts": 0}

    async def _from_catalog(name, *, progress_cb=None, **kw):
        for phase in ("prepare", "image", "start", "finalize"):
            progress_cb({"phase": phase, "pct": 1, "message": "x"})
            calls["events"].append(mu._jobs["camoufox"].detail if "camoufox" in mu._jobs else "")
        current["m"] = _manifest(image=new_image)
        return {"status": "updated", "name": name, "version": "0.0.77", "old_version": "0.0.76",
                "runtime": "docker", "install_log": "Docker MCP — use Start button to build and run.",
                "container_started": started}

    monkeypatch.setattr(community_installer, "install_from_catalog", _from_catalog)
    monkeypatch.setattr(docker_manager, "remove_image", calls["removed"].append)

    def _start(*a, **k):
        calls["starts"] += 1
        return True
    monkeypatch.setattr(docker_manager, "start_container", _start)
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_LOCAL)
    forgot: list[str] = []
    monkeypatch.setattr(mu, "_forget_check_result", forgot.append)
    return calls, forgot


@pytest.mark.asyncio
async def test_a_docker_update_starts_its_container_once_on_t1(monkeypatch, temp_db):
    calls, forgot = _docker_rig(monkeypatch, started=True)
    result = await mu.run_update("camoufox")
    assert result["version"] == "0.0.77"
    # The install's start is the one start: the updater starts nothing itself.
    assert calls["starts"] == 0
    assert calls["removed"] == ["ghcr.io/otodock/camoufox:0.0.76"]
    assert forgot == ["camoufox"]
    assert calls["events"] == ["Getting the new version ready.", "Getting the new image.",
                               "Finishing the update.", "Finishing the update."]
    finished = mu.job_state()["finished"]["camoufox"]
    assert finished["status"] == "done"
    assert finished["log"] == "Updated to v0.0.77. The container runs the new version."


@pytest.mark.asyncio
async def test_a_failed_start_keeps_the_old_image_and_the_offer(monkeypatch, temp_db):
    calls, forgot = _docker_rig(monkeypatch, started=False)
    with pytest.raises(HTTPException) as exc:
        await mu.run_update("camoufox")
    assert exc.value.detail == mu.CONTAINER_NOT_STARTED
    assert calls["removed"] == [] and forgot == []
    finished = mu.job_state()["finished"]["camoufox"]
    assert finished["status"] == "failed"
    assert finished["log"].startswith("The new version is installed but its container did not start.")


@pytest.mark.asyncio
async def test_a_turned_off_docker_mcp_says_its_container_starts_later(monkeypatch, temp_db):
    _docker_rig(monkeypatch, started=None)
    await mu.run_update("camoufox")
    assert mu.job_state()["finished"]["camoufox"]["log"] == (
        "Updated to v0.0.77. The MCP is turned off: its container starts with the new "
        "version when it is turned on.")


@pytest.mark.asyncio
async def test_an_update_queued_behind_a_delete_does_not_reinstall(monkeypatch):
    from core.credentials import catalog_install_registry

    # Its own name: a lock another loop waited on is bound to that loop.
    manifests = _known(monkeypatch, _manifest("pkg-deleted", runtime="node"))
    lock = catalog_install_registry.lock_for("pkg-deleted")
    await lock.acquire()
    try:
        mu.start_update("pkg-deleted")
        await _settle()
        assert mu.job_state()["running"]["pkg-deleted"]["phase"] == "queued"
        manifests.clear()   # the delete that held the lock removed it
    finally:
        lock.release()
    await _settle()
    finished = mu.job_state()["finished"]["pkg-deleted"]
    assert finished == {"status": "failed", "log": "MCP 'pkg-deleted' not found",
                        "ended_at": mu._jobs["pkg-deleted"].ended_at}


# ── the routes ─────────────────────────────────────────────────────

def _client():
    from api.mcp import mcps as mcps_api
    from auth.providers import UserContext, get_current_user

    async def _admin():
        return UserContext(sub="user-admin", email="admin@test.com", name="A", role="admin",
                           agents=[], agent_roles={}, is_api_key=False)

    app = FastAPI()
    app.include_router(mcps_api.router)
    app.dependency_overrides[get_current_user] = _admin
    return TestClient(app)


def test_the_update_route_answers_202_and_the_state_carries_the_job(monkeypatch, temp_db):
    from services.mcp import mcp_registry

    monkeypatch.setattr(mcp_registry, "get_manifest",
                        lambda n: _manifest("pkg", runtime="node") if n == "pkg" else None)
    monkeypatch.setattr(mcp_registry, "_manifests", {})

    async def _quick(name, **kw):
        return {"status": "updated", "version": "1.1.0", "runtime": "node", "install_log": "ok"}
    monkeypatch.setattr(mu, "update_one", _quick)

    with _client() as client:
        r = client.post("/v1/admin/mcps/pkg/update")
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "started", "name": "pkg"}
        body = {}
        for _ in range(50):
            body = client.get("/v1/admin/mcps/update-state").json()
            if body["finished"]:
                break
        assert body["running"] == {}
        assert body["finished"]["pkg"]["log"] == "ok"
        assert client.post("/v1/admin/mcps/ghost/update").status_code == 404


def test_a_second_press_is_409(monkeypatch, temp_db):
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda n: _manifest("pkg", runtime="node"))
    mu._register("pkg")
    with _client() as client:
        r = client.post("/v1/admin/mcps/pkg/update")
    assert r.status_code == 409
    assert r.json()["detail"] == "An update of this MCP is already running."


def test_a_job_that_ends_during_the_read_never_shows_beside_its_offer(monkeypatch, temp_db):
    """The job ends (its offer dropped, then the job finished) while the
    route reads the persisted offer: one answer must not carry the end and
    the offer the job cleared, or the card and its Update button stay."""
    from services.community import mcp_source_swap

    job = mu._register("pkg")
    offer = {"current": "1.0.0", "latest": "1.1.0", "registry": "npm", "package": "pkg",
             "reason": "package"}

    def _read_while_the_job_ends():
        mu._finish(job, mu.JOB_DONE, "ok")
        return {"updates": {"pkg": offer}, "checked": 1, "checked_at": "2026-10-29T16:00:00Z"}

    monkeypatch.setattr(mcp_source_swap, "build_update_state", _read_while_the_job_ends)
    with _client() as client:
        body = client.get("/v1/admin/mcps/update-state").json()
    assert not ("pkg" in body["finished"] and "pkg" in body["updates"])
    assert body["running"]["pkg"]["phase"] == "queued"


def _docker_controls(monkeypatch):
    from api.mcp import mcps as mcps_api
    from services.mcp import docker_manager, mcp_registry
    from storage.mcp import mcp_store

    monkeypatch.setattr(mcp_registry, "get_manifest", lambda n: _manifest())
    monkeypatch.setattr(docker_manager, "get_container_status", lambda m: docker_manager.RUNNING)
    monkeypatch.setattr(mcp_store, "get_mcp_state", lambda n: {"enabled": True})
    monkeypatch.setattr(docker_manager, "restart_container", lambda m, **k: False)
    monkeypatch.setattr(docker_manager, "start_container", lambda m, **k: True)
    monkeypatch.setattr(docker_manager, "stop_container", lambda m: True)
    return mcps_api


def test_restart_while_the_update_downloads_says_to_wait(monkeypatch, temp_db):
    _docker_controls(monkeypatch)
    mu._register("camoufox")
    with _client() as client:
        r = client.post("/v1/admin/mcps/camoufox/docker/restart")
        assert r.status_code == 409
        assert r.json()["detail"] == "An update is still downloading the image: wait for it to finish."
        for action in ("start", "stop"):
            r = client.post(f"/v1/admin/mcps/camoufox/docker/{action}")
            assert r.status_code == 409
            assert r.json()["detail"] == "An update of this MCP is running: wait for it to finish."


def test_restart_without_a_job_keeps_the_missing_image_sentence(monkeypatch, temp_db):
    _docker_controls(monkeypatch)
    with _client() as client:
        r = client.post("/v1/admin/mcps/camoufox/docker/restart")
    assert r.status_code == 500
    assert "run Update again" in r.json()["detail"]


def test_a_control_waits_for_a_change_that_holds_the_lock(monkeypatch, temp_db):
    from core.credentials import catalog_install_registry

    _docker_controls(monkeypatch)
    monkeypatch.setattr(catalog_install_registry, "lock_for",
                        lambda n: SimpleNamespace(locked=lambda: True))
    with _client() as client:
        r = client.post("/v1/admin/mcps/camoufox/docker/restart")
    assert r.status_code == 409
    assert r.json()["detail"] == "Another change to this MCP is running: wait for it to finish."
