"""Integration tests for the install progress event flow.

Verifies that mcp_sync.sync_mcps_for_session emits its plan via the
``plan_cb`` callback (the registry-agnostic surface introduced when
install_registry replaced chat_id routing), and that the same shape
fans out through install_registry to the registered per-user broadcaster.

Does not spawn a real satellite or CLI — uses module-level patching to
keep tests fast and deterministic.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.remote import install_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    install_registry._inflight.clear()
    install_registry.set_broadcaster(None)
    yield
    install_registry._inflight.clear()
    install_registry.set_broadcaster(None)


@pytest.mark.asyncio
async def test_progress_event_shape_fans_out_through_broadcaster():
    """The closure in remote_execution.start_session forwards satellite
    progress events to install_registry, which hands them to the per-user
    broadcaster. Simulate the closure + broadcaster pattern.
    """
    received: list[dict] = []

    async def _broadcast(machine_id, ev, recipients):
        received.append(ev)

    install_registry.set_broadcaster(_broadcast)

    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    sat_event = {
        "mcp": "file-tools",
        "phase": "pip",
        "pct": 50,
        "message": "pip install file-tools",
    }
    await install_registry.emit(machine_id, agent, {
        "type": "install_progress",
        "machine_id": machine_id,
        "agent": agent,
        "mcp": sat_event["mcp"],
        "phase": sat_event["phase"],
        "pct": sat_event["pct"],
        "message": sat_event["message"],
    })

    assert len(received) == 1
    assert received[0]["type"] == "install_progress"
    assert received[0]["machine_id"] == machine_id
    assert received[0]["agent"] == agent
    assert received[0]["mcp"] == "file-tools"
    assert received[0]["pct"] == 50


@pytest.mark.asyncio
async def test_mcp_install_failed_event_shape():
    """Soft-fail surface: each excluded MCP gets a mcp_install_failed event
    keyed by (machine_id, agent) — not chat_id.
    """
    received: list[dict] = []

    async def _broadcast(machine_id, ev, recipients):
        received.append(ev)

    install_registry.set_broadcaster(_broadcast)

    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    await install_registry.emit(machine_id, agent, {
        "type": "mcp_install_failed",
        "machine_id": machine_id,
        "agent": agent,
        "mcp": "unifi-network",
        "error": "Python>=3.13 required",
    })

    assert len(received) == 1
    assert received[0]["type"] == "mcp_install_failed"
    assert received[0]["mcp"] == "unifi-network"
    assert "Python" in received[0]["error"]


@pytest.mark.asyncio
async def test_connect_replays_history_then_live_broadcast():
    """A tab that connects mid-install replays the bounded event history
    (what the dashboard pushes from snapshot_inflight on connect) so the UI
    catches up; subsequent emits reach it live via the broadcaster.
    """
    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    # Live events fire before any tab is watching — they accumulate in
    # history and (if a broadcaster is set) get delivered live.
    await install_registry.emit(machine_id, agent, {"type": "install_started", "machine_id": machine_id, "agent": agent})
    await install_registry.emit(machine_id, agent, {"type": "install_progress", "machine_id": machine_id, "agent": agent, "mcp": "a", "pct": 10})
    await install_registry.emit(machine_id, agent, {"type": "install_progress", "machine_id": machine_id, "agent": agent, "mcp": "a", "pct": 50})

    # A tab connects: the dashboard reads snapshot_inflight and replays the
    # in-flight install's history into that connection.
    replayed: list[dict] = []
    snap = install_registry.snapshot_inflight()
    rec = next(r for r in snap if r.machine_id == machine_id and r.agent == agent)
    for past_ev in list(rec.event_history):
        replayed.append(past_ev)

    assert len(replayed) == 3
    assert replayed[0]["type"] == "install_started"
    assert replayed[-1]["pct"] == 50

    # From now on the connection is registered with notification_manager, so
    # live events reach it via the broadcaster.
    live: list[dict] = []

    async def _broadcast(mid, ev, recipients):
        live.append(ev)

    install_registry.set_broadcaster(_broadcast)
    await install_registry.emit(machine_id, agent, {"type": "install_done", "machine_id": machine_id, "agent": agent})
    assert live[-1]["type"] == "install_done"


@pytest.mark.asyncio
async def test_install_failed_emits_through_registry():
    """Whole-install failure is emitted through the registry so it reaches
    the broadcaster (and the bounded history for connect replay).
    """
    received: list[dict] = []

    async def _broadcast(machine_id, ev, recipients):
        received.append(ev)

    install_registry.set_broadcaster(_broadcast)

    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    await install_registry.emit(machine_id, agent, {
        "type": "install_failed",
        "machine_id": machine_id,
        "agent": agent,
        "error": "sync_mcps timed out",
    })

    assert received[-1]["type"] == "install_failed"
    assert "timed out" in received[-1]["error"]


@pytest.mark.asyncio
async def test_install_heartbeat_when_quiet():
    """During quiet install, a heartbeat fires every 15s of registry
    silence. Simulate by manually emitting heartbeats.
    """
    received: list[dict] = []

    async def _broadcast(machine_id, ev, recipients):
        received.append(ev)

    install_registry.set_broadcaster(_broadcast)

    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    await install_registry.emit(machine_id, agent, {"type": "install_heartbeat", "machine_id": machine_id, "agent": agent})
    await install_registry.emit(machine_id, agent, {"type": "install_heartbeat", "machine_id": machine_id, "agent": agent})

    assert sum(1 for ev in received if ev["type"] == "install_heartbeat") == 2


@pytest.mark.asyncio
async def test_mcp_sync_invokes_plan_cb(monkeypatch):
    """mcp_sync.sync_mcps_for_session calls plan_cb once the diff is
    computed. Verified by mocking the satellite connection manager.
    """
    from services.mcp import mcp_sync

    plan_events: list[dict] = []

    async def _plan_cb(ev):
        plan_events.append(ev)

    fake_cm = MagicMock()
    fake_cm.is_connected = MagicMock(return_value=True)
    fake_cm.get_install_lock = MagicMock(return_value=_async_ctx())
    fake_cm.send_command = AsyncMock(return_value={"results": {}})
    fake_cm.register_install_progress = MagicMock()
    fake_cm.unregister_install_progress = MagicMock()

    fake_layer = MagicMock()
    fake_layer._sessions = {}

    async def _fake_fetch(cm, machine_id):
        return {}

    fake_manifest = MagicMock()
    fake_manifest.server.runtime = "node"
    fake_manifest.server.source = "npm:fake@1.0"
    fake_manifest.category = "community"
    fake_manifest.system_requirements.debian = []
    fake_manifest.system_requirements.ubuntu = []
    fake_manifest.system_requirements.rhel = []
    fake_manifest.system_requirements.arch = []
    fake_manifest.system_requirements.macos_brew = []
    fake_manifest.system_requirements.node_min = ""
    fake_manifest.system_requirements.notes = ""

    fake_tarball = MagicMock()
    fake_tarball.tarball_b64 = ""
    fake_tarball.version_hash = "hash1"

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _fake_fetch)
    monkeypatch.setattr(
        "services.mcp.mcp_registry.get_manifest",
        lambda name: fake_manifest if name == "file-tools" else None,
    )
    built_on_loop: list[bool] = []

    def _build(name):
        # The walk and the gzip run off the loop (over a second at a start).
        try:
            asyncio.get_running_loop()
            built_on_loop.append(True)
        except RuntimeError:
            built_on_loop.append(False)
        return fake_tarball

    monkeypatch.setattr("services.mcp.mcp_tarball.build_tarball", _build)
    monkeypatch.setattr(
        "core.remote.satellite_connection.get_connection_manager",
        lambda: fake_cm,
    )
    monkeypatch.setattr(
        "core.remote.remote_execution.get_remote_layer",
        lambda: fake_layer,
    )
    monkeypatch.setattr(
        mcp_sync, "_manifest_to_dict",
        lambda m: {"name": "file-tools"},
    )

    await mcp_sync.sync_mcps_for_session(
        "machine-1", "session-1", ["file-tools"], plan_cb=_plan_cb,
    )

    assert built_on_loop == [False]
    assert len(plan_events) == 1
    assert plan_events[0]["mcps_to_install"] == ["file-tools"]
    assert plan_events[0]["mcps_to_update"] == []


@pytest.mark.asyncio
async def test_mcp_sync_skips_plan_cb_when_no_install_needed(monkeypatch):
    """When the satellite is already in sync, plan_cb is not invoked."""
    from services.mcp import mcp_sync

    plan_events: list[dict] = []

    async def _plan_cb(ev):
        plan_events.append(ev)

    fake_cm = MagicMock()
    fake_cm.is_connected = MagicMock(return_value=True)
    fake_cm.get_install_lock = MagicMock(return_value=_async_ctx())

    fake_layer = MagicMock()
    fake_layer._sessions = {}

    # Satellite already has file-tools healthy with matching version_hash.
    async def _fake_fetch(cm, machine_id):
        return {"file-tools": {"version_hash": "hash1", "healthy": True}}

    fake_manifest = MagicMock()
    fake_manifest.server.runtime = "node"
    fake_manifest.mcp_dir = "/fake/dir"

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _fake_fetch)
    monkeypatch.setattr(
        "services.mcp.mcp_registry.get_manifest",
        lambda name: fake_manifest if name == "file-tools" else None,
    )
    monkeypatch.setattr(
        "services.mcp.mcp_installer.compute_version_hash",
        lambda d: "hash1",
    )
    monkeypatch.setattr(
        "core.remote.satellite_connection.get_connection_manager",
        lambda: fake_cm,
    )
    monkeypatch.setattr(
        "core.remote.remote_execution.get_remote_layer",
        lambda: fake_layer,
    )

    result = await mcp_sync.sync_mcps_for_session(
        "machine-1", "session-1", ["file-tools"], plan_cb=_plan_cb,
    )

    assert result.ok
    # Nothing to install/update/remove → plan_cb never fires.
    assert plan_events == []


@pytest.mark.asyncio
async def test_sync_result_captures_warmup_failures(monkeypatch):
    """The satellite ack reports a per-MCP pre-warm boot result in
    ``results[name]["warmup"]``. ``sync_mcps_for_session`` folds
    ``"warn:<reason>"`` into ``SyncResult.warmup_failed`` — advisory only,
    so the MCP is still counted as installed and is NOT excluded."""
    from services.mcp import mcp_sync

    fake_cm = MagicMock()
    fake_cm.is_connected = MagicMock(return_value=True)
    fake_cm.get_install_lock = MagicMock(return_value=_async_ctx())
    fake_cm.register_install_progress = MagicMock()
    fake_cm.unregister_install_progress = MagicMock()
    # Two MCPs installed ok; one answered initialize, one timed out.
    fake_cm.send_command = AsyncMock(return_value={"results": {
        "file-tools": {"status": "ok", "version_hash": "h1", "warmup": "ok"},
        "google-maps": {"status": "ok", "version_hash": "h2", "warmup": "warn:timeout"},
    }})

    fake_layer = MagicMock()
    fake_layer._sessions = {}

    async def _fake_fetch(cm, machine_id):
        return {}

    def _mk_manifest():
        m = MagicMock()
        m.server.runtime = "python"
        m.server.source = "pypi:x@1"
        m.category = "custom"
        for f in ("debian", "ubuntu", "rhel", "arch", "macos_brew"):
            setattr(m.system_requirements, f, [])
        m.system_requirements.node_min = ""
        m.system_requirements.notes = ""
        return m

    fake_tb = MagicMock()
    fake_tb.tarball_b64 = ""
    fake_tb.version_hash = "h"

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _fake_fetch)
    monkeypatch.setattr(
        "services.mcp.mcp_registry.get_manifest",
        lambda name: _mk_manifest() if name in ("file-tools", "google-maps") else None,
    )
    monkeypatch.setattr("services.mcp.mcp_tarball.build_tarball", lambda name: fake_tb)
    monkeypatch.setattr(
        "core.remote.satellite_connection.get_connection_manager", lambda: fake_cm)
    monkeypatch.setattr(
        "core.remote.remote_execution.get_remote_layer", lambda: fake_layer)
    monkeypatch.setattr(
        mcp_sync, "_manifest_to_dict", lambda m: {"name": "x"})

    result = await mcp_sync.sync_mcps_for_session(
        "machine-1", "session-1", ["file-tools", "google-maps"],
    )

    assert set(result.installed) == {"file-tools", "google-maps"}
    assert result.warmup_failed == {"google-maps": "timeout"}
    # Advisory: a warm-up failure does NOT exclude the MCP from the session.
    assert result.excluded_names == set()


class _async_ctx:
    """Tiny async context manager stub that no-ops on enter/exit."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


# ---------------------------------------------------------------------------
# Deterministic-failure memo (_unsatisfiable_installs): a resolver-
# unsatisfiable install (e.g. MCP needs Python >= 3.11, satellite venv is
# 3.10) must not be re-attempted on every session — the doomed ~0.5s install
# re-ran per warmup and flashed the install bar each time. Memo keys on the
# shipped hash; success / platform change / force all clear it; transient
# failures are never memoized.
# ---------------------------------------------------------------------------

_UNSAT_ERR = (
    "RuntimeError: Using Python 3.10.12 environment at: venv\n"
    "  x No solution found when resolving dependencies:\n"
    "  Because the current Python version (3.10.12) does not satisfy "
    "Python>=3.11 ..."
)


def _memo_env(monkeypatch, *, results, version_hash="hash1"):
    """Shared mock rig for sync_mcps_for_session memo tests. Returns
    (fake_cm, plan_events)."""
    from services.mcp import mcp_sync

    plan_events: list[dict] = []

    async def _plan_cb(ev):
        plan_events.append(ev)

    fake_cm = MagicMock()
    fake_cm.is_connected = MagicMock(return_value=True)
    fake_cm.get_install_lock = MagicMock(return_value=_async_ctx())
    fake_cm.send_command = AsyncMock(return_value={"results": results})
    fake_cm.register_install_progress = MagicMock()
    fake_cm.unregister_install_progress = MagicMock()
    fake_cm.get_connection = MagicMock(
        return_value=MagicMock(satellite_version="0.5.100"))

    fake_layer = MagicMock()
    fake_layer._sessions = {}

    async def _fake_fetch(cm, machine_id):
        return {}  # nothing installed → music-gen-mcp plans as to_install

    fake_manifest = MagicMock()
    fake_manifest.server.runtime = "python"
    fake_manifest.server.source = "local"
    fake_manifest.category = "custom"
    fake_manifest.mcp_dir = "/tmp/music-gen-mcp"
    for f in ("debian", "ubuntu", "rhel", "arch", "macos_brew"):
        setattr(fake_manifest.system_requirements, f, [])
    fake_manifest.system_requirements.node_min = ""
    fake_manifest.system_requirements.notes = ""

    fake_tarball = MagicMock()
    fake_tarball.tarball_b64 = ""
    fake_tarball.version_hash = version_hash

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _fake_fetch)
    monkeypatch.setattr(
        "services.mcp.mcp_registry.get_manifest",
        lambda name: fake_manifest if name == "music-gen-mcp" else None,
    )
    monkeypatch.setattr(
        "services.mcp.mcp_installer.compute_version_hash",
        lambda mcp_dir: version_hash,
    )
    monkeypatch.setattr(
        "services.mcp.mcp_tarball.build_tarball", lambda name: fake_tarball,
    )
    monkeypatch.setattr(
        "core.remote.satellite_connection.get_connection_manager",
        lambda: fake_cm,
    )
    monkeypatch.setattr(
        "core.remote.remote_execution.get_remote_layer", lambda: fake_layer,
    )
    monkeypatch.setattr(
        mcp_sync, "_manifest_to_dict", lambda m: {"name": "music-gen-mcp"},
    )
    return fake_cm, _plan_cb, plan_events


@pytest.mark.asyncio
async def test_unsat_failure_memoizes_and_skips_next_sync(monkeypatch):
    from services.mcp import mcp_sync

    mcp_sync._unsatisfiable_installs.clear()
    fake_cm, plan_cb, plan_events = _memo_env(
        monkeypatch,
        results={"music-gen-mcp": {"status": "error", "error": _UNSAT_ERR}},
    )

    r1 = await mcp_sync.sync_mcps_for_session(
        "machine-1", "s1", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    assert "music-gen-mcp" in r1.excluded_names
    assert not r1.memoized  # first failure was a REAL attempt
    assert ("machine-1", "music-gen-mcp") in mcp_sync._unsatisfiable_installs
    assert fake_cm.send_command.await_count == 1
    assert len(plan_events) == 1

    # Second session: no re-attempt, no plan (→ no install bar), still
    # excluded so the CLI config strips it, and flagged memoized so the
    # caller skips the per-session failure frame.
    r2 = await mcp_sync.sync_mcps_for_session(
        "machine-1", "s2", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    assert r2.ok is True
    assert "music-gen-mcp" in r2.excluded_names
    assert "music-gen-mcp" in r2.memoized
    assert "No solution found" in r2.failed["music-gen-mcp"]
    assert fake_cm.send_command.await_count == 1  # unchanged
    assert len(plan_events) == 1  # unchanged

    # Another machine is unaffected by this machine's memo.
    assert ("machine-2", "music-gen-mcp") not in mcp_sync._unsatisfiable_installs
    mcp_sync._unsatisfiable_installs.clear()


@pytest.mark.asyncio
async def test_transient_failure_is_not_memoized(monkeypatch):
    from services.mcp import mcp_sync

    mcp_sync._unsatisfiable_installs.clear()
    fake_cm, plan_cb, plan_events = _memo_env(
        monkeypatch,
        results={"music-gen-mcp": {"status": "error",
                                   "error": "network timeout fetching wheel"}},
    )
    r1 = await mcp_sync.sync_mcps_for_session(
        "machine-1", "s1", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    assert "music-gen-mcp" in r1.excluded_names
    assert not mcp_sync._unsatisfiable_installs  # transient → no memo
    # Next session attempts again.
    await mcp_sync.sync_mcps_for_session(
        "machine-1", "s2", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    assert fake_cm.send_command.await_count == 2


@pytest.mark.asyncio
async def test_memo_cleared_when_platform_mcp_changes(monkeypatch):
    from services.mcp import mcp_sync

    mcp_sync._unsatisfiable_installs.clear()
    mcp_sync._unsatisfiable_installs[("machine-1", "music-gen-mcp")] = {
        "hash": "OLD_HASH", "error": _UNSAT_ERR,
    }
    # Platform now hashes differently → the verdict may differ → retry.
    fake_cm, plan_cb, plan_events = _memo_env(
        monkeypatch, results={"music-gen-mcp": {"status": "ok"}},
        version_hash="NEW_HASH",
    )
    r = await mcp_sync.sync_mcps_for_session(
        "machine-1", "s1", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    assert fake_cm.send_command.await_count == 1
    assert r.installed == ["music-gen-mcp"]
    assert not mcp_sync._unsatisfiable_installs  # dropped pre-attempt


@pytest.mark.asyncio
async def test_force_clears_memo_and_retries(monkeypatch):
    from services.mcp import mcp_sync

    mcp_sync._unsatisfiable_installs.clear()
    mcp_sync._unsatisfiable_installs[("machine-1", "music-gen-mcp")] = {
        "hash": "hash1", "error": _UNSAT_ERR,
    }
    fake_cm, plan_cb, plan_events = _memo_env(
        monkeypatch, results={"music-gen-mcp": {"status": "ok"}},
    )
    r = await mcp_sync.sync_mcps_for_session(
        "machine-1", "s1", ["music-gen-mcp"], plan_cb=plan_cb, force=True,
    )
    assert fake_cm.send_command.await_count == 1
    assert r.installed == ["music-gen-mcp"]
    assert not mcp_sync._unsatisfiable_installs


@pytest.mark.asyncio
async def test_successful_install_clears_memo(monkeypatch):
    # Belt-and-braces: a satellite that somehow installs it (e.g. its venv
    # python was upgraded and force-retried elsewhere) clears the memo via
    # the ok branch even if an entry lingered.
    from services.mcp import mcp_sync

    mcp_sync._unsatisfiable_installs.clear()
    fake_cm, plan_cb, plan_events = _memo_env(
        monkeypatch, results={"music-gen-mcp": {"status": "ok"}},
        version_hash="hash2",
    )
    mcp_sync._unsatisfiable_installs[("machine-1", "music-gen-mcp")] = {
        "hash": "STALE", "error": _UNSAT_ERR,
    }
    await mcp_sync.sync_mcps_for_session(
        "machine-1", "s1", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    assert not mcp_sync._unsatisfiable_installs


@pytest.mark.asyncio
async def test_memo_cleared_when_satellite_updates(monkeypatch):
    """A satellite self-update can change installer behavior (e.g. gain the
    python-floor retry) — a memo recorded under the old version must not
    mask the fix. The memo keys on satellite_version and re-attempts once
    the connected version differs."""
    from services.mcp import mcp_sync

    mcp_sync._unsatisfiable_installs.clear()
    fake_cm, plan_cb, plan_events = _memo_env(
        monkeypatch,
        results={"music-gen-mcp": {"status": "error", "error": _UNSAT_ERR}},
    )
    await mcp_sync.sync_mcps_for_session(
        "machine-1", "s1", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    memo = mcp_sync._unsatisfiable_installs[("machine-1", "music-gen-mcp")]
    assert memo["satellite_version"] == "0.5.100"

    # Same version → skipped.
    await mcp_sync.sync_mcps_for_session(
        "machine-1", "s2", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    assert fake_cm.send_command.await_count == 1

    # Satellite updated → memo dropped, install re-attempted.
    fake_cm.get_connection = MagicMock(
        return_value=MagicMock(satellite_version="0.5.101"))
    fake_cm.send_command = AsyncMock(
        return_value={"results": {"music-gen-mcp": {"status": "ok"}}})
    r = await mcp_sync.sync_mcps_for_session(
        "machine-1", "s3", ["music-gen-mcp"], plan_cb=plan_cb,
    )
    assert fake_cm.send_command.await_count == 1
    assert r.installed == ["music-gen-mcp"]
    assert not mcp_sync._unsatisfiable_installs


# ── a remote session's prompt after its MCP sync ─────────────────────────────

_PROMPT = "\n".join([
    "You are the agent.",
    "\n\n---\n\n# Available Tools (MCPs)\n\nYou have these MCP servers enabled in this session — each provides a set of tools you can call:\n",
    "- **Music** (`music-gen-mcp`) — Generate music.",
    "- **Notes** (`notes-mcp`) — Keep notes.",
    "\n\nDetailed usage instructions for each MCP follow in `# MCP Tool Skills` below.",
    "\n\n---\n\n# MCP Tool Skills\n\nThe following tool instructions are auto-loaded from active MCP servers.\n",
    "\n## Music\nCall generate_music.",
    "\n## Notes\nCall add_note.",
])


def _fold(adapter_cls, payload, reasons, monkeypatch):
    bodies = {"music-gen-mcp": ["## Music\nCall generate_music."]}
    monkeypatch.setattr("configuration.prompts._always_skill_bodies", lambda name: bodies.get(name, []))
    return adapter_cls.with_unavailable_mcps(adapter_cls, payload, reasons)


def test_unavailable_reasons_name_each_install_failure():
    from services.mcp.mcp_sync import SyncResult, unavailable_reasons
    r = SyncResult(failed={"music-gen-mcp": "npm ERR!\n  code E404", "stray.bak": "unsafe"},
                   excluded_names={"music-gen-mcp", "x-mcp", "stray.bak"})
    assert unavailable_reasons(r, {"music-gen-mcp", "x-mcp", "notes-mcp"}) == {
        "music-gen-mcp": "could not be installed on this machine (npm ERR! code E404)",
        "x-mcp": "could not be installed on this machine",
    }  # a folder the machine holds for no assigned MCP never reaches the prompt


def test_an_excluded_mcp_leaves_the_prompt_catalog_for_unavailable(monkeypatch):
    from core.layers.cli.remote import ClaudeRemoteAdapter
    payload = {"system_prompt": _PROMPT}
    out = _fold(ClaudeRemoteAdapter, payload, {"music-gen-mcp": "could not be installed"}, monkeypatch)
    text = out["system_prompt"]
    assert "(`music-gen-mcp`)" not in text and "(`notes-mcp`)" in text
    assert "Call generate_music." not in text and "Call add_note." in text
    assert text.endswith(
        "# Unavailable Tools\n\nThe following MCP servers are NOT available in this session:\n\n"
        "- **music-gen-mcp**: could not be installed\n\n"
        "Do not attempt to use tools from these servers. "
        "If the user asks about them, explain they need to be configured first.")


def test_the_fold_joins_an_existing_unavailable_block(monkeypatch):
    from configuration import prompts
    from core.layers.codex.remote import CodexRemoteAdapter
    prompt = _PROMPT + "\n".join([
        "", prompts._UNAVAILABLE_HEAD, prompts._unavailable_row("ads-mcp", "Excluded in task mode"),
        prompts._UNAVAILABLE_TAIL, "\n## Permission context",
    ])
    payload = {"system_prompt": prompt, "agents_md_content": prompt}
    out = _fold(CodexRemoteAdapter, payload, {"music-gen-mcp": "could not be installed"}, monkeypatch)
    for key in ("system_prompt", "agents_md_content"):
        text = out[key]
        assert text.count("# Unavailable Tools") == 1
        assert ("- **ads-mcp**: Excluded in task mode\n- **music-gen-mcp**: could not be installed\n\n"
                "Do not attempt") in text
        assert text.rstrip().endswith("## Permission context")
        assert "(`music-gen-mcp`)" not in text


# --- The churn: what one session leaves out, the next must not rebuild -------


class _McpFolder:
    """A satellite's MCP folder: verify reports it, ``sync_mcps`` applies a
    frame to it as the satellite does (``core`` only under the flag)."""

    def __init__(self, installed: dict[str, str], version: str):
        self.installed = dict(installed)       # name -> category
        self.version = version
        self.frames: list[dict] = []

    async def send_command(self, machine_id, msg, *, timeout=30.0, command_id=None):
        if msg["type"] == "sync_mcps_verify":
            return {"mcps": {n: {"version_hash": "h", "healthy": True, "category": c}
                             for n, c in self.installed.items()}}
        self.frames.append(msg)
        results = {}
        for spec in msg["mcps_to_install"]:
            self.installed[spec["name"]] = spec["category"]
            results[spec["name"]] = {"status": "ok"}
        cats = (("core", "custom", "community") if msg.get("remove_any_category")
                else ("custom", "community"))
        for name in msg["mcps_to_remove"]:
            if self.installed.get(name) in cats:
                del self.installed[name]
                results[name] = {"status": "removed"}
            else:
                results[name] = {"status": "not_found"}
        return {"results": results}


def _platform(monkeypatch, folder: _McpFolder, shipped: dict[str, str]):
    """The platform ships ``shipped`` (name -> category); the connection
    manager talks to ``folder``."""
    from types import SimpleNamespace

    from services.mcp import mcp_sync

    manifests = {}
    for name, cat in shipped.items():
        m = MagicMock()
        m.name = name
        m.category = cat
        m.server.runtime = "python"
        m.server.transport = "stdio"
        manifests[name] = m
    cm = MagicMock()
    cm.is_connected = MagicMock(return_value=True)
    cm.get_install_lock = MagicMock(side_effect=lambda mid: _async_ctx())
    cm.send_command = AsyncMock(side_effect=folder.send_command)
    cm.get_connection = MagicMock(return_value=SimpleNamespace(
        satellite_version=folder.version, capabilities={}))
    cm._satellite_at_least = lambda mid, v: tuple(
        int(p) for p in folder.version.split(".")) >= v

    async def _hashes(names, *, force=False):
        return {n: "h" for n in names}

    tb = MagicMock()
    tb.tarball_b64 = ""
    tb.version_hash = "h"
    monkeypatch.setattr("services.mcp.mcp_registry.get_manifest", manifests.get)
    monkeypatch.setattr("services.mcp.mcp_registry.get_all_manifests", lambda: dict(manifests))
    monkeypatch.setattr(mcp_sync, "version_hashes_for", _hashes)
    monkeypatch.setattr(mcp_sync, "_manifest_to_dict", lambda m: {"name": m.name})
    monkeypatch.setattr(mcp_sync, "_install_spec",
                        lambda name, m, tb: {"name": name, "category": m.category})
    monkeypatch.setattr("services.mcp.mcp_tarball.build_tarball", lambda name: tb)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    monkeypatch.setattr("core.remote.remote_execution.get_remote_layer",
                        lambda: SimpleNamespace(_sessions={}))


_SHIPPED = {"file-x": "custom", "image-gen-mcp": "custom", "agent-config-mcp": "core"}


@pytest.mark.asyncio
async def test_a_task_session_then_a_chat_session_installs_nothing(monkeypatch):
    from services.mcp import mcp_sync
    folder = _McpFolder(_SHIPPED, "0.5.136")
    _platform(monkeypatch, folder, _SHIPPED)
    plans: list[dict] = []

    async def _plan(ev):
        plans.append(ev)

    # The task leaves out its ``exclude_from: task`` MCPs.
    await mcp_sync.sync_mcps_for_session("m-1", "task-sess", ["file-x"], plan_cb=_plan)
    await mcp_sync.sync_mcps_for_session(
        "m-1", "chat-sess", ["file-x", "image-gen-mcp", "agent-config-mcp"], plan_cb=_plan)
    assert folder.installed == _SHIPPED
    assert folder.frames == []         # no rebuild, no removal, no round trip
    assert plans == []


@pytest.mark.asyncio
async def test_no_round_trip_for_a_core_leftover_on_an_old_satellite(monkeypatch):
    from services.mcp import mcp_sync
    folder = _McpFolder({**_SHIPPED, "retired-core": "core", "odd": "extras"}, "0.5.136")
    _platform(monkeypatch, folder, _SHIPPED)
    result = await mcp_sync.sync_mcps_for_session("m-1", "s-1", ["file-x"])
    assert folder.frames == []         # it would answer not_found every start
    assert result.ok


@pytest.mark.asyncio
async def test_a_0_5_137_satellite_removes_a_retired_mcp_from_any_category(monkeypatch, caplog):
    from services.mcp import mcp_sync
    folder = _McpFolder({**_SHIPPED, "retired-core": "core", "retired": "community"}, "0.5.137")
    _platform(monkeypatch, folder, _SHIPPED)
    with caplog.at_level("INFO", logger="otodock.mcp-sync"):
        result = await mcp_sync.sync_mcps_for_session("m-1", "s-1", ["file-x"])
    [frame] = folder.frames
    assert frame["remove_any_category"] is True
    assert sorted(frame["mcps_to_remove"]) == ["retired", "retired-core"]
    assert folder.installed == _SHIPPED
    assert sorted(result.removed) == ["retired", "retired-core"]
    assert "sync_mcps plan m-1: install=[] update=[] remove=['retired', 'retired-core']" in caplog.text


@pytest.mark.asyncio
async def test_sync_now_keeps_its_reach(monkeypatch):
    """Admin Sync Now (no session) removes what no agent targeting the
    machine wants, shipped or not, never ``core`` (no flag)."""
    from services.mcp import mcp_sync
    folder = _McpFolder(_SHIPPED, "0.5.137")
    _platform(monkeypatch, folder, _SHIPPED)
    await mcp_sync.sync_mcps_for_session("m-1", "", ["file-x"])
    [frame] = folder.frames
    assert "remove_any_category" not in frame
    assert frame["mcps_to_remove"] == ["image-gen-mcp"]
    assert folder.installed == {"file-x": "custom", "agent-config-mcp": "core"}


@pytest.mark.asyncio
async def test_a_lifted_mcp_is_installed_with_the_session(monkeypatch):
    from services.mcp import mcp_sync
    folder = _McpFolder({"file-x": "custom"}, "0.5.137")
    _platform(monkeypatch, folder, _SHIPPED)
    await mcp_sync.sync_mcps_for_session("m-1", "task-sess", ["file-x"],
                                         also_wanted={"image-gen-mcp"})
    assert folder.installed == {"file-x": "custom", "image-gen-mcp": "custom"}


def _racing_build(monkeypatch, races: int) -> list[str]:
    """``build_tarball`` meets an install rewriting the folder ``races``
    times, then builds; returns the names built."""
    from services.mcp import mcp_tarball
    tb = MagicMock()
    tb.tarball_b64 = ""
    tb.version_hash = "h"
    calls: list[str] = []

    def _build(name):
        calls.append(name)
        if len(calls) <= races:
            raise mcp_tarball.TreeChanged(f"{name} changed while its tarball was built")
        return tb

    monkeypatch.setattr("services.mcp.mcp_tarball.build_tarball", _build)
    return calls


@pytest.mark.asyncio
async def test_a_tree_changed_under_the_build_is_built_once_more(monkeypatch, caplog):
    from services.mcp import mcp_sync
    folder = _McpFolder({}, "0.5.137")
    _platform(monkeypatch, folder, _SHIPPED)
    calls = _racing_build(monkeypatch, races=1)
    with caplog.at_level("WARNING", logger=mcp_sync.logger.name):
        await mcp_sync.sync_mcps_for_session("m-1", "s-1", ["file-x"])
    assert calls == ["file-x", "file-x"]
    assert folder.installed == {"file-x": "custom"}
    assert [r for r in caplog.records if r.name == mcp_sync.logger.name
            and r.levelname in ("WARNING", "ERROR")] == []


@pytest.mark.asyncio
async def test_a_tree_changed_twice_leaves_the_mcp_out_with_one_quiet_line(monkeypatch, caplog):
    """An install rewriting the folder is an expected race: one WARNING with
    no traceback, and the session goes on without that MCP."""
    from services.mcp import mcp_sync
    folder = _McpFolder({}, "0.5.137")
    _platform(monkeypatch, folder, _SHIPPED)
    calls = _racing_build(monkeypatch, races=2)
    with caplog.at_level("WARNING", logger=mcp_sync.logger.name):
        await mcp_sync.sync_mcps_for_session("m-1", "s-1", ["file-x"])
    assert calls == ["file-x", "file-x"]
    assert "file-x" not in folder.installed
    lines = [r for r in caplog.records if r.name == mcp_sync.logger.name
             and r.levelname in ("WARNING", "ERROR")]
    assert [(r.levelname, r.getMessage(), r.exc_info) for r in lines] == [(
        "WARNING",
        "file-x changed while its tarball was built, twice: it is left out of this session's sync",
        None,
    )]



# ---------------------------------------------------------------------------
# A slow machine: each MCP judged by its own budget, the session started
# early with the tools that are ready, the chat told what still installs.
# The satellite installs a plan in order and names each MCP's own start and
# end; its installer's own lines say "<name>.new".
# ---------------------------------------------------------------------------

class _Satellite:
    """A satellite the test drives: the plan's progress through the
    registered callback, the ack when the test says."""

    def __init__(self):
        from types import SimpleNamespace
        self.cbs: dict = {}
        self.sent: list = []
        self.ack: asyncio.Future | None = None
        self.cid = ""
        self.lock = asyncio.Lock()
        self.conn = SimpleNamespace(satellite_version="0.5.157")
        self.sent_plan = asyncio.Event()

    def is_connected(self, mid):
        return True

    def get_install_lock(self, mid):
        return self.lock

    def register_install_progress(self, cid, cb):
        self.cbs[cid] = cb

    def unregister_install_progress(self, cid):
        self.cbs.pop(cid, None)

    def get_connection(self, mid):
        return self.conn

    def _satellite_at_least(self, mid, version):
        return True

    async def send_command(self, mid, frame, *, timeout=30.0, command_id=None):
        if frame["type"] != "sync_mcps":
            return {"mcps": {}}
        self.sent.append((frame, timeout))
        self.cid = command_id
        self.ack = asyncio.get_running_loop().create_future()
        self.sent_plan.set()
        return await asyncio.wait_for(self.ack, timeout)

    async def started(self):
        await self.sent_plan.wait()

    async def say(self, mcp, phase, **extra):
        await self.cbs[self.cid]({"command_id": self.cid, "mcp": mcp, "phase": phase,
                                  "pct": 100, **extra})

    def finish(self, results):
        self.ack.set_result({"status": "ok", "results": results})


@pytest.fixture
def slow_machine(monkeypatch):
    """Three MCPs to install (a, b, c), short budgets, a satellite the test
    drives. Returns the satellite."""
    from services.mcp import mcp_sync
    sat = _Satellite()
    names = ("mcp-a", "mcp-b", "mcp-c")

    def _manifest(name):
        if name not in names:
            return None
        m = MagicMock()
        m.label = {"mcp-a": "Alpha", "mcp-b": "Beta", "mcp-c": "Gamma"}[name]
        m.server.runtime = "python"
        m.server.source = "pypi:x@1"
        m.server.source_build = []
        m.category = "custom"
        for f in ("debian", "ubuntu", "rhel", "arch", "macos_brew"):
            setattr(m.system_requirements, f, [])
        m.system_requirements.node_min = ""
        m.system_requirements.notes = ""
        return m

    async def _nothing_installed(cm, machine_id):
        return {}

    tb = MagicMock(tarball_b64="", version_hash="h")
    fake_layer = MagicMock(_sessions={})
    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _nothing_installed)
    monkeypatch.setattr("services.mcp.mcp_registry.get_manifest", _manifest)
    monkeypatch.setattr("services.mcp.mcp_tarball.build_tarball", lambda name: tb)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: sat)
    monkeypatch.setattr("core.remote.remote_execution.get_remote_layer", lambda: fake_layer)
    monkeypatch.setattr(mcp_sync, "_manifest_to_dict", lambda m: {"name": "x"})
    monkeypatch.setattr(mcp_sync, "INSTALL_BUDGET_S", 0.8)
    monkeypatch.setattr(mcp_sync, "VERIFY_BUDGET_S", 0.8)
    mcp_sync._watches.clear()
    yield sat
    mcp_sync._watches.clear()


_PLAN = ["mcp-a", "mcp-b", "mcp-c"]
_ALL_OK = {n: {"status": "ok", "version_hash": "h"} for n in _PLAN}


@pytest.mark.asyncio
async def test_each_mcp_is_judged_by_its_own_budget(slow_machine):
    """The plan takes longer than any one budget, every MCP inside its own:
    all installed, nothing excluded (one ceiling for the plan failed this)."""
    from services.mcp import mcp_sync
    sat = slow_machine

    async def drive():
        await sat.started()
        for name in _PLAN:
            await sat.say(name, "deps")
            await sat.say(f"{name}.new", "done")      # the installer's own line
            await asyncio.sleep(0.5)
            await sat.say(name, "done")
        sat.finish(_ALL_OK)

    driver = asyncio.ensure_future(drive())
    result = await mcp_sync.sync_mcps_for_session("m-1", "s-1", list(_PLAN))
    await driver
    assert sorted(result.installed) == _PLAN and result.ok
    assert not result.excluded_names and not result.still_installing
    # The command's own timeout lies past every budget.
    assert sat.sent[0][1] == pytest.approx(3 * 0.8 + 0.8)
    assert not sat.cbs and not sat.lock.locked() and not mcp_sync._watches


@pytest.mark.asyncio
async def test_an_mcp_past_its_budget_reads_as_failed(slow_machine):
    """The one installing runs past its own budget: it failed, the ones
    queued behind it did not start; the listener and the lock are freed."""
    from services.mcp import mcp_sync
    sat = slow_machine

    async def drive():
        await sat.started()
        await sat.say("mcp-a", "done")
        await sat.say("mcp-b", "deps")      # then nothing more

    driver = asyncio.ensure_future(drive())
    result = await mcp_sync.sync_mcps_for_session("m-1", "s-1", list(_PLAN))
    await driver
    assert result.installed == ["mcp-a"] and not result.ok
    assert result.failed == {"mcp-b": "did not finish installing in 0 minutes",
                             "mcp-c": "did not start installing"}
    assert result.excluded_names == {"mcp-b", "mcp-c"} and not result.still_installing
    assert sat.ack.cancelled() and not sat.cbs and not sat.lock.locked()


@pytest.mark.asyncio
async def test_a_budget_passed_after_the_start_tells_the_chat_nothing_it_does_not_know(slow_machine):
    """Past the early start, the one installing runs past its budget: it
    reads as failed, the one queued behind it never started (unknown), so
    no line claims how it ended."""
    from services.mcp import mcp_sync
    sat = slow_machine
    settled = asyncio.Event()

    async def drive_start():
        await sat.started()
        await sat.say("mcp-a", "done")
        await sat.say("mcp-b", "deps")

    driver = asyncio.ensure_future(drive_start())
    result = await mcp_sync.sync_mcps_for_session("m-1", "s-1", list(_PLAN), early_start_s=0.3)
    await driver
    result.watch.when_settled(lambda w: settled.set())
    await asyncio.wait_for(settled.wait(), 3)
    assert result.watch.outcome(["mcp-b", "mcp-c"]) == ([], ["mcp-b"], ["mcp-c"])
    assert result.watch.result.failed["mcp-c"] == "did not start installing"
    for _ in range(50):
        if not sat.lock.locked():
            break
        await asyncio.sleep(0.01)
    assert not sat.lock.locked() and sat.ack.cancelled()


@pytest.mark.asyncio
async def test_a_failed_install_is_excluded_with_its_reason(slow_machine):
    from services.mcp import mcp_sync
    sat = slow_machine

    async def drive():
        await sat.started()
        await sat.say("mcp-a", "done")
        await sat.say("mcp-b", "failed", error="RuntimeError: pip failed")
        await sat.say("mcp-c", "done")
        sat.finish({**_ALL_OK, "mcp-b": {"status": "error", "error": "RuntimeError: pip failed"}})

    driver = asyncio.ensure_future(drive())
    result = await mcp_sync.sync_mcps_for_session("m-1", "s-1", list(_PLAN), early_start_s=1.0)
    await driver
    assert sorted(result.installed) == ["mcp-a", "mcp-c"]
    assert result.failed == {"mcp-b": "RuntimeError: pip failed"}
    assert result.excluded_names == {"mcp-b"} and not result.still_installing


@pytest.mark.asyncio
async def test_the_session_starts_early_with_the_tools_that_are_ready(slow_machine):
    """Past the early start the session gets what ended; the rest is still
    installing (left out, never a failure). The plan goes on with the lock
    and the listener, the session's progress callback hears no more, and
    the watch tells the chat how it ended."""
    from services.mcp import mcp_sync
    sat = slow_machine
    heard: list = []
    settled = asyncio.Event()

    async def drive_start():
        await sat.started()
        await sat.say("mcp-a", "done")
        await sat.say("mcp-b", "deps")

    driver = asyncio.ensure_future(drive_start())
    result = await mcp_sync.sync_mcps_for_session(
        "m-1", "s-1", list(_PLAN), progress_cb=heard.append, early_start_s=0.4)
    await driver
    assert result.installed == ["mcp-a"]
    assert result.still_installing == {"mcp-b", "mcp-c"}
    assert result.excluded_names == {"mcp-b", "mcp-c"} and not result.failed
    assert mcp_sync.unavailable_reasons(result, _PLAN) == {
        n: "still installing on this machine, a new chat here can use it once it is done"
        for n in ("mcp-b", "mcp-c")}
    # The plan holds the machine until it ends.
    assert sat.lock.locked() and sat.cbs and mcp_sync._watches["m-1"] is result.watch
    result.watch.when_settled(lambda w: settled.set())
    before = len(heard)
    await sat.say("mcp-b", "done")
    await sat.say("mcp-c", "done")
    sat.finish(_ALL_OK)
    await asyncio.wait_for(settled.wait(), 2)
    assert len(heard) == before                       # the session's bar is done
    assert result.watch.outcome(["mcp-b", "mcp-c"]) == (["mcp-b", "mcp-c"], [], [])
    for _ in range(50):
        if not sat.lock.locked():
            break
        await asyncio.sleep(0.01)
    assert not sat.lock.locked() and not sat.cbs and not mcp_sync._watches


@pytest.mark.asyncio
async def test_a_plan_done_before_the_early_start_returns_whole(slow_machine):
    from services.mcp import mcp_sync
    sat = slow_machine

    async def drive():
        await sat.started()
        for name in _PLAN:
            await sat.say(name, "done")
        await sat.say("", mcp_sync.VERIFYING_PHASE)
        sat.finish(_ALL_OK)

    driver = asyncio.ensure_future(drive())
    result = await mcp_sync.sync_mcps_for_session("m-1", "s-1", list(_PLAN), early_start_s=5.0)
    await driver
    assert sorted(result.installed) == _PLAN and not result.still_installing
    assert result.watch is None and not sat.lock.locked()


@pytest.mark.asyncio
async def test_a_start_beside_a_running_plan_installs_nothing(slow_machine, monkeypatch):
    """A second session while the first's plan still holds the machine: it
    waits the same short while for the lock, then starts with what the
    satellite has, the running plan's MCPs still installing."""
    from services.mcp import mcp_sync
    sat = slow_machine

    async def drive_start():
        await sat.started()
        await sat.say("mcp-a", "done")

    driver = asyncio.ensure_future(drive_start())
    first = await mcp_sync.sync_mcps_for_session("m-1", "s-1", list(_PLAN), early_start_s=0.3)
    await driver

    async def _has_a(cm, machine_id):
        return {"mcp-a": {"version_hash": "h", "healthy": True}}

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _has_a)
    monkeypatch.setattr(mcp_sync, "version_hashes_for",
                        AsyncMock(return_value={n: "h" for n in _PLAN}))
    second = await mcp_sync.sync_mcps_for_session("m-1", "s-2", list(_PLAN), early_start_s=0.3)
    assert len(sat.sent) == 1                            # no plan of its own
    assert second.still_installing == {"mcp-b", "mcp-c"} and second.excluded_names == {"mcp-b", "mcp-c"}
    assert second.watch is first.watch
    sat.finish(_ALL_OK)
    for _ in range(100):
        if not sat.lock.locked():
            break
        await asyncio.sleep(0.01)
    assert not sat.lock.locked()


def test_the_lines_name_the_tools_in_words():
    from services.mcp import mcp_sync
    assert mcp_sync.installing_sentence(["Schedules", "Nextcloud", "Blender"], "my laptop") == (
        "Three tools are still installing on my laptop: Schedules, Nextcloud and Blender. "
        "Start a new chat on this machine once they are done to use them.")
    assert mcp_sync.installing_sentence(["Blender"], "my laptop") == (
        "One tool is still installing on my laptop: Blender. Start a new chat on this machine "
        "once it is done to use it.")
    assert mcp_sync.installing_sentence([f"T{i}" for i in range(12)], "rack").startswith(
        "12 tools are still installing on rack: T0, T1")
    assert mcp_sync.finished_sentence(["Schedules", "Nextcloud"], [], "my laptop") == (
        "The tools finished installing on my laptop. Start a new chat on my laptop to use them.")
    assert mcp_sync.finished_sentence(["Schedules"], [], "my laptop") == (
        "The tool finished installing on my laptop. Start a new chat on my laptop to use it.")
    assert mcp_sync.finished_sentence(["Schedules", "Nextcloud"], ["Blender"], "my laptop") == (
        "Blender could not be installed on my laptop. Schedules and Nextcloud finished "
        "installing. Start a new chat on my laptop to use them.")
    assert mcp_sync.finished_sentence([], ["Blender"], "my laptop") == (
        "Blender could not be installed on my laptop.")
    for text in (mcp_sync.installing_sentence(["A", "B"], "x"),
                 mcp_sync.finished_sentence(["A"], ["B"], "x")):
        assert ";" not in text and "—" not in text and " - " not in text


@pytest.mark.asyncio
async def test_the_chat_is_told_at_the_start_and_when_the_tools_end(monkeypatch):
    """One line when the session starts with tools still installing; a
    second when the same plan's watch knows how each one ended; none when it
    never learned (a budget passed, the link dropped)."""
    from core.remote import remote_session_start as rss
    from services.mcp import mcp_sync
    rows: list[tuple[str, str]] = []

    async def _row(chat_id, message):
        rows.append((chat_id, message))

    monkeypatch.setattr(rss, "_tools_row", _row)
    monkeypatch.setattr(mcp_sync, "tool_labels", lambda names: [n.title() for n in names])
    monkeypatch.setattr("storage.database.get_chat",
                        lambda cid: {"id": cid, "execution_target": "m-1"})
    watch = mcp_sync.InstallWatch("m-1", ["blender", "schedules"],
                                  {"blender": 9.0, "schedules": 9.0}, {"blender"})
    await rss._tell_tools_installing("c-1", "my laptop", ["blender", "schedules"], watch)
    assert rows == [("c-1", "Two tools are still installing on my laptop: Blender and Schedules. "
                            "Start a new chat on this machine once they are done to use them.")]
    watch.settle(mcp_sync.SyncResult(installed=["blender"], updated=["schedules"]))
    for _ in range(50):
        if len(rows) == 2:
            break
        await asyncio.sleep(0.01)
    assert rows[1] == ("c-1", "The tools finished installing on my laptop. Start a new chat "
                              "on my laptop to use them.")

    rows.clear()
    unknown = mcp_sync.InstallWatch("m-1", ["blender"], {"blender": 9.0}, {"blender"})
    await rss._tell_tools_installing("c-2", "my laptop", ["blender"], unknown)
    unknown.settle(mcp_sync.SyncResult(ok=False, failed={"__transport__": "link lost"}))
    await asyncio.sleep(0.05)
    assert len(rows) == 1                                # the start line only


@pytest.mark.asyncio
async def test_a_remote_start_leaves_out_the_tools_still_installing(monkeypatch):
    """The remote start asks for the early start, ships the session without
    the tools still installing (its prompt names them), reports none of them
    as failed, ends the install bar without "Install complete" and tells the
    chat on the machine's name. A call starts sooner and writes no line."""
    from auth.path_policy import SecurityContext
    from core import placement
    from core.execution_layer import AgentConfig, RemoteStartPlan
    from core.remote import remote_session_start as rss
    from core.remote.remote_execution import RemoteExecutionLayer
    from services.mcp import mcp_sync

    cm = MagicMock()
    cm.is_connected.return_value = True
    cm.machine_at_capacity.return_value = False
    cm.satellite_engines.return_value = {"claude-code-cli"}
    layer = RemoteExecutionLayer(cm)
    servers = {n: {"command": "python", "args": [f"{n}.py"]} for n in ("memory", "blender", "schedules")}

    async def _payload(session_id, config, execution_path):
        return RemoteStartPlan(payload={"mcp_config": {"mcpServers": dict(servers)},
                                        "system_prompt": "PROMPT"})

    watch = mcp_sync.InstallWatch("m-1", ["blender", "schedules"],
                                  {"blender": 9.0, "schedules": 9.0}, {"blender"})
    seen: dict = {}

    async def _sync(machine_id, session_id, assigned, **kw):
        seen["assigned"], seen["kw"] = sorted(assigned), kw
        await kw["plan_cb"]({"mcps_to_install": ["blender"], "mcps_to_update": ["schedules"]})
        return mcp_sync.SyncResult(updated=["memory"], still_installing={"blender", "schedules"},
                                   excluded_names={"blender", "schedules"}, watch=watch)

    told: list = []

    async def _tell(chat_id, place, installing, w):
        told.append((chat_id, place, installing, w))

    frames: list = []

    async def _emit(machine_id, agent, ev):
        frames.append(ev)

    class _Stop(Exception):
        pass

    async def _gateway(session_id, config, payload, machine_id, plan):
        seen["payload"] = payload
        raise _Stop

    monkeypatch.setattr(layer, "_build_start_payload", _payload)
    monkeypatch.setattr(layer, "_initial_workspace_sync", AsyncMock())
    monkeypatch.setattr(layer, "_provision_gateway", _gateway)
    monkeypatch.setattr(mcp_sync, "sync_mcps_for_session", _sync)
    monkeypatch.setattr(mcp_sync, "lifted_by_context", AsyncMock(return_value=set()))
    monkeypatch.setattr(rss, "_tell_tools_installing", _tell)
    monkeypatch.setattr("core.remote.install_registry.emit", _emit)
    monkeypatch.setattr("storage.remote_store.get_remote_machine",
                        lambda mid: {"id": mid, "pairing_scope": "admin", "capabilities": "{}"})
    ctx = SecurityContext(role="manager", username="", agent="support", is_admin_agent=False,
                          session_scope="agent",
                          placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE,
                                                                   machine_id="m-1",
                                                                   label="my laptop"))
    cfg = AgentConfig(agent_name="support", permission_mode="default", chat_id="c-1",
                      execution_target="m-1", execution_path="claude-code-cli",
                      security_context=ctx, sandbox_host_claude_dir="/tmp/x")
    with pytest.raises(_Stop):
        await layer.start_session("s-early", cfg)
    assert seen["assigned"] == ["blender", "memory", "schedules"]
    assert seen["kw"]["early_start_s"] == mcp_sync.EARLY_START_S
    assert set(seen["payload"]["mcp_config"]["mcpServers"]) == {"memory"}
    assert "still installing on this machine" in seen["payload"]["system_prompt"]
    assert "mcp_install_failed" not in [f["type"] for f in frames]
    done = [f for f in frames if f["type"] == "install_done"]
    assert len(done) == 1 and done[0]["still_installing"] == ["blender", "schedules"]
    assert told == [("c-1", "my laptop", ["blender", "schedules"], watch)]
    # A phone call starts sooner, and nobody reads a line there: none is
    # written, the prompt still names the tools left out.
    from core.session import session_kind
    told.clear()
    cfg.client_type = session_kind.PHONE.name
    with pytest.raises(_Stop):
        await layer.start_session("s-call", cfg)
    assert seen["kw"]["early_start_s"] == mcp_sync.PHONE_EARLY_START_S < mcp_sync.EARLY_START_S
    assert "still installing on this machine" in seen["payload"]["system_prompt"]
    assert told == []
    # A run nobody watches (a task) waits for its whole plan.
    cfg.client_type = session_kind.TASK.name
    with pytest.raises(_Stop):
        await layer.start_session("s-task", cfg)
    assert seen["kw"]["early_start_s"] is None


@pytest.mark.asyncio
async def test_the_tools_line_is_a_system_row_of_the_chat(temp_db, monkeypatch):
    """The line is persisted in the chat (a reload shows it where it was
    written) and sent to the chat's open tabs."""
    import json
    import uuid

    from core.remote import remote_session_start as rss
    from core.session import session_state
    from storage import database as db
    from ws import wire_events as wire
    sent: list = []
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda chat_id, frame: sent.append((chat_id, frame)))
    db.upsert_user("u-row", "u-row@t.io", "u-row", "member", display_name="u-row")
    from tests.fixtures.ws_dashboard_harness import make_test_agent
    cid = str(uuid.uuid4())
    db.create_chat(cid, "u-row", make_test_agent(), "default")
    await rss._tools_row(cid, "One tool is still installing on my laptop: Blender.")
    rows = [r for r in db.get_chat_messages(cid) if r.get("event_type") == wire.SYSTEM]
    block = {"type": wire.SYSTEM, "subtype": wire.SUBTYPE_TOOLS_INSTALLING,
             "message": "One tool is still installing on my laptop: Blender."}
    assert [json.loads(r["event_data"]) for r in rows] == [block]
    assert sent == [(cid, {**block, "chat_id": cid})]


@pytest.mark.asyncio
async def test_a_start_beside_a_plan_promises_only_what_that_plan_installs(slow_machine, monkeypatch):
    """A second session beside a running plan names as still installing only
    what that plan installs; an MCP nobody installs is left out as not
    installed yet (never in the tools line, never promised an end)."""
    from services.mcp import mcp_sync
    sat = slow_machine
    monkeypatch.setattr(mcp_sync, "INSTALL_BUDGET_S", 30.0)   # the plan outlives the test's starts
    driver = asyncio.ensure_future(sat.started())
    first = await mcp_sync.sync_mcps_for_session("m-1", "s-1", ["mcp-a"], early_start_s=0.3)
    await driver
    assert first.still_installing == {"mcp-a"} and sat.lock.locked()
    monkeypatch.setattr(mcp_sync, "version_hashes_for",
                        AsyncMock(return_value={n: "h" for n in _PLAN}))
    second = await mcp_sync.sync_mcps_for_session("m-1", "s-2", ["mcp-a", "mcp-b"],
                                                  early_start_s=0.3)
    assert len(sat.sent) == 1
    assert second.still_installing == {"mcp-a"} and second.not_installed == {"mcp-b"}
    assert second.excluded_names == {"mcp-a", "mcp-b"} and not second.failed
    assert second.watch is first.watch
    assert mcp_sync.unavailable_reasons(second, ["mcp-a", "mcp-b"]) == {
        "mcp-a": "still installing on this machine, a new chat here can use it once it is done",
        "mcp-b": "not installed yet on this machine, a new chat here installs it"}
    # Nothing beside a plan that holds none of its MCPs: no watch to wait on.
    third = await mcp_sync.sync_mcps_for_session("m-1", "s-3", ["mcp-b"], early_start_s=0.3)
    assert third.not_installed == {"mcp-b"} and not third.still_installing
    assert third.watch is None
    sat.finish(_ALL_OK)
    await asyncio.wait_for(_lock_freed(sat), 2)


async def _lock_freed(sat):
    async with sat.lock:      # acquired once the plan's tail lets it go
        pass


@pytest.mark.asyncio
async def test_beside_a_plan_the_memo_and_the_deferral_answer_as_on_the_main_path(
        slow_machine, monkeypatch):
    """A memoized unsatisfiable install reads as that failure (memoized, no
    frame), never as still installing; a recently deferred update keeps the
    old version in the session."""
    from services.mcp import mcp_sync
    sat = slow_machine
    monkeypatch.setattr(mcp_sync, "INSTALL_BUDGET_S", 30.0)
    driver = asyncio.ensure_future(sat.started())
    await mcp_sync.sync_mcps_for_session("m-1", "s-1", ["mcp-a"], early_start_s=0.3)
    await driver

    async def _old_b(cm, machine_id):
        return {"mcp-b": {"version_hash": "old", "healthy": True}}

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _old_b)
    monkeypatch.setattr(mcp_sync, "version_hashes_for",
                        AsyncMock(return_value={n: "h" for n in _PLAN}))
    err = "No solution found when resolving dependencies"
    monkeypatch.setitem(mcp_sync._unsatisfiable_installs, ("m-1", "mcp-c"),
                        {"hash": "h", "error": err, "satellite_version": "0.5.157"})
    mcp_sync._mark_update_deferred("m-1", "mcp-b")
    try:
        second = await mcp_sync.sync_mcps_for_session("m-1", "s-2", ["mcp-a", "mcp-b", "mcp-c"],
                                                      early_start_s=0.3)
    finally:
        mcp_sync._deferred_updates.pop(("m-1", "mcp-b"), None)
    assert second.still_installing == {"mcp-a"} and not second.not_installed
    assert second.failed == {"mcp-c": err} and second.memoized == {"mcp-c"}
    assert second.excluded_names == {"mcp-a", "mcp-c"}          # mcp-b runs its old version
    sat.finish(_ALL_OK)
    await asyncio.wait_for(_lock_freed(sat), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("early", [None, 5.0])
async def test_a_cancelled_start_leaves_nothing_behind(slow_machine, early):
    """The person stops or closes a starting chat mid-plan: the ack, the
    progress listener and the watch go with the start, so no later event of
    the satellite reaches it (nor keeps it marked as starting), and the
    machine's lock is free."""
    from services.mcp import mcp_sync
    sat = slow_machine
    heard: list = []
    start = asyncio.ensure_future(mcp_sync.sync_mcps_for_session(
        "m-1", "s-1", list(_PLAN), progress_cb=heard.append, early_start_s=early))
    await sat.started()
    await sat.say("mcp-a", "done")
    assert heard and sat.cid in sat.cbs and "m-1" in mcp_sync._watches
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert sat.cid not in sat.cbs
    assert "m-1" not in mcp_sync._watches
    assert sat.ack.cancelled() and not sat.lock.locked()


@pytest.mark.asyncio
async def test_a_cancelled_plan_tail_leaves_nothing_behind(slow_machine):
    """The plan that went on after the early start, its tail cancelled (the
    proxy shutting down): the listener, the watch and the ack go too."""
    from core import background
    from services.mcp import mcp_sync
    sat = slow_machine
    driver = asyncio.ensure_future(sat.started())
    first = await mcp_sync.sync_mcps_for_session("m-1", "s-1", list(_PLAN), early_start_s=0.3)
    await driver
    assert first.still_installing and sat.cid in sat.cbs
    tails = [t for t in background.running("mcp-sync") if t.get_name().endswith("plan-m-1")]
    assert len(tails) == 1
    tails[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tails[0]
    assert sat.cid not in sat.cbs and "m-1" not in mcp_sync._watches
    assert sat.ack.cancelled() and not sat.lock.locked()


@pytest.mark.asyncio
async def test_the_chat_is_told_once_per_plan(monkeypatch):
    """A restart, a resume or a warm-up of the same chat during the same plan
    adds no second pair of lines; another chat, or a later plan, is told."""
    from core.remote import remote_session_start as rss
    from services.mcp import mcp_sync
    rows: list[tuple[str, str]] = []

    async def _row(chat_id, message):
        rows.append((chat_id, message))

    monkeypatch.setattr(rss, "_tools_row", _row)
    monkeypatch.setattr(mcp_sync, "tool_labels", lambda names: [n.title() for n in names])
    monkeypatch.setattr("storage.database.get_chat",
                        lambda cid: {"id": cid, "execution_target": "m-1"})
    watch = mcp_sync.InstallWatch("m-1", ["blender"], {"blender": 9.0}, {"blender"})
    for _ in range(3):
        await rss._tell_tools_installing("c-1", "my laptop", ["blender"], watch)
    await rss._tell_tools_installing("c-2", "my laptop", ["blender"], watch)
    assert [c for c, _ in rows] == ["c-1", "c-2"]
    watch.settle(mcp_sync.SyncResult(installed=["blender"]))
    for _ in range(50):
        if len(rows) == 4:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert [c for c, _ in rows[:2]] == ["c-1", "c-2"] and sorted(c for c, _ in rows[2:]) == ["c-1", "c-2"]
    later = mcp_sync.InstallWatch("m-1", ["blender"], {"blender": 9.0}, {"blender"})
    await rss._tell_tools_installing("c-1", "my laptop", ["blender"], later)
    assert len(rows) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("pin,told", [("m-1", True), (None, True), ("local", False),
                                      ("m-2", False)])
async def test_the_finished_line_waits_for_a_chat_still_on_the_machine(monkeypatch, pin, told):
    """The chat moved off the machine mid-install: no finished line (its
    place is no longer there). Still on it, or unpinned, the line names the
    place."""
    from core.remote import remote_session_start as rss
    from services.mcp import mcp_sync
    rows: list[str] = []

    async def _row(chat_id, message):
        rows.append(message)

    monkeypatch.setattr(rss, "_tools_row", _row)
    monkeypatch.setattr(mcp_sync, "tool_labels", lambda names: [n.title() for n in names])
    monkeypatch.setattr("storage.database.get_chat",
                        lambda cid: {"id": cid, "execution_target": pin})
    watch = mcp_sync.InstallWatch("m-1", ["blender"], {"blender": 9.0}, {"blender"})
    await rss._tell_tools_installing("c-1", "my laptop", ["blender"], watch)
    watch.settle(mcp_sync.SyncResult(installed=["blender"]))
    for _ in range(30):
        if len(rows) == 2:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert rows[1:] == (["The tool finished installing on my laptop. Start a new chat on my "
                         "laptop to use it."] if told else [])


def test_each_kind_of_start_waits_its_own_while():
    from core.session import session_kind
    from services.mcp import mcp_sync
    assert mcp_sync.early_start_for(session_kind.DASHBOARD.name) == mcp_sync.EARLY_START_S
    assert mcp_sync.early_start_for(session_kind.PHONE.name) == mcp_sync.PHONE_EARLY_START_S == 45.0
    for kind in (session_kind.TASK, session_kind.MEETING, session_kind.TRIGGER):
        assert mcp_sync.early_start_for(kind.name) is None
