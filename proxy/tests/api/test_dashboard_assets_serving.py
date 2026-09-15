"""Dashboard asset serving: precompressed siblings and the cache policy.

The build writes ``<file>.br`` / ``<file>.gz`` next to every text asset. The
mount must serve the representation the client accepts with the identity
file's media type, an ETag per representation, ``Vary`` whenever a sibling
exists, and far-future immutable caching on content-hashed names only.

httpx (behind TestClient) sends ``accept-encoding: gzip, deflate`` by default
and decodes gzip bodies transparently, so the identity cases ask for
``identity`` explicitly and the brotli cases read the wire bytes through
``iter_raw``. The brotli fixture is opaque bytes: nothing in the test
environment decodes them, and the contract under test is which file goes
out, not the codec.
"""

from __future__ import annotations

import gzip
import os

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from static_assets import (
    IMMUTABLE_CACHE_CONTROL,
    PrecompressedStaticFiles,
    is_hashed_asset,
    parse_accept_encoding,
)

JS = b"console.log('hello from the dashboard');\n" * 40
BR = b"\x1b\x0b\x00\x80not really brotli, just the wire bytes"
CHUNK = "/assets/index-XGlNw2dg.js"


@pytest.fixture
def assets(tmp_path):
    (tmp_path / "index-XGlNw2dg.js").write_bytes(JS)
    (tmp_path / "index-XGlNw2dg.js.br").write_bytes(BR)
    (tmp_path / "index-XGlNw2dg.js.gz").write_bytes(gzip.compress(JS))
    (tmp_path / "favicon.png").write_bytes(b"\x89PNG plain")
    (tmp_path / "lonely-AbCdEfGh.css").write_bytes(b"body{margin:0}")
    (tmp_path / "linked-AbCdEfGh.js").write_bytes(JS)
    os.symlink(tmp_path / "index-XGlNw2dg.js.br", tmp_path / "linked-AbCdEfGh.js.br")
    return tmp_path


@pytest.fixture
def client(assets):
    app = Starlette(routes=[Mount(
        "/assets",
        app=PrecompressedStaticFiles(directory=str(assets), immutable=is_hashed_asset),
    )])
    return TestClient(app)


def _raw(client, url, **headers):
    with client.stream("GET", url, headers=headers) as r:
        return r, b"".join(r.iter_raw())


def test_brotli_preferred_when_both_accepted(client):
    r, body = _raw(client, CHUNK, **{"accept-encoding": "gzip, deflate, br"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "br"
    assert body == BR
    assert int(r.headers["content-length"]) == len(BR)
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["vary"].lower() == "accept-encoding"
    assert r.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


def test_gzip_when_only_gzip_accepted(client):
    r = client.get(CHUNK, headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.content == JS  # httpx decoded the sibling back to the source
    assert r.headers["content-type"].startswith("text/javascript")


def test_identity_when_not_accepted(client):
    r = client.get(CHUNK, headers={"accept-encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.content == JS
    assert r.headers["vary"].lower() == "accept-encoding"
    assert r.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


def test_q_zero_excludes(client):
    r = client.get(CHUNK, headers={"accept-encoding": "br;q=0, gzip;q=0.0"})
    assert "content-encoding" not in r.headers
    assert r.content == JS


def test_etag_per_representation_and_304(client):
    br, _ = _raw(client, CHUNK, **{"accept-encoding": "br"})
    identity = client.get(CHUNK, headers={"accept-encoding": "identity"})
    assert br.headers["etag"] != identity.headers["etag"]

    r = client.get(CHUNK, headers={"accept-encoding": "br", "if-none-match": br.headers["etag"]})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL
    assert r.headers["vary"].lower() == "accept-encoding"

    r, body = _raw(client, CHUNK, **{
        "accept-encoding": "br", "if-none-match": identity.headers["etag"]})
    assert r.status_code == 200
    assert body == BR


def test_head_on_mount(client):
    r = client.head(CHUNK, headers={"accept-encoding": "br"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "br"
    assert int(r.headers["content-length"]) == len(BR)
    assert r.content == b""


def test_unhashed_file_is_not_immutable(client):
    r = client.get("/assets/favicon.png", headers={"accept-encoding": "br, gzip"})
    assert r.status_code == 200
    assert "cache-control" not in r.headers
    assert "content-encoding" not in r.headers
    assert "vary" not in r.headers


def test_no_sibling_serves_identity_without_vary(client):
    r = client.get("/assets/lonely-AbCdEfGh.css", headers={"accept-encoding": "br, gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert "vary" not in r.headers
    assert r.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL
    assert r.content == b"body{margin:0}"


def test_symlink_sibling_is_ignored(client):
    r = client.get("/assets/linked-AbCdEfGh.js", headers={"accept-encoding": "br"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.content == JS


def test_miss_is_404(client):
    assert client.get("/assets/nope-AbCdEfGh.js").status_code == 404


@pytest.mark.parametrize("values, expected", [
    (["gzip, deflate, br"], {"gzip", "deflate", "br"}),
    (["br;q=0, gzip"], {"gzip"}),
    (["*"], set()),
    (["identity"], set()),
    (["GZIP;q=0.5", "br"], {"gzip", "br"}),
    (["gzip;q=0.0"], set()),
    (["gzip;q=abc"], set()),
    (["gzip ; q=1.0 , br ; q=0.8"], {"gzip", "br"}),
    ([], set()),
])
def test_parse_accept_encoding(values, expected):
    assert parse_accept_encoding(values) == expected


@pytest.mark.parametrize("name, hashed", [
    ("index-XGlNw2dg.js", True),
    ("sky-night-Cu-3FVyW.jpg", True),
    ("comfortaa-latin-400-normal--KakQCjT.woff2", True),
    ("favicon.png", False),
    ("index-XGlNw2dg.js.br", False),
    ("echarts.min.js", False),
])
def test_is_hashed_asset(name, hashed):
    assert is_hashed_asset(name) is hashed
