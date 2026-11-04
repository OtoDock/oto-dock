"""edit_pdf and edit_image on the catalogue: help, did-you-mean, atomic
batches and best_effort, side outputs staged until the commit, the OCR
batch in a thread, and the description rules every catalogue writer keeps."""

import asyncio
import re
import sys
import time
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

import images
import pdf as pdf_mod
import shared
from formats.pdf import edit


async def _ident(p, writing=False, **kw):
    return p


async def _nothing(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    for mod in (shared, pdf_mod, images):
        if hasattr(mod, "_resolve_path"):
            monkeypatch.setattr(mod, "_resolve_path", _ident)
        for name in ("_push_preview", "_notify_file_written", "_push_image_preview"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, _nothing)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _pdf(path: Path, pages: int = 3, text: str = "Page {n} Draft") -> Path:
    doc = fitz.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 100), text.format(n=n + 1), fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


def _edit(path: Path, ops, **extra) -> str:
    return asyncio.run(pdf_mod.handle_edit_pdf({"path": str(path), "operations": ops, **extra}))


def test_help_lists_every_operation_in_exactly_one_group():
    groups = edit.CATALOG.groups()
    names = [n for ops in groups.values() for n in ops]
    assert sorted(names) == sorted(set(names))
    assert set(names) == set(edit.CATALOG.ops)
    whole = edit.CATALOG.help()
    for name in edit.CATALOG.ops:
        assert len(re.findall(rf"\n  {re.escape(name)}: ", whole)) == 1, name


@pytest.mark.parametrize("module, catalog", [("edit_pdf", edit.CATALOG), ("edit_image", images.CATALOG)])
def test_the_description_names_help_best_effort_and_every_group(module, catalog):
    import pdf_tool

    description = next(t.description for t in pdf_tool.TOOLS if t.name == module)
    assert '{"op": "help"}' in description and "best_effort" in description
    assert [g for g in catalog.groups() if g.lower() not in description.lower()] == []
    assert len(description) <= 2008


def test_a_help_call_never_opens_the_file(tmp_path, monkeypatch):
    async def _refuse(*a, **k):
        raise AssertionError("help resolved the path")

    monkeypatch.setattr(shared, "_resolve_path", _refuse)
    out = _edit(tmp_path / "never.pdf", [{"op": "help", "name": "fill_form"}])
    assert out.startswith("fill_form: values")


def test_unknown_operations_and_keys_fail_with_did_you_mean_before_any_work(tmp_path):
    src = _pdf(tmp_path / "a.pdf")
    before = src.read_bytes()
    out = _edit(src, [{"op": "rotat_page"}, {"op": "add_text", "txt": "x"}])
    assert out.startswith("Nothing saved:") and "2 of 2 operations are not valid" in out
    assert "Did you mean 'rotate_page'" in out and "Did you mean 'text'?" in out
    assert src.read_bytes() == before


def test_one_failure_saves_nothing_and_best_effort_keeps_the_rest(tmp_path):
    src = _pdf(tmp_path / "b.pdf")
    before = src.read_bytes()
    ops = [{"op": "rotate_page", "pages": "1", "degrees": 90}, {"op": "delete_page", "pages": "9"}]
    out = _edit(src, ops)
    assert out.startswith("Nothing saved:") and "op #1 delete_page" in out and "name no page" in out
    assert src.read_bytes() == before
    out = _edit(src, ops, best_effort=True)
    assert out.startswith("PDF saved:") and "1 failed and may be partly applied" in out
    assert fitz.open(str(src))[0].rotation == 90


def test_today_s_values_keep_working(tmp_path):
    """Manifest 1.3.1 shapes: type as the op key, colour lists, page -1,
    ints and lists 0-based, the image under path or image."""
    from PIL import Image

    src = _pdf(tmp_path / "c.pdf")
    logo = tmp_path / "logo.png"
    Image.new("RGB", (10, 10), "red").save(logo)
    out = _edit(src, [
        {"type": "add_text", "page": -1, "text": "end", "color": [1, 0, 0], "font": "hebo"},
        {"action": "rotate_page", "pages": [0, 1], "degrees": 180},
        {"operation": "add_image", "page": 0, "path": str(logo), "rect": [10, 10, 40, 40]},
        {"op": "add_annotation", "page": 0, "annotation_type": "rectangle", "rect": [50, 50, 90, 90]},
        {"op": "add_text", "position": [100, 300], "text": "at a position"},
    ])
    assert out.startswith("PDF saved:") and "(5 operations applied: 2 add_text, 1 rotate_page" in out, out
    doc = fitz.open(str(src))
    assert "end" in doc[2].get_text() and doc[0].rotation == doc[1].rotation == 180
    assert "at a position" in doc[0].get_text()


def test_a_split_lands_only_when_the_batch_commits(tmp_path):
    src = _pdf(tmp_path / "d.pdf")
    part = tmp_path / "part.pdf"
    out = _edit(src, [{"op": "split", "pages": "1-2", "output_path": str(part)},
                      {"op": "delete_page", "pages": "9"}])
    assert out.startswith("Nothing saved:") and not part.exists()
    out = _edit(src, [{"op": "split", "pages": "1-2", "output_path": str(part)}])
    assert out.startswith(f"PDF unchanged: {src}. Side files written: {part}")
    assert fitz.open(str(part)).page_count == 2


def test_extract_images_default_folder_is_resolved_like_a_named_one(tmp_path, monkeypatch):
    from PIL import Image

    seen = []

    async def _record(p, writing=False, **kw):
        seen.append((p, writing))
        return p

    monkeypatch.setattr(shared, "_resolve_path", _record)
    logo = tmp_path / "logo.png"
    Image.new("RGB", (10, 10), "blue").save(logo)
    doc = fitz.open()
    doc.new_page().insert_image(fitz.Rect(10, 10, 60, 60), filename=str(logo))
    src = tmp_path / "pics.pdf"
    doc.save(str(src))
    out = _edit(src, [{"op": "extract_images"}])
    assert (str(tmp_path / "pics_images"), True) in seen
    assert "extracted 1 image(s) to pics_images" in out
    assert len(list((tmp_path / "pics_images").iterdir())) == 1


def test_a_missing_file_saves_nothing_and_says_file_not_found(tmp_path):
    out = _edit(tmp_path / "nope.pdf", [{"op": "insert_page"}])
    assert out.startswith("Nothing saved:") and "File not found" in out
    assert not (tmp_path / "nope.pdf").exists()


def test_page_specs_count_strings_from_one_and_lists_from_zero():
    from formats.pdf.pages import PageSpecError, page_list

    assert page_list("1-3", 5) == ([0, 1, 2], [])
    assert page_list("2-", 4) == ([1, 2, 3], [])
    assert page_list("-1", 4) == ([3], [])
    assert page_list("last", 4) == ([3], [])
    assert page_list([0, -1], 4) == ([0, 3], [])
    assert page_list("1,9", 3) == ([0], ["9"])
    assert page_list(7, 3) == ([], ["7"])
    with pytest.raises(PageSpecError):
        page_list("first", 3)


def _ocr_write(args, core):
    """edit_pdf's OCR path: the shared write with its core in a thread."""
    import batch

    return batch.handle_write_call(args, edit.CATALOG, core, kind=edit.KIND, advice=edit.ADVICE, carry=("password",),
                                   in_thread=True)


def test_an_ocr_batch_runs_in_a_thread_and_holds_its_lock_until_it_ends(tmp_path, monkeypatch):
    """A cancelled OCR call leaves its thread running; the next write to
    the same file waits for it instead of running beside it."""
    events = []

    def _slow_core(call):
        events.append("start")
        time.sleep(0.4)
        events.append("end")
        return edit.WriteReport("PDF saved: x (1 operations applied)", False)

    src = _pdf(tmp_path / "e.pdf")
    args = {"path": str(src), "operations": [{"op": "ocr", "pages": "1"}]}

    async def main():
        first = asyncio.ensure_future(_ocr_write(dict(args), _slow_core))
        await asyncio.sleep(0.1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        events.append("cancelled")
        await _ocr_write(dict(args), _slow_core)

    asyncio.run(main())
    # The caller returns at once; the next call waits for the first thread.
    assert events == ["start", "cancelled", "end", "start", "end"]


def test_an_ocr_batch_never_takes_a_worker(tmp_path, monkeypatch):
    import isolation

    async def _no_spawn(*a, **k):
        raise AssertionError("an OCR batch went to a worker")

    monkeypatch.setattr(isolation, "run_parse", _no_spawn)
    monkeypatch.setattr(edit, "tesseract_page", edit._stub_ocr)
    src = _pdf(tmp_path / "f.pdf")
    out = _edit(src, [{"action": "ocr", "pages": "1"}])
    assert out.startswith("PDF saved:") or "OCR" in out


def test_ocr_with_an_output_path_leaves_the_pdf_as_it_was(tmp_path):
    from batch import WriteCall
    from results import ResolvedPath

    src = _pdf(tmp_path / "g.pdf")
    before = src.read_bytes()
    copy = tmp_path / "g-ocr.pdf"
    work = tmp_path / "work"
    work.mkdir()
    call = WriteCall(path=str(src), ops=[(0, {"op": "ocr", "pages": "1", "output_path": ResolvedPath(str(copy))})],
                     work_dir=str(work), original=None)
    report = edit.edit_pdf_core(call, ocr_page=edit._stub_ocr)
    assert report.text.startswith(f"PDF unchanged: {src}. Side files written: {copy}"), report.text
    assert src.read_bytes() == before and copy.exists()


def test_edit_image_refuses_unknown_ops_and_saves_nothing_on_a_failure(tmp_path):
    from PIL import Image

    src = tmp_path / "p.png"
    Image.new("RGB", (40, 30), (90, 120, 200)).save(src)
    before = src.read_bytes()
    out = asyncio.run(images.handle_edit_image({"path": str(src), "operations": [{"op": "blurr"}]}))
    assert out.startswith("Nothing saved:") and "Did you mean 'blur'" in out
    out = asyncio.run(images.handle_edit_image({"path": str(src), "operations": [
        {"op": "flip"}, {"op": "paste_image", "image_path": str(tmp_path / "missing.png")}]}))
    assert out.startswith("Nothing saved:") and "op #1 paste_image" in out
    assert src.read_bytes() == before
    out = asyncio.run(images.handle_edit_image({"path": str(src), "best_effort": True, "operations": [
        {"op": "flip"}, {"op": "paste_image", "image_path": str(tmp_path / "missing.png")}]}))
    assert out.startswith("Image saved:") and "Size: 40x30 px" in out


def test_edit_image_float_pixels_and_colours_work_as_before(tmp_path):
    from PIL import Image

    src = tmp_path / "q.png"
    Image.new("RGB", (40, 30), (90, 120, 200)).save(src)
    out = asyncio.run(images.handle_edit_image({"path": str(src), "operations": [
        {"type": "add_text", "text": "Αθήνα", "x": 2.5, "y": 3.7, "font_size": 9.5, "color": "#FF0000"},
        {"type": "add_border", "width": 2.0, "color": [0, 0, 255]},
        {"type": "blur_region", "x": 1, "y": 1, "width": 10, "height": 10, "intensity": 3},
    ]}))
    assert out.startswith("Image saved:") and "(3 operations applied: 1 add_text, 1 add_border, 1 blur_region)" in out, out
    assert Image.open(src).size == (44, 34)


def test_convert_format_writes_a_path_the_proxy_resolved(tmp_path, monkeypatch):
    from PIL import Image

    seen = []

    async def _record(p, writing=False, **kw):
        seen.append((p, writing))
        return p

    monkeypatch.setattr(images, "_resolve_path", _record)
    src = tmp_path / "r.png"
    Image.new("RGB", (8, 8), "red").save(src)
    out = asyncio.run(images.handle_edit_image({"path": str(src), "operations": [
        {"op": "convert_format", "format": "jpg"}]}))
    assert (str(tmp_path / "r.jpg"), True) in seen
    assert out.startswith(f"Image saved: {tmp_path / 'r.jpg'}")
    assert Image.open(tmp_path / "r.jpg").format == "JPEG"


def test_set_metadata_keeps_the_fields_it_was_not_given(tmp_path):
    src = _pdf(tmp_path / "m.pdf")
    doc = fitz.open(str(src))
    doc.set_metadata({"title": "Old", "author": "Μαρία"})
    doc.saveIncr()
    doc.close()
    _edit(src, [{"op": "set_metadata", "title": "Νέος τίτλος"}])
    meta = fitz.open(str(src)).metadata
    assert meta["title"] == "Νέος τίτλος" and meta["author"] == "Μαρία"


def test_read_takes_lists_last_pages_and_open_ranges(tmp_path):
    src = _pdf(tmp_path / "r.pdf", pages=5)
    out = pdf_mod.read_pdf(str(src), "1,3")
    assert "--- Page 1 ---" in out and "--- Page 3 ---" in out and "--- Page 2 ---" not in out
    assert "--- Page 5 ---" in pdf_mod.read_pdf(str(src), "-1")
    out = pdf_mod.read_pdf(str(src), "4-")
    assert "--- Page 4 ---" in out and "--- Page 5 ---" in out and "--- Page 3 ---" not in out
    with pytest.raises(ValueError, match="name no page"):
        pdf_mod.read_pdf(str(src), "9")


def test_fractions_today_s_handlers_took_as_whole_numbers_still_work(tmp_path):
    from PIL import Image

    src = _pdf(tmp_path / "w.pdf", pages=3)
    out = _edit(src, [{"op": "add_text", "page": 1.7, "text": "on page one"},
                      {"op": "rotate_page", "pages": "1", "degrees": 90.0},
                      {"op": "insert_page", "position": 0.9},
                      {"op": "reorder_pages", "order": [0, 1.2, 2, 3]},
                      {"op": "compress", "garbage": 3.5}])
    assert out.startswith("PDF saved:"), out
    assert "1.7 was taken as 1" in out and "3.5 was taken as 3" in out
    assert "on page one" in fitz.open(str(src))[2].get_text()   # the inserted page moved it to index 2
    img = tmp_path / "x.png"
    Image.new("RGB", (40, 30), (90, 120, 200)).save(img)
    out = asyncio.run(images.handle_edit_image({"path": str(img), "operations": [
        {"op": "denoise", "strength": 2.5}, {"op": "compress", "quality": 80.6}]}))
    assert out.startswith("Image saved:") and "2.5 was taken as 2" in out and "80.6 was taken as 80" in out, out


def test_a_cancelled_ocr_write_that_fails_is_logged_not_lost(tmp_path, caplog):
    import batch

    def _failing_core(call):
        time.sleep(0.2)
        raise RuntimeError("tesseract fell over")

    src = _pdf(tmp_path / "z.pdf")
    args = {"path": str(src), "operations": [{"op": "ocr", "pages": "1"}]}

    async def main():
        first = asyncio.ensure_future(_ocr_write(dict(args), _failing_core))
        await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.gather(*batch._THREAD_WRITES, return_exceptions=True)

    with caplog.at_level("WARNING"):
        asyncio.run(main())
    assert "tesseract fell over" in caplog.text
