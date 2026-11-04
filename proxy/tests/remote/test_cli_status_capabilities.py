"""``cli_status`` handling: satellite-reported CLI versions reach the
machines API, bounded and race-safe.

The satellite reports per-CLI ``{version, path}`` after every reconcile pass;
the proxy merges it into the connection's capabilities and hands a SNAPSHOT
to the per-machine persister's ``caps`` lane (json.dumps on the DB executor
must never iterate a dict the event loop can still mutate). The lane is
newest-wins per machine, so a register() snapshot and a cli_status re-report
can never land out of order — whichever was requested last is what stands.
The machines API carries ``cli_pins`` per machine so the dashboard can judge
drift.
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
    assert await cm.drain_persists()

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
    assert await cm.drain_persists()

    clis = conn.capabilities["cli_status"]
    assert set(clis) == {"claude"}          # whitelist: codex invalid, evil-key dropped
    assert len(clis["claude"]["version"]) == 300  # bounded
    assert clis["claude"]["path"] is None   # non-str coerced to None
    assert "extra" not in clis["claude"]


@pytest.mark.asyncio
async def test_cli_status_snapshot_never_overtakes_a_newer_register(monkeypatch):
    """The reconnect race, lane edition: a cli_status snapshot from the OLD
    connection and the fresh register()'s snapshot go through the same
    newest-wins lane, so the LAST requested snapshot is the one that stands —
    never the stale one landing after a newer write (the old two-thread
    to_thread pair could)."""
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    old = _conn({"gen": "old"})
    fresh = _conn({"gen": "fresh"})
    cm._connections["m"] = old
    persists = _capture_persists(monkeypatch)

    # Stale report (old connection) queued first…
    await cm.handle_message("m", {"type": "cli_status", "clis": {
        "claude": {"version": "2.1.220", "path": "/usr/bin/claude"},
    }})
    # …then the reconnect's register-time snapshot for the same machine.
    cm._connections["m"] = fresh
    cm.persist_lane("m", "caps", dict(fresh.capabilities))
    assert await cm.drain_persists()

    assert persists, "nothing persisted"
    assert persists[-1][1]["gen"] == "fresh"
    # Coalescing: the two requests may collapse into one write, but the
    # stale snapshot can never be the last one written.
    assert all(c[1]["gen"] == "fresh" for c in persists[-1:])


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
        "device_grants": "[]", "registered_by": "u", "satellite_version": "0.5.164",
    }, {
        "id": "m2", "name": "old", "status": "online", "last_seen": None,
        "capabilities": "{}", "device_grants": "[]", "registered_by": "u",
        "satellite_version": "0.5.163",
    }])

    resp = TestClient(_app()).get("/v1/admin/remote-machines")
    assert resp.status_code == 200
    m, old = resp.json()["machines"]
    assert m["cli_pins"] == {"claude": "9.9.9", "codex": "8.8.8"}
    assert m["capabilities"]["installed_clis"] == ["claude-code"]
    # A satellite before 0.5.164 is shown the CLIs it was sent (it keeps
    # them until it updates): its Plan spawn predates the prompt tool, its
    # Codex terminal the provider guard.
    assert old["cli_pins"] == {"claude": "2.1.296", "codex": "0.163.0"}


@pytest.mark.parametrize("satellite_version,pin,want", [
    ("0.5.164", "2.1.312", "2.1.312"),
    ("0.5.170", "2.1.312", "2.1.312"),
    ("0.5.163", "2.1.312", "2.1.296"),
    ("0.4.999", "2.1.312", "2.1.296"),
    ("", "2.1.312", "2.1.296"),
    ("0.5.163", "2.1.290", "2.1.290"),
    ("0.5.163", "", ""),
])
def test_a_satellite_before_the_plan_prompt_spawn_keeps_the_older_claude_code(
        monkeypatch, satellite_version, pin, want):
    """Claude Code past 2.1.296 asks for a call the hook allowed in a Plan
    chat, which only a 0.5.164 satellite's spawn answers: an older satellite
    is sent 2.1.296 (never a higher pin than the platform's, an empty pin
    stays empty, no version reads as old)."""
    import config as app_config
    from core.session.session_manager import get_layer_by_path
    monkeypatch.setattr(app_config, "PINNED_CLAUDE_CODE_VERSION", pin)
    assert get_layer_by_path("claude-code-cli").pinned_cli_version_for(satellite_version) == want


@pytest.mark.parametrize("satellite_version,pin,want", [
    ("0.5.164", "0.166.0", "0.166.0"),
    ("0.5.163", "0.166.0", "0.163.0"),
    ("", "0.166.0", "0.163.0"),
    ("0.5.163", "0.160.0", "0.160.0"),
    ("0.5.163", "", ""),
])
def test_a_satellite_before_the_provider_guard_keeps_the_older_codex(
        monkeypatch, satellite_version, pin, want):
    """Codex 0.166 refuses a terminal resume across a model provider change,
    which only a 0.5.164 satellite's terminal turns into a fresh start: an
    older satellite is sent 0.163.0 until it updates."""
    import config as app_config
    from core.session.session_manager import get_layer_by_path
    monkeypatch.setattr(app_config, "PINNED_CODEX_VERSION", pin)
    assert get_layer_by_path("codex-cli").pinned_cli_version_for(satellite_version) == want


def test_the_handshake_sends_the_capped_pins_and_the_update_push_none(monkeypatch):
    import config as app_config
    from ws import satellite as sat_ws
    monkeypatch.setattr(app_config, "PINNED_CLAUDE_CODE_VERSION", "2.1.312")
    monkeypatch.setattr(app_config, "PINNED_CODEX_VERSION", "0.166.0")
    machine = {"allow_full_fs": False, "device_grants": "[]"}
    old = sat_ws._auth_ok(machine, satellite_version="0.5.163")["cli_pins"]
    new = sat_ws._auth_ok(machine, satellite_version="0.5.164")["cli_pins"]
    assert old == {"claude_code": "2.1.296", "codex": "0.163.0"}
    assert new == {"claude_code": "2.1.312", "codex": "0.166.0"}
    assert sat_ws._auth_ok(machine, cli_pins={}, satellite_version="0.5.163")["cli_pins"] == {}
