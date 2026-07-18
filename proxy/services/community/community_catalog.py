"""Community MCP catalog fetcher.

Reads the OtoDock community MCP catalog from
``https://raw.githubusercontent.com/OtoDock/community-mcps/main/`` and exposes
helpers for the dashboard API:

- ``fetch_registry()`` — returns the registry as a dict, served from an
  in-process cache with ETag-aware revalidation. Network errors fall back to
  the most recently cached registry so a brief GitHub outage doesn't blank
  the catalog page.
- ``fetch_manifest(name)`` / ``fetch_readme(name)`` — on-demand single-MCP
  fetches, also cached for a short TTL.
- ``augment_entry(entry, ...)`` — adds local platform state to a registry
  entry (``installed``, ``installed_version``, ``enabled_for_agents``,
  ``pending_request``).

This module is read-only catalog access; :func:`fetch_manifest` is also reused
by the dashboard install API (``api/mcp/community.py``).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("claude-proxy.community-catalog")


REGISTRY_RAW_URL = (
    "https://raw.githubusercontent.com/OtoDock/community-mcps/main/registry.json"
)
MCP_RAW_BASE = "https://raw.githubusercontent.com/OtoDock/community-mcps/main"

# Standalone skill packages live in their own catalog repo (same layout:
# registry.json + one folder per package). Entries sit under a "skills" key.
SKILLS_REGISTRY_RAW_URL = (
    "https://raw.githubusercontent.com/OtoDock/community-skills/main/registry.json"
)
SKILLS_RAW_BASE = "https://raw.githubusercontent.com/OtoDock/community-skills/main"

# Short TTL keeps the dashboard snappy on repeated opens but lets a registry
# bump propagate within a minute. Long enough that an admin clicking around
# doesn't hammer GitHub raw with serialized requests.
REGISTRY_CACHE_TTL_SECONDS = 60
MANIFEST_CACHE_TTL_SECONDS = 300
README_CACHE_TTL_SECONDS = 300

# GitHub raw responses are tiny (registry < 50KB; manifests < 5KB; READMEs
# rarely > 10KB). A 10s timeout is generous; anything slower means GitHub
# itself is degraded and we should fall back to the stale cached entry.
HTTP_TIMEOUT_SECONDS = 10.0


@dataclass
class _CacheEntry:
    """One cached HTTP fetch result."""

    value: Any
    fetched_at: float = 0.0
    etag: str | None = None


# Module-level caches. Async-safe via the lock below.
_registry_cache: _CacheEntry = _CacheEntry(value=None)
_manifest_cache: dict[str, _CacheEntry] = {}
_readme_cache: dict[str, _CacheEntry] = {}
_skills_registry_cache: _CacheEntry = _CacheEntry(value=None)
_skills_manifest_cache: dict[str, _CacheEntry] = {}
_skills_readme_cache: dict[str, _CacheEntry] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

async def _http_get_json(url: str, etag: str | None) -> tuple[Any | None, str | None, int]:
    """GET a JSON URL with optional ETag. Returns (json, new_etag, status).

    Status 304 means "use the cached body". 200 means "use the new body".
    Other statuses raise.
    """
    headers = {"Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code == 304:
        return None, etag, 304
    resp.raise_for_status()
    return resp.json(), resp.headers.get("etag"), resp.status_code


async def _http_get_text(url: str, etag: str | None) -> tuple[str | None, str | None, int]:
    """GET a text URL with optional ETag."""
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code == 304:
        return None, etag, 304
    resp.raise_for_status()
    return resp.text, resp.headers.get("etag"), resp.status_code


async def fetch_registry() -> dict[str, Any]:
    """Return the parsed ``registry.json`` from the community-mcps repo.

    Cached for ~60s; revalidated via ETag once the TTL is up. If the network
    fails and we have a previously cached registry, we return the stale value
    rather than 500ing the dashboard.
    """
    now = time.monotonic()
    cached = _registry_cache.value
    if cached is not None and now - _registry_cache.fetched_at < REGISTRY_CACHE_TTL_SECONDS:
        return cached

    async with _lock:
        # Recheck inside the lock — another coroutine may have refreshed.
        now = time.monotonic()
        if _registry_cache.value is not None and now - _registry_cache.fetched_at < REGISTRY_CACHE_TTL_SECONDS:
            return _registry_cache.value

        try:
            body, etag, status = await _http_get_json(
                REGISTRY_RAW_URL, _registry_cache.etag,
            )
            if status == 304 and _registry_cache.value is not None:
                _registry_cache.fetched_at = now
                return _registry_cache.value
            if body is None:
                # Unexpected — 304 without a prior body. Treat as miss.
                raise RuntimeError("registry GET returned no body and no cache")
            _registry_cache.value = body
            _registry_cache.fetched_at = now
            _registry_cache.etag = etag
            return body
        except Exception as exc:
            if _registry_cache.value is not None:
                logger.warning(
                    "Failed to refresh community registry (%s) — serving stale cache",
                    exc,
                )
                return _registry_cache.value
            logger.error("Failed to load community registry and no cache available: %s", exc)
            raise


async def fetch_manifest(name: str) -> dict[str, Any]:
    """Return the parsed ``manifest.json`` for one community MCP."""
    url = f"{MCP_RAW_BASE}/{name}/manifest.json"
    cached = _manifest_cache.get(name)
    now = time.monotonic()
    if cached is not None and cached.value is not None and now - cached.fetched_at < MANIFEST_CACHE_TTL_SECONDS:
        return cached.value

    async with _lock:
        cached = _manifest_cache.get(name)
        now = time.monotonic()
        if cached is not None and cached.value is not None and now - cached.fetched_at < MANIFEST_CACHE_TTL_SECONDS:
            return cached.value

        body, etag, status = await _http_get_json(url, cached.etag if cached else None)
        if status == 304 and cached is not None and cached.value is not None:
            cached.fetched_at = now
            return cached.value
        entry = _CacheEntry(value=body, fetched_at=now, etag=etag)
        _manifest_cache[name] = entry
        return body


async def fetch_readme(name: str) -> str:
    """Return the markdown ``README.md`` for one community MCP."""
    url = f"{MCP_RAW_BASE}/{name}/README.md"
    cached = _readme_cache.get(name)
    now = time.monotonic()
    if cached is not None and cached.value is not None and now - cached.fetched_at < README_CACHE_TTL_SECONDS:
        return cached.value

    async with _lock:
        cached = _readme_cache.get(name)
        now = time.monotonic()
        if cached is not None and cached.value is not None and now - cached.fetched_at < README_CACHE_TTL_SECONDS:
            return cached.value

        body, etag, status = await _http_get_text(url, cached.etag if cached else None)
        if status == 304 and cached is not None and cached.value is not None:
            cached.fetched_at = now
            return cached.value
        entry = _CacheEntry(value=body, fetched_at=now, etag=etag)
        _readme_cache[name] = entry
        return body or ""


async def fetch_skills_registry() -> dict[str, Any]:
    """Return the parsed ``registry.json`` from the community-skills repo.

    Same caching/ETag/stale-fallback scheme as :func:`fetch_registry`, with
    one deliberate difference: when the registry is unreachable AND uncached,
    this returns an EMPTY catalog flagged ``catalog_unreachable`` instead of
    raising — a fresh install (or a pre-repo-publish dev box) must show an
    empty Skills catalog with a banner, never an erroring tab.
    """
    now = time.monotonic()
    cached = _skills_registry_cache.value
    if cached is not None and now - _skills_registry_cache.fetched_at < REGISTRY_CACHE_TTL_SECONDS:
        return cached

    async with _lock:
        now = time.monotonic()
        if _skills_registry_cache.value is not None and now - _skills_registry_cache.fetched_at < REGISTRY_CACHE_TTL_SECONDS:
            return _skills_registry_cache.value

        try:
            body, etag, status = await _http_get_json(
                SKILLS_REGISTRY_RAW_URL, _skills_registry_cache.etag,
            )
            if status == 304 and _skills_registry_cache.value is not None:
                _skills_registry_cache.fetched_at = now
                return _skills_registry_cache.value
            if body is None:
                raise RuntimeError("skills registry GET returned no body and no cache")
            _skills_registry_cache.value = body
            _skills_registry_cache.fetched_at = now
            _skills_registry_cache.etag = etag
            return body
        except Exception as exc:
            if _skills_registry_cache.value is not None:
                logger.warning(
                    "Failed to refresh community skills registry (%s) — "
                    "serving stale cache", exc,
                )
                return _skills_registry_cache.value
            logger.warning(
                "Community skills registry unavailable and uncached (%s) — "
                "serving empty catalog", exc,
            )
            return {"skills": [], "catalog_unreachable": True}


async def fetch_skills_manifest(name: str) -> dict[str, Any]:
    """Return the parsed ``manifest.json`` for one community skill package."""
    url = f"{SKILLS_RAW_BASE}/{name}/manifest.json"
    return await _fetch_cached_json(url, _skills_manifest_cache, name)


async def fetch_skills_readme(name: str) -> str:
    """Return the markdown ``README.md`` for one community skill package."""
    url = f"{SKILLS_RAW_BASE}/{name}/README.md"
    cached = _skills_readme_cache.get(name)
    now = time.monotonic()
    if cached is not None and cached.value is not None and now - cached.fetched_at < README_CACHE_TTL_SECONDS:
        return cached.value
    async with _lock:
        cached = _skills_readme_cache.get(name)
        now = time.monotonic()
        if cached is not None and cached.value is not None and now - cached.fetched_at < README_CACHE_TTL_SECONDS:
            return cached.value
        body, etag, status = await _http_get_text(url, cached.etag if cached else None)
        if status == 304 and cached is not None and cached.value is not None:
            cached.fetched_at = now
            return cached.value
        _skills_readme_cache[name] = _CacheEntry(value=body, fetched_at=now, etag=etag)
        return body or ""


async def _fetch_cached_json(url: str, cache: dict[str, _CacheEntry],
                             key: str) -> dict[str, Any]:
    """Shared per-key JSON fetch with the manifest TTL + ETag scheme."""
    cached = cache.get(key)
    now = time.monotonic()
    if cached is not None and cached.value is not None and now - cached.fetched_at < MANIFEST_CACHE_TTL_SECONDS:
        return cached.value
    async with _lock:
        cached = cache.get(key)
        now = time.monotonic()
        if cached is not None and cached.value is not None and now - cached.fetched_at < MANIFEST_CACHE_TTL_SECONDS:
            return cached.value
        body, etag, status = await _http_get_json(url, cached.etag if cached else None)
        if status == 304 and cached is not None and cached.value is not None:
            cached.fetched_at = now
            return cached.value
        cache[key] = _CacheEntry(value=body, fetched_at=now, etag=etag)
        return body


# ---------------------------------------------------------------------------
# Augmentation — adds local platform state to a registry entry
# ---------------------------------------------------------------------------

def normalized_manifest_hash(manifest: dict) -> str:
    """Stable hash of an MCP manifest, ignoring the locally-pinned fields.

    node/python community MCPs are unpinned in the catalog; on install the proxy
    pins ``version`` + ``server.source`` into the LOCAL manifest (the ONLY two
    fields it mutates — see ``mcp_updater.pin_local_manifest``). Dropping exactly
    those two makes a freshly-installed manifest hash-equal to its catalog source,
    so any OTHER catalog edit (args, oauth, skills, version_constraint, …) shows up
    as a changed hash → a detectable "integration update".

    This is a CONTRACT: the community-mcps ``scripts/generate-registry.py`` computes
    the identical hash for each ``registry.json`` entry. The serialization is pinned
    (``sort_keys``, compact ``separators``, ``ensure_ascii=True``) so the contributor's
    manifest formatting and the proxy's ``json.dumps(indent=2)`` rewrite both collapse
    to the same bytes. Any change here MUST be mirrored in both repos' generators.
    """
    m = copy.deepcopy(manifest)
    m.pop("version", None)
    server = m.get("server")
    if isinstance(server, dict):
        server.pop("source", None)
    blob = json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _catalog_is_newer(catalog_version, local_version) -> bool:
    """True iff the catalog version is strictly newer than the installed one.

    Uses PEP 440 / semver ordering so an MCP whose installed version moved PAST
    the catalog (an npm/pypi MCP auto-updated from its own registry, where the
    catalog only pins the seed version) is never flagged as a bogus downgrade.
    Falls back to inequality when a version string isn't parseable (e.g. a
    non-semver docker tag), preserving the old "any change" behaviour there.
    """
    if not catalog_version or not local_version:
        return False
    try:
        from packaging.version import Version
        return Version(str(catalog_version)) > Version(str(local_version))
    except Exception:
        return str(catalog_version) != str(local_version)


def augment_entry(
    entry: dict[str, Any],
    installed_versions: dict[str, str],
    enabled_for_agents: dict[str, list[str]],
    pending_requests: dict[tuple[str, str], int] | None = None,
    agent_slug: str | None = None,
    installed_manifest_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a new dict that merges a catalog entry with local platform state.

    Adds:
    - ``installed`` (bool): whether the platform already has this MCP locally.
    - ``installed_version`` (str | None): version reported by the local
      manifest, if installed.
    - ``update_available`` (bool): true if the catalog version is strictly NEWER
      than the installed one (docker; not merely different) OR — for node/python,
      whose catalog version is empty (unbounded) — the catalog **integration
      manifest** changed (``manifest_hash`` differs from the installed one). A
      package-version update for node/python is detected separately by the
      registry probe in ``mcp_updater.detect_available_updates`` (Browse has no
      network), so this surface only reflects catalog-derived signals.
    - ``enabled_for_agents`` (list[str]): agent slugs that have this MCP
      assigned via the manager UI.
    - ``pending_request`` (int | None): open request id when
      ``agent_slug`` is provided and a request exists for this (mcp, agent);
      otherwise ``None``.
    - ``pending_request_count`` (int): number of open requests across all
      agents (admin context only — useful for surfacing "3 pending" badges).
    """
    name = entry["name"]
    local_version = installed_versions.get(name)
    installed = local_version is not None
    catalog_version = entry.get("version")
    update_available = bool(installed and _catalog_is_newer(catalog_version, local_version))
    # node/python: catalog version is "" (unbounded), so the version compare never
    # fires — flag instead when the catalog integration manifest changed. Guarded:
    # both hashes present (a stale registry.json without ``manifest_hash`` must
    # not false-flag). runtime "none" = skill packages / context-only entries,
    # whose only non-version update signal is the manifest hash.
    if (
        installed
        and not update_available
        and installed_manifest_hashes
        and entry.get("runtime") in ("node", "python", "none")
    ):
        catalog_hash = entry.get("manifest_hash")
        installed_hash = installed_manifest_hashes.get(name)
        if catalog_hash and installed_hash and catalog_hash != installed_hash:
            update_available = True
    pending_request: int | None = None
    pending_request_count = 0
    if pending_requests is not None:
        if agent_slug:
            pending_request = pending_requests.get((name, agent_slug))
        for (mcp_name_key, _agent), _rid in pending_requests.items():
            if mcp_name_key == name:
                pending_request_count += 1
    return {
        **entry,
        "installed": installed,
        "installed_version": local_version,
        "update_available": update_available,
        "enabled_for_agents": sorted(enabled_for_agents.get(name, [])),
        "pending_request": pending_request,
        "pending_request_count": pending_request_count,
    }


def _collect_installed_versions() -> dict[str, str]:
    """Build a ``{mcp_name: version}`` map of every locally installed MCP.

    Reads from :mod:`services.mcp.mcp_registry`; doesn't touch the DB. Both
    community and non-community MCPs are included — a catalog entry only
    matches by ``name``.
    """
    from services.mcp import mcp_registry

    manifests = mcp_registry.get_all_manifests()
    return {name: m.version for name, m in manifests.items()}


def _collect_installed_manifest_hashes() -> dict[str, str]:
    """Build a ``{mcp_name: normalized_manifest_hash}`` map for installed
    node/python MCPs, read from each install's raw ``manifest.json``.

    The in-memory ``McpManifest`` dataclass is lossy (drops unknown keys, applies
    defaults), so the hash must come from the file — hence raw reads here. Used by
    :func:`augment_entry` to flag catalog integration-manifest changes for the
    Browse UI. Docker/git+/remote are skipped (their update signal is the version).
    """
    from services.mcp import mcp_registry

    out: dict[str, str] = {}
    for name, m in mcp_registry.get_all_manifests().items():
        if getattr(m.server, "runtime", "") not in ("node", "python", "none"):
            continue
        try:
            data = json.loads((Path(m.mcp_dir) / "manifest.json").read_text())
            out[name] = normalized_manifest_hash(data)
        except Exception:
            continue
    return out


async def collect_local_state() -> tuple[
    dict[str, str],
    dict[str, list[str]],
    dict[tuple[str, str], int],
    dict[str, str],
]:
    """Snapshot the local state needed to augment catalog entries.

    Returns ``(installed_versions, enabled_for_agents, pending_requests,
    installed_manifest_hashes)`` where:

    - ``installed_versions[mcp_name]`` = the local manifest's version.
    - ``enabled_for_agents[mcp_name]`` = list of agent slugs that have
      enabled the MCP via the manager UI.
    - ``pending_requests[(mcp_name, agent_slug)]`` = open request id (any
      state in :data:`storage.mcp_request_store.OPEN_STATES`).
    - ``installed_manifest_hashes[mcp_name]`` = normalized manifest hash for
      installed node/python MCPs (for integration-change detection in Browse).
    """
    from storage import mcp_store, mcp_request_store

    installed_versions = await asyncio.to_thread(_collect_installed_versions)
    installed_manifest_hashes = await asyncio.to_thread(_collect_installed_manifest_hashes)
    all_agent_mcps = await asyncio.to_thread(mcp_store.get_all_manager_enabled_mcps)
    enabled_for_agents: dict[str, list[str]] = {}
    for agent_name, mcp_names in all_agent_mcps.items():
        for mcp_name in mcp_names:
            enabled_for_agents.setdefault(mcp_name, []).append(agent_name)
    pending_requests = await asyncio.to_thread(mcp_request_store.open_requests_by_pair)
    return installed_versions, enabled_for_agents, pending_requests, installed_manifest_hashes
