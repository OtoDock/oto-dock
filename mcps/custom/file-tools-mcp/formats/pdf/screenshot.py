"""What screenshot_document adds around the page render: a contact sheet
(every page as a labelled thumbnail in one image), the slide numbers a
deck's pages stand for, and a workbook copy that prints one sheet.

Everything here reads the person's file, so it runs in a worker like the
page render, never in the server process; the package parts are read with
the safe parser (no entities, no network, no DTD).
"""

from __future__ import annotations

import io
import math
import os
import posixpath
import re
import zipfile
from typing import Any

MAX_SHEET_PAGES = 36
SHEET_LONG_EDGE = 2000
LANDSCAPE_COLUMNS, PORTRAIT_COLUMNS = 4, 6
GAP = 16
LABEL_PT = 18
PNG_FALLBACK_BYTES = int(1.5 * 1024 * 1024)
WORKBOOK_XML_CAP = 16 * 1024 * 1024
COPY_CHUNK = 1024 * 1024
_BACKGROUND = (226, 228, 232)
_BORDER = (150, 154, 160)
_INK = (40, 44, 52)
DECK_EXTS = frozenset({".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"})
XLSX_EXTS = frozenset({".xlsx", ".xlsm", ".xltx", ".xltm"})

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PRES = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _parser():
    from lxml import etree

    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def _label_font(size: int) -> Any:
    from PIL import ImageFont

    import fonts

    for family in ("Liberation Sans", "DejaVu Sans", "Noto Sans"):
        try:
            face = fonts.resolve(family).face
            return ImageFont.truetype(face.path, size, index=face.index)
        except (LookupError, OSError):
            continue
    return ImageFont.load_default()


def layout(sizes: list[tuple[float, float]]) -> tuple[int, int, int, int]:
    """(columns, rows, cell width, cell height) in pixels for pages of
    ``sizes`` (points): 4 columns when the widest page is landscape, 6
    otherwise, never more than there are pages; cells sized for the
    tallest page, the whole sheet within SHEET_LONG_EDGE."""
    n = len(sizes)
    landscape = any(w > h for w, h in sizes)
    cols = min(LANDSCAPE_COLUMNS if landscape else PORTRAIT_COLUMNS, n)
    rows = math.ceil(n / cols)
    aspect = max(h / max(w, 1.0) for w, h in sizes)
    label = LABEL_PT + 10
    cell_w = (SHEET_LONG_EDGE - GAP * (cols + 1)) / cols
    height = rows * (cell_w * aspect + label + GAP) + GAP
    if height > SHEET_LONG_EDGE:
        cell_w = (SHEET_LONG_EDGE - GAP - rows * (label + GAP)) / (rows * aspect)
    return cols, rows, max(int(cell_w), 40), max(int(cell_w * aspect), 40)


def contact_sheet_core(pdf_path: str, pages_spec: Any, src_path: str, password: str | None = None) -> dict:
    """Worker core: the pages ``pages_spec`` names (at most MAX_SHEET_PAGES)
    as thumbnails on one sheet, each over its label: a deck's slide_index
    (read from ``src_path``, the person's file), else its page number.
    Returns the image bytes, its layout and what it shows; ``error`` for a
    page spec that names no page."""
    import fitz
    from PIL import Image, ImageDraw

    from formats.pdf.pages import PageSpecError, parse_pages

    doc = fitz.open(pdf_path)
    if doc.needs_pass and not (password not in (None, "") and doc.authenticate(str(password))):
        doc.close()
        if password in (None, ""):
            raise ValueError("the PDF is password-protected: give its password (password)")
        raise ValueError(f"The password given is not the one that opens {os.path.basename(src_path)}.")
    try:
        total = doc.page_count
        try:
            wanted = parse_pages(pages_spec, total)
        except PageSpecError as exc:
            return {"error": str(exc), "total": total}
        if not wanted:
            return {"error": f"No valid pages to render (document has {total} pages).", "total": total}
        pages, rest = wanted[:MAX_SHEET_PAGES], wanted[MAX_SHEET_PAGES:]
        shown = shown_slides(src_path) if os.path.splitext(src_path)[1].lower() in DECK_EXTS else None
        slides = shown is not None and len(shown) == total
        labels = [f"slide_index {shown[i]}" if slides else f"Page {i + 1}" for i in pages]
        sizes = [(doc[i].rect.width, doc[i].rect.height) for i in pages]
        cols, rows, cell_w, cell_h = layout(sizes)
        label_h = LABEL_PT + 10
        width = GAP + cols * (cell_w + GAP)
        height = GAP + rows * (cell_h + label_h + GAP)
        sheet = Image.new("RGB", (width, height), _BACKGROUND)
        draw = ImageDraw.Draw(sheet)
        font = _label_font(LABEL_PT)
        for slot, (index, label) in enumerate(zip(pages, labels, strict=True)):
            page = doc[index]
            zoom = min(cell_w / page.rect.width, cell_h / page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            thumb = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            col, row = slot % cols, slot // cols
            x0 = GAP + col * (cell_w + GAP) + (cell_w - thumb.width) // 2
            y0 = GAP + row * (cell_h + label_h + GAP) + (cell_h - thumb.height)
            sheet.paste(thumb, (x0, y0))
            draw.rectangle((x0 - 1, y0 - 1, x0 + thumb.width, y0 + thumb.height), outline=_BORDER)
            box = draw.textbbox((0, 0), label, font=font)
            text_x = GAP + col * (cell_w + GAP) + (cell_w - (box[2] - box[0])) // 2
            draw.text((text_x, y0 + thumb.height + 4), label, fill=_INK, font=font)
        buf = io.BytesIO()
        sheet.save(buf, format="PNG", optimize=True)
        data, mime = buf.getvalue(), "image/png"
        if len(data) > PNG_FALLBACK_BYTES:
            buf = io.BytesIO()
            sheet.save(buf, format="JPEG", quality=85)
            data, mime = buf.getvalue(), "image/jpeg"
        return {"data": data, "mime": mime, "columns": cols, "rows": rows, "width": width, "height": height,
                "total": total, "pages": pages, "rest": rest, "slides": slides}
    finally:
        doc.close()


def shown_slides(path: str) -> list[int] | None:
    """The slide_index of each slide a deck shows, in order (LibreOffice
    exports only those, one page each); None when the deck cannot be read."""
    from lxml import etree

    try:
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            pres = etree.fromstring(package.read("ppt/presentation.xml"), _parser())
            rels = etree.fromstring(package.read("ppt/_rels/presentation.xml.rels"), _parser())
            targets = {r.get("Id"): r.get("Target", "") for r in rels}
            shown: list[int] = []
            for index, entry in enumerate(pres.iter(f"{_PRES}sldId")):
                part = posixpath.normpath(posixpath.join("ppt", targets.get(entry.get(f"{_REL}id"), "")))
                if part not in names:
                    return None
                head = package.open(part).read(4096).decode("utf-8", "replace")
                root = re.search(r"<(?:\w+:)?sld\b[^>]*>", head)
                if root is None or not re.search(r"\sshow=\"(?:0|false)\"", root.group(0)):
                    shown.append(index)
            return shown
    except (KeyError, OSError, zipfile.BadZipFile, etree.XMLSyntaxError):
        return None


def select_sheet(path: str, sheet: Any) -> tuple[int, list[str]]:
    """The workbook copy at ``path`` rewritten so it prints ``sheet`` (a name
    or a 0-based index) alone: every other worksheet hidden, the chosen one
    visible and active. The other parts are copied through in chunks.
    Returns its index and every sheet name; an unknown sheet raises
    ValueError naming them."""
    import shutil

    from lxml import etree

    with zipfile.ZipFile(path) as package:
        info = package.getinfo("xl/workbook.xml")
        if info.file_size > WORKBOOK_XML_CAP:
            raise ValueError(f"the workbook's sheet list is over {WORKBOOK_XML_CAP // (1024 * 1024)} MB")
        root = etree.fromstring(package.read(info), _parser())
        sheets = list(root.iter(f"{_MAIN}sheet"))
        names = [s.get("name", "") for s in sheets]
        index = _sheet_index(sheet, names)
        for number, element in enumerate(sheets):
            if number == index:
                element.attrib.pop("state", None)
            elif element.get("state") != "veryHidden":
                element.set("state", "hidden")
        for view in root.iter(f"{_MAIN}workbookView"):
            view.set("activeTab", str(index))
            view.set("firstSheet", str(index))
        rewritten = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        temp = path + ".sheet"
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as out:
            for entry in package.infolist():
                if entry.filename == info.filename:
                    out.writestr(entry, rewritten)
                    continue
                with package.open(entry) as src, out.open(entry, "w", force_zip64=True) as dst:
                    shutil.copyfileobj(src, dst, COPY_CHUNK)
    os.replace(temp, path)
    return index, names


def _sheet_index(sheet: Any, names: list[str]) -> int:
    if isinstance(sheet, int) or (isinstance(sheet, str) and sheet.strip().isdigit()):
        index = int(sheet)
        if not 0 <= index < len(names):
            raise ValueError(f"sheet {index} does not exist; the sheets are {', '.join(repr(n) for n in names)} "
                             f"(index 0 to {len(names) - 1})")
        return index
    if str(sheet) in names:
        return names.index(str(sheet))
    folded = [i for i, n in enumerate(names) if n.casefold() == str(sheet).casefold()]
    if len(folded) != 1:
        raise ValueError(f"the workbook has no sheet named {str(sheet)!r}; its sheets are "
                         f"{', '.join(repr(n) for n in names)}")
    return folded[0]


def workbook_core(path: str, temp_dir: str, sheet: Any = None) -> dict:
    """Worker core: the workbook laid out for a screenshot in ``temp_dir``
    and, with ``sheet``, set to print that sheet alone. ``path`` is None when
    the workbook could not be laid out (an .xls: the original renders);
    ``error`` names the sheets when ``sheet`` is not one of them."""
    from formats.xlsx.screenshot import prepare_for_screenshot

    try:
        copy, names = prepare_for_screenshot(path, temp_dir)
    except Exception as exc:  # noqa: BLE001  # the layout copy raises what the package does; the original renders
        return {"path": None, "reason": str(exc) or exc.__class__.__name__}
    if sheet is None:
        return {"path": copy, "names": names}
    try:
        index, names = select_sheet(copy, sheet)
    except ValueError as exc:
        return {"path": copy, "error": str(exc)}
    return {"path": copy, "names": names, "index": index}
