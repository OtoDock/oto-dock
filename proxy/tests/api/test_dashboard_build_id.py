"""The dashboard build id: read from dist/index.html, repeated on /health.

The build stamps ``<meta name="otodock-build" content="<id>">`` into the
page; ``static_assets.dashboard_build_id`` reads it back (cached on the
file's mtime/size so a rebuild is seen without a restart) and ``/health``
carries it for pages that have no dashboard socket.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import config
from static_assets import dashboard_build_id

STAMPED = (
    '<!doctype html><html><head><meta charset="UTF-8">'
    '<meta name="otodock-build" content="{id}">'
    '<script type="module" src="/assets/index-AAAAbbbb.js"></script>'
    "</head><body></body></html>"
)


def _write(dist, html: str, mtime_step: int):
    index = dist / "index.html"
    index.write_text(html, encoding="utf-8")
    # A rewrite inside the same clock tick must still be seen: bump mtime.
    os.utime(index, ns=(1_000_000_000 * mtime_step, 1_000_000_000 * mtime_step))


def test_reads_the_stamp_and_follows_a_rebuild(tmp_path):
    assert dashboard_build_id(tmp_path) == ""  # no index yet (a build empties dist)
    _write(tmp_path, STAMPED.format(id="0123456789abcdef"), 1)
    assert dashboard_build_id(tmp_path) == "0123456789abcdef"
    assert dashboard_build_id(tmp_path) == "0123456789abcdef"  # cached
    _write(tmp_path, STAMPED.format(id="fedcba9876543210"), 2)
    assert dashboard_build_id(tmp_path) == "fedcba9876543210"
    (tmp_path / "index.html").unlink()
    assert dashboard_build_id(tmp_path) == ""


@pytest.mark.parametrize("html", [
    "<html><head><title>x</title></head></html>",  # dev-server style page: no stamp
    "<meta content='abc' name='otodock-build'>",   # attribute order + quotes
])
def test_stamp_parsing_edge_cases(tmp_path, html):
    _write(tmp_path, html, 3)
    got = dashboard_build_id(tmp_path)
    assert got == ("abc" if "otodock-build" in html else "")


def test_health_carries_the_build(tmp_path, monkeypatch):
    # Import the app BEFORE pointing the dist at the temp dir: the module
    # mounts <dist>/assets at import time (and skips it when there is no
    # dist); the health route reads the stamp at request time.
    from app import app

    _write(tmp_path, STAMPED.format(id="1111222233334444"), 4)
    monkeypatch.setattr(config, "DASHBOARD_DIST", tmp_path)
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["build"] == "1111222233334444"


def test_the_dashboard_pages_answer_head(tmp_path, monkeypatch):
    """HEAD on a dashboard page, a file of the build or a legacy /dashboard
    URL answers what the GET answers, with no body; an API or retired docs
    path stays a 404, never the page."""
    from app import app

    _write(tmp_path, STAMPED.format(id="5555666677778888"), 5)
    icon = b"\x00\x00\x01\x00not really an icon"
    (tmp_path / "favicon.ico").write_bytes(icon)
    monkeypatch.setattr(config, "DASHBOARD_ENABLED", True)
    monkeypatch.setattr(config, "DASHBOARD_DIST", tmp_path)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    client = TestClient(app)
    for path in ("/", "/chat/abc", "/dashboard/chat/abc", "/favicon.ico"):
        get, head = client.get(path), client.head(path)
        assert head.status_code == get.status_code == 200, path
        assert head.content == b"", path
        for name in ("content-type", "content-length", "cache-control"):
            assert head.headers.get(name) == get.headers.get(name), (path, name)
    assert client.head("/favicon.ico").headers["content-length"] == str(len(icon))
    for path in ("/v1/does-not-exist", "/docs", "/ui-kit/missing.js"):
        assert client.head(path).status_code == 404, path
    assert client.post("/").status_code == 405
