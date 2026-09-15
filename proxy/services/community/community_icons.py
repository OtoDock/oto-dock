"""Icons for MCPs: the installed folder's ``icon.png``, else the catalog's.

Every MCP folder may carry a 256×256 ``icon.png``: the bundled MCPs ship
one, a community entry's arrives with the folder copy on install and on
every converge. The dashboard fetches every icon from the proxy
(``GET /v1/mcps/{name}/icon.png``), never from the catalog host directly.

Resolution order (:func:`installed_icon_path`, then :func:`catalog_icon`):

1. the installed MCP's ``<mcp_dir>/icon.png``, any category;
2. the community catalog: the registry entry's ``icon_url`` (a relative
   ``./<folder>/icon.png``) fetched from ``MCP_RAW_BASE`` and kept in an
   in-process cache for a day, next to the registry cache. "No icon" is
   cached the same way (a registry entry without ``icon_url``, a name the
   registry does not know, an upstream 404, a body that is not a 256×256
   PNG). A failed registry fetch marks the registry unavailable for a few
   minutes so an offline install answers at once instead of queueing every
   icon miss behind the registry's timeout.

Under ``OTODOCK_AIR_GAPPED`` step 2 never goes outbound, not even for the
registry: the install shows installed icons only.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

import config
from services.community import community_catalog

logger = logging.getLogger("claude-proxy.community-icons")

ICON_SIZE = 256
ICON_MAX_BYTES = 256 * 1024
ICON_CACHE_TTL_SECONDS = 24 * 3600
# After a failed registry or icon fetch, how long the last answer stands
# before the next request tries the network again.
RETRY_AFTER_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 10.0

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The installer's name rule (community_installer._is_safe_name widened to the
# upper case the manifest loader accepts): the name is a path segment here.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# The registry emits ``./<folder>/icon.png``; the folder can differ from the
# entry name (``ha-mcp`` holds ``home-assistant``). Anything else is refused,
# so a registry can never point the proxy outside the catalog tree.
_ICON_URL_RE = re.compile(r"^\./([A-Za-z0-9][A-Za-z0-9_-]*)/icon\.png$")


@dataclass
class _IconEntry:
    body: bytes | None      # None = the catalog has no icon for this name
    etag: str | None
    fetched_at: float

    def fresh(self, now: float) -> bool:
        return now - self.fetched_at < ICON_CACHE_TTL_SECONDS


_cache: dict[str, _IconEntry] = {}
_locks: dict[str, asyncio.Lock] = {}
_registry_unavailable_until: float = 0.0


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def installed_icon_path(name: str) -> Path | None:
    """The installed MCP's ``icon.png`` when the MCP is installed and ships one."""
    from services.mcp import mcp_registry

    manifest = mcp_registry.get_manifest(name)
    if manifest is None:
        return None
    path = Path(manifest.mcp_dir) / "icon.png"
    # The same contract the catalog fetch enforces: a community folder's
    # file arrives unvalidated with the tarball, and every row loads it.
    try:
        if not path.is_file() or path.stat().st_size > ICON_MAX_BYTES:
            return None
        with path.open("rb") as f:
            head = f.read(24)
    except OSError:
        return None
    return None if png_problem(head) else path


def catalog_folder(icon_url: object) -> str | None:
    """The catalog folder named by a registry entry's ``icon_url``, or None."""
    m = _ICON_URL_RE.match(icon_url) if isinstance(icon_url, str) else None
    return m.group(1) if m else None


def png_problem(body: bytes) -> str | None:
    """Why ``body`` is not the 256×256 PNG the catalog contract asks for (None when it is)."""
    if len(body) > ICON_MAX_BYTES:
        return f"{len(body)} bytes (limit {ICON_MAX_BYTES})"
    if len(body) < 24 or body[:8] != _PNG_SIGNATURE or body[12:16] != b"IHDR":
        return "not a PNG"
    width, height = struct.unpack(">II", body[16:24])
    if (width, height) != (ICON_SIZE, ICON_SIZE):
        return f"{width}x{height}, must be {ICON_SIZE}x{ICON_SIZE}"
    return None


def _stale_for_a_while(name: str, previous: _IconEntry | None, now: float) -> bytes | None:
    """Keep the last answer (or "no icon") for RETRY_AFTER_SECONDS after a
    failure, so a broken network costs one attempt per name per window."""
    body = previous.body if previous else None
    etag = previous.etag if previous else None
    _cache[name] = _IconEntry(body, etag, now - ICON_CACHE_TTL_SECONDS + RETRY_AFTER_SECONDS)
    return body


async def catalog_icon(name: str) -> bytes | None:
    """The community catalog's icon for ``name`` (None = no icon), cached for a day."""
    global _registry_unavailable_until

    now = time.monotonic()
    entry = _cache.get(name)
    if entry is not None and entry.fresh(now):
        return entry.body
    if config.OTODOCK_AIR_GAPPED:
        return entry.body if entry else None

    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        entry = _cache.get(name)
        if entry is not None and entry.fresh(now):
            return entry.body
        if now < _registry_unavailable_until:
            return entry.body if entry else None

        try:
            registry = await community_catalog.fetch_registry()
        except Exception as exc:
            _registry_unavailable_until = now + RETRY_AFTER_SECONDS
            logger.warning("Community registry unavailable for icons (%s); retry in %ss",
                           exc, RETRY_AFTER_SECONDS)
            return entry.body if entry else None

        catalog_entry = next(
            (e for e in registry.get("mcps", []) if isinstance(e, dict) and e.get("name") == name),
            None,
        )
        folder = catalog_folder((catalog_entry or {}).get("icon_url"))
        if folder is None:
            _cache[name] = _IconEntry(None, None, now)
            return None

        url = f"{community_catalog.MCP_RAW_BASE}/{folder}/icon.png"
        headers: dict[str, str] = {}
        if entry is not None and entry.body is not None and entry.etag:
            headers["If-None-Match"] = entry.etag
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Icon fetch failed for %s (%s)", name, exc)
            return _stale_for_a_while(name, entry, now)

        if resp.status_code == 304 and entry is not None and entry.body is not None:
            entry.fetched_at = now
            return entry.body
        if resp.status_code == 200:
            problem = png_problem(resp.content)
            if problem is None:
                _cache[name] = _IconEntry(resp.content, resp.headers.get("etag"), now)
                return resp.content
            logger.warning("Catalog icon for %s rejected: %s", name, problem)
            _cache[name] = _IconEntry(None, None, now)
            return None
        if resp.status_code == 404:
            _cache[name] = _IconEntry(None, None, now)
            return None
        logger.warning("Icon fetch for %s returned HTTP %s", name, resp.status_code)
        return _stale_for_a_while(name, entry, now)


def clear_cache() -> None:
    """Forget every cached icon (tests, and a deliberate refresh)."""
    global _registry_unavailable_until
    _cache.clear()
    _locks.clear()
    _registry_unavailable_until = 0.0
