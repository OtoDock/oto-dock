"""Documents that reach out when an engine follows what they hold, for the
rendering tests: each URL names its probe, so a listener's log says which
one reached it. Shared by the image's office test and the Collabora tests."""

import http.server
import io
import socket
import threading
import zipfile
from pathlib import Path

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CANARY = "LISTENER-CANARY-7d41"


class Listener:
    """An HTTP server on every address of this host that logs each request
    path and answers with a canary text."""

    def __init__(self) -> None:
        hits: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _log(self):
                hits.append(f"{self.command} {self.path}")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(CANARY.encode())

            do_GET = do_POST = do_HEAD = do_OPTIONS = do_PROPFIND = _log

            def log_message(self, *_args):
                pass

        self.hits = hits
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, host: str) -> str:
        return f"http://{host}:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def own_address(peer: str) -> str:
    """This host's address as ``peer`` (a host name or address) reaches it."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((socket.gethostbyname(peer), 1))
        return probe.getsockname()[0]


def _patch(data: bytes, edits: dict, add: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            part = src.read(item.filename)
            if item.filename in edits:
                part = edits[item.filename](part.decode()).encode()
            dst.writestr(item.filename, part)
        for name, text in add.items():
            dst.writestr(name, text)
    return out.getvalue()


def _rels(rid: str, kind: str, target: str, external: bool = True) -> str:
    mode = ' TargetMode="External"' if external else ""
    return ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="{rid}" Type="{REL}{kind}" Target="{target}"{mode}/></Relationships>')


def theme_picture_deck(path: Path, base: str) -> Path:
    """A deck whose theme fills a background with a linked picture."""
    import pptx

    buffer = io.BytesIO()
    deck = pptx.Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    deck.save(buffer)
    data = _patch(buffer.getvalue(), {"ppt/theme/theme1.xml": lambda t: t.replace(
        "<a:bgFillStyleLst>", '<a:bgFillStyleLst><a:blipFill><a:blip r:link="rIdTL"/><a:stretch><a:fillRect/>'
        "</a:stretch></a:blipFill>", 1).replace("<a:theme ", f'<a:theme xmlns:r="{R}" ', 1)},
        {"ppt/theme/_rels/theme1.xml.rels": _rels("rIdTL", "image", f"{base}/theme-picture.png")})
    path.write_bytes(data)
    return path


def drawing_picture_book(path: Path, base: str) -> Path:
    """A workbook whose drawing shows a linked picture, with a formula."""
    import openpyxl

    buffer = io.BytesIO()
    book = openpyxl.Workbook()
    book.active["A1"] = 2
    book.active["A2"] = "=A1*3"
    book.save(buffer)
    drawing = ('<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
               f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{R}">'
               '<xdr:oneCellAnchor><xdr:from><xdr:col>2</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>2</xdr:row>'
               '<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="914400" cy="914400"/><xdr:pic><xdr:nvPicPr>'
               '<xdr:cNvPr id="2" name="P"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:link="rId1"/>'
               '<a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:prstGeom prst="rect"><a:avLst/>'
               '</a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:oneCellAnchor></xdr:wsDr>')
    data = _patch(buffer.getvalue(), {
        "[Content_Types].xml": lambda t: t.replace("</Types>", '<Override PartName="/xl/drawings/drawing1.xml" '
                                                   'ContentType="application/vnd.openxmlformats-officedocument.'
                                                   'drawing+xml"/></Types>'),
        "xl/worksheets/sheet1.xml": lambda t: t.replace("</worksheet>",
                                                        f'<drawing xmlns:r="{R}" r:id="rIdD"/></worksheet>'),
    }, {
        "xl/worksheets/_rels/sheet1.xml.rels": _rels("rIdD", "drawing", "../drawings/drawing1.xml", False),
        "xl/drawings/drawing1.xml": drawing,
        "xl/drawings/_rels/drawing1.xml.rels": _rels("rId1", "image", f"{base}/drawing-picture.png"),
    })
    path.write_bytes(data)
    return path


def chunk_document(path: Path, base: str) -> Path:
    """A document with an HTML part (altChunk) that links a stylesheet and a
    picture."""
    import docx

    buffer = io.BytesIO()
    document = docx.Document()
    document.add_paragraph("before the chunk")
    document.save(buffer)
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    html = (f'<html><head><link rel="stylesheet" href="{base}/chunk-style.css"></head><body><p>chunk</p>'
            f'<img src="{base}/chunk-picture.png"></body></html>')
    data = _patch(buffer.getvalue(), {
        "[Content_Types].xml": lambda t: t.replace("</Types>", '<Default Extension="htm" ContentType="text/html"/>'
                                                   "</Types>"),
        "word/document.xml": lambda t: t.replace("<w:body>", f'<w:body><w:altChunk xmlns:r="{R}" r:id="rIdC"/>', 1)
        if f'xmlns:w="{w}"' in t else t,
        "word/_rels/document.xml.rels": lambda t: t.replace(
            "</Relationships>", f'<Relationship Id="rIdC" Type="{REL}aFChunk" Target="afchunk.htm"/></Relationships>'),
    }, {"word/afchunk.htm": html})
    path.write_bytes(data)
    return path


def function_book(path: Path, base: str) -> Path:
    """A workbook whose formulas would read the network."""
    import openpyxl

    book = openpyxl.Workbook()
    book.active["A1"] = f'=_xlfn.WEBSERVICE("{base}/webservice")'
    book.active["A2"] = f'=_xlfn.FILTERXML(_xlfn.WEBSERVICE("{base}/filterxml"),"//a")'
    book.active["A3"] = f'=_xlfn.IMAGE("{base}/image-function.png")'
    book.active["A4"] = f"='{base}/[remote.xlsx]Sheet1'!A1"
    book.save(path)
    return path


def field_document(path: Path, base: str) -> Path:
    """A document with a linked picture and INCLUDEPICTURE and INCLUDETEXT
    fields, its settings asking for every field to update on open."""
    import docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.shared import Inches
    from PIL import Image

    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = docx.Document()
    picture = io.BytesIO()
    Image.new("RGB", (4, 4)).save(picture, "PNG")
    picture.seek(0)
    document.add_picture(picture, width=Inches(1))
    rid = document.part.relate_to(f"{base}/linked-picture.png", RT.IMAGE, is_external=True)
    document.element.body.xpath(".//a:blip")[0].set(f"{{{R}}}link", rid)
    for instruction in (f'INCLUDEPICTURE "{base}/includepicture.png" \\d', f'INCLUDETEXT "{base}/includetext"'):
        paragraph = document.add_paragraph()
        for kind, text in (("begin", None), (None, instruction), ("end", None)):
            run = paragraph.add_run()
            if kind:
                run._r.append(run._r.makeelement(f"{{{w}}}fldChar", {f"{{{w}}}fldCharType": kind}))
            else:
                instr = run._r.makeelement(f"{{{w}}}instrText", {})
                instr.text = f" {text} "
                run._r.append(instr)
    buffer = io.BytesIO()
    document.save(buffer)
    path.write_bytes(_patch(buffer.getvalue(), {"word/settings.xml": lambda t: t.replace(
        "<w:compat", '<w:updateFields w:val="true"/><w:compat', 1)}, {}))
    return path
