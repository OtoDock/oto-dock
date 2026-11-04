"""Lists with real numbering: nine levels, each list counting from 1,
continue on request, and documents that bring no numbering of their own."""

import asyncio
import zipfile

import pytest

pytest.importorskip("docx")

import word

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    """Paths resolve to themselves and the proxy hooks stay silent."""
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


def _write(path, ops, create_new=False, best_effort=False) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops,
                                               "create_new": create_new, "best_effort": best_effort}))


def _items(path):
    """(text, style name, ilvl, numId) of every list paragraph."""
    from docx import Document

    rows = []
    for p in Document(str(path)).paragraphs:
        num_pr = p._p.find(f"{_w('pPr')}/{_w('numPr')}")
        if num_pr is None:
            continue
        rows.append((p.text, p.style.name, int(num_pr.find(_w("ilvl")).get(_w("val"))),
                     num_pr.find(_w("numId")).get(_w("val"))))
    return rows


def _numbering(path):
    from docx import Document

    return Document(str(path)).part.numbering_part.element


def _definition(numbering, name):
    found = [a for a in numbering.findall(_w("abstractNum"))
             if a.find(_w("name")) is not None and a.find(_w("name")).get(_w("val")) == name]
    assert len(found) == 1
    return found[0]


def test_nine_levels_nest_with_growing_indents(tmp_path):
    f = tmp_path / "levels.docx"
    msg = _write(f, [{"op": "add_list", "items": [
        {"text": f"Level {n}", "level": n} for n in range(1, 10)]}])
    assert "Warnings" not in msg
    rows = _items(f)
    assert [r[2] for r in rows] == list(range(9))
    assert {r[1] for r in rows} == {"List Bullet"}
    assert len({r[3] for r in rows}) == 1
    definition = _definition(_numbering(f), "Bullet list")
    indents = [int(lvl.find(f"{_w('pPr')}/{_w('ind')}").get(_w("left")))
               for lvl in definition.findall(_w("lvl"))]
    assert indents == [360 * (n + 1) for n in range(9)]
    assert [lvl.find(_w("numFmt")).get(_w("val")) for lvl in definition.findall(_w("lvl"))] == ["bullet"] * 9


def test_numbered_levels_cycle_formats(tmp_path):
    f = tmp_path / "numbered.docx"
    _write(f, [{"op": "add_list", "ordered": True, "items": ["one", {"text": "a", "level": 2}]}])
    definition = _definition(_numbering(f), "Numbered list")
    lvls = definition.findall(_w("lvl"))
    assert [lvl.find(_w("numFmt")).get(_w("val")) for lvl in lvls[:4]] == [
        "decimal", "lowerLetter", "lowerRoman", "decimal"]
    assert [lvl.find(_w("lvlText")).get(_w("val")) for lvl in lvls[:2]] == ["%1.", "%2."]
    assert {r[1] for r in _items(f)} == {"List Number"}


def test_each_numbered_list_starts_at_one_unless_it_continues(tmp_path):
    f = tmp_path / "restart.docx"
    _write(f, [
        {"op": "add_list", "ordered": True, "items": ["One", "Two"]},
        {"op": "add_paragraph", "text": "Between"},
        {"op": "add_list", "ordered": True, "items": ["One again"]},
        {"op": "add_list", "ordered": True, "items": ["Three"], "continue": True},
    ])
    num_ids = [r[3] for r in _items(f)]
    assert num_ids[0] == num_ids[1]
    assert num_ids[2] != num_ids[0]
    assert num_ids[3] == num_ids[2]
    numbering = _numbering(f)
    for num in numbering.findall(_w("num")):
        if num.get(_w("numId")) in num_ids:
            starts = [o.find(_w("startOverride")).get(_w("val"))
                      for o in num.findall(_w("lvlOverride"))]
            assert starts == ["1"] * 9


def test_continue_carries_on_across_calls(tmp_path):
    f = tmp_path / "calls.docx"
    _write(f, [{"op": "add_list", "ordered": True, "items": ["First"]}])
    _write(f, [{"op": "add_list", "ordered": True, "items": ["Second"], "continue": True},
               {"op": "add_list", "ordered": True, "items": ["New list"]}])
    num_ids = [r[3] for r in _items(f)]
    assert num_ids[0] == num_ids[1] != num_ids[2]
    assert len(_numbering(f).findall(_w("abstractNum"))) == 9 + 1


def test_levels_outside_one_to_nine_are_clamped_with_a_warning(tmp_path):
    f = tmp_path / "clamp.docx"
    msg = _write(f, [{"op": "add_list", "items": [{"text": "deep", "level": 12},
                                                   {"text": "shallow", "level": 0}]}])
    assert "level 12 is outside 1-9; written at level 9." in msg
    assert "level 0 is outside 1-9; written at level 1." in msg
    assert [r[2] for r in _items(f)] == [8, 0]


def _strip_numbering_and_list_styles(path):
    """Like a file from Word's blank template: no numbering part, no list styles."""
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    doc = Document()
    part = doc.part
    for r_id, rel in list(part.rels.items()):
        if rel.reltype == RT.NUMBERING:
            part.drop_rel(r_id)
    styles = doc.styles.element
    for style in list(styles.findall(_w("style"))):
        name = style.find(_w("name"))
        if name is not None and name.get(_w("val")).startswith("List"):
            styles.remove(style)
    doc.save(str(path))
    with zipfile.ZipFile(path) as zf:
        assert "word/numbering.xml" not in zf.namelist()


def test_document_without_numbering_gets_its_own(tmp_path):
    f = tmp_path / "bare.docx"
    _strip_numbering_and_list_styles(f)
    msg = _write(f, [{"op": "add_list", "items": ["α", {"text": "β", "level": 2}]}])
    assert "Warnings" not in msg
    with zipfile.ZipFile(f) as zf:
        assert "word/numbering.xml" in zf.namelist()
        types = zf.read("[Content_Types].xml").decode()
    assert 'PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"' in types
    rows = _items(f)
    assert [(r[0], r[1], r[2]) for r in rows] == [("α", "Normal", 0), ("β", "Normal", 1)]
    _definition(_numbering(f), "Bullet list")


def test_list_paragraph_is_the_fallback_style(tmp_path):
    from docx import Document

    f = tmp_path / "fallback.docx"
    doc = Document()
    styles = doc.styles.element
    for style in list(styles.findall(_w("style"))):
        if style.find(_w("name")).get(_w("val")).startswith("List Number"):
            styles.remove(style)
    doc.save(str(f))
    _write(f, [{"op": "add_list", "ordered": True, "items": ["x"]}])
    assert _items(f)[0][1] == "List Paragraph"


def test_new_numbers_stay_above_dangling_references(tmp_path):
    from docx import Document

    f = tmp_path / "dangling.docx"
    doc = Document()
    p = doc.add_paragraph("points at a list that does not exist")
    num_pr = p._p.get_or_add_pPr().get_or_add_numPr()
    num_pr.get_or_add_ilvl().val = 0
    num_pr.get_or_add_numId().val = 40
    doc.save(str(f))
    _write(f, [{"op": "add_list", "items": ["new"]}])
    assert int(_items(f)[-1][3]) == 41


def test_definitions_precede_every_num(tmp_path):
    f = tmp_path / "order.docx"
    _write(f, [{"op": "add_list", "items": ["a"]}, {"op": "add_list", "ordered": True, "items": ["b"]}])
    tags = [child.tag for child in _numbering(f)]
    last_abstract = max(i for i, t in enumerate(tags) if t == _w("abstractNum"))
    first_num = min(i for i, t in enumerate(tags) if t == _w("num"))
    assert last_abstract < first_num



def test_a_foreign_definition_that_shares_the_name_is_not_adopted(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    f = tmp_path / "foreign.docx"
    doc = Document()
    numbering = doc.part.numbering_part.element
    foreign = parse_xml(
        f'<w:abstractNum xmlns:w="{W_NS}" w:abstractNumId="50"><w:name w:val="Bullet list"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1)"/><w:lvlJc w:val="left"/></w:lvl></w:abstractNum>')
    numbering.find(_w("num")).addprevious(foreign)
    doc.save(str(f))
    _write(f, [{"op": "add_list", "items": ["mine"]}])
    _write(f, [{"op": "add_list", "items": ["mine again"]}])
    named = [a for a in _numbering(f).findall(_w("abstractNum"))
             if a.find(_w("name")) is not None and a.find(_w("name")).get(_w("val")) == "Bullet list"]
    assert [a.get(_w("abstractNumId")) for a in named] == ["50", "51"]
    num_ids = {r[3] for r in _items(f)}
    refs = {num.find(_w("abstractNumId")).get(_w("val"))
            for num in _numbering(f).findall(_w("num")) if num.get(_w("numId")) in num_ids}
    assert refs == {"51"}


def test_numbering_with_picture_bullets_cleanup_and_a_header_list(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    f = tmp_path / "busy.docx"
    doc = Document()
    numbering = doc.part.numbering_part.element
    numbering.insert(0, parse_xml(
        f'<w:numPicBullet xmlns:w="{W_NS}" w:numPicBulletId="0"><w:pict/></w:numPicBullet>'))
    numbering.append(parse_xml(f'<w:numIdMacAtCleanup xmlns:w="{W_NS}" w:val="9"/>'))
    header_p = doc.sections[0].header.paragraphs[0]
    num_pr = header_p._p.get_or_add_pPr().get_or_add_numPr()
    num_pr.get_or_add_ilvl().val = 0
    num_pr.get_or_add_numId().val = 40
    doc.save(str(f))
    _write(f, [{"op": "add_list", "items": ["a"]},
               {"op": "add_list", "ordered": True, "items": ["1"]},
               {"op": "add_list", "items": ["b"], "continue": True}])
    tags = [child.tag.split("}")[1] for child in _numbering(f)]
    assert tags[0] == "numPicBullet" and tags[-1] == "numIdMacAtCleanup"
    assert max(i for i, t in enumerate(tags) if t == "abstractNum") < tags.index("num")
    rows = _items(f)
    assert all(int(r[3]) > 40 for r in rows)
    assert rows[0][3] == rows[2][3] != rows[1][3]
