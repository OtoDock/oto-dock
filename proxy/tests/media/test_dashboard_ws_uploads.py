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


def test_pasted_photos_are_saved_off_the_loop_in_order(tmp_path, monkeypatch):
    """A pasted phone photo is decoded, resized and re-encoded: seconds of
    CPU that must never hold the event loop. Each photo is saved in a thread,
    one after another (the paths ride the prompt in attachment order),
    two at a time across every connection."""
    import asyncio
    import threading
    import ws.dashboard  # noqa: F401  (assembles the support mixin first)
    from ws import dashboard_chat_support as support

    main = threading.get_ident()
    seen: list[tuple[str, bool]] = []
    real = support._save_base64_image

    def _recording_save(data_url, *, save_dir):
        seen.append((data_url[-8:], threading.get_ident() != main))
        return real(data_url, save_dir=save_dir)

    monkeypatch.setattr(support, "_save_base64_image", _recording_save)
    monkeypatch.setattr(support, "_PHOTO_WAIT_S", 5.0)
    urls = [_TINY_PNG_DATA_URL + ("=" * i) for i in range(3)]

    async def run():
        return [await support._save_photo_off_loop(support._save_base64_image, u,
                                                   save_dir=tmp_path) for u in urls]

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

    target = tmp_path / "uploads" / "photos"
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=target)

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

    deep = tmp_path / "agent" / "users" / "alice" / "workspace" / "uploads" / "photos"
    assert not deep.exists()

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=deep)

    assert saved is not None
    assert deep.is_dir()
    assert Path(saved["path"]).is_file()


def test_save_base64_image_supports_agent_scoped_workspace(tmp_path):
    """Agent-scoped (internal-agent) save dir lives under the agent workspace
    root rather than a per-user subtree. Same `_save_base64_image` contract.
    """
    from ws.dashboard import _save_base64_image

    target = tmp_path / "agent" / "workspace" / "uploads" / "photos"
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=target)

    assert saved is not None
    assert Path(saved["path"]).parent == target


def test_save_base64_image_resize_preserves_format(tmp_path):
    """Tiny image stays PNG (not JPEG-converted); file extension matches."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=tmp_path)
    assert saved is not None
    # 1x1 PNG is way under 500KB → stays PNG (not JPEG-converted).
    assert Path(saved["path"]).suffix == ".png"


def test_save_base64_image_returns_path_base64_media_type(tmp_path):
    """The new return shape is a dict with three entries: ``path``, ``base64``,
    ``media_type``. ``base64`` is the base64 of the SAVED bytes (after resize/
    recompress), and ``media_type`` is ``image/png`` or ``image/jpeg``."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=tmp_path)
    assert saved is not None
    assert set(saved.keys()) == {"path", "base64", "media_type"}
    # base64 round-trips back to the same bytes that landed on disk.
    on_disk = Path(saved["path"]).read_bytes()
    assert _b64.b64decode(saved["base64"]) == on_disk


def test_save_base64_image_png_for_small_input(tmp_path):
    """Small input image keeps PNG format; ``media_type`` matches."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=tmp_path)
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

    saved = _save_base64_image(data_url, save_dir=tmp_path)
    assert saved is not None
    assert saved["media_type"] == "image/jpeg"
    assert Path(saved["path"]).suffix == ".jpg"


def _data_url(img, fmt: str, **save) -> str:
    import io
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save)
    mime = {"JPEG": "jpeg", "PNG": "png", "GIF": "gif"}[fmt]
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

    saved = _save_base64_image(url, save_dir=tmp_path)
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
        _data_url(Image.new("CMYK", (64, 64), (0, 50, 50, 0)), "JPEG"), save_dir=tmp_path)
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
    saved = _save_base64_image(url, save_dir=tmp_path)
    assert saved["media_type"] == "image/png"
    assert _saved_image(saved).format == "PNG"


def test_an_opaque_rgb_png_becomes_a_jpeg(tmp_path):
    from PIL import Image
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(
        _data_url(Image.new("RGB", (300, 200), (10, 120, 200)), "PNG"), save_dir=tmp_path)
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
        _save_base64_image(url, save_dir=tmp_path)
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
        save_dir=tmp_path)
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
    import config
    import api.media.uploads as uploads
    import ws.dashboard  # noqa: F401  (assembles the support mixin first)

    async def _no_push(*_a, **_k):
        return None
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(uploads, "_push_upload_to_active_remote_sessions", _no_push)
    return tmp_path


def _saved_photo(root: Path, rel: str, data: bytes = b"") -> str:
    from PIL import Image
    import io
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
    from ws import dashboard_chat_support as support

    monkeypatch.setattr(support, "_PHOTO_WAIT_S", 0.05)
    real = support._save_base64_image
    held: dict = {}

    def _take_both_slots():
        for _ in range(support._PHOTO_SLOTS):
            held["loop"].create_task(support._photo_slot().acquire())

    def _save_then_fill_the_slots(data_url, *, save_dir):
        out = real(data_url, save_dir=save_dir)
        held["loop"].call_soon_threadsafe(_take_both_slots)
        return out

    monkeypatch.setattr(support, "_save_base64_image", _save_then_fill_the_slots)

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
    from ws import dashboard_chat_support as support

    real = support._save_base64_image
    started, go, done = threading.Event(), threading.Event(), threading.Event()

    def _slow_save(data_url, *, save_dir):
        started.set()
        go.wait(5)
        try:
            return real(data_url, save_dir=save_dir)
        finally:
            done.set()

    monkeypatch.setattr(support, "_save_base64_image", _slow_save)

    async def run():
        task = asyncio.create_task(_attach([{"data": _TINY_PNG_DATA_URL, "name": "a.png"}]))
        while not started.is_set():
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
