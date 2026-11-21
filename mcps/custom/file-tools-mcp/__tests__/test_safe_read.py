"""Every read of a person's file opens the answer the server gave beneath
the mount with no link followed (``shared.safe_open_read`` and its
siblings); the tool's own temps outside the mount open plainly, decided on
the path's text alone. Stand-ins, links and pipes live in the test's own
``tmp_path``, which is the write root here (conftest).
"""

import os
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import safe_fs
import shared

STAND_IN = b"STAND-IN BYTES"


def _never_waits(fn, fifo: Path, seconds: float = 10.0):
    """``fn()`` run in a thread that must end within ``seconds``. A reader
    still blocked on ``fifo`` after that is released, and the pipe replaced
    by an empty file so no later open waits either (the suite never hangs);
    the test then fails."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001  # the test reads what the call raised
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if not worker.is_alive():
        return box
    for _ in range(100):
        try:
            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            writer = -1
        if fifo.exists() and not fifo.is_file():
            fifo.unlink()
            fifo.write_bytes(b"")
        if writer >= 0:
            os.close(writer)
        worker.join(0.1)
        if not worker.is_alive():
            break
    pytest.fail(f"the read waited on the pipe at {fifo.name}")


@pytest.fixture
def outside():
    """A folder of the tool's own outside the write root (the system temp)."""
    folder = tempfile.mkdtemp(prefix="ft-own-")
    yield Path(folder)
    import shutil
    shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_safe_open_read_reads_beneath_the_root_and_refuses_a_link(tmp_path):
    (tmp_path / "real.bin").write_bytes(b"MINE")
    standin = tmp_path / "standin"
    standin.mkdir()
    (standin / "real.bin").write_bytes(STAND_IN)
    with shared.safe_open_read(str(tmp_path / "real.bin")) as fh:
        assert fh.read() == b"MINE"
    (tmp_path / "linked.bin").symlink_to(standin / "real.bin")
    with pytest.raises(PermissionError, match="Cannot read"):
        shared.safe_open_read(str(tmp_path / "linked.bin"))
    (tmp_path / "folder").symlink_to(standin)
    with pytest.raises(PermissionError, match="Cannot read"):
        shared.safe_open_read(str(tmp_path / "folder" / "real.bin"))
    with pytest.raises(FileNotFoundError):
        shared.safe_open_read(str(tmp_path / "missing.bin"))


def test_safe_open_read_refuses_a_pipe_at_once(tmp_path):
    fifo = tmp_path / "pipe.docx"
    os.mkfifo(fifo)
    box = _never_waits(lambda: shared.safe_open_read(str(fifo)), fifo)
    assert isinstance(box.get("error"), PermissionError)


def test_safe_open_read_opens_the_tools_own_temp_plainly(outside):
    own = outside / "copy.docx"
    own.write_bytes(b"OWN")
    with shared.safe_open_read(str(own)) as fh:
        assert fh.read() == b"OWN"


@pytest.mark.parametrize("shape", ["{root}/a/../b.bin", "{root}/./b.bin", "{outside}/x/../b.bin", "b.bin"])
def test_safe_open_read_refuses_a_path_that_is_not_plain(tmp_path, outside, shape):
    """A path with a dot part, or a relative one, is refused on its text
    (only a normalized absolute path outside the root opens plainly)."""
    (tmp_path / "b.bin").write_bytes(b"x")
    (outside / "b.bin").write_bytes(b"x")
    with pytest.raises(PermissionError, match="Cannot read"):
        shared.safe_open_read(shape.format(root=tmp_path, outside=outside))


def test_safe_read_fd_hands_the_descriptor_and_its_status(tmp_path, outside):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7 mine")
    fd, st = shared.safe_read_fd(str(tmp_path / "doc.pdf"))
    try:
        assert st.st_size == len(b"%PDF-1.7 mine") and os.pread(fd, 4, 0) == b"%PDF"
    finally:
        os.close(fd)
    (tmp_path / "standin").mkdir()
    (tmp_path / "standin" / "doc.pdf").write_bytes(STAND_IN)
    (tmp_path / "lnk.pdf").symlink_to(tmp_path / "standin" / "doc.pdf")
    with pytest.raises(PermissionError, match="Cannot read"):
        shared.safe_read_fd(str(tmp_path / "lnk.pdf"))
    (outside / "own.pdf").write_bytes(b"%PDF own")
    fd, st = shared.safe_read_fd(str(outside / "own.pdf"))
    os.close(fd)
    assert st.st_size == len(b"%PDF own")


def test_safe_kind_never_follows_a_link(tmp_path, outside):
    (tmp_path / "f.txt").write_bytes(b"x")
    (tmp_path / "d").mkdir()
    (tmp_path / "lnk").symlink_to(tmp_path / "f.txt")
    (tmp_path / "dlnk").symlink_to(tmp_path / "d")
    (tmp_path / "d" / "in.txt").write_bytes(b"x")
    assert shared.safe_kind(str(tmp_path / "f.txt")).kind == "file"
    assert shared.safe_kind(str(tmp_path / "d")).kind == "dir"
    assert shared.safe_kind(str(tmp_path / "nope")).kind == "missing"
    assert shared.safe_kind(str(tmp_path / "lnk")).kind == "other"
    assert shared.safe_kind(str(tmp_path / "dlnk" / "in.txt")).kind == "other"
    (outside / "own.txt").write_bytes(b"xy")
    own = shared.safe_kind(str(outside / "own.txt"))
    assert own.kind == "file" and own.st.st_size == 2
    assert shared.safe_kind(str(outside / "nope")).kind == "missing"


def test_private_copy_copies_under_the_same_name_within_its_cap(tmp_path, outside):
    (tmp_path / "Report.docx").write_bytes(b"0123456789")
    copy = shared.private_copy(str(tmp_path / "Report.docx"), str(outside), 10)
    assert copy == str(outside / "Report.docx")
    assert Path(copy).read_bytes() == b"0123456789"
    other = outside / "again"
    other.mkdir()
    with pytest.raises(safe_fs.FileTooLarge):
        shared.private_copy(str(tmp_path / "Report.docx"), str(other), 9)


def test_private_copy_is_capped_while_it_copies(tmp_path, outside, monkeypatch):
    """A file that grows after its size was read is still cut off at the
    cap while it is copied."""
    target = tmp_path / "Report.docx"
    target.write_bytes(b"0123456789")
    real = os.fstat

    def growing(fd):
        st = real(fd)
        if sys._getframe(1).f_code.co_name == "private_copy":
            with open(target, "ab") as fh:
                fh.write(b"X" * 10)
        return st

    monkeypatch.setattr(os, "fstat", growing)
    with pytest.raises(safe_fs.FileTooLarge):
        shared.private_copy(str(target), str(outside), 10)


# ---------------------------------------------------------------------------
# One rule for the tool's own temps (no flag): the checks read a copy the
# tool made outside the mount
# ---------------------------------------------------------------------------


def _docx(path: Path) -> Path:
    from docx import Document

    doc = Document()
    doc.add_paragraph("hello")
    doc.save(path)
    return path


def test_preflight_and_the_checks_read_the_tools_own_copy_without_a_flag(outside, check_registry):
    import checks

    copy = _docx(outside / "copy.docx")
    assert checks.preflight(str(copy), "docx") is None
    result = checks.check_document_core(str(copy), "docx", (), ())
    assert result.unknown == []


def test_the_marked_copy_reads_the_tools_own_copy_without_a_flag(outside):
    from openpyxl import Workbook

    from formats.xlsx import print_marks

    book = Workbook()
    book.active["A1"] = 1
    src = outside / "book.xlsx"
    book.save(src)
    folder = outside / "marked"
    folder.mkdir()
    assert Path(print_marks.marked_copy(str(src), str(folder))).exists()


def _small_fstat(monkeypatch, module, size: int):
    """The size the copy sees at its open is ``size`` (the file grew after
    it was taken): the cap must hold while the bytes are copied."""
    real = os.fstat

    def fake(fd):
        st = real(fd)
        fields = list(st)
        fields[6] = min(st.st_size, size)
        return os.stat_result(fields)
    monkeypatch.setattr(module.os, "fstat", fake)


def test_a_legacy_copy_is_capped_while_it_is_copied(tmp_path, outside, monkeypatch):
    import legacy

    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"x" * 4096)
    monkeypatch.setattr(legacy, "LEGACY_MAX_BYTES", 1024)
    _small_fstat(monkeypatch, legacy, 100)
    with pytest.raises(legacy.LegacyError, match="past the"):
        legacy._copy_in(str(tmp_path / "old.doc"), str(outside / "source.doc"))


def test_a_workbook_copy_for_the_checks_is_capped_while_it_is_copied(tmp_path, monkeypatch):
    from openpyxl import Workbook

    import checks

    work = Path(tempfile.mkdtemp(prefix="ft-check-"))
    try:
        book = Workbook()
        book.active["A1"] = 1
        book.save(tmp_path / "big.xlsx")
        with zipfile.ZipFile(tmp_path / "big.xlsx", "a") as zf:
            zf.writestr("filler.bin", os.urandom(8192))
        monkeypatch.setattr(checks, "CHECK_MAX_FILE_BYTES", 4096)
        _small_fstat(monkeypatch, checks, 100)
        with pytest.raises(safe_fs.FileTooLarge):
            checks.copy_workbook(str(tmp_path / "big.xlsx"), str(work))
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# The readers: the answer's folder swapped for a link to a stand-in folder
# holding a file of the same name AFTER the path was resolved
# ---------------------------------------------------------------------------

_MCP_DIR = Path(__file__).resolve().parent.parent


class _Swap:
    """``_resolve_path`` (in every loaded module of the tool and in
    ``shared``) answers an armed name in its folder (``work`` or a folder in
    it) and then swaps that folder for a link to ``standin``, which holds a
    file of the same name; any other name is answered in ``work`` for a read
    and in ``out`` for a write (never swapped)."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.work = tmp_path / "work"
        self.standin = tmp_path / "standin"
        self.out = tmp_path / "out"
        for folder in (self.work, self.standin, self.out):
            folder.mkdir()
        self.armed: dict[str, Path] = {}

        async def resolve(p, writing=False, **_kw):
            name = Path(p).name
            if name in self.armed:
                folder = self.armed.pop(name)
                answer = str(folder / name)
                self.swap(folder)
                return answer
            return str((self.out if writing else self.work) / name)

        async def _noop(*_a, **_k):
            return None

        loaded = [m for m in list(sys.modules.values())
                  if Path(getattr(m, "__file__", "") or "/").resolve().parent == _MCP_DIR]
        for mod in {*loaded, shared}:
            if hasattr(mod, "_resolve_path"):
                monkeypatch.setattr(mod, "_resolve_path", resolve)
            for hook in ("_push_preview", "_push_image_preview", "_notify_file_written"):
                if hasattr(mod, hook):
                    monkeypatch.setattr(mod, hook, _noop)

    def put(self, name: str, honest: bytes, standin: bytes, sub: str = "") -> str:
        folder = self.work / sub if sub else self.work
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(honest)
        (self.standin / name).write_bytes(standin)
        self.armed[name] = folder
        return name

    def swap(self, folder: Path) -> None:
        folder.rename(folder.with_name(folder.name + ".orig"))
        folder.symlink_to(self.standin)


@pytest.fixture
def swap(tmp_path, monkeypatch):
    import excel  # noqa: F401  # every handler module loaded before the stubs go in
    import images  # noqa: F401
    import pdf  # noqa: F401
    import powerpoint  # noqa: F401
    import server  # noqa: F401
    import word  # noqa: F401

    return _Swap(tmp_path, monkeypatch)


def _docx_bytes(text: str) -> bytes:
    import io

    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _xlsx_bytes(text: str) -> bytes:
    import io

    from openpyxl import Workbook

    book = Workbook()
    book.active["A1"] = text
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _pptx_bytes(text: str) -> bytes:
    import io

    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _pdf_bytes(text: str, pages: int = 1) -> bytes:
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _png_bytes(size: tuple[int, int], colour=(200, 30, 30)) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def _run(coro_fn):
    import asyncio

    return asyncio.run(coro_fn)


def _read_document(name: str) -> str:
    import server

    return _run(server._handle_read_document({"path": name}))


def _refused(call) -> str:
    """The call's answer, or the error it raised (the server answers it as
    "Error: <text>"), as text."""
    try:
        return str(call())
    except (PermissionError, RuntimeError) as exc:
        return str(exc)


_MAKERS = {"docx": _docx_bytes, "xlsx": _xlsx_bytes, "pptx": _pptx_bytes, "pdf": _pdf_bytes}


@pytest.mark.parametrize("ext", ["docx", "xlsx", "pptx"])
def test_read_document_never_reads_a_stand_in_swapped_in_after_the_answer(swap, ext):
    make = _MAKERS[ext]
    name = swap.put(f"doc.{ext}", make("HONEST TEXT"), make("STAND-IN TEXT"))
    out = _read_document(name)
    assert "STAND-IN" not in out
    assert "Cannot read" in out


@pytest.mark.parametrize("ext", ["docx", "xlsx", "pptx", "pdf"])
def test_read_document_reads_an_honest_file(swap, ext):
    name = f"doc.{ext}"
    (swap.work / name).write_bytes(_MAKERS[ext]("HONEST TEXT"))
    assert "HONEST TEXT" in _read_document(name)


@pytest.mark.parametrize("ext", ["docx", "xlsx", "pptx"])
def test_read_document_refuses_a_pipe_at_the_answer_at_once(swap, ext):
    fifo = swap.work / f"doc.{ext}"
    os.mkfifo(fifo)
    box = _never_waits(lambda: _read_document(fifo.name), fifo)
    assert "Cannot read" in box.get("value", "")


def test_analyze_image_never_reads_a_stand_in(swap):
    import images

    name = swap.put("photo.png", _png_bytes((10, 10)), _png_bytes((37, 23)))
    out = _refused(lambda: _run(images.handle_analyze_image({"path": name})))
    assert "37x23" not in out and "Cannot read" in out


def test_analyze_image_reads_an_honest_picture(swap):
    import images

    (swap.work / "honest.png").write_bytes(_png_bytes((12, 9)))
    assert "12x9" in _run(images.handle_analyze_image({"path": "honest.png"}))


def test_analyze_image_refuses_a_pipe_at_once(swap):
    import images

    fifo = swap.work / "photo.png"
    os.mkfifo(fifo)
    box = _never_waits(lambda: _refused(lambda: _run(images.handle_analyze_image({"path": fifo.name}))), fifo)
    assert "Cannot read" in box.get("value", "")


def test_edit_image_never_reads_a_stand_in(swap):
    from PIL import Image

    import images

    name = swap.put("photo.png", _png_bytes((10, 10)), _png_bytes((37, 23), (0, 0, 255)))
    out = _refused(lambda: _run(images.handle_edit_image({
        "path": name, "output_path": "edited.png", "operations": [{"op": "rotate", "degrees": 90, "expand": True}]})))
    assert "Cannot read" in out
    edited = swap.out / "edited.png"
    assert not edited.exists() or Image.open(edited).size != (23, 37)


def test_a_pasted_picture_is_never_read_from_a_stand_in(swap):
    from PIL import Image

    import images

    (swap.work / "base.png").write_bytes(_png_bytes((40, 40), (255, 255, 255)))
    swap.put("logo.png", _png_bytes((8, 8), (0, 255, 0)), _png_bytes((8, 8), (0, 0, 255)), sub="pics")
    out = _refused(lambda: _run(images.handle_edit_image({
        "path": "base.png", "output_path": "pasted.png",
        "operations": [{"op": "paste_image", "image_path": "logo.png", "x": 0, "y": 0}]})))
    assert "Cannot read" in out
    saved = swap.out / "pasted.png"
    assert not saved.exists() or Image.open(saved).convert("RGB").getpixel((1, 1)) != (0, 0, 255)


def _embedded(package: Path) -> list[bytes]:
    if not package.exists():
        return []
    with zipfile.ZipFile(package) as zf:
        return [zf.read(n) for n in zf.namelist() if "/media/" in n]


def test_a_picture_for_a_docx_is_never_read_from_a_stand_in(swap):
    import word

    swap.put("logo.png", _png_bytes((8, 8)), _png_bytes((37, 23)))
    out = _run(word.handle_write_docx({"path": "new.docx",
                                       "operations": [{"op": "add_image", "image_path": "logo.png"}]}))
    assert "Cannot read" in out
    assert _png_bytes((37, 23)) not in _embedded(swap.out / "new.docx")


def test_a_picture_for_a_pptx_is_never_read_from_a_stand_in(swap):
    import powerpoint

    swap.put("logo.png", _png_bytes((8, 8)), _png_bytes((37, 23)))
    out = _run(powerpoint.handle_write_pptx({"path": "new.pptx", "operations": [
        {"op": "add_slide"}, {"op": "add_image", "slide_index": 0, "image_path": "logo.png"}]}))
    assert "Cannot read" in out
    assert _png_bytes((37, 23)) not in _embedded(swap.out / "new.pptx")


def test_a_picture_for_a_pptx_keeps_its_file_name_as_its_description(swap, tmp_path):
    from pptx import Presentation

    import powerpoint

    (swap.work / "logo.png").write_bytes(_png_bytes((8, 8)))
    _run(powerpoint.handle_write_pptx({"path": "named.pptx", "operations": [
        {"op": "add_slide"}, {"op": "add_image", "slide_index": 0, "image_path": "logo.png"}]}))
    picture = Presentation(str(swap.out / "named.pptx")).slides[0].shapes[-1]
    assert picture._element.nvPicPr.cNvPr.get("descr") == "logo.png"

# --- PyMuPDF: opened through the strict descriptor ---------------------------


def _pdf_text(path: Path) -> str:
    import fitz

    if not path.exists():
        return ""
    with fitz.open(path) as doc:
        return "".join(page.get_text() for page in doc)


def test_read_document_never_reads_a_pdf_stand_in(swap):
    name = swap.put("doc.pdf", _pdf_bytes("HONEST TEXT"), _pdf_bytes("STAND-IN TEXT"))
    out = _read_document(name)
    assert "STAND-IN" not in out and "Cannot read" in out


def test_read_document_refuses_a_pdf_pipe_at_once(swap):
    fifo = swap.work / "doc.pdf"
    os.mkfifo(fifo)
    box = _never_waits(lambda: _read_document(fifo.name), fifo)
    assert "Cannot read" in box.get("value", "")


def test_a_pdf_to_merge_is_never_read_from_a_stand_in(swap):
    import pdf

    (swap.out / "main.pdf").write_bytes(_pdf_bytes("MAIN TEXT"))
    swap.put("other.pdf", _pdf_bytes("HONEST TEXT"), _pdf_bytes("STAND-IN TEXT"))
    out = _run(pdf.handle_edit_pdf({"path": "main.pdf", "operations": [{"op": "merge", "files": ["other.pdf"]}]}))
    assert "Cannot read" in out
    assert "STAND-IN" not in _pdf_text(swap.out / "main.pdf")


def test_a_pdf_to_merge_is_read_from_the_descriptor_it_opened(swap, monkeypatch):
    """The merge reads the file its strict open answered: a link put at the
    name after the open is never read."""
    import pdf

    (swap.out / "main.pdf").write_bytes(_pdf_bytes("MAIN TEXT"))
    (swap.work / "other.pdf").write_bytes(_pdf_bytes("HONEST TEXT"))
    (swap.standin / "other.pdf").write_bytes(_pdf_bytes("STAND-IN TEXT"))
    real = shared.safe_read_fd

    def then_swap(path):
        got = real(path)
        if Path(path).name == "other.pdf":
            swap.swap(swap.work)
        return got

    monkeypatch.setattr(shared, "safe_read_fd", then_swap)
    _run(pdf.handle_edit_pdf({"path": "main.pdf", "operations": [{"op": "merge", "files": ["other.pdf"]}]}))
    text = _pdf_text(swap.out / "main.pdf")
    assert "HONEST TEXT" in text and "STAND-IN" not in text


def test_a_picture_for_a_pdf_is_never_read_from_a_stand_in(swap):
    import fitz

    import pdf

    (swap.out / "main.pdf").write_bytes(_pdf_bytes("MAIN TEXT"))
    swap.put("logo.png", _png_bytes((8, 8)), _png_bytes((37, 23)))
    out = _run(pdf.handle_edit_pdf({"path": "main.pdf", "operations": [
        {"op": "add_image", "page": 0, "image_path": "logo.png"}]}))
    assert "Cannot read" in out
    with fitz.open(swap.out / "main.pdf") as doc:
        sizes = [(img[2], img[3]) for img in doc[0].get_images()]
    assert (37, 23) not in sizes


def test_a_pdf_edited_in_place_is_opened_once_through_its_descriptor(swap):
    import pdf

    (swap.out / "main.pdf").write_bytes(_pdf_bytes("MAIN TEXT"))
    out = _run(pdf.handle_edit_pdf({"path": "main.pdf", "operations": [{"op": "rotate_page", "pages": "all",
                                                                        "degrees": 90}]}))
    assert "saved" in out, out
    import fitz

    with fitz.open(swap.out / "main.pdf") as doc:
        assert doc[0].rotation == 90


def test_images_to_pdf_never_reads_a_stand_in(swap):
    import fitz

    import pdf

    swap.put("a.png", _png_bytes((10, 10)), _png_bytes((37, 23)))
    out = _refused(lambda: _run(pdf.handle_images_to_pdf({"images": ["a.png"], "output_path": "made.pdf",
                                                          "page_size": "original"})))
    assert "Cannot read" in out
    made = swap.out / "made.pdf"
    if made.exists():
        with fitz.open(made) as doc:
            assert doc[0].rect != _picture_rect(_png_bytes((37, 23)))


def test_images_to_pdf_places_an_honest_picture(swap):
    import fitz

    import pdf

    (swap.work / "a.png").write_bytes(_png_bytes((30, 20)))
    out = _run(pdf.handle_images_to_pdf({"images": ["a.png"], "output_path": "made.pdf", "page_size": "original"}))
    assert "PDF created" in out
    with fitz.open(swap.out / "made.pdf") as doc:
        assert doc[0].rect == _picture_rect(_png_bytes((30, 20)))


def _picture_rect(data: bytes):
    import fitz

    with fitz.open(stream=data, filetype="png") as picture:
        return picture[0].rect


def test_pdf_to_images_never_renders_a_stand_in(swap):
    import pdf

    swap.put("doc.pdf", _pdf_bytes("HONEST TEXT"), _pdf_bytes("STAND-IN TEXT", pages=3))
    out = _refused(lambda: _run(pdf.handle_pdf_to_images({"path": "doc.pdf", "output_dir": "pages"})))
    assert "Cannot read" in out
    assert not (swap.out / "pages" / "page_003.png").exists()


@pytest.mark.parametrize("grid", [False, True])
def test_a_screenshot_never_renders_a_pdf_stand_in(swap, grid):
    import pdf

    swap.put("doc.pdf", _pdf_bytes("HONEST TEXT"), _pdf_bytes("STAND-IN TEXT", pages=3))
    items = _run(pdf.handle_screenshot_document({"path": "doc.pdf", "pages": "all", "grid": grid}))
    text = "\n".join(getattr(item, "text", "") for item in items)
    assert "Cannot read" in text and not any(getattr(item, "type", "") == "image" for item in items)

# --- Read whole, under a cap ---------------------------------------------------


def test_a_picture_for_a_workbook_is_never_read_from_a_stand_in(swap):
    import excel

    swap.put("logo.png", _png_bytes((8, 8)), _png_bytes((37, 23)))
    out = _run(excel.handle_write_xlsx({"path": "book.xlsx", "operations": [
        {"op": "add_image", "image_path": "logo.png", "cell": "B2"}]}))
    assert "Cannot read" in out
    assert _png_bytes((37, 23)) not in _embedded(swap.out / "book.xlsx")


def test_a_picture_for_a_workbook_is_capped_while_it_is_read(swap, monkeypatch):
    import excel

    (swap.work / "logo.png").write_bytes(_png_bytes((8, 8)))
    monkeypatch.setattr(excel, "_IMAGE_MAX_BYTES", 16)
    out = _run(excel.handle_write_xlsx({"path": "book.xlsx", "operations": [
        {"op": "add_image", "image_path": "logo.png"}]}))
    assert "over the" in out and not (swap.out / "book.xlsx").exists()


def test_a_csv_to_import_is_never_read_from_a_stand_in(swap):
    from openpyxl import load_workbook

    import excel

    swap.put("data.csv", b"HONEST,1\n", b"STAND-IN,2\n")
    out = _run(excel.handle_write_xlsx({"path": "book.xlsx", "operations": [
        {"op": "import_csv", "csv_path": "data.csv"}]}))
    assert "Cannot read" in out
    made = swap.out / "book.xlsx"
    assert not made.exists() or load_workbook(made).active["A1"].value != "STAND-IN"

# --- A program the tool starts reads a private copy -------------------------------


@pytest.fixture
def engine(monkeypatch):
    """The engine replaced by one that records the file it was handed and its
    bytes, and answers a PDF named after it."""
    import render_engine

    seen: list[tuple[str, bytes]] = []

    def render(path, out_dir):
        seen.append((path, Path(path).read_bytes()))
        out = Path(out_dir) / f"{Path(path).stem}.pdf"
        out.write_bytes(b"%PDF-1.4 rendered")
        return str(out)

    async def fake(path, output_format, out_dir, **_kw):
        import asyncio

        return await asyncio.to_thread(render, path, out_dir)

    monkeypatch.setattr(render_engine, "_libreoffice", fake)
    monkeypatch.setattr(render_engine, "_base_url", lambda: "")
    return seen


def test_the_engine_reads_a_private_copy_under_the_same_name(tmp_path, outside, engine):
    import render_engine

    person = tmp_path / "Quarterly Report.docx"
    person.write_bytes(_docx_bytes("HONEST TEXT"))
    produced = _run(render_engine.to_pdf(str(person), out_dir=str(outside)))
    assert Path(produced).name == "Quarterly Report.pdf"
    [(handed, data)] = engine
    assert handed != str(person) and Path(handed).name == person.name
    assert Path(handed).parent.parent == outside and data == person.read_bytes()
    assert not Path(handed).parent.exists(), "the copy's folder is removed afterwards"


def test_the_engine_never_reads_a_stand_in(tmp_path, outside, engine):
    import render_engine

    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "doc.docx").write_bytes(_docx_bytes("HONEST TEXT"))
    standin = tmp_path / "standin"
    standin.mkdir()
    (standin / "doc.docx").write_bytes(_docx_bytes("STAND-IN TEXT"))
    folder.rename(tmp_path / "docs.orig")
    folder.symlink_to(standin)
    with pytest.raises(PermissionError, match="Cannot read"):
        _run(render_engine.to_pdf(str(folder / "doc.docx"), out_dir=str(outside)))
    assert engine == [] and list(outside.iterdir()) == []


def test_a_file_past_the_conversion_cap_is_refused_with_a_sentence(tmp_path, outside, engine, monkeypatch):
    import render_engine

    monkeypatch.setattr(render_engine, "COPY_MAX_BYTES", 64)
    person = tmp_path / "big.docx"
    person.write_bytes(_docx_bytes("HONEST TEXT"))
    with pytest.raises(RuntimeError, match="past the"):
        _run(render_engine.to_pdf(str(person), out_dir=str(outside)))
    assert engine == [] and list(outside.iterdir()) == []


def test_the_engine_reads_the_tools_own_file_in_place(outside, engine):
    import render_engine

    own = outside / "work"
    own.mkdir()
    (own / "copy.docx").write_bytes(_docx_bytes("OWN"))
    out = outside / "out"
    out.mkdir()
    _run(render_engine.to_pdf(str(own / "copy.docx"), out_dir=str(out)))
    assert [handed for handed, _ in engine] == [str(own / "copy.docx")]

# --- Status checks look with no link followed -----------------------------------


def test_a_write_s_status_check_never_follows_a_link(tmp_path):
    import batch

    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "doc.docx").write_bytes(b"honest")
    st = (folder / "doc.docx").stat()
    assert batch._stat(str(folder / "doc.docx")) == (st.st_ino, st.st_mtime_ns, st.st_size)
    standin = tmp_path / "standin"
    standin.mkdir()
    (standin / "doc.docx").write_bytes(b"stand-in bytes, longer")
    folder.rename(tmp_path / "docs.orig")
    folder.symlink_to(standin)
    assert batch._stat(str(folder / "doc.docx")) is None
    assert batch._stat(str(tmp_path / "missing.docx")) is None


def test_a_status_check_answers_a_pipe_at_once(swap):
    import pdf

    fifo = swap.work / "doc.pdf"
    os.mkfifo(fifo)
    box = _never_waits(lambda: _run(pdf.handle_screenshot_document({"path": fifo.name})), fifo)
    text = "\n".join(getattr(item, "text", "") for item in box.get("value", []))
    assert "Cannot read" in text
