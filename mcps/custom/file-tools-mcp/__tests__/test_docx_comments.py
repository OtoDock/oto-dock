"""Comments: on words or a whole block, replies in Word's threads, resolving
a thread, deleting a comment or a thread with every entry Word keeps for
it."""

import asyncio
import re
import zipfile

import pytest

pytest.importorskip("docx")

import checks
import word
from formats.docx import address as addr, annotations
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


def _xml(path, name) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode() if name in zf.namelist() else ""


def _contract(tmp_path):
    f = tmp_path / "contract.docx"
    _write(f, [{"op": "add_paragraph", "text": "Payment is due within thirty (30) days of invoice."},
               {"op": "add_table", "headers": ["Item", "Fee"], "rows": [["Support", "100"]]}], create_new=True)
    return f


def _comments(path) -> list[str]:
    doc = open_document(str(path))
    return ["".join(t.text or "" for t in el.iter(_w("t"))) for _, el in addr.annotation_order(doc, addr.COMMENT)]


def test_a_comment_covers_exactly_its_words(tmp_path):
    f = _contract(tmp_path)
    msg = _write(f, [{"op": "add_comment", "target": "p0", "on_text": "thirty (30) days",
                      "text": "Our standard is 45 days.", "author": "Legal Team"}])
    assert "made comment1" in msg, msg
    p = next(open_document(str(f)).element.body.iter(_w("p")))
    order = []
    for el in p:
        tag = el.tag.rsplit("}", 1)[1]
        if tag == "r":
            ref = el.find(_w("commentReference"))
            order.append("ref" if ref is not None else "".join(t.text for t in el.iter(_w("t"))))
        else:
            order.append(tag)
    assert order == ["Payment is due within ", "commentRangeStart", "thirty (30) days", "commentRangeEnd", "ref",
                     " of invoice."]
    comments = _xml(f, "word/comments.xml")
    assert 'w:author="Legal Team"' in comments and 'w:initials="LT"' in comments
    assert "<w:annotationRef/>" in comments and 'w14:paraId="' in comments
    assert _comments(f) == ["Our standard is 45 days."]
    assert _errors(f) == []


def test_a_comment_on_a_table_spans_its_paragraphs(tmp_path):
    f = _contract(tmp_path)
    _write(f, [{"op": "add_comment", "target": "t0", "text": "Fees to be confirmed."}])
    table = next(open_document(str(f)).element.body.iter(_w("tbl")))
    paragraphs = list(table.iter(_w("p")))
    assert paragraphs[0].find(_w("commentRangeStart")) is not None
    assert paragraphs[-1].find(_w("commentRangeEnd")) is not None
    assert _errors(f) == []


def test_replies_form_one_thread_in_order(tmp_path):
    f = _contract(tmp_path)
    _write(f, [{"op": "add_comment", "target": "p0", "on_text": "thirty", "text": "Why 30?", "author": "Legal"}])
    _write(f, [{"op": "reply_to_comment", "target": "comment1", "text": "Client asked.", "author": "Sales"}])
    msg = _write(f, [{"op": "reply_to_comment", "target": "comment2", "text": "Then fine.", "author": "Legal"}])
    assert "a reply in the thread of comment1" in msg, msg
    assert _comments(f) == ["Why 30?", "Client asked.", "Then fine."]
    extended = _xml(f, "word/commentsExtended.xml")
    entries = re.findall(r'<w15:commentEx w15:paraId="([0-9A-F]{8})"(?: w15:paraIdParent="([0-9A-F]{8})")?',
                         extended)
    head = entries[0][0]
    assert entries[0][1] == "" and [parent for _, parent in entries[1:]] == [head, head]
    assert _errors(f) == []


def test_resolving_marks_the_whole_thread(tmp_path):
    f = _contract(tmp_path)
    _write(f, [{"op": "add_comment", "target": "p0", "text": "One"}])
    _write(f, [{"op": "reply_to_comment", "target": "comment1", "text": "Two"}])
    msg = _write(f, [{"op": "resolve_comment", "target": "comment2"}])
    assert "thread of 2 comments is resolved" in msg, msg
    assert _xml(f, "word/commentsExtended.xml").count('w15:done="1"') == 2
    msg = _write(f, [{"op": "resolve_comment", "target": "comment1"}])
    assert "already resolved" in msg
    _write(f, [{"op": "resolve_comment", "target": "comment1", "resolved": False}])
    assert 'w15:done="1"' not in _xml(f, "word/commentsExtended.xml")


def test_deleting_a_thread_or_a_reply(tmp_path):
    f = _contract(tmp_path)
    _write(f, [{"op": "add_comment", "target": "p0", "text": "Root"}])
    _write(f, [{"op": "reply_to_comment", "target": "comment1", "text": "Reply A"}])
    _write(f, [{"op": "reply_to_comment", "target": "comment1", "text": "Reply B"}])
    msg = _write(f, [{"op": "delete_comment", "target": "comment2"}])
    assert "deleted 1 comment" in msg, msg
    assert _comments(f) == ["Root", "Reply B"]
    assert _xml(f, "word/commentsExtended.xml").count("<w15:commentEx") == 2
    msg = _write(f, [{"op": "delete_comment", "target": "comment1"}])
    assert "deleted 2 comments" in msg, msg
    assert _comments(f) == []
    document = _xml(f, "word/document.xml")
    assert "commentRange" not in document and "commentReference" not in document
    assert "<w15:commentEx" not in _xml(f, "word/commentsExtended.xml")
    assert _errors(f) == []


def test_word_companion_parts_get_entries_and_lose_them(tmp_path):
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    f = _contract(tmp_path)
    _write(f, [{"op": "add_comment", "target": "p0", "text": "Existing"}])
    doc = open_document(str(f))
    ids_xml = f'<w16cid:commentsIds xmlns:w16cid="{annotations.W16CID_NS}"/>'.encode()
    cex_xml = f'<w16cex:commentsExtensible xmlns:w16cex="{annotations.W16CEX_NS}"/>'.encode()
    for name, reltype, ctype, blob in (
            ("/word/commentsIds.xml", annotations.RT_COMMENTS_IDS, annotations.CT_COMMENTS_IDS, ids_xml),
            ("/word/commentsExtensible.xml", annotations.RT_COMMENTS_EXTENSIBLE, annotations.CT_COMMENTS_EXTENSIBLE,
             cex_xml)):
        doc.part.relate_to(Part(PackURI(name), ctype, blob, doc.part.package), reltype)
    doc.save(str(f))
    _write(f, [{"op": "add_comment", "target": "p0", "on_text": "invoice", "text": "New"}])
    ids_part, cex_part = _xml(f, "word/commentsIds.xml"), _xml(f, "word/commentsExtensible.xml")
    durable = re.findall(r'w16cid:durableId="([0-9A-F]{8})"', ids_part)
    assert len(durable) == 1 and f'w16cex:durableId="{durable[0]}"' in cex_part
    # "New" anchors at "invoice", before "Existing"'s mark at the paragraph's end.
    assert _comments(f) == ["New", "Existing"]
    _write(f, [{"op": "delete_comment", "target": "comment1"}])
    assert "durableId" not in _xml(f, "word/commentsIds.xml")
    assert "durableId" not in _xml(f, "word/commentsExtensible.xml")


def test_a_reply_to_a_comment_without_ids_stamps_it_first(tmp_path):
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import parse_xml

    f = _contract(tmp_path)
    doc = open_document(str(f))
    p = next(doc.element.body.iter(_w("p")))
    p.insert(0, parse_xml(f'<w:commentRangeStart {W} w:id="0"/>'))
    p.append(parse_xml(f'<w:commentRangeEnd {W} w:id="0"/>'))
    p.append(parse_xml(f'<w:r {W}><w:commentReference w:id="0"/></w:r>'))
    comments = (f'<w:comments {W}><w:comment w:id="0" w:author="LibreOffice user"><w:p><w:r><w:t>From LO'
                f'</w:t></w:r></w:p></w:comment></w:comments>').encode()
    doc.part.relate_to(Part(PackURI("/word/comments.xml"), annotations.CT_COMMENTS, comments, doc.part.package),
                       annotations.RT_COMMENTS)
    doc.save(str(f))
    msg = _write(f, [{"op": "reply_to_comment", "target": "comment1", "text": "Answer"}])
    assert msg.startswith("Document saved"), msg
    head_id = re.search(r'<w:comment [^>]*w:id="0"[^>]*>\s*<w:p [^>]*w14:paraId="([0-9A-F]{8})"',
                        _xml(f, "word/comments.xml"))
    assert head_id is not None
    assert f'w15:paraIdParent="{head_id.group(1)}"' in _xml(f, "word/commentsExtended.xml")
    assert _errors(f) == []


@pytest.mark.parametrize("op, why", [
    ({"op": "add_comment", "target": "header0/p0", "text": "x"}, "cannot hold a comment"),
    ({"op": "add_comment", "target": "p0", "on_text": "absent", "text": "x"}, "is not in the paragraph"),
    ({"op": "reply_to_comment", "target": "p0", "text": "x"}, "is not a comment"),
    ({"op": "insert_paragraph", "target": "comment1/p0", "text": "x"}, "only the text changes"),
])
def test_refusals(tmp_path, op, why):
    f = _contract(tmp_path)
    _write(f, [{"op": "add_header", "text": "Head"}, {"op": "add_comment", "text": "Note"}])
    msg = _write(f, [op])
    assert msg.startswith("Nothing saved") and why in msg, msg


def test_results_name_each_comment_by_its_final_number(tmp_path):
    f = _contract(tmp_path)
    msg = _write(f, [{"op": "add_comment", "target": "p0", "on_text": "invoice", "text": "Second in the text."},
                     {"op": "add_comment", "target": "p0", "on_text": "Payment", "text": "First in the text."}])
    assert "op #0 add_comment: made comment2" in msg and "op #1 add_comment: made comment1" in msg, msg
    assert _comments(f) == ["First in the text.", "Second in the text."]
