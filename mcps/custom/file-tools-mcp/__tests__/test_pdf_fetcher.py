"""write_pdf fetches nothing the proxy did not resolve: an
``http`` image, a CSS ``url()`` and an ``@import`` naming a listener are
never requested and render as missing; a ``file:`` reference outside the
resolved set is not read; an in-tree image (a name with a space included)
and a ``data:`` image embed as before.
"""

import asyncio
import base64
import http.server
import io
import sys
import threading
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")
pytest.importorskip("weasyprint")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pdf as pdf_mod  # noqa: E402
import shared  # noqa: E402


class _Listener:
    """A one-shot HTTP listener that records every request path."""

    def __init__(self):
        self.hits: list[str] = []
        outer = self

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(_png_bytes())

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), _H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (6, 6), "green").save(buf, format="PNG")
    return buf.getvalue()


def _tree_resolver(tree: Path):
    """The proxy's answer for a path: inside ``tree`` it resolves, anything
    else (a URL, another tree) is refused as the hook refuses it."""
    async def _resolve(p, writing=False, **kw):
        s = str(p)
        if "://" not in s and Path(s).is_relative_to(tree):
            return s
        raise ValueError(f"Cannot open '{p}': proxy resolve-path 403")
    return _resolve


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_mod, "_resolve_path", _tree_resolver(tmp_path))
    monkeypatch.setattr(shared, "_resolve_path", _tree_resolver(tmp_path))
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(pdf_mod, "_push_preview", _noop)
    listener = _Listener()
    yield tmp_path, listener
    listener.stop()


def _image_count(pdf_path: Path) -> int:
    doc = fitz.open(str(pdf_path))
    n = sum(len(page.get_images(full=True)) for page in doc)
    doc.close()
    return n


def test_external_and_foreign_references_are_never_fetched(rig, tmp_path):
    tree, listener = rig
    outside = tmp_path.parent / f"outside-{tmp_path.name}.png"
    outside.write_bytes(_png_bytes())
    try:
        port = listener.port
        content = (
            f'<img src="http://127.0.0.1:{port}/ssrf">'
            f'<div style="background:url(http://127.0.0.1:{port}/css)">x</div>'
            f'<img src="file://{outside}">'
            f'<img src="{outside}">'
            "<p>text</p>"
        )
        css = f'@import "http://127.0.0.1:{port}/import.css"; body {{ background: url("http://127.0.0.1:{port}/bg") }}'
        out = tree / "out.pdf"
        asyncio.run(pdf_mod.handle_write_pdf({
            "path": str(out), "content": content, "content_type": "html", "css": css,
        }))
        assert out.read_bytes().startswith(b"%PDF")
        assert listener.hits == []
        assert _image_count(out) == 0
    finally:
        outside.unlink()


def test_in_tree_and_data_images_still_embed(rig):
    tree, listener = rig
    img = tree / "site photo.png"
    img.write_bytes(_png_bytes())
    b64 = base64.b64encode(_png_bytes()).decode()
    content = (
        f'<img src="{img}">'
        f'<img src="data:image/png;base64,{b64}">'
        f'<div style="background:url({img})">bg</div>'
    )
    out = tree / "out.pdf"
    asyncio.run(pdf_mod.handle_write_pdf({
        "path": str(out), "content": content, "content_type": "html",
    }))
    assert _image_count(out) >= 2
    assert listener.hits == []


def test_the_reference_pass_covers_src_css_url_and_import(rig):
    tree, _ = rig
    (tree / "a.png").write_bytes(_png_bytes())
    (tree / "s.css").write_text("body{}")
    html = f'<img src="{tree}/a.png"><i style="background: url( ' + "'" + f"{tree}/a.png" + "' )\"></i>"
    resolved = asyncio.run(pdf_mod._resolve_references(
        html,
        f'@import "{tree}/s.css"; @import url({tree}/s.css); x{{background:url(http://h/x)}}',
    ))
    assert set(resolved) == {f"{tree}/a.png", f"{tree}/s.css"}
    text = pdf_mod._rewrite_references(f'@import "{tree}/s.css"; url({tree}/a.png) url(http://h/x)', resolved)
    assert f'@import url("file://{tree}/s.css")' in text
    assert f'url("file://{tree}/a.png")' in text and "url(http://h/x)" in text


def test_the_fetcher_admits_only_the_allowed_files_and_data(tmp_path):
    ok = tmp_path / "ok.png"
    ok.write_bytes(_png_bytes())
    other = tmp_path / "other.png"
    other.write_bytes(_png_bytes())
    fetcher = pdf_mod._allowed_files_fetcher([str(ok)])
    from weasyprint.urls import URLFetchingError
    assert fetcher.fetch(f"file://{ok}") is not None
    assert fetcher.fetch("data:image/png;base64," + base64.b64encode(_png_bytes()).decode()) is not None
    for url in (f"file://{other}", "http://127.0.0.1:9/x", "https://example.invalid/x", f"file://{tmp_path}"):
        with pytest.raises(Exception) as excinfo:
            fetcher.fetch(url)
        assert isinstance(excinfo.value, (URLFetchingError, ValueError, OSError))


def test_the_fetcher_decodes_a_file_url_once_and_reads_beneath_the_root(tmp_path, monkeypatch):
    """The reference the rewrite builds is decoded exactly once; a URL that
    names the allowed file only after a second decode is refused, and the
    bytes come from a no-follow read beneath the mount, never a second open
    by URL."""
    from urllib.request import pathname2url
    monkeypatch.setenv("FILETOOLS_WRITE_ROOT", str(tmp_path))
    ok = tmp_path / "a b%c.png"
    ok.write_bytes(_png_bytes())
    fetcher = pdf_mod._allowed_files_fetcher([str(ok)])
    once = "file://" + pathname2url(str(ok))
    assert fetcher.fetch(once).read() == _png_bytes()
    assert pdf_mod._rewrite_references('src="a b%c.png"', {"a b%c.png": str(ok)}) == f'src="{once}"'
    twice = "file://" + pathname2url(pathname2url(str(ok)))
    with pytest.raises(Exception):
        fetcher.fetch(twice)
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_png_bytes())
    try:
        with pytest.raises(Exception):
            pdf_mod._allowed_files_fetcher([str(outside)]).fetch("file://" + pathname2url(str(outside)))
    finally:
        outside.unlink()
