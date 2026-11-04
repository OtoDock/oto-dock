"""Paragraph ids: fresh ones never collide with the package, stamping keeps
valid ids and settles duplicates, lookups reach headers and footers without
making parts, and a root that does not declare w14 is rebuilt to."""

import copy
import io
import random
import re
import zipfile

import pytest

pytest.importorskip("docx")

from formats.docx import ids

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
WP14_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
VML_NS = "urn:schemas-microsoft-com:vml"
PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
WML = "application/vnd.openxmlformats-officedocument.wordprocessingml."
PARA_ID = f"{{{ids.W14_NS}}}paraId"
TEXT_ID = f"{{{ids.W14_NS}}}textId"
IGNORABLE = f"{{{ids.MC_NS}}}Ignorable"

DECLS = (
    f'xmlns:w="{W_NS}" xmlns:r="{R_NS}" xmlns:w14="{ids.W14_NS}" xmlns:w15="{ids.W15_NS}" '
    f'xmlns:mc="{ids.MC_NS}" xmlns:wps="{WPS_NS}" xmlns:v="{VML_NS}" mc:Ignorable="w14 w15"'
)

KINDS = {
    "header": (WML + "header+xml", RT + "header"),
    "footer": (WML + "footer+xml", RT + "footer"),
    "footnotes": (WML + "footnotes+xml", RT + "footnotes"),
    "comments": (WML + "comments+xml", RT + "comments"),
    "commentsExtended": (
        WML + "commentsExtended+xml",
        "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
    ),
}


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _p(text: str, para: str | None = None, text_id: str | None = None, inner: str = "") -> str:
    pairs = (("paraId", para), ("textId", text_id))
    attrs = "".join(f' w14:{name}="{value}"' for name, value in pairs if value)
    return f"<w:p{attrs}><w:r><w:t>{text}</w:t></w:r>{inner}</w:p>"


def _text_box(text: str, para: str, text_id: str) -> str:
    """A run holding a text box: the mc:Choice paragraph and its mc:Fallback copy, same ids."""
    inner = _p(text, para, text_id)
    return (
        '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:txbx><w:txbxContent>'
        f"{inner}</w:txbxContent></wps:txbx></w:drawing></mc:Choice><mc:Fallback><w:pict><v:textbox>"
        f"<w:txbxContent>{inner}</w:txbxContent></v:textbox></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )


def _table(cell: str, row_attrs: str = "") -> str:
    return (f'<w:tbl><w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>'
            f"<w:tr{row_attrs}><w:tc>{cell}</w:tc></w:tr></w:tbl>")


def _sect_pr(*refs: tuple[str, str]) -> str:
    references = "".join(f'<w:{kind}Reference w:type="default" r:id="{rid}"/>' for kind, rid in refs)
    return f"<w:sectPr>{references}</w:sectPr>"


def _document(body: str, decls: str = DECLS) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:document {decls}><w:body>{body}</w:body></w:document>")


def _package(document_xml: str, parts: dict[str, tuple[str, str, str]] | None = None) -> bytes:
    """A minimal .docx: the document part and ``parts`` (rId -> (kind, file name under word/, XML))."""
    parts = parts or {}
    overrides = [f'<Override PartName="/word/document.xml" ContentType="{WML}document.main+xml"/>']
    rels = []
    for rid, (kind, name, _xml) in parts.items():
        content_type, reltype = KINDS[kind]
        overrides.append(f'<Override PartName="/word/{name}" ContentType="{content_type}"/>')
        rels.append(f'<Relationship Id="{rid}" Type="{reltype}" Target="{name}"/>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("[Content_Types].xml", (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>' + "".join(overrides) + "</Types>"))
        zf.writestr("_rels/.rels", (
            f'<Relationships xmlns="{PACKAGE_RELS}"><Relationship Id="rId1" '
            f'Type="{RT}officeDocument" Target="word/document.xml"/></Relationships>'))
        zf.writestr("word/_rels/document.xml.rels",
                    f'<Relationships xmlns="{PACKAGE_RELS}">{"".join(rels)}</Relationships>')
        zf.writestr("word/document.xml", document_xml)
        for _kind, name, xml in parts.values():
            zf.writestr(f"word/{name}", xml)
    return out.getvalue()


def _open(data: bytes):
    from docx import Document

    from formats.docx import package

    package._register_main_types()
    return Document(io.BytesIO(data))


def _saved(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _member(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(name).decode()


def _story(tag: str, content: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:{tag} {DECLS}>{content}</w:{tag}>'


def _part(doc, reltype: str):
    return doc.part.part_related_by(reltype)


def _element(xml: str):
    from docx.oxml import parse_xml

    return parse_xml(xml)


def _loose_p(para: str | None = None, text_id: str | None = None):
    p = _element(f'<w:p xmlns:w="{W_NS}" xmlns:w14="{ids.W14_NS}"><w:r><w:t>x</w:t></w:r></w:p>')
    for attr, value in ((PARA_ID, para), (TEXT_ID, text_id)):
        if value is not None:
            p.set(attr, value)
    return p


def _text(p) -> str:
    """The ``w:t`` text under ``p``: python-docx's own ``.text`` on paragraphs and
    runs makes lxml's ``itertext`` repeat it."""
    return "".join(t.text or "" for t in p.iter(_w("t")))


class ScriptedRng:
    """``randint`` returning the scripted values in order."""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, a: int, b: int) -> int:
        assert (a, b) == (1, 0x7FFFFFFF)
        return self.values.pop(0)


def test_new_ids_are_upper_case_hex_in_range_and_unique():
    from docx import Document

    alloc = ids.IdAllocator(Document(), rng=random.Random(1234))
    drawn = [alloc.new() for _ in range(5000)]
    assert all(re.fullmatch(r"[0-9A-F]{8}", v) and 0 < int(v, 16) < 0x80000000 for v in drawn)
    assert len(set(drawn)) == 5000
    assert ids.is_valid_id(ids.IdAllocator(Document()).new())


@pytest.mark.parametrize(("value", "valid"), [
    ("1A2B3C4D", True), ("1a2b3c4d", True), ("00000001", True), ("7FFFFFFF", True), ("77777777", True),
    ("00000000", False), ("80000000", False), ("FFFFFFFF", False), ("ZZZ", False), ("0", False),
    (None, False), ("", False), (" 1A2B3C4", False), ("1A2B_3C4", False), ("1A2B3C4D0", False),
])
def test_is_valid_id(value, valid):
    assert ids.is_valid_id(value) is valid


def _package_with_ids() -> bytes:
    body = (
        _p("Body", "11111111", "22222222")
        + _table(_p("Cell"), ' w14:paraId="0F0F0F0F" w14:textId="77777777"')
        + _sect_pr(("header", "rIdH1"))
    )
    comments_ex = (
        f'<w15:commentsEx xmlns:w15="{ids.W15_NS}" xmlns:mc="{ids.MC_NS}" mc:Ignorable="w15">'
        '<w15:commentEx w15:paraId="66666666" w15:paraIdParent="12121212" w15:done="0"/></w15:commentsEx>'
    )
    footnote = f'<w:footnote w:id="1">{_p("Note", "44444444", "55555555")}</w:footnote>'
    comment = f'<w:comment w:id="0" w:author="A">{_p("Remark", "13131313", "14141414")}</w:comment>'
    return _package(_document(body), {
        "rIdH1": ("header", "header1.xml", _story("hdr", _p("Head", "33333333", "77777777"))),
        "rIdFn": ("footnotes", "footnotes.xml", _story("footnotes", footnote)),
        "rIdCm": ("comments", "comments.xml", _story("comments", comment)),
        "rIdCx": ("commentsExtended", "commentsExtended.xml", comments_ex),
    })


def test_ids_used_anywhere_in_the_package_are_never_handed_out():
    doc = _open(_package_with_ids())
    assert _part(doc, KINDS["footnotes"][1]).element is not None
    assert _part(doc, KINDS["commentsExtended"][1]).element is not None
    taken = [0x11111111, 0x22222222, 0x0F0F0F0F, 0x33333333, 0x77777777, 0x44444444, 0x55555555,
             0x13131313, 0x14141414, 0x66666666, 0x12121212]
    rng = ScriptedRng([*taken, 0x0ABCDEF0, 0x0ABCDEF0, 0x0BADF00D, 0x0ABCDEF1])
    alloc = ids.IdAllocator(doc, rng=rng)
    alloc.reserve("0badf00d")
    alloc.reserve("not hex")
    assert alloc.new() == "0ABCDEF0"
    assert alloc.new() == "0ABCDEF1"
    assert rng.values == []


def test_the_package_is_read_on_first_use():
    from docx import Document

    doc = Document()
    alloc = ids.IdAllocator(doc, rng=ScriptedRng([0x21212121, 0x31313131]))
    doc.add_paragraph("Later")._p.set(PARA_ID, "21212121")
    assert alloc.new() == "31313131"


def test_stamp_leaves_a_valid_id_alone():
    p = _loose_p("1a2b3c4d", "77777777")
    assert ids.stamp(p, ids.IdAllocator(_open(_package(_document(""))), rng=random.Random(3))) == "1A2B3C4D"
    assert (p.get(PARA_ID), p.get(TEXT_ID)) == ("1a2b3c4d", "77777777")
    assert ids.paragraph_id(p) == "1A2B3C4D"


@pytest.mark.parametrize("bad", ["ZZZ", "80000000", "0", "00000000", None])
def test_stamp_fixes_a_missing_or_malformed_id(bad):
    p = _loose_p(bad, "ZZZ" if bad else None)
    assert ids.paragraph_id(p) is None
    value = ids.stamp(p, ids.IdAllocator(_open(_package(_document(""))), rng=random.Random(5)))
    assert ids.is_valid_id(value) and p.get(PARA_ID) == value == ids.paragraph_id(p)
    assert ids.is_valid_id(p.get(TEXT_ID)) and p.get(TEXT_ID) != value


def test_stamp_gives_a_missing_text_id_and_keeps_the_para_id():
    p = _loose_p("0000ABCD")
    ids.stamp(p, ids.IdAllocator(_open(_package(_document(""))), rng=random.Random(9)))
    assert p.get(PARA_ID) == "0000ABCD" and ids.is_valid_id(p.get(TEXT_ID))


def test_new_text_id_changes_the_text_id_only():
    alloc = ids.IdAllocator(_open(_package(_document(""))), rng=random.Random(11))
    p = _loose_p("1A2B3C4D", "77777777")
    ids.new_text_id(p, alloc)
    assert p.get(PARA_ID) == "1A2B3C4D"
    assert ids.is_valid_id(p.get(TEXT_ID)) and p.get(TEXT_ID) != "77777777"
    bare = _loose_p()
    ids.new_text_id(bare, alloc)
    assert bare.get(PARA_ID) is None and ids.is_valid_id(bare.get(TEXT_ID))


def test_stamp_part_keeps_the_first_of_a_duplicate_and_leaves_fallback_copies():
    from lxml import etree

    body = (
        _p("First", "0A000001", "0B000001")
        + _p("Second", "0A000001", "0B000002")
        + _p("Third")
        + _p("Fourth", "ZZZ", "0B000004")
        + _p("Host", "0A000005", "0B000005", inner=_text_box("Box", "0C000001", "0B000003"))
    )
    doc = _open(_package(_document(body)))
    root = doc.part.element
    paragraphs = {_text(p): p for p in root.iter(_w("p")) if not ids.is_fallback_copy(p)}
    fallback = [p for p in root.iter(_w("p")) if ids.is_fallback_copy(p)]
    assert len(fallback) == 1 and ids.paragraph_id(fallback[0]) == "0C000001"
    fallback_before = etree.tostring(fallback[0])
    alloc = ids.IdAllocator(doc, rng=random.Random(17))

    assert ids.stamp_part(root, alloc) == 3
    assert ids.paragraph_id(paragraphs["First"]) == "0A000001"
    assert ids.paragraph_id(paragraphs["Second"]) not in (None, "0A000001")
    assert paragraphs["Second"].get(TEXT_ID) == "0B000002"
    assert ids.paragraph_id(paragraphs["Box"]) == "0C000001"
    assert etree.tostring(fallback[0]) == fallback_before
    stamped = [ids.paragraph_id(p) for p in paragraphs.values()]
    assert None not in stamped and len(set(stamped)) == len(stamped)
    assert all(ids.is_valid_id(p.get(TEXT_ID)) for p in paragraphs.values())
    assert ids.stamp_part(root, alloc) == 0


def test_stamp_part_counts_ids_kept_in_parts_stamped_before():
    doc = _open(_package(
        _document(_p("Body", "0A000001", "0B000001") + _sect_pr(("header", "rIdH1"))),
        {"rIdH1": ("header", "header1.xml", _story("hdr", _p("Head", "0A000001", "0B000002")))},
    ))
    (_, body), (_, header) = ids.part_roots(doc)
    alloc = ids.IdAllocator(doc, rng=random.Random(19))
    assert ids.stamp_part(body, alloc) == 0
    assert ids.stamp_part(header, alloc) == 1
    assert ids.paragraph_id(next(body.iter(_w("p")))) == "0A000001"
    assert ids.paragraph_id(next(header.iter(_w("p")))) not in (None, "0A000001")
    assert ids.stamp_part(body, alloc) == 0


def test_a_removed_paragraph_gives_up_its_id():
    doc = _open(_package(_document(_p("Only", "0A000001", "0B000001"))))
    body = doc.part.element.body
    alloc = ids.IdAllocator(doc, rng=random.Random(23))
    assert ids.stamp_part(doc.part.element, alloc) == 0
    original = body[0]
    body.replace(original, copy.deepcopy(original))
    assert ids.stamp_part(doc.part.element, alloc) == 0
    assert ids.paragraph_id(body[0]) == "0A000001"


def _addressed_package() -> bytes:
    cell = _table(_p("Cell", "0B0B0B0B", "0B0B0B0B"))
    section_break = f'<w:p><w:pPr>{_sect_pr(("header", "rIdH1"), ("footer", "rIdF1"))}</w:pPr></w:p>'
    body = (
        _p("Body", "0A0A0A0A", "0A0A0A0A") + cell
        + _p("Host", "01010101", "01010101", inner=_text_box("Box", "0D0D0D0D", "0D0D0D0D"))
        + section_break + _sect_pr(("header", "rIdH1"), ("header", "rIdH2"))
    )
    return _package(_document(body), {
        "rIdH1": ("header", "header1.xml", _story("hdr", _p("Head", "0C0C0C0C", "0C0C0C0C"))),
        "rIdF1": ("footer", "footer1.xml", _story("ftr", _p("Foot", "0E0E0E0E", "0E0E0E0E"))),
        "rIdH2": ("header", "header2.xml", _story("hdr", _p("Head two", "0F0F0F0F", "0F0F0F0F"))),
    })


def test_part_roots_follow_the_section_references_once_each():
    doc = _open(_addressed_package())
    roots = ids.part_roots(doc)
    assert [str(part.partname) for part, _ in roots] == [
        "/word/document.xml", "/word/header1.xml", "/word/footer1.xml", "/word/header2.xml"]
    assert all(part.element is root for part, root in roots)


def test_find_by_id_reaches_body_cells_headers_and_footers():
    doc = _open(_addressed_package())

    def found(value):
        return [(str(part.partname), _text(p)) for part, p in ids.find_by_id(doc, value)]

    assert found("0a0a0a0a") == [("/word/document.xml", "Body")]
    assert found("0B0B0B0B") == [("/word/document.xml", "Cell")]
    assert ids.find_by_id(doc, "0B0B0B0B")[0][1].getparent().tag == _w("tc")
    assert found("0c0c0c0c") == [("/word/header1.xml", "Head")]
    assert found("0E0E0E0E") == [("/word/footer1.xml", "Foot")]
    assert found("0F0F0F0F") == [("/word/header2.xml", "Head two")]
    box = ids.find_by_id(doc, "0D0D0D0D")
    assert len(box) == 1 and not ids.is_fallback_copy(box[0][1])
    assert found("12345678") == [] and found("ZZZ") == []


def test_lookups_and_stamping_make_no_part():
    from docx import Document

    doc = Document()
    doc.add_paragraph("Only body")
    before = zipfile.ZipFile(io.BytesIO(_saved(doc))).namelist()
    assert ids.find_by_id(doc, "00000001") == []
    assert [part for part, _ in ids.part_roots(doc)] == [doc.part]
    alloc = ids.IdAllocator(doc, rng=random.Random(29))
    assert ids.stamp_part(doc.part.element, alloc) == 1
    after = zipfile.ZipFile(io.BytesIO(_saved(doc))).namelist()
    assert after == before
    assert not any(word in name for name in after for word in ("header", "footer", "comments"))


def test_ensure_w14_namespace_rebuilds_a_document_root_without_it():
    decls = (f'xmlns:w="{W_NS}" xmlns:r="{R_NS}" xmlns:mc="{ids.MC_NS}" xmlns:wp14="{WP14_NS}" '
             f'xmlns:w15="{ids.W15_NS}" mc:Ignorable="wp14 w15"')
    body = (_p("First") + _table(_p("Cell")) + _p("Last")
            + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>')
    doc = _open(_package(_document(body, decls)))
    part = doc.part
    old = part.element
    assert "w14" not in old.nsmap

    assert ids.ensure_w14_namespace(part) is True
    new = part.element
    assert new is not old and part._element is new and part.document.element is new
    assert type(new) is type(old)
    assert new.nsmap["w14"] == ids.W14_NS and new.nsmap["wp14"] == WP14_NS
    assert new.get(IGNORABLE) == "wp14 w15 w14"
    assert [p.text for p in part.document.paragraphs] == ["First", "Last"]

    alloc = ids.IdAllocator(doc, rng=random.Random(31))
    assert ids.stamp_part(new, alloc) == 3
    stamped = [ids.paragraph_id(p) for p in new.iter(_w("p"))]
    data = _saved(doc)
    xml = _member(data, "word/document.xml")
    assert f'xmlns:w14="{ids.W14_NS}"' in xml
    assert 'mc:Ignorable="wp14 w15 w14"' in xml
    assert re.findall(r'<w:p w14:paraId="([0-9A-F]{8})" w14:textId="[0-9A-F]{8}"', xml) == stamped
    assert "ns0" not in xml

    reopened = _open(data)
    assert [p.text for p in reopened.paragraphs] == ["First", "Last"]
    assert reopened.tables[0].cell(0, 0).text == "Cell"
    assert [ids.paragraph_id(p) for p in reopened.part.element.iter(_w("p"))] == stamped
    assert ids.ensure_w14_namespace(reopened.part) is False


def test_ensure_w14_namespace_adds_mc_when_the_root_has_none():
    from docx import Document

    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "Head"
    header = _part(doc, KINDS["header"][1])
    header._element = _element(f'<w:hdr xmlns:w="{W_NS}" xmlns:r="{R_NS}">{_p("Head")}</w:hdr>')

    assert ids.ensure_w14_namespace(header) is True
    root = header.element
    assert root.nsmap["w14"] == ids.W14_NS and root.nsmap["mc"] == ids.MC_NS
    assert root.get(IGNORABLE) == "w14"
    assert ids.stamp_part(root, ids.IdAllocator(doc, rng=random.Random(37))) == 1

    data = _saved(doc)
    xml = _member(data, str(header.partname).lstrip("/"))
    assert f'xmlns:mc="{ids.MC_NS}"' in xml and 'mc:Ignorable="w14"' in xml
    assert re.search(r'<w:p w14:paraId="[0-9A-F]{8}" w14:textId="[0-9A-F]{8}"', xml)
    assert _open(data).sections[0].header.paragraphs[0].text == "Head"


def test_ensure_w14_namespace_moves_a_parts_second_reference():
    from docx import Document

    doc = Document()
    paragraph = doc.add_paragraph("Commented")
    doc.add_comment(paragraph.runs, text="Remark")
    comments = _part(doc, KINDS["comments"][1])
    assert comments.element.get(IGNORABLE) is None

    assert ids.ensure_w14_namespace(comments) is True
    assert comments._comments is comments.element
    assert comments.element.get(IGNORABLE) == "w14"
    assert [c.text for c in _open(_saved(doc)).comments] == ["Remark"]


def test_ensure_w14_namespace_leaves_python_docx_parts_alone():
    from docx import Document

    doc = Document()
    doc.add_paragraph("Body")
    doc.sections[0].header.paragraphs[0].text = "Head"
    for part in (doc.part, _part(doc, KINDS["header"][1])):
        root = part.element
        before = part.blob
        assert ids.ensure_w14_namespace(part) is False
        assert part.element is root
        assert part.blob == before
