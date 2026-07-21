"""Orchestrate MCP install/update/remove on a satellite for a session.

Called from ``RemoteExecutionLayer.start_session`` BEFORE the actual
``start_session`` command. Computes the desired set of MCPs for the
satellite (union over all active sessions plus this agent's assignment),
diffs it against the satellite's reported ``installed_mcps``, and ships a
batched ``sync_mcps`` command with pre-built tarballs for anything to
install or update.

Per-MCP failures are soft — the session still starts with the failed MCP
excluded (surfaced to the caller via ``SyncResult.excluded_names`` so it
can strip those entries from the shipped MCP config). The caller routes
plan + progress events through its own ``plan_cb`` / ``progress_cb``
callbacks; this module is registry-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from services.mcp import mcp_installer, mcp_registry, mcp_tarball

logger = logging.getLogger("claude-proxy.mcp-sync")

# Per-(machine_id, mcp) backoff for updates the satellite DEFERRED because the
# old version was still in use — its running process held the files so the
# in-place swap was blocked (common on Windows; see sync_mcps). Without this we
# would re-ship + rebuild the same update (~70s on the satellite) on EVERY
# session until the holding session closes. In-memory + transient: maps key ->
# monotonic deadline, self-expires, and is cleared once the update lands. The
# companion leak fix (reap a session's MCP children on close) bounds the hold to
# the holder's lifetime, so a short backoff is enough to absorb that window.
_DEFER_BACKOFF_S = 600.0
_deferred_updates: dict[tuple[str, str], float] = {}


def _is_update_deferred(machine_id: str, mcp_name: str) -> bool:
    """True while a recently-deferred update for (machine, mcp) is backed off."""
    deadline = _deferred_updates.get((machine_id, mcp_name))
    if deadline is None:
        return False
    if time.monotonic() >= deadline:
        _deferred_updates.pop((machine_id, mcp_name), None)
        return False
    return True


def _mark_update_deferred(machine_id: str, mcp_name: str) -> None:
    _deferred_updates[(machine_id, mcp_name)] = time.monotonic() + _DEFER_BACKOFF_S


# Per-(machine_id, mcp) memo of DETERMINISTIC install failures — the uv
# resolver declaring the requirements unsatisfiable (e.g. the MCP needs
# Python >= 3.11 and the satellite venv is 3.10). Retrying an unsatisfiable
# resolve on EVERY session start re-ran a doomed ~0.5s install per warmup —
# wasted latency plus an install-bar flash the user reads as "it's installing
# something again". The memo keys on the SHIPPED version_hash: a platform-side
# MCP change (new requirements) invalidates it, a successful install clears
# it, and ``force`` (the dashboard retry button) bypasses + clears it.
# In-memory: a proxy restart grants every memoized MCP one fresh attempt.
# Transient failures (network, file locks, disk) are NEVER memoized.
_UNSAT_FAIL_RE = re.compile(
    r"No solution found when resolving dependencies"
    r"|does not satisfy Python"
)
_unsatisfiable_installs: dict[tuple[str, str], dict] = {}

# Callback signature: proxy-side mcp_sync calls this with each progress
# event received from the satellite. Used by the pump to surface progress
# as SYSTEM CommonEvents in the chat.
ProgressCb = Callable[[dict], Awaitable[None]] | Callable[[dict], None] | None


@dataclass
class SyncResult:
    """What happened during a sync_mcps round."""
    ok: bool = True
    installed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    # MCPs excluded from this session due to install failures. The caller
    # should strip these from the MCP config it ships to the satellite.
    excluded_names: set[str] = field(default_factory=set)
    # Subset of excluded_names that were skipped from a MEMOIZED deterministic
    # failure (no install was attempted this session) — the caller must not
    # emit per-session mcp_install_failed frames for these: a lone failure
    # frame outside a started/done lifecycle would seed a ghost install bar.
    memoized: set[str] = field(default_factory=set)
    # MCPs that installed fine but failed the post-install pre-warm boot
    # check: name -> reason. Advisory only — these are NOT excluded
    # (a missing startup credential isn't a real failure; the real session
    # env supplies it). Surfaced so the caller can log + show a UI hint.
    warmup_failed: dict[str, str] = field(default_factory=dict)
    # MCPs whose UPDATE the satellite deferred because the old version was still
    # in use (swap blocked). NOT excluded — the session uses the working old
    # version; the update lands once the holder closes. name -> reason.
    deferred: dict[str, str] = field(default_factory=dict)


async def sync_mcps_for_session(
    machine_id: str,
    session_id: str,
    agent_assigned_mcps: list[str],
    *,
    plan_cb: Callable[[dict], Awaitable[None]] | None = None,
    progress_cb: ProgressCb = None,
    force: bool = False,
) -> SyncResult:
    """Align the satellite's installed MCPs with what this session needs.

    ``agent_assigned_mcps`` is the set of MCP names this session requires.
    The desired set is the union of these with MCPs already in use by any
    active session on the same satellite (prevents premature uninstall of
    an MCP still bound to an older session).

    ``plan_cb`` (optional) is invoked once the install plan is known
    (``mcps_to_install`` / ``mcps_to_update`` resolved) so the caller can
    surface the per-MCP rows in any progress UI before the first satellite
    progress event arrives. Caller-owned: this module knows nothing about
    chat_id / install_registry routing.

    Returns SyncResult detailing installs/updates/removes/failures. On any
    failed install, the MCP name is added to ``excluded_names``; callers
    should filter it from the shipped MCP config.
    """
    from core.remote.remote_execution import get_remote_layer
    from core.remote.satellite_connection import get_connection_manager

    cm = get_connection_manager()
    if not cm.is_connected(machine_id):
        return SyncResult(ok=False, failed={"__connection__": "machine not connected"})

    layer = get_remote_layer()
    # Desired set = this agent's assignment ∪ every other active session's
    # MCPs on the same satellite. Prevents uninstalling an MCP a peer
    # session still depends on (scope-aware GC).
    desired: set[str] = set(agent_assigned_mcps)
    for other_sid, other_info in layer._sessions.items():
        if other_sid == session_id:
            continue
        if other_info.machine_id != machine_id:
            continue
        desired.update(getattr(other_info, "used_mcps", set()) or set())

    async with cm.get_install_lock(machine_id):
        # Compute satellite state. Fresh verify on every sync to avoid
        # acting on stale capability reports (the heartbeat capability
        # doesn't refresh on each install — a reconnect might though).
        installed = await _fetch_satellite_state(cm, machine_id)

        to_install, to_update, to_remove = _diff(
            desired=desired,
            installed=installed,
            force=force,
        )

        # Drop updates the satellite recently DEFERRED (old version still in use
        # → swap blocked). Re-shipping would rebuild ~70s on the satellite every
        # session for nothing; the session keeps using the working old version.
        # ``force`` overrides; the backoff self-expires and is cleared on a
        # successful install below.
        if to_update and not force:
            deferred_now = {n for n in to_update if _is_update_deferred(machine_id, n)}
            if deferred_now:
                logger.info(
                    "sync_mcps: skipping %d deferred update(s) for %s (in use): %s",
                    len(deferred_now), machine_id[:8], sorted(deferred_now),
                )
                to_update -= deferred_now

        # Memoized deterministic failures: don't re-attempt an install the
        # resolver already declared unsatisfiable for the SAME platform bytes.
        # ``force`` (the retry button) clears the memo and attempts anyway.
        memo_skips: dict[str, str] = {}
        _conn = cm.get_connection(machine_id)
        _sat_ver = getattr(_conn, "satellite_version", "") or ""
        for name in sorted(to_install | to_update):
            memo = _unsatisfiable_installs.get((machine_id, name))
            if memo is None:
                continue
            if force:
                _unsatisfiable_installs.pop((machine_id, name), None)
                continue
            if memo.get("satellite_version") != _sat_ver:
                # The satellite self-updated since the failure — installer
                # behavior may have changed (e.g. a new python-floor retry) —
                # the verdict deserves a fresh attempt.
                _unsatisfiable_installs.pop((machine_id, name), None)
                continue
            manifest = mcp_registry.get_manifest(name)
            try:
                current_hash = (
                    mcp_installer.compute_version_hash(manifest.mcp_dir)
                    if manifest else ""
                )
            except Exception:
                current_hash = ""
            if current_hash and current_hash == memo.get("hash"):
                memo_skips[name] = memo.get("error", "install unsatisfiable")
            else:
                # Platform-side MCP changed — the verdict may differ now.
                _unsatisfiable_installs.pop((machine_id, name), None)
        if memo_skips:
            to_install -= set(memo_skips)
            to_update -= set(memo_skips)
            logger.info(
                "sync_mcps: skipping %d memoized unsatisfiable install(s) "
                "for %s: %s", len(memo_skips), machine_id[:8],
                sorted(memo_skips),
            )

        def _with_memo_skips(r: SyncResult) -> SyncResult:
            for name, err in memo_skips.items():
                r.failed[name] = err
                r.excluded_names.add(name)
                r.memoized.add(name)
            return r

        if not (to_install or to_update or to_remove):
            return _with_memo_skips(SyncResult(ok=True))

        # Announce the install plan upfront so the caller's UI can render
        # per-MCP rows even before the first progress event fires.
        if plan_cb and (to_install or to_update):
            await plan_cb({
                "mcps_to_install": sorted(to_install),
                "mcps_to_update": sorted(to_update),
            })

        # Build tarballs for install + update. Docker MCPs stay on the
        # platform; don't ship those.
        specs = []
        shipped_hashes: dict[str, str] = {}
        for name in sorted(to_install | to_update):
            manifest = mcp_registry.get_manifest(name)
            if manifest is None:
                logger.warning("sync_mcps: unknown MCP in desired set: %s", name)
                continue
            if manifest.server.runtime in ("docker", "none"):
                # Docker MCPs live on the platform host; context-only MCPs
                # (runtime "none") have no server to install anywhere. Skip
                # satellite install for both.
                continue
            try:
                tb = mcp_tarball.build_tarball(name)
            except Exception as e:
                logger.exception("tarball build failed for %s: %s", name, e)
                continue
            shipped_hashes[name] = tb.version_hash
            specs.append({
                "name": name,
                "category": manifest.category,
                "runtime": manifest.server.runtime,
                "source": manifest.server.source,
                "manifest_data": _manifest_to_dict(manifest),
                "tarball_b64": tb.tarball_b64,
                "version_hash": tb.version_hash,
                # Optional: system_requirements forwarded so the satellite
                # installer runs its pre-install checks.
                "system_requirements": {
                    "debian": manifest.system_requirements.debian,
                    "ubuntu": manifest.system_requirements.ubuntu,
                    "rhel": manifest.system_requirements.rhel,
                    "arch": manifest.system_requirements.arch,
                    "macos_brew": manifest.system_requirements.macos_brew,
                    "node_min": manifest.system_requirements.node_min,
                    "notes": manifest.system_requirements.notes,
                },
            })

        command_id = str(uuid.uuid4())

        async def _progress_forwarder(ev: dict):
            logger.debug(
                "mcp_sync._progress_forwarder: cmd=%s mcp=%s phase=%s pct=%s cb=%s",
                command_id[:8], ev.get("mcp", "-"), ev.get("phase", "-"),
                ev.get("pct", "-"), "set" if progress_cb else "NONE",
            )
            if progress_cb is None:
                return
            r = progress_cb(ev)
            if asyncio.iscoroutine(r):
                await r

        cm.register_install_progress(command_id, _progress_forwarder)
        logger.info("mcp_sync: registered progress cb for command_id=%s", command_id[:8])
        try:
            # Critical: pass command_id as the kwarg, NOT just in the msg
            # dict. ``SatelliteConnectionManager.send_command`` overwrites
            # ``msg["command_id"]`` with the kwarg value (or generates a
            # fresh UUID if the kwarg is None). Without this, the
            # `register_install_progress` above keyed on `command_id`, but
            # the satellite would receive a different generated id and emit
            # `mcp_install_progress` events with that one — leaving the
            # callback orphaned and the dashboard install bar stuck at 0%.
            ack = await cm.send_command(machine_id, {
                "type": "sync_mcps",
                "mcps_to_install": specs,
                "mcps_to_remove": sorted(to_remove),
            }, timeout=600.0, command_id=command_id)  # 10 min ceiling for slow pip builds
        except Exception as e:
            logger.exception("sync_mcps command failed: %s", e)
            cm.unregister_install_progress(command_id)
            return _with_memo_skips(
                SyncResult(ok=False, failed={"__transport__": str(e)})
            )
        finally:
            cm.unregister_install_progress(command_id)

        results = ack.get("results", {}) or {}
        result = SyncResult()
        for name, info in results.items():
            status = info.get("status", "error")
            if status == "ok":
                # Update landed — clear any deferral backoff for this MCP.
                _deferred_updates.pop((machine_id, name), None)
                _unsatisfiable_installs.pop((machine_id, name), None)
                if name in to_install:
                    result.installed.append(name)
                else:
                    result.updated.append(name)
            elif status == "deferred":
                # Update couldn't swap (old version in use); the satellite kept
                # the working old version. NOT a failure — do not exclude. Back
                # off so we don't re-ship + rebuild it every session until the
                # holding session closes.
                result.deferred[name] = info.get("error", "")
                _mark_update_deferred(machine_id, name)
            elif status == "removed":
                result.removed.append(name)
            elif status == "not_found":
                # Nothing to remove — not a failure.
                pass
            else:
                err = info.get("error", "unknown")
                result.failed[name] = err
                result.excluded_names.add(name)
                # A resolver-unsatisfiable failure is DETERMINISTIC for these
                # platform bytes on this machine — memo it so every later
                # session skips the doomed re-attempt (and its bar flash)
                # until the MCP changes, an install succeeds, or force retry.
                if _UNSAT_FAIL_RE.search(err) and shipped_hashes.get(name):
                    _unsatisfiable_installs[(machine_id, name)] = {
                        "hash": shipped_hashes[name], "error": err,
                        "satellite_version": _sat_ver,
                    }
            # Pre-warm boot check. "warn:<reason>" = installed but
            # didn't answer initialize in time. Advisory — not excluded.
            warm = info.get("warmup", "")
            if isinstance(warm, str) and warm.startswith("warn:"):
                result.warmup_failed[name] = warm[len("warn:"):]
        return _with_memo_skips(result)


async def _fetch_satellite_state(cm, machine_id: str) -> dict[str, dict]:
    """Run sync_mcps_verify and return {mcp_name: {version_hash, healthy}}."""
    try:
        ack = await cm.send_command(machine_id, {
            "type": "sync_mcps_verify",
        }, timeout=15.0)
        return ack.get("mcps", {}) or {}
    except Exception as e:
        logger.warning("sync_mcps_verify failed: %s", e)
        # Fall back to the last-reported capabilities — better than nothing.
        conn = cm.get_connection(machine_id)
        if conn:
            caps_installed = conn.capabilities.get("installed_mcps", [])
            return {name: {"version_hash": "", "healthy": True} for name in caps_installed}
        return {}


def _diff(
    desired: set[str], installed: dict[str, dict], *, force: bool = False,
) -> tuple[set[str], set[str], set[str]]:
    """Split desired vs installed into (install, update, remove).

    install: in desired but not installed (or unhealthy)
    update:  in desired, installed, but version_hash differs from current
    remove:  installed but not desired
    """
    to_install: set[str] = set()
    to_update: set[str] = set()
    to_remove: set[str] = set()

    for name in desired:
        info = installed.get(name)
        manifest = mcp_registry.get_manifest(name)
        if manifest is None:
            continue
        if manifest.server.runtime in ("docker", "none"):
            # Docker = platform-hosted; "none" = context-only (no server).
            # Neither ever installs on a satellite.
            continue
        if info is None or not info.get("healthy", False):
            to_install.add(name)
            continue
        # Compare version_hash against what the platform has now.
        try:
            current_hash = mcp_installer.compute_version_hash(manifest.mcp_dir)
        except Exception:
            current_hash = ""
        if force or (current_hash and current_hash != info.get("version_hash", "")):
            to_update.add(name)

    for name in installed:
        if name not in desired:
            to_remove.add(name)

    return to_install, to_update, to_remove


def _manifest_to_dict(manifest) -> dict:
    """Minimal serialization of a manifest for the satellite.

    The satellite doesn't re-parse the full manifest; it just needs the
    basics so future versioning can evolve without a breaking schema
    change. For now we ship name, server_name, category, version — the
    rest is already baked into the tarball.
    """
    return {
        "name": manifest.name,
        "server_name": manifest.server_name,
        "category": manifest.category,
        "version": manifest.version,
    }
