"""What saving a workbook through openpyxl would lose, read from the
original package before it is loaded.

openpyxl rebuilds the workbook from its own model, and what the model
lacks is gone after the save. What a person would miss (slicers, form
controls, shapes, threaded comments, sparklines, data connections...) is
refused unless the call carries allow_loss, which the person agreed to;
what is only simplified (chart styling, comment box sizes, printer driver
settings) is a warning. FOUNDATION's parts_lost check stays the safety net
after the save.
"""

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from formats.xlsx.package import related, sheet_targets

_MACRO_EXTS = frozenset({".xlsm", ".xltm"})

# Part folders and parts openpyxl drops, with what a person calls them.
_DROPPED_PARTS = (
    ("xl/slicers/", "slicers"), ("xl/slicerCaches/", "slicers"),
    ("xl/timelines/", "timelines"), ("xl/timelineCaches/", "timelines"),
    ("xl/ctrlProps/", "form controls (buttons, check boxes, lists)"),
    ("xl/activeX/", "ActiveX controls"),
    ("xl/embeddings/", "embedded objects (another document inside the workbook)"),
    ("xl/threadedComments/", "threaded comments (replies are lost; the first note stays)"),
    ("xl/connections.xml", "data connections"),
    ("xl/queryTables/", "query tables"),
    ("xl/model/", "a data model (Power Pivot)"),
    ("xl/richData/", "linked data types and pictures in cells"),
    ("xl/featurePropertyBag/", "check boxes in cells (they become TRUE and FALSE)"),
    ("xl/webextensions/", "Office add-in panes"),
)
# Dropped, but nothing the person sees: SharePoint and OneDrive keep a
# library's columns here and write them again when the file is uploaded.
_SHAREPOINT_PARTS = "customXml/"
_SHAREPOINT = "SharePoint document properties (the library sets them again on upload)"
_CHART_EX_TYPE = b"application/vnd.ms-office.chartex+xml"
_SHAPE_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?(?:sp|grpSp|cxnSp)[\s>]")
_VM_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\svm=")
_EXT_URI_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?ext\b[^>]*\suri=\"(\{[^\"]+\})\"")
_X14_CF_ID_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?cfRule\b[^>]*\sid=\"(\{[^\"]+\})\"")
_BASE_ID_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?id\b[^>]*>(\{[^<]+\})</(?:[A-Za-z_][\w.-]*:)?id>")
_PIVOT_CACHE_RE = re.compile(r"xl/pivotCache/pivotCacheDefinition[^/]*\.xml")
_CACHE_SOURCE_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?cacheSource\b([^>]*)>")
_SOURCE_TYPE_RE = re.compile(rb"\stype=\"([^\"]*)\"")
# A worksheet source carrying a relationship names a range in another
# workbook; openpyxl drops the relationship, so the pivot would read this one.
_OTHER_BOOK_SOURCE_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?worksheetSource\b[^>]*\s[A-Za-z_][\w.-]*:id=")
_VML_SIZE_RE = re.compile(rb"width:\s*([\d.]+)(pt|px);\s*height:\s*([\d.]+)(pt|px)")

_SPARKLINES = b"{05C60535-1F16-4fd2-B633-F4F36F0B64E0}"
_X14_CF = b"{78C0D931-6437-407d-A8EE-F0AAD7539E65}"
_X14_DV = b"{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}"
_BASE_CF_LINK = b"{B025F937-C7B1-47D3-B67F-A62EFF666E3E}"
# Sheet extensions accounted for: those above, the slicer and timeline
# lists (their parts are refused already), and Excel's harmless ones.
_KNOWN_SHEET_EXTS = frozenset({
    _SPARKLINES, _X14_CF, _X14_DV, _BASE_CF_LINK,
    b"{A8765BA9-456A-4dab-B4F3-ACF838C121DE}", b"{3A4CF648-6AED-40f4-86FF-DC5316D8AED3}",
    b"{7E03D99C-DC04-49d9-9315-930204A7B6E9}", b"{C3380CC4-5D6E-409C-BE32-E72D297353CC}",
})
# openpyxl's own comment box (and Excel's default).
_DEFAULT_COMMENT_SIZE = (108.0, 59.25)


@dataclass
class Loss:
    """One class of content an edit would drop or simplify: refused unless
    allow_loss, or a warning that says the save drops it (``drops``) or
    simplifies it."""
    what: str
    where: list[str] = field(default_factory=list)
    refuse: bool = True
    parts: list[str] = field(default_factory=list)
    drops: bool = False

    def line(self) -> str:
        return self.what + (f" (on {', '.join(sorted(set(self.where)))})" if self.where else "")


def _add(found: dict[str, Loss], what: str, *, where: str = "", refuse: bool = True, part: str = "",
         drops: bool = False) -> None:
    loss = found.setdefault(what, Loss(what, refuse=refuse, drops=drops))
    if where and where not in loss.where:
        loss.where.append(where)
    if part and part not in loss.parts:
        loss.parts.append(part)


def scan(path: str) -> list[Loss]:
    """What an edit of the workbook at ``path`` would lose; empty for a
    file that is not a readable package (the load reports that)."""
    try:
        with zipfile.ZipFile(path) as zf:
            return scan_zip(zf, Path(path).suffix.lower())
    except (OSError, zipfile.BadZipFile):
        return []


def scan_zip(zf: zipfile.ZipFile, ext: str) -> list[Loss]:
    found: dict[str, Loss] = {}
    names = zf.namelist()
    if "xl/vbaProject.bin" in names and ext not in _MACRO_EXTS:
        _add(found, "macros (the workbook is named .xlsx, so the save drops them: rename it to .xlsm to keep "
                    "them)", part="xl/vbaProject.bin")
    for name in names:
        for prefix, what in _DROPPED_PARTS:
            if name == prefix or name.startswith(prefix):
                _add(found, what, part=name)
        if name.startswith(_SHAREPOINT_PARTS):
            _add(found, _SHAREPOINT, refuse=False, part=name, drops=True)
    try:
        types = zf.read("[Content_Types].xml")
    except KeyError:
        types = b""
    if _CHART_EX_TYPE in types:
        for name in names:
            if posixpath.basename(name).startswith("chartEx"):
                _add(found, "charts of the newer kinds (waterfall, treemap, funnel, map...)", part=name)
    for part in outside_pivots(zf)[0]:
        _add(found, "pivot tables that read another workbook (they would read this one)", part=part)
    for title, part in sheet_targets(zf).items():
        _scan_sheet(zf, found, title, part)
    for name in names:
        if name.startswith("xl/printerSettings/"):
            _add(found, "printer driver settings (the page setup is kept)", refuse=False, part=name)
    return list(found.values())


def _scan_sheet(zf: zipfile.ZipFile, found: dict[str, Loss], title: str, part: str) -> None:
    try:
        data = zf.read(part)
    except KeyError:
        return
    if _VM_RE.search(data):
        _add(found, "linked data types and pictures in cells", where=title)
    uris = set(_EXT_URI_RE.findall(data))
    if _SPARKLINES in uris:
        _add(found, "sparklines", where=title)
    if _X14_DV in uris:
        _add(found, "dropdowns that read another sheet (stored the Excel 2010 way)", where=title)
    if _X14_CF in uris:
        linked = set(_BASE_ID_RE.findall(data))
        extended = set(_X14_CF_ID_RE.findall(data[data.find(_X14_CF):]))
        if extended - linked:
            _add(found, "conditional formats only newer Excel reads", where=title)
        if extended & linked:
            _add(found, "data bar and icon details (the rules stay, drawn simpler)", where=title, refuse=False)
    unknown = uris - _KNOWN_SHEET_EXTS
    if unknown:
        _add(found, "features of newer Excel versions this tool does not know", where=title, refuse=False)
    for drawing in related(zf, part, "/drawing"):
        try:
            shapes = _SHAPE_RE.search(zf.read(drawing))
        except KeyError:
            continue
        if shapes:
            _add(found, "shapes and text boxes", where=title, part=drawing)
        for chart in related(zf, drawing, "/chart"):
            _scan_chart(zf, found, title, chart)
    for vml in related(zf, part, "/vmlDrawing"):
        try:
            sizes = _VML_SIZE_RE.findall(zf.read(vml))
        except KeyError:
            continue
        if any((_pt(w, wu), _pt(h, hu)) != _DEFAULT_COMMENT_SIZE for w, wu, h, hu in sizes):
            _add(found, "comment box sizes (they return to the default)", where=title, refuse=False)


def _pt(number: bytes, unit: bytes) -> float:
    return float(number) * (0.75 if unit == b"px" else 1.0)


def _scan_chart(zf: zipfile.ZipFile, found: dict[str, Loss], title: str, chart: str) -> None:
    try:
        data = zf.read(chart)
    except KeyError:
        return
    styled = related(zf, chart, "/chartStyle") + related(zf, chart, "/chartColorStyle")
    if styled or b"AlternateContent" in data or b"<c:extLst" in data or b"userShapes" in data:
        what = "chart styling openpyxl does not model (colours, styles, label details)"
        _add(found, what, where=title, refuse=False)
        for part in styled:
            _add(found, what, part=part, refuse=False)


def outside_pivots(zf: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    """The pivot caches that read outside the workbook: (those over another
    workbook's range, those over a data connection or another source)."""
    other_book, other_source = [], []
    for name in zf.namelist():
        if not _PIVOT_CACHE_RE.fullmatch(name):
            continue
        try:
            data = zf.read(name)
        except KeyError:
            continue
        source = _CACHE_SOURCE_RE.search(data)
        if source is None:
            continue
        kind = _SOURCE_TYPE_RE.search(source.group(1))
        if _OTHER_BOOK_SOURCE_RE.search(data):
            other_book.append(name)
        elif (kind is not None and kind.group(1) != b"worksheet") or b"connectionId=" in source.group(1):
            other_source.append(name)
    return other_book, other_source


def refused(losses: list[Loss]) -> list[Loss]:
    return [loss for loss in losses if loss.refuse]


def refusal(path_shown: str, existed: bool, losses: list[Loss]) -> str:
    state = "is unchanged" if existed else "was not created"
    return "\n".join([
        f"Nothing saved: {path_shown} {state}. Saving it through write_xlsx would drop what this tool "
        "cannot keep (listed below). Ask the person whether to go ahead without it, then send the batch "
        "again with allow_loss: true; or make this change in Excel.",
        "Would be lost:",
        *[f"  - {loss.line()}" for loss in losses],
    ])


def warnings(losses: list[Loss], *, allowed: bool) -> list[str]:
    """The result's lines: what was dropped or simplified, and with
    allow_loss what was dropped as agreed."""
    out = []
    for loss in losses:
        if loss.refuse and allowed:
            out.append(f"dropped as agreed (allow_loss): {loss.line()}")
        elif not loss.refuse:
            out.append(f"{'dropped' if loss.drops else 'simplified'} by the save: {loss.line()}")
    return out


def declared(losses: list[Loss], *, allowed: bool) -> list[str]:
    """The parts a save that goes ahead drops on purpose (the result says
    so): every class that only warns, and the refused ones once allowed."""
    return [part for loss in losses if allowed or not loss.refuse for part in loss.parts]
