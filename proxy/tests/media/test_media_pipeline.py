"""Unit tests for the audio/video media pipeline (mime detection, codec
classification, transcode decision, and a real ffmpeg remux when available)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from services.media import media_pipeline as mp


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """The three serving roots in a test folder: the agents tree (with one
    agent's workspace made), the media cache and the host media cache. The
    agents root is answered as given."""
    import config
    agents = tmp_path / "agents"
    (agents / "agent-a" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(mp, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(mp, "_HOST_CACHE_DIR", tmp_path / "host-cache")
    return agents

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
    with fast.open("rb") as fh:
        assert mp._is_faststart(fh) is True
    with slow.open("rb") as fh:
        assert mp._is_faststart(fh) is False
    # Unparseable / truncated → conservatively True (skip remux).
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x00")
    with junk.open("rb") as fh:
        assert mp._is_faststart(fh) is True


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
async def test_remux_h264_mov_to_mp4(roots):
    src = roots / "agent-a" / "workspace" / "in.mov"
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


# ------------------- the input the media tools are given -------------------


class _ToolStarts:
    """A stand-in for ``subprocess.run`` in the pipeline: records each start
    with what its descriptor refers to, and answers like a tool that wrote
    its output and printed one H.264 stream."""

    def __init__(self):
        self.starts: list[dict] = []

    def __call__(self, argv, **kwargs):
        fds = kwargs.get("pass_fds") or ()
        self.starts.append({
            "argv": list(argv), "pass_fds": tuple(fds),
            "inodes": {fd: os.fstat(fd).st_ino for fd in fds},
            "stdin": kwargs.get("stdin"),
        })
        if argv[-1].endswith((".mp4", ".m4a")) and not argv[-1].startswith("/proc/"):
            Path(argv[-1]).write_bytes(b"converted")
        out = b'{"streams": [{"codec_type": "video", "codec_name": "h264"}]}'
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")


@pytest.fixture
def tool_starts(monkeypatch):
    starts = _ToolStarts()
    monkeypatch.setattr(mp.subprocess, "run", starts)
    monkeypatch.setattr(mp, "FFMPEG", "/stand-in/ffmpeg")
    monkeypatch.setattr(mp, "FFPROBE", "/stand-in/ffprobe")
    return starts


def _slow_mp4() -> bytes:
    return _mp4_box(b"ftyp", b"isom") + _mp4_box(b"mdat", b"\x00" * 64) + _mp4_box(b"moov", b"\x00" * 8)


def test_each_media_tool_reads_the_one_file_the_server_opened(roots, tool_starts):
    """ffprobe and ffmpeg get a descriptor the server opened, through
    ``/proc/self/fd/N``, behind the input format list and the file-only
    protocol list. The path's text is never on their command line."""
    clip = roots / "agent-a" / "workspace" / "clip.mp4"
    clip.write_bytes(_slow_mp4())
    inode = clip.stat().st_ino
    assert mp._probe_sync(clip) == {"video": "h264", "audio": ""}
    out, mime = mp._transcode_sync(clip, {"video": "hevc", "audio": "aac"})
    assert out is not None and mime == "video/mp4"
    assert mp._faststart_remux_sync(clip) is not None
    assert len(tool_starts.starts) == 3
    for start in tool_starts.starts:
        argv = start["argv"]
        assert str(clip) not in " ".join(argv)
        [fd] = start["pass_fds"]
        assert start["inodes"][fd] == inode
        i = argv.index("-i")
        assert argv[i + 1] == f"/proc/self/fd/{fd}"
        head = argv[:i]
        assert head[head.index("-format_whitelist") + 1] == ",".join(mp.INPUT_FORMATS)
        assert head[head.index("-protocol_whitelist") + 1] == "file"
        assert start["stdin"] == subprocess.DEVNULL


def test_the_input_formats_are_the_containers_the_product_plays():
    assert mp.INPUT_FORMATS == ("mov", "matroska", "ogg", "avi", "asf", "flv", "mpegts", "mpeg",
                                "mp3", "aac", "wav", "flac", "aiff", "amr", "gif", "caf", "ac3", "wv")
    assert not {"hls", "concat", "image2", "dash", "tee", "data"} & set(mp.INPUT_FORMATS)


def _link_at_file(agents: Path, tmp: Path) -> Path:
    outside = tmp / "outside.mp4"
    outside.write_bytes(_slow_mp4())
    link = agents / "agent-a" / "workspace" / "clip.mp4"
    link.symlink_to(outside)
    return link


def _link_on_the_way(agents: Path, tmp: Path) -> Path:
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "clip.mp4").write_bytes(_slow_mp4())
    (agents / "agent-a" / "workspace" / "shared").symlink_to(elsewhere)
    return agents / "agent-a" / "workspace" / "shared" / "clip.mp4"


def _link_to_another_agents_file(agents: Path, tmp: Path) -> Path:
    other = agents / "agent-b" / "workspace"
    other.mkdir(parents=True)
    (other / "clip.mp4").write_bytes(_slow_mp4())
    link = agents / "agent-a" / "workspace" / "clip.mp4"
    link.symlink_to(other / "clip.mp4")
    return link


def _link_on_the_way_inside_the_root(agents: Path, tmp: Path) -> Path:
    other = agents / "agent-b" / "workspace"
    other.mkdir(parents=True)
    (other / "clip.mp4").write_bytes(_slow_mp4())
    (agents / "agent-a" / "workspace" / "shared").symlink_to(other)
    return agents / "agent-a" / "workspace" / "shared" / "clip.mp4"


def _outside_every_root(agents: Path, tmp: Path) -> Path:
    stray = tmp / "stray.mp4"
    stray.write_bytes(_slow_mp4())
    return stray


def _a_fifo(agents: Path, tmp: Path) -> Path:
    fifo = agents / "agent-a" / "workspace" / "clip.mp4"
    os.mkfifo(fifo)
    return fifo


@pytest.mark.parametrize("make", [_link_at_file, _link_on_the_way, _link_to_another_agents_file,
                                  _link_on_the_way_inside_the_root, _outside_every_root, _a_fifo],
                         ids=["link-at-the-file", "link-on-the-way", "link-to-another-agents-file",
                              "link-on-the-way-inside-the-root", "outside-every-root", "fifo"])
@pytest.mark.asyncio
async def test_an_input_the_server_cannot_open_plainly_reaches_no_tool(roots, tool_starts, tmp_path, make):
    """A link at any part, a path under no serving root or anything but a
    regular file is never handed to a media tool: the file passes through as
    it is, unconverted."""
    path = make(roots, tmp_path)
    assert mp._probe_sync(path) is None
    assert mp._transcode_sync(path, {"video": "hevc", "audio": "aac"})[0] is None
    assert mp._faststart_remux_sync(path) is None
    served, mime, produced = await mp.ensure_playable_async(path, codecs={"video": "hevc", "audio": "aac"})
    assert (served, mime, produced) == (path, "video/mp4", False)
    assert tool_starts.starts == []


def test_a_media_file_that_already_starts_fast_is_not_remuxed(roots, tool_starts):
    clip = roots / "agent-a" / "workspace" / "fast.mp4"
    clip.write_bytes(_mp4_box(b"ftyp", b"isom") + _mp4_box(b"moov", b"\x00" * 8) + _mp4_box(b"mdat", b"\x00" * 8))
    assert mp._faststart_remux_sync(clip) is None
    assert tool_starts.starts == []


def _make_clip(path: Path, *args: str) -> None:
    subprocess.run([mp.FFMPEG, "-y", "-loglevel", "error", *args, str(path)], capture_output=True, check=True)


@pytest.mark.skipif(not mp.ffmpeg_available(), reason="ffmpeg/ffprobe not installed")
def test_honest_media_still_probes_through_the_descriptor(roots):
    """Real ffmpeg, generated honest clips: what the product plays still
    probes with its extension's hint gone (the tool sees ``/proc/self/fd/N``),
    and a still picture, which no listed format reads, probes to nothing."""
    ws = roots / "agent-a" / "workspace"
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    _make_clip(ws / "clip.mov", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
               *tone, "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", "-shortest")
    _make_clip(ws / "song.mp3", *tone, "-c:a", "libmp3lame", "-write_id3v2", "0")
    _make_clip(ws / "song.aac", *tone, "-c:a", "aac", "-f", "adts")
    _make_clip(ws / "still.png", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=1", "-frames:v", "1")
    _make_clip(ws / "tone.caf", *tone, "-c:a", "pcm_s16le")
    _make_clip(ws / "tone.ac3", *tone, "-c:a", "ac3")
    _make_clip(ws / "tone.wv", *tone, "-c:a", "wavpack")
    assert mp._probe_sync(ws / "clip.mov") == {"video": "h264", "audio": "aac"}
    assert mp._probe_sync(ws / "song.mp3") == {"video": "", "audio": "mp3"}
    assert mp._probe_sync(ws / "song.aac") == {"video": "", "audio": "aac"}
    assert mp._probe_sync(ws / "still.png") is None
    assert mp._probe_sync(ws / "tone.caf") == {"video": "", "audio": "pcm_s16le"}
    assert mp._probe_sync(ws / "tone.ac3") == {"video": "", "audio": "ac3"}
    assert mp._probe_sync(ws / "tone.wv") == {"video": "", "audio": "wavpack"}
