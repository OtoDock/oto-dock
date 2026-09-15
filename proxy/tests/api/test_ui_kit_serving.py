"""Dashboard SPA catch-all: /ui-kit/* serving contract.

/ui-kit/* files are the only subresources sandboxed display_ui artifact
iframes may load (kit JS/CSS/fonts, built into dashboard/dist/ui-kit/).
Unlike every other dist path, a MISS must be a loud 404 — the generic SPA
fallback would serve index.html with HTTP 200, and a <script src> pointing at
a mis-copied kit file would then silently load HTML-as-JS (SyntaxError, no
signal). These tests pin that guard plus the untouched fallback behavior.
"""

from __future__ import annotations

import gzip

import pytest
from fastapi.testclient import TestClient

KIT_JS = b"/* echarts */ var echarts = {};\n" * 64


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """A fake dashboard dist tree with a kit file, wired into config."""
    import config

    (tmp_path / "index.html").write_text("<!doctype html><title>spa</title>")
    # app.py mounts /assets from DASHBOARD_DIST at import time (check_dir)
    (tmp_path / "assets").mkdir()
    kit = tmp_path / "ui-kit"
    (kit / "fonts").mkdir(parents=True)
    (kit / "otodock-tokens.css").write_text(":root { --p-bg: #FAF9F9; }")
    (kit / "fonts" / "comfortaa-latin-400-normal.woff2").write_bytes(b"wOF2fake")
    # The build writes a gzip sibling next to the kit JS (static_assets.py).
    (kit / "echarts.min.js").write_bytes(KIT_JS)
    (kit / "echarts.min.js.gz").write_bytes(gzip.compress(KIT_JS))
    monkeypatch.setattr(config, "DASHBOARD_ENABLED", True)
    monkeypatch.setattr(config, "DASHBOARD_DIST", tmp_path)
    return tmp_path


@pytest.fixture
def client(dist):
    from app import app

    return TestClient(app)


def test_ui_kit_real_file_served(client):
    r = client.get("/ui-kit/otodock-tokens.css")
    assert r.status_code == 200
    assert r.text == ":root { --p-bg: #FAF9F9; }"
    assert r.headers["content-type"].startswith("text/css")
    # Artifact iframes fetch from an OPAQUE origin, and @font-face requests
    # are CORS-mode — kit files must carry ACAO or fonts silently fall back.
    assert r.headers["access-control-allow-origin"] == "*"


def test_ui_kit_nested_file_served(client):
    r = client.get("/ui-kit/fonts/comfortaa-latin-400-normal.woff2")
    assert r.status_code == 200
    assert r.content == b"wOF2fake"
    assert r.headers["access-control-allow-origin"] == "*"


def test_non_kit_dist_files_get_no_cors_header(client):
    r = client.get("/index.html")
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_loose_dist_files_revalidate_on_every_load(client, dist):
    # public/ files keep their names across releases (the wake-word worker):
    # without an explicit policy a browser applies heuristic freshness and
    # keeps running the old file after the page reloaded onto a new build.
    (dist / "wake-word-worker.js").write_text("self.onmessage = () => {}")
    r = client.get("/wake-word-worker.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert "etag" in r.headers


def test_ui_kit_miss_is_404_not_index(client):
    r = client.get("/ui-kit/missing.min.js")
    assert r.status_code == 404
    assert "spa" not in r.text  # never the index.html fallback


def test_ui_kit_compressed_sibling_served_with_cors(client):
    r = client.get("/ui-kit/echarts.min.js", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.content == KIT_JS  # httpx decoded the sibling back to the source
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["vary"].lower() == "accept-encoding"
    # Kit names are stable across releases: revalidated, never immutable.
    assert "cache-control" not in r.headers


def test_ui_kit_identity_when_not_accepted(client):
    r = client.get("/ui-kit/echarts.min.js", headers={"accept-encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.content == KIT_JS


def test_ui_kit_304_keeps_cors_header(client):
    first = client.get("/ui-kit/echarts.min.js", headers={"accept-encoding": "identity"})
    r = client.get("/ui-kit/echarts.min.js", headers={
        "accept-encoding": "identity", "if-none-match": first.headers["etag"]})
    assert r.status_code == 304
    # A font revalidation from the opaque-origin iframe is a CORS request
    # too: the 304 must carry the header, not rely on cache freshening.
    assert r.headers["access-control-allow-origin"] == "*"


def test_ui_kit_traversal_escape_is_404(client):
    # uvicorn percent-decodes scope['path'] before routing, so dot-segments
    # reach the handler literally. A path escaping dist resolves to None in
    # _safe_dashboard_file and must hit the ui-kit 404 guard — without it,
    # this fell through to index.html with 200.
    r = client.get("/ui-kit/%2e%2e/%2e%2e/whatever.js")
    assert r.status_code == 404


def test_spa_fallback_unaffected(client):
    r = client.get("/chat/some-agent/some-chat-id")
    assert r.status_code == 200
    assert "spa" in r.text
    assert "no-store" in r.headers["cache-control"]
