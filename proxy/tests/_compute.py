"""A compute machine for tests that build or start a session of the
platform's place.

The platform runs no session itself: a session resolved to ``local`` is
built on the compute machine that serves (``remote_status.compute_row_for``)
and refused with none. A test that builds through the real builders with no
machine of its own calls ``serve``; one that wants the refusal calls
``serve_none``; one with a connection manager of its own needs neither.
"""

from __future__ import annotations

from core import placement

#: The row the one decision answers in these tests.
COMPUTE = {"id": "compute-0123456789ab", "name": "Compute", "pairing_scope": "compute",
           "satellite_version": "", "compute_address": ""}


def serve(monkeypatch, row: dict | None = None) -> dict:
    """A compute machine serves the platform's place: every builder is
    answered ``row`` (``COMPUTE`` by default). Returns the row."""
    from services.remote import remote_status
    answer = dict(row or COMPUTE)
    monkeypatch.setattr(remote_status, "serving_compute_machine", lambda *a, **k: dict(answer))
    return answer


def serve_none(monkeypatch) -> None:
    """No compute machine is connected: the builders refuse the session."""
    from services.remote import remote_status
    monkeypatch.setattr(remote_status, "serving_compute_machine", lambda *a, **k: None)


def placed(row: dict | None = None) -> placement.PlacementCapabilities:
    """The compute placement of ``row``, as a built config carries it."""
    return placement.from_machine(placement.KIND_COMPUTE, dict(row or COMPUTE))


def connect(monkeypatch, row: dict | None = None) -> str:
    """``serve``, with the machine connected beside the platform and memory
    to spare: a session built on it reserves on its box, which shares the
    platform's budget (one host). Returns the machine's id."""
    import time

    from core.remote.satellite_connection import (
        SatelliteConnection,
        get_connection_manager,
    )
    made = serve(monkeypatch, row)
    conn = SatelliteConnection(machine_id=made["id"], ws=None, compute=True)  # type: ignore[arg-type]
    conn.memory = {"total_bytes": 256 << 30, "available_bytes": 128 << 30,
                   "swap_free_bytes": 0, "capped": False}
    conn.memory_at = time.monotonic()
    monkeypatch.setitem(get_connection_manager()._connections, made["id"], conn)
    return str(made["id"])


def tools_run_here(monkeypatch) -> list[dict]:
    """The relay's stand-in for a test that calls a tool for real: the
    process a compute machine would start is started by the test, with the
    SDK's stdio client, from the argv and the environment the relay was
    given. Returns the opens, each ``{"machine_id", "session_id", "server",
    "argv"}``."""
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    from core.remote import tool_relay
    opened: list[dict] = []

    def _relay(machine_id, session_id, server, *, argv, env, **_kw):
        opened.append({"machine_id": machine_id, "session_id": session_id, "server": server, "argv": list(argv)})
        return stdio_client(StdioServerParameters(command=argv[0], args=list(argv[1:]), env=dict(env)))
    monkeypatch.setattr(tool_relay, "relay_client", _relay)
    return opened


def builds(monkeypatch, *, ok: bool = True, resolved: str = "", log: str = "",
           machine_id: str = "compute-own", install=None, listed: tuple[str, ...] = ()) -> list[dict]:
    """The compute machine that builds an install's tool, answered with no
    machine and no frame (``mcp_sync.build_on_machine``). Returns the builds
    asked for, each ``{"name", "mcp_dir", "manifest", "source"}``.

    ``install`` is an ``install_mcp``-shaped fake
    (``async (mcp_dir, runtime, source, **kw) -> InstallResult``), run in the
    build's place on the platform's folder: for a test that pins what a
    build is given (``source``, ``source_build``, ``progress_cb``) or writes
    what a build leaves. ``listed`` is what the registry answers for the
    package's versions (none: the registry cannot be read, so the version
    the build read back is taken as it is), never the network."""
    from services.mcp import mcp_sync, mcp_updater
    asked: list[dict] = []

    async def _listed(_package):
        return list(listed)
    monkeypatch.setattr(mcp_updater, "_all_npm_versions", _listed)
    monkeypatch.setattr(mcp_updater, "_all_pypi_versions", _listed)

    async def _build(name, mcp_dir, manifest_data, source, *, progress_cb=None, on_wait=None):
        asked.append({"name": name, "mcp_dir": mcp_dir, "manifest": manifest_data, "source": source})
        if install is None:
            return mcp_sync.Built(ok, log, resolved if ok else "", "", machine_id)
        server = manifest_data.get("server") or {}
        r = await install(mcp_dir, server.get("runtime", ""), source, progress_cb=progress_cb,
                          source_build=list(server.get("source_build") or []))
        return mcp_sync.Built(r.ok, r.log, r.resolved_version if r.ok else "", r.version_hash, machine_id)
    monkeypatch.setattr(mcp_sync, "build_on_machine", _build)
    return asked
