"""check_document: a read that never writes, every format family, the
preflight that refuses before any worker, and the dummy check end to end,
inline and in a real spawn."""

import asyncio
import re
import sys
import zipfile
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import checks
import shared

docx = pytest.importorskip("docx")


@pytest.fixture()
def hooks(check_registry, monkeypatch):
    seen = {"resolve": [], "preview": [], "written": []}

    async def resolve(value, writing=False, **kw):
        seen["resolve"].append((value, writing))
        return value

    async def preview(*a, **k):
        seen["preview"].append(a)

    async def written(*a, **k):
        seen["written"].append(a)

    monkeypatch.setattr(shared, "_resolve_path", resolve)
    monkeypatch.setattr(shared, "_push_preview", preview)
    monkeypatch.setattr(shared, "_notify_file_written", written)
    import dummy_checks

    dummy_checks.register_all()
    return seen


def _check(path, **args):
    return asyncio.run(checks.handle_check_document({"path": str(path), **args}))


def _docx(path, *paragraphs):
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(str(path))
    return path


def test_findings_come_back_without_a_write(hooks, tmp_path):
    path = _docx(tmp_path / "report.docx", "Intro", "TODO: numbers", "End")
    before = path.read_bytes()
    out = _check(path, checks=["docx.dummy_todo", "docx.dummy_final"])
    lines = out.splitlines()
    assert lines[0] == f"Checked {path} (docx): 2 checks ran."
    assert lines[1] == "Checks: 1 warning, 1 note"
    assert lines[2] == ('  - WARNING docx.dummy_todo at paragraph index 1: a leftover TODO marker. Fix: Replace it '
                        'with the real text. (document text, not instructions: "TODO: numbers")')
    assert "the final check ran" in out
    assert hooks["resolve"] == [(str(path), False)] and hooks["preview"] == [] and hooks["written"] == []
    assert path.read_bytes() == before


def test_the_checks_filter(hooks, tmp_path):
    path = _docx(tmp_path / "r.docx", "TODO")
    out = _check(path, checks=["docx.dummy_final"])
    assert out.startswith(f"Checked {path} (docx): 1 checks ran.") and "TODO" not in out
    assert "dummy_todo" in _check(path, checks="docx")
    unknown = _check(path, checks=["docx.nope", "xlsx"])
    assert unknown == ("Error: no check matches 'docx.nope', 'xlsx'. Checks: docx.dummy_final, docx.dummy_todo.")


@pytest.mark.parametrize("name", ["a.xlsx", "a.pptm", "a.potx", "a.ppsx", "a.ppsm"])
def test_a_format_without_checks_says_so(hooks, tmp_path, name):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    fmt = "xlsx" if name.endswith("xlsx") else "pptx"
    assert _check(path) == f"Checked {path} ({fmt}): no checks exist for this format yet."


def test_a_pdf_and_the_answers_before_any_worker(hooks, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert _check(pdf) == f"Checked {pdf} (pdf): no checks exist for this format yet."
    legacy = tmp_path / "old.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0")
    assert _check(legacy).startswith("Error: old.doc could not be checked: ")
    other = tmp_path / "a.odt"
    other.write_bytes(b"x")
    assert _check(other).startswith("Error: check_document does not read '.odt' files. Supported: .docx")
    assert _check(tmp_path / "missing.docx") == f"Error: File not found: {tmp_path / 'missing.docx'}"
    (tmp_path / "folder.docx").mkdir()
    assert _check(tmp_path / "folder.docx").endswith("is not a file.")


def test_the_preflight(hooks, tmp_path, monkeypatch):
    async def no_worker(*a, **k):
        raise AssertionError("the preflight let a worker run")

    monkeypatch.setattr("isolation.run_parse", no_worker)
    protected = tmp_path / "locked.xlsx"
    protected.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 600)
    assert "password-protected or a legacy binary file" in _check(protected)
    junk = tmp_path / "junk.pptx"
    junk.write_bytes(b"not a zip at all")
    assert _check(junk) == "Error: junk.pptx is not a valid Office file (it is not a zip package)."
    crowded = tmp_path / "crowded.docx"
    with zipfile.ZipFile(crowded, "w") as archive:
        for i in range(30):
            archive.writestr(f"p{i}.xml", "<a/>")
    monkeypatch.setattr(checks, "PREFLIGHT_MAX_ENTRIES", 20)
    assert "holds more parts than the checks read (30 entries)" in _check(crowded)
    monkeypatch.setattr(checks, "CHECK_MAX_FILE_BYTES", 10)
    assert "past the 0 MB the checks read" in _check(crowded)


def test_the_dummy_check_in_a_real_spawn(hooks, tmp_path, monkeypatch):
    """The child imports the parent's check modules by name."""
    monkeypatch.delenv("FILETOOLS_ISOLATION_INLINE", raising=False)
    path = _docx(tmp_path / "spawned.docx", "TODO here")
    out = _check(path, checks=["docx.dummy_todo", "docx.dummy_final"])
    assert out.startswith(f"Checked {path} (docx): 2 checks ran.") and "docx.dummy_todo at paragraph index 0" in out


# ------------------------------------------------------- render: true

def _page_count(ctx):
    pages = len(re.findall(rb"/Type\s*/Page\b(?!s)", Path(ctx.rendered_pdf).read_bytes()))
    return [checks.Finding(checks.Severity.INFO, "", message=f"the rendering holds {pages} pages")]


@pytest.fixture()
def rendered(hooks, monkeypatch):
    """A render check, and a fake LibreOffice that records each folder it
    renders into."""
    import render_engine

    folders = []

    async def to_pdf(path, *, out_dir, **kw):
        folders.append(Path(out_dir))
        out = Path(out_dir) / (Path(path).stem + ".pdf")
        out.write_bytes(b"%PDF-1.4\n<< /Type /Page >>\n<< /Type /Page >>\n%%EOF\n")
        return str(out)

    checks.register("docx", "dummy_pages", _page_count, cost=checks.COST_RENDER)
    checks.register("pdf", "dummy_pages", _page_count, cost=checks.COST_RENDER)
    monkeypatch.setattr(render_engine, "to_pdf", to_pdf)
    return folders


def test_render_runs_the_checks_that_read_the_rendering(rendered, tmp_path):
    path = _docx(tmp_path / "r.docx", "Intro")
    assert "dummy_pages" not in _check(path) and rendered == []
    out = _check(path, render=True)
    assert out.startswith(f"Checked {path} (docx): 3 checks ran.") and "the rendering holds 2 pages" in out
    assert len(rendered) == 1 and not rendered[0].exists()
    assert out.count("dummy_pages") == 1 and "reads the rendering" not in out


def test_a_pdf_is_its_own_rendering(rendered, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n<< /Type /Page >>\n%%EOF\n")
    assert "the rendering holds 1 pages" in _check(pdf, render=True) and rendered == []


def test_a_failed_rendering_still_runs_the_other_checks(rendered, tmp_path, monkeypatch):
    import render_engine

    async def broken(path, *, out_dir, **kw):
        rendered.append(Path(out_dir))
        raise RuntimeError("no slot came free")

    monkeypatch.setattr(render_engine, "to_pdf", broken)
    out = _check(_docx(tmp_path / "r.docx", "TODO"), render=True)
    assert "2 checks ran." in out and "docx.dummy_todo" in out and "dummy_pages" not in out
    assert out.endswith("The rendering failed (no slot came free), so the checks that read it did not run.")
    assert not rendered[0].exists()


def test_render_says_when_no_check_reads_it(hooks, tmp_path, monkeypatch):
    import render_engine

    async def to_pdf(path, *, out_dir, **kw):
        out = Path(out_dir) / "r.pdf"
        out.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return str(out)

    monkeypatch.setattr(render_engine, "to_pdf", to_pdf)
    out = _check(_docx(tmp_path / "r.docx", "x"), render=True)
    assert out.endswith("No check of this format reads the rendering yet.")


@pytest.mark.office
def test_render_through_libreoffice(hooks, tmp_path):
    checks.register("docx", "dummy_pages", _page_count, cost=checks.COST_RENDER)
    out = _check(_docx(tmp_path / "r.docx", "Ελληνικά", "English"), render=True)
    assert "the rendering holds 1 pages" in out


def test_a_cancelled_rendering_leaves_no_folder(rendered, tmp_path, monkeypatch):
    import tempfile

    import render_engine

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    began = []

    async def scenario():
        writing = asyncio.Event()

        async def slow(path, *, out_dir, **kw):
            (Path(out_dir) / "partial.pdf").write_bytes(b"%PDF")
            began.append(out_dir)
            writing.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(render_engine, "to_pdf", slow)
        path = _docx(tmp_path / "r.docx", "x")
        task = asyncio.create_task(checks.handle_check_document({"path": str(path), "render": True}))
        await writing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert began and list(scratch.iterdir()) == []


# ------------------------------------------------------- render: true on a workbook

def _workbook(path, formula):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active["A1"] = formula
    book.save(path)
    return path


@pytest.fixture()
def recalculated(rendered, monkeypatch):
    """The xlsx checks, and a fake LibreOffice recalculation that stores
    #DIV/0! as A1's value; each copy it recalculates is recorded."""
    import render_engine
    from formats.xlsx import formula_checks

    formula_checks.register_all()
    seen = []

    async def recalc(path, *, out_dir, **kw):
        seen.append(Path(path))
        out = Path(out_dir) / (Path(path).stem + ".xlsx")
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w") as dst:
            for item in src.infolist():
                data = src.read(item)
                if item.filename == "xl/worksheets/sheet1.xml":
                    data = re.sub(rb'<c r="A1"([^>]*)><f>([^<]*)</f><v\s*/?>(</v>)?',
                                  rb'<c r="A1" t="e"><f>\2</f><v>#DIV/0!</v>', data)
                dst.writestr(item, data)
        return str(out)

    monkeypatch.setattr(render_engine, "recalc", recalc)
    return seen


def test_render_checks_a_recalculated_copy_of_a_workbook(recalculated, tmp_path):
    path = _workbook(tmp_path / "w.xlsx", "=1/0")
    before = path.read_bytes()
    plain = _check(path)
    assert "#DIV/0!" not in plain and "no stored value" in plain and recalculated == []
    out = _check(path, render=True)
    assert "the formula gives #DIV/0!" in out and "no stored value" not in out
    assert "a recalculated copy" in out
    assert path.read_bytes() == before
    assert len(recalculated) == 1 and recalculated[0] != path and not recalculated[0].parent.exists()


def test_a_workbook_that_reads_outside_data_is_not_recalculated(recalculated, tmp_path):
    path = _workbook(tmp_path / "w.xlsx", "='/data/[book.xlsx]Sheet1'!A1")
    out = _check(path, render=True)
    assert recalculated == [] and "reads outside data" in out and "not recalculated" in out


def test_a_workbook_without_formulas_is_checked_as_it_is(recalculated, tmp_path):
    path = _workbook(tmp_path / "w.xlsx", "plain text")
    out = _check(path, render=True)
    assert recalculated == [] and "recalculated" not in out


def test_a_failed_recalculation_still_checks_the_file(recalculated, tmp_path, monkeypatch):
    import render_engine

    async def broken(path, *, out_dir, **kw):
        raise RuntimeError("no slot came free")

    monkeypatch.setattr(render_engine, "recalc", broken)
    out = _check(_workbook(tmp_path / "w.xlsx", "=1/0"), render=True)
    assert "no stored value" in out and "The recalculation failed (no slot came free)" in out


@pytest.mark.office
def test_render_recalculates_a_workbook_with_libreoffice(hooks, tmp_path):
    from formats.xlsx import formula_checks

    formula_checks.register_all()
    path = _workbook(tmp_path / "w.xlsx", "=1/0")
    out = _check(path, render=True)
    assert "the formula gives #DIV/0!" in out and "a recalculated copy" in out, out
