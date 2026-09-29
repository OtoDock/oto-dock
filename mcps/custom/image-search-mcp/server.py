"""Image Search MCP Server — web image search, reverse-image search via
Google Lens (SerpAPI), and save-to-workspace, all over stdio.

Three tools:
  - find_images:       Unsplash + Pexels + Google CSE (parallel fan-out)
  - search_by_image:   SerpAPI Google Lens (requires the platform's
                       /v1/images/temp/* endpoint, since Lens only accepts
                       URLs — not base64 or file uploads)
  - save_image:        HTTP(S) download → scope-correct workspace path (the
                       host and every redirect hop must answer with a
                       public address — the vendored ``_url_guard``)

Cost is declared in the manifest's `costs` block and evaluated by the proxy
at TOOL_RESULT time — this server does NOT report cost itself. Stock
providers (Unsplash, Pexels) are free; Google CSE + SerpAPI are paid.

Graceful no-config: each tool checks the env at call time and returns an
admin-actionable {"error": "..."} message when its required keys aren't
set. The MCP installs cleanly with zero configuration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from urllib.parse import urlparse

import httpx
from PIL import Image
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from _url_guard import validate_outbound_url

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
SESSION_ID = os.environ.get("OTO_SESSION_ID", "")

UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

IMAGE_WORKSPACE = os.environ.get("IMAGE_WORKSPACE", "")


def _vendor_get_args(vendor: str, path: str, params: dict) -> tuple[str, dict, dict]:
    """Build (url, headers, params) for a GET to ``vendor`` + ``path`` using the
    admin-configured (bring-your-own) key, per the vendor's own auth scheme.
    """
    params = dict(params)
    if vendor == "unsplash":
        return (f"https://api.unsplash.com{path}",
                {"Authorization": f"Client-ID {UNSPLASH_KEY}"}, params)
    if vendor == "pexels":
        return f"https://api.pexels.com{path}", {"Authorization": PEXELS_KEY}, params
    if vendor == "serpapi":
        params["api_key"] = SERPAPI_KEY
        return f"https://serpapi.com{path}", {}, params
    raise ValueError(f"unknown vendor: {vendor}")


def _has_stock() -> bool:
    return bool(UNSPLASH_KEY or PEXELS_KEY)


# SerpAPI is the gateway for BOTH `find_images` Google Images results AND
# `search_by_image` Google Lens results. Google's own Programmable Search
# Engine API can no longer enable "search the entire web" for new engines
# (deprecated by Google late 2024 / early 2025), so site-restricted CSEs
# are the only thing it can produce — useless for general image search.
# SerpAPI's `google_images` engine returns the actual full Google Image
# Search results across the entire web at ~$0.01 per query.
def _has_google() -> bool:
    return bool(SERPAPI_KEY)


def _has_serpapi() -> bool:
    return bool(SERPAPI_KEY)


# Limits
MAX_SAVE_BYTES = 15 * 1024 * 1024  # 15 MB hard cap for save_image downloads
SAVE_CHUNK_SIZE = 64 * 1024
MAX_REDIRECTS = 5                  # save_image follows redirects by hand, re-checking each hop
DEFAULT_SAVE_SUBDIR = "images"     # auto-saved files land under workspace/images/
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("image-search")


# ---------------------------------------------------------------------------
# Server + tool surface
# ---------------------------------------------------------------------------

server = Server("image-search")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="find_images",
            description=(
                "Search the web for images matching a query. Returns URL "
                "candidates with attribution + source-page links. The agent "
                "picks the best 1-4 and passes them to display-mcp's "
                "`display_images` tool to render inline. Three providers fan "
                "out in parallel: Unsplash (free, stock/scenic), Pexels (free, "
                "stock/lifestyle), Google Custom Search (paid, broad web). "
                "Use `prefer_provider=\"stock\"` to stay free; `\"google\"` "
                "for paid specifics (people / news / products); `\"all\"` "
                "(default) for best coverage with Google billed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for (e.g. 'Iceland landscape').",
                    },
                    "count": {
                        "type": "integer",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Max images returned (1-20).",
                    },
                    "prefer_provider": {
                        "type": "string",
                        "enum": ["stock", "google", "all"],
                        "default": "all",
                        "description": (
                            "stock = Unsplash+Pexels only (free). "
                            "google = Google CSE only (paid). "
                            "all = every configured provider in parallel (Google billed)."
                        ),
                    },
                    "orientation": {
                        "type": "string",
                        "enum": ["", "landscape", "portrait", "square"],
                        "default": "",
                        "description": "Filter by orientation; empty = any.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_by_image",
            description=(
                "Reverse image search via Google Lens (powered by SerpAPI). "
                "Use when the user uploads a photo and asks what it is, where "
                "to buy it, where it appears online, etc. `image_path` can be "
                "relative to the agent's workspace or a sandbox-absolute path "
                "(e.g. /users/{u}/workspace/uploads/photos/sweater.jpg). For "
                "shopping queries pass mode='products' — returns prices + "
                "retailer links the agent should render with display_images "
                "using `link_url` so cards are clickable. Paid."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Path to the image file in the agent's readable scope.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["all", "products", "visual_matches", "exact_matches"],
                        "default": "all",
                        "description": (
                            "products = shopping (prices + retailer links). "
                            "visual_matches = similar-looking images. "
                            "exact_matches = exact-image copies online. "
                            "all = mix."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 30,
                        "description": "Max matches returned (1-30).",
                    },
                },
                "required": ["image_path"],
            },
        ),
        Tool(
            name="save_image",
            description=(
                "Download an image URL and save it to the agent's workspace. "
                "Use only when the user explicitly asks to save an image. "
                "dest_path may be empty (auto-generates images/saved_<uuid>.<ext>), "
                "relative (joined under workspace), or a sandbox-absolute path "
                "under the agent's writable scope. Rejects non-image URLs and "
                "files >15MB. No API keys required — pure HTTP download."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "HTTPS URL of the image to download.",
                    },
                    "dest_path": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Empty = auto-generate. Relative = under workspace. "
                            "Absolute sandbox path = validated to stay inside writable scope."
                        ),
                    },
                    "alt_text": {
                        "type": "string",
                        "default": "",
                        "description": "Optional alt text (reserved for future sidecar files).",
                    },
                },
                "required": ["url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "find_images":
            result = await _tool_find_images(arguments)
        elif name == "search_by_image":
            result = await _tool_search_by_image(arguments)
        elif name == "save_image":
            result = await _tool_save_image(arguments)
        else:
            result = {"error": f"unknown tool: {name}"}
    except Exception as e:
        logger.exception("Tool %s crashed: %s", name, e)
        result = {"error": f"internal error: {e}"}
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# Tool: find_images
# ---------------------------------------------------------------------------

async def _tool_find_images(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    count = max(1, min(int(args.get("count") or 3), 20))
    prefer = (args.get("prefer_provider") or "all").lower()
    orientation = (args.get("orientation") or "").lower()
    if orientation and orientation not in ("landscape", "portrait", "square"):
        orientation = ""

    # Resolve which providers to actually call given `prefer` ∩ configured.
    use_stock = prefer in ("stock", "all") and _has_stock()
    use_google = prefer in ("google", "all") and _has_google()

    if not (use_stock or use_google):
        # Decide which error message helps the admin most.
        if prefer == "stock":
            need = "UNSPLASH_ACCESS_KEY or PEXELS_API_KEY"
        elif prefer == "google":
            need = "SERPAPI_KEY (Google Images is served via SerpAPI's google_images engine; Google's own Custom Search API can no longer search the entire web for new engines)"
        else:
            need = "any of UNSPLASH_ACCESS_KEY, PEXELS_API_KEY, or SERPAPI_KEY"
        return {
            "error": (
                f"Image search not configured for prefer_provider='{prefer}'. "
                f"Ask an admin to add {need} in the image-search-mcp admin page."
            )
        }

    # Fan out — per-provider count is balanced so a single down provider
    # doesn't starve the result set. Final list is deduped + truncated to `count`.
    per_provider = max(count, 5)
    tasks: list[asyncio.Task] = []
    if use_stock and UNSPLASH_KEY:
        tasks.append(asyncio.create_task(_search_unsplash(query, per_provider, orientation)))
    if use_stock and PEXELS_KEY:
        tasks.append(asyncio.create_task(_search_pexels(query, per_provider, orientation)))
    if use_google:
        tasks.append(asyncio.create_task(_search_google(query, per_provider, orientation)))

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[dict] = []
    seen_urls: set[str] = set()
    errors: list[str] = []
    for res in raw_results:
        if isinstance(res, Exception):
            errors.append(str(res))
            continue
        for hit in res:
            url = hit.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(hit)

    if errors and not merged:
        return {"error": "All image providers failed: " + "; ".join(errors[:3])}
    if errors:
        logger.warning("Some providers failed: %s", errors)

    return {"images": merged[:count]}


async def _search_unsplash(query: str, count: int, orientation: str) -> list[dict]:
    params = {"query": query, "per_page": str(count)}
    if orientation:
        # Unsplash uses 'orientation' with same vocab
        params["orientation"] = orientation
    url, headers, params = _vendor_get_args("unsplash", "/search/photos", params)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    out: list[dict] = []
    for item in (data.get("results") or [])[:count]:
        urls = item.get("urls") or {}
        user = item.get("user") or {}
        out.append({
            "url": urls.get("regular") or urls.get("full") or "",
            "source": "unsplash",
            "attribution": f"Photo by {user.get('name') or 'Unknown'}",
            "source_page": (item.get("links") or {}).get("html") or "",
            "width": item.get("width") or 0,
            "height": item.get("height") or 0,
            "thumbnail_url": urls.get("small") or urls.get("thumb") or "",
        })
    return out


async def _search_pexels(query: str, count: int, orientation: str) -> list[dict]:
    params = {"query": query, "per_page": str(count)}
    if orientation:
        params["orientation"] = orientation  # Pexels uses same vocab
    url, headers, params = _vendor_get_args("pexels", "/v1/search", params)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    out: list[dict] = []
    for item in (data.get("photos") or [])[:count]:
        src = item.get("src") or {}
        out.append({
            "url": src.get("large") or src.get("original") or "",
            "source": "pexels",
            "attribution": f"Photo by {item.get('photographer') or 'Unknown'}",
            "source_page": item.get("url") or "",
            "width": item.get("width") or 0,
            "height": item.get("height") or 0,
            "thumbnail_url": src.get("medium") or src.get("small") or "",
        })
    return out


async def _search_google(query: str, count: int, orientation: str) -> list[dict]:
    """Google Image Search via SerpAPI's `google_images` engine.

    Returns real full-web Google Image Search results. We use SerpAPI here
    instead of Google's native Programmable Search Engine API because Google
    deprecated "search the entire web" for new Programmable Search Engines
    in late 2024 / early 2025 — native CSE can now only do site-restricted
    image search, useless for general queries. SerpAPI ships the same
    Google Image Search results without that restriction.

    Same SERPAPI_KEY as `search_by_image` (Google Lens), same plan quota.
    Pricing: ~$0.01 per call (counted as one SerpAPI search).
    """
    params: dict[str, str] = {
        "engine": "google_images",
        "q": query,
        "safe": "active",
        "ijn": "0",  # results page index — 0 returns the first batch
    }
    # SerpAPI's google_images accepts img_type / img_size but not a clean
    # orientation switch. We pass `tbs=ift:jpg` etc. only when meaningful;
    # for orientation we'd need to filter client-side. Keep it simple for v1.
    url, headers, params = _vendor_get_args("serpapi", "/search", params)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"SerpAPI google_images: {data['error']}")
    out: list[dict] = []
    for item in (data.get("images_results") or [])[:count]:
        # SerpAPI's google_images shape per item:
        #   original / original_width / original_height — full-res image
        #   thumbnail — small preview
        #   title — image title (often the page title)
        #   link — the page where the image lives (NOT the image URL)
        #   source — site display name (e.g. "BBC News")
        out.append({
            "url": item.get("original") or "",
            "source": "google",
            "attribution": item.get("source") or item.get("title") or "",
            "source_page": item.get("link") or "",
            "width": item.get("original_width") or 0,
            "height": item.get("original_height") or 0,
            "thumbnail_url": item.get("thumbnail") or "",
        })
        if len(out) >= count:
            break
    return out


# ---------------------------------------------------------------------------
# Tool: search_by_image (SerpAPI Google Lens)
# ---------------------------------------------------------------------------

def _resolve_image_path(image_path: str) -> tuple[str | None, str | None]:
    """Resolve a relative or absolute image_path to an OS path.

    Returns (abs_path, error). Either abs_path is set OR error is set.

    Absolute paths arrive already-translated to satellite-host form via
    the stdio interceptor (or already-bwrap-mapped on local sandbox), so
    we just normalize + parent-traversal-guard and return.
    Relative paths anchor to ``IMAGE_WORKSPACE``.
    """
    if not image_path:
        return None, "image_path is required"

    workspace = IMAGE_WORKSPACE.rstrip("/") if IMAGE_WORKSPACE else ""

    if os.path.isabs(image_path):
        normalized = os.path.normpath(image_path)
        if ".." in normalized.split("/"):
            return None, "parent-traversal (..) is not allowed"
        return normalized, None

    if not workspace:
        return None, (
            "IMAGE_WORKSPACE not set — cannot resolve relative image_path"
        )
    # Relative path — join under IMAGE_WORKSPACE, no parent escapes.
    normalized = os.path.normpath(image_path)
    if normalized.startswith("..") or normalized == "..":
        return None, "parent-traversal (..) is not allowed"
    return os.path.join(workspace, normalized), None


def _looks_like_login_redirect(location: str) -> bool:
    """Heuristic: is this Location header a reverse-proxy auth gate?"""
    if not location:
        return False
    lc = location.lower()
    return any(kw in lc for kw in (
        "/login", "/auth", "/sso", "/oauth", "/sign", "authentik",
        "authelia", "oauth2-proxy", "access.cloudflare",
    ))


async def _create_temp_url(abs_path: str) -> tuple[str | None, str | None]:
    """Ask the proxy for a tokenized public URL for `abs_path`.

    Returns (url, error). Either url is set OR error is set.
    """
    if not PROXY_URL or not PROXY_API_KEY or not SESSION_ID:
        return None, (
            "PROXY_URL / PROXY_API_KEY / OTO_SESSION_ID not injected — "
            "image-search-mcp is misconfigured."
        )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(
                f"{PROXY_URL.rstrip('/')}/v1/images/temp",
                json={
                    "session_id": SESSION_ID,
                    "abs_path": abs_path,
                    "ttl_seconds": 300,
                },
                headers={
                    "Authorization": f"Bearer {PROXY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                return resp.json().get("url"), None
            return None, f"proxy returned {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return None, f"failed to mint temp URL: {e}"


async def _preflight_temp_url(url: str) -> str | None:
    """Verify the temp URL is reachable WITHOUT auth (as SerpAPI will fetch it).

    Returns None on success, or a clear error message if a reverse-proxy
    auth gate (Authentik / Authelia / oauth2-proxy / CF Access) is
    redirecting to a login page. This catches the most common deployment
    pitfall before we waste a paid SerpAPI call on a URL that won't resolve.
    """
    try:
        # Don't follow redirects — we want to *see* the 302 → /login if it's there.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0), follow_redirects=False, verify=True,
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                ctype = resp.headers.get("content-type", "")
                if ctype.startswith("image/"):
                    return None
                # 200 but not an image — proxy returned an HTML login page
                # with status 200 (some auth setups do this).
                if "html" in ctype.lower():
                    return (
                        "Temp image URL returns an HTML page (not an image) — "
                        "your reverse-proxy is likely gating /v1/images/temp/* "
                        "with auth. Add an unauthenticated-paths bypass for "
                        "^/v1/images/temp/[A-Za-z0-9_-]+$ in Authentik / "
                        "Authelia / oauth2-proxy / CF Access."
                    )
                return f"Temp image URL returned unexpected Content-Type: {ctype}"
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if _looks_like_login_redirect(location):
                    return (
                        f"Temp image URL redirects to a login page "
                        f"(Location: {location[:120]}). Your reverse-proxy "
                        f"(Authentik / Authelia / oauth2-proxy / CF Access) is "
                        f"gating /v1/images/temp/*. Add an unauthenticated-paths "
                        f"bypass for ^/v1/images/temp/[A-Za-z0-9_-]+$."
                    )
                return f"Temp image URL redirects to {location[:120]} (HTTP {resp.status_code})."
            return f"Temp image URL returned HTTP {resp.status_code}."
    except Exception as e:
        return f"Temp image URL pre-flight failed: {e}"


async def _tool_search_by_image(args: dict) -> dict:
    image_path = (args.get("image_path") or "").strip()
    mode = (args.get("mode") or "all").lower()
    count = max(1, min(int(args.get("count") or 10), 30))

    if mode not in ("all", "products", "visual_matches", "exact_matches"):
        return {"error": f"mode must be one of: all, products, visual_matches, exact_matches"}

    if not _has_serpapi():
        return {
            "error": (
                "Reverse image search not configured. Ask an admin to add "
                "SERPAPI_KEY in the image-search-mcp admin page "
                "(https://serpapi.com/manage-api-key — ~$0.01/search)."
            )
        }

    abs_path, err = _resolve_image_path(image_path)
    if err:
        return {"error": err}
    if not os.path.isfile(abs_path):
        return {"error": f"file not found: {image_path}"}

    # Mint the temp URL via the platform.
    temp_url, err = await _create_temp_url(abs_path)
    if err:
        return {"error": err}

    # Pre-flight: catch reverse-proxy auth-gate misconfig with a clear hint.
    preflight_err = await _preflight_temp_url(temp_url)
    if preflight_err:
        return {"error": preflight_err}

    # Call SerpAPI Google Lens.
    params = {
        "engine": "google_lens",
        "url": temp_url,
        "type": mode,
    }
    url, headers, params = _vendor_get_args("serpapi", "/search", params)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                return {
                    "error": (
                        f"SerpAPI returned HTTP {resp.status_code}: "
                        f"{resp.text[:300]}"
                    )
                }
            data = resp.json()
    except Exception as e:
        return {"error": f"SerpAPI request failed: {e}"}

    if data.get("error"):
        return {"error": f"SerpAPI: {data['error']}"}

    matches = _normalize_serpapi_lens(data, mode, count)
    return {"matches": matches}


def _normalize_serpapi_lens(data: dict, mode: str, count: int) -> list[dict]:
    """Flatten SerpAPI Google Lens response into a list[Match] for the agent.

    The Lens response shape varies by `type`:
      - products: data["shopping_results"] = [...] with price + source
      - visual_matches: data["visual_matches"] = [...] with source page
      - exact_matches: data["exact_matches"] = [...] same shape as visual
      - all: any of the above may be present; we union them
    """
    sources: list[list[dict]] = []
    if mode in ("products", "all"):
        sources.append(data.get("shopping_results") or [])
    if mode in ("visual_matches", "all"):
        sources.append(data.get("visual_matches") or [])
    if mode in ("exact_matches", "all"):
        sources.append(data.get("exact_matches") or [])

    out: list[dict] = []
    seen_links: set[str] = set()
    for src_list in sources:
        for raw in src_list:
            link = raw.get("link") or raw.get("source_url") or ""
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            # Extract price + currency when present (shopping_results only)
            price_raw = raw.get("price") or {}
            if isinstance(price_raw, dict):
                price_str = price_raw.get("value") or price_raw.get("extracted_value")
                currency = price_raw.get("currency") or ""
            elif isinstance(price_raw, str):
                price_str = price_raw
                currency = ""
            else:
                price_str = None
                currency = ""
            out.append({
                "title": raw.get("title") or "",
                "link": link,
                "source_url": raw.get("source") or _domain_of(link),
                "source_name": raw.get("source") or _domain_of(link),
                "thumbnail_url": raw.get("thumbnail") or "",
                "price": str(price_str) if price_str else "",
                "currency": currency,
            })
            if len(out) >= count:
                return out
    return out


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Tool: save_image
# ---------------------------------------------------------------------------

_EXT_FROM_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/avif": "avif",
}


def _resolve_save_path(dest_path: str, ext: str) -> tuple[str | None, str | None]:
    """Resolve dest_path to a writable absolute path inside IMAGE_WORKSPACE.

    Mirrors image-gen-mcp's `_get_save_path` semantics: empty → auto-name,
    relative → joined under workspace, absolute sandbox path → validated to
    stay inside workspace. Re-anchors out-of-scope absolute paths to the
    default subdir so accidental escapes (`/etc/passwd`) become safe writes
    under workspace/images/.
    """
    if not IMAGE_WORKSPACE:
        return None, "IMAGE_WORKSPACE not set"
    workspace = IMAGE_WORKSPACE.rstrip("/")
    default_dir = os.path.join(workspace, DEFAULT_SAVE_SUBDIR)

    if not dest_path:
        return os.path.join(default_dir, f"saved_{uuid.uuid4().hex[:8]}.{ext}"), None

    if os.path.isabs(dest_path):
        # Path is already in satellite-host form on remote (stdio
        # interceptor) or sandbox-virtual on local (bwrap maps to host).
        # IMAGE_WORKSPACE is also pre-translated to match.
        host_path = os.path.normpath(dest_path)
        if host_path == workspace or host_path.startswith(workspace + os.sep):
            return host_path, None
        # Re-anchor out-of-scope absolute paths to the default subdir
        # (MCP-level safety so save_image always writes inside workspace
        # even when the LLM picks an external full-FS-admitted path).
        return os.path.join(default_dir, os.path.basename(host_path) or f"saved_{uuid.uuid4().hex[:8]}.{ext}"), None

    # Relative path — block parent escapes, then join.
    normalized = os.path.normpath(dest_path)
    if normalized.startswith("..") or normalized == "..":
        return None, "parent-traversal (..) is not allowed in dest_path"
    return os.path.join(workspace, normalized), None


async def _open_final(client: httpx.AsyncClient, method: str, url: str):
    """Send ``method url`` without following redirects; on a 3xx validate
    the joined ``Location`` and try again, at most ``MAX_REDIRECTS`` hops —
    ``follow_redirects=True`` would let a public URL bounce the fetch into
    the machine's LAN or the loopback tunnel. Returns ``(response, url,
    None)`` with a streaming response the caller must close, or
    ``(None, url, reason)``."""
    for _hop in range(MAX_REDIRECTS + 1):
        resp = await client.send(client.build_request(method, url), stream=True)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp, url, None
        location = resp.headers.get("location", "")
        await resp.aclose()
        if not location:
            return None, url, f"HTTP {resp.status_code} without a Location"
        url = str(httpx.URL(url).join(location))
        reason = validate_outbound_url(url)
        if reason:
            return None, url, f"redirect refused: {reason}"
    return None, url, "too many redirects"


async def _tool_save_image(args: dict) -> dict:
    url = (args.get("url") or "").strip()
    dest_path = (args.get("dest_path") or "").strip()
    # alt_text reserved for future use
    _ = args.get("alt_text", "")

    if not url:
        return {"error": "url is required"}
    if not url.startswith(("http://", "https://")):
        return {"error": "url must be http:// or https://"}
    # The fetch runs in this process — on a satellite with the machine's LAN
    # in reach — so the host must answer with a public address, on every hop.
    if reason := validate_outbound_url(url):
        return {"error": f"url refused: {reason}"}

    # HEAD pre-check: content-type + size. Some hosts refuse HEAD —
    # fall back to allowing the GET if HEAD returns 405/501.
    mime = "application/octet-stream"
    declared_size = 0
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        try:
            head, _final, reason = await _open_final(client, "HEAD", url)
            if reason and reason.startswith("redirect refused"):
                return {"error": reason}
            if head is not None:
                try:
                    if head.status_code in (200, 204):
                        mime = (head.headers.get("content-type") or "").split(";")[0].strip().lower()
                        cl = head.headers.get("content-length")
                        if cl and cl.isdigit():
                            declared_size = int(cl)
                    elif head.status_code in (405, 501):
                        pass  # HEAD not supported — fall through to GET with stream guard
                    else:
                        return {"error": f"URL returned HTTP {head.status_code} on HEAD"}
                finally:
                    await head.aclose()
        except Exception:
            # Treat HEAD failures as "unknown" — proceed to GET with guards.
            pass

    if mime and mime != "application/octet-stream" and not mime.startswith("image/"):
        return {"error": f"URL is not an image (Content-Type: {mime})"}
    if declared_size and declared_size > MAX_SAVE_BYTES:
        return {"error": f"image too large ({declared_size} bytes; max {MAX_SAVE_BYTES})"}

    # Determine an extension for the auto-generated filename case.
    ext = _EXT_FROM_MIME.get(mime, "")
    if not ext:
        # Fall back to URL extension or generic .img.
        url_ext = os.path.splitext(urlparse(url).path)[1].lstrip(".").lower()
        ext = url_ext if url_ext in _EXT_FROM_MIME.values() else "img"

    save_path, err = _resolve_save_path(dest_path, ext)
    if err:
        return {"error": err}

    # Stream download with per-chunk size guard (defends against missing
    # Content-Length headers + malicious senders that lie).
    try:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        tmp_path = save_path + f".tmp.{uuid.uuid4().hex[:6]}"
        total = 0
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
            resp, _final, reason = await _open_final(client, "GET", url)
            if reason:
                return {"error": reason}
            try:
                if resp.status_code != 200:
                    return {"error": f"download HTTP {resp.status_code}"}
                got_ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if got_ctype and not got_ctype.startswith("image/"):
                    return {"error": f"URL is not an image (Content-Type: {got_ctype})"}
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(SAVE_CHUNK_SIZE):
                        total += len(chunk)
                        if total > MAX_SAVE_BYTES:
                            try:
                                f.close()
                                os.remove(tmp_path)
                            except OSError:
                                pass
                            return {
                                "error": (
                                    f"image too large mid-stream (>{MAX_SAVE_BYTES} "
                                    f"bytes; max {MAX_SAVE_BYTES})"
                                )
                            }
                        f.write(chunk)
            finally:
                await resp.aclose()
        os.replace(tmp_path, save_path)
    except Exception as e:
        return {"error": f"download/write failed: {e}"}

    # Extract dimensions + final mime via PIL.
    width = height = 0
    final_mime = mime
    try:
        with Image.open(save_path) as img:
            width, height = img.size
            if not final_mime.startswith("image/") and img.format:
                final_mime = f"image/{img.format.lower()}"
    except Exception:
        pass  # non-fatal — file is saved either way

    return {
        "saved_path": save_path,
        "size_bytes": total,
        "mime_type": final_mime,
        "width": width,
        "height": height,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main():
    logger.info(
        "image-search-mcp starting: unsplash=%s, pexels=%s, serpapi=%s "
        "(google_images + google_lens), workspace=%s",
        bool(UNSPLASH_KEY), bool(PEXELS_KEY), _has_serpapi(),
        IMAGE_WORKSPACE or "(unset)",
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
