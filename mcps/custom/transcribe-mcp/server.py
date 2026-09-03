"""Transcribe MCP — audio/video → timed text → SRT subtitles, all over stdio.

Thin client: the heavy speech-to-text runs on the platform. Each tool sends the
audio to the proxy's ``POST /v1/audio/transcribe`` (Deepgram prerecorded; the
provider's word-level timings come back), writes the result as a transcript JSON
in the workspace, and `generate_subtitles` turns those word timings into an SRT.

Every transcription is a thin call to the proxy. Additional STT engines (cloud
APIs, or a self-hosted Canary/Whisper service the proxy reaches over the LAN) are
registered platform-side as providers, so the MCP itself never changes — pick one
per call via ``provider_id`` (or let the platform default decide).

Env (auto-injected by the platform): ``PROXY_URL`` + ``PROXY_API_KEY`` reach the
proxy; ``TRANSCRIBE_WORKSPACE`` is the agent's workspace dir. Paths in tool args
are gated by the framework (manifest ``tool_arg_paths``) — we resolve relative
paths against the workspace but never add a second allowlist.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from urllib.parse import urlparse

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("transcribe-mcp")

PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
WORKSPACE = os.environ.get("TRANSCRIBE_WORKSPACE", "")
# Per-instance STT provider binding (the instance's stt_provider_select field).
# Used as the default provider when a tool call doesn't pass its own provider_id.
INSTANCE_PROVIDER_ID = os.environ.get("STT_PROVIDER_ID", "").strip()

# Transcript JSON files land under workspace/transcripts/ to keep the root clean.
TRANSCRIPT_SUBDIR = "transcripts"
# URL-download cap (the proxy enforces its own upload cap too).
MAX_DOWNLOAD_MB = 200

server = Server("transcribe")


class TranscribeError(Exception):
    """A user-facing transcription failure (formatted as ``Error: ...``)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _text(s: str) -> list[TextContent]:
    return [TextContent(type="text", text=s)]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _effective_provider_id(args: dict):
    """Which STT provider to use: the tool's ``provider_id`` arg wins; else the
    instance binding (``STT_PROVIDER_ID`` env); else ``None`` → the proxy picks
    the platform default."""
    pid = args.get("provider_id")
    if pid is not None:
        return pid
    if INSTANCE_PROVIDER_ID:
        try:
            return int(INSTANCE_PROVIDER_ID)
        except ValueError:
            return None
    return None


# Path contract: the tool path args (file_path / transcript_json / output_path) are
# declared in the manifest's ``tool_arg_paths``, so the platform's stdio interceptor
# has ALREADY gated them and rewritten them to a real, satellite-native absolute path
# before this code runs (every form — satellite-native, sandbox-virtual, relative — is
# resolved upstream). So we use them VERBATIM (os.path.isfile / open) with NO local
# anchoring, normalization or allowlist. Re-resolving here is both redundant and breaks
# on Windows satellites (forward-slash assumptions) — the same trap removed from
# display-mcp / image-search. Only the DERIVED transcript-JSON write (not a tool arg)
# anchors on TRANSCRIBE_WORKSPACE — see _write_transcript_json.


def _safe_stem(stem: str) -> str:
    """A filesystem-safe transcript stem (basename, no separators)."""
    stem = re.sub(r"[/\\\x00]", "_", os.path.basename(stem or ""))
    return stem[:120] or "transcript"


# ---------------------------------------------------------------------------
# STT seam (thin client to the proxy's STT endpoint; provider placement is
# resolved platform-side).
# ---------------------------------------------------------------------------

async def resolve_and_transcribe(
    data: bytes, filename: str, *, language: str | None, provider_id: int | None,
) -> dict:
    """Transcribe ``data`` and return the proxy's JSON
    (``{text, language, audio_seconds, provider_used, words: [...]}``).

    The MCP is always a thin client: it POSTs to the proxy's
    ``/v1/audio/transcribe``, which resolves the configured STT provider (Deepgram
    today; a self-hosted Canary/Whisper LAN service later — added platform-side,
    transparent here) and records cost server-side. ``provider_id`` selects which
    provider; otherwise the platform default is used.
    """
    if not PROXY_URL or not PROXY_API_KEY:
        raise TranscribeError(
            "PROXY_URL / PROXY_API_KEY not injected — transcribe-mcp is misconfigured."
        )
    form: dict[str, str] = {}
    if language:
        form["language"] = language
    if provider_id is not None:
        form["provider_id"] = str(provider_id)
    files = {"file": (filename or "audio", data, "application/octet-stream")}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15.0, read=600.0, write=120.0, pool=15.0),
    ) as client:
        resp = await client.post(
            f"{PROXY_URL.rstrip('/')}/v1/audio/transcribe",
            files=files, data=form,
            headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
        )
    if resp.status_code != 200:
        raise TranscribeError(
            f"transcription service returned {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


def _url_blocked(url: str) -> str | None:
    """SSRF guard: this sidecar runs platform-side, so agent-supplied URLs
    must not reach loopback/private/link-local hosts (proxy API, docker
    peers, cloud metadata). Resolves the hostname and checks EVERY address;
    re-checked per redirect hop. Returns a reason, or None when allowed."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"unsupported URL scheme: {parsed.scheme!r}"
        host = parsed.hostname or ""
        if not host:
            return "URL has no host"
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_unspecified):
                return f"host {host} resolves to a private/internal address"
    except socket.gaierror:
        return "host does not resolve"
    except ValueError:
        return "invalid URL"
    return None


async def _download(url: str, max_mb: int) -> tuple[bytes, str, str | None]:
    """Stream ``url`` to memory with a size cap. Returns (data, filename, error).
    Redirects are followed manually (≤5 hops) so each hop passes the SSRF
    guard — `follow_redirects=True` would let a public URL bounce the fetch
    into the internal network."""
    max_bytes = max_mb * 1024 * 1024
    if reason := _url_blocked(url):
        return b"", "", reason
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0),
        follow_redirects=False,
    ) as client:
        for _hop in range(5):
            async with client.stream("GET", url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    nxt = resp.headers.get("location", "")
                    url = str(httpx.URL(url).join(nxt))
                    if reason := _url_blocked(url):
                        return b"", "", reason
                    continue
                if resp.status_code != 200:
                    return b"", "", f"download returned {resp.status_code}"
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    return b"", "", f"file too large (> {max_mb} MB)"
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return b"", "", f"file too large (> {max_mb} MB)"
                name = os.path.basename(urlparse(url).path) or "audio"
                return bytes(buf), name, None
        return b"", "", "too many redirects"


def _write_transcript_json(result: dict, stem: str, source: str) -> tuple[str | None, str | None]:
    """Persist the full transcript (incl. the large word list) to the workspace."""
    if not WORKSPACE:
        return None, "TRANSCRIBE_WORKSPACE not set — cannot write transcript JSON"
    out_dir = os.path.join(WORKSPACE.rstrip("/"), TRANSCRIPT_SUBDIR)
    out_path = os.path.join(out_dir, f"{_safe_stem(stem)}.transcript.json")
    payload = {
        "text": result.get("text", ""),
        "language": result.get("language", ""),
        "audio_seconds": result.get("audio_seconds", 0),
        "provider_used": result.get("provider_used", ""),
        "source": source,
        "words": result.get("words", []),
    }
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return None, f"could not write transcript JSON: {e}"
    return out_path, None


def _transcribe_response(result: dict, stem: str, source: str) -> list[TextContent]:
    """Write the transcript JSON + return a readable markdown summary."""
    out_path, err = _write_transcript_json(result, stem, source)
    if err:
        return _err(err)
    text = (result.get("text") or "").strip()
    words = result.get("words") or []
    secs = result.get("audio_seconds") or 0
    provider = result.get("provider_used") or "unknown"
    detected = result.get("language") or "unknown"
    preview = text if len(text) <= 280 else text[:280].rsplit(" ", 1)[0] + " …"
    lines = [
        "## Transcription complete",
        f"- **Words:** {len(words)}  •  **Duration:** {secs}s  •  "
        f"**Language:** {detected}  •  **Provider:** {provider}",
        f"- **Transcript JSON:** `{out_path}`",
        "",
        "**Preview:**",
        f"> {preview or '(no speech detected)'}",
        "",
    ]
    if not words:
        # The file decoded fine (we got a duration) but STT recognized nothing —
        # almost always non-speech audio rather than an error. Spell it out so the
        # agent doesn't misread it as a failure.
        lines.append(
            "⚠️ No speech was recognized. STT transcribes **spoken** audio — "
            "singing/music, silence, or a non-speech track yields no words (the "
            "file processed fine; the model just found no speech). Songs are "
            "expected to fail this way; speech recordings (talks, meetings, "
            "voiceovers) transcribe normally."
        )
    else:
        lines.append(
            "Pass the transcript JSON to `generate_subtitles` to build an SRT "
            "(set `per_word=true` for TikTok-style word-pop captions)."
        )
    return _text("\n".join(lines))


# ---------------------------------------------------------------------------
# Subtitle generation (provider-agnostic — works off the word timings alone)
# ---------------------------------------------------------------------------

def _format_timestamp(seconds: float) -> str:
    """Seconds → ``HH:MM:SS,mmm`` (SRT timestamp)."""
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _group_words(
    words: list, *, max_words_per_cue: int, max_chars_per_cue: int,
    max_duration_s: float, split_on_pause_ms: int, per_word: bool,
) -> list[list[tuple[float, float, str]]]:
    """Group ``[{word,start,end}]`` into cues, each a list of ``(start,end,word)``.

    ``per_word`` → one word per cue (TikTok pop). Otherwise greedily fill a cue
    until any cap trips (word count / char width / duration) or a silence gap
    (``split_on_pause_ms``) or sentence-ending punctuation marks a boundary.
    Keeping the per-word breakdown lets SRT join it and ASS karaoke time each word.
    """
    norm: list[tuple[float, float, str]] = []
    for w in words:
        try:
            txt = (w.get("word") or "").strip()
            start, end = float(w.get("start")), float(w.get("end"))
        except (AttributeError, TypeError, ValueError):
            continue
        if txt:
            norm.append((start, end, txt))

    if per_word:
        return [[w] for w in norm]

    pause_s = max(0, split_on_pause_ms) / 1000.0
    cues: list[list[tuple[float, float, str]]] = []
    cur: list[tuple[float, float, str]] = []
    cur_chars = 0
    prev_end: float | None = None

    def flush() -> None:
        nonlocal cur, cur_chars
        if cur:
            cues.append(cur)
            cur, cur_chars = [], 0

    for (start, end, txt) in norm:
        if cur:
            gap = start - prev_end if prev_end is not None else 0.0
            if ((pause_s > 0 and gap >= pause_s)
                    or len(cur) + 1 > max_words_per_cue
                    or cur_chars + 1 + len(txt) > max_chars_per_cue
                    or (end - cur[0][0]) > max_duration_s):
                flush()
        cur_chars = cur_chars + 1 + len(txt) if cur else len(txt)
        cur.append((start, end, txt))
        prev_end = end
        if txt[-1] in ".!?":
            flush()
    flush()
    return cues


def _join_cue(cue: list[tuple[float, float, str]]) -> tuple[float, float, str]:
    """A grouped cue (list of words) → ``(start, end, joined-text)`` for SRT."""
    return (cue[0][0], cue[-1][1], " ".join(w for _, _, w in cue))


def _words_to_cues(
    words: list, *, max_words_per_cue: int, max_chars_per_cue: int,
    max_duration_s: float, split_on_pause_ms: int, per_word: bool,
) -> list[tuple[float, float, str]]:
    """Grouped text cues (SRT shape). Thin wrapper over :func:`_group_words`."""
    return [_join_cue(c) for c in _group_words(
        words, max_words_per_cue=max_words_per_cue, max_chars_per_cue=max_chars_per_cue,
        max_duration_s=max_duration_s, split_on_pause_ms=split_on_pause_ms, per_word=per_word,
    )]


def _format_srt(cues: list[tuple[float, float, str]]) -> str:
    """Render cues as an SRT document (1-based index, blank-line separated)."""
    blocks: list[str] = []
    for i, (start, end, text) in enumerate(cues):
        s = max(0.0, start)
        e = end if end > s else s + 0.3  # guard zero/negative-length cues
        if i + 1 < len(cues):  # never overlap the next cue
            e = min(e, max(s + 0.05, cues[i + 1][0]))
        blocks.append(f"{i + 1}\n{_format_timestamp(s)} --> {_format_timestamp(e)}\n{text}\n")
    return "\n".join(blocks) + ("\n" if blocks else "")


# ── ASS / karaoke (per-word highlight captions — TikTok style) ─────

# Default karaoke colours (ASS is &HAABBGGRR): the spoken word turns yellow
# (PrimaryColour), pending words stay white (SecondaryColour). `\k` flips each
# word Secondary→Primary as it's sung. Override via the tool's *_color params.
_DEFAULT_PRIMARY = "&H0000FFFF"    # yellow — active / sung word
_DEFAULT_SECONDARY = "&H00FFFFFF"  # white — pending word

# position → ASS alignment (numpad layout): 5 = middle-centre (TikTok/Hormozi),
# 2 = bottom-centre (lifted into the lower third), 8 = top-centre.
_POSITION_ALIGN = {"center": 5, "lower_third": 2, "upper_third": 8}


def _hex_to_ass_color(hex_str: str | None, default: str) -> str:
    """``#RRGGBB`` → ASS ``&H00BBGGRR``. Invalid / empty → *default*."""
    h = (hex_str or "").lstrip("#").strip()
    if len(h) != 6:
        return default
    try:
        int(h, 16)
    except ValueError:
        return default
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_header(font_size: int, primary: str, secondary: str, alignment: int, margin_v: int) -> str:
    """Build the ASS script-info + Karaoke style block for the given look."""
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1280\n"
        "PlayResY: 720\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Karaoke,Arial,{font_size},{primary},{secondary},&H00000000,"
        f"&H64000000,-1,0,0,0,100,100,0,0,1,3,1,{alignment},40,40,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    )


def _ass_ts(seconds: float) -> str:
    """Seconds → ``H:MM:SS.cc`` (ASS timestamp, centiseconds)."""
    cs = max(0, int(round(seconds * 100)))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(word: str) -> str:
    """Strip ASS override-block chars so a word can't break the markup."""
    return word.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ")


def _ass_karaoke_line(cue: list[tuple[float, float, str]]) -> str:
    r"""A cue's words → ASS karaoke text: ``{\k<cs>}word`` per word, where each
    word's highlight lasts until the next word begins (covers the trailing gap)."""
    parts: list[str] = []
    for i, (start, _end, word) in enumerate(cue):
        nxt = cue[i + 1][0] if i + 1 < len(cue) else _end
        dur_cs = max(1, int(round((nxt - start) * 100)))
        parts.append(f"{{\\k{dur_cs}}}{_ass_escape(word)}")
    return " ".join(parts)


def _format_ass(
    cues: list[list[tuple[float, float, str]]], *,
    font_size: int = 54, primary: str = _DEFAULT_PRIMARY,
    secondary: str = _DEFAULT_SECONDARY, position: str = "lower_third",
) -> str:
    """Render grouped cues as an ASS document with per-word karaoke highlight,
    styled by the given font size / active+pending colours / on-screen position."""
    alignment = _POSITION_ALIGN.get(position, 2)
    margin_v = 0 if alignment == 5 else 90  # centred needs no lift; edges lift off
    lines = [_ass_header(font_size, primary, secondary, alignment, margin_v)]
    for cue in cues:
        start = _ass_ts(cue[0][0])
        end = _ass_ts(max(cue[-1][1], cue[0][0] + 0.3))
        lines.append(f"Dialogue: 0,{start},{end},Karaoke,,0,0,0,,{_ass_karaoke_line(cue)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    _lang = {
        "type": "string",
        "description": "Language of the audio. Omit to auto-detect a single dominant "
                       "language. Pass a code (e.g. 'en', 'el', 'es') to force one. Pass "
                       "'multi' for a file that MIXES languages (code-switching; depends "
                       "on the provider + its supported set). If unsure, ask the user "
                       "what language(s) the audio is in.",
    }
    _provider = {
        "type": "integer",
        "description": "Optional STT provider id to override the platform default.",
    }
    return [
        Tool(
            name="transcribe_audio_file",
            description=(
                "Transcribe a local audio or video file to text with word-level "
                "timing. Writes a `<name>.transcript.json` to the workspace and "
                "returns its path. Use it for plain transcription (read the text) "
                "or as the first step of subtitle generation (feed the JSON to "
                "generate_subtitles)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the audio/video file — relative to the "
                                       "workspace or a sandbox-absolute path.",
                    },
                    "language": _lang,
                    "provider_id": _provider,
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="transcribe_audio_url",
            description=(
                "Download an audio or video URL and transcribe it to text with "
                "word-level timing. Writes a transcript JSON to the workspace and "
                "returns its path (same shape as transcribe_audio_file)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http(s) URL of the audio/video to transcribe.",
                    },
                    "language": _lang,
                    "provider_id": _provider,
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="generate_subtitles",
            description=(
                "Build an SRT subtitle file from a transcript JSON (produced by "
                "transcribe_audio_file / transcribe_audio_url). Groups the word "
                "timings into cues; set per_word=true for TikTok-style one-word-at-"
                "a-time captions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transcript_json": {
                        "type": "string",
                        "description": "Path to the transcript JSON (workspace-relative "
                                       "or sandbox-absolute).",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Where to write the subtitles (workspace-relative or "
                                       "sandbox-absolute). The format extension is appended "
                                       "if missing.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["srt", "ass"],
                        "description": "Subtitle format: 'srt' (plain) or 'ass' (per-word "
                                       "karaoke highlight — words light up as spoken, for "
                                       "TikTok-style captions).",
                    },
                    "max_words_per_cue": {
                        "type": "integer",
                        "description": "Max words per cue when grouping (default 4 for ass, "
                                       "7 for srt).",
                    },
                    "max_chars_per_cue": {
                        "type": "integer",
                        "description": "Max characters per cue when grouping (default 42).",
                    },
                    "max_duration_s": {
                        "type": "number",
                        "description": "Max on-screen seconds per cue (default 3.0).",
                    },
                    "split_on_pause_ms": {
                        "type": "integer",
                        "description": "A silence gap this long (ms) starts a new cue "
                                       "(default 400).",
                    },
                    "per_word": {
                        "type": "boolean",
                        "description": "One cue per word (TikTok word-pop). Ignores the "
                                       "grouping caps. Default false.",
                    },
                    "position": {
                        "type": "string",
                        "enum": ["center", "lower_third", "upper_third"],
                        "description": "(ass only) On-screen position. 'center' is the "
                                       "TikTok/Hormozi look; default 'lower_third'.",
                    },
                    "active_color": {
                        "type": "string",
                        "description": "(ass only) Colour of each word as it's spoken, "
                                       "#RRGGBB. Default '#FFFF00' (yellow).",
                    },
                    "inactive_color": {
                        "type": "string",
                        "description": "(ass only) Colour of not-yet-spoken words, #RRGGBB. "
                                       "Default '#FFFFFF' (white).",
                    },
                    "font_size": {
                        "type": "integer",
                        "description": "(ass only) Caption font size (default 54).",
                    },
                },
                "required": ["transcript_json", "output_path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "transcribe_audio_file":
            return await _handle_transcribe_file(arguments)
        if name == "transcribe_audio_url":
            return await _handle_transcribe_url(arguments)
        if name == "generate_subtitles":
            return await _handle_generate_subtitles(arguments)
        return _err(f"unknown tool: {name}")
    except httpx.HTTPError as e:
        return _err(f"could not reach the transcription service: {e}")
    except Exception as e:  # last-resort guard so the MCP never crashes the turn
        logger.exception("transcribe tool failed")
        return _err(f"unexpected failure: {e}")


async def _handle_transcribe_file(args: dict) -> list[TextContent]:
    file_path = (args.get("file_path") or "").strip()
    if not file_path:
        return _err("file_path is required")
    # Already gated + translated to a real absolute path by the interceptor — verbatim.
    if not os.path.isfile(file_path):
        return _err(f"file not found: {file_path}")
    try:
        with open(file_path, "rb") as f:
            data = f.read()
    except OSError as e:
        return _err(f"could not read file: {e}")
    if not data:
        return _err("file is empty")
    try:
        result = await resolve_and_transcribe(
            data, os.path.basename(file_path),
            language=(args.get("language") or None), provider_id=_effective_provider_id(args),
        )
    except TranscribeError as e:
        return _err(str(e))
    return _transcribe_response(result, os.path.splitext(os.path.basename(file_path))[0], file_path)


async def _handle_transcribe_url(args: dict) -> list[TextContent]:
    url = (args.get("url") or "").strip()
    if not url:
        return _err("url is required")
    if not url.startswith(("http://", "https://")):
        return _err("url must be http:// or https://")
    data, filename, derr = await _download(url, MAX_DOWNLOAD_MB)
    if derr:
        return _err(derr)
    if not data:
        return _err("downloaded file is empty")
    try:
        result = await resolve_and_transcribe(
            data, filename,
            language=(args.get("language") or None), provider_id=_effective_provider_id(args),
        )
    except TranscribeError as e:
        return _err(str(e))
    return _transcribe_response(result, os.path.splitext(filename)[0] or "audio", url)


async def _handle_generate_subtitles(args: dict) -> list[TextContent]:
    transcript_json = (args.get("transcript_json") or "").strip()
    output_path = (args.get("output_path") or "").strip()
    if not transcript_json:
        return _err("transcript_json is required")
    if not output_path:
        return _err("output_path is required")
    fmt = (args.get("format") or "srt").lower()
    if fmt not in ("srt", "ass"):
        return _err(f"unsupported format '{fmt}' (use 'srt' or 'ass')")

    # transcript_json + output_path are gated + translated by the interceptor — verbatim.
    if not os.path.isfile(transcript_json):
        return _err(f"transcript JSON not found: {transcript_json}")
    try:
        with open(transcript_json, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return _err(f"could not read transcript JSON: {e}")

    words = doc.get("words") if isinstance(doc, dict) else (doc if isinstance(doc, list) else None)
    if not words:
        return _err(
            "transcript JSON has no word timings (the provider may not support "
            "word-level timestamps)"
        )

    # Karaoke reads best in short chunks (~3-4 words); SRT blocks can run longer.
    default_words = 4 if fmt == "ass" else 7
    cues = _group_words(
        words,
        max_words_per_cue=max(1, _int(args.get("max_words_per_cue"), default_words)),
        max_chars_per_cue=max(1, _int(args.get("max_chars_per_cue"), 42)),
        max_duration_s=max(0.1, _float(args.get("max_duration_s"), 3.0)),
        split_on_pause_ms=_int(args.get("split_on_pause_ms"), 400),
        per_word=bool(args.get("per_word", False)),
    )
    if not cues:
        return _err("no subtitle cues could be generated from the transcript")

    if fmt == "ass":
        content, ext, kind = _format_ass(
            cues,
            font_size=max(8, _int(args.get("font_size"), 54)),
            primary=_hex_to_ass_color(args.get("active_color"), _DEFAULT_PRIMARY),
            secondary=_hex_to_ass_color(args.get("inactive_color"), _DEFAULT_SECONDARY),
            position=(args.get("position") or "lower_third"),
        ), ".ass", "ASS (karaoke)"
    else:
        content, ext, kind = _format_srt([_join_cue(c) for c in cues]), ".srt", "SRT"

    out_path = output_path if output_path.endswith(ext) else output_path + ext
    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return _err(f"could not write subtitles: {e}")

    style = "per-word (TikTok)" if args.get("per_word") else "grouped"
    # ASS keeps styling via the `ass=` filter; SRT uses `subtitles=`.
    burn = f"ass={os.path.basename(out_path)}" if fmt == "ass" else f"subtitles={os.path.basename(out_path)}"
    return _text("\n".join([
        "## Subtitles generated",
        f"- **Cues:** {len(cues)}  •  **Format:** {kind}  •  **Grouping:** {style}",
        f"- **File:** `{out_path}`",
        "",
        f"Burn into a video with: `ffmpeg -i input.mp4 -vf {burn} output.mp4`",
    ]))


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
