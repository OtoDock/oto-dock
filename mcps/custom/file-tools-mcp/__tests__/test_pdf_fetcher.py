"""write_pdf fetches nothing the proxy did not resolve: an
``http`` image, a CSS ``url()`` and an ``@import`` naming a listener are
never requested and render as missing; a ``file:`` reference outside the
resolved set is not read; an in-tree image (a name with a space included)
and a ``data:`` image embed as before.
"""

import asyncio
import base64
import codecs
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


# ---------------------------------------------------------------------------
# An HTML document converts and screenshots through the same renderer, never
# through LibreOffice (which loads an HTML file's linked pictures from
# anywhere it can read); its relative references name files beside it
# ---------------------------------------------------------------------------


def _png(color: str, size: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _image_widths(pdf_path: Path) -> list[int]:
    doc = fitz.open(str(pdf_path))
    widths = [doc.extract_image(img[0])["width"]
              for page in doc for img in page.get_images(full=True)]
    doc.close()
    return widths


@pytest.fixture
def html_doc(rig, tmp_path, monkeypatch):
    """``docs/page.html`` naming a picture beside it (6 px), one outside the
    session's tree by a relative path and by a ``file:`` URL (9 px), and
    LibreOffice wired to fail the test."""
    tree, _ = rig
    docs = tree / "docs"
    docs.mkdir()
    (docs / "pic.png").write_bytes(_png("green", 6))
    outside = tmp_path.parent / f"outside-{tmp_path.name}.png"
    outside.write_bytes(_png("red", 9))
    page = docs / "page.html"
    page.write_text(
        "<html><head><title>t</title></head><body><p>text</p>"
        '<img src="pic.png">'
        f'<img src="../../{outside.name}">'
        f'<img src="file://{outside}">'
        "</body></html>",
        encoding="utf-8",
    )

    async def _no_libreoffice(*a, **kw):
        raise AssertionError("an HTML document reached LibreOffice")

    monkeypatch.setattr(pdf_mod, "_libreoffice_convert", _no_libreoffice)
    yield page
    outside.unlink()


@pytest.mark.parametrize("ext", [".html", ".htm"])
def test_an_html_convert_renders_through_write_pdf_with_its_own_pictures_only(html_doc, ext):
    page = html_doc.rename(html_doc.with_suffix(ext))
    msg = asyncio.run(pdf_mod.handle_convert_document(
        {"input_path": str(page), "output_format": "pdf"},
    ))
    out = page.with_suffix(".pdf")
    assert msg.startswith("Converted"), msg
    assert out.read_bytes().startswith(b"%PDF")
    assert _image_widths(out) == [6]


def test_an_html_convert_lands_at_output_path_and_reads_off_the_loop(html_doc, monkeypatch):
    real_read = pdf_mod._read_source
    on_loop = []

    def _read(path, **kw):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real_read(path, **kw)

    monkeypatch.setattr(pdf_mod, "_read_source", _read)
    out = html_doc.parent.parent / "out" / "final.pdf"
    asyncio.run(pdf_mod.handle_convert_document(
        {"input_path": str(html_doc), "output_format": "pdf", "output_path": str(out)},
    ))
    assert _image_widths(out) == [6]
    assert not html_doc.with_suffix(".pdf").exists()
    assert on_loop == [False]


def test_a_converted_pdf_beside_its_input_resolves_as_a_sandbox_write(rig, tmp_path, monkeypatch):
    """The proxy resolves a container path's agents-relative form for reads
    only, so the default output beside the input is asked for in its
    sandbox form."""
    mount = tmp_path / "agents"
    docs = mount / "pa/users/u/workspace/docs"
    docs.mkdir(parents=True)
    (docs / "notes.md").write_text("# Notes\n", encoding="utf-8")
    monkeypatch.setattr(pdf_mod, "MOUNT_AGENTS_DIR", str(mount), raising=False)
    writes = []

    async def _resolve(p, writing=False, **kw):
        if writing:
            writes.append(p)
            return str(mount / "pa") + p
        return p

    monkeypatch.setattr(pdf_mod, "_resolve_path", _resolve)
    monkeypatch.setattr(shared, "_resolve_path", _resolve)
    asyncio.run(pdf_mod.handle_convert_document(
        {"input_path": str(docs / "notes.md"), "output_format": "pdf"},
    ))
    assert writes == ["/users/u/workspace/docs/notes.pdf"]
    assert (docs / "notes.pdf").read_bytes().startswith(b"%PDF")


def test_an_html_screenshot_renders_through_write_pdf_with_its_own_pictures_only(
        html_doc, monkeypatch):
    from mcp.types import ImageContent
    seen = []
    real_core = pdf_mod._render_screenshot_core

    def _spy(pdf_path, *a, **kw):
        seen.append(_image_widths(Path(pdf_path)))
        return real_core(pdf_path, *a, **kw)

    monkeypatch.setattr(pdf_mod, "_render_screenshot_core", _spy)
    items = asyncio.run(pdf_mod.handle_screenshot_document({"path": str(html_doc)}))
    assert isinstance(items[0], ImageContent), items
    assert seen == [[6]]
    assert sorted(p.name for p in html_doc.parent.iterdir()) == ["page.html", "pic.png"]


# ---------------------------------------------------------------------------
# A converted or screenshotted HTML file is read beneath the mount under a
# cap and decoded as a browser would: its BOM, then its declared charset,
# else UTF-8 with undecodable bytes replaced
# ---------------------------------------------------------------------------


def _pdf_text(pdf_path: Path) -> str:
    doc = fitz.open(str(pdf_path))
    text = "".join(page.get_text() for page in doc)
    doc.close()
    return text


@pytest.mark.parametrize("raw, expected", [
    ('<meta charset="iso-8859-1"><p>café crème</p>'.encode("latin-1"), "café crème"),
    ('<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
     "<p>naïve “quotes”</p>".encode("cp1252"), "naïve “quotes”"),
    ("<p>ünïcode sixteen</p>".encode("utf-16"), "ünïcode sixteen"),
    (b"\xef\xbb\xbf<p>bom \xc3\xa9t\xc3\xa9</p>", "bom été"),
    (b"<p>plain \xff tail</p>", "plain"),
])
def test_an_html_convert_decodes_by_bom_or_declared_charset(rig, raw, expected):
    tree, _ = rig
    page = tree / "page.html"
    page.write_bytes(raw)
    asyncio.run(pdf_mod.handle_convert_document(
        {"input_path": str(page), "output_format": "pdf"},
    ))
    text = " ".join(_pdf_text(tree / "page.pdf").split())
    assert expected in text, text


def test_the_source_decoder_takes_the_bom_then_the_declaration_then_utf8():
    decode = pdf_mod._decode_source
    assert decode(b"<p>plain \xff tail</p>", html=True) == "<p>plain \ufffd tail</p>"
    # An unknown label, or a UTF-16 label in a body readable as ASCII, is UTF-8.
    assert decode('<meta charset="no-such">é'.encode(), html=True).endswith("é")
    assert decode('<meta charset="utf-16">é'.encode(), html=True).endswith("é")
    # A BOM wins over the declaration; a declaration past 1024 bytes is not read.
    assert decode(codecs.BOM_UTF8 + '<meta charset="latin1">é'.encode(), html=True).endswith("é")
    late = b" " * 1100 + b'<meta charset="latin1">\xc3\xa9'
    assert decode(late, html=True).endswith("é")
    # Markdown has no declaration to read.
    assert decode('<meta charset="latin1">é'.encode(), html=False).endswith("é")


def test_an_html_screenshot_decodes_a_declared_charset(rig):
    from mcp.types import ImageContent
    tree, _ = rig
    page = tree / "page.html"
    page.write_bytes('<meta charset="latin1"><p>déjà vu</p>'.encode("latin-1"))
    items = asyncio.run(pdf_mod.handle_screenshot_document({"path": str(page)}))
    assert isinstance(items[0], ImageContent), items


def test_an_html_input_is_read_beneath_the_mount_under_the_cap(rig, tmp_path, monkeypatch):
    tree, _ = rig
    # A link at the input's name is not followed (the read opens beneath the
    # mount), whatever it points at.
    outside = tmp_path.parent / f"outside-{tmp_path.name}.html"
    outside.write_text("<p>SECRET</p>", encoding="utf-8")
    page = tree / "page.html"
    page.symlink_to(outside)
    try:
        with pytest.raises(Exception):
            asyncio.run(pdf_mod.handle_convert_document(
                {"input_path": str(page), "output_format": "pdf"},
            ))
        assert not (tree / "page.pdf").exists()
    finally:
        outside.unlink()
    # Past the cap the call is refused and the error names the cap.
    monkeypatch.setattr(pdf_mod, "_MAX_SOURCE_BYTES", 1024 * 1024, raising=False)
    big = tree / "big.html"
    big.write_bytes(b"<p>" + b"x" * (1024 * 1024) + b"</p>")
    with pytest.raises(Exception, match="over the 1 MB"):
        asyncio.run(pdf_mod.handle_convert_document(
            {"input_path": str(big), "output_format": "pdf"},
        ))
    items = asyncio.run(pdf_mod.handle_screenshot_document({"path": str(big)}))
    assert "over the 1 MB" in items[0].text
