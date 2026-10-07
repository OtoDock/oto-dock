"""The dashboard's script policy reports (``POST /v1/csp-report``).

The shell carries its policy report-only (``middleware.SHELL_SCRIPT_POLICY``)
until its reports show nothing the dashboard needs is refused. A browser
posts a violation here (``application/csp-report``, or a Reporting API
list), with no credential; the route logs one WARNING per directive,
blocked origin and page path an hour, at most ``_DISTINCT_PER_HOUR`` of
them, drops what a browser extension caused, and answers 204. The body is
capped at 64 KB (``middleware._ROUTE_CAPS``) and nothing is stored.
"""

from __future__ import annotations

import json
import re
import logging
import time
from urllib.parse import urlsplit

from fastapi import Request, Response

from api.auth._router import router

logger = logging.getLogger("claude-proxy")

_WINDOW_S = 3600.0
_DISTINCT_PER_HOUR = 200
_EXTENSION_SCHEMES = ("chrome-extension", "moz-extension", "safari-web-extension",
                      "safari-extension", "edge-extension")
_seen: dict[tuple[str, str, str], float] = {}
_window = {"started": 0.0, "suppressed": 0}


def _origin_of(value: str) -> str:
    """A URL's origin (``inline``/``eval``-style keywords as they are)."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value[:80]
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return value[:80]


def _reports(raw: bytes) -> list[dict]:
    try:
        doc = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return []
    if isinstance(doc, dict) and isinstance(doc.get("csp-report"), dict):
        return [doc["csp-report"]]
    if isinstance(doc, list):
        return [e["body"] for e in doc
                if isinstance(e, dict) and e.get("type") == "csp-violation"
                and isinstance(e.get("body"), dict)]
    return []


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _field(report: dict, *names: str) -> str:
    """A report field as a single line (an anonymous sender writes these)."""
    for name in names:
        value = report.get(name)
        if isinstance(value, str) and value:
            return _CONTROL.sub("?", value[:2048])
    return ""


def _note(report: dict, now: float) -> None:
    blocked = _field(report, "blocked-uri", "blockedURL")
    source = _field(report, "source-file", "sourceFile")
    if any(v.split(":", 1)[0] in _EXTENSION_SCHEMES for v in (blocked, source) if v):
        return
    directive = _field(report, "effective-directive", "effectiveDirective",
                       "violated-directive")[:60]
    page = urlsplit(_field(report, "document-uri", "documentURL")).path[:120] or "?"
    key = (directive, _origin_of(blocked), page)
    if now - _window["started"] > _WINDOW_S:
        if _window["suppressed"]:
            logger.warning("CSP report-only: %d more violations in the last hour were not logged",
                           _window["suppressed"])
        _window.update(started=now, suppressed=0)
        _seen.clear()
    if key in _seen:
        return
    if len(_seen) >= _DISTINCT_PER_HOUR:
        _window["suppressed"] += 1
        return
    _seen[key] = now
    line = report.get("line-number", report.get("lineNumber", ""))
    line = line if isinstance(line, int) and not isinstance(line, bool) else ""
    logger.warning("CSP report-only: %s would block %s on %s (source %s%s)",
                   directive or "?", key[1] or "?", page,
                   _origin_of(source) if source else "?", f":{line}" if line else "")


@router.post("/v1/csp-report", include_in_schema=False)
async def csp_report(request: Request) -> Response:
    raw = await request.body()
    now = time.monotonic()
    for report in _reports(raw)[:20]:
        _note(report, now)
    return Response(status_code=204)
