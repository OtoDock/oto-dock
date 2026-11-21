"""The picture, music and video tools ask whether their save folder takes a
write before any paid call. A button pressed by someone who may only read the
workspace, or who reaches no folder of the agent, runs those tools with a read
only workspace or none: the vendor is never paid for a file that cannot be
saved, and the person reads one plain sentence.

Stand-in folders in the test's own temporary folder, every vendor call
stubbed to record that it ran.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

REFUSED = "This session cannot save files here, so nothing was made."

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read only folder")


@pytest.fixture
def folders(tmp_path):
    """A read only workspace with its default subfolder, and a workspace that
    is not there at all under a read only parent (a press with no folder)."""
    readonly = tmp_path / "ro" / "workspace"
    (readonly / "generated-assets").mkdir(parents=True)
    missing = tmp_path / "none" / "workspace"
    missing.parent.mkdir()
    (tmp_path / "open").mkdir()
    for folder in (readonly / "generated-assets", readonly, missing.parent):
        folder.chmod(0o555)
    yield {"readonly": readonly, "missing": missing, "open": tmp_path / "open"}
    for folder in (readonly / "generated-assets", readonly, missing.parent):
        folder.chmod(0o755)


def _load(monkeypatch, name: str, env: dict):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_mcp_server(CUSTOM_MCPS / name)


def _text(result) -> str:
    return result[0].text


@pytest.mark.parametrize("where", ["readonly", "missing"])
def test_a_picture_is_never_paid_for_when_it_cannot_be_saved(monkeypatch, folders, where):
    mod = _load(monkeypatch, "image-gen-mcp", {"IMAGE_SAVE_DIR": str(folders[where]),
                                                "GOOGLE_AI_API_KEY": "g-test",
                                                "OPENAI_IMAGE_API_KEY": "o-test"})
    paid: list = []

    async def vendor(*a, **kw):
        paid.append(a)
        return [b"png"]

    async def edit(*a, **kw):
        paid.append(a)
        return b"png"

    async def quiet(*a, **kw):
        return True
    monkeypatch.setattr(mod, "_generate_nano_banana", vendor)
    monkeypatch.setattr(mod, "_generate_openai", vendor)
    monkeypatch.setattr(mod, "_edit_nano_banana", edit)
    monkeypatch.setattr(mod, "_edit_openai", edit)
    for helper in ("_push_generating", "_push_failed", "_push_image_preview", "_push_file_download"):
        monkeypatch.setattr(mod, helper, quiet)
    assert _text(asyncio.run(mod._handle_generate({"prompt": "a cat"}))) == REFUSED
    assert _text(asyncio.run(mod._handle_generate({"prompt": "a cat", "save_path": "pics/c.png",
                                                    "model": "gpt-image", "num_images": 2}))) == REFUSED
    edited = asyncio.run(mod._handle_edit({"image_path": str(Path(__file__)), "prompt": "brighter"}))
    assert _text(edited) == REFUSED
    assert paid == []


def test_a_picture_is_made_where_it_can_be_saved(monkeypatch, folders):
    mod = _load(monkeypatch, "image-gen-mcp", {"IMAGE_SAVE_DIR": str(folders["open"]),
                                                "GOOGLE_AI_API_KEY": "g-test"})

    async def vendor(*a, **kw):
        return [b"png"]

    async def quiet(*a, **kw):
        return True
    monkeypatch.setattr(mod, "_generate_nano_banana", vendor)
    for helper in ("_push_generating", "_push_failed", "_push_image_preview", "_push_file_download"):
        monkeypatch.setattr(mod, helper, quiet)
    answer = _text(asyncio.run(mod._handle_generate({"prompt": "a cat"})))
    assert answer.startswith("Generated 1 image(s)")
    assert list((folders["open"] / "generated-assets").iterdir())


@pytest.mark.parametrize("where", ["readonly", "missing"])
def test_music_is_never_paid_for_when_it_cannot_be_saved(monkeypatch, folders, where):
    mod = _load(monkeypatch, "music-gen-mcp", {"AUDIO_SAVE_DIR": str(folders[where]),
                                                "ELEVENLABS_API_KEY": "e-test"})
    paid: list = []

    async def vendor(path, body):
        paid.append(path)
        return b"ID3"
    monkeypatch.setattr(mod, "_eleven_post", vendor)
    assert _text(asyncio.run(mod._handle_compose({"prompt": "a tune", "duration_seconds": 10}))) == REFUSED
    assert _text(asyncio.run(mod._handle_sfx({"prompt": "a door"}))) == REFUSED
    assert paid == []


def test_music_is_made_where_it_can_be_saved(monkeypatch, folders):
    mod = _load(monkeypatch, "music-gen-mcp", {"AUDIO_SAVE_DIR": str(folders["open"]),
                                                "ELEVENLABS_API_KEY": "e-test"})

    async def vendor(path, body):
        return b"ID3"

    async def player(*a, **kw):
        return True
    monkeypatch.setattr(mod, "_eleven_post", vendor)
    monkeypatch.setattr(mod, "_push_audio_player", player)
    assert "Saved to:" in _text(asyncio.run(mod._handle_sfx({"prompt": "a door"})))


@pytest.mark.parametrize("where", ["readonly", "missing"])
def test_a_video_is_never_paid_for_when_it_cannot_be_saved(monkeypatch, folders, where):
    mod = _load(monkeypatch, "video-gen-mcp", {"VIDEO_SAVE_DIR": str(folders[where]),
                                                "GOOGLE_AI_API_KEY": "g-test", "RUNWAY_API_KEY": "r-test",
                                                "FAL_API_KEY": "f-test"})
    paid: list = []

    async def vendor(*a, **kw):
        paid.append(a)
    for name in [n for n in dir(mod) if n.startswith(("_omni_", "_veo_", "_fal_", "_seedance_", "_runway_"))
                 and asyncio.iscoroutinefunction(getattr(mod, n))]:
        monkeypatch.setattr(mod, name, vendor)
    clip = folders["open"] / "clip.mp4"
    clip.write_bytes(b"MP4" * 8)
    assert _text(asyncio.run(mod._handle_generate({"prompt": "a wave", "model": "omni-flash",
                                                    "duration_seconds": 6}))) == REFUSED
    assert _text(asyncio.run(mod._handle_transition({"video_a_path": str(clip), "video_b_path": str(clip),
                                                      "model": "seedance", "duration_seconds": 5}))) == REFUSED
    assert _text(asyncio.run(mod._handle_edit({"video_path": str(clip), "prompt": "rain"}))) == REFUSED
    assert paid == []
