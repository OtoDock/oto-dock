"""Inputs write_docx took in manifest 1.3.1 keep working: string list
levels, a hyperlink paragraph past the end, a fill for a row that is not
there, one alternating colour, an empty colour, negative indexes meaning
the last."""

import asyncio

import pytest

pytest.importorskip("docx")

import word


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


def test_a_string_level_in_a_list_item(tmp_path):
    msg = _write(tmp_path / "a.docx", [{"op": "add_list", "items": ["one", {"text": "a", "level": "2"}]}],
                 create_new=True)
    assert "Failed:" not in msg


def test_a_hyperlink_paragraph_past_the_end_makes_a_new_paragraph(tmp_path):
    from docx import Document

    f = tmp_path / "b.docx"
    msg = _write(f, [{"op": "add_paragraph", "text": "x"},
                     {"op": "add_hyperlink", "url": "https://example.com", "text": "link", "paragraph_index": 9}],
                 create_new=True)
    assert msg.startswith("Document saved") and "paragraph_index 9 is past the end" in msg
    assert [p.text for p in Document(str(f)).paragraphs] == ["x", "link"]


def test_style_table_skips_what_it_cannot_use(tmp_path):
    f = tmp_path / "c.docx"
    msg = _write(f, [{"op": "add_table", "headers": ["A"], "rows": [["1"]]},
                     {"op": "style_table", "row_fill": {"7": "FFFF00"}, "alternating_colors": ["EEEEEE"]}],
                 create_new=True)
    assert msg.startswith("Document saved")
    assert "row_fill row 7 does not exist" in msg and "alternating_colors needs two colours" in msg


@pytest.mark.parametrize("op", [
    {"op": "add_hyperlink", "url": "https://example.com", "color": ""},
    {"op": "add_paragraph", "text": "x", "color": ""},
    {"op": "style_table", "header": {"fill": "", "font_color": ""}, "alternating_colors": ["", "EEEEEE"]},
])
def test_an_empty_colour_means_none(tmp_path, op):
    f = tmp_path / "d.docx"
    msg = _write(f, [{"op": "add_table", "headers": ["A"], "rows": [["1"]]}, op], create_new=True)
    assert msg.startswith("Document saved"), msg


def test_any_negative_index_means_the_last(tmp_path):
    from docx import Document

    f = tmp_path / "e.docx"
    msg = _write(f, [{"op": "add_table", "headers": ["A"], "rows": [["1"], ["2"]]},
                     {"op": "add_table", "headers": ["B"], "rows": [["3"]]},
                     {"op": "add_table_row", "table_index": -5, "values": ["4"]},
                     {"op": "delete_table_row", "table_index": -3, "row_index": -9}], create_new=True)
    assert msg.startswith("Document saved"), msg
    tables = Document(str(f)).tables
    assert [r.cells[0].text for r in tables[0].rows] == ["A", "1", "2"]
    assert [r.cells[0].text for r in tables[1].rows] == ["B", "3"]


def test_merge_still_refuses_a_negative_table_index(tmp_path):
    msg = _write(tmp_path / "f.docx", [{"op": "add_table", "headers": ["A", "B"]},
                                       {"op": "merge_table_cells", "table_index": -1, "start_row": 0,
                                        "start_col": 0, "end_row": 0, "end_col": 1}], create_new=True)
    assert msg.startswith("Nothing saved:") and "table_index -1 does not exist" in msg
