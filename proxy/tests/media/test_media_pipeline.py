"""Unit tests for the audio/video media pipeline (mime detection, codec
classification, transcode decision, and a real ffmpeg remux when available)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from services.media import media_pipeline as mp

# ----------------------------- mime + kind --------------------------------


@pytest.mark.parametrize("name,expected", [
    ("clip.mp4", "video/mp4"),
    ("clip.mov", "video/quicktime"),
    ("clip.webm", "video/webm"),
    ("clip.mkv", "video/x-matroska"),
    ("song.mp3", "audio/mpeg"),
    ("song.m4a", "audio/mp4"),
    ("song.flac", "audio/flac"),
    ("song.opus", "audio/ogg"),
    ("mystery.xyz", "application/octet-stream"),
])
def test_guess_media_mime(name, expected):
    assert mp.guess_media_mime(name) == expected


@pytest.mark.parametrize("mime,ext", [
    ("video/mp4", ".mp4"),
    ("video/quicktime", ".mov"),
    ("audio/mpeg", ".mp3"),
    ("audio/mp4", ".m4a"),
    ("audio/flac", ".flac"),
    ("audio/mpeg; charset=binary", ".mp3"),  # mime params tolerated
    ("AUDIO/MPEG", ".mp3"),                   # case-insensitive
    ("application/octet-stream", ""),
    ("", ""),
])
def test_guess_media_ext(mime, ext):
    assert mp.guess_media_ext(mime) == ext


def _mp4_box(typ: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + typ + payload


def test_is_faststart(tmp_path):
    # moov before mdat → faststart (True); mdat before moov → not (False).
    fast = tmp_path / "fast.mp4"
    fast.write_bytes(
        _mp4_box(b"ftyp", b"isom") + _mp4_box(b"moov", b"\x00" * 8)
        + _mp4_box(b"mdat", b"\x00" * 64)
    )
    slow = tmp_path / "slow.mp4"
    slow.write_bytes(
        _mp4_box(b"ftyp", b"isom") + _mp4_box(b"mdat", b"\x00" * 64)
        + _mp4_box(b"moov", b"\x00" * 8)
    )
    assert mp._is_faststart(fast) is True
    assert mp._is_faststart(slow) is False
    # Unparseable / truncated → conservatively True (skip remux).
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x00")
    assert mp._is_faststart(junk) is True


@pytest.mark.parametrize("name,kind", [
    ("a.mp4", "video"), ("a.mov", "video"), ("a.mkv", "video"),
    ("a.mp3", "audio"), ("a.m4a", "audio"), ("a.flac", "audio"),
    ("a.txt", ""),
])
def test_media_kind_from_path(name, kind):
    assert mp.media_kind_from_path(name) == kind


@pytest.mark.parametrize("mime,kind", [
    ("video/mp4", "video"), ("audio/mpeg", "audio"), ("text/plain", ""),
])
def test_media_kind_from_mime(mime, kind):
    assert mp.media_kind_from_mime(mime) == kind


# --------------------------- transcode decision ----------------------------


@pytest.mark.parametrize("name,codecs,expected", [
    # web-safe codec + web-safe container → leave alone
    ("x.mp4", {"video": "h264", "audio": "aac"}, False),
    ("x.webm", {"video": "vp9", "audio": "opus"}, False),
    ("x.mp3", {"video": "", "audio": "mp3"}, False),
    ("x.flac", {"video": "", "audio": "flac"}, False),
    # non-web-safe codec → transcode
    ("x.mov", {"video": "hevc", "audio": "aac"}, True),
    ("x.mp4", {"video": "mpeg4", "audio": "aac"}, True),
    ("x.mp4", {"video": "h264", "audio": "ac3"}, True),
    ("x.wma", {"video": "", "audio": "wmav2"}, True),
    # web-safe codec but unreliable container → remux
    ("x.mov", {"video": "h264", "audio": "aac"}, True),
    ("x.mkv", {"video": "h264", "audio": "aac"}, True),
    ("x.avi", {"video": "h264", "audio": "aac"}, True),
    # can't probe → conservative no-op
    ("x.mp4", None, False),
])
def test_needs_transcode(name, codecs, expected):
    assert mp.needs_transcode(Path(name), codecs) is expected


# --------------------------- ensure_playable -------------------------------


def test_ensure_playable_native_passthrough():
    p, mime, owned = mp.ensure_playable(Path("/tmp/whatever.mp4"))
    assert p == Path("/tmp/whatever.mp4")
    assert mime == "video/mp4"
    assert owned is False


@pytest.mark.asyncio
async def test_ensure_playable_async_no_transcode_for_websafe():
    # h264/aac in an .mp4 — no transcode; returns the original untouched.
    p = Path("/tmp/websafe.mp4")
    out, mime, owned = await mp.ensure_playable_async(
        p, codecs={"video": "h264", "audio": "aac"},
    )
    assert out == p
    assert mime == "video/mp4"
    assert owned is False


@pytest.mark.skipif(not mp.ffmpeg_available(), reason="ffmpeg/ffprobe not installed")
@pytest.mark.asyncio
async def test_remux_h264_mov_to_mp4(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "_CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "in.mov"
    subprocess.run(  # noqa: ASYNC221  # the test builds its fixture clip with ffmpeg before the call under test
        [mp.FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, check=True,
    )
    codecs = await mp.probe(src)
    assert codecs and codecs["video"] == "h264"
    out, mime, owned = await mp.ensure_playable_async(src, codecs=codecs)
    assert out.suffix == ".mp4"
    assert mime == "video/mp4"
    assert owned is True
    assert out.is_file() and out.stat().st_size > 0
    # Second call hits the cache (same output path).
    out2, _, _ = await mp.ensure_playable_async(src)
    assert out2 == out
