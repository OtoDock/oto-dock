"""Old-format documents (.doc, .xls, .ppt): read and checked through a copy
LibreOffice converts, a modern package under an old name read directly, an
encrypted file refused before LibreOffice starts, and the work folder always
removed. The office tests convert real files in the image."""

import asyncio
import io
import shutil
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import checks
import legacy
import render_engine
import server
import shared

docx = pytest.importorskip("docx")
_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@pytest.fixture()
def hooks(check_registry, monkeypatch, tmp_path):
    async def resolve(value, writing=False, **kw):
        return value

    monkeypatch.setattr(shared, "_resolve_path", resolve)
    monkeypatch.setattr(server, "_resolve_path", resolve)
    import dummy_checks

    dummy_checks.register_all()
    from ooxml import validate

    validate.register_checks()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


def _docx_bytes(*paragraphs) -> bytes:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


_FREE, _END, _FATSECT, _NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


def _compound(streams: dict, shift: int = 9) -> bytes:
    """A minimal OLE2 compound file (512-byte sectors, or 4,096 in a
    version 4 file with ``shift`` 12; no mini stream: every stream padded
    to 4,096 bytes) holding ``streams``."""
    size, start, chains = 1 << shift, 2, []
    for name, payload in streams.items():
        payload = payload.ljust(4096, b"\x00")
        chains.append((name, start, payload))
        start += len(payload) // size
    fat = [_FATSECT, _END]
    for _, first, payload in chains:
        count = len(payload) // size
        fat += [first + i + 1 for i in range(count - 1)] + [_END]
    fat += [_FREE] * (size // 4 - len(fat))

    def entry(name, kind, first, length, child=_NOSTREAM):
        raw = name.encode("utf-16-le") + b"\x00\x00"
        return (raw.ljust(64, b"\x00") + len(raw).to_bytes(2, "little") + bytes([kind, 1])
                + _NOSTREAM.to_bytes(4, "little") * 2 + child.to_bytes(4, "little") + b"\x00" * 36
                + first.to_bytes(4, "little") + length.to_bytes(8, "little"))

    directory = entry("Root Entry", 5, _END, 0, child=1)
    for name, first, payload in chains:
        directory += entry(name, 2, first, len(payload))
    directory = directory.ljust(size, b"\x00")
    header = (_CFB + b"\x00" * 16 + (0x3E).to_bytes(2, "little") + (3 if shift == 9 else 4).to_bytes(2, "little")
              + b"\xfe\xff" + shift.to_bytes(2, "little") + (6).to_bytes(2, "little") + b"\x00" * 6 + (0).to_bytes(4, "little")
              + (1).to_bytes(4, "little") + (1).to_bytes(4, "little") + (0).to_bytes(4, "little")
              + (4096).to_bytes(4, "little") + _END.to_bytes(4, "little") + (0).to_bytes(4, "little")
              + _END.to_bytes(4, "little") + (0).to_bytes(4, "little") + (0).to_bytes(4, "little")
              + _FREE.to_bytes(4, "little") * 108)
    header = header.ljust(size, b"\x00")
    body = b"".join(v.to_bytes(4, "little") for v in fat) + directory + b"".join(p for _, _, p in chains)
    return header + body


def _word97(encrypted: bool = False, text: bytes = b"", shift: int = 9) -> bytes:
    flags = (0x0100 if encrypted else 0).to_bytes(2, "little")
    return _compound({"WordDocument": b"\xec\xa5\xc1\x00\x00\x00\x00\x00\x00\x00" + flags + text,
                      "1Table": b"\x00"}, shift)


def _excel97(encrypted: bool = False) -> bytes:
    bof = (0x0809).to_bytes(2, "little") + (16).to_bytes(2, "little") + b"\x00" * 16
    filepass = (0x002F).to_bytes(2, "little") + (4).to_bytes(2, "little") + b"\x00" * 4
    return _compound({"Workbook": bof + (filepass if encrypted else b"")})


def _powerpoint97(encrypted: bool = False) -> bytes:
    token = 0xF3D1C4DF if encrypted else 0xE391C05F
    atom = b"\x00" * 8 + (20).to_bytes(4, "little") + token.to_bytes(4, "little")
    return _compound({"PowerPoint Document": b"\x00" * 8, "Current User": atom})


@pytest.fixture()
def converter(monkeypatch):
    """A LibreOffice stand-in: the converted copy is a python-docx file."""
    seen = []

    async def to_ooxml(path, target_ext, *, out_dir, **kw):
        seen.append((Path(path).name, target_ext, kw))
        out = Path(out_dir) / (Path(path).stem + target_ext)
        out.write_bytes(_docx_bytes("Παλιό έγγραφο", "TODO: numbers"))
        return str(out)

    monkeypatch.setattr(render_engine, "to_ooxml", to_ooxml)
    return seen


def _read(path):
    return asyncio.run(server._handle_read_document({"path": str(path)}))


def _check(path, **args):
    return asyncio.run(checks.handle_check_document({"path": str(path), **args}))


def test_a_legacy_file_is_read_through_its_converted_copy(hooks, converter, tmp_path):
    old = tmp_path / "Έκθεση με πολύ μεγάλο όνομα.doc"
    old.write_bytes(_word97())
    out = _read(old)
    assert out.splitlines()[0].startswith(f"[{old.name} was read from a .docx copy LibreOffice made of it")
    assert "Παλιό έγγραφο" in out
    assert converter == [("source.doc", ".docx", {"slot_wait_s": legacy.SLOT_WAIT_S,
                                                   "timeout_s": converter[0][2]["timeout_s"]})]
    assert converter[0][2]["timeout_s"] <= legacy.CONVERT_S
    assert list(hooks.iterdir()) == []


def test_a_modern_package_with_an_old_name_is_read_without_libreoffice(hooks, monkeypatch, tmp_path):
    async def never(*a, **k):
        raise AssertionError("LibreOffice ran")

    monkeypatch.setattr(render_engine, "to_ooxml", never)
    renamed = tmp_path / "renamed.doc"
    renamed.write_bytes(_docx_bytes("modern inside"))
    out = _read(renamed)
    assert out.splitlines()[0] == "[renamed.doc is a .docx package with an older name; it was read as .docx.]"
    assert "modern inside" in out and list(hooks.iterdir()) == []


def test_an_encrypted_file_is_refused_before_libreoffice(hooks, monkeypatch, tmp_path):
    async def never(*a, **k):
        raise AssertionError("LibreOffice ran")

    monkeypatch.setattr(render_engine, "to_ooxml", never)
    locked = tmp_path / "locked.xls"
    locked.write_bytes(_compound({"EncryptionInfo": b"\x04\x00", "EncryptedPackage": b"\x00"}))
    assert _read(locked) == ("Error: locked.xls could not be read: the file is password-protected, so it cannot "
                             "be read. Save an unprotected copy and use that.")
    assert _check(locked).startswith("Error: locked.xls could not be checked: the file is password-protected")
    assert list(hooks.iterdir()) == []


@pytest.mark.parametrize("name, data, fragment", [
    ("letter.doc", b"{\\rtf1\\ansi Hello}", "it is RTF"),
    ("page.doc", b"<!DOCTYPE html><html><body><img src='http://example.com/x.png'></body></html>",
     "it is a web page"),
    ("book.xls", b'<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"/>',
     "it is XML"),
    ("notes.ppt", b"just some text", "match no Office format"),
    ("broken.doc", _CFB + b"\x00" * 600, "not a readable old Office file"),
    ("wrong-kind.xls", None, "is not a Excel 97-2003 file"),
])
def test_only_a_real_old_office_file_reaches_libreoffice(hooks, monkeypatch, tmp_path, name, data, fragment):
    async def never(*a, **k):
        raise AssertionError("LibreOffice ran")

    monkeypatch.setattr(render_engine, "to_ooxml", never)
    path = tmp_path / name
    path.write_bytes(_word97() if data is None else data)
    out = _read(path)
    assert out.startswith(f"Error: {name} could not be read: ") and fragment in out, out
    assert list(hooks.iterdir()) == []


@pytest.mark.parametrize("make", [_word97, _excel97, _powerpoint97], ids=["doc", "xls", "ppt"])
def test_an_old_format_encryption_is_refused_before_libreoffice(hooks, monkeypatch, tmp_path, make):
    async def never(*a, **k):
        raise AssertionError("LibreOffice ran")

    monkeypatch.setattr(render_engine, "to_ooxml", never)
    suffix = {"_word97": ".doc", "_excel97": ".xls", "_powerpoint97": ".ppt"}[make.__name__]
    locked = tmp_path / f"locked{suffix}"
    locked.write_bytes(make(encrypted=True))
    assert "password-protected" in _read(locked)


@pytest.mark.parametrize("make", [_word97, _excel97, _powerpoint97], ids=["doc", "xls", "ppt"])
def test_an_old_format_file_that_is_not_encrypted_is_converted(hooks, converter, tmp_path, make):
    suffix = {"_word97": ".doc", "_excel97": ".xls", "_powerpoint97": ".ppt"}[make.__name__]
    path = tmp_path / f"plain{suffix}"
    path.write_bytes(make())
    _read(path)
    assert converter[-1][:2] == ("source" + suffix, legacy.LEGACY[suffix])


def test_text_that_names_an_encryption_stream_is_not_encryption(hooks, converter, tmp_path):
    path = tmp_path / "about-encryption.doc"
    path.write_bytes(_word97(text="EncryptionInfo EncryptedPackage".encode("utf-16-le")))
    assert "copy LibreOffice made of it" in _read(path).splitlines()[0]


@pytest.mark.parametrize("path", sorted((_HERE / "corpus" / "shared").glob("*.[dx][ol][cs]")), ids=lambda p: p.name)
def test_the_corpus_legacy_files_pass_the_compound_check(path):
    assert legacy.compound_problem(str(path), path.suffix) is None


def test_a_conversion_that_fails_says_what_to_do(hooks, monkeypatch, tmp_path):
    async def broken(*a, **k):
        raise RuntimeError("LibreOffice error: general input/output error")

    monkeypatch.setattr(render_engine, "to_ooxml", broken)
    old = tmp_path / "old.ppt"
    old.write_bytes(_powerpoint97())
    out = _read(old)
    assert out.startswith("Error: old.ppt could not be read: LibreOffice could not convert it to .pptx")
    assert out.endswith("save it there as .pptx and use that.") and list(hooks.iterdir()) == []


def test_a_file_too_large_to_convert_is_refused(hooks, converter, monkeypatch, tmp_path):
    monkeypatch.setattr(legacy, "LEGACY_MAX_BYTES", 100)
    big = tmp_path / "big.doc"
    big.write_bytes(_word97())
    assert "past the 0 MB an old-format file is converted at" in _read(big) and converter == []


def test_check_document_checks_the_copy_and_says_so(hooks, converter, tmp_path):
    old = tmp_path / "old.doc"
    old.write_bytes(_word97())
    out = _check(old)
    assert out.splitlines()[0] == (f"Checked {old} (docx, as the .docx copy LibreOffice made of it; locations "
                                   "refer to that copy): 2 checks ran.")
    assert "docx.dummy_todo" in out and "docx.package" not in out and "docx.parts_lost" not in out
    assert list(hooks.iterdir()) == []


def test_a_modern_package_with_an_old_name_keeps_its_package_checks(hooks, monkeypatch, tmp_path):
    """Unconverted, the bytes are the person's own: the package checks judge
    them as they do any .docx."""
    async def never(*a, **k):
        raise AssertionError("LibreOffice ran")

    monkeypatch.setattr(render_engine, "to_ooxml", never)
    renamed = tmp_path / "renamed.doc"
    renamed.write_bytes(_docx_bytes("TODO in a renamed file"))
    out = _check(renamed)
    assert "package under an older name" in out.splitlines()[0]
    assert out.splitlines()[0].endswith("4 checks ran."), out


def test_the_work_folder_goes_when_the_read_is_cancelled(hooks, monkeypatch, tmp_path):
    started = asyncio.Event()

    async def slow(path, target_ext, *, out_dir, **kw):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(render_engine, "to_ooxml", slow)
    old = tmp_path / "slow.doc"
    old.write_bytes(_word97())

    async def scenario():
        task = asyncio.create_task(server._handle_read_document({"path": str(old)}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert list(hooks.iterdir()) == []


# ---------------------------------------------------------------- the engine

def _saved_as(tmp_path, data: bytes, suffix: str, legacy_filter: str, extension: str) -> Path:
    """A real old-format file: python-library output saved by LibreOffice
    in the old format."""
    source_dir = tmp_path / "make"
    source_dir.mkdir(exist_ok=True)
    source = source_dir / f"made{suffix}"
    source.write_bytes(data)
    out = tmp_path / "old"
    out.mkdir(exist_ok=True)
    produced = asyncio.run(render_engine._libreoffice_convert(str(source), legacy_filter, str(out), extension=extension))
    target = tmp_path / f"Παλιό.{extension}"
    shutil.move(produced, target)
    return target


@pytest.mark.office
def test_a_real_doc_xls_and_ppt_are_read_and_checked(hooks, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    pptx = pytest.importorskip("pptx")
    doc = _saved_as(tmp_path, _docx_bytes("Ελληνικά στο παλιό Word", "TODO later"), ".docx", "doc:MS Word 97", "doc")
    book = openpyxl.Workbook()
    book.active["A1"], book.active["A2"] = "Τιμή", 42
    buf = io.BytesIO()
    book.save(buf)
    xls = _saved_as(tmp_path, buf.getvalue(), ".xlsx", "xls:MS Excel 97", "xls")
    deck = pptx.Presentation()
    deck.slides.add_slide(deck.slide_layouts[0]).shapes.title.text = "Παρουσίαση"
    buf = io.BytesIO()
    deck.save(buf)
    ppt = _saved_as(tmp_path, buf.getvalue(), ".pptx", "ppt:MS PowerPoint 97", "ppt")
    assert doc.read_bytes()[:8] == _CFB and xls.read_bytes()[:8] == _CFB and ppt.read_bytes()[:8] == _CFB
    for path, expected in ((doc, "Ελληνικά στο παλιό Word"), (xls, "Τιμή"), (ppt, "Παρουσίαση")):
        out = _read(path)
        assert "copy LibreOffice made of it" in out.splitlines()[0] and expected in out, out[:400]
    checked = _check(doc)
    assert "as the .docx copy LibreOffice made of it" in checked and "docx.dummy_todo" in checked


@pytest.mark.office
@pytest.mark.parametrize("path", sorted((_HERE / "corpus" / "shared").glob("*.[dx][ol][cs]")), ids=lambda p: p.name)
def test_the_corpus_legacy_files_are_read(hooks, path, tmp_path):
    """Files Word and Excel saved in their old binary formats."""
    local = tmp_path / path.name
    shutil.copyfile(path, local)
    out = _read(local)
    assert out.splitlines()[0].startswith(f"[{path.name} was read from a") and len(out) > 200, out[:300]


def _crafted_header(*, fat_count=1, directory=1, difat_start=_END, difat_count=0, difat=(), minifat=_END) -> bytes:
    listed = list(difat) + [_FREE] * (109 - len(difat))
    return (_CFB + b"\x00" * 16 + (0x3E).to_bytes(2, "little") + (3).to_bytes(2, "little") + b"\xfe\xff"
            + (9).to_bytes(2, "little") + (6).to_bytes(2, "little") + b"\x00" * 6 + (0).to_bytes(4, "little")
            + fat_count.to_bytes(4, "little") + directory.to_bytes(4, "little") + (0).to_bytes(4, "little")
            + (4096).to_bytes(4, "little") + minifat.to_bytes(4, "little") + (0).to_bytes(4, "little")
            + difat_start.to_bytes(4, "little") + difat_count.to_bytes(4, "little")
            + b"".join(v.to_bytes(4, "little") for v in listed))


def _sector(*entries) -> bytes:
    return b"".join(v.to_bytes(4, "little") for v in entries).ljust(512, b"\x00")


_LOOPS = {
    # the review's file: DIFAT count 0xFFFFFFFF, DIFAT at sector 0, sector 0 all zeros (it points to itself)
    "difat-points-to-itself": _crafted_header(difat_start=0, difat_count=0xFFFFFFFF) + b"\x00" * 1024,
    "difat-two-sector-cycle": _crafted_header(difat_start=0, difat_count=0xFFFFFFFF)
    + _sector(*([2] * 127 + [1])) + _sector(*([2] * 127 + [0])) + _sector(),
    "fat-sector-listed-109-times": _crafted_header(difat=[0] * 109) + _sector(0, 1) + _sector(),
    "directory-points-to-itself": _crafted_header(difat=[0]) + _sector(_FATSECT, 1) + _sector(),
}


@pytest.mark.parametrize("data", _LOOPS.values(), ids=_LOOPS.keys())
def test_a_compound_file_whose_tables_loop_is_refused_at_once(tmp_path, data):
    import time

    path = tmp_path / "loop.doc"
    path.write_bytes(data)
    began = time.monotonic()
    problem = legacy.compound_problem(str(path), ".doc")
    assert time.monotonic() - began < 1.0
    assert problem and "not a readable old Office file" in problem


def test_the_review_file_is_refused_by_read_document(hooks, converter, tmp_path):
    path = tmp_path / "loop.doc"
    path.write_bytes(_LOOPS["difat-points-to-itself"])
    assert len(path.read_bytes()) == 1536
    out = _read(path)
    assert out.startswith("Error: loop.doc could not be read: it is not a readable old Office file")
    assert converter == [] and list(hooks.iterdir()) == []


def test_the_compound_check_runs_in_a_parse_worker(hooks, converter, monkeypatch, tmp_path):
    import isolation

    ran = []
    real = isolation.run_parse

    async def recording(target, *args, **kwargs):
        ran.append((target.__name__, kwargs.get("_timeout_s")))
        return await real(target, *args, **kwargs)

    monkeypatch.setattr(isolation, "run_parse", recording)
    path = tmp_path / "old.doc"
    path.write_bytes(_word97())
    assert "copy LibreOffice made of it" in _read(path).splitlines()[0]
    assert ran[0] == ("compound_problem", legacy.CHECK_S)


def test_a_compound_check_that_fails_in_its_worker_says_what_to_do(hooks, converter, monkeypatch, tmp_path):
    import isolation

    async def over_budget(target, *args, **kwargs):
        raise RuntimeError("processing took longer than 30s and was cancelled. " + kwargs["_advice"])

    monkeypatch.setattr(isolation, "run_parse", over_budget)
    path = tmp_path / "old.doc"
    path.write_bytes(_word97())
    out = _read(path)
    assert out.startswith("Error: old.doc could not be read: it could not be checked as an old Office file")
    assert out.endswith("save it as .docx and use that.") and converter == []


@pytest.mark.parametrize("encrypted", [False, True])
def test_a_file_with_4096_byte_sectors_is_read(tmp_path, encrypted):
    """Version 4 files put sector n at (n + 1) x 4,096 bytes."""
    path = tmp_path / "v4.doc"
    path.write_bytes(_word97(encrypted=encrypted, shift=12))
    problem = legacy.compound_problem(str(path), ".doc")
    assert (problem is not None and "password" in problem) if encrypted else problem is None


def _mini_across_the_file(sectors: int) -> bytes:
    """A version 4 file whose mini FAT chain and root chain run across
    every sector, holding one small (mini) WordDocument stream."""
    size, fat_sectors = 4096, -(-sectors // 1024)
    directory_at, first = fat_sectors, fat_sectors + 1
    fat = [_FATSECT] * fat_sectors + [_END] + [n + 1 for n in range(first, sectors - 1)] + [_END]
    fat += [_FREE] * (fat_sectors * 1024 - len(fat))

    def entry(name, kind, start, length, child=_NOSTREAM):
        raw = name.encode("utf-16-le") + b"\x00\x00"
        return (raw.ljust(64, b"\x00") + len(raw).to_bytes(2, "little") + bytes([kind, 1])
                + _NOSTREAM.to_bytes(4, "little") * 2 + child.to_bytes(4, "little") + b"\x00" * 36
                + start.to_bytes(4, "little") + length.to_bytes(8, "little"))

    directory = (entry("Root Entry", 5, first, sectors * size, child=1) + entry("WordDocument", 2, 0, 100))
    header = (_CFB + b"\x00" * 16 + (0x3E).to_bytes(2, "little") + (4).to_bytes(2, "little") + b"\xfe\xff"
              + (12).to_bytes(2, "little") + (6).to_bytes(2, "little") + b"\x00" * 6 + (1).to_bytes(4, "little")
              + fat_sectors.to_bytes(4, "little") + directory_at.to_bytes(4, "little") + (0).to_bytes(4, "little")
              + (4096).to_bytes(4, "little") + first.to_bytes(4, "little") + (0xFFFFFFF).to_bytes(4, "little")
              + _END.to_bytes(4, "little") + (0).to_bytes(4, "little")
              + b"".join(n.to_bytes(4, "little") for n in range(fat_sectors))
              + _FREE.to_bytes(4, "little") * (109 - fat_sectors))
    fib = b"\xec\xa5\xc1\x00" + b"\x00" * 60
    body = (b"".join(v.to_bytes(4, "little") for v in fat) + directory.ljust(size, b"\x00")
            + fib.ljust(size, b"\x00") + b"\x00" * size * (sectors - first - 1))
    return header.ljust(size, b"\x00") + body


def test_a_mini_fat_across_the_whole_file_is_read_only_where_needed(tmp_path):
    """The review's worst file, at 20 MB: the mini FAT chain runs across
    every sector. Only the mini FAT sector the stream's chain needs is
    read."""
    import time
    import tracemalloc

    path = tmp_path / "big.doc"
    path.write_bytes(_mini_across_the_file(5000))
    tracemalloc.start()
    began = time.monotonic()
    problem = legacy.compound_problem(str(path), ".doc")
    took = time.monotonic() - began
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert problem is None
    assert took < 0.5 and peak < 16 * 1024 * 1024, (took, peak)


def test_a_difat_chain_of_repeats_stops_at_what_the_file_can_need(tmp_path):
    """The review's other worst file, at 20 MB: every sector a DIFAT sector
    listing sector 0 again."""
    import time

    sectors = 40_000
    block = (b"\x00\x00\x00\x00" * 127)
    body = b"\x00" * 512 + b"".join(block + (n + 1).to_bytes(4, "little") for n in range(1, sectors))
    path = tmp_path / "repeats.doc"
    path.write_bytes(_crafted_header(difat_start=1, difat_count=0xFFFFFFFF) + body)
    began = time.monotonic()
    problem = legacy.compound_problem(str(path), ".doc")
    assert time.monotonic() - began < 0.3
    assert problem and "not a readable old Office file" in problem
