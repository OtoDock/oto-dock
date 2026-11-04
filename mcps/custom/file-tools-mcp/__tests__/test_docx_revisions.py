"""Tracked changes: edits recorded as revisions at word edges, never across
a field, reference or another author's insertion; accepting and rejecting
every kind; the document's own switch; warnings for edits that cannot be
tracked."""

import asyncio
import zipfile

import pytest

pytest.importorskip("docx")

import checks
import word
from formats.docx import annotations, revisions
from formats.docx.package import open_document
from ooxml import validate

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f'xmlns:w="{W_NS}"'


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


def _write(path, ops, create_new=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops, "create_new": create_new}))


def _errors(path) -> list[str]:
    with checks.CheckContext(str(path), "docx") as ctx:
        return [f"{f.location}: {f.message}" for f in validate.validate_package(ctx)
                if f.severity is checks.Severity.ERROR]


def _body(path):
    return open_document(str(path)).element.body


def _shown(p) -> str:
    """The paragraph's text with deletions left out: what Word shows with
    markup off and the changes accepted."""
    return "".join(t.text or "" for t in p.iter(_w("t")))


def _old(p) -> str:
    """The paragraph's text as it was: deletions in, insertions out."""
    out = []
    for el in p.iter(_w("t"), _w("delText")):
        if not any(a.tag == _w("ins") for a in el.iterancestors()):
            out.append(el.text or "")
    return "".join(out)


def _doc(tmp_path, *texts, name="contract.docx"):
    f = tmp_path / name
    _write(f, [{"op": "add_paragraph", "text": t} for t in texts], create_new=True)
    return f


def test_a_tracked_replace_changes_only_the_word_that_differs(tmp_path):
    f = _doc(tmp_path, "Payment is due within 30 days of invoice.")
    msg = _write(f, [{"op": "search_and_replace", "find": "30 days", "replace": "45 days", "track": True,
                      "author": "Legal"}])
    assert "1 replaced as tracked changes" in msg, msg
    p = next(_body(f).iter(_w("p")))
    deleted = [d.xpath("string(.)") for d in p.iter(_w("del"))]
    inserted = [i.xpath("string(.)") for i in p.iter(_w("ins"))]
    assert (deleted, inserted) == (["30"], ["45"])
    assert {d.get(_w("author")) for d in p.iter(_w("del"), _w("ins"))} == {"Legal"}
    assert _shown(p) == "Payment is due within 45 days of invoice."
    assert _old(p) == "Payment is due within 30 days of invoice."
    assert _errors(f) == []


@pytest.mark.parametrize("old, new, deleted, inserted", [
    ("the Licensor", "the Licensee", ["Licensor"], ["Licensee"]),
    ("in 2025", "in 2026", ["2025"], ["2026"]),
    ("net revenue", "revenue", ["net "], []),
    ("revenue", "net revenue", [], ["net "]),
])
def test_tracked_edits_cut_at_word_edges(tmp_path, old, new, deleted, inserted):
    f = _doc(tmp_path, f"Clause: {old}.")
    _write(f, [{"op": "search_and_replace", "find": old, "replace": new, "track": True}])
    p = next(_body(f).iter(_w("p")))
    assert [d.xpath("string(.)") for d in p.iter(_w("del"))] == deleted
    assert [i.xpath("string(.)") for i in p.iter(_w("ins"))] == inserted
    assert _shown(p) == f"Clause: {new}."


def test_the_document_switch_decides_and_track_false_opts_out(tmp_path):
    f = _doc(tmp_path, "Within 30 days.")
    msg = _write(f, [{"op": "set_track_changes"}])
    assert "now records every change" in msg, msg
    _write(f, [{"op": "search_and_replace", "find": "30", "replace": "45"}])
    p = next(_body(f).iter(_w("p")))
    assert [i.get(_w("author")) for i in p.iter(_w("ins"))] == ["OtoDock"]
    msg = _write(f, [{"op": "search_and_replace", "find": "Within", "replace": "In", "track": False}])
    assert "as tracked changes" not in msg
    assert _shown(next(_body(f).iter(_w("p")))).startswith("In ")


def test_our_own_insertion_is_edited_in_place_and_another_authors_is_split(tmp_path):
    f = _doc(tmp_path, "Intro.")
    _write(f, [{"op": "insert_paragraph", "index": 1, "text": "Fee of 100 EUR.", "track": True,
                "author": "Legal"},
               {"op": "insert_paragraph", "index": 2, "text": "Fee of 200 EUR.", "track": True,
                "author": "Sales"}])
    msg = _write(f, [{"op": "search_and_replace", "find": "EUR", "replace": "euros", "track": True,
                      "author": "Legal"}])
    assert "2 replaced as tracked changes" in msg, msg
    legal, sales = list(_body(f).iter(_w("p")))[1:3]
    assert list(legal.iter(_w("del"))) == [] and _shown(legal) == "Fee of 100 euros."
    # In Sales' insertion: Legal's deletion nests in it, Legal's insertion splits it.
    nested = [d for d in sales.iter(_w("del")) if d.getparent().tag == _w("ins")]
    assert [(d.get(_w("author")), d.getparent().get(_w("author")), d.xpath("string(.)")) for d in nested] == \
        [("Legal", "Sales", "EUR")]
    assert [(i.get(_w("author")), i.xpath("string(.)")) for i in sales.iter(_w("ins")) if
            i.find(_w("r")) is not None and i.find(f".//{_w('t')}") is not None and
            i.get(_w("author")) == "Legal"] == [("Legal", "euros")]
    assert _shown(sales) == "Fee of 200 euros."
    ids = [el.get(_w("id")) for el in _body(f).iter() if isinstance(el.tag, str) and el.get(_w("id"))]
    assert len(ids) == len(set(ids))
    assert _errors(f) == []
    rejected = tmp_path / "rejected.docx"
    rejected.write_bytes(f.read_bytes())
    _write(rejected, [{"op": "reject_all_changes", "author": "Legal"}])
    # Legal's clause was Legal's own insertion: rejecting Legal takes it away.
    assert [_shown(p) for p in _body(rejected).iter(_w("p"))] == ["Intro.", "Fee of 200 EUR."]
    _write(f, [{"op": "reject_all_changes", "author": "Sales"}])
    # Sales' clause goes; the word Legal inserted into it stays, as in Word.
    shown = [_shown(p) for p in _body(f).iter(_w("p"))]
    assert shown[0] == "Intro." and "200" not in "".join(shown) and shown[-1].endswith("euros")
    assert _errors(f) == []


def test_a_reference_or_a_bookmark_never_goes_into_a_revision(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {W}><w:r><w:t xml:space="preserve">net </w:t></w:r><w:bookmarkStart w:id="5" w:name="b"/>'
        f'<w:r><w:t xml:space="preserve">sales grew</w:t></w:r><w:bookmarkEnd w:id="5"/>'
        f'<w:r><w:footnoteReference w:id="1"/></w:r><w:r><w:t xml:space="preserve"> fast</w:t></w:r></w:p>'))
    f = tmp_path / "marks.docx"
    doc.save(str(f))
    _write(f, [{"op": "search_and_replace", "find": "net sales grew", "replace": "revenue rose", "track": True}])
    p = next(_body(f).iter(_w("p")))
    for wrapper in p.iter(_w("del"), _w("ins")):
        assert wrapper.find(f".//{_w('bookmarkStart')}") is None
        assert wrapper.find(f".//{_w('footnoteReference')}") is None
    assert _shown(p) == "revenue rose fast"
    msg = _write(f, [{"op": "search_and_replace", "find": "rose fast", "replace": "jumped", "track": True}])
    assert "Changed nothing" in msg and "crosses" in msg, msg


def test_splitting_a_run_keeps_its_formatting_change_under_a_new_id(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {W}><w:r><w:rPr><w:b/><w:rPrChange w:id="3" w:author="R"><w:rPr/></w:rPrChange></w:rPr>'
        f'<w:t>alpha beta gamma</w:t></w:r></w:p>'))
    f = tmp_path / "split.docx"
    doc.save(str(f))
    _write(f, [{"op": "search_and_replace", "find": "beta", "replace": "delta", "track": True}])
    changes = [c.get(_w("id")) for c in _body(f).iter(_w("rPrChange"))]
    assert len(changes) >= 3 and len(set(changes)) == len(changes) and "3" in changes
    assert _errors(f) == []


def test_tracked_paragraph_text_is_a_word_diff(tmp_path):
    f = _doc(tmp_path, "Payment within 30 days.")
    msg = _write(f, [{"op": "set_paragraph_text", "target": "p0", "text": "Payment within 45 days of invoice.",
                      "track": True}])
    assert msg.startswith("Document saved"), msg
    p = next(_body(f).iter(_w("p")))
    assert [d.xpath("string(.)") for d in p.iter(_w("del"))] == ["30"]
    assert [i.xpath("string(.)") for i in p.iter(_w("ins"))] == ["45", " of invoice"]
    assert _shown(p) == "Payment within 45 days of invoice."
    assert _old(p) == "Payment within 30 days."
    assert _errors(f) == []


def test_tracked_paragraph_text_with_formatting_deletes_and_inserts_whole(tmp_path):
    f = _doc(tmp_path, "Old words.")
    _write(f, [{"op": "set_paragraph_text", "target": "p0", "runs": [{"text": "New", "bold": True}],
                "track": True}])
    p = next(_body(f).iter(_w("p")))
    assert [d.xpath("string(.)") for d in p.iter(_w("del"))] == ["Old words."]
    assert [i.xpath("string(.)") for i in p.iter(_w("ins"))] == ["New"]
    assert next(p.iter(_w("ins"))).find(f".//{_w('b')}") is not None


def test_a_tracked_paragraph_deletion_stays_until_accepted(tmp_path):
    f = _doc(tmp_path, "Keep.", "Remove this clause.", "Keep too.")
    msg = _write(f, [{"op": "delete_paragraph", "target": "p1", "track": True}])
    assert "marked deleted" in msg, msg
    paras = list(_body(f).iter(_w("p")))
    assert len(paras) == 3 and paras[1].find(f"{_w('pPr')}/{_w('rPr')}/{_w('del')}") is not None
    assert _errors(f) == []
    accepted = tmp_path / "accepted.docx"
    accepted.write_bytes(f.read_bytes())
    msg = _write(accepted, [{"op": "accept_all_changes"}])
    assert "accepted:" in msg, msg
    assert [_shown(p) for p in _body(accepted).iter(_w("p"))] == ["Keep.", "Keep too."]
    msg = _write(f, [{"op": "reject_all_changes"}])
    assert [_shown(p) for p in _body(f).iter(_w("p"))] == ["Keep.", "Remove this clause.", "Keep too."]
    assert list(_body(f).iter(_w("del"))) == []


def test_accept_and_reject_settle_inserted_paragraphs(tmp_path):
    f = _doc(tmp_path, "One.", "Two.")
    _write(f, [{"op": "insert_paragraph", "index": 1, "text": "Inserted.", "track": True}])
    rejected = tmp_path / "rejected.docx"
    rejected.write_bytes(f.read_bytes())
    _write(f, [{"op": "accept_all_changes"}])
    assert [_shown(p) for p in _body(f).iter(_w("p"))] == ["One.", "Inserted.", "Two."]
    assert list(_body(f).iter(_w("ins"))) == []
    _write(rejected, [{"op": "reject_all_changes"}])
    assert [_shown(p) for p in _body(rejected).iter(_w("p"))] == ["One.", "Two."]
    assert _errors(rejected) == []


def _settled(tmp_path, xml, accept=True, name="settle.docx"):
    from docx import Document
    from docx.oxml import parse_xml

    doc = Document()
    body = doc.element.body
    for fragment in xml:
        body.insert(len(body) - 1, parse_xml(fragment))
    f = tmp_path / name
    doc.save(str(f))
    msg = _write(f, [{"op": "accept_all_changes" if accept else "reject_all_changes"}])
    return f, msg


def test_a_deleted_mark_with_text_left_joins_the_next_paragraph(tmp_path):
    f, msg = _settled(tmp_path, [
        f'<w:p {W}><w:pPr><w:jc w:val="center"/><w:rPr><w:del w:id="1" w:author="A"/></w:rPr></w:pPr>'
        f'<w:r><w:t xml:space="preserve">Kept </w:t></w:r></w:p>',
        f'<w:p {W}><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>joined</w:t></w:r></w:p>'])
    paras = list(_body(f).iter(_w("p")))
    assert [_shown(p) for p in paras] == ["Kept joined"]
    assert paras[0].find(f"{_w('pPr')}/{_w('jc')}") is not None


def test_a_deleted_mark_before_a_table_or_ending_a_section_is_handled(tmp_path):
    f, msg = _settled(tmp_path, [
        f'<w:p {W}><w:pPr><w:rPr><w:del w:id="1" w:author="A"/></w:rPr></w:pPr><w:r><w:t>Before</w:t></w:r></w:p>',
        f'<w:tbl {W}><w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>cell</w:t>'
        f'</w:r></w:p></w:tc></w:tr></w:tbl>',
        f'<w:p {W}><w:pPr><w:sectPr/><w:rPr><w:del w:id="2" w:author="A"/></w:rPr></w:pPr>'
        f'<w:r><w:t>Ends a section</w:t></w:r></w:p>'], name="table.docx")
    assert "ends a section was left as it is" in msg, msg
    body = _body(f)
    assert _shown(next(body.iter(_w("p")))) == "Before"
    assert body.find(_w("tbl")) is not None


def test_reject_restores_old_properties_and_keeps_the_section_break(tmp_path):
    f, _ = _settled(tmp_path, [
        f'<w:p {W}><w:pPr><w:jc w:val="center"/><w:sectPr/><w:pPrChange w:id="4" w:author="A"><w:pPr>'
        f'<w:jc w:val="right"/></w:pPr></w:pPrChange></w:pPr><w:r><w:rPr><w:b/><w:rPrChange w:id="5" '
        f'w:author="A"><w:rPr><w:i/></w:rPr></w:rPrChange></w:rPr><w:t>Text</w:t></w:r></w:p>'],
        accept=False, name="props.docx")
    p = next(_body(f).iter(_w("p")))
    assert p.find(f"{_w('pPr')}/{_w('jc')}").get(_w("val")) == "right"
    assert p.find(f"{_w('pPr')}/{_w('sectPr')}") is not None
    rpr = p.find(f"{_w('r')}/{_w('rPr')}")
    assert rpr.find(_w("i")) is not None and rpr.find(_w("b")) is None


def test_reject_warns_when_it_brings_back_an_active_field(tmp_path):
    _, msg = _settled(tmp_path, [
        f'<w:p {W}><w:r><w:t>See </w:t></w:r><w:del w:id="1" w:author="A"><w:r><w:fldChar w:fldCharType="begin"/>'
        f'</w:r><w:r><w:delInstrText> INCLUDETEXT "x.docx" </w:delInstrText></w:r><w:r>'
        f'<w:fldChar w:fldCharType="end"/></w:r></w:del></w:p>'], accept=False, name="field.docx")
    assert "brought back a field that fetches or runs something" in msg, msg


def test_accepting_a_deletion_takes_its_note_and_keeps_its_bookmark(tmp_path):
    from docx import Document
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import parse_xml

    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {W}><w:r><w:t xml:space="preserve">Kept </w:t></w:r><w:del w:id="2" w:author="A">'
        f'<w:bookmarkStart w:id="3" w:name="b"/><w:r><w:delText>gone</w:delText></w:r>'
        f'<w:r><w:footnoteReference w:id="1"/></w:r></w:del><w:r><w:t>end</w:t></w:r>'
        f'<w:bookmarkEnd w:id="3"/></w:p>'))
    notes = (f'<w:footnotes {W}><w:footnote w:id="1"><w:p><w:r><w:t>Note</w:t></w:r></w:p></w:footnote>'
             f'</w:footnotes>').encode()
    doc.part.relate_to(Part(PackURI("/word/footnotes.xml"), annotations.CT_FOOTNOTES, notes, doc.part.package),
                       annotations.RT_FOOTNOTES)
    f = tmp_path / "notes.docx"
    doc.save(str(f))
    _write(f, [{"op": "accept_all_changes"}])
    p = next(_body(f).iter(_w("p")))
    assert _shown(p) == "Kept end" and p.find(_w("bookmarkStart")) is not None
    with zipfile.ZipFile(f) as zf:
        assert b"Note" not in zf.read("word/footnotes.xml")


def test_only_one_authors_changes(tmp_path):
    f = _doc(tmp_path, "Alpha beta.")
    _write(f, [{"op": "search_and_replace", "find": "Alpha", "replace": "Gamma", "track": True, "author": "A"},
               {"op": "search_and_replace", "find": "beta", "replace": "delta", "track": True, "author": "B"}])
    _write(f, [{"op": "accept_all_changes", "author": "A"}])
    p = next(_body(f).iter(_w("p")))
    assert {w.get(_w("author")) for w in p.iter(_w("ins"), _w("del"))} == {"B"}
    assert revisions.authors(open_document(str(f))) == ["B"]


def test_edits_that_cannot_be_tracked_say_so(tmp_path):
    f = _doc(tmp_path, "Text.")
    _write(f, [{"op": "set_track_changes"}])
    msg = _write(f, [{"op": "add_table", "headers": ["A"], "rows": [["1"]]}])
    assert "not recorded as a tracked change" in msg, msg
    msg = _write(f, [{"op": "set_properties", "title": "T"}])
    assert "not recorded" not in msg


def test_an_explicit_track_refuses_what_cannot_be_tracked(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    doc = Document()
    body = doc.element.body
    body.insert(0, parse_xml(f'<w:p {W}><w:r><w:t>Start</w:t></w:r></w:p>'))
    body.insert(1, parse_xml(f'<w:p {W}><w:pPr><w:sectPr/></w:pPr><w:r><w:t>Section end</w:t></w:r></w:p>'))
    f = tmp_path / "sect.docx"
    doc.save(str(f))
    msg = _write(f, [{"op": "delete_paragraph", "target": "p1", "track": True}])
    assert msg.startswith("Nothing saved") and "cannot be recorded as a tracked change: it ends a section" in msg
    _write(f, [{"op": "set_track_changes"}])
    msg = _write(f, [{"op": "delete_paragraph", "target": "p1"}])
    assert "not recorded as a tracked change: it ends a section" in msg, msg


def test_removing_personal_information_writes_a_neutral_author(tmp_path):
    from docx.oxml import parse_xml

    f = _doc(tmp_path, "Within 30 days.")
    doc = open_document(str(f))
    from ooxml.order import insert_child

    insert_child(doc.settings.element, parse_xml(f'<w:removePersonalInformation {W}/>'))
    doc.save(str(f))
    _write(f, [{"op": "search_and_replace", "find": "30", "replace": "45", "track": True, "author": "Legal"},
               {"op": "add_comment", "text": "Check.", "author": "Legal", "initials": "LG"}])
    revisions_found = list(_body(f).iter(_w("ins"), _w("del")))
    assert revisions_found
    for el in revisions_found:
        assert el.get(_w("author")) == "Author" and el.get(_w("date")) is None
    with zipfile.ZipFile(f) as zf:
        comments = zf.read("word/comments.xml").decode()
    assert 'w:author="Author"' in comments and "Legal" not in comments and "w:date=" not in comments
    assert "w:initials" not in comments
    assert _errors(f) == []


def test_an_author_alone_makes_the_edit_tracked(tmp_path):
    f = _doc(tmp_path, "Payment within thirty (30) days.")
    msg = _write(f, [{"op": "search_and_replace", "find": "thirty (30)", "replace": "forty-five (45)",
                      "author": "Legal"}])
    assert "as tracked changes" in msg, msg
    p = next(_body(f).iter(_w("p")))
    assert {el.get(_w("author")) for el in p.iter(_w("ins"), _w("del"))} == {"Legal"}
    msg = _write(f, [{"op": "search_and_replace", "find": "Payment", "replace": "Settlement", "author": "Legal",
                      "track": False}])
    assert "as tracked changes" not in msg and "author is ignored" in msg, msg


def test_revision_ids_never_repeat(tmp_path):
    f = _doc(tmp_path, "one two three four five.")
    _write(f, [{"op": "search_and_replace", "find": "two", "replace": "2", "track": True},
               {"op": "search_and_replace", "find": "four", "replace": "4", "track": True},
               {"op": "set_paragraph_text", "target": "p0", "text": "one 2 three 4 five six.", "track": True}])
    ids = [el.get(_w("id")) for el in _body(f).iter() if isinstance(el.tag, str) and el.get(_w("id"))]
    assert len(ids) == len(set(ids))
    assert _errors(f) == []


def test_a_new_paragraph_marks_the_mark_word_would(tmp_path):
    f = tmp_path / "marks.docx"
    _write(f, [{"op": "add_heading", "text": "Terms", "level": 1},
               {"op": "add_paragraph", "text": "Clause one."}], create_new=True)
    _write(f, [{"op": "insert_paragraph", "target": "p1", "text": "Clause two.", "track": True},
               {"op": "insert_paragraph", "target": "p0", "text": "Under the heading.", "track": True}])
    heading, under, one, two = list(_body(f).iter(_w("p")))
    marked = [p.find(f"{_w('pPr')}/{_w('rPr')}/{_w('ins')}") is not None for p in (heading, under, one, two)]
    # Clause two follows a paragraph like it: that one's mark is the new one;
    # the line under the heading is styled unlike it, so its own mark is.
    assert marked == [False, True, True, False]


def test_rejecting_several_new_paragraphs_leaves_the_original(tmp_path):
    f = _doc(tmp_path, "Intro.", "Heading stays.")
    _write(f, [{"op": "insert_paragraph", "target": "p0", "text": "New one.", "track": True},
               {"op": "insert_paragraph", "target": "p0", "position": "after", "text": "New two.", "track": True}])
    _write(f, [{"op": "add_paragraph", "text": "At the end.", "track": True},
               {"op": "add_paragraph", "text": "And after.", "track": True}])
    _write(f, [{"op": "reject_all_changes"}])
    assert [_shown(p) for p in _body(f).iter(_w("p"))] == ["Intro.", "Heading stays."]
    assert _errors(f) == []


def test_deleting_your_own_new_paragraph_removes_it_with_its_note(tmp_path):
    f = _doc(tmp_path, "Keep.", "Also keep.")
    _write(f, [{"op": "insert_paragraph", "target": "p0", "text": "Added clause.", "track": True}])
    _write(f, [{"op": "add_footnote", "target": "p1", "text": "Source.", "track": True},
               {"op": "add_comment", "target": "p1", "text": "Why?"}])
    msg = _write(f, [{"op": "delete_paragraph", "target": "p1", "track": True}])
    assert msg.startswith("Document saved"), msg
    assert [_shown(p) for p in _body(f).iter(_w("p"))] == ["Keep.", "Also keep."]
    assert list(_body(f).iter(_w("ins"))) == [] and list(_body(f).iter(_w("del"))) == []
    with zipfile.ZipFile(f) as zf:
        assert b"Source." not in zf.read("word/footnotes.xml")
        assert b"Why?" not in zf.read("word/comments.xml")
    assert _errors(f) == []


def test_deleting_a_note_you_added_removes_it(tmp_path):
    f = _doc(tmp_path, "Claim.")
    _write(f, [{"op": "add_footnote", "text": "Source.", "track": True}])
    msg = _write(f, [{"op": "delete_note", "target": "footnote1", "track": True}])
    assert "deleted the footnote" in msg and "already" not in msg, msg
    with zipfile.ZipFile(f) as zf:
        assert b"Source." not in zf.read("word/footnotes.xml")
        assert b"footnoteReference" not in zf.read("word/document.xml")


def test_an_index_delete_names_the_paragraph(tmp_path):
    f = _doc(tmp_path, "One.", "Two.")
    msg = _write(f, [{"op": "delete_paragraph", "index": 1, "track": True}])
    assert "p1 is marked deleted" in msg, msg


def test_track_false_in_a_document_that_enforces_tracking_warns(tmp_path):
    from docx.oxml import parse_xml

    from ooxml.order import insert_child

    f = _doc(tmp_path, "Within 30 days.")
    doc = open_document(str(f))
    insert_child(doc.settings.element, parse_xml(
        f'<w:documentProtection {W} w:edit="trackedChanges" w:enforcement="1"/>'))
    doc.save(str(f))
    msg = _write(f, [{"op": "search_and_replace", "find": "30", "replace": "45", "track": False}])
    assert "protection requires every edit to be a tracked change" in msg, msg
    msg = _write(f, [{"op": "search_and_replace", "find": "Within", "replace": "In"}])
    assert "protection requires" not in msg and "as tracked changes" in msg, msg



def test_tracked_rows_are_marked_and_settled(tmp_path):
    f = tmp_path / "rows.docx"
    _write(f, [{"op": "add_table", "headers": ["Item", "Fee"], "rows": [["Pens", "4"], ["Ink", "2"]]}],
           create_new=True)
    msg = _write(f, [{"op": "add_table_row", "values": ["Paper", "9"], "author": "Legal"},
                     {"op": "delete_table_row", "target": "t0/r1", "author": "Legal"}])
    assert "the row is marked deleted" in msg, msg
    rows = list(_body(f).iter(_w("tr")))
    assert len(rows) == 4
    assert rows[1].find(f"{_w('trPr')}/{_w('del')}") is not None
    assert rows[3].find(f"{_w('trPr')}/{_w('ins')}") is not None
    assert {i.xpath("string(.)") for i in rows[3].iter(_w("ins")) if i.find(_w("r")) is not None} == {"Paper", "9"}
    assert _errors(f) == []
    rejected = tmp_path / "rejected.docx"
    rejected.write_bytes(f.read_bytes())
    _write(f, [{"op": "accept_all_changes"}])
    assert [[_shown(p) for p in r.iter(_w("p"))] for r in _body(f).iter(_w("tr"))] == \
        [["Item", "Fee"], ["Ink", "2"], ["Paper", "9"]]
    _write(rejected, [{"op": "reject_all_changes"}])
    assert [[_shown(p) for p in r.iter(_w("p"))] for r in _body(rejected).iter(_w("tr"))] == \
        [["Item", "Fee"], ["Pens", "4"], ["Ink", "2"]]
    assert _errors(f) == [] and _errors(rejected) == []


def test_deleting_a_row_you_added_removes_it(tmp_path):
    f = tmp_path / "own-row.docx"
    _write(f, [{"op": "add_table", "headers": ["Item"], "rows": [["Pens"]]}], create_new=True)
    _write(f, [{"op": "add_table_row", "values": ["Paper"], "author": "Legal"}])
    msg = _write(f, [{"op": "delete_table_row", "target": "t0/r2", "author": "Legal"}])
    assert "marked deleted" not in msg, msg
    assert len(list(_body(f).iter(_w("tr")))) == 2


def test_accepting_the_changes_in_one_block_leaves_the_others(tmp_path):
    f = _doc(tmp_path, "Payment within 30 days.", "Interest at 8% a year.", "Notices by email.")
    _write(f, [{"op": "search_and_replace", "find": "30", "replace": "45", "author": "Legal"},
               {"op": "search_and_replace", "find": "8%", "replace": "6%", "author": "Legal"},
               {"op": "set_paragraph_text", "target": "p2", "text": "Notices by registered email.",
                "author": "Sales"}])
    msg = _write(f, [{"op": "accept_all_changes", "target": "p1", "expect_text": "Interest"}])
    assert "accepted: 1 deletion, 1 insertion" in msg, msg
    p0, p1, p2 = _body(f).iter(_w("p"))
    assert p1.find(f".//{_w('ins')}") is None and p1.find(f".//{_w('del')}") is None and _shown(p1) == \
        "Interest at 6% a year."
    assert _old(p0) == "Payment within 30 days." and _shown(p0) == "Payment within 45 days."
    assert p2.find(f".//{_w('ins')}") is not None
    msg = _write(f, [{"op": "reject_all_changes", "target": "p2", "author": "Legal"}])
    assert "p2 holds no tracked changes by Legal" in msg, msg
    msg = _write(f, [{"op": "reject_all_changes", "target": "p2", "author": "Sales"}])
    assert "rejected:" in msg, msg
    assert _shown(list(_body(f).iter(_w("p")))[2]) == "Notices by email."
    assert _errors(f) == []


def test_accepting_one_tracked_row(tmp_path):
    f = tmp_path / "row.docx"
    _write(f, [{"op": "add_table", "headers": ["Item"], "rows": [["Pens"]]}], create_new=True)
    _write(f, [{"op": "add_table_row", "values": ["Paper"], "author": "Legal"},
               {"op": "add_table_row", "values": ["Ink"], "author": "Legal"}])
    msg = _write(f, [{"op": "reject_all_changes", "target": "t0/r2", "expect_text": "Paper"}])
    assert "rejected: 1 row" in msg, msg
    rows = list(_body(f).iter(_w("tr")))
    assert [_shown(r) for r in rows] == ["Item", "Pens", "Ink"]
    assert rows[2].find(f"{_w('trPr')}/{_w('ins')}") is not None
    assert _errors(f) == []


def test_a_block_without_changes_says_so(tmp_path):
    f = _doc(tmp_path, "Payment within 30 days.", "Interest at 8% a year.")
    _write(f, [{"op": "search_and_replace", "find": "30", "replace": "45", "author": "Legal"}])
    msg = _write(f, [{"op": "accept_all_changes", "target": "p1"}])
    assert "p1 holds no tracked changes" in msg, msg


def test_a_block_already_marked_deleted_is_not_deleted_again(tmp_path):
    f = tmp_path / "twice.docx"
    _write(f, [{"op": "add_table", "headers": ["Item"], "rows": [["A"], ["B"], ["C"]]},
               {"op": "add_paragraph", "text": "One."}, {"op": "add_paragraph", "text": "Two."}], create_new=True)
    _write(f, [{"op": "set_track_changes"}])
    msg = _write(f, [{"op": "delete_table_row", "row_index": -1}, {"op": "delete_table_row", "row_index": -1}])
    assert "1 changed nothing" in msg and "row_index -1 names t0/r3, which is already marked deleted" in msg, msg
    rows = list(_body(f).iter(_w("tr")))
    assert [r.find(f"{_w('trPr')}/{_w('del')}") is not None for r in rows] == [False, False, False, True]
    msg = _write(f, [{"op": "delete_table_row", "target": "t0/r3"}])
    assert "t0/r3 is already marked deleted" in msg, msg
    msg = _write(f, [{"op": "delete_paragraph", "index": 1}, {"op": "delete_paragraph", "index": 1}])
    assert "index 1 names p1, which is already marked deleted" in msg, msg
    marks = [p.find(f"{_w('pPr')}/{_w('rPr')}/{_w('del')}") is not None for p in _body(f).iterchildren(_w("p"))]
    assert marks == [False, True], marks


@pytest.mark.parametrize("settle, texts", [("accept_all_changes", ["One.", "Two.", "Three.", "New clause."]),
                                           ("reject_all_changes", ["One.", "Two.", "Three."])])
def test_settling_a_new_last_paragraph_by_its_block_takes_its_mark(tmp_path, settle, texts):
    f = _doc(tmp_path, "One.", "Two.", "Three.")
    _write(f, [{"op": "add_paragraph", "text": "New clause.", "author": "Legal"}])
    msg = _write(f, [{"op": settle, "target": "p3", "expect_text": "New clause."}])
    assert "1 paragraph mark" in msg, msg
    paragraphs = list(_body(f).iterchildren(_w("p")))
    assert [_shown(p) for p in paragraphs] == texts
    assert not any(p.find(f".//{_w('ins')}") is not None for p in paragraphs)
    assert _errors(f) == []


def test_settling_the_first_of_two_new_paragraphs_leaves_the_second(tmp_path):
    f = _doc(tmp_path, "One.", "Two.")
    _write(f, [{"op": "add_paragraph", "text": "New A.", "author": "Legal"},
               {"op": "add_paragraph", "text": "New B.", "author": "Legal"}])
    _write(f, [{"op": "reject_all_changes", "target": "p2", "expect_text": "New A."}])
    assert [_shown(p) for p in _body(f).iterchildren(_w("p"))] == ["One.", "Two.", "New B."]
    _write(f, [{"op": "reject_all_changes"}])
    assert [_shown(p) for p in _body(f).iterchildren(_w("p"))] == ["One.", "Two."]
