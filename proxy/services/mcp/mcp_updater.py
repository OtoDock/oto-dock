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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

from services.mcp import mcp_manifest_types as _mt
from services.mcp import mcp_registry
from storage import database as task_store
from storage.mcp import mcp_store

logger = logging.getLogger("otodock.mcp-updater")


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


def _is_git(manifest) -> bool:
    """A community MCP installed from a ``git+`` source: its updates are the
    catalog's (a new ref under a new version). The weekly job builds one only
    when the catalog pins a full commit (``pinned_commit`` on the offer); an
    admin's Update builds any ref the catalog install accepts."""
    source = getattr(getattr(manifest, "server", None), "source", "")
    return isinstance(source, str) and source.startswith("git+")


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
    *, extras: tuple[str, ...] = (),
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
    docker/git+ manifests are never passed here. A PyPI source keeps its
    ``extras``: ``pypi:name[a,b]@1.2.3``.
    """
    from services.mcp.mcp_installer import ParsedSource
    manifest_path = mcp_dir / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["version"] = resolved_version
    data.setdefault("server", {})["source"] = ParsedSource(
        registry, package, "", tuple(extras)).pinned(resolved_version)
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
# The waiting period — what the weekly job may apply
# ---------------------------------------------------------------------------
#
# The weekly job applies an npm/pypi release only once it has been public for
# RELEASE_MIN_AGE, so a compromised or broken upstream release has a week to
# be noticed and pulled before it reaches an install. An admin's Update never
# waits (resolve_latest_in_bound): that is how an upstream security fix goes
# in at once.

RELEASE_MIN_AGE = timedelta(days=7)
# The full npm document carries every version's publish time and can run to
# tens of MB; past this (decompressed) the probe answers without times.
_REGISTRY_DOC_CAP = 64 * 1024 * 1024


@dataclass
class Release:
    """What a registry offers within a bound: ``latest`` (what an admin's
    Update installs), ``settled`` (the newest stable version, not above
    ``latest``, public for at least ``RELEASE_MIN_AGE``: what the weekly job
    applies) and ``latest_at`` (when ``latest`` was released, "" unknown)."""
    latest: str | None
    settled: str | None = None
    latest_at: str = ""


def _read_capped(url: str, accept: str) -> dict | None:
    """GET a JSON document, gzip accepted, its decompressed body capped at
    ``_REGISTRY_DOC_CAP``. ``None`` on any error or a body over the cap."""
    import urllib.request
    import zlib
    req = urllib.request.Request(url, headers={"Accept": accept, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read(_REGISTRY_DOC_CAP + 1)
        if len(raw) > _REGISTRY_DOC_CAP:
            return None
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = inflate.decompress(raw, _REGISTRY_DOC_CAP + 1)
            if len(raw) > _REGISTRY_DOC_CAP or inflate.unconsumed_tail:
                return None
    doc = json.loads(raw)
    return doc if isinstance(doc, dict) else None


async def _registry_doc(registry: str, package: str) -> dict | None:
    """The full npm document (``versions``, ``time``, ``dist-tags``) or the
    PyPI JSON (``info``, ``releases``). ``None`` on any failure."""
    if registry == "npm":
        url, accept = f"https://registry.npmjs.org/{package}", "application/json"
    else:
        url, accept = f"https://pypi.org/pypi/{package}/json", "application/json"
    try:
        return await asyncio.to_thread(_read_capped, url, accept)
    except Exception:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _release_times(registry: str, doc: dict) -> dict[str, datetime | None]:
    """Every published version and when it was released (``None`` unknown).
    npm: the ``versions`` map, its ``time`` entry. PyPI: releases with a file
    that is not yanked, timed by their NEWEST such file (a wheel added to an
    old release waits as a new release would)."""
    out: dict[str, datetime | None] = {}
    if registry == "npm":
        times = doc.get("time") if isinstance(doc.get("time"), dict) else {}
        for version in (doc.get("versions") or {}):
            out[str(version)] = _parse_time(times.get(version))
        return out
    for version, files in (doc.get("releases") or {}).items():
        live = [f for f in (files or []) if isinstance(f, dict) and not f.get("yanked")]
        if not live:
            continue
        stamps = [_parse_time(f.get("upload_time_iso_8601") or f.get("upload_time")) for f in live]
        out[str(version)] = None if None in stamps else max(stamps)
    return out


async def resolve_release(
    registry: str, package: str, constraint: str | None, *, now: datetime | None = None,
) -> Release:
    """``latest`` by today's rule (the registry's latest when unbounded, the
    newest stable version in bound otherwise) and the newest version the
    waiting period allows. A version whose release time is missing or in the
    future never settles. When the registry document cannot be read, the
    answer is ``latest`` alone (nothing settles: the job waits for an admin)."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    doc = await _registry_doc(registry, package)
    if doc is None:
        return Release(latest=await resolve_latest_in_bound(registry, package, constraint))
    try:
        spec = SpecifierSet(constraint or "")
    except InvalidSpecifier:
        logger.warning("Invalid version_constraint %r for %s — ignoring", constraint, package)
        spec, constraint = SpecifierSet(""), ""
    times = _release_times(registry, doc)
    usable = [v for v in times if _is_stable_in_bound(registry, v, spec)]
    if constraint:
        latest = str(max(usable, key=Version)) if usable else None
    elif registry == "npm":
        latest = (doc.get("dist-tags") or {}).get("latest")
    else:
        latest = (doc.get("info") or {}).get("version")
    if not latest:
        return Release(latest=None)
    now = now or _utcnow()
    try:
        ceiling = Version(str(latest))
    except InvalidVersion:
        return Release(latest=str(latest))
    settled_ok = [
        v for v in usable
        if Version(v) <= ceiling and times[v] is not None and times[v] <= now - RELEASE_MIN_AGE
    ]
    at = times.get(str(latest))
    return Release(
        latest=str(latest),
        settled=str(max(settled_ok, key=Version)) if settled_ok else None,
        latest_at=at.isoformat() if at else "",
    )


def _waiting_fields(rel: Release, current: str) -> dict:
    """The offer's fields for the weekly job: ``settled`` (the version it
    would apply now, "" none newer than the install) and, while ``latest``
    waits, ``auto_after`` (when it settles, "" when its time is unknown)."""
    fields = {"settled": rel.settled if _is_pkg_newer(rel.settled, current) else ""}
    if rel.settled != rel.latest:
        at = _parse_time(rel.latest_at) if rel.latest_at else None
        fields["auto_after"] = (at + RELEASE_MIN_AGE).isoformat() if at else ""
    return fields


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
    An npm/pypi offer also carries what the weekly job may apply under the
    waiting period (``settled``, ``auto_after``; ``resolve_release``), and
    ``held`` / ``rebuild`` when they apply. Docker and git+ MCPs compare the
    installed version against the catalog version (``registry`` "catalog" or
    "git"); a git+ offer carries ``pinned_commit``, whether the catalog's
    source pins a full commit (the weekly job builds only that, and the
    catalog install refuses anything else). The catalog ``registry.json`` is fetched ONCE up front; on failure
    both bound + manifest signals degrade off (the npm/pypi package probe still
    runs), so a transient outage never breaks the caller.
    """
    from services.community import community_catalog, community_installer
    from services.mcp import mcp_installer, mcp_venv_bootstrap

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
    # A venv on an interpreter the session sandbox does not bind is rebuilt by
    # the converge at the installed version (the admin's Update repairs it).
    outside = await asyncio.to_thread(
        lambda: {n for n, m, _r, _p in checks if _r == "pypi"
                 and mcp_venv_bootstrap.venv_outside_sandbox(Path(m.mcp_dir))})
    now = _utcnow()
    # Each probe may read a registry document of tens of MB: a few at a time.
    registry_reads = asyncio.Semaphore(4)

    async def _check_one(mcp_name, m, registry, package):
        entry = catalog.get(mcp_name)
        # Bound comes from the catalog (source of truth); fall back to the local
        # manifest only when the catalog entry is unavailable (fetch failed).
        if entry is not None:
            constraint = entry.get("version_constraint", "") or ""
        else:
            constraint = getattr(m.server, "version_constraint", "") or ""
        async with registry_reads:
            rel = await resolve_release(registry, package, constraint, now=now)
        target = rel.latest
        pkg_newer = _is_pkg_newer(target, m.version)
        catalog_hash = entry.get("manifest_hash") if entry else None
        installed_hash = installed_hashes.get(mcp_name)
        manifest_changed = bool(catalog_hash) and bool(installed_hash) and installed_hash != catalog_hash
        if entry is not None and community_catalog.source_detail_differs(
                m.server.source, entry.get("source")):
            manifest_changed = True
        # A rebuild converges through the catalog: an MCP no longer there has none.
        rebuild = mcp_name in outside and entry is not None
        if not pkg_newer and not manifest_changed and not rebuild:
            return None
        manifest_changed = manifest_changed or rebuild
        reason = "both" if (pkg_newer and manifest_changed) else ("package" if pkg_newer else "manifest")
        info = {
            "current": m.version,
            "latest": target if pkg_newer else m.version,
            "registry": registry, "package": package, "reason": reason,
        }
        if pkg_newer:
            info.update(_waiting_fields(rel, m.version))
        if rebuild:
            info["rebuild"] = True
        if is_held(m):
            info["held"] = True
        return (mcp_name, info)

    for result in await asyncio.gather(
        *[_check_one(*c) for c in checks], return_exceptions=True,
    ):
        if result and not isinstance(result, Exception):
            mcp_name, info = result
            results[mcp_name] = info

    # Catalog-pinned MCPs — a container (its image tag) and a git+ source (its
    # ref): the installed version against the catalog version, then the
    # integration manifest (a catalog edit under the same version: the
    # converge re-fetches the folder; a git+ ref that moved under the same
    # version counts too, the hash leaves the source out). Community only,
    # same rationale as above (a custom docker MCP — file-tools — updates via
    # platform releases, and a name collision with a catalog entry must never
    # produce a converge offer for it). The weekly job builds a git+ offer only
    # when the catalog pins a full commit (``pinned_commit``).
    docker_targets = [
        (name, m) for name, m in manifests.items()
        if m.category == "community" and name not in source_changes
        and (_mt.is_container(m.server) or _is_git(m))
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

    git_outside = await asyncio.to_thread(
        lambda: {n for n, m in docker_targets
                 if _is_git(m) and mcp_venv_bootstrap.venv_outside_sandbox(Path(m.mcp_dir))})

    docker_checked = 0
    for name, m in docker_targets:
        docker_checked += 1
        entry = catalog.get(name)
        latest = entry.get("version") if entry else None
        catalog_hash = entry.get("manifest_hash") if entry else None
        installed_hash = docker_hashes.get(name)
        relation = _catalog_relation(latest, m.version)
        kind = "git" if _is_git(m) else "catalog"
        changed = bool(catalog_hash and installed_hash and catalog_hash != installed_hash)
        if entry is not None and kind == "git" and community_catalog.source_detail_differs(
                m.server.source, entry.get("source")):
            changed = True
        rebuild = name in git_outside and entry is not None
        if relation == REL_SAME and (changed or rebuild):
            results[name] = {
                "current": m.version, "latest": m.version,
                "registry": kind, "package": name, "reason": "manifest",
            }
            if rebuild:
                results[name]["rebuild"] = True
        elif relation == REL_UPDATE:
            results[name] = {
                "current": m.version, "latest": latest,
                "registry": kind, "package": name, "reason": "package",
            }
        elif relation == REL_AHEAD:
            # An install past the catalog (an out-of-band deploy, a newer
            # archive) is never moved back by the job; the row reports it and
            # the admin may revert it on purpose through the Update route.
            results[name] = {
                "current": m.version, "latest": latest,
                "registry": kind, "package": name, "reason": "ahead",
            }
            logger.info(
                "MCP %s: installed %s is ahead of the catalog %s; left alone",
                name, m.version, latest,
            )
        if name in results and is_held(m):
            results[name]["held"] = True
        if kind == "git" and name in results and entry is not None:
            catalog_source = entry.get("source")
            results[name]["pinned_commit"] = community_installer.git_ref_is_commit(catalog_source)
            parsed_ref = mcp_installer.parse_source(catalog_source) if isinstance(catalog_source, str) else None
            results[name]["catalog_ref"] = parsed_ref.version if parsed_ref is not None else ""

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


async def _update_node_python_mcp(name: str, manifest, *, settled_only: bool = False) -> dict:
    """Update an npm/pypi MCP by **converging it to the catalog folder**.

    The catalog is the source of truth for both axes:
      * **package** — install the latest version WITHIN the catalog
        ``version_constraint`` (absolute latest when unbounded), pinned into the
        local manifest; with ``settled_only`` (the weekly job) the newest one
        public for ``RELEASE_MIN_AGE``;
      * **integration** — re-fetch + re-apply the catalog manifest + skills + files
        so a contributor's args/oauth/skill fix reaches installs (a changed
        hash, the extras of the catalog's source, or a venv the session sandbox
        cannot run, which the converge rebuilds).

    The two axes are decoupled: a manifest-only change re-pins the SAME package
    version (``install_version=cur``) — it never downgrades the package to a
    tightened bound. With ``settled_only``, a manifest change beside a package
    release that has not settled waits whole (``status: waiting``): the edit
    may be written for the new package. The whole-dir backup + rollback lives
    in ``install_from_extracted_folder``; running stdio sessions keep their
    already spawned subprocess, new sessions pick up the converged install.
    """
    from services.community import community_catalog, community_installer
    from services.mcp import mcp_installer, mcp_venv_bootstrap

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

    rel: Release | None = None
    if settled_only:
        rel = await resolve_release(registry, package, constraint)
        target = rel.settled
    else:
        target = await resolve_latest_in_bound(registry, package, constraint)
    pkg_newer = _is_pkg_newer(target, cur)

    edited = False
    if catalog_hash:
        try:
            mf = Path(manifest.mcp_dir) / "manifest.json"
            data = await asyncio.to_thread(lambda: json.loads(mf.read_text()))
            edited = community_catalog.normalized_manifest_hash(data) != catalog_hash
        except Exception:
            edited = False
    if entry is not None and community_catalog.source_detail_differs(
            manifest.server.source, entry.get("source")):
        edited = True
    rebuild = registry == "pypi" and await asyncio.to_thread(
        mcp_venv_bootstrap.venv_outside_sandbox, Path(manifest.mcp_dir))
    manifest_changed = edited or rebuild

    latest_waits = rel is not None and _is_pkg_newer(rel.latest, cur) and rel.settled != rel.latest
    if not pkg_newer and not manifest_changed:
        # A newer release the job may not apply yet (or a registry it could
        # not read): the offer stays on the page.
        if latest_waits:
            return {"status": "waiting", "name": name, "version": cur}
        return {"status": "already_latest", "name": name, "version": cur}
    if edited and latest_waits:
        # The catalog edit may be written for the release that has not
        # settled: the whole converge waits (a rebuild alone never does).
        return {"status": "waiting", "name": name, "version": cur}

    # Package axis only moves forward; a manifest-only converge keeps the version.
    install_version = target if pkg_newer else cur
    result = await community_installer.install_from_catalog(name, install_version=install_version)
    if rel is not None and _is_pkg_newer(rel.latest, install_version):
        # The newer release still waits: the page keeps showing it.
        result["pending_offer"] = {
            "current": install_version, "latest": rel.latest,
            "registry": registry, "package": package, "reason": "package",
            **_waiting_fields(rel, install_version),
        }
    return result


async def update_one(name: str, *, settled_only: bool = False) -> dict:
    """Update a single MCP, holding the per-MCP install lock so it can't race a
    concurrent install/update of the same MCP.

    Docker MCPs re-fetch from the community catalog (image tag); git+ MCPs
    converge to the catalog folder and build its ref; npm/pypi MCPs converge to
    the catalog folder + install the latest version within the catalog bound
    (``settled_only``: the newest past the waiting period, for the weekly job,
    which builds a git+ source only at a catalog-pinned commit, with no wait:
    the catalog's bump is a reviewed change). Raises ``HTTPException``
    (404/400/409/500/502) — callers that want best-effort behaviour (the weekly
    job) catch it per-MCP.
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
        elif _is_git(manifest):
            from services.community import community_installer
            if settled_only:
                entry = await _catalog_entry(name)
                if entry is None or not community_installer.git_ref_is_commit(entry.get("source")):
                    raise HTTPException(
                        409, f"'{name}' builds from a git source the catalog does not pin to a "
                             "commit: automatic updates never build a tag or a branch",
                    )
            # The catalog install refuses a ref that is not a commit, which
            # also covers a catalog that moved since the check above.
            result = await community_installer.install_from_catalog(name)
        else:
            result = await _update_node_python_mcp(name, manifest, settled_only=settled_only)
        if result.get("status") == "waiting":
            return result
        offer = result.pop("pending_offer", None)
        if offer:
            await asyncio.to_thread(_keep_check_result, name, offer)
        else:
            await asyncio.to_thread(_forget_check_result, name)
        return result


def _keep_check_result(name: str, info: dict) -> None:
    """A settled apply below the newest release rewrites the persisted offer
    (the newer release still waits) instead of retiring it. Best effort."""
    from storage.mcp import mcp_update_state_store as state_store
    try:
        state_store.put_check_result(name, info)
    except Exception:
        logger.warning("The persisted check result of %s could not be rewritten", name)


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
