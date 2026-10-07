"""Shared MCP update detection + single-MCP update.

Extracted from the admin endpoints in ``api/mcp/mcps.py`` so that BOTH the manual
"Check Updates / Update" buttons AND the weekly automatic-update job
(``services/mcp/mcp_autoupdate.py``) drive the exact same code path — there is one
detection function and one update function, not two copies that can drift.

Tier behaviour (resolved by ``core/config/deployment.current_mode()``) lives in
``_update_docker_mcp``: T1 rebuilds + recreates the container, T2 pulls the new
pre-built image + recreates, T3 (cloud / external-pool) only refreshes the
catalog files (the central pool owns the image lifecycle). npm/pypi MCPs converge
to the catalog folder (``_update_node_python_mcp`` → ``install_from_catalog``) and
install the latest version within the catalog ``version_constraint``, pinning the
resolved version into the local manifest.

Concurrency: ``update_one`` holds the per-MCP install lock
(``core/credentials/catalog_install_registry.lock_for``) for the whole update,
so it can't race a manual install/update of the same MCP (those paths hold the
same lock). The lock is NOT acquired inside
``community_installer.install_from_catalog`` — its callers own it — so taking
it here cannot self-deadlock.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import HTTPException

from services.mcp import mcp_manifest_types as _mt
from services.mcp import mcp_registry
from storage import database as task_store
from storage.mcp import mcp_store

logger = logging.getLogger("claude-proxy.mcp-updater")


# ---------------------------------------------------------------------------
# Setting
# ---------------------------------------------------------------------------

def auto_update_enabled() -> bool:
    """Weekly automatic MCP updates — default ON (unset/empty = "1" semantics,
    mirrors ``session_retention_enabled``). On cloud the operator can pin it via
    ``OTODOCK_FORCED_SETTINGS`` (``get_platform_setting`` applies that overlay)."""
    return task_store.get_platform_setting("mcp_auto_update_enabled") != "0"


# ---------------------------------------------------------------------------
# Local hold marker (out-of-band deploys)
# ---------------------------------------------------------------------------

# Presence of this file in an MCP's install dir excludes it from the WEEKLY
# automatic converge. Manual admin updates ignore it — an explicit Update
# click is an explicit decision. An install AHEAD of the catalog needs no
# marker: the job leaves it alone (``_catalog_relation``); the marker is for
# an install the operator does not want converged for a catalog edit at all.
HOLD_MARKER = ".hold"


def is_held(manifest) -> bool:
    """Whether this MCP's install dir carries the auto-update hold marker."""
    mcp_dir = getattr(manifest, "mcp_dir", None)
    if not mcp_dir:
        return False
    try:
        return (Path(mcp_dir) / HOLD_MARKER).exists()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Local-manifest pinning
# ---------------------------------------------------------------------------

def pin_local_manifest(
    mcp_dir: Path, registry: str, package: str, resolved_version: str,
) -> None:
    """Pin a node/python install's LOCAL manifest to the resolved version.

    node/python community MCPs are unpinned in the catalog (``npm:pkg`` /
    ``pypi:pkg``, ``version: ""``); their version of record is the upstream
    registry. After install/update the proxy resolves the concrete version and
    writes it into BOTH ``manifest.version`` and ``manifest.server.source``
    (``{registry}:{package}@{resolved_version}``) so detection and display are
    deterministic and a remote satellite reinstalls the exact same version
    (the satellite syncs this pinned manifest and never resolves "latest"
    itself). Proxy-side only — the satellite receives an already-pinned manifest.

    No-op-safe: callers pass a non-empty ``resolved_version`` for npm/pypi only;
    docker/git+ manifests are never passed here.
    """
    manifest_path = mcp_dir / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["version"] = resolved_version
    data.setdefault("server", {})["source"] = f"{registry}:{package}@{resolved_version}"
    manifest_path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Registry version probes (npm / pypi)
# ---------------------------------------------------------------------------

async def _check_npm_version(package: str) -> str | None:
    """Query the npm registry for the latest version. None on any error."""
    import urllib.request
    try:
        url = f"https://registry.npmjs.org/{package}/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        return json.loads(resp.read()).get("version")
    except Exception:
        return None


async def _check_pypi_version(package: str) -> str | None:
    """Query PyPI for the latest version. None on any error."""
    import urllib.request
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        return json.loads(resp.read()).get("info", {}).get("version")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Bounded version resolution — "latest version within the catalog bound"
# ---------------------------------------------------------------------------
#
# Proxy-only (NOT vendored into the satellite — the satellite installs an
# already-pinned exact version and never resolves a bound). When a community
# node/python MCP declares ``server.version_constraint``, auto-update tracks the
# latest version *within* that bound instead of the absolute latest, so an
# upstream major can't auto-apply until a contributor widens the bound. With no
# constraint we keep the cheap ``/latest`` fast path.


def _is_stable_in_bound(registry: str, version: str, spec) -> bool:
    """True if ``version`` is a usable stable release inside ``spec``.

    Skips prereleases. npm marks prereleases with a ``-`` suffix (``2.0.0-rc.1``,
    and the numeric ``2.0.0-0`` which PEP 440 mis-reads as a *post* release ordered
    ABOVE the stable ``2.0.0``) — so for npm we treat any ``-`` as a prerelease.
    """
    from packaging.version import InvalidVersion, Version
    if registry == "npm" and "-" in version:
        return False
    try:
        v = Version(version)
    except InvalidVersion:
        return False
    if v.is_prerelease:
        return False
    return spec.contains(v)


async def _all_npm_versions(package: str) -> list[str]:
    """All published version strings for an npm package (abbreviated metadata —
    ~3x smaller than the full packument, same ``versions`` map)."""
    import urllib.request
    try:
        url = f"https://registry.npmjs.org/{package}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.npm.install-v1+json"},
        )
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        return list((json.loads(resp.read()).get("versions") or {}).keys())
    except Exception:
        return []


async def _all_pypi_versions(package: str) -> list[str]:
    """All PyPI release versions that have at least one (non-yanked) uploaded file.

    A ``releases`` key with an empty file list is registered-but-not-uploaded and
    isn't installable, so it must not be selected as the bounded latest."""
    import urllib.request
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        releases = json.loads(resp.read()).get("releases") or {}
        return [
            v for v, files in releases.items()
            if files and not all(f.get("yanked") for f in files)
        ]
    except Exception:
        return []


async def resolve_latest_in_bound(
    registry: str, package: str, constraint: str | None,
) -> str | None:
    """Latest published stable version of ``package`` within ``constraint``.

    No constraint → the cheap ``/latest`` fast path (no full version-list fetch).
    Otherwise enumerate all published versions, drop prereleases + (pypi) unuploaded
    releases, keep those satisfying the PEP 440 ``constraint``, and return the max.
    Returns ``None`` when nothing published satisfies the bound (caller decides
    whether that's "no update" or an error).
    """
    if not constraint:
        if registry == "npm":
            return await _check_npm_version(package)
        return await _check_pypi_version(package)

    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import Version
    try:
        spec = SpecifierSet(constraint)
    except InvalidSpecifier:
        # Defensive — _parse_manifest already validates + blanks bad constraints,
        # so a non-empty constraint here is expected valid. Treat as unbounded.
        logger.warning("Invalid version_constraint %r for %s — ignoring", constraint, package)
        if registry == "npm":
            return await _check_npm_version(package)
        return await _check_pypi_version(package)

    versions = await (_all_npm_versions(package) if registry == "npm" else _all_pypi_versions(package))
    usable = [v for v in versions if _is_stable_in_bound(registry, v, spec)]
    if not usable:
        return None
    return str(max(usable, key=Version))


# ---------------------------------------------------------------------------
# Detection — "what updates are available?"
# ---------------------------------------------------------------------------

def _is_pkg_newer(target: str | None, current: str) -> bool:
    """Whether a resolved ``target`` version is a genuine forward update of the
    installed ``current``. An empty/unparseable ``current`` (e.g. a prior install
    whose readback missed and left ``version: ""``) flags True so the update
    re-pins it. Never flags a downgrade."""
    if not target:
        return False
    if not current:
        return True
    try:
        from packaging.version import Version
        return Version(target) > Version(current)
    except Exception:
        return target != current


def _is_downgrade(target: str | None, current: str) -> bool:
    """Whether converging to ``target`` would move the installed ``current``
    BACKWARDS. Parse-only: unparseable versions never flag (a false positive
    would hide a real update behind "ahead")."""
    if not target or not current:
        return False
    try:
        from packaging.version import Version
        return Version(target) < Version(current)
    except Exception:
        return False


REL_UPDATE = "update"
REL_AHEAD = "ahead"
REL_SAME = "same"


def _catalog_relation(latest: str | None, current: str) -> str:
    """How the catalog's version of record stands to the installed one, for
    the axes that converge to the catalog (a container's image tag, a skill
    package): ``update`` when the catalog is newer (an empty installed
    version counts as older, so a missed readback is re-pinned; two
    different strings that do not both parse count as an update too, as the
    old inequality did), ``ahead`` when the install is past the catalog (an
    out-of-band deploy, a newer zip), ``same`` otherwise."""
    if not latest:
        return REL_SAME
    if not current:
        return REL_UPDATE
    if latest == current:
        return REL_SAME
    if _is_downgrade(latest, current):
        return REL_AHEAD
    return REL_UPDATE


async def detect_available_updates() -> dict:
    """Check npm/pypi + the OtoDock community catalog for available MCP updates.

    Returns ``{"updates": {name: {current, latest, registry, package, reason}},
    "checked": N}`` where ``reason`` ∈ ``package`` / ``manifest`` / ``both``.

    Two axes for node/python community MCPs:
      * **package** — a newer version published within the catalog
        ``version_constraint`` (absolute latest when unbounded);
      * **manifest** — the catalog integration manifest changed (``manifest_hash``
        differs from the installed one), so the install should converge.
    Docker MCPs compare the installed version against the catalog version tag.
    The catalog ``registry.json`` is fetched ONCE up front; on failure both
    bound + manifest signals degrade off (the npm/pypi package probe still runs),
    so a transient outage never breaks the caller.
    """
    from services.community import community_catalog
    from services.mcp import mcp_installer

    from services.community import mcp_source_swap

    manifests = mcp_registry.get_all_manifests()
    results: dict[str, dict] = {}

    registry_ok = True
    try:
        registry_doc = await community_catalog.fetch_registry()
        catalog = {e.get("name"): e for e in registry_doc.get("mcps", []) if isinstance(e, dict)}
    except Exception as e:
        logger.warning("Catalog registry fetch failed for update check: %s", e)
        catalog = {}
        registry_ok = False

    # The source pass runs first: an MCP whose catalog source differs from
    # the installed one gets a pending source change (persisted below, shown
    # on the page until the admin switches it or the catalog reverts) and
    # no ordinary update, which the install gate would refuse anyway.
    source_changes: dict[str, dict] = {}
    unchanged: set[str] = set()
    if registry_ok:
        try:
            source_changes, unchanged = await mcp_source_swap.detect_changes(manifests, catalog)
        except Exception:
            logger.exception("Source-change detection failed; ordinary axes only")

    # npm/pypi candidates — COMMUNITY only. Update execution converges to the
    # community-catalog folder (install_from_catalog), which custom/local MCPs
    # are not in: they ship with the platform and update via platform releases.
    # Without this gate the dashboard offered updates the endpoint would 404 on
    # (e.g. the browser-control wrapper tracking @playwright/mcp upstream).
    # Mirrors the weekly job's community_targets() filter.
    checks = []
    for name, m in manifests.items():
        if m.category != "community" or name in source_changes:
            continue
        parsed = mcp_installer.parse_source(m.server.source)
        if not parsed or parsed.registry not in ("npm", "pypi"):
            continue
        checks.append((name, m, parsed.registry, parsed.package))

    # Installed manifest hashes (raw-file reads) computed off the event loop — the
    # in-memory dataclass is lossy, so the hash must come from the manifest.json file.
    def _installed_hashes() -> dict:
        out: dict[str, str | None] = {}
        for cname, cm, _reg, _pkg in checks:
            try:
                data = json.loads((Path(cm.mcp_dir) / "manifest.json").read_text())
                out[cname] = community_catalog.normalized_manifest_hash(data)
            except Exception:
                out[cname] = None
        return out
    installed_hashes = await asyncio.to_thread(_installed_hashes)

    async def _check_one(mcp_name, m, registry, package):
        entry = catalog.get(mcp_name)
        # Bound comes from the catalog (source of truth); fall back to the local
        # manifest only when the catalog entry is unavailable (fetch failed).
        if entry is not None:
            constraint = entry.get("version_constraint", "") or ""
        else:
            constraint = getattr(m.server, "version_constraint", "") or ""
        target = await resolve_latest_in_bound(registry, package, constraint)
        pkg_newer = _is_pkg_newer(target, m.version)
        catalog_hash = entry.get("manifest_hash") if entry else None
        installed_hash = installed_hashes.get(mcp_name)
        manifest_changed = bool(catalog_hash) and bool(installed_hash) and installed_hash != catalog_hash
        if not pkg_newer and not manifest_changed:
            return None
        reason = "both" if (pkg_newer and manifest_changed) else ("package" if pkg_newer else "manifest")
        return (mcp_name, {
            "current": m.version,
            "latest": target if pkg_newer else m.version,
            "registry": registry, "package": package, "reason": reason,
        })

    for result in await asyncio.gather(
        *[_check_one(*c) for c in checks], return_exceptions=True,
    ):
        if result and not isinstance(result, Exception):
            mcp_name, info = result
            results[mcp_name] = info

    # Docker MCPs — compare the installed version against the catalog version
    # tag, then the integration manifest (a catalog edit under the same image
    # tag: the converge re-fetches the folder and recreates the container).
    # Community only, same rationale as above (a custom docker MCP —
    # file-tools — updates via platform releases, and a name collision with a
    # catalog entry must never produce a converge offer for it).
    docker_targets = [
        (name, m) for name, m in manifests.items()
        if m.category == "community" and _mt.is_container(m.server)
        and name not in source_changes
    ]

    def _docker_hashes() -> dict:
        out: dict[str, str | None] = {}
        for dname, dm in docker_targets:
            try:
                data = json.loads((Path(dm.mcp_dir) / "manifest.json").read_text())
                out[dname] = community_catalog.normalized_manifest_hash(data)
            except Exception:
                out[dname] = None
        return out
    docker_hashes = await asyncio.to_thread(_docker_hashes) if docker_targets else {}

    docker_checked = 0
    for name, m in docker_targets:
        docker_checked += 1
        entry = catalog.get(name)
        latest = entry.get("version") if entry else None
        catalog_hash = entry.get("manifest_hash") if entry else None
        installed_hash = docker_hashes.get(name)
        relation = _catalog_relation(latest, m.version)
        if relation == REL_SAME and catalog_hash and installed_hash and catalog_hash != installed_hash:
            results[name] = {
                "current": m.version, "latest": m.version,
                "registry": "catalog", "package": name, "reason": "manifest",
            }
        elif relation == REL_UPDATE:
            results[name] = {
                "current": m.version, "latest": latest,
                "registry": "catalog", "package": name, "reason": "package",
            }
        elif relation == REL_AHEAD:
            # An install past the catalog (an out-of-band deploy, a newer
            # archive) is never moved back by the job; the row reports it and
            # the admin may revert it on purpose through the Update route.
            results[name] = {
                "current": m.version, "latest": latest,
                "registry": "catalog", "package": name, "reason": "ahead",
            }
            logger.info(
                "MCP %s: installed %s is ahead of the catalog %s; left alone",
                name, m.version, latest,
            )

    # Standalone skill packages (category "skill", runtime none) — the skills
    # catalog's version moves an install forward only (``_catalog_relation``,
    # as for a container), plus the manifest-hash axis. Registry fetch
    # degrades to an empty catalog, so a transient outage simply reports no
    # skill updates.
    skills_checked = 0
    try:
        skills_doc = await community_catalog.fetch_skills_registry()
        skills_catalog = {
            e.get("name"): e for e in skills_doc.get("skills", [])
            if isinstance(e, dict)
        }
    except Exception as e:
        logger.warning("Skills registry fetch failed for update check: %s", e)
        skills_catalog = {}
    if skills_catalog:
        def _skill_hashes() -> dict:
            out: dict[str, str | None] = {}
            for sname, sm in manifests.items():
                if sm.category != "skill":
                    continue
                try:
                    data = json.loads((Path(sm.mcp_dir) / "manifest.json").read_text())
                    out[sname] = community_catalog.normalized_manifest_hash(data)
                except Exception:
                    out[sname] = None
            return out
        skill_hashes = await asyncio.to_thread(_skill_hashes)
        for name, m in manifests.items():
            if m.category != "skill":
                continue
            skills_checked += 1
            entry = skills_catalog.get(name)
            if entry is None:
                continue
            latest = entry.get("version")
            relation = _catalog_relation(latest, m.version)
            if relation == REL_AHEAD:
                results[name] = {
                    "current": m.version, "latest": latest,
                    "registry": "skills-catalog", "package": name, "reason": "ahead",
                }
                logger.info(
                    "Skill package %s: installed %s is ahead of the catalog %s; "
                    "left alone", name, m.version, latest,
                )
                continue
            pkg_newer = relation == REL_UPDATE
            catalog_hash = entry.get("manifest_hash")
            installed_hash = skill_hashes.get(name)
            manifest_changed = (bool(catalog_hash) and bool(installed_hash)
                                and installed_hash != catalog_hash)
            if not pkg_newer and not manifest_changed:
                continue
            reason = ("both" if (pkg_newer and manifest_changed)
                      else ("package" if pkg_newer else "manifest"))
            results[name] = {
                "current": m.version,
                "latest": latest if pkg_newer else m.version,
                "registry": "skills-catalog", "package": name, "reason": reason,
            }

    checked = len(checks) + docker_checked + skills_checked + len(source_changes)
    state = await _persist_check(results, source_changes, unchanged, registry_ok)
    if state is None:
        # The store is unavailable: answer with what this check found.
        versions = {name: m.version for name, m in manifests.items()}
        rows = [{"mcp_name": n, "status": "pending", **c} for n, c in source_changes.items()]
        return {
            "updates": mcp_source_swap.merge_update_state(results, rows, versions),
            "checked": checked, "checked_at": "",
        }
    state["checked"] = checked
    return state


async def _persist_check(results: dict, source_changes: dict, unchanged: set,
                         registry_ok: bool) -> dict | None:
    """Write the check's results and source changes, then read the page's
    state back. The ordinary results are replaced whole on every check; a
    source change is upserted (the same pair keeps its row); a pending row
    whose MCP agrees with the catalog again, or whose entry left the
    catalog, is dropped. A check that could not read the catalog judged only
    the npm/pypi axes: it persists nothing (the previous check stands, its
    time unchanged) and answers with what it saw over the persisted source
    changes. ``None`` when the store failed, so a check never fails on
    persistence."""
    from services.community import mcp_source_swap
    from storage.mcp import mcp_update_state_store as state_store

    def _write() -> dict:
        if not registry_ok:
            rows = state_store.list_source_changes()
            versions = {n: m.version for n, m in mcp_registry.get_all_manifests().items()}
            return {
                "updates": mcp_source_swap.merge_update_state(results, rows, versions),
                "checked": len(results),
                "checked_at": state_store.last_checked_at(),
            }
        state_store.replace_check_results(results)
        for name, change in source_changes.items():
            state_store.upsert_pending_source_change(name, change)
        for name in unchanged:
            state_store.delete_pending_source_change(name)
        return mcp_source_swap.build_update_state()

    try:
        return await asyncio.to_thread(_write)
    except Exception:
        logger.exception("The update check could not be persisted")
        return None


# ---------------------------------------------------------------------------
# Execution — update one MCP (docker / npm / pypi), tier-aware
# ---------------------------------------------------------------------------

async def _update_docker_mcp(name: str) -> dict:
    """Update a Docker MCP from the OtoDock community catalog.

    Docker MCPs run a pre-built image whose version of record is the catalog's
    manifest + ``server.image`` tag. ``install_from_catalog`` re-fetches the
    manifest + compose and, on T2, pulls the (retagged/rebuilt) image and
    force-recreates the container. Two topology fix-ups on top:
      * T1 bare-metal — the catalog install only replaces files (auto-start is
        T2-only), so rebuild + recreate the container here.
      * T3 cloud (external-pool) — there is no per-install container; the MCP is
        a connection to the OtoDock-owned central pool. Refreshing the catalog
        files is the whole update (the orchestrator skips docker MCPs on T3
        anyway; this stays correct if a manual update is invoked).
    """
    from services.community import community_installer
    from services.mcp import docker_manager
    from core.config import deployment

    # Capture the current image tag BEFORE the catalog replaces the manifest so
    # we can reclaim it afterwards if the update moves to a new tag.
    old = mcp_registry.get_manifest(name)
    old_image = (getattr(old.server, "image", "") if old else "") or ""

    result = await community_installer.install_from_catalog(name)

    refreshed = mcp_registry.get_manifest(name)
    if (
        refreshed
        and _mt.is_container(refreshed.server)
        and deployment.current_mode() == deployment.MANAGED_LOCAL
    ):
        states = await asyncio.to_thread(mcp_store.get_all_mcp_states)
        if states.get(name, False):
            await asyncio.to_thread(
                docker_manager.start_container, refreshed,
                force_recreate=True,
            )

    # Reclaim the previous image tag once the new one is running, so the daemon
    # doesn't keep one stale image per update. Only when the tag changed and
    # we're self-host (cloud has no local image). Best-effort.
    new_image = (getattr(refreshed.server, "image", "") if refreshed else "") or ""
    if (
        old_image
        and new_image
        and old_image != new_image
        and deployment.current_mode() != deployment.EXTERNAL_POOL
    ):
        await asyncio.to_thread(docker_manager.remove_image, old_image)

    return result


async def _catalog_entry(name: str) -> dict | None:
    """The community-catalog ``registry.json`` entry for ``name`` (or None)."""
    from services.community import community_catalog
    try:
        doc = await community_catalog.fetch_registry()
    except Exception:
        return None
    return next(
        (e for e in doc.get("mcps", []) if isinstance(e, dict) and e.get("name") == name),
        None,
    )


async def _update_node_python_mcp(name: str, manifest) -> dict:
    """Update an npm/pypi MCP by **converging it to the catalog folder**.

    The catalog is the source of truth for both axes:
      * **package** — install the latest version WITHIN the catalog
        ``version_constraint`` (absolute latest when unbounded), pinned into the
        local manifest;
      * **integration** — re-fetch + re-apply the catalog manifest + skills + files
        so a contributor's args/oauth/skill fix reaches installs.

    The two axes are decoupled: a manifest-only change re-pins the SAME package
    version (``install_version=cur``) — it never downgrades the package to a
    tightened bound. The whole-dir backup + rollback lives in
    ``install_from_extracted_folder``; running stdio sessions keep their already
    spawned subprocess, new sessions pick up the converged install.
    """
    from services.community import community_catalog, community_installer
    from services.mcp import mcp_installer

    parsed = mcp_installer.parse_source(manifest.server.source)
    if not parsed or parsed.registry not in ("npm", "pypi"):
        raise HTTPException(400, "MCP has no updatable source (npm/pypi)")
    registry, package = parsed.registry, parsed.package
    cur = manifest.version

    entry = await _catalog_entry(name)
    if entry is not None:
        constraint = entry.get("version_constraint", "") or ""
        catalog_hash = entry.get("manifest_hash")
    else:
        constraint = getattr(manifest.server, "version_constraint", "") or ""
        catalog_hash = None

    target = await resolve_latest_in_bound(registry, package, constraint)
    pkg_newer = _is_pkg_newer(target, cur)

    manifest_changed = False
    if catalog_hash:
        try:
            mf = Path(manifest.mcp_dir) / "manifest.json"
            data = await asyncio.to_thread(lambda: json.loads(mf.read_text()))
            manifest_changed = community_catalog.normalized_manifest_hash(data) != catalog_hash
        except Exception:
            manifest_changed = False

    if not pkg_newer and not manifest_changed:
        return {"status": "already_latest", "name": name, "version": cur}

    # Package axis only moves forward; a manifest-only converge keeps the version.
    install_version = target if pkg_newer else cur
    return await community_installer.install_from_catalog(name, install_version=install_version)


async def update_one(name: str) -> dict:
    """Update a single MCP, holding the per-MCP install lock so it can't race a
    concurrent install/update of the same MCP.

    Docker MCPs re-fetch from the community catalog (image tag); npm/pypi MCPs
    converge to the catalog folder + install the latest version within the catalog
    bound. Raises ``HTTPException`` (404/400/500/502) — callers that want
    best-effort behaviour (the weekly job) catch it per-MCP.
    """
    from core.credentials import catalog_install_registry

    manifest = mcp_registry.get_manifest(name)
    if not manifest:
        raise HTTPException(404, f"MCP '{name}' not found")

    async with catalog_install_registry.lock_for(name):
        # Re-read inside the lock in case a concurrent update just changed it.
        manifest = mcp_registry.get_manifest(name) or manifest
        if manifest.category == "skill":
            # Converge to the skills catalog (idempotent reinstall — the
            # generic runtime-none path below would 400 on "no source").
            from services.community import skills_installer
            result = await skills_installer.install_skill_package_from_catalog(name)
        elif _mt.is_container(manifest.server):
            result = await _update_docker_mcp(name)
        else:
            result = await _update_node_python_mcp(name, manifest)
        await asyncio.to_thread(_forget_check_result, name)
        return result


def _forget_check_result(name: str) -> None:
    """An applied update retires the persisted offer for the MCP; the next
    check writes a fresh one. Best effort: the update already happened."""
    from storage.mcp import mcp_update_state_store as state_store
    try:
        state_store.delete_check_result(name)
    except Exception:
        logger.warning("The persisted check result of %s could not be dropped", name)


# ---------------------------------------------------------------------------
# Auto-update targeting + in-use detection (weekly job helpers)
# ---------------------------------------------------------------------------

def community_targets() -> list:
    """Installed community MCPs the weekly job may update.

    Local/core + custom MCPs ship with the platform → out of scope. On T3
    (external-pool / cloud) docker MCPs are managed centrally by OtoDock, so they
    are dropped here — the weekly job is a clean no-op for them (non-docker
    community MCPs, if any, are still updated).
    """
    from core.config import deployment

    cloud = deployment.current_mode() == deployment.EXTERNAL_POOL
    out = []
    for m in mcp_registry.get_all_manifests().values():
        # Skill packages update from the skills catalog on every tier (pure
        # file converge — no docker/pool concerns).
        if m.category == "skill":
            out.append(m)
            continue
        if m.category != "community":
            continue
        if cloud and _mt.is_container(m.server):
            continue
        out.append(m)
    return out


async def mcp_in_use(name: str) -> bool:
    """True if any live agent session currently holds MCP ``name``.

    Used to DEFER recreating a docker MCP's shared container while a session is
    connected to it (a force-recreate would drop that connection). Signals:
      * Direct-LLM layer — precise: the MCP names actually started for each live
        in-process session (``mcp_pool``).
      * CLI + Codex layers — each live session's agent mapped through the
        registry's runtime MCP set (``get_agent_mcps``); these layers connect to
        the same shared container via their config files.
    Best-effort: a layer that can't be inspected is skipped (degrades to the same
    behaviour as the manual Update button — no worse).
    """
    # Direct layer — precise per-session connections.
    try:
        from core.layers.direct.mcp import mcp_pool
        if name in await mcp_pool.active_mcp_names():
            return True
    except Exception:
        logger.exception("mcp_in_use: direct-layer probe failed")

    # CLI + Codex — collect live-session agents, then map to their MCP sets.
    agents: set[str] = set()
    try:
        from core.layers.cli import session as cli_session
        agents |= await cli_session.active_agent_names()
    except Exception:
        logger.exception("mcp_in_use: cli-layer probe failed")
    try:
        from core.layers.codex import session as codex_session
        agents |= await codex_session.active_agent_names()
    except Exception:
        logger.exception("mcp_in_use: codex-layer probe failed")

    for agent in agents:
        if not agent:
            continue
        try:
            mcps = await asyncio.to_thread(mcp_registry.get_agent_mcps, agent)
        except Exception:
            logger.exception("mcp_in_use: get_agent_mcps(%s) failed", agent)
            continue
        if any(getattr(m, "name", "") == name for m in mcps):
            return True

    return False
