"""The document pane's conversion route (convert_route.py): the proxy's
one-shot token checked before the body, the inputs and targets it takes, its
caps, and its folders removed whatever happens. The engine is faked: its own
tests cover the conversion."""

from __future__ import annotations

import glob
import os
import tempfile

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import convert_route
import render
import render_engine


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write(path: str, body: bytes) -> None:
    with open(path, "wb") as f:
        f.write(body)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    state = {"live": {"good"}, "calls": [], "verified": [], "fail": None}

    async def _verify(bearer, path="/v1/internal/render/verify"):
        state["verified"].append((bearer, path))
        return bearer in state["live"]

    async def _convert(path, fmt, *, out_dir, deadline=None, **_kw):
        body = _read(path)
        state["calls"].append({"name": os.path.basename(path), "fmt": fmt, "body": body,
                               "deadline": deadline, "out_dir": out_dir})
        if state["fail"]:
            raise RuntimeError(state["fail"])
        out = os.path.join(out_dir, f"document.{fmt}")
        _write(out, b"%PDF converted " + body)
        return out

    monkeypatch.setattr(render, "_verify", _verify)
    monkeypatch.setattr(render_engine, "convert", _convert)
    app = Starlette(routes=[Route("/convert", convert_route.handle_convert, methods=["POST"])])
    state["client"] = TestClient(app)
    state["tmp"] = tmp_path
    return state


def _post(rig, body=b"PK docx", *, ext="docx", fmt="pdf", bearer="good", headers=None):
    h = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    h.update(headers or {})
    return rig["client"].post(f"/convert?ext={ext}&format={fmt}", content=body, headers=h)


def _leftovers(rig) -> list[str]:
    return glob.glob(str(rig["tmp"] / "file-tools-conver*"))


def test_a_word_file_converts_to_pdf_and_its_folders_go(rig):
    r = _post(rig)
    assert r.status_code == 200
    assert r.content == b"%PDF converted PK docx"
    assert r.headers["content-type"] == "application/octet-stream"
    call = rig["calls"][0]
    assert call["name"] == "document.docx" and call["fmt"] == "pdf"
    assert call["deadline"] is not None
    assert rig["verified"] == [("good", "/v1/internal/convert/verify")]
    assert _leftovers(rig) == []


def test_no_token_or_a_refused_one_converts_nothing(rig):
    assert _post(rig, bearer="").status_code == 401
    assert _post(rig, bearer="spent").status_code == 401
    assert rig["calls"] == []
    assert _leftovers(rig) == []


def test_the_inputs_and_targets_it_takes(rig):
    assert _post(rig, ext="exe").status_code == 400
    assert _post(rig, ext="pdf").status_code == 400
    assert _post(rig, ext="docx", fmt="csv").status_code == 400
    assert _post(rig, ext="docx", fmt="html").status_code == 400
    assert rig["calls"] == []
    assert _post(rig, ext="xlsx", fmt="csv").status_code == 200
    assert _post(rig, ext="pptx", fmt="pdf").status_code == 200
    assert _post(rig, ext="ODT", fmt="PDF").status_code == 200


def test_the_size_caps(rig, monkeypatch):
    monkeypatch.setattr(convert_route, "MAX_INPUT_BYTES", 10)
    big = _post(rig, b"x" * 11, headers={"Content-Length": "11"})
    assert big.status_code == 413

    def _chunks():
        yield b"x" * 6
        yield b"x" * 6
    streamed = rig["client"].post("/convert?ext=docx&format=pdf", content=_chunks(),
                                  headers={"Authorization": "Bearer good"})
    assert streamed.status_code == 413
    assert _post(rig, b"").status_code == 400
    assert rig["calls"] == []
    monkeypatch.setattr(convert_route, "MAX_INPUT_BYTES", 1000)
    monkeypatch.setattr(convert_route, "MAX_ANSWER_BYTES", 5)
    assert _post(rig).status_code == 413
    assert _leftovers(rig) == []


def test_an_engine_failure_is_a_reason_and_leaves_nothing(rig):
    rig["fail"] = "the document links to content outside it"
    r = _post(rig)
    assert r.status_code == 422 and "links to content" in r.json()["reason"]
    rig["fail"] = "the conversion timed out (60s)"
    assert _post(rig).status_code == 504
    assert _leftovers(rig) == []


def test_the_server_mounts_the_route():
    import server
    paths = {getattr(r, "path", "") for r in server.starlette_app.routes}
    assert "/convert" in paths
