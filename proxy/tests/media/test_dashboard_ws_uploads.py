"""Tests for chat-photo save + sandbox-virtual path injection.

Chat-attached photos via the WS plus-menu (`Take Photo` / `Upload Photo`)
land in scope-correct upload subdirs:

- User-scoped chats (regular agents): ``users/<u>/workspace/uploads/photos/``
- Agent-scoped chats (internal agents):     ``workspace/uploads/photos/``

We unit-test `_save_base64_image`, which the chat WS handler delegates the
decode, re-encode and write to, and `_process_attachments`, which checks
re-attached photos and attached files in worker threads and builds the
prompt's sandbox-virtual paths (the agent-relative path with a leading `/`).
"""

import asyncio
import base64 as _b64
from pathlib import Path
from types import SimpleNamespace

import pytest

# 1×1 transparent PNG (smallest valid image we can decode round-trip)
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAA"
    "C0lEQVR42mP8/wcAAwAB/epv2AIAAAAASUVORK5CYII="
)
_TINY_PNG_DATA_URL = f"data:image/png;base64,{_TINY_PNG_B64}"


@pytest.fixture(autouse=True)
def _agents_root_is_the_tests_folder(tmp_path, monkeypatch):
    """A photo is written beneath the agents root: the test's own folder."""
    import config
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)


def _photo_folder(tmp_path: Path) -> dict:
    """``save_dir`` and ``scope_dir`` in a stand-in agent folder (``ws``): a
    photo is written beneath the agent's real folder, and an agent's folder
    always exists."""
    folder = tmp_path / "ws"
    folder.mkdir(exist_ok=True)
    return {"save_dir": folder, "scope_dir": folder}


def test_pasted_photos_are_saved_off_the_loop_in_order(tmp_path, monkeypatch):
    """A pasted phone photo is decoded, resized and re-encoded: seconds of
    CPU that must never hold the event loop. Each photo is saved in a thread,
    one after another (the paths ride the prompt in attachment order),
    two at a time across every connection."""
    import asyncio
    import threading

    import ws.dashboard  # noqa: F401  (assembles the support mixin first)
    from ws import dashboard as dash, dashboard_chat_support as support

    main = threading.get_ident()
    seen: list[tuple[str, bool]] = []
    real = dash._save_base64_image

    def _recording_save(data_url, *, save_dir, scope_dir):
        seen.append((data_url[-8:], threading.get_ident() != main))
        return real(data_url, save_dir=save_dir, scope_dir=scope_dir)

    monkeypatch.setattr(dash, "_save_base64_image", _recording_save)
    monkeypatch.setattr(support, "_PHOTO_WAIT_S", 5.0)
    urls = [_TINY_PNG_DATA_URL + ("=" * i) for i in range(3)]

    async def run():
        return [await support._save_photo_off_loop(dash._save_base64_image, u,
                                                   **_photo_folder(tmp_path)) for u in urls]

    saved = asyncio.run(run())
    assert [s for _, s in seen] == [True, True, True]      # every save off the loop
    assert [u for u, _ in seen] == [u[-8:] for u in urls]  # in attachment order
    assert all(s["path"].startswith(str(tmp_path)) for s in saved)


def test_save_base64_image_uses_passed_save_dir(tmp_path):
    """`_save_base64_image(data_url, save_dir=X)` writes inside X.

    This is the contract the chat WS handler relies on — it computes
    `img_dir = ... / users / <u> / workspace / uploads / photos` and
    expects `_save_base64_image` to honor it.
    """
    from ws.dashboard import _save_base64_image

    (tmp_path / "uploads").mkdir()  # the stand-in agent's folder
    target = tmp_path / "uploads" / "photos"
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=target, scope_dir=target)

    assert saved is not None, "save should succeed for a valid data URL"
    p = Path(saved["path"])
    assert p.parent == target, "file must land inside the passed save_dir"
    assert p.is_file()
    assert p.name.startswith("img_"), "filename pattern preserved"
    assert p.suffix in (".png", ".jpg")


def test_save_base64_image_creates_missing_parents(tmp_path):
    """Deep `save_dir` (e.g. /uploads/photos) auto-creates parents.

    The chat WS handler's `img_dir` is two levels deep under the user's
    workspace; on first chat-photo upload, neither `uploads/` nor
    `uploads/photos/` exists. `_save_base64_image` must mkdir -p.
    """
    from ws.dashboard import _save_base64_image

    (tmp_path / "agent").mkdir()  # the stand-in agent's folder
    deep = tmp_path / "agent" / "users" / "alice" / "workspace" / "uploads" / "photos"
    assert not deep.exists()

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=deep, scope_dir=deep)

    assert saved is not None
    assert deep.is_dir()
    assert Path(saved["path"]).is_file()


def test_save_base64_image_supports_agent_scoped_workspace(tmp_path):
    """Agent-scoped (internal-agent) save dir lives under the agent workspace
    root rather than a per-user subtree. Same `_save_base64_image` contract.
    """
    from ws.dashboard import _save_base64_image

    (tmp_path / "agent").mkdir()  # the stand-in agent's folder
    target = tmp_path / "agent" / "workspace" / "uploads" / "photos"
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=target, scope_dir=target)

    assert saved is not None
    assert Path(saved["path"]).parent == target


def test_save_base64_image_resize_preserves_format(tmp_path):
    """Tiny image stays PNG (not JPEG-converted); file extension matches."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, **_photo_folder(tmp_path))
    assert saved is not None
    # 1x1 PNG is way under 500KB → stays PNG (not JPEG-converted).
    assert Path(saved["path"]).suffix == ".png"


def test_save_base64_image_returns_path_base64_media_type(tmp_path):
    """The new return shape is a dict with three entries: ``path``, ``base64``,
    ``media_type``. ``base64`` is the base64 of the SAVED bytes (after resize/
    recompress), and ``media_type`` is ``image/png`` or ``image/jpeg``."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, **_photo_folder(tmp_path))
    assert saved is not None
    assert set(saved.keys()) == {"path", "base64", "media_type"}
    # base64 round-trips back to the same bytes that landed on disk.
    on_disk = Path(saved["path"]).read_bytes()
    assert _b64.b64decode(saved["base64"]) == on_disk


def test_save_base64_image_png_for_small_input(tmp_path):
    """Small input image keeps PNG format; ``media_type`` matches."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, **_photo_folder(tmp_path))
    assert saved is not None
    assert saved["media_type"] == "image/png"
    assert Path(saved["path"]).suffix == ".png"


def test_save_base64_image_jpeg_for_large_input(tmp_path):
    """Inputs over 500KB get re-encoded as JPEG q=85 to keep payloads sane.
    Construct a >500KB synthetic input to trigger the JPEG branch.

    Use a JPEG-from-the-start for the source bytes — high-entropy random
    content so it stays large after JPEG compression too. Building from PNG
    fails because PNG compresses synthetic patterns aggressively (test
    setup needs >500KB pre-resize)."""
    import io
    import os

    from PIL import Image

    from ws.dashboard import _save_base64_image

    # 1500x1500 random RGB pixels — enough entropy that JPEG can't compress
    # below 500KB. Stays under the 1568px resize threshold so the resize
    # branch doesn't shrink it before the JPEG decision is made.
    raw_pixels = os.urandom(1500 * 1500 * 3)
    img = Image.frombytes("RGB", (1500, 1500), raw_pixels)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    raw = buf.getvalue()
    assert len(raw) > 500_000, (
        f"test setup: input must exceed 500KB to trigger JPEG branch "
        f"(got {len(raw)} bytes — random data should not compress this small)"
    )
    data_url = f"data:image/jpeg;base64,{_b64.b64encode(raw).decode('ascii')}"

    saved = _save_base64_image(data_url, **_photo_folder(tmp_path))
    assert saved is not None
    assert saved["media_type"] == "image/jpeg"
    assert Path(saved["path"]).suffix == ".jpg"


def _data_url(img, fmt: str, **save) -> str:
    import io
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save)
    mime = {"JPEG": "jpeg", "PNG": "png", "GIF": "gif", "TIFF": "tiff"}[fmt]
    return f"data:image/{mime};base64,{_b64.b64encode(buf.getvalue()).decode('ascii')}"


def _saved_image(saved):
    from PIL import Image
    return Image.open(Path(saved["path"]))


def test_a_small_jpeg_stays_a_jpeg_without_metadata(tmp_path):
    """The client's shrunk phone photo (a JPEG well under 500 KB) is
    saved as a JPEG, not re-encoded as a PNG; the orientation the user saw
    is applied, EXIF and the comment are gone, the colour profile is kept."""
    from PIL import Image, ImageCms

    from ws.dashboard import _save_base64_image

    img = Image.new("RGB", (400, 300), (200, 30, 30))
    exif = img.getexif()
    exif[0x0112] = 6            # rotate 90 degrees on display
    exif[0x010F] = "PhoneMaker"
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    url = _data_url(img, "JPEG", quality=85, exif=exif.tobytes(),
                    comment=b"secret note", icc_profile=icc)

    saved = _save_base64_image(url, **_photo_folder(tmp_path))
    assert saved["media_type"] == "image/jpeg"
    assert Path(saved["path"]).suffix == ".jpg"
    out = _saved_image(saved)
    assert out.format == "JPEG"
    assert out.size == (300, 400)
    assert not out.getexif()
    assert "comment" not in out.info
    assert out.info.get("icc_profile") == icc


def test_a_cmyk_jpeg_is_saved_as_rgb(tmp_path):
    from PIL import Image

    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(
        _data_url(Image.new("CMYK", (64, 64), (0, 50, 50, 0)), "JPEG"), **_photo_folder(tmp_path))
    out = _saved_image(saved)
    assert out.format == "JPEG" and out.mode == "RGB"


@pytest.mark.parametrize("make", ["rgba", "colour_key", "palette_gif", "sixteen_bit"])
def test_transparency_palette_and_deep_images_stay_png(tmp_path, make):
    from PIL import Image

    from ws.dashboard import _save_base64_image

    if make == "rgba":
        url = _data_url(Image.new("RGBA", (64, 64), (0, 0, 255, 128)), "PNG")
    elif make == "colour_key":
        url = _data_url(Image.new("RGB", (64, 64), (0, 255, 0)), "PNG",
                        transparency=(0, 255, 0))
    elif make == "palette_gif":
        url = _data_url(Image.new("P", (64, 64), 3), "GIF")
    else:
        url = _data_url(Image.new("I;16", (64, 64), 40000), "PNG")
    saved = _save_base64_image(url, **_photo_folder(tmp_path))
    assert saved["media_type"] == "image/png"
    assert _saved_image(saved).format == "PNG"


def test_a_32_bit_grey_image_is_kept_as_a_16_bit_png(tmp_path):
    """A scientific TIFF opens as mode I, which PNG cannot hold: it is
    clipped to 16 bits rather than refused."""
    from PIL import Image

    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(
        _data_url(Image.new("I", (64, 64), 70000), "TIFF"), **_photo_folder(tmp_path))
    assert saved["media_type"] == "image/png"
    out = _saved_image(saved)
    assert out.format == "PNG" and out.mode == "I;16"
    assert out.getpixel((0, 0)) == 65535


def test_an_opaque_rgb_png_becomes_a_jpeg(tmp_path):
    from PIL import Image

    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(
        _data_url(Image.new("RGB", (300, 200), (10, 120, 200)), "PNG"), **_photo_folder(tmp_path))
    assert saved["media_type"] == "image/jpeg"
    assert _saved_image(saved).format == "JPEG"


def test_a_huge_non_jpeg_is_refused_with_a_reason(tmp_path):
    """Past 50 MP a PNG would be decoded at full size (hundreds of MB): the
    message is refused with a sentence instead of the photo silently
    vanishing."""
    from PIL import Image

    from ws.dashboard import _save_base64_image
    from ws.dashboard_chat_support import AttachmentsRefused

    url = _data_url(Image.new("1", (7200, 7200), 1), "PNG")
    with pytest.raises(AttachmentsRefused, match="megapixels"):
        _save_base64_image(url, save_dir=tmp_path / "ws", scope_dir=tmp_path / "ws")
    assert list(tmp_path.iterdir()) == []


def test_a_converted_jpeg_keeps_no_profile_of_its_old_mode(tmp_path):
    """A CMYK JPEG is saved as RGB; the profile it carried describes CMYK
    values, so the RGB copy carries none (read as sRGB, the space the
    conversion writes) rather than one that misdescribes its pixels."""
    from PIL import Image, ImageCms

    from ws.dashboard import _save_base64_image

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    saved = _save_base64_image(
        _data_url(Image.new("CMYK", (64, 64), (0, 50, 50, 0)), "JPEG", icc_profile=profile),
        **_photo_folder(tmp_path))
    out = _saved_image(saved)
    assert out.mode == "RGB"
    assert "icc_profile" not in out.info


# ── _process_attachments ───────────────────────────────────────────────────

_AGENT = "photo-agent"
_SCOPE = "users/alice/workspace/"


def _attach(images, files=(), *, direct=False):
    """``_process_attachments`` for a user-scoped chat of ``alice``."""
    import config
    from ws.dashboard_chat_support import ChatSupportMixin
    return ChatSupportMixin._process_attachments(
        SimpleNamespace(), "look", list(images), list(files),
        agent=_AGENT, agent_dir=config.AGENTS_DIR / _AGENT,
        is_agent_scoped=False, username="alice", is_direct_llm=direct)


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    """A private agents root, and no satellite push (no remote session)."""
    import api.media.uploads as uploads
    import config
    import ws.dashboard  # noqa: F401  (assembles the support mixin first)

    async def _no_push(*_a, **_k):
        return None
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(uploads, "_push_upload_to_active_remote_sessions", _no_push)
    return tmp_path


def _saved_photo(root: Path, rel: str, data: bytes = b"") -> str:
    import io

    from PIL import Image
    path = root / _AGENT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
        data = buf.getvalue()
    path.write_bytes(data)
    return rel


def test_a_refused_message_keeps_none_of_the_photos_it_saved(agents_root):
    """A later photo refusing the message (over 50 MP here) removes the
    photos this message already saved; a photo re-attached by its saved
    path belongs to an earlier message and stays."""
    from PIL import Image

    from ws.dashboard_chat_support import AttachmentsRefused

    earlier = _saved_photo(agents_root, _SCOPE + "uploads/photos/img_earlier.png")
    huge = _data_url(Image.new("1", (7200, 7200), 1), "PNG")
    with pytest.raises(AttachmentsRefused, match="megapixels"):
        asyncio.run(_attach([
            {"path": earlier, "name": "earlier.png"},
            {"data": _TINY_PNG_DATA_URL, "name": "a.png"},
            {"data": _TINY_PNG_DATA_URL, "name": "b.png"},
            {"data": huge, "name": "huge.png"},
        ]))
    photos = agents_root / _AGENT / _SCOPE / "uploads" / "photos"
    assert sorted(p.name for p in photos.iterdir()) == ["img_earlier.png"]


def test_a_photo_that_waits_too_long_for_a_slot_refuses_the_message(agents_root, monkeypatch):
    """Both photo slots busy past the wait: the message is refused with the
    busy sentence and the photo saved before it is removed."""
    from ws import dashboard as dash, dashboard_chat_support as support

    monkeypatch.setattr(support, "_PHOTO_WAIT_S", 0.05)
    (agents_root / _AGENT).mkdir()  # an agent's folder always exists
    real = dash._save_base64_image
    held: dict = {}

    def _take_both_slots():
        for _ in range(support._PHOTO_SLOTS):
            held["loop"].create_task(support._photo_slot().acquire())

    def _save_then_fill_the_slots(data_url, *, save_dir, scope_dir):
        out = real(data_url, save_dir=save_dir, scope_dir=scope_dir)
        held["loop"].call_soon_threadsafe(_take_both_slots)
        return out

    monkeypatch.setattr(dash, "_save_base64_image", _save_then_fill_the_slots)

    async def run():
        held["loop"] = asyncio.get_running_loop()
        await _attach([{"data": _TINY_PNG_DATA_URL, "name": "a.png"},
                       {"data": _TINY_PNG_DATA_URL, "name": "b.png"}])

    with pytest.raises(support.AttachmentsRefused, match="busy saving other photos"):
        asyncio.run(run())
    photos = agents_root / _AGENT / _SCOPE / "uploads" / "photos"
    assert list(photos.iterdir()) == []


def test_a_send_cancelled_while_a_photo_saves_keeps_no_photo(agents_root, monkeypatch):
    """A cancel cannot stop a save's thread: the send waits for it, removes
    the file it wrote, and holds the photo slot until the thread ends."""
    import threading
    import time

    from ws import dashboard as dash

    real = dash._save_base64_image
    started, go, done = threading.Event(), threading.Event(), threading.Event()

    def _slow_save(data_url, *, save_dir, scope_dir):
        started.set()
        go.wait(5)
        try:
            return real(data_url, save_dir=save_dir, scope_dir=scope_dir)
        finally:
            done.set()

    monkeypatch.setattr(dash, "_save_base64_image", _slow_save)

    async def run():
        task = asyncio.create_task(_attach([{"data": _TINY_PNG_DATA_URL, "name": "a.png"}]))
        while not started.is_set():  # noqa: ASYNC110  # a thread sets this event: there is nothing to await
            await asyncio.sleep(0.01)
        task.cancel()
        go.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return done.is_set()

    ended_first = asyncio.run(run())
    done.wait(5)
    time.sleep(0.05)
    photos = agents_root / _AGENT / _SCOPE / "uploads" / "photos"
    assert not photos.exists() or list(photos.iterdir()) == []
    assert ended_first, "the send ended before its save's thread"


def test_a_reattached_photo_is_checked_and_read_in_one_job(agents_root):
    """The scope check, the open and the read of a photo re-attached by its
    saved path run together in the worker thread, after the slot wait: a
    photos directory replaced by a link while the photo waits is refused,
    never followed."""
    import os

    from ws import dashboard_chat_support as support

    mine = _saved_photo(agents_root, _SCOPE + "uploads/photos/img_same.png")
    other = _saved_photo(agents_root, "users/bob/workspace/uploads/photos/img_same.png",
                         data=b"\x89PNG not alice's")
    photos = agents_root / _AGENT / _SCOPE / "uploads" / "photos"

    async def run():
        sem = support._photo_slot()
        for _ in range(support._PHOTO_SLOTS):
            await sem.acquire()
        task = asyncio.create_task(_attach([{"path": mine, "name": "same.png"}], direct=True))
        await asyncio.sleep(0.05)               # waiting for a slot
        photos.rename(photos.with_name("photos-moved"))
        os.symlink(agents_root / _AGENT / os.path.dirname(other), photos)
        for _ in range(support._PHOTO_SLOTS):
            sem.release()
        return await task

    _cli, attached, meta, _files = asyncio.run(run())
    assert attached == [] and meta == []


def test_a_reattached_photo_by_a_dotted_or_control_path_is_refused(agents_root):
    """A photo named by a path that climbs out with ``..`` or carries a
    control character never reaches the agent, even when the file it points
    at exists: another person's photo stays theirs."""
    mine = _saved_photo(agents_root, _SCOPE + "uploads/photos/img_mine.png")
    _saved_photo(agents_root, "users/bob/workspace/uploads/photos/img_bob.png")
    climbing = _SCOPE + "../../bob/workspace/uploads/photos/img_bob.png"
    dotted = _SCOPE + "uploads/./photos/img_mine.png"
    control = _SCOPE + "uploads/photos/img\nmine.png"
    cli, attached, meta, _files = asyncio.run(_attach(
        [{"path": climbing, "name": "bob.png"}, {"path": dotted, "name": "dot.png"},
         {"path": control, "name": "nl.png"}, {"path": mine, "name": "mine.png"}], direct=True))
    assert meta == [{"name": "mine.png", "path": mine}]
    assert len(attached) == 1
    assert "bob" not in cli


def test_no_attachment_path_is_resolved_on_the_loop(agents_root, monkeypatch):
    """Re-attached photos and attached files are scope-checked in a worker
    thread: nothing in the attachment path touches the filesystem on the
    event loop."""
    import pathlib

    photo = _saved_photo(agents_root, _SCOPE + "uploads/photos/img_one.png")
    doc = _saved_photo(agents_root, _SCOPE + "uploads/files/notes.txt", data=b"hi")

    def _off_loop(real):
        def guarded(self, *a, **k):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return real(self, *a, **k)
            raise AssertionError(f"Path.{real.__name__} on the event loop: {self}")
        return guarded

    async def run():
        for name in ("resolve", "is_file", "exists", "stat"):
            monkeypatch.setattr(pathlib.Path, name, _off_loop(getattr(pathlib.Path, name)))
        return await _attach([{"path": photo, "name": "one.png"}],
                             [{"path": doc, "name": "notes.txt"}])

    cli, _attached, meta, files = asyncio.run(run())
    monkeypatch.undo()
    assert meta == [{"name": "one.png", "path": photo}]
    assert files == [{"path": doc, "name": "notes.txt"}]
    assert f"- /{photo}" in cli and f"- /{doc} " in cli


def test_a_folder_attached_in_chat_is_listed_as_a_folder(agents_root):
    """A folder inside the chat's scope rides the prompt as a folder; the
    scope root itself, and a folder outside the scope, never do."""
    folder = _SCOPE + "uploads/files/mesitis"
    _saved_photo(agents_root, folder + "/agent.json", data=b"{}")
    _saved_photo(agents_root, "knowledge/x/readme.md", data=b"x")
    cli, _a, _m, files = asyncio.run(_attach([], [
        {"path": folder, "name": "mesitis"},
        {"path": _SCOPE.rstrip("/"), "name": "workspace"},
        {"path": _SCOPE + "../../../knowledge/x", "name": "x"},
    ]))
    assert files == [{"path": folder, "name": "mesitis", "folder": True}]
    assert f"- /{folder}/ (Folder)" in cli
    assert "List a folder's contents first" in cli


def test_an_attached_link_leading_out_of_the_scope_is_left_out(agents_root):
    """A link inside the chat's scope pointing at another user's files, or
    out of the agent, is checked where it leads and left out; a path naming
    another user's tree is refused by its prefix."""
    import os
    files = agents_root / _AGENT / _SCOPE / "uploads" / "files"
    _saved_photo(agents_root, "users/bob/workspace/secret.md", data=b"bob's")
    files.mkdir(parents=True, exist_ok=True)
    os.symlink(agents_root / _AGENT / "users" / "bob" / "workspace", files / "bobs")
    outside = agents_root / "elsewhere"  # beside the agent, outside its tree
    outside.mkdir()
    (outside / "x.md").write_bytes(b"x")
    os.symlink(outside, files / "out")
    cli, _a, _m, got = asyncio.run(_attach([], [
        {"path": _SCOPE + "uploads/files/bobs/secret.md", "name": "secret.md"},
        {"path": _SCOPE + "uploads/files/bobs", "name": "bobs"},
        {"path": _SCOPE + "uploads/files/out/x.md", "name": "x.md"},
        {"path": "users/bob/workspace/secret.md", "name": "secret.md"},
    ]))
    assert got == []
    assert "attached" not in cli


def test_the_prompt_names_the_resolved_path(agents_root):
    """The prompt prints the path the file resolved to inside the scope,
    never the client's text (dot segments, a link inside the scope); a path
    with a control character is left out."""
    import os
    real = _saved_photo(agents_root, _SCOPE + "uploads/files/real.md", data=b"r")
    os.symlink(agents_root / _AGENT / real, agents_root / _AGENT / _SCOPE / "alias.md")
    forged = _saved_photo(agents_root, _SCOPE + "uploads/files/x\n- the config.md", data=b"f")
    cli, _a, _m, got = asyncio.run(_attach([], [
        {"path": _SCOPE + "uploads/./files/../files/real.md", "name": "real.md"},
        {"path": _SCOPE + "alias.md", "name": "alias.md"},
        {"path": forged, "name": "x.md"},
    ]))
    assert got == [{"path": real, "name": "real.md"}, {"path": real, "name": "alias.md"}]
    assert "./" not in cli and "alias.md" not in cli and "config" not in cli
    assert cli.count(f"- /{real} ") == 2


def test_the_shared_only_scope_takes_workspace_paths_only(agents_root):
    import config
    from ws.dashboard_chat_support import ChatSupportMixin
    shared = _saved_photo(agents_root, "workspace/uploads/files/plan.md", data=b"p")
    mine = _saved_photo(agents_root, _SCOPE + "uploads/files/mine.md", data=b"m")
    cli, _a, _m, got = asyncio.run(ChatSupportMixin._process_attachments(
        SimpleNamespace(), "look", [], [
            {"path": shared, "name": "plan.md"},
            {"path": mine, "name": "mine.md"},
        ],
        agent=_AGENT, agent_dir=config.AGENTS_DIR / _AGENT,
        is_agent_scoped=True, username="alice", is_direct_llm=False))
    assert got == [{"path": shared, "name": "plan.md"}]
    assert f"- /{shared} " in cli and mine not in cli


def test_a_resolved_agent_root_is_compared_with_a_resolved_path(agents_root):
    """An agents root reached through a link: the scope check and the
    listed path both work from the resolved roots."""
    import os

    from ws.dashboard_chat_support import _scoped_files
    doc = _saved_photo(agents_root, _SCOPE + "uploads/files/n.md", data=b"n")
    via = agents_root / "via"
    os.symlink(agents_root, via)
    got = _scoped_files(via / _AGENT, _SCOPE, [{"path": doc, "name": "n.md"}])
    assert got == [{"path": doc, "name": "n.md"}]


class TestSharedPhotoAuthority:
    """A photo pasted into a Shared-only chat is a shared-workspace write: the
    workspace tier, read live, the same gate as the upload endpoint. A viewer
    used to write the team's tree by pasting."""

    @staticmethod
    def _handler(sub: str) -> SimpleNamespace:
        return SimpleNamespace(user_sub=sub, user={"sub": sub, "role": "member"})

    def test_viewer_refused_contributor_allowed(self, temp_db):
        from storage import database
        from storage.agents import agent_store
        from ws.dashboard_chat_support import AttachmentsRefused, ChatSupportMixin
        agent_store.create_agent("team-line", "Team Line", created_by="user-admin")
        database.set_user_agents("user-viewer", ["team-line"], "user-admin",
                                 agent_roles={"team-line": "viewer"})
        database.set_user_agents("user-viewer2", ["team-line"], "user-admin",
                                 agent_roles={"team-line": "contributor"})
        gate = ChatSupportMixin._require_shared_photo_authority
        with pytest.raises(AttachmentsRefused, match="contributor role or above"):
            asyncio.run(gate(self._handler("user-viewer"), "team-line"))
        asyncio.run(gate(self._handler("user-viewer2"), "team-line"))


# ---- a pasted photo lands where its folder was judged (stand-in folders) ----


def _scope(tmp_path, user="alice"):
    ws = tmp_path / "agent-x" / "users" / user / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def test_a_photo_folder_that_leads_out_of_the_persons_folder_keeps_nothing(tmp_path):
    from ws.dashboard import _save_base64_image
    mine, theirs = _scope(tmp_path), _scope(tmp_path, "bob")
    (mine / "uploads").mkdir()
    (mine / "uploads" / "photos").symlink_to(theirs)
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=mine / "uploads" / "photos", scope_dir=mine)
    assert saved is None
    assert list(theirs.iterdir()) == []


def test_a_photo_never_lands_in_the_engines_state(tmp_path):
    from ws.dashboard import _save_base64_image
    shared = tmp_path / "agent-x" / "workspace"
    (shared / ".claude").mkdir(parents=True)
    (shared / "uploads").mkdir()
    (shared / "uploads" / "photos").symlink_to(shared / ".claude")
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=shared / "uploads" / "photos",
                               scope_dir=shared / "uploads" / "photos")
    assert saved is None
    assert list((shared / ".claude").iterdir()) == []


def test_a_photo_folder_linked_inside_the_persons_own_folder_still_works(tmp_path):
    from ws.dashboard import _save_base64_image
    mine = _scope(tmp_path)
    (mine / "pictures").mkdir()
    (mine / "uploads").mkdir()
    (mine / "uploads" / "photos").symlink_to(mine / "pictures")
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=mine / "uploads" / "photos",
                               scope_dir=mine)
    assert saved is not None
    assert Path(saved["path"]).parent == mine / "pictures"
    assert Path(saved["path"]).is_file()


def test_a_photo_never_lands_in_the_engines_state_inside_its_scope(tmp_path):
    from ws.dashboard import _save_base64_image
    shared = tmp_path / "agent-x" / "workspace"
    (shared / ".claude").mkdir(parents=True)
    (shared / "uploads").mkdir()
    (shared / "uploads" / "photos").symlink_to(shared / ".claude")
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=shared / "uploads" / "photos",
                               scope_dir=shared)
    assert saved is None
    assert list((shared / ".claude").iterdir()) == []


def test_a_photo_with_no_scope_folder_keeps_nothing(tmp_path):
    """The scope folder is required and is never the agents root itself."""
    from ws.dashboard import _save_base64_image
    assert _save_base64_image(_TINY_PNG_DATA_URL, save_dir=tmp_path, scope_dir=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_a_photo_never_lands_in_a_sign_in_folder_inside_its_scope(tmp_path):
    """A folder name earlier releases gave a tool's sign-in copies is refused
    even inside the person's own folder."""
    from ws.dashboard import _save_base64_image
    mine = _scope(tmp_path)
    kept = mine / "google-tokens"
    kept.mkdir()
    (mine / "uploads").mkdir()
    (mine / "uploads" / "photos").symlink_to(kept)
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=mine / "uploads" / "photos", scope_dir=mine)
    assert saved is None
    assert list(kept.iterdir()) == []


def _data_url_of(fmt: str, mime: str) -> str:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (8, 8), 128).save(buf, format=fmt)
    return f"data:{mime};base64," + _b64.b64encode(buf.getvalue()).decode()


@pytest.mark.parametrize(("fmt", "mime"), [("PPM", "image/x-portable-pixmap"), ("EPS", "image/png")],
                         ids=["ppm", "eps-named-as-png"])
def test_a_picture_in_a_format_the_chat_does_not_keep_is_not_decoded(tmp_path, monkeypatch, fmt, mime):
    """Only the formats the chat keeps are opened: a still picture in any
    other format is left out before the server decodes it, whatever its
    declared type, and nothing is saved. The message goes on, and the chat
    says the photo did not reach the agent. One of those formats would have
    the decoder start a program of the host."""
    from PIL import EpsImagePlugin

    from ws.dashboard import _save_base64_image
    loads = []
    monkeypatch.setattr(EpsImagePlugin.EpsImageFile, "load", lambda self, *a, **k: loads.append(self))
    assert _save_base64_image(_data_url_of(fmt, mime), save_dir=tmp_path / "out", scope_dir=tmp_path / "out") is None
    assert loads == []
    assert not (tmp_path / "out").exists()


def test_a_picture_that_is_no_picture_at_all_is_left_out_too(tmp_path, caplog):
    """A person's picture the decoder cannot read is no fault of the server:
    a warning, never an error."""
    import logging

    from ws.dashboard import _save_base64_image
    junk = "data:image/png;base64," + _b64.b64encode(b"not a picture at all").decode()
    with caplog.at_level(logging.INFO, logger="otodock"):
        assert _save_base64_image(junk, save_dir=tmp_path / "out", scope_dir=tmp_path / "out") is None
    assert not (tmp_path / "out").exists()
    assert [r.levelname for r in caplog.records if "image" in r.getMessage().lower()] == ["WARNING"]


def test_a_truncated_picture_is_a_warning_and_a_refused_save_an_error(tmp_path, caplog, monkeypatch):
    """Pillow raises OSError for a truncated file, as the write does: the
    level follows where it was raised, the decoder or the platform's save."""
    import io
    import logging

    from PIL import Image

    from ws import dashboard
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 20, 30)).save(buf, format="PNG")
    cut = "data:image/png;base64," + _b64.b64encode(buf.getvalue()[:60]).decode()
    with caplog.at_level(logging.INFO, logger="otodock"):
        assert dashboard._save_base64_image(cut, **_photo_folder(tmp_path)) is None
    assert [r.levelname for r in caplog.records if "image" in r.getMessage().lower()] == ["WARNING"]
    caplog.clear()

    def _refused(*_a, **_k):
        raise PermissionError("the photo folder is not the person's to write")
    monkeypatch.setattr(dashboard, "_land_photo", _refused)
    with caplog.at_level(logging.INFO, logger="otodock"):
        assert dashboard._save_base64_image(_data_url_of("PNG", "image/png"), **_photo_folder(tmp_path)) is None
    assert [r.levelname for r in caplog.records if "image" in r.getMessage().lower()] == ["ERROR"]


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF", "AVIF", "ICO"])
def test_every_format_the_chat_keeps_still_saves(tmp_path, fmt):
    from ws.dashboard import _save_base64_image
    saved = _save_base64_image(_data_url_of(fmt, f"image/{fmt.lower()}"), **_photo_folder(tmp_path))
    assert saved is not None and Path(saved["path"]).is_file()


# ---- an agent folder that is the operator's link (stand-in folders) ----


@pytest.fixture
def linked_agent(tmp_path, monkeypatch):
    """The agent's folder is a link the operator made to a folder elsewhere:
    the server follows that one link, and nothing beneath it."""
    import config
    agents = tmp_path / "agents"
    agents.mkdir()
    real = tmp_path / "big-disk" / "pa"
    (real / "users" / "alice" / "workspace").mkdir(parents=True)
    (real / "users" / "bob" / "workspace").mkdir(parents=True)
    (agents / "pa").symlink_to(real)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    return agents, real


def test_a_photo_lands_in_a_linked_agent_folder(linked_agent):
    from ws.dashboard import _save_base64_image
    agents, real = linked_agent
    mine = agents / "pa" / "users" / "alice" / "workspace"
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=mine / "uploads" / "photos", scope_dir=mine)
    assert saved is not None
    assert Path(saved["path"]).parent == mine / "uploads" / "photos"
    assert (real / "users/alice/workspace/uploads/photos" / Path(saved["path"]).name).is_file()


def test_a_photo_folder_that_leaves_the_person_is_still_refused_in_a_linked_agent_folder(linked_agent):
    from ws.dashboard import _save_base64_image
    agents, real = linked_agent
    mine = agents / "pa" / "users" / "alice" / "workspace"
    (real / "users" / "alice" / "workspace" / "uploads").symlink_to(real / "users" / "bob" / "workspace")
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=mine / "uploads" / "photos", scope_dir=mine)
    assert saved is None
    assert list((real / "users" / "bob" / "workspace").iterdir()) == []


def test_a_linked_data_folder_still_takes_a_photo(tmp_path, monkeypatch):
    import config
    from ws.dashboard import _save_base64_image
    real = tmp_path / "real-agents"
    (real / "pa" / "users" / "alice" / "workspace").mkdir(parents=True)
    agents = tmp_path / "data-link"
    agents.symlink_to(real)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    mine = agents / "pa" / "users" / "alice" / "workspace"
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=mine / "uploads" / "photos", scope_dir=mine)
    assert saved is not None
    assert saved["path"].startswith(str(agents) + "/")
    assert Path(saved["path"]).is_file()


def test_a_photo_folder_beside_the_scope_folder_is_outside_it(tmp_path):
    """A folder whose name only starts like the person's scope folder is not
    inside it."""
    from ws.dashboard import _save_base64_image
    mine = _scope(tmp_path)
    beside = mine.parent / "workspace-old"
    beside.mkdir()
    (mine / "uploads").symlink_to(beside)
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=mine / "uploads" / "photos", scope_dir=mine)
    assert saved is None
    assert list(beside.iterdir()) == []


def test_a_reattached_photo_past_the_image_cap_is_not_read_whole(agents_root, monkeypatch):
    """An engine that takes photos inline gets a re-attached photo only
    under the chat's image cap: a larger file is left out, never read
    whole into memory."""
    import config
    from ws import dashboard_chat_support as support
    monkeypatch.setattr(config, "TEMP_IMAGE_MAX_BYTES", 64)
    rel = _SCOPE + "uploads/photos/img_big.png"
    big = agents_root / _AGENT / rel
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"\x89PNG" + b"x" * 200)
    assert support._reattach_saved_photo(_AGENT, agents_root / _AGENT, rel, _SCOPE, with_bytes=True) is None
    small = agents_root / _AGENT / (_SCOPE + "uploads/photos/img_small.png")
    small.write_bytes(b"\x89PNG small")
    got = support._reattach_saved_photo(_AGENT, agents_root / _AGENT, _SCOPE + "uploads/photos/img_small.png",
                                        _SCOPE, with_bytes=True)
    assert got is not None and got["base64"]
    assert support._reattach_saved_photo(_AGENT, agents_root / _AGENT, rel, _SCOPE, with_bytes=False) is not None
