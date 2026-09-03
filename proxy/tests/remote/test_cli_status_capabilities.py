"""``cli_status`` handling: satellite-reported CLI versions reach the
machines API, bounded and race-safe.

The satellite reports per-CLI ``{version, path}`` after every reconcile pass;
the proxy merges it into the connection's capabilities and persists a
SNAPSHOT (json.dumps on a worker thread must never iterate a dict the event
loop can still mutate), re-persisting the fresh connection's capabilities if
a reconnect replaced the connection mid-persist. The machines API carries
``cli_pins`` per machine so the dashboard can judge drift.
"""

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


def _conn(capabilities=None):
    return types.SimpleNamespace(capabilities=capabilities or {})


def _capture_persists(monkeypatch):
    from storage import remote_store
    calls = []
    monkeypatch.setattr(
        remote_store, "update_machine_capabilities",
        lambda machine_id, caps: calls.append((machine_id, caps)),
    )
    return calls


@pytest.mark.asyncio
async def test_cli_status_merges_and_persists_snapshot(monkeypatch):
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    conn = _conn({"os": "linux", "installed_clis": ["claude-code"]})
    cm._connections["m"] = conn
    persists = _capture_persists(monkeypatch)

    await cm.handle_message("m", {"type": "cli_status", "clis": {
        "claude": {"version": "2.1.220", "path": "/usr/bin/claude"},
        "codex": {"version": "0.145.0", "path": "/usr/bin/codex"},
    }})

    assert conn.capabilities["cli_status"]["claude"]["version"] == "2.1.220"
    assert len(persists) == 1
    machine_id, caps = persists[0]
    assert machine_id == "m"
    assert caps["cli_status"]["codex"]["path"] == "/usr/bin/codex"
    assert caps["os"] == "linux"  # merged into the full capabilities dict
    assert caps is not conn.capabilities  # snapshot, not the live dict


@pytest.mark.asyncio
async def test_cli_status_bounds_untrusted_input(monkeypatch):
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    conn = _conn()
    cm._connections["m"] = conn
    _capture_persists(monkeypatch)

    await cm.handle_message("m", {"type": "cli_status", "clis": {
        "claude": {"version": "v" * 1000, "path": 123, "extra": "dropped"},
        "codex": "not-a-dict",
        "evil-key": {"version": "x"},
    }})

    clis = conn.capabilities["cli_status"]
    assert set(clis) == {"claude"}          # whitelist: codex invalid, evil-key dropped
    assert len(clis["claude"]["version"]) == 300  # bounded
    assert clis["claude"]["path"] is None   # non-str coerced to None
    assert "extra" not in clis["claude"]


@pytest.mark.asyncio
async def test_cli_status_repersists_fresh_connection_after_race(monkeypatch):
    """If a reconnect replaces the connection while the persist runs, the
    fresh connection's capabilities are re-persisted so the stale thread
    write never stands as the last one."""
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    old = _conn({"gen": "old"})
    fresh = _conn({"gen": "fresh"})
    cm._connections["m"] = old

    from storage import remote_store
    persists = []

    def _persist(machine_id, caps):
        persists.append(dict(caps))
        # First (stale) persist: simulate the reconnect landing meanwhile.
        if len(persists) == 1:
            cm._connections["m"] = fresh

    monkeypatch.setattr(remote_store, "update_machine_capabilities", _persist)

    await cm.handle_message("m", {"type": "cli_status", "clis": {
        "claude": {"version": "2.1.220", "path": "/usr/bin/claude"},
    }})

    assert len(persists) == 2
    assert persists[1]["gen"] == "fresh"


def _app() -> FastAPI:
    from api.remote import remote_machines as rm

    user = UserContext(
        sub="admin-sub", email="a@test.com", name="A",
        role="admin", agents=[], agent_roles={},
    )

    async def _stub_user():
        return user

    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


def test_list_machines_carries_cli_pins_per_machine(monkeypatch):
    import config as app_config
    from storage import remote_store

    monkeypatch.setattr(app_config, "PINNED_CLAUDE_CODE_VERSION", "9.9.9")
    monkeypatch.setattr(app_config, "PINNED_CODEX_VERSION", "8.8.8")
    monkeypatch.setattr(remote_store, "get_all_remote_machines", lambda: [{
        "id": "m1", "name": "box", "status": "online", "last_seen": None,
        "capabilities": '{"installed_clis": ["claude-code"]}',
        "device_grants": "[]", "registered_by": "u",
    }])

    resp = TestClient(_app()).get("/v1/admin/remote-machines")
    assert resp.status_code == 200
    (m,) = resp.json()["machines"]
    assert m["cli_pins"] == {"claude": "9.9.9", "codex": "8.8.8"}
    assert m["capabilities"]["installed_clis"] == ["claude-code"]
