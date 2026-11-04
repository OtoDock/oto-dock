"""Rendering in the parent: the profile and filter of each kind, the
recalculation's bounds, the caller's folder, a cancelled render ending its
LibreOffice, and the Collabora backend against a fake service (what it is
sent, when it is used, how every failure falls back). The office tests run
the real engine in the image."""

import asyncio
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import render_engine
import shared


class _Run:
    """A fake LibreOffice run: records its argv, writes its output, and can
    hang until cancelled."""

    def __init__(self, hang: bool = False) -> None:
        self.hang = hang
        self.argv: list = []
        self.killed: list = []

    async def spawn(self, *argv, **kw):
        self.argv = list(argv)
        run = self

        class _Proc:
            returncode = None if run.hang else 0
            pid = 4242

            async def communicate(self):
                if run.hang:
                    await asyncio.sleep(60)
                out_dir = Path(argv[argv.index("--outdir") + 1])
                target = argv[argv.index("--convert-to") + 1].split(":", 1)[0]
                (out_dir / f"{Path(argv[-1]).stem}.{target}").write_bytes(b"made")
                return b"", b""

            async def wait(self):
                return 0

        return _Proc()


@pytest.fixture
def fake_lo(tmp_path, monkeypatch):
    monkeypatch.setattr(render_engine, "_LO_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(render_engine, "_lo_slot_count", lambda: 1)
    run = _Run()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", run.spawn)

    def killpg(pgid, sig):
        run.killed.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError(pgid)

    monkeypatch.setattr(render_engine.os, "killpg", killpg)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    return tmp_path, run


def test_to_pdf_converts_under_the_convert_profile(fake_lo):
    tmp_path, run = fake_lo
    source = tmp_path / "in" / "report.docx"
    source.write_bytes(b"x")
    out = asyncio.run(render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out")))
    assert out == str(tmp_path / "out" / "report.pdf")
    assert run.argv[run.argv.index("libreoffice") + 1].endswith("/profiles/slot-0-convert") and run.argv[run.argv.index("--convert-to") + 1] == "pdf"


def test_recalc_saves_xlsx_under_its_own_profile(fake_lo):
    tmp_path, run = fake_lo
    source = tmp_path / "in" / "budget.xlsx"
    source.write_bytes(b"x")
    out = asyncio.run(render_engine.recalc(str(source), out_dir=str(tmp_path / "out")))
    assert out == str(tmp_path / "out" / "budget.xlsx")
    assert run.argv[run.argv.index("libreoffice") + 1].endswith("/profiles/slot-0-recalc")
    assert run.argv[run.argv.index("--convert-to") + 1] == "xlsx:Calc MS Excel 2007 XML"
    profile = (tmp_path / "profiles" / "slot-0-recalc" / "user" / "registrymodifications.xcu").read_text()
    assert 'name="OOXMLRecalcMode" oor:op="fuse"><value>0' in profile
    assert 'name="DisableMacrosExecution" oor:op="fuse"><value>true' in profile


def test_recalc_keeps_its_bounds_whatever_the_caller_allows(fake_lo, monkeypatch):
    tmp_path, _ = fake_lo
    seen = {}
    real = render_engine._libreoffice_convert

    async def recording(*args, **kw):
        seen.update(kw)
        return await real(*args, **kw)

    monkeypatch.setattr(render_engine, "_libreoffice_convert", recording)
    (tmp_path / "in" / "b.xlsx").write_bytes(b"x")
    asyncio.run(render_engine.recalc(str(tmp_path / "in" / "b.xlsx"), out_dir=str(tmp_path / "out"),
                                     timeout_s=500, slot_wait_s=500))
    assert seen["timeout_s"] == render_engine.RECALC_RUN_S and seen["slot_wait_s"] == render_engine.RECALC_SLOT_WAIT_S


def test_the_output_folder_is_the_callers_own(fake_lo):
    tmp_path, _ = fake_lo
    source = tmp_path / "in" / "r.docx"
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match="must not be the input's own folder"):
        asyncio.run(render_engine.to_pdf(str(source), out_dir=str(tmp_path / "in")))
    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(render_engine.to_pdf(str(source), out_dir=str(tmp_path / "missing")))


def test_a_cancelled_render_ends_its_libreoffice(fake_lo):
    """A render phase cut at its budget leaves no LibreOffice running and
    its slot free again."""
    tmp_path, run = fake_lo
    run.hang = True
    source = tmp_path / "in" / "slow.docx"
    source.write_bytes(b"x")

    async def scenario():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out")), 0.2)
        for _ in range(50):
            if render_engine._lo_free_slots().qsize() == 1:
                break
            await asyncio.sleep(0.02)
        return render_engine._lo_free_slots().qsize()

    assert asyncio.run(scenario()) == 1
    assert (4242, 9) in run.killed


def test_a_cancelled_conversion_has_ended_before_its_caller_goes_on(fake_lo):
    """The caller removes the output folder right after a cancel: by then
    LibreOffice's processes are gone and its slot is free."""
    tmp_path, run = fake_lo
    run.hang = True
    source = tmp_path / "in" / "slow.docx"
    source.write_bytes(b"x")

    async def scenario():
        task = asyncio.create_task(render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out")))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return run.killed[:], render_engine._lo_free_slots().qsize()

    killed, free = asyncio.run(scenario())
    assert (4242, 9) in killed and free == 1


def test_libreoffice_runs_with_the_highest_oom_score(fake_lo, monkeypatch):
    tmp_path, run = fake_lo
    render_engine._oom_first.cache_clear()
    monkeypatch.setattr(render_engine.shutil, "which", lambda name: f"/usr/bin/{name}")
    source = tmp_path / "in" / "r.docx"
    source.write_bytes(b"x")
    asyncio.run(render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out")))
    render_engine._oom_first.cache_clear()
    assert run.argv[:5] == ["choom", "-n", "1000", "--", "libreoffice"]


# ---------------------------------------------------------------- the engine

@pytest.mark.office
def test_a_document_becomes_a_pdf(tmp_path):
    docx = pytest.importorskip("docx")
    fitz = pytest.importorskip("fitz")
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    document = docx.Document()
    document.add_paragraph("Ελληνικά and English")
    document.save(str(tmp_path / "in" / "r.docx"))
    out = asyncio.run(render_engine.to_pdf(str(tmp_path / "in" / "r.docx"), out_dir=str(tmp_path / "out")))
    with fitz.open(out) as pdf:
        assert pdf.page_count == 1 and "English" in pdf[0].get_text()


@pytest.mark.office
def test_a_workbook_gets_its_computed_values(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet["A1"], sheet["A2"], sheet["A3"], sheet["B1"] = 2, 3, "=A1*A2", "=1/0"
    book.save(str(tmp_path / "in" / "b.xlsx"))
    out = asyncio.run(render_engine.recalc(str(tmp_path / "in" / "b.xlsx"), out_dir=str(tmp_path / "out")))
    values = openpyxl.load_workbook(io.BytesIO(Path(out).read_bytes()), data_only=True).active
    formulas = openpyxl.load_workbook(io.BytesIO(Path(out).read_bytes())).active
    assert values["A3"].value == 6 and formulas["A3"].value == "=A1*A2"
    assert values["B1"].value == "#DIV/0!"


def test_a_write_renders_into_a_subfolder_of_its_work_folder(fake_lo):
    """The staged file lives in the call's work folder, so the output goes
    to a subfolder of it (the output keeps the input's name)."""
    tmp_path, _ = fake_lo
    work = tmp_path / "work"
    work.mkdir()
    staged = work / "staged.xlsx"
    staged.write_bytes(b"x")
    out_dir = render_engine.output_folder(str(work), "recalc")
    assert out_dir == str(work / "recalc") and render_engine.output_folder(str(work), "recalc") == out_dir
    assert asyncio.run(render_engine.recalc(str(staged), out_dir=out_dir)) == str(work / "recalc" / "staged.xlsx")
    for bad in ("", ".", "..", "a/b", "../escape"):
        with pytest.raises(ValueError, match="plain folder name"):
            render_engine.output_folder(str(work), bad)


# ---------------------------------------------------------------- Collabora

CAPABILITIES = {"convert-to": {"available": True, "endpoint": "/cool/convert-to"}, "productVersion": "26.04.5.1"}
BASE = "http://collabora:9980/collabora"


class _Cool:
    """A fake Collabora: answers the probe and the conversions as a test
    says, and records every request."""

    def __init__(self) -> None:
        self.probe = (200, json.dumps(CAPABILITIES).encode())
        self.convert = (200, b"%PDF-1.7 made by collabora")
        self.delay = 0.0
        self.requests: list = []
        self.in_flight = 0
        self.most_in_flight = 0

    async def handle(self, request):
        import httpx

        self.requests.append(request)
        if request.url.path.endswith("/hosting/capabilities"):
            status, body = self.probe
            return httpx.Response(status, content=body)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            body = await request.aread()
            request.body_seen = body
            if self.delay:
                await asyncio.sleep(self.delay)
            status, answer = self.convert
            headers = {"location": "http://elsewhere/"} if 300 <= status < 400 else {}
            return httpx.Response(status, content=answer, headers=headers)
        finally:
            self.in_flight -= 1

    def posts(self) -> list:
        return [r for r in self.requests if r.method == "POST"]


@pytest.fixture
def cool(fake_lo, monkeypatch):
    import httpx

    tmp_path, run = fake_lo
    fake = _Cool()
    made: list[dict] = []

    def client(connect):
        made.append({"connect": connect})
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handle), timeout=httpx.Timeout(None),
                                 trust_env=False, follow_redirects=False)

    monkeypatch.setenv(render_engine.URL_SETTING, BASE + "/")
    monkeypatch.setattr(render_engine, "_client", client)
    render_engine.reset_backend()
    yield tmp_path, run, fake
    render_engine.reset_backend()


def _docx(tmp_path, name="report.docx", extra: dict | None = None) -> Path:
    source = tmp_path / "in" / name
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        for member, data in (extra or {}).items():
            zf.writestr(member, data)
    return source


def _pdf(tmp_path, source: Path) -> str:
    return asyncio.run(render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out")))


def test_collabora_is_off_without_the_setting(fake_lo, monkeypatch):
    tmp_path, run = fake_lo
    monkeypatch.delenv(render_engine.URL_SETTING, raising=False)
    render_engine.reset_backend()
    out = _pdf(tmp_path, _docx(tmp_path))
    assert Path(out).read_bytes() == b"made" and run.argv


def test_collabora_renders_when_it_answers(cool, caplog):
    tmp_path, run, fake = cool
    with caplog.at_level("INFO", logger="file-tools"):
        out = _pdf(tmp_path, _docx(tmp_path))
    assert "Collabora converted a .docx file to pdf in" in caplog.text and "report" not in caplog.text
    assert out == str(tmp_path / "out" / "report.pdf")
    assert Path(out).read_bytes().startswith(b"%PDF-") and not run.argv
    post = fake.posts()[0]
    assert str(post.url) == BASE + "/cool/convert-to/pdf"
    assert b'name="lang"\r\n\r\nen-US' in post.body_seen
    assert b'filename="document.docx"' in post.body_seen and b"report" not in post.body_seen


def test_the_probe_is_kept_for_its_time_and_asked_again_after(cool):
    tmp_path, _, fake = cool
    source = _docx(tmp_path)
    _pdf(tmp_path, source)
    _pdf(tmp_path, source)
    assert len([r for r in fake.requests if r.method == "GET"]) == 1
    render_engine._probe.at -= render_engine.PROBE_TTL_UP_S + 1
    _pdf(tmp_path, source)
    assert len([r for r in fake.requests if r.method == "GET"]) == 2


@pytest.mark.parametrize("probe", [
    (500, b"{}"),
    (200, b"not json"),
    (200, json.dumps({"convert-to": {"available": "true"}}).encode()),
    (200, json.dumps({"convert-to": {"available": False}}).encode()),
    (200, json.dumps({"other": 1}).encode()),
    (200, b"{" + b" " * (render_engine.PROBE_MAX_BYTES + 10) + b"}"),
])
def test_a_bad_probe_means_libreoffice(cool, probe):
    tmp_path, run, fake = cool
    fake.probe = probe
    out = _pdf(tmp_path, _docx(tmp_path))
    assert Path(out).read_bytes() == b"made" and run.argv and not fake.posts()


@pytest.mark.parametrize("answer, marks_down", [
    ((503, b"busy"), True),
    ((502, b"gateway"), True),
    ((500, b"faileddocloading"), False),
    ((403, b"no"), True),
    ((302, b""), True),
    ((200, b"<html>not a pdf</html>"), True),
    ((400, b"cannot load"), False),
])
def test_a_failed_conversion_falls_back_and_only_a_service_fault_marks_it_down(cool, answer, marks_down):
    tmp_path, run, fake = cool
    fake.convert = answer
    source = _docx(tmp_path)
    out = _pdf(tmp_path, source)
    assert Path(out).read_bytes() == b"made" and run.argv
    assert not [r for r in fake.requests if "elsewhere" in str(r.url)]
    fake.convert = (200, b"%PDF-1.7 again")
    run.argv = []
    out = _pdf(tmp_path, source)
    assert (Path(out).read_bytes() == b"made") is marks_down
    assert len(fake.posts()) == (1 if marks_down else 2)


def test_an_answer_past_the_cap_falls_back(cool, monkeypatch):
    tmp_path, run, fake = cool
    monkeypatch.setattr(render_engine, "ANSWER_MAX_BYTES", 64)
    fake.convert = (200, b"%PDF-" + b"x" * 200)
    out = _pdf(tmp_path, _docx(tmp_path))
    assert Path(out).read_bytes() == b"made"


def test_a_collabora_timeout_falls_back_when_time_is_left(cool, monkeypatch):
    tmp_path, run, fake = cool
    monkeypatch.setattr(render_engine, "COLLABORA_RUN_S", 2.2)
    monkeypatch.setattr(render_engine, "MIN_FALLBACK_S", 1.0)
    fake.delay = 5
    source = _docx(tmp_path)
    out = _pdf(tmp_path, source)
    assert Path(out).read_bytes() == b"made"
    assert not (tmp_path / "out" / "report.pdf").read_bytes().startswith(b"%PDF")


def test_a_collabora_timeout_with_too_little_left_is_a_timeout(cool, monkeypatch):
    tmp_path, run, fake = cool
    monkeypatch.setattr(render_engine, "COLLABORA_RUN_S", 2.2)
    fake.delay = 5
    source = _docx(tmp_path)

    async def scenario():
        return await render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out"),
                                          deadline=time.monotonic() + 4)

    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(scenario())
    assert not run.argv and not (tmp_path / "out" / "report.pdf").exists()


def test_after_a_timeout_collabora_is_left_alone_while_it_may_still_convert(cool, monkeypatch):
    tmp_path, run, fake = cool
    monkeypatch.setattr(render_engine, "COLLABORA_RUN_S", 2.2)
    monkeypatch.setattr(render_engine, "MIN_FALLBACK_S", 1.0)
    fake.delay = 5
    source = _docx(tmp_path)

    async def scenario():
        await render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out"))
        render_engine._probe.at -= render_engine.PROBE_TTL_DOWN_S + 1
        fake.delay = 0
        return await render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out"))

    out = asyncio.run(scenario())
    assert Path(out).read_bytes() == b"made" and len(fake.posts()) == 1


class _Silent:
    """A Collabora that answers its probe and accepts every conversion but
    never answers it (a full disk did that): a real socket, so the client's
    own timeouts are what is tested."""

    def __init__(self) -> None:
        import socket
        import threading

        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.posts = 0
        self.held: list = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.server.settimeout(0.1)
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                continue
            conn.settimeout(2)
            try:
                head = conn.recv(65536)
            except OSError:
                conn.close()
                continue
            if head.startswith(b"GET") and b"/hosting/capabilities" in head:
                body = json.dumps(CAPABILITIES).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                conn.close()
            else:
                self.posts += 1
                self.held.append(conn)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        for conn in self.held:
            conn.close()
        self.server.close()


@pytest.fixture
def silent(fake_lo, monkeypatch):
    tmp_path, run = fake_lo
    fake = _Silent()
    monkeypatch.setenv(render_engine.URL_SETTING, f"http://127.0.0.1:{fake.port}")
    render_engine.reset_backend()
    yield tmp_path, run, fake
    render_engine.reset_backend()
    fake.close()


def test_a_collabora_that_accepts_and_never_answers_costs_one_short_wait(silent, monkeypatch):
    tmp_path, run, fake = silent
    monkeypatch.setattr(render_engine, "COLLABORA_RUN_S", 8.0)
    monkeypatch.setattr(render_engine, "MIN_FALLBACK_S", 1.0)
    monkeypatch.setattr(render_engine, "FIRST_ANSWER_S", 0.5, raising=False)
    source = _docx(tmp_path)
    began = time.monotonic()
    out = _pdf(tmp_path, source)
    assert Path(out).read_bytes() == b"made" and fake.posts == 1
    assert time.monotonic() - began < 4, "the fallback waited out the whole share"
    # Down for the cooldown at once, for every event loop of the process:
    # the probe's own short memory running out does not send another call.
    render_engine._probe.at -= render_engine.PROBE_TTL_DOWN_S + 1
    began = time.monotonic()
    out = _pdf(tmp_path, source)
    assert Path(out).read_bytes() == b"made" and fake.posts == 1
    assert time.monotonic() - began < 1


def test_a_cancelled_conversion_removes_its_output_and_leaves_collabora_alone(cool):
    tmp_path, run, fake = cool
    fake.delay = 5
    source = _docx(tmp_path)

    async def scenario():
        task = asyncio.create_task(render_engine.to_pdf(str(source), out_dir=str(tmp_path / "out")))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if fake.in_flight:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return render_engine._state().busy_until

    assert asyncio.run(scenario()) > time.monotonic()
    assert not (tmp_path / "out" / "report.pdf").exists()


def test_one_conversion_at_a_time_the_rest_render_with_libreoffice(cool, monkeypatch):
    tmp_path, run, fake = cool
    monkeypatch.setattr(render_engine, "COLLABORA_QUEUE_WAIT_S", 0.2)
    fake.delay = 1.0
    sources = [_docx(tmp_path, f"r{i}.docx") for i in range(3)]

    async def scenario():
        return await asyncio.gather(*(render_engine.to_pdf(str(s), out_dir=str(tmp_path / "out"))
                                      for s in sources))

    outs = asyncio.run(scenario())
    made_by_collabora = [o for o in outs if Path(o).read_bytes().startswith(b"%PDF")]
    assert fake.most_in_flight == 1 and len(made_by_collabora) == 1


def test_each_event_loop_has_its_own_state(cool):
    tmp_path, _, fake = cool
    source = _docx(tmp_path)
    for _ in range(2):
        out = _pdf(tmp_path, source)
        assert Path(out).read_bytes().startswith(b"%PDF")
    assert len(fake.posts()) == 2


@pytest.mark.parametrize("name, members", [
    ("deck.odp", {}),
    ("report.docx", {"mimetype": "application/vnd.oasis.opendocument.text"}),
    ("report.docx", {"content.xml": "<office:document-content/>"}),
])
def test_only_an_office_open_xml_package_goes_to_collabora(cool, name, members):
    tmp_path, run, fake = cool
    out = _pdf(tmp_path, _docx(tmp_path, name, members))
    assert Path(out).read_bytes() == b"made" and not fake.posts()


def test_a_file_that_is_not_a_package_or_too_big_or_a_link_stays_here(cool, monkeypatch):
    tmp_path, run, fake = cool
    plain = tmp_path / "in" / "plain.docx"
    plain.write_bytes(b"{\\rtf1 not a zip}")
    _pdf(tmp_path, plain)
    big = _docx(tmp_path, "big.docx", {"word/media/x.bin": "x" * 5000})
    monkeypatch.setattr(render_engine, "SEND_MAX_BYTES", 1000)
    _pdf(tmp_path, big)
    monkeypatch.setattr(render_engine, "SEND_MAX_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(render_engine, "SEND_MAX_UNCOMPRESSED", 1000)
    _pdf(tmp_path, big)
    monkeypatch.setattr(render_engine, "SEND_MAX_UNCOMPRESSED", 256 * 1024 * 1024)
    link = tmp_path / "in" / "link.docx"
    link.symlink_to(_docx(tmp_path, "target.docx"))
    _pdf(tmp_path, link)
    assert not fake.posts()


def _workbook(path: Path, *, cached: bool) -> Path:
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData><row r="1"><c r="A1"><v>2</v></c>'
             f'<c r="B1"><f>A1*3</f>{"<v>99</v>" if cached else "<v></v>"}</c></row></sheetData>'
             '<pageMargins left="1" right="1" top="1" bottom="1" header="0.5" footer="0.5"/></worksheet>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                    'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


def _sent_sheet(post) -> bytes:
    body = post.body_seen
    start = body.index(b"PK\x03\x04")
    end = body.rindex(b"\r\n--")
    with zipfile.ZipFile(io.BytesIO(body[start:end])) as zf:
        return zf.read("xl/worksheets/sheet1.xml")


def test_a_recalculation_sends_collabora_a_copy_without_cached_values(cool):
    tmp_path, run, fake = cool
    fake.convert = (200, b"PK\x03\x04 computed")
    source = _workbook(tmp_path / "in" / "budget.xlsx", cached=True)
    out = asyncio.run(render_engine.recalc(str(source), out_dir=str(tmp_path / "out")))
    assert out == str(tmp_path / "out" / "budget.xlsx") and not run.argv
    sent = _sent_sheet(fake.posts()[0])
    assert b"<f>A1*3</f></c>" in sent and b"<v>2</v>" in sent and b"99" not in sent
    assert b"paperSize" not in sent
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["budget.xlsx"]
    assert b"<v>99</v>" in zipfile.ZipFile(source).read("xl/worksheets/sheet1.xml")


def test_a_workbook_with_no_cached_values_goes_as_it_is(cool):
    tmp_path, run, fake = cool
    fake.convert = (200, b"PK\x03\x04 computed")
    source = _workbook(tmp_path / "in" / "staged.xlsx", cached=False)
    asyncio.run(render_engine.recalc(str(source), out_dir=str(tmp_path / "out"), cached_values=False))
    body = fake.posts()[0].body_seen
    assert source.read_bytes() in body


def test_a_workbook_rendered_by_collabora_gets_a4_in_its_copy(cool):
    tmp_path, run, fake = cool
    source = _workbook(tmp_path / "in" / "sheet.xlsx", cached=True)
    _pdf(tmp_path, source)
    sent = _sent_sheet(fake.posts()[0])
    assert b'<pageSetup paperSize="9"/></worksheet>' in sent and b"<v>99</v>" in sent


def test_a_workbook_copy_that_fails_renders_with_libreoffice(cool):
    tmp_path, run, fake = cool
    source = tmp_path / "in" / "odd.xlsx"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
    out = asyncio.run(render_engine.recalc(str(source), out_dir=str(tmp_path / "out")))
    assert Path(out).read_bytes() == b"made" and not fake.posts()


def test_the_client_ignores_proxies_from_the_environment_and_redirects():
    client = render_engine._client(1)
    try:
        assert client._trust_env is False and client.follow_redirects is False
        assert client.timeout.read is None and client.timeout.connect == 1
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize("value, base", [
    ("", ""), ("ftp://collabora", ""), ("collabora:9980", ""), ("http://collabora:9980/collabora?x=1", ""),
    ("http://collabora:9980/collabora/", "http://collabora:9980/collabora"),
    ("https://cool.example.com", "https://cool.example.com"),
])
def test_the_setting_is_an_http_address(monkeypatch, value, base):
    monkeypatch.setenv(render_engine.URL_SETTING, value)
    render_engine.reset_backend()
    try:
        assert render_engine._base_url() == base
    finally:
        render_engine.reset_backend()


def test_plain_http_outside_the_network_is_warned_and_credentials_never_logged(monkeypatch, caplog):
    monkeypatch.setenv(render_engine.URL_SETTING, "http://user:secret@cool.example.com/collabora")
    render_engine.reset_backend()
    try:
        with caplog.at_level("WARNING", logger="file-tools"):
            assert render_engine._base_url()
        assert "plain http" in caplog.text and "secret" not in caplog.text
    finally:
        render_engine.reset_backend()


@pytest.mark.parametrize("text, expected", [
    ("Collabora_Office/26.04.5.1$Linux_X86_64 LibreOffice_project/22ce52f",
     (render_engine.Engine.COLLABORA, "26.04")),
    ("LibreOffice/7.4.7.2$Linux_X86_64 LibreOffice_project/40$Build-2", (render_engine.Engine.LIBREOFFICE, "7.4")),
    ("LibreOffice/25.2.3.2$Linux_AARCH64", (render_engine.Engine.LIBREOFFICE, "25.2")),
    ("Microsoft Excel", (None, "")), ("", (None, "")), (None, (None, "")),
    ("LibreOffice_project/7.4", (None, "")), ("Collabora_Office/", (render_engine.Engine.COLLABORA, "")),
])
def test_the_engine_is_read_from_the_application_name(text, expected):
    assert render_engine.engine_of_application(text) == expected


LINKED = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
          'Target="http://10.0.0.5/theme.png" TargetMode="External"/></Relationships>')


def test_neither_engine_sees_a_linked_picture(cool):
    tmp_path, run, fake = cool
    source = _docx(tmp_path, "deck.pptx", {"ppt/theme/_rels/theme1.xml.rels": LINKED})
    out = _pdf(tmp_path, source)
    assert out == str(tmp_path / "out" / "deck.pdf")
    body = fake.posts()[0].body_seen
    assert b"10.0.0.5" not in body and b"theme1.xml.rels" in body
    fake.convert = (503, b"down")
    render_engine._probe.at -= render_engine.PROBE_TTL_UP_S + 1
    out = _pdf(tmp_path, source)
    sent = Path(run.argv[-1])
    assert sent.parent.name.startswith("unlinked-") and sent.parent.parent == tmp_path / "out"
    assert out == str(tmp_path / "out" / "deck.pdf") and not sent.parent.exists()


def test_a_linked_picture_is_removed_for_libreoffice_too(fake_lo):
    tmp_path, run = fake_lo
    source = _docx(tmp_path, "deck.pptx", {"ppt/theme/_rels/theme1.xml.rels": LINKED})
    seen = {}
    real = render_engine._libreoffice_convert

    async def recording(path, *args, **kw):
        with zipfile.ZipFile(path) as zf:
            seen["rels"] = zf.read("ppt/theme/_rels/theme1.xml.rels")
        return await real(path, *args, **kw)

    shared_convert = render_engine._libreoffice_convert
    try:
        render_engine._libreoffice_convert = recording
        out = _pdf(tmp_path, source)
    finally:
        render_engine._libreoffice_convert = shared_convert
    assert b"10.0.0.5" not in seen["rels"] and out == str(tmp_path / "out" / "deck.pdf")


def test_a_package_that_stays_inside_needs_no_copy_and_a_hyperlink_gets_one(fake_lo):
    tmp_path, run = fake_lo
    inside = LINKED.replace('Target="http://10.0.0.5/theme.png" TargetMode="External"', 'Target="media/a.png"')
    source = _docx(tmp_path, "r.docx", {"word/_rels/document.xml.rels": inside})
    _pdf(tmp_path, source)
    assert run.argv[-1] == str(source)
    hyperlink = LINKED.replace("relationships/image", "relationships/hyperlink")
    linked = _docx(tmp_path, "h.docx", {"word/_rels/document.xml.rels": hyperlink})
    _pdf(tmp_path, linked)
    assert Path(run.argv[-1]).parent.name.startswith("unlinked-")


def test_a_document_with_a_part_in_another_format_stays_with_libreoffice(cool):
    tmp_path, run, fake = cool
    chunk = LINKED.replace("relationships/image", "relationships/aFChunk").replace(
        'Target="http://10.0.0.5/theme.png" TargetMode="External"', 'Target="afchunk.htm"')
    source = _docx(tmp_path, "r.docx", {"word/_rels/document.xml.rels": chunk, "word/afchunk.htm": "<p>x</p>"})
    out = _pdf(tmp_path, source)
    assert Path(out).read_bytes() == b"made" and run.argv[-1] == str(source) and not fake.posts()


def test_a_package_whose_links_cannot_be_removed_is_not_rendered(fake_lo):
    tmp_path, run = fake_lo
    broken = LINKED.replace("</Relationships>", "<Relationship")
    source = _docx(tmp_path, "r.docx", {"word/_rels/document.xml.rels": broken})
    with pytest.raises(RuntimeError, match="links to content outside it"):
        _pdf(tmp_path, source)
    assert not run.argv


@pytest.mark.office
def test_the_image_engine_fetches_nothing_a_document_links(tmp_path, monkeypatch):
    """LibreOffice 7.4 fetches a theme fill's linked picture and a workbook
    drawing's linked picture whatever its profile says (the positive
    control, straight to LibreOffice); through render_engine it gets a copy
    without them, and nothing a probe names is requested."""
    import engine_probes

    monkeypatch.delenv(render_engine.URL_SETTING, raising=False)
    render_engine.reset_backend()
    listener = engine_probes.Listener()
    base = listener.url("127.0.0.1")
    for folder in ("in", "control", "out"):
        (tmp_path / folder).mkdir()
    try:
        deck = engine_probes.theme_picture_deck(tmp_path / "in" / "deck.pptx", base)
        book = engine_probes.drawing_picture_book(tmp_path / "in" / "book.xlsx", base)
        asyncio.run(render_engine._libreoffice_convert(str(deck), "pdf", str(tmp_path / "control")))
        asyncio.run(render_engine._libreoffice_convert(str(book), render_engine.XLSX_FILTER, str(tmp_path / "control"),
                                                kind=render_engine.LO_RECALC, extension="xlsx"))
        time.sleep(1)
        assert any("theme-picture" in hit for hit in listener.hits), listener.hits
        assert any("drawing-picture" in hit for hit in listener.hits), listener.hits
        listener.hits.clear()
        probes = [deck, book, engine_probes.chunk_document(tmp_path / "in" / "chunk.docx", base),
                  engine_probes.function_book(tmp_path / "in" / "functions.xlsx", base),
                  engine_probes.field_document(tmp_path / "in" / "fields.docx", base)]
        for probe in probes:
            pdf = asyncio.run(render_engine.to_pdf(str(probe), out_dir=str(tmp_path / "out")))
            assert engine_probes.CANARY.encode() not in Path(pdf).read_bytes()
            if probe.suffix == ".xlsx":
                again = asyncio.run(render_engine.recalc(str(probe), out_dir=str(tmp_path / "out")))
                with zipfile.ZipFile(again) as zf:
                    assert not any(engine_probes.CANARY.encode() in zf.read(n) for n in zf.namelist())
        time.sleep(1)
        assert listener.hits == []
    finally:
        listener.close()


def _theme_variant(source: Path, target: Path, change) -> Path:
    """``source`` with its theme's relationships part changed by ``change``."""
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "ppt/theme/_rels/theme1.xml.rels":
                data = change(data)
            zout.writestr(item.filename, data)
    return target


THEME_VARIANTS = {
    "a character reference in TargetMode": lambda d: d.replace(b'TargetMode="External"', b'TargetMode="&#69;xternal"'),
    "the hyperlink type on a picture": lambda d: d.replace(b"relationships/image", b"relationships/hyperlink"),
    "a bracket in the target": lambda d: d.replace(b'theme-picture.png"', b'theme-picture.png?a>b"'),
}


@pytest.mark.office
@pytest.mark.parametrize("variant", [*THEME_VARIANTS, "named .ppt", "named .odp"])
def test_no_disguise_of_a_linked_picture_gets_it_fetched(tmp_path, monkeypatch, variant):
    """The ways round 1's copy was bypassed: each variant of the theme's
    linked picture reaches the listener straight from LibreOffice, and
    nothing through render_engine."""
    import shutil

    import engine_probes

    monkeypatch.delenv(render_engine.URL_SETTING, raising=False)
    render_engine.reset_backend()
    listener = engine_probes.Listener()
    for folder in ("in", "control", "out"):
        (tmp_path / folder).mkdir()
    try:
        deck = engine_probes.theme_picture_deck(tmp_path / "in" / "plain.pptx", listener.url("127.0.0.1"))
        if variant in THEME_VARIANTS:
            probe = _theme_variant(deck, tmp_path / "in" / "deck.pptx", THEME_VARIANTS[variant])
        else:
            probe = tmp_path / "in" / f"deck{variant[-4:]}"
            shutil.copy(deck, probe)
        asyncio.run(render_engine._libreoffice_convert(str(probe), "pdf", str(tmp_path / "control")))
        time.sleep(1.5)
        assert any("theme-picture" in hit for hit in listener.hits), listener.hits
        listener.hits.clear()
        asyncio.run(render_engine.to_pdf(str(probe), out_dir=str(tmp_path / "out")))
        time.sleep(1.5)
        assert listener.hits == []
    finally:
        listener.close()


@pytest.mark.office
def test_a_document_cannot_pull_a_server_file_into_its_rendering(tmp_path, monkeypatch):
    """A picture relationship (or a hyperlink-typed one a picture uses) aimed
    at a file:// path: straight LibreOffice draws the file into the PDF;
    through render_engine it draws nothing."""
    import engine_probes
    from PIL import Image

    monkeypatch.delenv(render_engine.URL_SETTING, raising=False)
    render_engine.reset_backend()
    for folder in ("in", "control", "out"):
        (tmp_path / folder).mkdir()
    secret = tmp_path / "secret.png"
    Image.new("RGB", (64, 64), (255, 0, 255)).save(secret)
    deck = engine_probes.theme_picture_deck(tmp_path / "in" / "plain.pptx", "http://127.0.0.1:9")
    url = f"file://{secret}".encode()

    def to_file(data: bytes) -> bytes:
        return data.replace(b"http://127.0.0.1:9/theme-picture.png", url)

    image_deck = _theme_variant(deck, tmp_path / "in" / "image.pptx", to_file)
    hyperlink_deck = _theme_variant(deck, tmp_path / "in" / "hyper.pptx", lambda d: to_file(d).replace(
        b"relationships/image", b"relationships/hyperlink"))

    def has_image(pdf: str) -> bool:
        data = Path(pdf).read_bytes()
        return b"/Subtype/Image" in data or b"/Subtype /Image" in data

    assert has_image(asyncio.run(render_engine._libreoffice_convert(str(image_deck), "pdf",
                                                                     str(tmp_path / "control"))))
    for probe in (image_deck, hyperlink_deck):
        out = tmp_path / "out" / probe.stem
        out.mkdir()
        assert not has_image(asyncio.run(render_engine.to_pdf(str(probe), out_dir=str(out)))), probe.name


# ---------------------------------------------------------------- one place

BACKEND_NAMES = frozenset({
    "_libreoffice_convert", "_libreoffice_run", "_libreoffice_profile", "_lo_free_slots", "_lo_slot_count",
    "_lo_runs", "_lo_free", "_LO_PROFILE_DIR", "_LO_REGISTRY", "_LO_REGISTRIES", "LO_CONVERT", "LO_RECALC",
    "_end_process_group", "_end_and_free", "_oom_first",
})
ENGINE_PROGRAMS = frozenset({"libreoffice", "soffice", "soffice.bin", "lowriter", "localc", "loimpress"})


_ENGINE_WORDS = re.compile(r"\b(soffice(\.bin)?|libreoffice|lowriter|localc|loimpress|unoconv|unoserver)\b|convert-to")
_UNO_MODULES = ("uno", "unohelper", "com.sun.star")


def _backend_uses(source: str) -> list[tuple[int, str]]:
    """Where ``source`` reaches the LibreOffice backend's names (as a name,
    an attribute, an import or a string, as getattr takes one), names an
    office program or Collabora's convert-to in any string that is not a
    docstring or a bare comment string, or imports the office API."""
    import ast

    tree = ast.parse(source)
    bare = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in BACKEND_NAMES:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in BACKEND_NAMES:
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.ImportFrom):
            found += [(node.lineno, alias.name) for alias in node.names if alias.name in BACKEND_NAMES]
            if (node.module or "").startswith(_UNO_MODULES):
                found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names if alias.name.startswith(_UNO_MODULES)]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in bare:
            if node.value in BACKEND_NAMES or _ENGINE_WORDS.search(node.value):
                found.append((node.lineno, node.value[:60]))
    return found


def test_the_scan_finds_each_way_to_the_engines():
    for source in (
        "from shared import _libreoffice_convert\n",
        "import render_engine\nrender_engine._libreoffice_convert(p, 'pdf', d)\n",
        "asyncio.create_subprocess_exec('soffice', '--headless')\n",
        "subprocess.run(['/usr/bin/libreoffice', '--convert-to', 'pdf'])\n",
        "httpx.post(base + '/cool/convert-to/pdf')\n",
        "PROG = 'soffice'\nsubprocess.run([PROG, '--headless'])\n",
        "ARGS = ['lowriter', '--headless']\nsubprocess.run(ARGS)\n",
        "os.system('libreoffice --headless --cat x')\n",
        "getattr(render_engine, '_libreoffice_convert')(p, 'pdf', d)\n",
        "import uno\n",
        "from com.sun.star.beans import PropertyValue\n",
        "subprocess.run(['unoconv', '-f', 'pdf', p])\n",
        "PATH = '/cool/convert-to/'\n",
    ):
        assert _backend_uses(source), source
    assert not _backend_uses('"""LibreOffice renders it: soffice."""\nrender_engine.to_pdf(p, out_dir=d)\n')
    assert not _backend_uses('def f():\n    """Through libreoffice, never directly."""\n    return "LibreOffice"\n')


def test_every_conversion_goes_through_render_engine():
    """Which engine converts a file (Collabora or LibreOffice) is decided in
    render_engine alone: no other shipped module reaches the LibreOffice
    backend, starts an office program or calls convert-to."""
    root = Path(render_engine.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[0] in ("__tests__", "venv", ".venv") or relative == Path("render_engine.py"):
            continue
        offenders += [f"{relative}:{line} {what}" for line, what in _backend_uses(path.read_text(encoding="utf-8"))]
    assert offenders == []
    assert not [name for name in BACKEND_NAMES if hasattr(shared, name)]
    assert {"_libreoffice_convert", "LO_CONVERT", "LO_RECALC"} <= set(vars(render_engine))


def _convert(tmp_path, source: Path, fmt: str) -> str:
    return asyncio.run(render_engine.convert(str(source), fmt, out_dir=str(tmp_path / "out")))


def test_a_conversion_to_another_office_format_goes_to_collabora(cool):
    tmp_path, run, fake = cool
    fake.convert = (200, b"PK\x03\x04 odt")
    out = _convert(tmp_path, _docx(tmp_path), "odt")
    assert out == str(tmp_path / "out" / "report.odt") and not run.argv
    assert str(fake.posts()[0].url) == BASE + "/cool/convert-to/odt"


@pytest.mark.parametrize("fmt, answer", [("txt", b"plain text"), ("png", b"\x89PNG page"), ("csv", b"a,b\n")])
def test_a_text_or_picture_format_needs_only_an_answer_of_its_kind(cool, fmt, answer):
    tmp_path, run, fake = cool
    fake.convert = (200, answer)
    out = _convert(tmp_path, _docx(tmp_path), fmt)
    assert Path(out).read_bytes() == answer and not run.argv


@pytest.mark.parametrize("answer", [(200, b""), (200, b"<html>error</html>")])
def test_an_empty_or_wrong_answer_falls_back(cool, answer):
    tmp_path, run, fake = cool
    fake.convert = answer
    out = _convert(tmp_path, _docx(tmp_path), "png" if answer[1] else "txt")
    assert Path(out).read_bytes() == b"made" and run.argv


@pytest.mark.parametrize("fmt, lo_format, made", [
    ("epub", "epub", "report.epub"),
    ("txt:Text (encoded):UTF8", "txt:Text (encoded):UTF8", "report.txt"),
    ("csv", "csv:Text - txt - csv (StarCalc):44,34,76", "report.csv"),
])
def test_libreoffice_makes_the_rest_and_names_its_output_by_extension(fake_lo, fmt, lo_format, made):
    tmp_path, run = fake_lo
    out = _convert(tmp_path, _docx(tmp_path), fmt)
    assert run.argv[run.argv.index("--convert-to") + 1] == lo_format
    assert out == str(tmp_path / "out" / made)


def test_only_listed_formats_and_no_filter_go_to_collabora(cool):
    tmp_path, run, fake = cool
    source = _docx(tmp_path)
    for fmt in ("epub", "svg", "txt:Text (encoded):UTF8"):
        _convert(tmp_path, source, fmt)
    assert not fake.posts() and render_engine.COLLABORA_FORMATS >= {"pdf", "docx", "xlsx", "pptx", "csv", "html"}


# ---------------------------------------------------------------- the parent's look at a package


def _linked_deck(tmp_path, name: str, rels: str) -> Path:
    return _docx(tmp_path, name, {"ppt/theme/_rels/theme1.xml.rels": rels,
                                  "ppt/theme/theme1.xml": '<a:theme xmlns:a="http://schemas.openxmlformats.org/'
                                  'drawingml/2006/main"/>'})


@pytest.mark.parametrize("name, rels", [
    ("deck.pptx", LINKED.replace('TargetMode="External"', 'TargetMode="&#69;xternal"')),
    ("deck.pptx", LINKED.replace("relationships/image", "relationships/hyperlink")),
    ("deck.pptx", LINKED.replace('theme.png"', 'theme.png?a>b"')),
    ("deck.ppt", LINKED),
    ("deck.odp", LINKED),
    ("deck.pptx", LINKED.replace('http://10.0.0.5/theme.png" TargetMode="External"', 'file:///etc/hostname"')),
])
def test_every_way_a_link_was_hidden_from_the_scan_now_gets_the_copy(fake_lo, name, rels):
    tmp_path, run = fake_lo
    source = _linked_deck(tmp_path, name, rels)
    seen = {}
    real = render_engine._libreoffice_convert

    async def recording(path, *args, **kw):
        with zipfile.ZipFile(path) as zf:
            seen["rels"] = zf.read("ppt/theme/_rels/theme1.xml.rels")
        return await real(path, *args, **kw)

    render_engine._libreoffice_convert = recording
    try:
        _pdf(tmp_path, source)
    finally:
        render_engine._libreoffice_convert = real
    assert b"10.0.0.5" not in seen["rels"] and b"/etc/hostname" not in seen["rels"]
    assert b'Id="rId1"' not in seen["rels"]


def _zip64_tail(path: Path) -> None:
    """``path`` with a zip64 end locator before its end record, as a
    directory past the 32-bit fields would have."""
    import struct

    data = path.read_bytes()
    eocd = data.rfind(b"PK\x05\x06")
    record = struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, 400_000, 400_000, 30_000_000, 0)
    locator = struct.pack("<IIQI", 0x07064B50, 0, eocd, 1)
    path.write_bytes(data[:eocd] + record + locator + data[eocd:])


def test_a_package_the_parent_cannot_bound_goes_to_the_worker_unread(fake_lo, monkeypatch):
    tmp_path, run = fake_lo
    zip64 = _docx(tmp_path, "big.docx")
    _zip64_tail(zip64)
    large = _docx(tmp_path, "large.docx")

    def no_zip(*_args, **_kw):
        raise AssertionError("the parent parsed a package past its bounds")

    monkeypatch.setattr(render_engine.zipfile, "ZipFile", no_zip)
    assert render_engine._links(str(zip64)).outside
    monkeypatch.setattr(render_engine, "SCAN_MAX_FILE_BYTES", 10)
    assert render_engine._links(str(large)).outside


def test_a_broken_package_is_not_rendered_unseen(fake_lo):
    tmp_path, run = fake_lo
    good = _docx(tmp_path, "good.docx")
    broken = tmp_path / "in" / "broken.docx"
    broken.write_bytes(good.read_bytes().replace(b"PK\x01\x02", b"XX\x01\x02"))
    assert render_engine._links(str(broken)).outside
    with pytest.raises(RuntimeError, match="Save the file again"):
        _pdf(tmp_path, broken)
    assert not run.argv



def test_a_collabora_failing_every_load_is_left_alone(cool):
    tmp_path, run, fake = cool
    source = _docx(tmp_path)
    fake.convert = (500, b"faileddocloading")
    _pdf(tmp_path, source)
    fake.convert = (200, b"%PDF-1.7 ok")
    _pdf(tmp_path, source)
    fake.convert = (500, b"faileddocloading")
    for _ in range(render_engine.FAILED_LOADS_DOWN):
        _pdf(tmp_path, source)
    posts = len(fake.posts())
    fake.convert = (200, b"%PDF-1.7 again")
    out = _pdf(tmp_path, source)
    assert posts == 2 + render_engine.FAILED_LOADS_DOWN and len(fake.posts()) == posts
    assert Path(out).read_bytes() == b"made"


def test_a_read_timeout_of_the_client_is_a_timeout(cool, monkeypatch):
    import httpx

    tmp_path, run, fake = cool
    monkeypatch.setattr(render_engine, "MIN_FALLBACK_S", 0.0)

    async def stalls(request):
        if request.url.path.endswith("/hosting/capabilities"):
            return httpx.Response(200, content=json.dumps(CAPABILITIES).encode())
        raise httpx.ReadTimeout("the answer stopped", request=request)

    fake.handle = stalls
    out = _pdf(tmp_path, _docx(tmp_path))
    assert Path(out).read_bytes() == b"made" and render_engine._probe.up is False


def test_a_file_collabora_never_gets_costs_no_probe(cool):
    tmp_path, run, fake = cool
    fake.probe = (500, b"down")
    odt = tmp_path / "in" / "notes.odt"
    with zipfile.ZipFile(odt, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", "<office:document-content/>")
    _convert(tmp_path, odt, "pdf")
    plain = tmp_path / "in" / "data.csv"
    plain.write_text("a,b\n")
    _convert(tmp_path, plain, "pdf")
    assert fake.requests == []


def test_a_workbook_converted_to_another_workbook_keeps_its_page_setup(cool):
    tmp_path, run, fake = cool
    fake.convert = (200, b"PK\x03\x04 ods")
    source = _workbook(tmp_path / "in" / "sheet.xlsx", cached=True)
    _convert(tmp_path, source, "ods")
    assert source.read_bytes() in fake.posts()[0].body_seen
    fake.convert = (200, b"%PDF-1.7 pdf")
    _convert(tmp_path, source, "pdf")
    assert b'paperSize="9"' in _sent_sheet(fake.posts()[1])
