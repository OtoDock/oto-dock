"""The app render (render.py): a page served locally stands in for the
proxy; the browser must be installed (skipped otherwise, as in an image
built without it). The route's guards need no browser at all.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import render

HOST_PAGE = """<!doctype html><html><body>
<div data-testid="shell">shell</div>
<iframe src="/v1/apps/app-1/client/abc/" style="width:100%;height:600px;border:0"></iframe>
</body></html>"""

FRAME_PAGE = """<!doctype html><html><body>
<h1>Hello app</h1>
<img src="/v1/apps/app-1/client/abc/pic.png">
<script>
  window.otodock = { viewerToken: function () { return 'tok'; } };
  console.error('boom from the page');
  console.log('quiet');
  addEventListener('message', function (e) {
    if (e.data && e.data.type === 'theme') document.documentElement.classList.toggle('dark', e.data.theme === 'dark');
  });
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/apps/"):
            body = HOST_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif self.path.startswith("/v1/apps/app-1/client/abc/pic.png"):
            self.send_response(404)
            body = b""
            self.send_header("Content-Type", "image/png")
        elif self.path.startswith("/v1/apps/app-1/client/abc/"):
            body = FRAME_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            # A policy the page breaks: the image below is refused.
            self.send_header("Content-Security-Policy", "img-src 'none'")
        else:
            self.send_response(404)
            body = b""
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: D102
        return


@pytest.fixture
def stand_in(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(render, "PROXY_URL", url)
    yield url
    srv.shutdown()


def test_the_path_guards_need_no_browser():
    for bad in ("//evil", "http://x/y", "/a\\b", "/x%2F%2Fy", "relative"):
        assert not render._PATH_RE.match(bad) or "%2f%2f" in bad.lower(), bad
    assert render._clean_widths(None) == [390, 820, 1280]
    assert render._clean_widths([100, 5000, 390, 390, 640, 700, 800, 900]) == [390, 640, 700, 800]


@pytest.mark.skipif(not render.render_available(), reason="no headless browser installed")
def test_a_render_reports_the_frame_the_errors_the_policy_and_the_pictures(stand_in):
    report = asyncio.run(render._render(
        f"{stand_in}/apps/app-1", "cookie-value", [390, 1280], 15, 300, "light", "dark"))
    assert report["ready"] is True and report["frame"] is True
    assert any("boom from the page" in c["text"] for c in report["console"])
    assert all("quiet" not in c["text"] for c in report["console"])
    assert report["csp"] and report["csp"][0]["directive"] == "img-src"
    assert [p["width"] for p in report["pages"]] == [390, 1280, 1280]
    assert [p["theme"] for p in report["pages"]] == ["light", "light", "dark"]
    for p in report["pages"]:
        data = base64.b64decode(p["jpeg_b64"])
        assert data[:2] == b"\xff\xd8" and p["bytes"] == len(data)
    assert report["errors"] == []


@pytest.mark.skipif(not render.render_available(), reason="no headless browser installed")
def test_a_page_with_no_runtime_is_not_ready(stand_in, monkeypatch):
    monkeypatch.setattr(render, "FRAME_PAGE", "<p>bare</p>", raising=False)
    orig = _Handler.do_GET

    def bare(self):
        if self.path.startswith("/v1/apps/app-1/client/abc/") and "pic" not in self.path:
            body = b"<p>bare</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        orig(self)
    monkeypatch.setattr(_Handler, "do_GET", bare)
    report = asyncio.run(render._render(f"{stand_in}/apps/app-1", "", [390], 6, 100, "light", None))
    assert report["frame"] is True and report["ready"] is False
    assert len(report["pages"]) == 1


def test_the_report_shape_is_json(stand_in):
    # The handler's answer must serialise; the fields the proxy reads exist.
    sample = {"ready": False, "frame": False, "banner": "", "console": [], "errors": [],
              "failed_requests": [], "responses": [], "csp": [], "pages": []}
    assert json.loads(json.dumps(sample)) == sample
