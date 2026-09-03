"""Wake-word spotter asset serving: /kws-assets/* contract.

The dashboard's wake-word worker importScripts the wasm glue and fetches the
model .data from /kws-assets/<version>/. Two things are pinned here:

1. Presence gate — the committed bundle files exist in the repo (a rebuild
   or version bump that forgets a file must fail CI, not 404 in production;
   same discipline as the ui-kit filename gate).
2. Serving contract — immutable far-future caching (the version directory in
   the URL is the cache buster; nothing else in the static path sets
   Cache-Control at all) and LOUD 404 on a miss (a worker importScripts miss
   must never receive index.html-as-JS — the ui-kit lesson).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config

KWS_VERSION_DIR = "1.13.5-gigaspeech-3.3M"

BUNDLE_FILES = (
    "sherpa-onnx-kws.js",
    "sherpa-onnx-wasm-kws-main.js",
    "sherpa-onnx-wasm-kws-main.wasm",
    "sherpa-onnx-wasm-kws-main.data",
)

ENCODER_FILES = ("bpe.model", "tokens.txt")


@pytest.fixture
def client():
    from app import app

    return TestClient(app)


def test_bundle_files_present_in_repo():
    d = config.KWS_ASSETS_DIR / KWS_VERSION_DIR
    for name in BUNDLE_FILES:
        assert (d / name).is_file(), f"missing committed KWS asset: {name}"
    for name in ENCODER_FILES:
        assert (config.KWS_ASSETS_DIR / "encoder" / name).is_file(), (
            f"missing committed keyword-encoder input: {name}"
        )


def test_small_asset_served_with_immutable_cache(client):
    r = client.get("/kws-assets/encoder/tokens.txt")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_wrapper_js_served(client):
    r = client.get(f"/kws-assets/{KWS_VERSION_DIR}/sherpa-onnx-kws.js")
    assert r.status_code == 200
    assert "createKws" in r.text
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_miss_is_404_not_index(client):
    r = client.get("/kws-assets/no-such-version/sherpa-onnx-kws.js")
    assert r.status_code == 404
    assert "<title>" not in r.text  # never the index.html fallback
