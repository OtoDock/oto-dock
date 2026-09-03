"""Server plumbing — path resolution, transcript JSON, handlers, errors."""

import asyncio
import json

import pytest

import server


def run(coro):
    return asyncio.run(coro)


# ── error + path helpers ───────────────────────────────────────────

def test_err_prefix():
    out = server._err("boom")
    assert out[0].text == "Error: boom"


# Path resolution is the framework's job: declared tool_arg_paths arrive already
# gated + translated to an absolute path by the satellite interceptor, so the MCP
# uses them verbatim (there is no _resolve_path). "Missing file → clean error" is
# covered by test_handle_transcribe_file_missing below.


def test_safe_stem_strips_separators():
    assert server._safe_stem("../../etc/passwd") == "passwd"
    assert server._safe_stem("a/b") == "b"
    assert server._safe_stem("") == "transcript"


# ── transcript JSON write ──────────────────────────────────────────

def test_write_transcript_json(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    result = {
        "text": "hello world", "language": "en", "audio_seconds": 3,
        "provider_used": "deepgram",
        "words": [{"word": "hello", "start": 0.0, "end": 0.5}],
    }
    out_path, err = server._write_transcript_json(result, "clip", "clip.wav")
    assert err is None
    assert out_path.endswith("transcripts/clip.transcript.json")
    with open(out_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["text"] == "hello world"
    assert doc["source"] == "clip.wav"
    assert doc["words"][0]["word"] == "hello"


def test_write_transcript_json_no_workspace(monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", "")
    out_path, err = server._write_transcript_json({}, "x", "x")
    assert out_path is None and "TRANSCRIBE_WORKSPACE" in err


# ── transcribe seam ────────────────────────────────────────────────

def test_resolve_and_transcribe_misconfigured(monkeypatch):
    monkeypatch.setattr(server, "PROXY_URL", "")
    monkeypatch.setattr(server, "PROXY_API_KEY", "")
    with pytest.raises(server.TranscribeError):
        run(server.resolve_and_transcribe(b"x", "f.wav", language=None, provider_id=None))


# ── file-transcription handler (STT mocked) ────────────────────────

def test_handle_transcribe_file_writes_json(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"\x00\x01\x02")

    async def fake_transcribe(data, filename, *, language, provider_id):
        assert data == b"\x00\x01\x02"
        return {
            "text": "the quick brown fox", "language": "en", "audio_seconds": 2,
            "provider_used": "deepgram",
            "words": [{"word": "the", "start": 0.0, "end": 0.3}],
        }

    monkeypatch.setattr(server, "resolve_and_transcribe", fake_transcribe)
    out = run(server._handle_transcribe_file({"file_path": str(audio)}))
    text = out[0].text
    assert "Transcription complete" in text
    assert "the quick brown fox" in text
    written = tmp_path / "transcripts" / "clip.transcript.json"
    assert written.is_file()


def test_handle_transcribe_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    out = run(server._handle_transcribe_file({"file_path": "does-not-exist.wav"}))
    assert out[0].text.startswith("Error: file not found")


def test_handle_transcribe_file_requires_path(monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", "/ws")
    out = run(server._handle_transcribe_file({}))
    assert out[0].text == "Error: file_path is required"


def test_transcribe_response_zero_words_hint(tmp_path, monkeypatch):
    """0 words (e.g. a song) → a clear non-speech hint, not a silent empty result."""
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    result = {"text": "", "language": "el", "audio_seconds": 229,
              "provider_used": "deepgram", "words": []}
    text = server._transcribe_response(result, "greek-song", "greek-song.mp3")[0].text
    assert "No speech was recognized" in text
    assert "(no speech detected)" in text


# ── subtitle handler end-to-end ────────────────────────────────────

def test_handle_generate_subtitles_e2e(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    # Write a transcript JSON the way the transcribe tools do.
    result = {
        "text": "one two three", "language": "en", "audio_seconds": 2,
        "provider_used": "deepgram",
        "words": [
            {"word": "one", "start": 0.0, "end": 0.4},
            {"word": "two", "start": 0.4, "end": 0.8},
            {"word": "three", "start": 0.8, "end": 1.2},
        ],
    }
    json_path, _ = server._write_transcript_json(result, "clip", "clip.wav")

    out = run(server._handle_generate_subtitles({
        "transcript_json": json_path, "output_path": str(tmp_path / "subs"), "per_word": True,
    }))
    text = out[0].text
    assert "Subtitles generated" in text
    srt_file = tmp_path / "subs.srt"
    assert srt_file.is_file()
    srt = srt_file.read_text(encoding="utf-8")
    assert "1\n00:00:00,000 --> 00:00:00,400\none" in srt
    assert "3\n" in srt  # per_word → 3 cues


def test_handle_generate_subtitles_ass_karaoke(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    result = {
        "text": "one two", "language": "en", "audio_seconds": 1,
        "provider_used": "deepgram",
        "words": [
            {"word": "one", "start": 0.0, "end": 0.4},
            {"word": "two", "start": 0.5, "end": 1.0},
        ],
    }
    json_path, _ = server._write_transcript_json(result, "clip", "clip.wav")
    out = run(server._handle_generate_subtitles({
        "transcript_json": json_path, "output_path": str(tmp_path / "subs"), "format": "ass",
    }))
    text = out[0].text
    assert "ASS (karaoke)" in text
    assert "ass=subs.ass" in text  # burn hint uses the styling-preserving filter
    ass_file = tmp_path / "subs.ass"
    assert ass_file.is_file()
    ass = ass_file.read_text(encoding="utf-8")
    assert "[Events]" in ass and r"\k" in ass


def test_handle_generate_subtitles_ass_style_params(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    result = {
        "text": "a b", "language": "en", "audio_seconds": 1, "provider_used": "deepgram",
        "words": [{"word": "a", "start": 0.0, "end": 0.4}, {"word": "b", "start": 0.4, "end": 0.8}],
    }
    json_path, _ = server._write_transcript_json(result, "clip", "clip.wav")
    run(server._handle_generate_subtitles({
        "transcript_json": json_path, "output_path": str(tmp_path / "subs.ass"), "format": "ass",
        "position": "center", "active_color": "#FF0000", "font_size": 80,
    }))
    ass = (tmp_path / "subs.ass").read_text(encoding="utf-8")
    style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Karaoke,"))
    assert ",80," in style                 # font size
    assert "&H000000FF" in style           # red active colour (#FF0000 → &H000000FF)
    assert style.endswith(",5,40,40,0,1")  # centre


def test_handle_generate_subtitles_ass_default_chunk_is_four(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    words = [{"word": f"w{i}", "start": i * 0.3, "end": i * 0.3 + 0.25} for i in range(6)]
    result = {"text": "x", "language": "en", "audio_seconds": 2,
              "provider_used": "deepgram", "words": words}
    json_path, _ = server._write_transcript_json(result, "clip", "clip.wav")
    out = run(server._handle_generate_subtitles({
        "transcript_json": json_path, "output_path": str(tmp_path / "subs.ass"), "format": "ass",
    }))
    assert "**Cues:** 2" in out[0].text  # 6 words, ass default chunk 4 → 2 cues


def test_handle_generate_subtitles_no_words(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"text": "hi", "words": []}), encoding="utf-8")
    out = run(server._handle_generate_subtitles({
        "transcript_json": str(p), "output_path": "out.srt",
    }))
    assert out[0].text.startswith("Error:") and "word timings" in out[0].text


def test_handle_generate_subtitles_bad_format(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE", str(tmp_path))
    out = run(server._handle_generate_subtitles({
        "transcript_json": "x.json", "output_path": "o.srt", "format": "vtt",
    }))
    assert "unsupported format" in out[0].text


# ── per-instance provider binding ──────────────────────────────────

def test_effective_provider_id_arg_wins(monkeypatch):
    monkeypatch.setattr(server, "INSTANCE_PROVIDER_ID", "7")
    assert server._effective_provider_id({"provider_id": 3}) == 3


def test_effective_provider_id_falls_back_to_instance(monkeypatch):
    monkeypatch.setattr(server, "INSTANCE_PROVIDER_ID", "7")
    assert server._effective_provider_id({}) == 7


def test_effective_provider_id_none_when_unbound(monkeypatch):
    monkeypatch.setattr(server, "INSTANCE_PROVIDER_ID", "")
    assert server._effective_provider_id({}) is None


def test_effective_provider_id_bad_env_is_none(monkeypatch):
    monkeypatch.setattr(server, "INSTANCE_PROVIDER_ID", "notanint")
    assert server._effective_provider_id({}) is None


# ── dispatch guard ─────────────────────────────────────────────────

def test_unknown_tool():
    out = run(server.call_tool("nope", {}))
    assert out[0].text == "Error: unknown tool: nope"
