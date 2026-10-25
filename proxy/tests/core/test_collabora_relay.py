"""The editor relay forwards only what Collabora 26.04's editor uses (an
allow list, the deny list behind it), and a WOPI call lifts the body cap
only with Collabora's proof headers (``api/media/collabora_proxy.py``,
``middleware.credential_state``)."""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import middleware
from api.media import collabora_proxy as relay, wopi_proof

_SRC = quote("http://host.docker.internal:8400/wopi/files/ZG9jLmRvY3g", safe="")
_SRC2 = quote(_SRC, safe="")


@pytest.mark.parametrize("path", [
    "browser/22ce52fb51/cool.html", "browser/22ce52fb51/images/lc_bold.svg",
    "browser/22ce52fb51/welcome/welcome.html", "browser/dist/bundle.js",
    "hosting/discovery", "hosting/capabilities", "livez", "readyz", "favicon.ico", "robots.txt",
    "cool/clipboard", "cool/media", "cool/mediavtt", "cool/mediaVTT", "cool/signature",
    f"cool/{_SRC}/insertfile", f"cool/{_SRC}/download/2HQ70zaKbefRFqTiLjvF",
    f"cool/{_SRC}/avatar", f"cool/{_SRC}/browser-logging", f"cool/{_SRC2}/insertfile",
])
def test_the_editors_http_paths_are_allowed(path):
    assert relay._is_allowed_subpath(path)


@pytest.mark.parametrize("path", [
    "", "foo", "cool", "cool/x", "cool/convert-to", "cool/convert-to/pdf",
    "cool/adminws", "cool/getMetrics", "hosting/wopiAccessCheck", "hosting",
    "co/collab", "wopi/files/x", "wopi/settings/sharedconfig.json", "lool/convert-to",
    f"cool/{_SRC}/convert-to", f"cool/{_SRC}/insertfile/x", f"cool/{_SRC}/download",
    "cool/not-a-url/insertfile", f"cool/{quote('ftp://h/x', safe='')}/download/1",
    "cool/ws", f"cool/{_SRC}/ws",
    # a dot segment, raw, encoded or double-encoded: the client below folds it
    "browser/../cool/convert-to/pdf", "browser/x/../../cool/extract-link-targets",
    "browser/%2e%2e/cool/convert-to", "browser/%2E%2E/cool/convert-to",
    "browser/%252e%252e/cool/convert-to", "browser/./x.js", "browser/x/%2e/y.js",
    f"cool/{quote('http://h/wopi/files/../../x', safe='')}/insertfile",
])
def test_anything_else_is_refused(path):
    assert not relay._is_allowed_subpath(path)


@pytest.mark.parametrize("path, ok", [
    ("cool/ws", True), (f"cool/{_SRC}/ws", True), (f"cool/{_SRC2}/ws", True),
    ("cool/adminws", False), ("cool/x/ws", False), ("browser/x/cool.html", False),
    ("cool/../cool/ws", False), (f"cool/{_SRC}/../ws", False),
    (f"cool/{_SRC}/insertfile", False),
])
def test_only_the_editors_socket_is_a_socket(path, ok):
    assert relay._is_allowed_subpath(path, socket=True) is ok


@pytest.fixture
def relay_app(monkeypatch):
    monkeypatch.setattr(config, "COLLABORA_URL", "https://example.com/collabora")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://example.com")
    monkeypatch.setattr(config, "COLLABORA_BACKEND_URL", "http://backend:9980")
    app = FastAPI()
    app.include_router(relay.router)
    return app


def test_a_signed_in_person_gets_404_for_a_path_the_editor_never_uses(relay_app, monkeypatch):
    async def _who(scope, cookies):
        return "user-a"
    upstream = MagicMock(status_code=200, content=b"x", headers={})
    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client_cm)
    client_cm.__aexit__ = AsyncMock(return_value=None)
    client_cm.request = AsyncMock(return_value=upstream)
    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=client_cm):
        with TestClient(relay_app) as client:
            assert client.get("/collabora/cool/x").status_code == 403   # nobody signed in
            monkeypatch.setattr(relay, "_collabora_authorized", _who)
            assert client.get("/collabora/cool/x").status_code == 404
            assert client.get(f"/collabora/cool/{_SRC}/convert-to").status_code == 404
            assert client.get("/collabora/browser/dist/cool.html").status_code == 200
            assert client.post(f"/collabora/cool/{_SRC}/insertfile", content=b"x").status_code == 200
    assert client_cm.request.await_count == 2


# --- the WOPI body cap -------------------------------------------------------

def test_a_wopi_token_without_the_proof_headers_lifts_no_cap(monkeypatch):
    import time

    from api.media.wopi import create_wopi_token, encode_file_id
    from tests.api.test_http_middleware import CHUNK, _drive
    monkeypatch.setattr(config, "WOPI_ACCEPT_UNSIGNED", False, raising=False)
    monkeypatch.setattr(wopi_proof, "_held", wopi_proof.Keys(
        current=None, old=None, editor_scheme="", read_at=time.monotonic()))
    rel = "agent-x/workspace/doc.docx"
    token, _ = create_wopi_token(rel, "user-admin", "Admin", "edit", "agent-x")
    path = f"/wopi/files/{encode_file_id(rel)}/contents"
    status, _, handed = _drive("POST", path, declared=4 * CHUNK, query=f"access_token={token}")
    assert status == 401 and handed == 0
    proof = [("x-wopi-proof", "AA=="), ("x-wopi-proofold", "AA=="), ("x-wopi-timestamp", "1")]
    status, _, _ = _drive("POST", path, declared=4 * CHUNK, query=f"access_token={token}",
                          headers=proof[:2])
    assert status == 401
    # All three present: the cap lifts and the route judges the signature.
    status, _, _ = _drive("POST", path, chunks=4, query=f"access_token={token}", headers=proof)
    assert status != 413
    # The editor relay carries no proof headers and keeps its rule.
    assert middleware.credential_state(
        _request(f"/collabora/cool/ws?access_token={token}"), {}, "/collabora/cool/ws") == "valid"


def _request(url: str):
    from starlette.requests import Request
    path, _, query = url.partition("?")
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(),
                    "headers": [], "raw_path": path.encode()})
