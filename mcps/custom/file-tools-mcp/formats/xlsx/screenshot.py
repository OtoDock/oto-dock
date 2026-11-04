"""A workbook copy laid out for a screenshot.

Each worksheet fits its columns to the page width: portrait up to ten
columns, landscape up to twenty, and past twenty landscape without fitting
(the columns would be unreadably small), on A4 unless the sheet names its
own paper. Only the page setup changes: the
copy keeps every other part, and every cell, byte for byte, so the values a
recalculation stored, the charts and whatever openpyxl would drop still
render. A re-save through openpyxl would print every formula blank.
"""

import os
import re
import zipfile
from pathlib import Path

from lxml import etree

from formats.xlsx.package import sheet_targets
from ooxml.order import insert_child
from ooxml.render_copy import A4

PORTRAIT_MAX_COLUMNS = 10
FIT_MAX_COLUMNS = 20
# The pageSetup attributes that name a paper: a sheet with none gets A4.
_PAPER_ATTRIBUTES = ("paperSize", "paperWidth", "paperHeight")

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
_NAME = rb"(?:[A-Za-z_][\w.-]*:)?"
_DATA_RE = re.compile(rb"<" + _NAME + rb"sheetData\b[^>]*?(?:/>|>.*?</" + _NAME + rb"sheetData>)", re.S)
_HOLE_RE = re.compile(rb"<" + _NAME + rb"sheetData/>")
_DIMENSION_RE = re.compile(rb"<" + _NAME + rb"dimension\b[^>]*?ref=\"([^\"]+)\"")
_CELL_COL_RE = re.compile(rb"<" + _NAME + rb"c\b[^>]*?\sr=\"([A-Z]{1,3})[0-9]")
_COL_LETTERS_RE = re.compile(r"([A-Z]{1,3})[0-9]*$")


def _width(letters: str) -> int:
    col = 0
    for ch in letters:
        col = col * 26 + ord(ch) - 64
    return col


def used_columns(data: bytes) -> int:
    """The sheet's last used column: its <dimension>, or the cells
    themselves when it has none."""
    m = _DIMENSION_RE.search(data)
    if m is not None:
        end = m.group(1).decode("ascii", "replace").split(":")[-1].upper()
        letters = _COL_LETTERS_RE.match(end)
        if letters is not None:
            return max(1, _width(letters.group(1)))
    return max((_width(m.group(1).decode()) for m in _CELL_COL_RE.finditer(data)), default=1)


def _child(parent, tag: str):
    found = parent.find(tag)
    return found if found is not None else insert_child(parent, etree.Element(tag))


def lay_out(data: bytes) -> bytes:
    """A worksheet part with its page set to fit its columns; any other
    part (a chart sheet) as it is."""
    span = _DATA_RE.search(data)
    if span is None:
        return data
    skeleton = data[:span.start()] + b"<sheetData/>" + data[span.end():]
    root = etree.fromstring(skeleton, _PARSER)
    if root.tag != f"{_MAIN}worksheet":
        return data
    columns = used_columns(data)
    setup = _child(root, f"{_MAIN}pageSetup")
    if not any(setup.get(name) for name in _PAPER_ATTRIBUTES):
        setup.set("paperSize", A4)
    setup.set("orientation", "portrait" if columns <= PORTRAIT_MAX_COLUMNS else "landscape")
    if columns <= FIT_MAX_COLUMNS:
        setup.set("fitToWidth", "1")
        setup.set("fitToHeight", "0")
        _child(_child(root, f"{_MAIN}sheetPr"), f"{_MAIN}pageSetUpPr").set("fitToPage", "1")
    out = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    hole = _HOLE_RE.search(out)
    return out[:hole.start()] + data[span.start():span.end()] + out[hole.end():]


def prepare_for_screenshot(path: str, temp_dir: str) -> tuple[str, list[str]]:
    """A copy of the workbook at ``path`` in ``temp_dir``, laid out for a
    screenshot, and its sheet names in order. Raises for a file that is
    not a workbook package (an .xls); the caller then renders the
    original."""
    out = os.path.join(temp_dir, Path(path).name)
    with zipfile.ZipFile(path) as zin:
        targets = sheet_targets(zin)
        if not targets:
            raise ValueError(f"{Path(path).name} has no worksheets to lay out")
        changed = {part: lay_out(zin.read(part)) for part in set(targets.values())}
        with zipfile.ZipFile(out, "w") as zout:
            for info in zin.infolist():
                zout.writestr(info, changed.get(info.filename) or zin.read(info.filename))
    return out, list(targets)
