"""Every output of a write tool lands beneath the mount through
``shared.safe_open_write``: a link at the output name is
replaced, never written through; a link at any component refuses the write;
the temps a failed worker leaves are removed by the parent. The suite runs
the cores inline (conftest) with the write root pointed at ``tmp_path``.
"""

import asyncio
import contextlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import shared


async def _ident(p, writing=False, **kw):
    return p


@pytest.fixture
def victim(tmp_path):
    """A folder beside the writable tree: what a redirected write would reach."""
    v = tmp_path.parent / f"victim-{tmp_path.name}"
    v.mkdir(exist_ok=True)
    (v / "agent.md").write_bytes(b"ORIGINAL")
    yield v
    for p in v.iterdir():
        p.unlink()
    v.rmdir()


def _untouched(victim):
    assert (victim / "agent.md").read_bytes() == b"ORIGINAL"
    assert sorted(p.name for p in victim.iterdir()) == ["agent.md"]


def test_safe_open_write_replaces_a_leaf_link_and_refuses_a_link_component(tmp_path, victim):
    out = tmp_path / "out.bin"
    out.symlink_to(victim / "agent.md")
    with shared.safe_open_write(str(out)) as fh:
        fh.write(b"NEW")
    _untouched(victim)
    assert not out.is_symlink() and out.read_bytes() == b"NEW"
    (tmp_path / "lnk").symlink_to(victim)
    with pytest.raises(PermissionError):
        with shared.safe_open_write(str(tmp_path / "lnk" / "agent.md")) as fh:
            fh.write(b"NEW")
    _untouched(victim)
    with pytest.raises(PermissionError):
        with shared.safe_open_write(str(victim / "agent.md")) as fh:  # outside the root
            fh.write(b"NEW")
    _untouched(victim)


def test_safe_mkdirs_and_cleanup_partials(tmp_path, victim):
    shared.safe_mkdirs(str(tmp_path / "a" / "b"))
    assert (tmp_path / "a" / "b").is_dir()
    (tmp_path / "lnk").symlink_to(victim)
    with pytest.raises(PermissionError):
        shared.safe_mkdirs(str(tmp_path / "lnk" / "deeper"))
    _untouched(victim)
    target = tmp_path / "report.pdf"
    (tmp_path / ".report.pdf.0123456789ab.partial").write_bytes(b"x")
    (tmp_path / ".other.pdf.0123456789ab.partial").write_bytes(b"x")
    (tmp_path / ".report.pdf.bak.0123456789ab.partial").write_bytes(b"x")
    shared.cleanup_partials(str(target))
    assert not (tmp_path / ".report.pdf.0123456789ab.partial").exists()
    assert (tmp_path / ".other.pdf.0123456789ab.partial").exists()
    assert (tmp_path / ".report.pdf.bak.0123456789ab.partial").exists()
    # A name over 200 bytes: the temp carries the name cut to 200 bytes,
    # inside a multibyte character too.
    for name in ("x" * 240, "文" * 80):
        partial = b"." + name.encode()[:200] + b".0123456789ab.partial"
        os.close(os.open(os.path.join(os.fsencode(tmp_path), partial), os.O_CREAT | os.O_WRONLY))
        shared.cleanup_partials(str(tmp_path / name))
        assert not os.path.lexists(os.path.join(os.fsencode(tmp_path), partial))


def _bypass(monkeypatch, *mods):
    async def _noop(*a, **k):
        return None
    for mod in mods:
        monkeypatch.setattr(mod, "_resolve_path", _ident)
        for name in ("_push_preview", "_push_image_preview", "_notify_file_written"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, _noop)
    monkeypatch.setattr(shared, "_resolve_path", _ident)


def test_write_docx_xlsx_pptx_never_write_through_a_link(tmp_path, victim, monkeypatch):
    import excel
    import powerpoint
    import word
    _bypass(monkeypatch, word, excel, powerpoint)
    for _mod, handler, name in (
        (word, word.handle_write_docx, "out.docx"),
        (excel, excel.handle_write_xlsx, "out.xlsx"),
        (powerpoint, powerpoint.handle_write_pptx, "out.pptx"),
    ):
        out = tmp_path / name
        out.symlink_to(victim / "agent.md")
        ops = [{"type": "add_heading", "text": "t"}] if name != "out.xlsx" else \
            [{"op": "write", "sheet": "Sheet1", "cell": "A1", "value": "x"}]
        if name == "out.pptx":
            ops = [{"type": "add_slide", "layout": "title", "title": "t"}]
        msg = asyncio.run(handler({"path": str(out), "operations": ops, "create_new": True}))
        assert "saved" in msg.lower(), msg
        _untouched(victim)
        assert not out.is_symlink() and out.stat().st_size > 0
        # A link component refuses the write with the helper's sentence.
        (tmp_path / f"lnk-{name}").symlink_to(victim)
        with pytest.raises(Exception, match="Cannot write"):
            asyncio.run(handler({"path": str(tmp_path / f"lnk-{name}" / name),
                                 "operations": ops, "create_new": True}))
        _untouched(victim)


def test_edit_image_and_create_chart_never_write_through_a_link(tmp_path, victim, monkeypatch):
    from PIL import Image

    import charts
    import images
    _bypass(monkeypatch, images, charts)
    src = tmp_path / "in.png"
    Image.new("RGB", (8, 8), "red").save(src)
    out = tmp_path / "out.png"
    out.symlink_to(victim / "agent.md")
    msg = asyncio.run(images.handle_edit_image({
        "path": str(src), "output_path": str(out),
        "operations": [{"type": "resize", "width": 4, "height": 4}],
    }))
    assert "error" not in msg.lower(), msg
    _untouched(victim)
    assert not out.is_symlink() and Image.open(out).size == (4, 4)
    chart = tmp_path / "chart.png"
    chart.symlink_to(victim / "agent.md")
    msg = asyncio.run(charts.handle_create_chart({
        "chart_type": "bar", "series": [{"name": "s", "values": [1, 2]}],
        "categories": ["a", "b"], "save_path": str(chart),
    }))
    assert "Saved to" in msg, msg
    _untouched(victim)
    assert not chart.is_symlink() and chart.stat().st_size > 0


def test_pdf_sinks_never_write_through_a_link(tmp_path, victim, monkeypatch):
    import fitz
    from PIL import Image

    import pdf as pdf_mod
    _bypass(monkeypatch, pdf_mod)
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)
    # write_pdf
    out = tmp_path / "out.pdf"
    out.symlink_to(victim / "agent.md")
    asyncio.run(pdf_mod.handle_write_pdf({"path": str(out), "content": "# hi"}))
    _untouched(victim)
    assert not out.is_symlink() and out.read_bytes().startswith(b"%PDF")
    # images_to_pdf
    img = tmp_path / "i.png"
    Image.new("RGB", (8, 8), "blue").save(img)
    out2 = tmp_path / "imgs.pdf"
    out2.symlink_to(victim / "agent.md")
    asyncio.run(pdf_mod.handle_images_to_pdf({"images": [str(img)], "output_path": str(out2)}))
    _untouched(victim)
    assert not out2.is_symlink() and out2.read_bytes().startswith(b"%PDF")
    # edit_pdf: the save tail, a split and an image extraction
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(fitz.Point(72, 100), "hello", fontsize=12)
    page.insert_image(fitz.Rect(72, 150, 172, 250), filename=str(img))
    src = tmp_path / "src.pdf"
    doc.save(str(src))
    doc.close()
    split_out = tmp_path / "split.pdf"
    split_out.symlink_to(victim / "agent.md")
    (tmp_path / "imgdir").symlink_to(victim)
    msg = asyncio.run(pdf_mod.handle_edit_pdf({"path": str(src), "operations": [
        {"type": "split", "pages": "1", "output_path": str(split_out)},
        {"type": "extract_images", "output_dir": str(tmp_path / "imgdir")},
        {"type": "rotate_page", "pages": "1", "degrees": 90},
    ]}))
    _untouched(victim)
    assert not split_out.is_symlink() and split_out.read_bytes().startswith(b"%PDF")
    assert "extract_images" in msg and "Cannot" in msg  # the linked folder was refused
    assert fitz.open(str(src))[0].rotation == 90
    # pdf_to_images lands the pages beneath the mount
    (tmp_path / "pages").symlink_to(victim)
    with pytest.raises(Exception, match="Cannot"):
        asyncio.run(pdf_mod.handle_pdf_to_images({"path": str(src), "output_dir": str(tmp_path / "pages")}))
    _untouched(victim)
    asyncio.run(pdf_mod.handle_pdf_to_images({"path": str(src), "output_dir": str(tmp_path / "pages2")}))
    assert (tmp_path / "pages2" / "page_001.png").exists()


def test_convert_document_lands_the_result_through_the_helper(tmp_path, victim, monkeypatch):
    """LibreOffice's output is faked in a temp directory; the landing copies it
    beneath the mount through the helper (a link at the name is replaced)."""
    import pdf as pdf_mod
    _bypass(monkeypatch, pdf_mod)
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)
    seen = {}

    async def _fake_convert(input_path, output_format, output_dir=None):
        seen["output_dir"] = output_dir
        produced = Path(output_dir) / (Path(input_path).stem + "." + output_format)
        produced.write_bytes(b"%PDF-FAKE")
        return str(produced)

    monkeypatch.setattr(pdf_mod, "_libreoffice_convert", _fake_convert)
    src = tmp_path / "doc.docx"
    src.write_bytes(b"docx")
    out = tmp_path / "doc.pdf"
    out.symlink_to(victim / "agent.md")
    msg = asyncio.run(pdf_mod.handle_convert_document({"input_path": str(src), "output_format": "pdf"}))
    assert msg.startswith("Converted"), msg
    assert not Path(seen["output_dir"]).is_relative_to(tmp_path)  # written outside the tree
    _untouched(victim)
    assert not out.is_symlink() and out.read_bytes() == b"%PDF-FAKE"


def _sandbox_rig(tmp_path, monkeypatch):
    """A mount at ``tmp_path/agents`` holding ``pa/users/u/workspace/docs``,
    a faked LibreOffice, and a resolver that answers a read as given and a
    write in sandbox form only (``/users/...``), as the proxy does: a
    container path reaches it in its agents-relative form, which it
    resolves for reads only. Returns ``(pdf module, docs dir, writes)``."""
    import fitz
    from PIL import Image

    import pdf as pdf_mod
    mount = tmp_path / "agents"
    docs = mount / "pa/users/u/workspace/docs"
    docs.mkdir(parents=True)
    Image.new("RGB", (8, 8), "green").save(docs / "photo.png")
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "page")
    doc.save(str(docs / "doc.pdf"))
    doc.close()
    (docs / "sheet.docx").write_bytes(b"docx")
    writes = []

    async def _resolve(p, writing=False, **kw):
        if not writing:
            return p
        writes.append(p)
        if not Path(p).is_relative_to("/users/u/workspace"):
            raise ValueError(f"Cannot open '{p}': proxy resolve-path 403")
        return str(mount / "pa") + p

    async def _fake_convert(input_path, output_format, output_dir=None):
        produced = Path(output_dir) / (Path(input_path).stem + "." + output_format)
        produced.write_bytes(b"%PDF-FAKE")
        return str(produced)

    _bypass(monkeypatch, pdf_mod)
    monkeypatch.setattr(pdf_mod, "_resolve_path", _resolve)
    monkeypatch.setattr(shared, "_resolve_path", _resolve)
    monkeypatch.setattr(pdf_mod, "_to_agents_relative", lambda p: p)
    monkeypatch.setattr(pdf_mod, "_libreoffice_convert", _fake_convert)
    monkeypatch.setattr(pdf_mod, "MOUNT_AGENTS_DIR", str(mount))
    return pdf_mod, docs, writes


@pytest.mark.parametrize("name, fmt, landed", [
    ("photo.png", "pdf", "photo.pdf"),
    ("doc.pdf", "png", "doc/page_001.png"),
    ("sheet.docx", "pdf", "sheet.pdf"),
])
def test_a_default_conversion_output_resolves_as_a_sandbox_write(tmp_path, monkeypatch, name, fmt, landed):
    pdf_mod, docs, writes = _sandbox_rig(tmp_path, monkeypatch)
    msg = asyncio.run(pdf_mod.handle_convert_document(
        {"input_path": str(docs / name), "output_format": fmt},
    ))
    assert msg.startswith("Converted"), msg
    assert writes == ["/users/u/workspace/docs/" + landed.split("/")[0]]
    assert (docs / landed).exists()


@pytest.mark.parametrize("name, fmt, output_path, landed", [
    ("photo.png", "pdf", "/users/u/workspace/out/final.pdf", "out/final.pdf"),
    ("doc.pdf", "png", "/users/u/workspace/out/pages", "out/pages/page_001.png"),
    ("sheet.docx", "pdf", "/users/u/workspace/out/s.pdf", "out/s.pdf"),
])
def test_a_conversion_lands_at_output_path_by_its_own_name(
        tmp_path, monkeypatch, name, fmt, output_path, landed):
    pdf_mod, docs, writes = _sandbox_rig(tmp_path, monkeypatch)
    msg = asyncio.run(pdf_mod.handle_convert_document(
        {"input_path": str(docs / name), "output_format": fmt, "output_path": output_path},
    ))
    assert msg.startswith("Converted"), msg
    assert writes == [output_path]
    assert (docs.parent / landed).exists()


def test_pdf_to_images_default_folder_resolves_as_a_sandbox_write(tmp_path, monkeypatch):
    pdf_mod, docs, writes = _sandbox_rig(tmp_path, monkeypatch)
    asyncio.run(pdf_mod.handle_pdf_to_images({"path": str(docs / "doc.pdf")}))
    assert writes == ["/users/u/workspace/docs/doc_pages"]
    assert (docs / "doc_pages" / "page_001.png").exists()


def test_libreoffice_runs_under_the_pinned_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "_LO_PROFILE_DIR", str(tmp_path / "lo-profile"))
    monkeypatch.setattr(shared, "_lo_slot_count", lambda: 1)
    seen = {}
    real_profile = shared._libreoffice_profile
    on_loop = []

    def _profile(slot):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real_profile(slot)

    monkeypatch.setattr(shared, "_libreoffice_profile", _profile)

    class _Proc:
        returncode = 0
        pid = 0

        async def communicate(self):
            return b"", b""

    async def _spawn(*argv, **kw):
        seen["argv"] = argv
        Path(kw.get("cwd") or ".")
        produced = Path(argv[argv.index("--outdir") + 1]) / "in.pdf"
        produced.write_bytes(b"%PDF")
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    (tmp_path / "in.docx").write_bytes(b"x")
    # A profile that drifted (an older pin set, a user's change) is rewritten.
    xcu_path = tmp_path / "lo-profile" / "slot-0" / "user" / "registrymodifications.xcu"
    xcu_path.parent.mkdir(parents=True)
    xcu_path.write_text(shared._LO_REGISTRY.replace("BlockUntrustedRefererLinks", "Other"))
    out = asyncio.run(shared._libreoffice_convert(str(tmp_path / "in.docx"), "pdf", str(tmp_path)))
    assert out.endswith("in.pdf")
    argv = seen["argv"]
    assert argv[0] == "libreoffice" and argv[1] == f"-env:UserInstallation=file://{tmp_path / 'lo-profile' / 'slot-0'}"
    xcu = xcu_path.read_text()
    for needle in ('name="ODFRecalcMode"', 'name="OOXMLRecalcMode"',
                   'Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>1',
                   'Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0',
                   'name="DisableMacrosExecution" oor:op="fuse"><value>true',
                   'name="MacroSecurityLevel" oor:op="fuse"><value>3',
                   'Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks"'
                   ' oor:op="fuse"><value>true'):
        assert needle in xcu, needle
    assert xcu == shared._LO_REGISTRY and xcu.rstrip().endswith("</oor:items>")
    assert on_loop == [False]  # the profile's reads and writes run off the loop
    with contextlib.suppress(OSError):
        os.unlink(tmp_path / "in.pdf")


class _FakeLibreOffice:
    """create_subprocess_exec for LibreOffice: each run records its span, the
    slot profile it ran under and its output folder, and waits on ``gate``."""

    def __init__(self, gate: asyncio.Event | None = None, hang: bool = False):
        self.gate, self.hang = gate, hang
        self.started = asyncio.Event()
        self.running = 0
        self.most = 0
        self.profiles: list[str] = []
        self.procs: list = []

    async def __call__(self, *argv, **kw):
        fake = self
        assert kw.get("start_new_session") is True
        fake.profiles.append(argv[1])

        class _Proc:
            pid = 40000 + len(fake.procs)
            returncode = None

            async def communicate(self):
                fake.running += 1
                fake.most = max(fake.most, fake.running)
                fake.started.set()
                try:
                    if fake.hang:
                        await asyncio.Event().wait()
                    if fake.gate is not None:
                        await fake.gate.wait()
                    await asyncio.sleep(0.01)
                    out = Path(argv[argv.index("--outdir") + 1]) / (Path(argv[-1]).stem + ".pdf")
                    out.write_bytes(b"%PDF")
                    self.returncode = 0
                    return b"", b""
                finally:
                    fake.running -= 1

            async def wait(self):
                self.returncode = -9
                return -9

        proc = _Proc()
        fake.procs.append(proc)
        return proc


@pytest.fixture
def lo_rig(tmp_path, monkeypatch):
    """Two slots, profiles under tmp_path, and os.killpg a recorder (a fake
    pid must never reach a real process group)."""
    monkeypatch.setattr(shared, "_LO_PROFILE_DIR", str(tmp_path / "lo-profile"))
    monkeypatch.setattr(shared, "_lo_slot_count", lambda: 2)
    killed: list = []

    def _killpg(pgid, sig):
        killed.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError(pgid)

    monkeypatch.setattr(shared.os, "killpg", _killpg)
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.docx").write_bytes(b"x")
    return tmp_path, killed


def _convert(tmp_path, name):
    out = tmp_path / f"out-{name}-{len(list(tmp_path.glob('out-*')))}"
    out.mkdir()
    return shared._libreoffice_convert(str(tmp_path / f"{name}.docx"), "pdf", str(out))


def test_two_conversions_run_side_by_side_on_two_slots(lo_rig, monkeypatch):
    tmp_path, _ = lo_rig
    fake = _FakeLibreOffice()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    async def main():
        return await asyncio.gather(_convert(tmp_path, "a"), _convert(tmp_path, "b"), _convert(tmp_path, "a"))

    outs = asyncio.run(main())
    assert all(o.endswith(".pdf") for o in outs)
    assert fake.most == 2  # two at once, the third waited for a free slot
    assert sorted(set(fake.profiles)) == [f"-env:UserInstallation=file://{tmp_path / 'lo-profile' / f'slot-{n}'}"
                                          for n in (0, 1)]


def test_one_slot_runs_one_conversion_at_a_time(lo_rig, monkeypatch):
    tmp_path, _ = lo_rig
    monkeypatch.setattr(shared, "_lo_slot_count", lambda: 1)
    fake = _FakeLibreOffice()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    async def main():
        await asyncio.gather(_convert(tmp_path, "a"), _convert(tmp_path, "b"))

    asyncio.run(main())
    assert fake.most == 1


def test_a_timeout_ends_the_process_group_before_the_slot_is_free(lo_rig, monkeypatch):
    tmp_path, killed = lo_rig
    monkeypatch.setattr(shared, "_LO_TIMEOUT_S", 0.05)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _FakeLibreOffice(hang=True))

    async def main():
        with pytest.raises(RuntimeError, match=r"timed out \(0\.05s\)"):
            await _convert(tmp_path, "a")
        return shared._lo_free_slots().qsize()

    assert asyncio.run(main()) == 2
    assert [sig for _, sig in killed] == [9, 0]


def test_a_caller_that_stops_waiting_ends_the_run(lo_rig, monkeypatch):
    tmp_path, killed = lo_rig
    fake = _FakeLibreOffice(hang=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    async def main():
        call = asyncio.ensure_future(_convert(tmp_path, "a"))
        await fake.started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await _settle()
        return shared._lo_free_slots().qsize()

    assert asyncio.run(main()) == 2
    assert killed == [(fake.procs[0].pid, 9), (fake.procs[0].pid, 0)]


async def _settle():
    """Wait for every run and cleanup in flight, the ones they start too. A
    gather of tasks already done does not yield, and their done callbacks
    (the set's discard) run on the next turn: sleep(0) gives them it."""
    while shared._lo_runs:
        await asyncio.gather(*list(shared._lo_runs), return_exceptions=True)
        await asyncio.sleep(0)


def test_a_stop_while_a_timed_out_group_is_ended_still_frees_the_slot(lo_rig, monkeypatch):
    """The conversion timed out, its group lingers while it is ended, and
    the person stops the call then: the slot comes back once the group is
    gone, and the next conversion runs (on one slot it used to hang)."""
    tmp_path, _ = lo_rig
    monkeypatch.setattr(shared, "_lo_slot_count", lambda: 1)
    monkeypatch.setattr(shared, "_LO_TIMEOUT_S", 0.05)
    polls = []

    def _killpg(pgid, sig):
        if sig == 0:
            polls.append(pgid)
            if len(polls) > 20:  # the group lingers about a second
                raise ProcessLookupError(pgid)

    monkeypatch.setattr(shared.os, "killpg", _killpg)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _FakeLibreOffice(hang=True))

    async def main():
        call = asyncio.ensure_future(_convert(tmp_path, "a"))
        await asyncio.sleep(0.2)  # timed out at 0.05, the group still being ended
        assert polls and not call.done()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await _settle()
        freed = shared._lo_free_slots().qsize()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _FakeLibreOffice())
        nxt = await asyncio.wait_for(_convert(tmp_path, "b"), 2)
        return freed, nxt

    freed, nxt = asyncio.run(main())
    assert freed == 1 and nxt.endswith("b.pdf")
    assert len(polls) == 21  # the slot came back only after the group was gone


def test_a_conversion_that_finds_no_free_slot_says_so(lo_rig, monkeypatch):
    tmp_path, _ = lo_rig
    monkeypatch.setattr(shared, "_LO_SLOT_WAIT_S", 0.1)

    async def main():
        free = shared._lo_free_slots()
        held = [free.get_nowait() for _ in range(free.qsize())]
        with pytest.raises(RuntimeError, match=r"busy: no conversion slot came free within 0\.1s"):
            await _convert(tmp_path, "a")
        for slot in held:
            free.put_nowait(slot)

    asyncio.run(main())


def test_the_output_folder_is_the_callers():
    import inspect

    params = inspect.signature(shared._libreoffice_convert).parameters
    assert params["output_dir"].default is inspect.Parameter.empty
