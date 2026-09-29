"""The headless render of an app page for the platform's checks (APPS.md
"Deploy pipeline").

This container holds the only browser a plain install has, so the proxy
asks it to load an app the way a viewer would and say what happened:
whether the frame came up, what the page logged, what the policy blocked,
what failed to load, and a picture at each width. The request's bearer is
the render token the proxy minted (the same one the page's cookie
carries); it is verified with one call back to the proxy before a browser
starts, so no key lives here and a sibling container gains nothing from the
route. The target is always ``PROXY_URL`` plus a path: no other host is
ever loaded, and the page's own requests to any other host are refused.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from shared import PROXY_URL

logger = logging.getLogger("file-tools.render")

COOKIE_NAME = "otodock_render"
MAX_WIDTHS = 4
WIDTH_MIN, WIDTH_MAX = 320, 2560
SETTLE_MAX_MS = 5000
TIMEOUT_MAX_S = 30
CAPTURE_MAX_PX = 4000
IMAGE_MAX_BYTES = 2 * 1024 * 1024
JPEG_QUALITY = 80
HEIGHT_BY_WIDTH = {390: 844, 820: 1180}
DEFAULT_HEIGHT = 800
_PATH_RE = re.compile(r"^/(?!/)[^\\\s]*$")
_CSP_RE = re.compile(r"Content Security Policy directive: \"([a-z-]+)")

# One browser at a time: the container shares its memory with the document
# tools, and a render is seconds long.
_slots = asyncio.Semaphore(int(os.environ.get("RENDER_CONCURRENCY", "1")))


def render_available() -> bool:
    """Playwright importable and a Chromium headless shell installed."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    try:
        return any(p.name.startswith(("chromium_headless_shell", "chromium-")) for p in root.iterdir())
    except OSError:
        return False


async def _verify(bearer: str) -> bool:
    """The proxy says whether this render token is live."""
    if not bearer or not PROXY_URL:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{PROXY_URL}/v1/internal/render/verify",
                                 headers={"Authorization": f"Bearer {bearer}"})
        return r.status_code == 204
    except httpx.HTTPError:
        return False


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def _clean_widths(raw) -> list[int]:
    widths: list[int] = []
    for w in raw if isinstance(raw, list) else []:
        try:
            n = int(w)
        except (TypeError, ValueError):
            continue
        if WIDTH_MIN <= n <= WIDTH_MAX and n not in widths:
            widths.append(n)
    return widths[:MAX_WIDTHS] or [390, 820, 1280]


async def handle_render(request: Request) -> JSONResponse:
    """``POST /render`` ``{path, cookie, widths?, timeout_s?, settle_ms?,
    theme?, second_theme?}``: the report and the pictures, or 401 / 400 /
    503 with a reason."""
    if not render_available():
        return JSONResponse({"reason": "no browser in this image"}, status_code=503)
    token = _bearer(request)
    if not await _verify(token):
        return JSONResponse({"reason": "the render token was refused"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"reason": "a JSON body is required"}, status_code=400)
    path = str(body.get("path") or "")
    if not _PATH_RE.match(path) or "%2f%2f" in path.lower() or "%5c" in path.lower():
        return JSONResponse({"reason": "path must be a path on the proxy"}, status_code=400)
    url = PROXY_URL.rstrip("/") + path
    if urlsplit(url).netloc != urlsplit(PROXY_URL).netloc:
        return JSONResponse({"reason": "path must be a path on the proxy"}, status_code=400)
    cookie = str(body.get("cookie") or "")
    widths = _clean_widths(body.get("widths"))
    timeout_s = max(5, min(TIMEOUT_MAX_S, int(body.get("timeout_s") or 20)))
    settle_ms = max(0, min(SETTLE_MAX_MS, int(body.get("settle_ms") or 1500)))
    theme = "dark" if body.get("theme") == "dark" else "light"
    second = body.get("second_theme")
    second = second if second in ("light", "dark") and second != theme else None
    if _slots.locked():
        return JSONResponse({"reason": "a render is in progress; try again in a moment"},
                            status_code=503, headers={"Retry-After": "5"})
    async with _slots:
        started = time.monotonic()
        try:
            report = await asyncio.wait_for(
                _render(url, cookie, widths, timeout_s, settle_ms, theme, second),
                timeout=timeout_s + 20,
            )
        except asyncio.TimeoutError:
            return JSONResponse({"reason": f"the render did not finish in {timeout_s + 20}s"},
                                status_code=504)
        except Exception as e:  # noqa: BLE001 — the reason is the answer
            logger.warning("render of %s failed: %s", path, e)
            return JSONResponse({"reason": f"the browser failed: {type(e).__name__}"},
                                status_code=500)
        report["ms"] = int((time.monotonic() - started) * 1000)
        logger.info("render %s widths=%s ready=%s ms=%d", path, widths, report.get("ready"), report["ms"])
        return JSONResponse(report)


def _app_frame(page):
    for f in page.frames:
        if "/v1/apps/" in f.url and ("/client/" in f.url or "/html" in f.url):
            return f
    return None


async def _state(page, frame) -> tuple[bool, str]:
    """(the frame holds a viewer token, the host's banner text if any)."""
    try:
        tok = await frame.evaluate(
            "() => (window.otodock && typeof window.otodock.viewerToken === 'function')"
            " ? (window.otodock.viewerToken() ? 1 : 0) : -1")
        banner = await page.evaluate(
            "() => { const b = document.querySelector('[data-testid=\"app-server-banner\"]');"
            " return b ? b.textContent : ''; }")
    except Exception:  # noqa: BLE001 — a navigating frame throws mid-evaluate
        return False, ""
    return tok == 1, (banner or "").strip()


_OVERFLOW_JS = ("() => Math.max(0, document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth)")


async def _capture(page, width: int, height: int, tag: str, frame=None) -> dict:
    await page.set_viewport_size({"width": width, "height": height})
    await asyncio.sleep(0.4)
    frame_el = page.locator("iframe").first
    box = None
    try:
        box = await frame_el.bounding_box()
    except Exception:  # noqa: BLE001 — no frame yet
        box = None
    clip = None
    if box and box["width"] > 0 and box["height"] > 0:
        clip = {"x": box["x"], "y": box["y"], "width": box["width"],
                "height": min(box["height"], CAPTURE_MAX_PX)}
    # How far the app's document runs past its own edge at this width: a
    # table or a long line that does not fit makes the page scroll
    # sideways, which a picture alone does not show (the phone's capture
    # is the visible slice). The judge words it; the author lets it wrap.
    overflow = 0
    for doc in (frame, page):
        if doc is None:
            continue
        with contextlib.suppress(Exception):  # a document mid-navigation
            overflow = max(overflow, int(await doc.evaluate(_OVERFLOW_JS) or 0))
    data = await page.screenshot(type="jpeg", quality=JPEG_QUALITY, clip=clip)
    if len(data) > IMAGE_MAX_BYTES:
        data = await page.screenshot(type="jpeg", quality=45, clip=clip)
    return {"width": width, "height": height, "theme": tag, "overflow": overflow,
            "jpeg_b64": base64.b64encode(data).decode("ascii"), "bytes": len(data)}


async def _render(url: str, cookie: str, widths: list[int], timeout_s: int, settle_ms: int,
                  theme: str, second: str | None) -> dict:
    from playwright.async_api import async_playwright
    origin = urlsplit(url)
    origin_prefix = f"{origin.scheme}://{origin.netloc}"
    console: list[dict] = []
    errors: list[str] = []
    failed: list[dict] = []
    bad: list[dict] = []
    csp: list[dict] = []

    def in_app(frame) -> bool:
        try:
            return "/v1/apps/" in frame.url
        except Exception:  # noqa: BLE001
            return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, chromium_sandbox=False,
                                          args=["--disable-dev-shm-usage"])
        try:
            first_w = widths[0]
            context = await browser.new_context(
                viewport={"width": first_w, "height": HEIGHT_BY_WIDTH.get(first_w, DEFAULT_HEIGHT)},
                color_scheme=theme, ignore_https_errors=True,
            )
            if cookie:
                await context.add_cookies([{"name": COOKIE_NAME, "value": cookie, "url": origin_prefix,
                                            "httpOnly": True, "secure": False}])

            async def gate(route, request):
                if request.url.startswith(origin_prefix + "/"):
                    await route.continue_()
                else:
                    await route.abort("blockedbyclient")
            await context.route("**/*", gate)
            page = await context.new_page()

            def on_console(msg):
                if msg.type not in ("error", "warning"):
                    return
                loc = msg.location or {}
                where = str(loc.get("url") or "")
                if where and "/v1/apps/" not in where:
                    return
                text = msg.text
                row = {"level": msg.type, "text": text[:500], "line": loc.get("lineNumber")}
                m = _CSP_RE.search(text)
                if m:
                    csp.append({"directive": m.group(1), "text": text[:500]})
                else:
                    console.append(row)
            page.on("console", on_console)
            page.on("pageerror", lambda e: errors.append(str(e)[:500]))

            def on_failed(req):
                if in_app(req.frame):
                    failed.append({"url": req.url[:300], "reason": str(req.failure or "")[:120]})
            page.on("requestfailed", on_failed)

            def on_response(resp):
                if resp.status >= 400 and in_app(resp.request.frame):
                    bad.append({"url": resp.url[:300], "status": resp.status,
                                "server": resp.headers.get("x-otodock-server", "")})
            page.on("response", on_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            deadline = time.monotonic() + timeout_s
            frame = None
            ready, banner = False, ""
            while time.monotonic() < deadline:
                frame = frame or _app_frame(page)
                if frame is not None:
                    ready, banner = await _state(page, frame)
                    if ready:
                        break
                await asyncio.sleep(0.25)
            await asyncio.sleep(settle_ms / 1000)
            if frame is not None:
                _r, banner = await _state(page, frame)
            pages: list[dict] = []
            for w in widths:
                pages.append(await _capture(page, w, HEIGHT_BY_WIDTH.get(w, DEFAULT_HEIGHT), theme, frame))
            if second and frame is not None:
                # The runtime flips its own document's theme on this message;
                # the host page stays as it is, and the capture is the frame.
                try:
                    await frame.evaluate(
                        "(t) => window.postMessage({source: 'otodock-host', type: 'theme', theme: t}, '*')",
                        second)
                    await asyncio.sleep(0.6)
                    pages.append(await _capture(page, widths[-1], HEIGHT_BY_WIDTH.get(widths[-1], DEFAULT_HEIGHT), second, frame))
                except Exception:  # noqa: BLE001 — a second theme is a bonus
                    pass
            return {"ready": ready, "frame": frame is not None, "banner": banner,
                    "console": console[:50], "errors": errors[:20], "failed_requests": failed[:50],
                    "responses": bad[:50], "csp": csp[:50], "pages": pages}
        finally:
            await browser.close()
