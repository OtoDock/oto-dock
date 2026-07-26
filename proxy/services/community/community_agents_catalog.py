"""Community-agents catalog fetcher.

Mirrors ``services/community/community_catalog.py`` (which serves the MCPs catalog)
but points at ``OtoDock/community-agents``. Same caching strategy: 60s
in-process TTL, ETag-aware revalidation, stale fallback on GitHub outage.

Entry points used by ``api/mcp/community.py`` and
``services/community/community_agent_installer.py``:

- :func:`fetch_registry` — parsed ``registry.json`` (cached).
- :func:`fetch_manifest` — one template's ``agent.json`` (cached).
- :func:`fetch_readme` — one template's ``README.md`` (cached).
- :func:`fetch_and_extract_template` — downloads the repo tarball, extracts
  the ``<slug>/`` subfolder into a tempdir, returns the path. Caller is
  responsible for cleaning it up.
- :func:`augment_entry` — adds local platform state (``installed_as``,
  ``platform_compat_ok``).
"""

from __future__ import annotations

import asyncio
import logging
import tarfile
import tempfile
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("claude-proxy.community-agents-catalog")


# Env-overridable for DEV/TEST only (staging catalog / local static server).
import os as _os

REGISTRY_RAW_URL = _os.environ.get("OTODOCK_AGENTS_CATALOG_REGISTRY_URL") or (
    "https://raw.githubusercontent.com/OtoDock/community-agents/main/registry.json"
)
TEMPLATE_RAW_BASE = _os.environ.get("OTODOCK_AGENTS_CATALOG_RAW_BASE") or (
    "https://raw.githubusercontent.com/OtoDock/community-agents/main"
)
TARBALL_URL = _os.environ.get("OTODOCK_AGENTS_CATALOG_TARBALL_URL") or (
    "https://codeload.github.com/OtoDock/community-agents/tar.gz/main"
)
TARBALL_FALLBACK_URL = _os.environ.get("OTODOCK_AGENTS_CATALOG_TARBALL_URL") or (
    "https://github.com/OtoDock/community-agents/tarball/main"
)

REGISTRY_CACHE_TTL_SECONDS = 60
MANIFEST_CACHE_TTL_SECONDS = 300
README_CACHE_TTL_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 10.0
TARBALL_TIMEOUT_SECONDS = 30.0


@dataclass
class _CacheEntry:
    value: Any
    fetched_at: float = 0.0
    etag: str | None = None


_registry_cache: _CacheEntry = _CacheEntry(value=None)
_manifest_cache: dict[str, _CacheEntry] = {}
_readme_cache: dict[str, _CacheEntry] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Fetch helpers (mirror services/community/community_catalog.py)
# ---------------------------------------------------------------------------

async def _http_get_json(url: str, etag: str | None) -> tuple[Any | None, str | None, int]:
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
    """Return the parsed agents registry.json (cached + stale-fallback)."""
    now = time.monotonic()
    cached = _registry_cache.value
    if cached is not None and now - _registry_cache.fetched_at < REGISTRY_CACHE_TTL_SECONDS:
        return cached

    async with _lock:
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
                raise RuntimeError("agents registry GET returned no body and no cache")
            _registry_cache.value = body
            _registry_cache.fetched_at = now
            _registry_cache.etag = etag
            return body
        except Exception as exc:
            if _registry_cache.value is not None:
                logger.warning(
                    "Failed to refresh agents registry (%s) — serving stale cache",
                    exc,
                )
                return _registry_cache.value
            logger.error("Failed to load agents registry: %s", exc)
            raise


async def fetch_manifest(template_slug: str) -> dict[str, Any]:
    """Return one template's parsed ``agent.json``."""
    url = f"{TEMPLATE_RAW_BASE}/{template_slug}/agent.json"
    cached = _manifest_cache.get(template_slug)
    now = time.monotonic()
    if cached and cached.value and now - cached.fetched_at < MANIFEST_CACHE_TTL_SECONDS:
        return cached.value
    async with _lock:
        cached = _manifest_cache.get(template_slug)
        now = time.monotonic()
        if cached and cached.value and now - cached.fetched_at < MANIFEST_CACHE_TTL_SECONDS:
            return cached.value
        body, etag, status = await _http_get_json(url, cached.etag if cached else None)
        if status == 304 and cached and cached.value:
            cached.fetched_at = now
            return cached.value
        entry = _CacheEntry(value=body, fetched_at=now, etag=etag)
        _manifest_cache[template_slug] = entry
        return body


async def fetch_readme(template_slug: str) -> str:
    """Return one template's markdown README."""
    url = f"{TEMPLATE_RAW_BASE}/{template_slug}/README.md"
    cached = _readme_cache.get(template_slug)
    now = time.monotonic()
    if cached and cached.value and now - cached.fetched_at < README_CACHE_TTL_SECONDS:
        return cached.value
    async with _lock:
        cached = _readme_cache.get(template_slug)
        now = time.monotonic()
        if cached and cached.value and now - cached.fetched_at < README_CACHE_TTL_SECONDS:
            return cached.value
        body, etag, status = await _http_get_text(url, cached.etag if cached else None)
        if status == 304 and cached and cached.value:
            cached.fetched_at = now
            return cached.value
        entry = _CacheEntry(value=body, fetched_at=now, etag=etag)
        _readme_cache[template_slug] = entry
        return body or ""


# ---------------------------------------------------------------------------
# Tarball fetch + extract
# ---------------------------------------------------------------------------

async def fetch_and_extract_template(template_slug: str) -> Path:
    """Download the community-agents tarball, extract the ``<slug>/``
    subfolder into a fresh tempdir, and return the path. Caller cleans up.

    Path-traversal safe: every extracted entry's resolved absolute path is
    checked to remain inside the tempdir.
    """
    tarball = await _fetch_tarball()
    return await asyncio.to_thread(_extract_template_subfolder, tarball, template_slug)


async def _fetch_tarball() -> bytes:
    """Fetch the repo tarball. Tries codeload first (CDN), then archive
    fallback. Both follow redirects automatically."""
    async with httpx.AsyncClient(
        timeout=TARBALL_TIMEOUT_SECONDS, follow_redirects=True,
    ) as client:
        try:
            resp = await client.get(TARBALL_URL)
            resp.raise_for_status()
            return resp.content
        except Exception:
            logger.warning("codeload fetch failed; trying archive URL")
            resp = await client.get(TARBALL_FALLBACK_URL)
            resp.raise_for_status()
            return resp.content


def _is_safe_name(name: str) -> bool:
    if not name:
        return False
    if ".." in name.split("/"):
        return False
    if name.startswith("/"):
        return False
    return True


def _extract_template_subfolder(tarball: bytes, template_slug: str) -> Path:
    """Extract OtoDock-community-agents-<sha>/<template_slug>/* into a temp dir.
    Returns the path to the per-template extracted dir.
    """
    if not _is_safe_name(template_slug):
        raise ValueError(f"Unsafe template slug: {template_slug!r}")
    tempdir = Path(tempfile.mkdtemp(prefix=f"community-agent-{template_slug}-"))
    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        root_prefix: str | None = None
        for member in tar.getmembers():
            # The repo tarball wraps everything in a top-level dir like
            # "OtoDock-community-agents-9c62e60/". Strip it.
            parts = member.name.split("/", 1)
            if root_prefix is None and parts and parts[0]:
                root_prefix = parts[0] + "/"
            if not root_prefix or not member.name.startswith(root_prefix):
                continue
            rel = member.name[len(root_prefix):]
            if not rel or not rel.startswith(f"{template_slug}/"):
                continue
            if not _is_safe_name(rel):
                logger.warning("skipping unsafe tar entry %r", member.name)
                continue
            dest = tempdir / rel
            dest_resolved = dest.resolve()
            if not str(dest_resolved).startswith(str(tempdir.resolve())):
                logger.warning("path-traversal blocked %r", member.name)
                continue
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            f = tar.extractfile(member)
            if f is None:
                continue
            dest.write_bytes(f.read())
    template_dir = tempdir / template_slug
    if not template_dir.is_dir():
        # Clean up before raising.
        import shutil
        shutil.rmtree(tempdir, ignore_errors=True)
        raise FileNotFoundError(
            f"Template '{template_slug}' not found in community-agents tarball",
        )
    return template_dir


# ---------------------------------------------------------------------------
# Augmentation (local-state overlay)
# ---------------------------------------------------------------------------

def augment_entry(
    entry: dict[str, Any],
    installed_as: dict[str, list[str]],
) -> dict[str, Any]:
    """Add ``installed_as`` (list of agent slugs on this platform installed
    from this template) to a registry entry.

    ``installed_as`` is the inverse of the agent's ``community_template``
    column — built from a single SQL query in the API layer.
    """
    from services.community import community_catalog
    name = entry.get("slug") or entry.get("name")
    return {
        **entry,
        "installed_as": sorted(installed_as.get(name, [])),
        "platform_compat_ok": community_catalog.platform_version_ok(
            entry.get("platform_min_version"),
        ),
    }


async def collect_local_state() -> dict[str, list[str]]:
    """Return ``{template_slug: [agent_slug, ...]}`` of every agent installed
    from a community-agents template.
    """
    from storage.pg import get_conn

    def _q():
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT slug, community_template FROM agents "
                "WHERE community_template IS NOT NULL"
            ).fetchall()
            out: dict[str, list[str]] = {}
            for r in rows:
                out.setdefault(r["community_template"], []).append(r["slug"])
            return out

    return await asyncio.to_thread(_q)
