"""Run-aware find and replace and template tokens in Word documents: a match
across runs takes the first run's formatting and leaves every other run
alone, boundaries (containers, field characters) are never crossed, and
every story (body, text boxes, headers, footers, notes, comments) is
reached without adding parts or touching the bytes of an unchanged one."""

import copy
import zipfile

import pytest

pytest.importorskip("docx")

from lxml import etree

from formats.docx import replace
from formats.docx.text import paragraph_text

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = (
    f'xmlns:w="{W_NS}" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml"'
)
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
CT = "application/vnd.openxmlformats-officedocument.wordprocessingml."


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _parse(xml: str):
    from docx.oxml import parse_xml

    return parse_xml(xml)


def _doc(*blocks: str):
    """A new document whose body holds ``blocks`` (XML of w:p, w:tbl or w:sdt)."""
    from docx import Document

    doc = Document()
    for block in blocks:
        doc.element.body.sectPr.addprevious(_parse(block))
    return doc


def _p(inner: str) -> str:
    return f"<w:p {NS}>{inner}</w:p>"


def _r(text: str, rpr: str = "", rsid: str = "") -> str:
    attr = f' w:rsidR="{rsid}"' if rsid else ""
    return f'<w:r{attr}>{f"<w:rPr>{rpr}</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _reopen(doc, tmp_path, name="doc.docx"):
    from formats.docx.package import open_document

    path = tmp_path / name
    doc.save(str(path))
    return open_document(str(path))


def _paras(doc):
    return list(doc.element.body.iter(_w("p")))


def _xml(element) -> bytes:
    return etree.tostring(element)


def _run(doc, find, replacement, **kwargs):
    options = {k: kwargs.pop(k) for k in ("match_case", "whole_word", "regex") if k in kwargs}
    return replace.replace_all(doc, replace.compile_pattern(find, **options), replacement, **kwargs)


# --- matching and editing within one paragraph -------------------------------


def test_every_occurrence_in_one_paragraph(tmp_path):
    doc = _reopen(_doc(_p(_r("cat and cat and cat"))), tmp_path)
    result = _run(doc, "cat", "dog")
    assert (result.total, result.skipped, len(result.hits)) == (3, 0, 1)
    assert result.hits[0].count == 3 and result.hits[0].scope == "body"
    assert result.changed_parts == [doc.part] and result.by_scope == {"body": 3}
    assert paragraph_text(_paras(doc)[0]) == "dog and dog and dog"


def test_match_across_three_runs_keeps_first_run_format(tmp_path):
    doc = _reopen(_doc(_p(
        _r("Say ", "<w:color w:val=\"FF0000\"/>") + _r("Hel", "<w:b/>") + _r("lo Wo", "<w:i/>")
        + _r("rld", "<w:u w:val=\"single\"/>") + _r(" now", "<w:strike/>"))), tmp_path)
    runs = _paras(doc)[0].findall(_w("r"))
    before, after = _xml(runs[0]), _xml(runs[4])
    result = _run(doc, "Hello World", "Bye")
    assert result.total == 1
    doc = _reopen(doc, tmp_path, "after.docx")
    runs = _paras(doc)[0].findall(_w("r"))
    assert len(runs) == 3
    assert _xml(runs[0]) == before and _xml(runs[2]) == after
    assert runs[1].find(f"{_w('rPr')}/{_w('b')}") is not None
    assert runs[1].findtext(_w("t")) == "Bye"
    assert paragraph_text(_paras(doc)[0]) == "Say Bye now"


def test_tail_run_keeps_its_rest_and_format():
    doc = _doc(_p(_r("Hel", "<w:b/>") + "<w:r><w:rPr><w:i/></w:rPr><w:t>lo world</w:t></w:r>"))
    _run(doc, "Hello", "Hi")
    runs = _paras(doc)[0].findall(_w("r"))
    assert [r.findtext(_w("t")) for r in runs] == ["Hi", " world"]
    assert runs[1].find(f"{_w('rPr')}/{_w('i')}") is not None
    assert runs[1].find(_w("t")).get(XML_SPACE) == "preserve"


def test_markers_between_runs_are_kept():
    doc = _doc(_p(
        _r("Hel") + '<w:bookmarkStart w:id="0" w:name="mark"/><w:proofErr w:type="spellStart"/>'
        + '<w:commentRangeStart w:id="3"/>' + _r("lo") + '<w:proofErr w:type="spellEnd"/>'
        + '<w:commentRangeEnd w:id="3"/><w:bookmarkEnd w:id="0"/>' + _r(" there")))
    assert _run(doc, "Hello", "Bye").total == 1
    tags = [etree.QName(c).localname for c in _paras(doc)[0]]
    assert tags == ["r", "bookmarkStart", "proofErr", "commentRangeStart", "proofErr",
                    "commentRangeEnd", "bookmarkEnd", "r"]
    assert paragraph_text(_paras(doc)[0]) == "Bye there"


def test_match_across_a_hyperlink_edge_is_skipped_and_counted():
    doc = _doc(_p(_r("see Foo") + f'<w:hyperlink r:id="rId99">{_r("Bar link")}</w:hyperlink>'))
    before = _xml(_paras(doc)[0])
    result = _run(doc, "FooBar", "X")
    assert (result.total, result.skipped) == (0, 1)
    assert [(h.count, h.skipped) for h in result.hits] == [(0, 1)]
    assert result.changed_parts == []
    assert _xml(_paras(doc)[0]) == before


def test_text_inside_a_hyperlink_is_replaced():
    doc = _doc(_p(_r("see ") + f'<w:hyperlink r:id="rId99">{_r("Bar link")}</w:hyperlink>'))
    assert _run(doc, "Bar", "Baz").total == 1
    link = _paras(doc)[0].find(_w("hyperlink"))
    assert link.findtext(f"{_w('r')}/{_w('t')}") == "Baz link"
    assert link.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id") == "rId99"


FIELD = (
    _r("Page ") + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    + '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
    + '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' + _r("12")
    + '<w:r><w:fldChar w:fldCharType="end"/></w:r>' + _r("3 more")
)


def test_complex_field_instruction_never_matched_result_replaceable():
    doc = _doc(_p(FIELD))
    assert _run(doc, "PAGE", "X").total == 0
    assert _paras(doc)[0].findtext(f".//{_w('instrText')}") == " PAGE "
    crossing = _run(doc, "123", "X")
    assert (crossing.total, crossing.skipped) == (0, 1)
    assert _run(doc, "Page 1", "X").skipped == 1
    assert _run(doc, "12", "7").total == 1
    texts = [t.text for t in _paras(doc)[0].iter(_w("t"))]
    assert texts == ["Page ", "7", "3 more"]


def test_field_nested_in_an_instruction_is_not_visible():
    begin, sep, end = (f'<w:r><w:fldChar w:fldCharType="{k}"/></w:r>' for k in ("begin", "separate", "end"))
    doc = _doc(_p(
        begin + "<w:r><w:instrText>IF </w:instrText></w:r>" + begin
        + "<w:r><w:instrText>MERGEFIELD x</w:instrText></w:r>" + sep + _r("INNER") + end
        + '<w:r><w:instrText> = 1 "a" "b"</w:instrText></w:r>' + sep + _r("OUTER") + end))
    assert _run(doc, "INNER", "X").total == 0
    assert _run(doc, "OUTER", "X").total == 1
    assert [t.text for t in _paras(doc)[0].iter(_w("t"))] == ["INNER", "X"]


def test_adjacent_insertions_are_separate_segments():
    doc = _doc(_p(f'<w:ins w:id="1" w:author="a">{_r("y")}</w:ins>'
                  f'<w:ins w:id="2" w:author="a">{_r("z")}</w:ins>'))
    assert _run(doc, "yz", "Q").skipped == 1


def test_deleted_text_never_matched_inserted_text_matched():
    doc = _doc(_p(
        '<w:del w:id="1" w:author="a"><w:r><w:delText>old</w:delText></w:r>'
        '<w:r><w:t>gone</w:t></w:r></w:del>'
        f'<w:ins w:id="2" w:author="a">{_r("new")}</w:ins>'))
    assert _run(doc, "old", "X").total == 0
    assert _run(doc, "gone", "X").total == 0
    assert _run(doc, "new", "fresh").total == 1
    p = _paras(doc)[0]
    assert p.findtext(f"{_w('del')}/{_w('r')}/{_w('delText')}") == "old"
    assert p.findtext(f"{_w('del')}/{_w('r')}/{_w('t')}") == "gone"
    assert p.findtext(f"{_w('ins')}/{_w('r')}/{_w('t')}") == "fresh"


def test_tab_and_break_in_the_find():
    doc = _doc(_p("<w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t></w:r>"),
               _p("<w:r><w:t>x</w:t><w:tab/><w:t>y</w:t></w:r>"))
    assert _run(doc, "a\tb\nc", "z").total == 1
    first, second = _paras(doc)
    assert [etree.QName(c).localname for c in first.find(_w("r"))] == ["t"]
    assert first.findtext(f"{_w('r')}/{_w('t')}") == "z"
    assert _run(doc, "\t", " - ").total == 1
    assert [t.text for t in second.iter(_w("t"))] == ["x", " - ", "y"]
    assert second.find(f"{_w('r')}/{_w('tab')}") is None


def test_line_break_and_tab_in_the_replacement():
    doc = _doc(_p(_r("Dear NAME, hi")))
    _run(doc, "NAME", "Line1\nLine2\tEnd")
    run = _paras(doc)[0].find(_w("r"))
    assert [(etree.QName(c).localname, c.text) for c in run] == [
        ("t", "Dear Line1"), ("br", None), ("t", "Line2"), ("tab", None), ("t", "End, hi")]


def test_several_matches_sharing_a_text_keep_their_order():
    doc = _doc(_p(_r("A x B x C")), _p(_r("fo") + _r("o bar foo")))
    assert _run(doc, "x", "1\n2").total == 2
    assert _run(doc, "foo", "Z\tW").total == 2
    first, second = _paras(doc)
    assert [(etree.QName(c).localname, c.text) for c in first.find(_w("r"))] == [
        ("t", "A 1"), ("br", None), ("t", "2 B 1"), ("br", None), ("t", "2 C")]
    assert [[(etree.QName(c).localname, c.text) for c in r] for r in second.findall(_w("r"))] == [
        [("t", "Z"), ("tab", None), ("t", "W")],
        [("t", " bar Z"), ("tab", None), ("t", "W")]]


def test_whitespace_at_a_text_edge_is_preserved():
    doc = _doc(_p("<w:r><w:t>X</w:t></w:r>"), _p("<w:r><w:t>aXb</w:t></w:r>"))
    _run(doc, "X", " y ")
    first, second = _paras(doc)
    t = first.find(f"{_w('r')}/{_w('t')}")
    assert (t.text, t.get(XML_SPACE)) == (" y ", "preserve")
    t = second.find(f"{_w('r')}/{_w('t')}")
    assert t.text == "a y b"


def test_regex_groups_expand_only_with_a_template():
    doc = _doc(_p(_r("mail ann@acme now")))
    pattern = replace.compile_pattern(r"(?P<user>\w+)@(\w+)", regex=True)
    assert replace.replace_all(doc, pattern, r"\2 for \g<user>", regex_template=True).total == 1
    assert paragraph_text(_paras(doc)[0]) == "mail acme for ann now"


def test_case_insensitive_literal_replacement_stays_literal():
    doc = _doc(_p(_r("ACME and acme")))
    assert _run(doc, "acme", r"\1 Ltd", match_case=False).total == 2
    assert paragraph_text(_paras(doc)[0]) == r"\1 Ltd and \1 Ltd"


def test_whole_word():
    doc = _doc(_p(_r("cat concat cat. cats")))
    assert _run(doc, "cat", "dog", whole_word=True).total == 2
    assert paragraph_text(_paras(doc)[0]) == "dog concat dog. cats"
    pattern = replace.compile_pattern("(?i)DOG", regex=True, whole_word=True)
    assert pattern.search("a dog") and not pattern.search("dogs")


def test_greek_case_insensitive_with_final_sigma():
    doc = _doc(_p(_r("ΤΗΣ ΣΟΦΙΑΣ")))
    assert _run(doc, "σοφιας", "γνώσης", match_case=False).total == 1
    assert paragraph_text(_paras(doc)[0]) == "ΤΗΣ γνώσης"
    assert _run(_doc(_p(_r("σοφίας"))), "ΣΟΦΊΑΣ", "x", match_case=False).total == 1
    assert _run(_doc(_p(_r("ΣΟΦΙΑΣ"))), "σοφιας", "x").total == 0


def test_limit_stops_in_document_order():
    doc = _doc(_p(_r("x x")), _p(_r("x x")), _p(_r("x")))
    result = _run(doc, "x", "y", limit=3)
    assert result.total == 3 and [h.count for h in result.hits] == [2, 1]
    assert [paragraph_text(p) for p in _paras(doc)] == ["y y", "y x", "x"]
    assert _run(doc, "x", "y", limit=0).total == 0


def test_zero_length_matches_are_ignored():
    doc = _doc(_p(_r("axxb")))
    before = _xml(_paras(doc)[0])
    assert _run(doc, "^", "Z", regex=True).total == 0
    assert _xml(_paras(doc)[0]) == before
    assert _run(doc, "x*", "Y", regex=True).total == 1
    assert paragraph_text(_paras(doc)[0]) == "aYb"


def test_hyphens_no_break_matched_soft_ignored():
    doc = _doc(_p("<w:r><w:t>e</w:t><w:noBreakHyphen/><w:t>mail co</w:t><w:softHyphen/>"
                  "<w:t>operate</w:t></w:r>"))
    assert _run(doc, "e‑mail", "email").total == 1
    assert _run(doc, "cooperate", "work").total == 1
    run = _paras(doc)[0].find(_w("r"))
    assert [(etree.QName(c).localname, c.text) for c in run] == [
        ("t", "email"), ("t", " work"), ("softHyphen", None)]


def test_empty_replacement_removes_emptied_runs_but_not_containers():
    doc = _doc(_p(_r("keep ") + f'<w:hyperlink r:id="rId9">{_r("drop")}</w:hyperlink>'))
    assert _run(doc, "drop", "").total == 1
    p = _paras(doc)[0]
    assert p.find(_w("hyperlink")) is not None and len(p.find(_w("hyperlink"))) == 0
    assert paragraph_text(p) == "keep "


# --- body structure: tables, content controls, text boxes --------------------


def test_nested_tables_and_content_controls(tmp_path):
    def sdt(pr: str, text: str) -> str:
        return (f"<w:sdt {NS}><w:sdtPr>{pr}</w:sdtPr><w:sdtContent>"
                f"<w:p>{_r(text)}</w:p></w:sdtContent></w:sdt>")

    doc = _doc(
        sdt("<w:showingPlcHdr/>", "Click TOKEN here"),
        sdt('<w:dataBinding w:xpath="/root/a" w:storeItemID="{0}"/>', "bound TOKEN TOKEN"),
        _p(_r("inline ") + "<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>"
           + _r("TOKEN") + "</w:sdtContent></w:sdt>"),
    )
    outer = doc.add_table(rows=1, cols=1)
    inner = outer.cell(0, 0).add_table(rows=1, cols=1)
    inner.cell(0, 0).paragraphs[0].add_run("deep TOKEN")
    doc = _reopen(doc, tmp_path)
    result = _run(doc, "TOKEN", "v")
    assert result.total == 5
    assert result.placeholder_cleared == 2 and result.data_bound == 2
    assert not list(doc.element.body.iter(_w("showingPlcHdr")))
    assert list(doc.element.body.iter(_w("dataBinding")))
    deep = doc.tables[0].cell(0, 0).tables[0].cell(0, 0).paragraphs[0]
    assert deep.text == "deep v"


def _textbox_paragraph() -> str:
    box = "<w:txbxContent><w:p>{0}</w:p><w:p>{1}</w:p></w:txbxContent>"
    content = box.format(_r("box TOKEN one"), _r("box TOKEN two"))
    return _p(
        _r("host TOKEN")
        + "<w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><wp:anchor>"
        + "<a:graphic><a:graphicData><wps:wsp><wps:txbx>" + content
        + "</wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
        + "<mc:Fallback><w:pict><v:shape><v:textbox>" + content
        + "</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>")


def _box_texts(doc, side: str) -> list[str]:
    holder = next(doc.element.body.iter(f"{{http://schemas.openxmlformats.org/markup-compatibility/2006}}{side}"))
    return [paragraph_text(p) for p in holder.iter(_w("p"))]


def test_text_box_both_copies_edited_counted_once(tmp_path):
    doc = _reopen(_doc(_textbox_paragraph()), tmp_path)
    flags = [fallback for _s, _part, _p, fallback in replace.iter_scope_paragraphs(doc, "body")]
    assert flags == [False, False, False, True, True]
    result = _run(doc, "TOKEN", "v")
    assert result.total == 3 and len(result.hits) == 3
    assert _box_texts(doc, "Choice") == _box_texts(doc, "Fallback") == ["box v one", "box v two"]
    host = _paras(doc)[0]
    assert host.findtext(f"{_w('r')}/{_w('t')}") == "host v"


def test_text_box_limit_keeps_both_copies_alike():
    doc = _doc(_textbox_paragraph())
    assert _run(doc, "TOKEN", "v", limit=2).total == 2
    assert _box_texts(doc, "Choice") == _box_texts(doc, "Fallback") == ["box v one", "box TOKEN two"]


def test_given_paragraphs_only_and_their_fallback_copy():
    doc = _doc(_p(_r("a TOKEN")), _textbox_paragraph())
    choice_second = list(next(doc.element.body.iter(
        "{http://schemas.openxmlformats.org/markup-compatibility/2006}Choice")).iter(_w("p")))[1]
    result = _run(doc, "TOKEN", "v", paragraphs=[choice_second])
    assert result.total == 1 and result.hits[0].paragraph is choice_second
    assert _box_texts(doc, "Fallback") == ["box TOKEN one", "box v two"]
    assert paragraph_text(_paras(doc)[0]) == "a TOKEN"
    with pytest.raises(ValueError, match="not in this document"):
        _run(doc, "TOKEN", "v", paragraphs=[_parse(_p(_r("loose")))])


# --- headers, footers, notes, comments ---------------------------------------


def _header_doc(tmp_path):
    doc = _doc(_p(_r("body TOKEN")))
    first = doc.sections[0]
    first.header.paragraphs[0].text = "default TOKEN"
    first.different_first_page_header_footer = True
    first.first_page_header.paragraphs[0].text = "first TOKEN"
    doc.settings.odd_and_even_pages_header_footer = True
    first.even_page_header.paragraphs[0].text = "even TOKEN"
    first.footer.paragraphs[0].text = "footer TOKEN"
    doc.add_section()
    shared = [r for r in doc.sections[0]._sectPr.findall(_w("headerReference"))
              if r.get(_w("type")) == "default"][0]
    doc.sections[1]._sectPr.insert(0, copy.deepcopy(shared))
    return _reopen(doc, tmp_path)


def test_headers_of_every_kind_shared_part_once(tmp_path):
    doc = _header_doc(tmp_path)
    refs = [s._sectPr.find(_w("headerReference")) for s in doc.sections]
    assert all(r is not None for r in refs)
    result = _run(doc, "TOKEN", "v", scope="headers")
    assert result.total == 3 and result.by_scope == {"header": 3}
    assert len(result.changed_parts) == 3 and {h.scope for h in result.hits} == {"header"}
    assert paragraph_text(_paras(doc)[0]) == "body TOKEN"
    footers = _run(doc, "TOKEN", "v", scope="footers")
    assert footers.total == 1 and footers.hits[0].scope == "footer"
    rest = _run(doc, "TOKEN", "v")
    assert rest.by_scope == {"body": 1}
    first = doc.sections[0]
    texts = {p.text for h in (first.header, first.first_page_header, first.even_page_header)
             for p in h.paragraphs}
    assert texts == {"default v", "first v", "even v"}


def test_no_header_part_is_ever_added(tmp_path):
    from docx import Document

    plain = _doc(_p(_r("plain TOKEN")))
    plain.add_section()
    first = tmp_path / "plain.docx"
    plain.save(str(first))
    doc = Document(str(first))
    for scope in ("all", "headers", "footers", "notes", "comments"):
        _run(doc, "TOKEN", "v", scope=scope)
    second = tmp_path / "plain-after.docx"
    doc.save(str(second))
    with zipfile.ZipFile(first) as a, zipfile.ZipFile(second) as b:
        assert sorted(a.namelist()) == sorted(b.namelist())
    assert not list(doc.element.body.iter(_w("headerReference")))


FOOTNOTES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<w:footnotes xmlns:w="' + W_NS.encode() + b'">'
    b'<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/><w:t>TOKEN</w:t></w:r>'
    b'</w:p></w:footnote><w:footnote w:id="1"><w:p><w:r><w:t>See TOKEN here.</w:t></w:r></w:p>'
    b"</w:footnote></w:footnotes>"
)
ENDNOTES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b'<w:endnotes xmlns:w="' + W_NS.encode() + b'">\n  <w:endnote w:id="1">\n'
    b"    <w:p><w:r><w:t>Nothing here.</w:t></w:r></w:p>\n  </w:endnote>\n</w:endnotes>"
)


def _add_part(doc, name: str, kind: str, blob: bytes) -> None:
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    part = Part(PackURI(f"/word/{name}.xml"), CT + kind + "+xml", blob, doc.part.package)
    doc.part.relate_to(part, RT + kind)


def test_notes_are_story_parts_and_untouched_notes_stay_canonically_equal(tmp_path):
    from lxml import etree
    doc = _doc(_p(_r("body")))
    _add_part(doc, "footnotes", "footnotes", FOOTNOTES)
    _add_part(doc, "endnotes", "endnotes", ENDNOTES)
    doc = _reopen(doc, tmp_path)
    notes = [p for s, _part, p, _f in replace.iter_scope_paragraphs(doc, "notes")]
    assert [paragraph_text(p) for p in notes] == ["See TOKEN here.", "Nothing here."]
    result = _run(doc, "TOKEN", "v")
    assert result.total == 1 and result.by_scope == {"footnote": 1}
    assert len(result.changed_parts) == 1 and result.hits[0].part is result.changed_parts[0]
    assert type(result.changed_parts[0]).__name__ == "NotesPart"
    path = tmp_path / "after.docx"
    doc.save(str(path))
    with zipfile.ZipFile(path) as zf:
        footnotes = zf.read("word/footnotes.xml")
        endnotes = zf.read("word/endnotes.xml")
    assert b"See v here." in footnotes and b"<w:t>TOKEN</w:t>" in footnotes

    def canonical(data: bytes) -> bytes:
        parser = etree.XMLParser(remove_blank_text=True)
        return etree.tostring(etree.fromstring(data, parser), method="c14n")

    assert canonical(endnotes) == canonical(ENDNOTES)


def test_comments_only_with_comments_scope():
    doc = _doc(_p(_r("anchor")))
    paragraph = doc.paragraphs[0]
    doc.add_comment(paragraph.runs, text="Comment TOKEN", author="A")
    assert _run(doc, "TOKEN", "v").total == 0
    assert "comment" not in {s for s, *_ in replace.iter_scope_paragraphs(doc, "all")}
    result = _run(doc, "TOKEN", "v", scope="comments")
    assert result.total == 1 and result.hits[0].scope == "comment"
    assert [c.text for c in doc.comments] == ["Comment v"]


def test_comments_held_as_bytes_are_written_back():
    doc = _doc(_p(_r("anchor")))
    _add_part(doc, "comments", "comments", (
        b'<w:comments xmlns:w="' + W_NS.encode() + b'"><w:comment w:id="0">'
        b"<w:p><w:r><w:t>old TOKEN</w:t></w:r></w:p></w:comment></w:comments>"))
    result = _run(doc, "TOKEN", "v", scope="comments")
    assert result.total == 1
    assert b"old v" in result.changed_parts[0].blob


def test_tracking_is_on():
    doc = _doc()
    assert replace.tracking_is_on(doc) is False
    settings = doc.settings.element
    settings.append(_parse(f'<w:trackRevisions {NS}/>'))
    assert replace.tracking_is_on(doc) is True
    settings.find(_w("trackRevisions")).set(_w("val"), "false")
    assert replace.tracking_is_on(doc) is False


# --- template tokens ----------------------------------------------------------


def test_token_split_over_three_runs_keeps_the_first_runs_format(tmp_path):
    doc = _reopen(_doc(_p(
        _r("Dear {{", "<w:b/>", "00A1") + _r(" client", "", "00B2")
        + _r(".name  }}, hi", "<w:i/>", "00C3"))), tmp_path)
    result = replace.fill_template(doc, {"client.name": "Ann"})
    assert result.filled == {"client.name": 1} and result.unresolved == [] and result.unused == []
    runs = _paras(doc)[0].findall(_w("r"))
    assert [r.findtext(_w("t")) for r in runs] == ["Dear Ann", ", hi"]
    assert runs[0].find(f"{_w('rPr')}/{_w('b')}") is not None
    assert runs[1].find(f"{_w('rPr')}/{_w('i')}") is not None


def test_missing_and_unused_keys_and_a_line_break_value():
    doc = _doc(_p(_r("To {{name}} at {{ missing }}")), _p(_r("{{addr}}")))
    result = replace.fill_template(doc, {"name": "Ann", "addr": "1 Road\nTown", "spare": "x"})
    assert result.filled == {"name": 1, "addr": 1}
    assert [(s, t) for _p, s, t in result.unresolved] == [("body", "{{ missing }}")]
    assert result.unresolved[0][0] is _paras(doc)[0]
    assert result.unused == ["spare"]
    assert paragraph_text(_paras(doc)[0]) == "To Ann at {{ missing }}"
    run = _paras(doc)[1].find(_w("r"))
    assert [etree.QName(c).localname for c in run] == ["t", "br", "t"]


def test_other_delimiters_and_one_pass():
    doc = _doc(_p(_r("[[a]] [[b]] {{a}}")))
    result = replace.fill_template(doc, {"a": "[[b]]", "b": "B"}, delimiters=("[[", "]]"))
    assert result.filled == {"a": 1, "b": 1}
    assert paragraph_text(_paras(doc)[0]) == "[[b]] B {{a}}"


def test_a_value_holding_a_token_is_not_filled_again():
    doc = _doc(_p(_r("{{a}} and {{b}}")))
    result = replace.fill_template(doc, {"a": "{{b}}", "b": "B"})
    assert result.filled == {"a": 1, "b": 1} and result.unresolved == []
    assert paragraph_text(_paras(doc)[0]) == "{{b}} and B"


def test_token_across_a_boundary_is_skipped():
    doc = _doc(_p(_r("{{na") + f'<w:hyperlink r:id="rId9">{_r("me}}")}</w:hyperlink>'))
    result = replace.fill_template(doc, {"name": "Ann"})
    assert [t for _p, _s, t in result.skipped] == ["{{name}}"]
    assert result.filled == {} and result.unused == [] and result.changed_parts == []


def test_tokens_in_headers_and_text_boxes(tmp_path):
    doc = _doc(_textbox_paragraph().replace("TOKEN", "{{t}}"))
    doc.sections[0].header.paragraphs[0].text = "head {{t}}"
    doc = _reopen(doc, tmp_path)
    result = replace.fill_template(doc, {"t": "v"})
    assert result.filled == {"t": 4}
    assert _box_texts(doc, "Fallback") == ["box v one", "box v two"]
    assert doc.sections[0].header.paragraphs[0].text == "head v"


# --- refusals ----------------------------------------------------------------


@pytest.mark.parametrize("delimiters", [("{{", "{{"), ("", "}}"), ("<\n", ">"), ("{{",)])
def test_bad_delimiters(delimiters):
    with pytest.raises(ValueError, match="delimiters"):
        replace.fill_template(_doc(), {"a": "b"}, delimiters=delimiters)


def test_bad_find_and_bad_replacement_are_refused_before_any_change():
    with pytest.raises(ValueError, match=r"^The regex does not compile: .+\.$"):
        replace.compile_pattern("(unclosed", regex=True)
    with pytest.raises(ValueError, match="empty"):
        replace.compile_pattern("")
    doc = _doc(_p(_r("a1 b2")))
    before = _xml(_paras(doc)[0])
    pattern = replace.compile_pattern(r"(\w)\d", regex=True)
    with pytest.raises(ValueError, match="template"):
        replace.replace_all(doc, pattern, r"\2", regex_template=True)
    with pytest.raises(ValueError, match="U\\+000B"):
        replace.replace_all(doc, pattern, "a\x0bb")
    with pytest.raises(ValueError, match="scope"):
        replace.replace_all(doc, pattern, "x", scope="everywhere")
    assert _xml(_paras(doc)[0]) == before


def test_a_match_never_bridges_an_equation():
    omml = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'
    doc = _doc(f'<w:p {NS} {omml}><w:r><w:t>foo</w:t></w:r><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath>'
               f"<w:r><w:t>bar</w:t></w:r></w:p>")
    result = replace.replace_all(doc, replace.compile_pattern("foobar"), "baz")
    assert result.total == 0 and result.skipped == 1


@pytest.mark.parametrize("content", [
    '<w:footnoteReference w:id="1"/>',
    '<w:commentReference w:id="4"/>',
    '<w:sym w:font="Wingdings" w:char="F0E0"/>',
])
def test_a_match_never_takes_a_reference_or_symbol_with_it(content):
    doc = _doc(_p(_r("net ") + f"<w:r>{content}</w:r>" + _r("revenue")))
    result = _run(doc, "net revenue", "income")
    assert result.total == 0 and result.skipped
    assert content.split()[0] in _xml(_paras(doc)[0]).decode()
    assert _run(doc, "revenue", "income").total == 1
    assert content.split()[0] in _xml(_paras(doc)[0]).decode()
