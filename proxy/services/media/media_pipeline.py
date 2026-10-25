"""Audio/video playback pipeline: mime detection, codec probing, and
transcoding to browser-safe formats.

ffmpeg/ffprobe are **proxy-side playback infra only** — never mounted into the
agent bwrap sandbox, never installed on satellites, never in the bash tiers.
The proxy invokes them by an absolute path (`OTO_FFMPEG_PATH` / `OTO_FFPROBE_PATH`,
falling back to `shutil.which`) so a stray binary in `/usr/bin` is not the
dependency surface.

Web-safe inputs are served as-is (native passthrough — the browser plays what
it can). The transcode path converts non-web-safe codecs (HEVC etc.) to
MP4/H.264+AAC; if ffmpeg/ffprobe are unavailable it falls back to passthrough
with a download fallback in the player.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import config
from services.infra import safe_fs

logger = logging.getLogger("otodock.media")

# ffmpeg/ffprobe are resolved via config (config.env / os.environ, per the
# platform's _cfg precedence). Point OTO_FFMPEG_PATH at a non-sandbox-mounted
# path (e.g. /opt/otodock/bin/ffmpeg) in production so a sandboxed process can't
# even see the binary; fall back to a PATH lookup for dev convenience.
FFMPEG = config.FFMPEG_PATH or shutil.which("ffmpeg") or ""
FFPROBE = config.FFPROBE_PATH or shutil.which("ffprobe") or ""

# Transcode cache. Persistent (under PLATFORM_DATA_DIR) so a video shown in chat
# still plays when the history is reloaded after a proxy restart.
_CACHE_DIR = Path(
    config.MEDIA_CACHE_DIR or str(config.PLATFORM_DATA_DIR / "media-cache")
)

# Satellite-host (Desktop/Downloads) media is NOT retained durably: pulled
# originals + their transcode/faststart outputs live here and age out by mtime
# (sweep_host_media_cache). Replay re-pulls from the laptop on demand.
_HOST_CACHE_DIR = config.PLATFORM_DATA_DIR / "host-media-cache"


def host_cache_dir() -> Path:
    """The TTL'd working dir for satellite-host media (created on first use)."""
    _HOST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _HOST_CACHE_DIR

# Codecs browsers decode natively. Anything else is transcoded to H.264/AAC.
WEB_SAFE_VIDEO = frozenset({"h264", "vp8", "vp9", "av1"})
WEB_SAFE_AUDIO = frozenset({
    "aac", "mp3", "opus", "vorbis", "flac",
    "pcm_s16le", "pcm_s24le", "pcm_u8", "pcm_f32le",
})
# Containers that don't serve reliably across browsers even with web-safe codecs
# (Firefox won't play video/quicktime; Chrome is shaky on Matroska) — remux to mp4.
_REMUX_CONTAINERS = frozenset({".mov", ".mkv", ".avi", ".wmv", ".flv", ".m4v"})
_REMUX_AUDIO_CONTAINERS = frozenset({".wma", ".aiff", ".aif"})

# Explicit mime map — do NOT rely on stdlib `mimetypes` (Python 3.10 misses
# .m4a/.opus/.flac/.mkv and is inconsistent for .webm across platforms).
MEDIA_MIME: dict[str, str] = {
    # video
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".ogv": "video/ogg",
    ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv",
    # audio
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".weba": "audio/webm",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
}

VIDEO_EXTS: frozenset[str] = frozenset(
    e for e, m in MEDIA_MIME.items() if m.startswith("video/")
)
AUDIO_EXTS: frozenset[str] = frozenset(
    e for e, m in MEDIA_MIME.items() if m.startswith("audio/")
)


def guess_media_mime(path: str | Path) -> str:
    """Mime for a media file by extension. `application/octet-stream` if unknown."""
    return MEDIA_MIME.get(Path(path).suffix.lower(), "application/octet-stream")


# Canonical extension per mime, for naming a download when the client-supplied
# filename has none (MEDIA_MIME is many-to-one; this is the reverse pick).
_CANONICAL_EXT: dict[str, str] = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/ogg": ".ogv",
    "video/x-msvideo": ".avi",
    "video/x-ms-wmv": ".wmv",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/webm": ".weba",
    "audio/aiff": ".aiff",
}


def guess_media_ext(mime: str) -> str:
    """Canonical file extension (incl. dot) for a media mime, '' if unknown."""
    return _CANONICAL_EXT.get((mime or "").split(";")[0].strip().lower(), "")


def media_kind_from_path(path: str | Path) -> str:
    """'video' | 'audio' | '' for a path, by extension."""
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return ""


def media_kind_from_mime(mime: str) -> str:
    """'video' | 'audio' | '' for a mime string."""
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return ""


def ensure_playable(path: Path, *, media_kind: str = "") -> tuple[Path, str, bool]:
    """Return ``(served_path, mime, cache_owned)`` for a proxy-local media file.

    **Native passthrough** — return the file as-is with its extension-derived
    mime. The dashboard player falls back to a download affordance when the
    browser can't decode the codec (e.g. HEVC on Chrome/Linux). The async
    variant :func:`ensure_playable_async` adds an ffprobe/ffmpeg branch that
    transcodes non-web-safe codecs to a cached MP4 and returns
    ``cache_owned=True`` for that copy.

    ``cache_owned`` marks ``served_path`` as a proxy-cache copy safe to delete
    on cleanup; it is always False here because this sync path never copies.
    """
    mime = guess_media_mime(path)
    return path, mime, False


# --------------------------------------------------------------------------
# Ffprobe codec detection + ffmpeg transcoding (proxy-side only)
# --------------------------------------------------------------------------


def ffmpeg_available() -> bool:
    return bool(FFMPEG and FFPROBE)


def _probe_sync(path: Path) -> dict | None:
    """Return ``{"video": codec, "audio": codec}`` (either may be ""), or None
    if ffprobe is unavailable/fails."""
    if not FFPROBE:
        return None
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.warning("ffprobe failed for %s: %s", path, e)
        return None
    v = a = ""
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not v:
            v = s.get("codec_name", "")
        elif s.get("codec_type") == "audio" and not a:
            a = s.get("codec_name", "")
    return {"video": v, "audio": a}


async def probe(path: Path) -> dict | None:
    return await asyncio.to_thread(_probe_sync, path)


def needs_transcode(path: Path, codecs: dict | None) -> bool:
    """True if the file should be transcoded/remuxed for reliable browser
    playback. Conservative: if we can't probe (codecs is None) we leave the
    file alone and let the player's download fallback handle the rare miss."""
    if codecs is None:
        return False
    ext = Path(path).suffix.lower()
    v = codecs.get("video", "")
    a = codecs.get("audio", "")
    if v:  # video file
        if v not in WEB_SAFE_VIDEO:
            return True
        if a and a not in WEB_SAFE_AUDIO:
            return True
        if ext in _REMUX_CONTAINERS:
            return True
        return False
    if a:  # audio-only
        if a not in WEB_SAFE_AUDIO:
            return True
        if ext in _REMUX_AUDIO_CONTAINERS:
            return True
        return False
    return False


def _transcode_sync(
    path: Path, codecs: dict | None, dest_dir: Path | None = None,
) -> tuple[Path | None, str]:
    """Produce a browser-playable copy. Remux (stream-copy, fast) when the
    codecs are already web-safe; full transcode to H.264/AAC otherwise. Cached
    by (path, mtime, size) under ``dest_dir`` (default durable media-cache;
    satellite-host media passes the TTL'd host cache). Returns (output_path,
    mime) or (None, mime) on failure so the caller can fall back to the
    original."""
    cache = dest_dir or _CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    try:
        st = path.stat()
        key = hashlib.sha256(
            f"{path}:{st.st_mtime_ns}:{st.st_size}".encode()
        ).hexdigest()[:32]
    except OSError:
        return None, ""

    v = (codecs or {}).get("video", "")
    a = (codecs or {}).get("audio", "")
    is_video = bool(v)

    if is_video:
        out = cache / f"{key}.mp4"
        mime = "video/mp4"
        if v == "h264" and a in ("aac", "mp3", ""):
            cmd = [FFMPEG, "-y", "-i", str(path), "-c", "copy",
                   "-movflags", "+faststart", str(out)]
        else:
            audio_args = ["-c:a", "aac"] if a else ["-an"]
            cmd = [FFMPEG, "-y", "-i", str(path),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   *audio_args, "-movflags", "+faststart", str(out)]
    else:
        out = cache / f"{key}.m4a"
        mime = "audio/mp4"
        cmd = [FFMPEG, "-y", "-i", str(path), "-c:a", "aac",
               "-movflags", "+faststart", str(out)]

    if out.is_file() and out.stat().st_size > 0:
        return out, mime  # cache hit

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("ffmpeg transcode error for %s: %s", path, e)
        out.unlink(missing_ok=True)
        return None, mime
    if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        tail = (r.stderr or b"")[-300:].decode(errors="replace")
        logger.warning("ffmpeg transcode failed for %s: %s", path, tail)
        out.unlink(missing_ok=True)
        return None, mime
    return out, mime


def _is_faststart(path: Path) -> bool:
    """True if an MP4's ``moov`` box precedes ``mdat`` (progressive/faststart),
    so the browser can start + seek immediately. Conservative: returns True
    (skip the remux) when the file can't be parsed."""
    try:
        with open(path, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return True
                size = int.from_bytes(header[:4], "big")
                box = header[4:8]
                if box == b"moov":
                    return True
                if box == b"mdat":
                    return False
                if size == 1:  # 64-bit extended size
                    ext = f.read(8)
                    if len(ext) < 8:
                        return True
                    size = int.from_bytes(ext, "big")
                    skip = size - 16
                elif size == 0:
                    return True  # box runs to EOF
                else:
                    skip = size - 8
                if skip < 0:
                    return True
                f.seek(skip, 1)
    except OSError:
        return True


def _faststart_remux_sync(path: Path, dest_dir: Path | None = None) -> Path | None:
    """Stream-copy remux an MP4 with ``+faststart`` (moov → front). No re-encode,
    fast. Cached by (path, mtime, size). Returns the output or None on failure."""
    cache = dest_dir or _CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    try:
        st = path.stat()
        key = hashlib.sha256(
            f"faststart:{path}:{st.st_mtime_ns}:{st.st_size}".encode()
        ).hexdigest()[:32]
    except OSError:
        return None
    out = cache / f"{key}.mp4"
    if out.is_file() and out.stat().st_size > 0:
        return out
    cmd = [FFMPEG, "-y", "-i", str(path), "-c", "copy",
           "-movflags", "+faststart", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("faststart remux error for %s: %s", path, e)
        out.unlink(missing_ok=True)
        return None
    if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        return None
    return out


async def ensure_playable_async(
    path: Path, *, codecs: dict | None = None, media_kind: str = "",
    dest_dir: Path | None = None,
) -> tuple[Path, str, bool]:
    """Async ``ensure_playable``: probe, transcode non-web-safe codecs, and
    faststart-remux web-safe MP4s whose ``moov`` atom is at the end (so playback
    starts fast + seeks cleanly).

    Returns ``(served_path, mime, produced)``. ``produced`` is True when a fresh
    proxy-side file was created (transcode or faststart remux). ``dest_dir``
    selects the cache (default durable media-cache; satellite-host media passes
    the TTL'd host cache). Any failure falls back to native passthrough so the
    player can still try / offer a download."""
    if codecs is None:
        codecs = await probe(path)
    if needs_transcode(path, codecs):
        if not ffmpeg_available():
            logger.info("ffmpeg unavailable; serving %s without transcode", path)
            return path, guess_media_mime(path), False
        out, mime = await asyncio.to_thread(_transcode_sync, path, codecs, dest_dir)
        if out is None:
            return path, guess_media_mime(path), False
        return out, mime, True
    # Web-safe already. A web-safe MP4 with moov-at-end still starts slowly +
    # seeks poorly → cheap stream-copy faststart remux.
    if (
        ffmpeg_available()
        and (codecs or {}).get("video")
        and Path(path).suffix.lower() in (".mp4", ".m4v")
        and not await asyncio.to_thread(_is_faststart, path)
    ):
        out = await asyncio.to_thread(_faststart_remux_sync, path, dest_dir)
        if out is not None:
            return out, "video/mp4", True
    return path, guess_media_mime(path), False


def sweep_host_media_cache(ttl_seconds: int = 7200) -> int:
    """Remove satellite-host media files idle longer than ``ttl_seconds`` (by
    mtime). Nothing here is load-bearing — it's re-pulled on demand — so this
    just bounds disk. Returns the number of files removed."""
    if not _HOST_CACHE_DIR.is_dir():
        return 0
    import time as _time
    cutoff = _time.time() - ttl_seconds
    removed = 0
    for p in _HOST_CACHE_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


# The lazy-pull cache for satellite-host files sits inside the agents tree
# (``core/remote/remote_file_flow._host_cache_root``), one folder per session.
HOST_CACHE_SEGMENT = ".remote-host-cache"


class MediaUnserveable(Exception):
    """The row's path is under no serving root or fails its binding; the
    client gets the same 404 as for a missing file."""


def _serve_roots() -> tuple[str, str, str]:
    """``(agents, media cache, host media cache)`` as absolute paths."""
    return (
        os.path.abspath(os.fspath(config.AGENTS_DIR)),
        os.path.abspath(os.fspath(_CACHE_DIR)),
        os.path.abspath(os.fspath(_HOST_CACHE_DIR)),
    )


def locate_media_row(info: dict) -> tuple[str, str]:
    """``(root, rel)`` for a ``media_tokens`` row: the closest of the three
    roots holding its ``abs_path`` and the path below it. Under the agents
    root the first segment is bound to the row's agent (a legacy row with
    ``agent=''`` is bound to the slug the path names) or, for a lazy-pull
    cache path, to the row's session. Raises ``MediaUnserveable``."""
    abs_path = (info.get("abs_path") or "").strip()
    roots = _serve_roots()
    try:
        root, rel = safe_fs.split_under(abs_path, roots)
    except OSError as exc:
        raise MediaUnserveable(f"path outside every serving root: {exc}") from None
    if root == roots[0]:
        first, _, rest = rel.partition("/")
        if first == HOST_CACHE_SEGMENT:
            session = rest.partition("/")[0]
            if not session or session != (info.get("session_id") or ""):
                raise MediaUnserveable("host-cache path bound to another session")
        else:
            agent = (info.get("agent") or "").strip()
            if not config.is_safe_agent_name(first) or (agent and first != agent):
                raise MediaUnserveable("agent segment does not match the row")
    return root, rel
