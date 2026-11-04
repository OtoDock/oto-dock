"""Opening, creating and naming Word packages.

python-docx opens only plain documents: its ``Document()`` refuses a
template (.dotx) or a macro-enabled file (.docm, .dotm) by the main part's
content type. The four main content types share one part class, so the
package opens through python-docx's own ``DocumentPart`` and the content
type follows the file's name at save. A macro project rides along as an
opaque part: never read, never run.
"""

import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DOCUMENT_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
TEMPLATE_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
MACRO_DOCUMENT_MAIN = "application/vnd.ms-word.document.macroEnabled.main+xml"
MACRO_TEMPLATE_MAIN = "application/vnd.ms-word.template.macroEnabledTemplate.main+xml"

MAIN_TYPE_FOR_SUFFIX = {
    ".docx": DOCUMENT_MAIN,
    ".dotx": TEMPLATE_MAIN,
    ".docm": MACRO_DOCUMENT_MAIN,
    ".dotm": MACRO_TEMPLATE_MAIN,
}
_MACRO_TYPES = frozenset({MACRO_DOCUMENT_MAIN, MACRO_TEMPLATE_MAIN})
_WORD_MAIN_TYPES = frozenset(MAIN_TYPE_FOR_SUFFIX.values())

# ISO 216 and US sizes in twips, portrait (short side, long side), as Word
# writes them.
PAGE_SIZES_TWIPS = {
    "a4": (11906, 16838),
    "letter": (12240, 15840),
    "legal": (12240, 20160),
    "a3": (16838, 23811),
    "a5": (8391, 11906),
}
PAGE_SIZE_LABELS = {"a4": "A4", "letter": "Letter", "legal": "Legal", "a3": "A3", "a5": "A5"}
NEW_DOCUMENT_MARGIN_TWIPS = 1440

_STRICT_NAMESPACE = "purl.oclc.org/ooxml"

# python-docx holds every XML part in memory, at several times its size: past
# these a worker would run out of memory instead of answering.
MAX_ENTRIES = 20_000
MAX_DOCUMENT_XML = 120 * 1024 * 1024
MAX_XML_TOTAL = 400 * 1024 * 1024
_MB = 1024 * 1024


def preflight(path: str) -> None:
    """Refuse a file too large to open, before anything loads it."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        raise ValueError("This file is not a valid Word document (it is not a zip package). If it is an "
                         "old .doc file, convert it with convert_document first.") from None
    if len(infos) > MAX_ENTRIES:
        raise ValueError(f"This Word file has {len(infos):,} parts, more than {MAX_ENTRIES:,}: it cannot be "
                         "opened here. Look at it with screenshot_document or convert it to PDF.")
    xml_total = sum(i.file_size for i in infos if i.filename.endswith((".xml", ".rels")))
    document = next((i.file_size for i in infos if i.filename == "word/document.xml"), 0)
    if document > MAX_DOCUMENT_XML or xml_total > MAX_XML_TOTAL:
        raise ValueError(
            f"This Word file is too large to open here (its text is {document // _MB:,} MB unpacked, its XML "
            f"{xml_total // _MB:,} MB; the limits are {MAX_DOCUMENT_XML // _MB} MB and {MAX_XML_TOTAL // _MB} MB). "
            "Split it, or look at it with screenshot_document or a PDF conversion.")


def _register_main_types() -> None:
    from docx.opc.part import PartFactory
    from docx.parts.document import DocumentPart

    from formats.docx.annotations import register_part_types

    for content_type in (TEMPLATE_MAIN, MACRO_DOCUMENT_MAIN, MACRO_TEMPLATE_MAIN):
        PartFactory.part_type_for.setdefault(content_type, DocumentPart)
    register_part_types()


def _is_strict(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            rels = zf.read("_rels/.rels")
    except (KeyError, OSError, zipfile.BadZipFile):
        return False
    return _STRICT_NAMESPACE.encode() in rels


def open_document(path: str) -> Any:
    """The python-docx Document of a .docx, .dotx, .docm or .dotm file."""
    from docx.package import Package

    _register_main_types()
    package = Package.open(path)
    try:
        part = package.main_document_part
    except KeyError:
        if _is_strict(path):
            raise ValueError(
                "This document is saved as Strict Open XML, which these tools cannot "
                "edit. Save it as a normal Word document (.docx) first."
            ) from None
        raise ValueError("This file has no main document part: it is not a Word document.") from None
    if part.content_type not in _WORD_MAIN_TYPES:
        raise ValueError(
            f"This file is not a Word document (its main part is {part.content_type})."
        )
    return part.document


def new_document() -> Any:
    """A blank document on an A4 portrait page with 1 inch margins."""
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Twips

    _register_main_types()
    doc = Document()
    # python-docx's template says it was written by python-docx in 2013.
    props = doc.core_properties
    now = datetime.now(UTC).replace(microsecond=0)
    props.author = ""
    props.comments = ""
    props.last_modified_by = ""
    props.created = now
    props.modified = now
    for section in doc.sections:
        short, long_ = PAGE_SIZES_TWIPS["a4"]
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_width, section.page_height = Twips(short), Twips(long_)
        margin = Twips(NEW_DOCUMENT_MARGIN_TWIPS)
        section.top_margin = section.bottom_margin = margin
        section.left_margin = section.right_margin = margin
    return doc


def match_content_type_to_name(doc: Any, path: str) -> None:
    """Give the main part the content type the file's name asks for.

    Word refuses a file whose name and main content type disagree. A plain
    document and a template differ only in that type, and so do the two
    macro-enabled kinds; a macro project under a name without macros is
    refused instead of being dropped.
    """
    wanted = MAIN_TYPE_FOR_SUFFIX.get(Path(path).suffix.lower())
    if wanted is None:
        return
    part = doc.part
    if part.content_type in _MACRO_TYPES and wanted not in _MACRO_TYPES:
        suffix = Path(path).suffix.lower()
        better = ".dotm" if suffix == ".dotx" else ".docm"
        raise ValueError(
            f"This file holds macros, so Word refuses it under the name {suffix}. "
            f"Save it with the {better} extension instead."
        )
    part._content_type = wanted
