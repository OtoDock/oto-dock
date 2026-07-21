"""Unit tests for tts-mcp (platform-TTS voice-over generation).

The MCP code lives outside the proxy's import path; ``load_mcp_server``
imports it by file location. The proxy endpoints are mocked with an
``httpx.MockTransport`` — no live provider or key is ever needed.
"""

from __future__ import annotations

import asyncio
import io
import json
import wave

import httpx
import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

MCP_DIR = CUSTOM_MCPS / "tts-mcp"

_ENV_KEYS = ("PROXY_URL", "PROXY_API_KEY", "OTO_SESSION_ID", "AUDIO_SAVE_DIR", "TTS_PROVIDER_ID")


def _import_server_with_env(monkeypatch, **env):
    """Re-import the server module after setting env vars (module-level
    constants snapshot at import time)."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_mcp_server(MCP_DIR)


def _wav_bytes(seconds=0.5, rate=24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(rate * seconds))
    return buf.getvalue()


def _mock_proxy(monkeypatch, mod, handler):
    """Route the MCP's httpx traffic through a MockTransport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)


def _run(coro):
    return asyncio.run(coro)


# ───────────────────────────── manifest invariants ──────────────────────────


class TestManifest:
    @pytest.fixture()
    def manifest(self):
        return json.loads((MCP_DIR / "manifest.json").read_text())

    def test_identity_and_server(self, manifest):
        assert manifest["name"] == "tts-mcp"
        assert manifest["category"] == "custom"
        assert manifest["server"] == {
            "runtime": "python",
            "transport": "stdio",
            "command": "venv/bin/python",
            "args": ["server.py"],
        }

    def test_thin_client_shape(self, manifest):
        # No vendor credentials — the platform holds provider keys.
        assert manifest["credentials"] == {"type": "none"}
        fields = manifest["instances"]["fields"]
        assert [f["key"] for f in fields] == ["TTS_PROVIDER_ID"]
        assert fields[0]["input_type"] == "tts_provider_select"
        assert not fields[0].get("secret")

    def test_capability_gate(self, manifest):
        assert manifest["requires_capability"] == "audio_tts"
        assert manifest["assignment_mode"] == "explicit"
        assert manifest["exclude_from"] == ["phone"]

    def test_workspace_path_role_and_write_gating(self, manifest):
        assert manifest["path_env"] == {"AUDIO_SAVE_DIR": {"role": "workspace"}}
        assert manifest["tool_arg_paths"]["generate_speech"]["save_path"] == {"mode": "write"}

    def test_skill_declared(self, manifest):
        skills = manifest["skills"]
        assert len(skills) == 1
        assert skills[0]["id"] == "voiceover"
        assert (MCP_DIR / skills[0]["file"]).is_file()


# ───────────────────────────── provider precedence ──────────────────────────


def test_provider_precedence_tool_arg_wins(monkeypatch):
    mod = _import_server_with_env(
        monkeypatch, PROXY_URL="http://p", PROXY_API_KEY="t", TTS_PROVIDER_ID="7",
    )
    assert mod._effective_provider_id({"provider_id": 3}) == 3
    assert mod._effective_provider_id({}) == 7


def test_provider_precedence_defaults_to_none(monkeypatch):
    mod = _import_server_with_env(monkeypatch, PROXY_URL="http://p", PROXY_API_KEY="t")
    assert mod._effective_provider_id({}) is None
    mod2 = _import_server_with_env(
        monkeypatch, PROXY_URL="http://p", PROXY_API_KEY="t", TTS_PROVIDER_ID="garbage",
    )
    assert mod2._effective_provider_id({}) is None


# ───────────────────────────── save-path anchoring ──────────────────────────


class TestSavePath:
    @pytest.fixture()
    def mod(self, monkeypatch, tmp_path):
        return _import_server_with_env(
            monkeypatch, PROXY_URL="http://p", PROXY_API_KEY="t",
            AUDIO_SAVE_DIR=str(tmp_path),
        )

    def test_default_lands_in_generated_assets(self, mod, tmp_path):
        p = mod._get_save_path(None)
        assert p.startswith(str(tmp_path / "generated-assets" / "voiceover_"))
        assert p.endswith(".wav")

    def test_bare_filename_and_extension(self, mod, tmp_path):
        p = mod._get_save_path("intro")
        assert p == str(tmp_path / "generated-assets" / "intro.wav")

    def test_relative_subfolder_joins_workspace(self, mod, tmp_path):
        p = mod._get_save_path("projects/promo/vo-01.wav")
        assert p == str(tmp_path / "projects" / "promo" / "vo-01.wav")

    def test_escapes_reanchor(self, mod, tmp_path):
        p = mod._get_save_path("../../etc/evil.wav")
        assert p == str(tmp_path / "generated-assets" / "evil.wav")
        p = mod._get_save_path("/somewhere/else/clip.wav")
        assert p == str(tmp_path / "generated-assets" / "clip.wav")

    def test_absolute_inside_workspace_kept(self, mod, tmp_path):
        target = tmp_path / "sub" / "final.wav"
        assert mod._get_save_path(str(target)) == str(target)

    def test_missing_save_dir_fails_loudly(self, monkeypatch):
        mod = _import_server_with_env(monkeypatch, PROXY_URL="http://p", PROXY_API_KEY="t")
        with pytest.raises(mod.TtsError, match="AUDIO_SAVE_DIR"):
            mod._get_save_path(None)


# ───────────────────────────── tool handlers (wire-level) ────────────────────


class TestGenerate:
    @pytest.fixture()
    def mod(self, monkeypatch, tmp_path):
        return _import_server_with_env(
            monkeypatch, PROXY_URL="http://proxy", PROXY_API_KEY="tok",
            OTO_SESSION_ID="sess-1", AUDIO_SAVE_DIR=str(tmp_path),
        )

    def test_generate_saves_wav_and_reports(self, monkeypatch, mod, tmp_path):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            if request.url.path == "/v1/audio/tts/generate":
                seen["body"] = json.loads(request.content)
                return httpx.Response(200, content=_wav_bytes(), headers={
                    "X-Audio-Seconds": "0.50", "X-Provider-Used": "elevenlabs",
                    "X-Voice-Used": "v-george",
                })
            if request.url.path == "/v1/hooks/media":
                seen["hook"] = json.loads(request.content)
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(404)

        _mock_proxy(monkeypatch, mod, handler)
        out = _run(mod._handle_generate({
            "text": "Hello world",
            "voice_id": "v-george",
            "model_id": "eleven_v3",
            "stability": 0.4,
            "speed": 1.05,
            "sample_rate": 24000,
        }))
        msg = out[0].text
        assert "Error" not in msg
        assert "elevenlabs" in msg and "v-george" in msg
        assert seen["auth"] == "Bearer tok"
        assert seen["body"]["voice_id"] == "v-george"
        assert seen["body"]["model_id"] == "eleven_v3"
        assert seen["body"]["voice_settings"] == {"stability": 0.4, "speed": 1.05}
        assert seen["hook"]["media_kind"] == "audio"
        # The WAV landed under generated-assets/.
        saved = list((tmp_path / "generated-assets").glob("voiceover_*.wav"))
        assert len(saved) == 1
        with wave.open(str(saved[0]), "rb") as w:
            assert w.getframerate() == 24000

    def test_generate_surfaces_proxy_error(self, monkeypatch, mod):
        def handler(request):
            return httpx.Response(400, json={"detail": "No voice configured for this provider"})

        _mock_proxy(monkeypatch, mod, handler)
        out = _run(mod._handle_generate({"text": "hi"}))
        assert out[0].text.startswith("Error:")
        assert "No voice configured" in out[0].text

    def test_generate_hook_failure_still_returns_path(self, monkeypatch, mod, tmp_path):
        def handler(request):
            if request.url.path == "/v1/audio/tts/generate":
                return httpx.Response(200, content=_wav_bytes(), headers={
                    "X-Audio-Seconds": "0.50", "X-Provider-Used": "cartesia",
                    "X-Voice-Used": "v",
                })
            return httpx.Response(500)

        _mock_proxy(monkeypatch, mod, handler)
        out = _run(mod._handle_generate({"text": "hi"}))
        assert "Saved to:" in out[0].text
        assert "displayed" not in out[0].text

    def test_generate_requires_text(self, monkeypatch, mod):
        out = _run(mod._handle_generate({"text": "  "}))
        assert out[0].text.startswith("Error:")

    def test_missing_injection_is_clear(self, monkeypatch):
        mod = _import_server_with_env(monkeypatch)
        with pytest.raises(mod.TtsError, match="misconfigured"):
            _run(mod._handle_generate({"text": "hi"}))


class TestVoices:
    @pytest.fixture()
    def mod(self, monkeypatch, tmp_path):
        return _import_server_with_env(
            monkeypatch, PROXY_URL="http://proxy", PROXY_API_KEY="tok",
            AUDIO_SAVE_DIR=str(tmp_path), TTS_PROVIDER_ID="5",
        )

    def test_list_voices_formats_catalog(self, monkeypatch, mod):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={
                "provider_id": 5, "provider_name": "elevenlabs",
                "configured": {"en": "v-en"},
                "voices": [
                    {"id": "v1", "name": "George", "languages": ["en", "el"],
                     "category": "premade", "preview_url": "https://x/p.mp3",
                     "description": "male narrator", "owner_id": ""},
                ],
            })

        _mock_proxy(monkeypatch, mod, handler)
        out = _run(mod._handle_list_voices({}))
        msg = out[0].text
        assert seen["params"]["provider_id"] == "5"  # instance binding applied
        assert "George" in msg and "`v1`" in msg and "en,el" in msg
        assert "preview" in msg

    def test_search_passes_filters_and_mentions_add(self, monkeypatch, mod):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"provider_name": "elevenlabs", "voices": [
                {"id": "v9", "name": "Nikos", "languages": ["el"],
                 "category": "high_quality", "preview_url": "", "description": "",
                 "owner_id": "own1"},
            ]})

        _mock_proxy(monkeypatch, mod, handler)
        out = _run(mod._handle_search({"search": "warm", "language": "el", "gender": "male"}))
        assert seen["params"]["search"] == "warm"
        assert seen["params"]["gender"] == "male"
        assert "own1" in out[0].text
        assert "add_library_voice" in out[0].text

    def test_add_voice_admin_refusal_is_clear(self, monkeypatch, mod):
        _mock_proxy(monkeypatch, mod, lambda request: httpx.Response(403, json={"detail": "Admin required"}))
        out = _run(mod._handle_add_voice({"public_owner_id": "o", "voice_id": "v"}))
        assert "admin-only" in out[0].text

    def test_add_voice_success(self, monkeypatch, mod):
        _mock_proxy(monkeypatch, mod, lambda request: httpx.Response(200, json={"voice_id": "v9"}))
        out = _run(mod._handle_add_voice({"public_owner_id": "o", "voice_id": "v9"}))
        assert "generate_speech" in out[0].text and "v9" in out[0].text
