"""The rendered check of an app (APPS.md "Deploy pipeline").

A deploy, a check and ``screenshot_app`` look at the page the way a viewer
would: the tree to judge (the release copy that ships, or a scratch copy of
the working tree) runs as the app's ``check`` instance on scratch data, a
render principal (``auth/render_principal.py``) is minted for it, and
file-tools' headless browser loads the real dashboard page at phone, tablet
and desktop widths plus the other theme. What comes back is the verdict
with its reasons, the page's own errors, what the policy blocked, and the
pictures, which the tools hand to the agent as images. The live database is
never touched: the check instance's data is a copy (when it is small) or
empty, and the instance is stopped when the job ends. Without a renderer
(an older file-tools image, the container down) the verdict is
``unavailable`` and nothing is refused for it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from auth import render_principal as rp
from services.apps import app_sandbox, app_supervisor, releases

logger = logging.getLogger("claude-proxy.apps")

WIDTHS = [390, 820, 1280]
IMAGE_NAMES = {390: "phone", 820: "tablet", 1280: "desktop"}
DB_SEED_MAX_BYTES = 64 * 1024 * 1024
RENDER_TIMEOUT_S = 20
CALL_TIMEOUT_S = 45.0
PROBE_TTL_S = 60.0
HARD_CSP = frozenset({"script-src", "connect-src", "style-src"})
OVERFLOW_TOLERANCE_PX = 8   # a scrollbar's width, never a layout
CHECKS_DIRNAME = "checks"
STALE_CHECK_S = 3600

_probe: dict[str, object] = {"at": 0.0, "ok": False, "reason": "", "base": ""}
_ROUTE_RE = re.compile(r"/v1/apps/[^/?#]+/(api|client)/([^?#]*)")
# file-tools renders one page at a time and answers any other with a 503,
# which would read as "no renderer" and let a deploy skip its check: the
# jobs queue here instead.
_renderer_slot = asyncio.Lock()


@dataclass
class RenderReport:
    status: str = "unavailable"   # ok | soft | hard | partial | unavailable
    reason: str = ""
    ready: bool = False
    banner: str = ""
    console: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    csp: list = field(default_factory=list)
    failed_requests: list = field(default_factory=list)
    responses: list = field(default_factory=list)
    hard: list = field(default_factory=list)
    soft: list = field(default_factory=list)
    images: list = field(default_factory=list)   # {name, width, theme, bytes, jpeg_b64}
    seeded: bool = False
    egress: bool = True
    ms: int = 0

    def as_dict(self, *, images: bool = True) -> dict:
        out = asdict(self)
        out["summary"] = self.summary()
        if not images:
            out["images"] = [{k: v for k, v in im.items() if k != "jpeg_b64"} for im in self.images]
        return out

    def summary(self) -> str:
        """The verdict in words, for the tool's text."""
        if self.status == "unavailable":
            return (f"The rendered check was not available ({self.reason}); the page was not "
                    "looked at, the static checks alone decided.")
        pictures = f"{len(self.images)} picture(s) attached" if self.images else "no picture"
        if self.status == "hard":
            return ("The page FAILED when rendered: " + "; ".join(self.hard) + f". ({pictures}.)")
        if self.status == "partial":
            return ("Rendered before approval: the server runs only after the user approves the "
                    f"manifest, so the static page was captured ({pictures})."
                    + (" Worth fixing: " + "; ".join(self.soft) if self.soft else ""))
        if self.status == "soft":
            return (f"Rendered with warnings ({pictures}): " + "; ".join(self.soft)
                    + ". " + _VIEWER_NOTE)
        return (f"Rendered clean at phone, tablet and desktop widths and in the other theme "
                f"({pictures}). " + _VIEWER_NOTE)


# The render's viewer is synthetic: its feeds are empty and its buttons are
# answered by the frame ("not available in the rendered check") without a
# call, so the pictures show the page's empty and refused states — which is
# what the author should judge.
_VIEWER_NOTE = ("The render viewer is a synthetic member of the agent: personal feeds are "
                "empty and every button answers 'not available in the rendered check', so "
                "the pictures show the empty and refused states — check they read well.")


# ── the renderer ────────────────────────────────────────────────────────────


def renderer_base_url() -> str:
    """Where file-tools answers (its manifest's host and port), "" when the
    MCP is not on this install."""
    from core.config import deployment
    from services.mcp import mcp_registry
    m = mcp_registry.get_manifest("file-tools")
    srv = getattr(m, "server", None) if m else None
    if srv is None:
        return ""
    port = int(getattr(srv, "port", 0) or 0) or 8932
    return f"http://{deployment.docker_mcp_host(m)}:{port}"


async def renderer_available() -> tuple[bool, str]:
    """A cached probe of file-tools' ``/health``: (available, reason)."""
    now = time.monotonic()
    if now - float(_probe["at"]) < PROBE_TTL_S:
        return bool(_probe["ok"]), str(_probe["reason"])
    base = await asyncio.to_thread(renderer_base_url)
    ok, reason = False, ""
    if not base:
        reason = "file-tools is not installed on this platform"
    else:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{base}/health")
            body = r.json() if r.status_code == 200 else {}
            ok = bool(body.get("render"))
            reason = "" if ok else "file-tools has no browser (an older image; pull the release's)"
        except (httpx.HTTPError, ValueError):
            reason = "file-tools did not answer"
    _probe.update({"at": now, "ok": ok, "reason": reason, "base": base})
    return ok, reason


async def _call_renderer(base: str, token: str, path: str, *, timeout_s: int) -> dict:
    """One ``POST /render`` on file-tools; raises ``RuntimeError`` with the
    reason when the renderer refuses or fails."""
    async with httpx.AsyncClient(timeout=CALL_TIMEOUT_S) as client:
        r = await client.post(f"{base}/render", json={
            "path": path, "cookie": token, "widths": WIDTHS, "timeout_s": timeout_s,
            "settle_ms": 1500, "theme": "light", "second_theme": "dark",
        }, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        try:
            reason = str(r.json().get("reason") or "")
        except ValueError:
            reason = ""
        raise RuntimeError(reason or f"the renderer answered {r.status_code}")
    return r.json()


# ── the job ─────────────────────────────────────────────────────────────────


def viewer_sub_for(row: dict) -> str:
    """A personal app renders as its owner, a shared app as a synthetic
    viewer of the agent (never a person's inbox in a picture)."""
    if row.get("username"):
        return row.get("owner_sub") or rp.synthetic_sub(row["id"])
    return rp.synthetic_sub(row["id"])


def copy_working_tree(row: dict, source_dir: Path) -> Path:
    """A scratch copy of a working tree under the row's release root
    (``check-<nonce>``, with a manifest so the client routes find it by its
    hash); stale ones from a crashed job are swept first."""
    from services.apps.app_deploy import confine_to_scope
    confine_to_scope(row["agent"], row.get("username") or "", source_dir)
    base = releases.app_release_dir(row)
    base.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in base.glob(f"{releases.CHECK_DIR_PREFIX}*"):
        try:
            if now - old.stat().st_mtime > STALE_CHECK_S:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    dest = base / f"{releases.CHECK_DIR_PREFIX}{secrets.token_hex(4)}"
    try:
        releases.copy_tree(source_dir, dest)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _seed(row: dict, scratch: Path) -> bool:
    """The live database copied into the scratch data directory when it is
    small enough; False when there is none or it is too big to copy."""
    from services.apps.app_deploy import snapshot_db
    live = releases.app_data_dir(row) / "app.db"
    try:
        if not live.is_file() or live.stat().st_size > DB_SEED_MAX_BYTES:
            return False
        return snapshot_db(live, scratch / "app.db")
    except Exception:  # noqa: BLE001 — a seed is a nicety
        return False


def judge(raw: dict, report: RenderReport, *, approved: bool) -> RenderReport:
    """The verdict from the renderer's answer (APPS.md "Deploy pipeline")."""
    report.ready = bool(raw.get("ready"))
    report.banner = str(raw.get("banner") or "")
    report.console = list(raw.get("console") or [])
    report.errors = list(raw.get("errors") or [])
    report.csp = list(raw.get("csp") or [])
    report.failed_requests = list(raw.get("failed_requests") or [])
    report.responses = list(raw.get("responses") or [])
    report.ms = int(raw.get("ms") or 0)
    hard: list[str] = []
    soft: list[str] = []
    unapproved = not approved
    if not report.ready:
        if unapproved or "approval" in report.banner.lower():
            pass  # the server cannot run yet; the static page is what there is
        elif not raw.get("frame"):
            hard.append("the app's frame never loaded")
        else:
            hard.append("the page never became ready" + (f" ({report.banner})" if report.banner else ""))
    for e in report.errors:
        hard.append(f"an uncaught error at load: {str(e)[:200]}")
    for v in report.csp:
        d = str(v.get("directive") or "")
        text = str(v.get("text") or "")[:200]
        (hard if d in HARD_CSP else soft).append(f"the policy blocked {d}: {text}")
    for r in report.responses:
        url, status = str(r.get("url") or ""), int(r.get("status") or 0)
        # The route is the segment after the app id: the app's own API may
        # have a /client/ path of its own, which is no asset of the page.
        m = _ROUTE_RE.search(url)
        route, rest = (m.group(1), m.group(2)) if m else ("", "")
        if route == "client" and status >= 400:
            hard.append(f"an asset of the page answered {status}: {rest[:120]}")
        elif route == "api" and status >= 500 and not r.get("server"):
            hard.append(f"the app's API answered {status} on {rest[:120]}")
        elif route == "api" and status >= 400 and not r.get("server"):
            soft.append(f"the app's API answered {status} on {rest[:120]}")
    for c in report.console:
        soft.append(f"console.{c.get('level', 'error')}: {str(c.get('text') or '')[:200]}")
    for f in report.failed_requests:
        soft.append(f"a request failed: {str(f.get('url') or '')[:120]} ({f.get('reason', '')})")
    # A page wider than its viewport scrolls sideways on a phone — a table
    # or a long line that does not fit. The picture is the visible slice
    # and cannot show it; the renderer measures it per width (found on the
    # developer dashboard's tasks table on a phone, 2026-09-15).
    for p in raw.get("pages") or []:
        over = int(p.get("overflow") or 0)
        if over > OVERFLOW_TOLERANCE_PX:
            w = int(p.get("width") or 0)
            soft.append(f"the page runs {over} px past the edge at {IMAGE_NAMES.get(w, str(w))} width "
                        f"({w} px): something does not fit — let it wrap, or scroll it inside its own box")
    report.hard, report.soft = hard, soft
    if hard:
        report.status = "hard"
    elif not report.ready and (unapproved or "approval" in report.banner.lower()):
        report.status = "partial"
    elif soft:
        report.status = "soft"
    else:
        report.status = "ok"
    return report


def _store(row: dict, report: RenderReport, raw: dict) -> None:
    """The pictures and the report beside the releases, for the record."""
    d = releases.app_release_dir(row) / CHECKS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    for p in raw.get("pages") or []:
        w, theme = int(p.get("width") or 0), str(p.get("theme") or "light")
        name = IMAGE_NAMES.get(w, str(w)) + ("" if theme == "light" else f"-{theme}")
        b64 = str(p.get("jpeg_b64") or "")
        try:
            (d / f"{name}.jpg").write_bytes(base64.b64decode(b64))
        except (OSError, ValueError):
            continue
        report.images.append({"name": name, "width": w, "theme": theme,
                              "bytes": int(p.get("bytes") or 0), "jpeg_b64": b64,
                              "overflow": int(p.get("overflow") or 0)})
    with contextlib.suppress(OSError):
        (d / "report.json").write_text(json.dumps(report.as_dict(images=False), indent=1), "utf-8")


async def render_tree(row: dict, tree_dir: Path, *, approved: bool) -> RenderReport:
    """Render ``tree_dir`` (a release copy or a scratch copy under the row's
    release root) as the row's check instance. The caller holds the deploy
    lock."""
    report = RenderReport()
    ok, reason = await renderer_available()
    if not ok:
        report.reason = reason
        return report
    base = str(_probe["base"])
    sha = await asyncio.to_thread(releases.tree_sha, tree_dir)
    if not sha:
        report.reason = "the tree has no manifest"
        return report
    async with _renderer_slot:
        scratch = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="otodock-app-render-"))
        report.seeded = await asyncio.to_thread(_seed, row, scratch)
        report.egress = approved
        token, jti = rp.mint(row["id"], viewer_sub_for(row), row.get("agent") or "")
        started = False
        try:
            if app_sandbox.server_entry(tree_dir):
                try:
                    await app_supervisor.start(row, "check", release_dir=tree_dir, data_dir=scratch,
                                               allow_hosts=None if approved else [])
                    started = True
                except (app_sandbox.AppStartError, app_supervisor.AppUnavailable) as e:
                    report.status = "hard"
                    report.hard = [f"the server did not start: {e}"]
                    return report
            else:
                await app_supervisor.start(row, "check", release_dir=tree_dir, data_dir=scratch)
                started = True
            path = f"/apps/{row['id']}?render={sha}"
            raw: dict | None = None
            last = ""
            for attempt in range(2):
                try:
                    raw = await _call_renderer(base, token, path, timeout_s=RENDER_TIMEOUT_S)
                except (httpx.HTTPError, RuntimeError, ValueError) as e:
                    last = str(e) or type(e).__name__
                    raw = None
                    break
                if raw.get("ready") or attempt == 1:
                    break
                # One retry of the whole load before "never ready" counts.
            if raw is None:
                report.status = "unavailable"
                report.reason = f"the renderer failed: {last}"
                _probe["at"] = 0.0
                return report
            judge(raw, report, approved=approved)
            await asyncio.to_thread(_store, row, report, raw)
            logger.info("App render: app=%s status=%s ready=%s ms=%d", row.get("slug"), report.status,
                        report.ready, report.ms)
            return report
        finally:
            rp.release(jti)
            if started:
                await app_supervisor.stop(row["id"], "check")
            await asyncio.to_thread(shutil.rmtree, scratch, True)


async def render_working_tree(row: dict, source_dir: Path, *, approved: bool) -> RenderReport:
    """``check_app`` and ``screenshot_app(source="working")``: a scratch copy
    of the working tree, rendered and removed. Takes the deploy lock."""
    from services.apps.app_deploy import _deploy_lock
    async with _deploy_lock(row["id"]):
        tree = await asyncio.to_thread(copy_working_tree, row, source_dir)
        try:
            return await render_tree(row, tree, approved=approved)
        finally:
            await asyncio.to_thread(shutil.rmtree, tree, True)


async def render_live(row: dict, *, approved: bool) -> RenderReport:
    """``screenshot_app(source="live")``: the live release, as a check
    instance on scratch data. Takes the deploy lock."""
    from services.apps.app_deploy import _deploy_lock
    async with _deploy_lock(row["id"]):
        try:
            live = await asyncio.to_thread(releases.live_release_dir, row)
        except releases.ReleaseDamaged:
            live = None
        if live is None:
            report = RenderReport()
            report.reason = "the app has no release yet"
            return report
        return await render_tree(row, live, approved=approved)
