"""A refused batch is kept, and the agent resends only the fix: the
refusal names a reference, the next call sends {"batch": ref, "replace",
"remove", "add_after"} and the kept batch runs again with the corrections,
all or nothing, with the checks once. Up to three correction rounds; a
reference that is gone, names another file or chat, or finds the file
changed since the refusal is refused in plain words."""

import asyncio
import re
import sys
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

sys.path.insert(0, str(Path(__file__).parent.parent))

from pptx import Presentation

import kept_batches
import powerpoint as ppt_mod
import shared


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _noop)
    monkeypatch.setattr(shared, "_notify_file_written", _noop)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)
    kept_batches.STORE.clear()
    yield
    kept_batches.STORE.clear()


def _call(args: dict) -> str:
    return asyncio.run(ppt_mod.handle_write_pptx(args))


def _write(path: Path, ops: list, create_new: bool = True) -> str:
    return _call({"path": str(path), "create_new": create_new, "operations": ops})


def _fix(path: Path, **fix) -> str:
    return _call({"path": str(path), "fix": fix})


def _ref(out: str) -> str:
    found = re.search(r'kept as "([a-z0-9-]+)"', out)
    assert found, out
    return found.group(1)


def _deck(bad_chart: dict | None = None, bad_key_at: int | None = None) -> list[dict]:
    """A 90-operation deck: 30 slides, each with a title colour and a card;
    every tenth slide a chart and its formatting."""
    ops: list[dict] = []
    for n in range(30):
        if n % 10 == 3:
            ops += [{"op": "add_slide", "layout": "title_only", "title": f"Chart {n}"},
                    {"op": "add_chart", "slide_index": n, "chart_type": "column_clustered",
                     "categories": ["Q1", "Q2"], "series": [{"name": "Revenue", "values": [12, 15]}],
                     "left": 1, "top": 1.6, "width": 8, "height": 4.5},
                    {"op": "format_chart", "slide_index": n, "data_labels": {"show": "value"}}]
        else:
            ops += [{"op": "add_slide", "layout": "title_only", "title": f"Slide {n}"},
                    {"op": "format_text", "slide_index": n, "find": "Slide", "color": "dk2"},
                    {"op": "add_shape", "slide_index": n, "text": f"Card {n}", "left": 1, "top": 2,
                     "width": 3, "height": 1.5}]
    assert len(ops) == 90
    if bad_chart is not None:
        ops[11] = {"op": "format_chart", "slide_index": 3, **bad_chart}
    if bad_key_at is not None:
        ops[bad_key_at] = {**ops[bad_key_at], "colour_of_the_box": "red"}
    return ops


_BAD_POSITION = {"data_labels": {"show": "value", "position": "middle"}}
_GOOD_CHART = {"op": "format_chart", "slide_index": 3, "data_labels": {"show": "value", "position": "outside_end"}}


def test_the_walks_deck_is_fixed_by_sending_one_replaced_operation(tmp_path):
    deck = tmp_path / "deck.pptx"
    out = _write(deck, _deck(bad_chart=_BAD_POSITION))
    assert out.startswith("Nothing saved: ") and "op #11 format_chart" in out, out
    assert not deck.exists()
    assert "3 correction rounds left" in out
    ref = _ref(out)
    import json
    shown = re.search(r"fix: (\{.*\}) instead of operations", out).group(1)
    assert json.loads(shown.replace("{...}", "{}").replace("[n]", "[0]").replace("[...]", "[]"))["batch"] == ref
    out = _fix(deck, batch=ref, replace={"11": _GOOD_CHART})
    assert out.startswith("Presentation saved: ") and "90 operations applied" in out, out
    assert out.count("Checks:") == 1
    prs = Presentation(str(deck))
    assert len(prs.slides) == 30
    plot = next(s for s in prs.slides[3].shapes if s.has_chart).chart.plots[0]
    assert plot.has_data_labels and plot.data_labels.show_value
    # The save drops the kept batch: its reference is gone.
    out = _fix(deck, batch=ref, replace={"11": _GOOD_CHART})
    assert out.startswith("Nothing saved: ") and f'no batch is kept as "{ref}"' in out.lower(), out


def test_a_batch_fixed_in_two_rounds(tmp_path):
    deck = tmp_path / "deck.pptx"
    # Round 0: an unknown key is refused before anything runs.
    out = _write(deck, _deck(bad_chart=_BAD_POSITION, bad_key_at=40))
    assert "op #40" in out and "colour_of_the_box" in out and "3 correction rounds left" in out, out
    ref = _ref(out)
    # Round 1 fixes the key; the run then finds the bad chart setting.
    fixed_shape = {k: v for k, v in _deck()[40].items()}
    out = _fix(deck, batch=ref, replace={"40": fixed_shape})
    assert out.startswith("Nothing saved: ") and "op #11 format_chart" in out, out
    assert _ref(out) == ref and "2 correction rounds left" in out, out
    # Round 2: the kept batch is the corrected one, so only the chart is sent.
    out = _fix(deck, batch=ref, replace={"11": _GOOD_CHART})
    assert out.startswith("Presentation saved: ") and "90 operations applied" in out, out


def test_after_three_rounds_the_whole_batch_is_asked_for(tmp_path):
    deck = tmp_path / "deck.pptx"
    out = _write(deck, _deck(bad_chart=_BAD_POSITION))
    ref = _ref(out)
    still_bad = {"op": "format_chart", "slide_index": 3, **_BAD_POSITION}
    for left in ("2 correction rounds left", "1 correction round left"):
        out = _fix(deck, batch=ref, replace={"11": still_bad})
        assert left in out and _ref(out) == ref, out
    out = _fix(deck, batch=ref, replace={"11": still_bad})
    assert "No correction rounds are left: send the whole batch again." in out, out
    assert "kept as" not in out and "the note below says how" not in out
    out = _fix(deck, batch=ref, replace={"11": _GOOD_CHART})
    assert out.startswith("Nothing saved: ") and "send the whole batch again" in out, out


def test_remove_and_add_after_and_a_bad_index(tmp_path):
    deck = tmp_path / "deck.pptx"
    ops = [{"op": "add_slide", "layout": "title_only", "title": "One"},
           {"op": "add_shape", "slide_index": 0, "text": "Card", "left": 1, "top": 2, "width": 3, "height": 1.5,
            "no_such_key": 1},
           {"op": "add_slide", "layout": "title_only", "title": "Two"}]
    ref = _ref(_write(deck, ops))
    out = _fix(deck, batch=ref, replace={"7": ops[0]})
    assert out.startswith("Nothing saved: ") and "7" in out and "batch has 3 operations" in out, out
    assert "kept" in out
    out = _fix(deck, batch=ref, remove=[1], add_after={"-1": [{"op": "set_core_properties", "title": "Deck"}],
                                                        "2": [{"op": "add_slide", "layout": "title_only",
                                                               "title": "Three"}]})
    assert out.startswith("Presentation saved: ") and "4 operations applied" in out, out
    prs = Presentation(str(deck))
    assert [s.shapes.title.text for s in prs.slides] == ["One", "Two", "Three"]
    assert prs.core_properties.title == "Deck"


def test_a_file_changed_since_the_refusal_is_refused(tmp_path):
    deck = tmp_path / "deck.pptx"
    _write(deck, [{"op": "add_slide", "layout": "title_only", "title": "One"}])
    ref = _ref(_write(deck, [{"op": "add_slide", "layout": "title_only", "title": "Two", "bad": 1}],
                      create_new=False))
    _write(deck, [{"op": "add_slide", "layout": "title_only", "title": "Elsewhere"}], create_new=False)
    out = _fix(deck, batch=ref, replace={"0": {"op": "add_slide", "layout": "title_only", "title": "Two"}})
    assert out.startswith("Nothing saved: ") and "send the whole batch again" in out, out
    assert [s.shapes.title.text for s in Presentation(str(deck)).slides] == ["One", "Elsewhere"]


def test_a_reference_that_expired_or_names_another_file_is_refused(tmp_path, monkeypatch):
    deck, other = tmp_path / "deck.pptx", tmp_path / "other.pptx"
    bad = [{"op": "add_slide", "layout": "title_only", "title": "One", "bad": 1}]
    ref = _ref(_write(deck, bad))
    out = _fix(other, batch=ref, replace={"0": {"op": "add_slide"}})
    assert out.startswith("Nothing saved: ") and "send the whole batch again" in out, out
    monkeypatch.setattr(kept_batches, "KEEP_S", 0.0)
    out = _fix(deck, batch=ref, replace={"0": {"op": "add_slide"}})
    assert out.startswith("Nothing saved: ") and "send the whole batch again" in out, out


def test_another_chat_cannot_use_the_reference(tmp_path, monkeypatch):
    deck = tmp_path / "deck.pptx"
    shared.set_request_context("chat-a", "")
    ref = _ref(_write(deck, [{"op": "add_slide", "layout": "title_only", "title": "One", "bad": 1}]))
    shared.set_request_context("chat-b", "")
    out = _fix(deck, batch=ref, replace={"0": {"op": "add_slide"}})
    assert out.startswith("Nothing saved: ") and "send the whole batch again" in out, out
    shared.set_request_context("", "")


def test_fix_and_operations_together_are_refused_and_a_plain_batch_still_works(tmp_path):
    deck = tmp_path / "deck.pptx"
    out = _call({"path": str(deck), "operations": [{"op": "add_slide"}], "fix": {"batch": "x"}})
    assert out.startswith("Nothing saved: ") and "fix or operations" in out, out
    assert _write(deck, [{"op": "add_slide"}]).startswith("Presentation saved: ")


def test_a_best_effort_batch_is_not_kept(tmp_path):
    out = _call({"path": str(tmp_path / "d.pptx"), "create_new": True, "best_effort": True,
                 "operations": [{"op": "add_slide"}, {"op": "add_slide", "bad": 1}]})
    assert "kept as" not in out


def test_the_store_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(kept_batches, "MAX_BATCHES", 2)
    refs = [_ref(_write(tmp_path / f"d{n}.pptx", [{"op": "add_slide", "bad": 1}])) for n in range(3)]
    assert len(kept_batches.STORE) == 2 and refs[0] not in kept_batches.STORE
    monkeypatch.setattr(kept_batches, "MAX_BYTES", 10)
    out = _write(tmp_path / "big.pptx", [{"op": "add_slide", "bad": 1}])
    assert out.startswith("Nothing saved: ") and "kept as" not in out


def test_the_write_tools_document_fix():
    import docx_tool
    import pdf_tool
    import pptx_tool
    import xlsx_tool

    for module in (docx_tool, pptx_tool, xlsx_tool, pdf_tool):
        for tool in module.TOOLS:
            if tool.name in ("write_docx", "write_xlsx", "write_pptx", "edit_pdf"):
                assert "fix" in tool.inputSchema["properties"], tool.name
                assert tool.inputSchema["required"] == ["path"], tool.name
                assert "fix" in tool.description, tool.name
                assert len(tool.description) <= 2048 - 40, tool.name


def test_the_whole_help_ends_with_how_to_fix_a_refused_batch(tmp_path):
    out = _call({"path": str(tmp_path / "d.pptx"), "operations": [{"op": "help"}]})
    assert out.rstrip().endswith(kept_batches.HELP_LINE), out[-400:]
    one = _call({"path": str(tmp_path / "d.pptx"), "operations": [{"op": "help", "name": "add_slide"}]})
    assert "REFUSED BATCHES" not in one


def _pdf(path: Path) -> Path:
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Page one")
    doc.save(str(path))
    return path


@pytest.mark.parametrize("tool, name, bad, good", [
    ("word", "r.docx", {"op": "add_paragraph", "text": "Hello", "colour_wrong": 1},
     {"op": "add_paragraph", "text": "Hello"}),
    ("excel", "b.xlsx", {"op": "write_cells", "start_cell": "A1", "values": [["x"]], "colour_wrong": 1},
     {"op": "write_cells", "start_cell": "A1", "values": [["x"]]}),
    ("pdf", "p.pdf", {"op": "rotate_page", "page": 5, "angle": 90}, {"op": "rotate_page", "page": 0, "angle": 90}),
])
def test_every_batch_writer_keeps_and_fixes(tmp_path, tool, name, bad, good):
    import excel
    import word
    from formats.pdf import edit

    handler = {"word": word.handle_write_docx, "excel": excel.handle_write_xlsx, "pdf": edit.handle_edit_pdf}[tool]
    path = tmp_path / name
    if tool == "pdf":
        _pdf(path)
    first = {"op": "add_paragraph", "text": "First"} if tool == "word" else None
    ops = [op for op in (first, bad) if op]
    out = asyncio.run(handler({"path": str(path), "operations": ops}))
    assert out.startswith("Nothing saved: ") and "kept as" in out, out
    n = len(ops) - 1
    out = asyncio.run(handler({"path": str(path), "fix": {"batch": _ref(out), "replace": {str(n): good}}}))
    assert not out.startswith("Nothing saved"), out


# A fix routes on the batch it runs: the kept batch with its corrections,
# not the call's own (absent) operations.

_BAD_ROTATE = {"op": "rotate_page", "page": 5, "angle": 90}


def _kept_pdf(path: Path) -> str:
    """A PDF whose refused batch is kept: the reference to fix."""
    from formats.pdf import edit

    out = asyncio.run(edit.handle_edit_pdf({"path": str(path), "operations": [_BAD_ROTATE]}))
    return _ref(out)


def _fix_pdf(path: Path, ref: str, op: dict) -> str:
    from formats.pdf import edit

    return asyncio.run(edit.handle_edit_pdf({"path": str(path), "fix": {"batch": ref, "replace": {"0": op}}}))


@pytest.fixture
def ocr_in_thread(monkeypatch):
    """OCR stubbed, a worker forbidden, and every core's deadline recorded
    (taken after the refusal, which runs in a worker)."""
    import isolation
    from formats.pdf import edit

    async def _no_spawn(*a, **k):
        raise AssertionError("a fixed OCR batch went to a worker")

    deadlines = []
    core = edit.edit_pdf_core

    def _core(call):
        deadlines.append(call.extra.get("deadline"))
        return core(call)

    monkeypatch.setattr(isolation, "run_parse", _no_spawn)
    monkeypatch.setattr(edit, "tesseract_page", edit._stub_ocr)
    monkeypatch.setattr(edit, "edit_pdf_core", _core)
    return deadlines


def test_a_fix_that_adds_ocr_runs_in_the_thread_with_its_deadline(tmp_path, request):
    path = _pdf(tmp_path / "scan.pdf")
    ref = _kept_pdf(path)
    deadlines = request.getfixturevalue("ocr_in_thread")
    out = _fix_pdf(path, ref, {"op": "ocr", "pages": "1", "language": "eng"})
    assert out.startswith("PDF saved:"), out
    assert len(deadlines) == 1 and deadlines[0] is not None


def test_a_fixed_ocr_batch_keeps_the_size_refusal(tmp_path, monkeypatch, request):
    from formats.pdf import edit

    path = _pdf(tmp_path / "big.pdf")
    ref = _kept_pdf(path)
    request.getfixturevalue("ocr_in_thread")
    monkeypatch.setattr(edit, "OCR_MAX_BYTES", 16)
    out = _fix_pdf(path, ref, {"op": "ocr", "pages": "1", "language": "eng"})
    assert out.startswith("Nothing saved:") and "OCR runs in the server process" in out, out


def test_an_extract_images_added_in_a_fix_gets_the_default_folder(tmp_path):
    import fitz
    from PIL import Image

    logo = tmp_path / "logo.png"
    Image.new("RGB", (10, 10), "blue").save(logo)
    doc = fitz.open()
    doc.new_page().insert_image(fitz.Rect(10, 10, 60, 60), filename=str(logo))
    path = tmp_path / "pics.pdf"
    doc.save(str(path))
    out = _fix_pdf(path, _kept_pdf(path), {"op": "extract_images"})
    assert "extracted 1 image(s) to pics_images" in out, out
    assert len(list((tmp_path / "pics_images").iterdir())) == 1


@pytest.fixture
def word_routes(monkeypatch):
    """The render phase each write_docx call is handed (None: one worker)."""
    import word

    seen = []
    real = word.handle_write_call

    async def _record(*args, **kwargs):
        seen.append(kwargs.get("render"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(word, "handle_write_call", _record)
    return seen


_WORD_FIRST = {"op": "add_heading", "text": "Terms", "level": 1}
_WORD_BAD = {"op": "add_paragraph", "text": "Body", "colour_wrong": 1}


@pytest.mark.parametrize("page_op", [
    {"op": "add_table_of_contents", "target": "p0", "position": "before"},
    {"op": "add_cross_reference", "bookmark": "terms", "show": "page", "target": "p0"},
    {"op": "update_fields"},
])
def test_a_fixed_word_batch_with_a_page_field_takes_the_layout_step(tmp_path, word_routes, page_op):
    import word

    path = tmp_path / "r.docx"
    out = asyncio.run(word.handle_write_docx({"path": str(path), "operations": [_WORD_FIRST, _WORD_BAD]}))
    assert word_routes == [None], out
    asyncio.run(word.handle_write_docx({"path": str(path), "fix": {"batch": _ref(out), "replace": {"1": page_op}}}))
    assert word_routes[1] is word.render_toc


def test_a_fixed_table_of_contents_gets_its_page_numbers(tmp_path, monkeypatch):
    import word
    from batch import Rendered
    from formats.docx import fields

    async def render(staged, call, budget_s):
        return Rendered(payload="layout.pdf")

    monkeypatch.setattr(word, "render_toc", render)
    monkeypatch.setattr(fields, "read_pages", lambda pdf, names, marks=(): {name: "7" for name, _ in names})
    path = tmp_path / "r.docx"
    out = asyncio.run(word.handle_write_docx({"path": str(path), "operations": [_WORD_FIRST, _WORD_BAD]}))
    toc = {"op": "add_table_of_contents", "levels": 1}
    out = asyncio.run(word.handle_write_docx({"path": str(path), "fix": {"batch": _ref(out), "replace": {"1": toc}}}))
    assert out.startswith("Document saved:") and "from a LibreOffice layout" in out, out


def test_a_word_batch_sent_as_a_json_string_routes_on_its_operations(tmp_path, word_routes):
    import json

    import word

    ops = [_WORD_FIRST, {"action": "add_table_of_contents", "target": "p0", "position": "before"}]
    out = asyncio.run(word.handle_write_docx({"path": str(tmp_path / "s.docx"), "operations": json.dumps(ops)}))
    assert word_routes == [word.render_toc], out


def test_a_fix_takes_its_kept_batch_once(tmp_path, monkeypatch):
    path = _pdf(tmp_path / "once.pdf")
    ref = _kept_pdf(path)
    taken = []
    take = kept_batches.take

    def _counted(*args, **kwargs):
        taken.append(args[0])
        return take(*args, **kwargs)

    monkeypatch.setattr(kept_batches, "take", _counted)
    out = _fix_pdf(path, ref, {"op": "rotate_page", "page": 0, "angle": 90})
    assert out.startswith("PDF saved:") and len(taken) == 1, out


@pytest.mark.parametrize("ops", [_deck(bad_chart=_BAD_POSITION), _deck(bad_key_at=40)],
                         ids=["failed as it ran", "not valid"])
def test_a_kept_refusal_points_at_its_note_and_an_unkept_one_asks_for_the_batch(tmp_path, monkeypatch, ops):
    import results

    out = _write(tmp_path / "deck.pptx", ops)
    first = out.splitlines()[0]
    assert results.RESEND_FIX in first and results.RESEND_WHOLE not in first, first
    assert out.index("kept as") > len(first)
    monkeypatch.setattr(kept_batches, "MAX_BYTES", 10)
    out = _write(tmp_path / "big.pptx", ops)
    assert results.RESEND_WHOLE in out.splitlines()[0] and "kept as" not in out, out
