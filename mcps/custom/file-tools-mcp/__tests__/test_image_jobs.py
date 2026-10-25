"""edit_image and analyze_image run off the shared server's event loop, one
image job at a time: the loop serves other sessions while a large photo is
decoded or edited, two edits of one file apply one after the other, and a
call the client gave up on still holds its turn until its thread ends."""

import asyncio
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import images

pytest.importorskip("PIL")


async def _ident(p, writing=False, **kw):
    return p


async def _nothing(*a, **k):
    return None


@pytest.fixture
def rig(monkeypatch, tmp_path):
    from PIL import Image

    monkeypatch.setenv("FILETOOLS_WRITE_ROOT", str(tmp_path))
    monkeypatch.setattr(images, "_resolve_path", _ident)
    monkeypatch.setattr(images, "_notify_file_written", _nothing)
    monkeypatch.setattr(images, "_push_image_preview", _nothing)
    monkeypatch.setattr(images, "_to_agents_relative", lambda p: p)
    src = tmp_path / "photo.png"
    Image.new("RGB", (40, 30), (90, 120, 200)).save(src)
    return src


def _slow_op(monkeypatch, events: list, seconds: float = 0.3):
    """An operation that holds its thread for a while, recording its span."""

    def _slow(job, op):
        events.append(("slow-start", op.get("tag")))
        time.sleep(seconds)
        events.append(("slow-end", op.get("tag")))

    monkeypatch.setitem(images._HANDLERS, "slow", _slow)


def test_an_edit_leaves_the_loop_free(rig, monkeypatch):
    events: list = []
    _slow_op(monkeypatch, events)

    async def main():
        ticks = 0
        edit = asyncio.ensure_future(images.handle_edit_image(
            {"path": str(rig), "operations": [{"type": "slow"}]}))
        while not edit.done():
            ticks += 1
            await asyncio.sleep(0.01)
        return ticks, edit.result()

    ticks, out = asyncio.run(main())
    assert out.startswith("Image saved:")
    assert ticks >= 10  # the loop ran other work during the 0.3 s edit


def test_two_edits_of_one_file_apply_one_after_the_other(rig):
    from PIL import Image

    async def main():
        border = {"path": str(rig), "operations": [{"type": "add_border", "width": 5}]}
        return await asyncio.gather(images.handle_edit_image(dict(border)),
                                    images.handle_edit_image(dict(border)))

    sizes = sorted(out.split("(")[1].split(",")[0] for out in asyncio.run(main()))
    assert sizes == ["50x40", "60x50"]  # whichever ran first, the other saw its border
    assert Image.open(rig).size == (60, 50)  # both borders: no edit was lost


def test_an_analysis_waits_for_the_edit_before_it(rig, monkeypatch):
    events: list = []
    _slow_op(monkeypatch, events)
    real = images._analyze_image_core

    def _analyze(path):
        events.append(("analyze", None))
        return real(path)

    monkeypatch.setattr(images, "_analyze_image_core", _analyze)

    async def main():
        edit = asyncio.ensure_future(images.handle_edit_image(
            {"path": str(rig), "operations": [{"type": "slow"}]}))
        await asyncio.sleep(0.05)
        report = await images.handle_analyze_image({"path": str(rig)})
        await edit
        return report

    report = asyncio.run(main())
    assert report.startswith("**Image Analysis**")
    assert [e[0] for e in events] == ["slow-start", "slow-end", "analyze"]


def test_a_cancelled_edit_holds_its_turn_until_its_thread_ends(rig, monkeypatch):
    events: list = []
    _slow_op(monkeypatch, events)

    async def main():
        edit = asyncio.ensure_future(images.handle_edit_image(
            {"path": str(rig), "operations": [{"type": "slow", "tag": "first"}]}))
        await asyncio.sleep(0.05)
        edit.cancel()
        with pytest.raises(asyncio.CancelledError):
            await edit
        await images.handle_edit_image({"path": str(rig), "operations": [{"type": "slow", "tag": "next"}]})

    asyncio.run(main())
    assert events == [("slow-start", "first"), ("slow-end", "first"),
                      ("slow-start", "next"), ("slow-end", "next")]


def test_a_paste_whose_image_cannot_be_found_names_that_operation(rig, monkeypatch):
    seen = []

    async def _resolve(p, writing=False, **kw):
        seen.append(p)
        if p == "/elsewhere/logo.png":
            raise PermissionError("this session may not read /elsewhere/logo.png")
        return p

    monkeypatch.setattr(images, "_resolve_path", _resolve)
    out = asyncio.run(images.handle_edit_image({"path": str(rig), "operations": [
        {"type": "flip"},
        {"type": "paste_image", "image_path": "/elsewhere/logo.png"},
    ]}))
    assert out == "Error in operation 'paste_image': this session may not read /elsewhere/logo.png"
    # Looked up before the edit started, in op order.
    assert seen == [str(rig), "/elsewhere/logo.png"]


def test_a_paste_without_its_image_path_fails_at_its_turn(rig):
    out = asyncio.run(images.handle_edit_image({"path": str(rig), "operations": [
        {"type": "paste_image", "path": str(rig)},
    ]}))
    assert out == "Error in operation 'paste_image': 'image_path'"


def test_the_background_model_is_built_once_whatever_thread_asks(monkeypatch):
    built = []

    def _new_session(model):
        built.append(model)
        time.sleep(0.05)
        return object()

    monkeypatch.setitem(sys.modules, "rembg", types.SimpleNamespace(new_session=_new_session))
    monkeypatch.setattr(images, "_rembg_session", None)
    got = []
    threads = [threading.Thread(target=lambda: got.append(images._get_rembg_session())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert built == [images._REMBG_MODEL]
    assert len({id(s) for s in got}) == 1


def test_a_tonal_operation_takes_a_one_bit_image_as_grey(rig, tmp_path):
    """The tonal operations rebuild the image from 8-bit data in its own
    mode, which a 1-bit scan cannot take: it goes through them as grey."""
    from PIL import Image

    src = tmp_path / "scan.png"
    Image.new("1", (40, 30), 1).save(src)
    out = tmp_path / "scan-tones.png"
    reply = asyncio.run(images.handle_edit_image({"path": str(src), "output_path": str(out), "operations": [
        {"type": "blacks", "level": 20}, {"type": "whites", "level": -20},
    ]}))
    assert "Error" not in str(reply)
    assert Image.open(out).mode == "L"


def test_compress_holds_through_an_operation_that_returns_a_new_image(rig, tmp_path):
    """The quality and the format a compress asked for used to ride on the
    image object, which rotate, crop or grayscale replace."""
    from PIL import Image

    out = tmp_path / "small.jpg"
    asyncio.run(images.handle_edit_image({"path": str(rig), "output_path": str(out), "operations": [
        {"type": "compress", "quality": 30}, {"type": "rotate", "degrees": 90},
    ]}))
    reference = tmp_path / "reference.jpg"
    Image.open(rig).rotate(-90, expand=True, resample=Image.BICUBIC).save(reference, quality=30, subsampling=0)
    assert Image.open(out).quantization == Image.open(reference).quantization

    webp = tmp_path / "as-webp.png"
    asyncio.run(images.handle_edit_image({"path": str(rig), "output_path": str(webp), "operations": [
        {"type": "compress", "format": "webp"}, {"type": "crop", "right": 20, "bottom": 20},
    ]}))
    assert Image.open(webp).format == "WEBP"
