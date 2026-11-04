"""The Word checks: leftover tokens, empty and skipped headings, tables and
pictures wider than the page, each located by the address write_docx takes."""

import asyncio

import pytest

pytest.importorskip("docx")

import checks
import word
from formats.docx import document_checks  # noqa: F401  (registers the checks)

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


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


def _findings(path, check: str) -> list[checks.Finding]:
    with checks.CheckContext(str(path), "docx") as ctx:
        return checks.run_checks(ctx, phase="document", only=[f"docx.{check}"])


def test_leftover_tokens(tmp_path):
    f = tmp_path / "tokens.docx"
    _write(f, [{"op": "add_paragraph", "text": "Dear {{ name }}, see [[ref]]"},
               {"op": "add_table", "headers": ["Lorem ipsum dolor"]},
               {"op": "add_paragraph", "text": "A todo list is fine; TODO is not."}], create_new=True)
    found = _findings(f, "leftover_tokens")
    got = [(x.location.split(" (#")[0], x.excerpt) for x in found]
    assert sorted(got) == [("paragraph p0", "[[ref]]"), ("paragraph p0", "{{ name }}"),
                           ("paragraph p1", "TODO"), ("paragraph t0/r0/c0/p0", "Lorem ipsum")]
    assert all("fill_template" in x.fix for x in found)


def test_empty_heading_and_a_skipped_level(tmp_path):
    f = tmp_path / "heads.docx"
    msg = _write(f, [{"op": "add_heading", "text": "One", "level": 1},
                     {"op": "add_heading", "text": "", "level": 2},
                     {"op": "add_heading", "text": "Three", "level": 3}], create_new=True)
    assert "a level 3 heading follows a level 1 heading" in msg
    empty = _findings(f, "empty_heading")
    assert [x.location.split(" (#")[0] for x in empty] == ["paragraph p1"]


def test_wide_tables_and_pictures(tmp_path):
    from PIL import Image

    logo = tmp_path / "logo.png"
    Image.new("RGB", (8, 8), "navy").save(logo)
    f = tmp_path / "wide.docx"
    msg = _write(f, [{"op": "add_table", "headers": ["A", "B"]},
                     {"op": "set_table_column_widths", "widths": [5, 5]},
                     {"op": "add_image", "image_path": str(logo), "width_inches": 9}], create_new=True)
    assert "table t0: the table is 25.4 cm wide; the page's text area is 15.9 cm" in msg
    assert "a picture is 22.9 cm wide" in msg
    narrow = tmp_path / "ok.docx"
    msg = _write(narrow, [{"op": "add_table", "headers": ["A", "B"]}], create_new=True)
    assert "cm wide" not in msg and "Checks: no problems found." in msg


def test_findings_already_in_the_file_are_not_new(tmp_path):
    f = tmp_path / "inherited.docx"
    _write(f, [{"op": "add_heading", "text": "One", "level": 1}, {"op": "add_heading", "text": "Three", "level": 3}],
           create_new=True)
    msg = _write(f, [{"op": "add_paragraph", "text": "more"}])
    checks_part = msg.split("Checks:", 1)[1]
    assert "already in the file" in checks_part


# --- tables of contents, links, changes, comments ---------------------------


@pytest.fixture
def _no_layout(monkeypatch):
    from batch import Rendered

    async def render(staged, call, budget_s):
        return Rendered(note="no LibreOffice here")

    monkeypatch.setattr(word, "render_toc", render)


def test_toc_checks(tmp_path, _no_layout):
    from docx import Document
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {w}><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o '
        f'"1-3" \\h \\z \\u </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>Update '
        f'table of contents to see entries</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'))
    f = tmp_path / "toc.docx"
    doc.save(str(f))
    _write(f, [{"op": "add_heading", "text": "Scope", "level": 1}])
    assert [x.location.split(" (#")[0] for x in _findings(f, "toc_without_entries")] == ["paragraph p0"]
    _write(f, [{"op": "update_table_of_contents"}])
    assert _findings(f, "toc_without_entries") == []
    assert [x.severity for x in _findings(f, "toc_without_page_numbers")] == [checks.Severity.INFO]
    assert _findings(f, "toc_out_of_date") == []
    _write(f, [{"op": "add_heading", "text": "Annex", "level": 1}])
    assert len(_findings(f, "toc_out_of_date")) == 1
    _write(f, [{"op": "update_table_of_contents"}])
    assert _findings(f, "toc_out_of_date") == []


def test_broken_internal_link(tmp_path):
    f = tmp_path / "links.docx"
    _write(f, [{"op": "add_paragraph", "text": "Terms"}, {"op": "add_paragraph", "text": "See"}], create_new=True)
    _write(f, [{"op": "add_bookmark", "target": "p0", "name": "terms"},
               {"op": "add_hyperlink", "bookmark": "TERMS", "text": "terms", "target": "p1"}])
    assert _findings(f, "broken_internal_link") == []
    _write(f, [{"op": "delete_paragraph", "target": "p0"}])
    found = _findings(f, "broken_internal_link")
    assert [(x.severity, x.excerpt) for x in found] == [(checks.Severity.ERROR, "terms")]


def test_tracked_changes_and_open_comments(tmp_path):
    f = tmp_path / "review.docx"
    _write(f, [{"op": "add_paragraph", "text": "Within 30 days."}], create_new=True)
    assert _findings(f, "tracked_changes_left") == [] and _findings(f, "open_comments") == []
    _write(f, [{"op": "search_and_replace", "find": "30", "replace": "45", "track": True, "author": "Legal"},
               {"op": "add_comment", "text": "Why?"}])
    _write(f, [{"op": "reply_to_comment", "target": "comment1", "text": "Client asked."}])
    changes = _findings(f, "tracked_changes_left")
    assert len(changes) == 1 and "1 insertion and 1 deletion are still tracked by Legal" in changes[0].message
    assert [x.message for x in _findings(f, "open_comments")] == ["1 comment thread is still open"]
    _write(f, [{"op": "resolve_comment", "target": "comment1"}, {"op": "accept_all_changes"}])
    assert _findings(f, "open_comments") == [] and _findings(f, "tracked_changes_left") == []


@pytest.fixture
def _layout(monkeypatch):
    """A layout that puts every heading on page 7."""
    from batch import Rendered
    from formats.docx import fields

    async def render(staged, call, budget_s):
        return Rendered(payload="layout.pdf")

    monkeypatch.setattr(word, "render_toc", render)
    monkeypatch.setattr(fields, "read_pages", lambda pdf, names: {name: "7" for name, _ in names})


def test_a_numbered_toc_is_not_out_of_date(tmp_path, _layout):
    f = tmp_path / "numbered.docx"
    _write(f, [{"op": "add_heading", "text": "Introduction", "level": 1},
               {"op": "add_heading", "text": "Scope", "level": 2}], create_new=True)
    msg = _write(f, [{"op": "add_table_of_contents", "target": "p0", "position": "before"}])
    assert "toc_out_of_date" not in msg, msg
    assert _findings(f, "toc_out_of_date") == []


def test_a_link_whose_bookmark_differs_only_in_case(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = Document()
    doc.element.body.insert(0, parse_xml(
        f'<w:p {w}><w:bookmarkStart w:id="1" w:name="Target_A"/><w:r><w:t>Terms</w:t></w:r>'
        f'<w:bookmarkEnd w:id="1"/></w:p>'))
    doc.element.body.insert(1, parse_xml(f'<w:p {w}><w:hyperlink w:anchor="target_a"><w:r><w:t>see</w:t></w:r>'
                                         f'</w:hyperlink></w:p>'))
    f = tmp_path / "case.docx"
    doc.save(str(f))
    found = _findings(f, "broken_internal_link")
    assert [(x.severity, x.excerpt) for x in found] == [(checks.Severity.WARNING, "target_a")]
    assert "Target_A" in found[0].message
