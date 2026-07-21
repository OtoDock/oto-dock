"""Unit tests for music-gen-mcp (ElevenLabs music + sound effects).

The MCP code lives outside the proxy's import path; ``load_mcp_server``
imports it by file location. HTTP is mocked — either by stubbing the module's
``_eleven_post`` helper (handler-level tests) or by swapping in an
``httpx.MockTransport`` (wire-level tests) — so no live key is ever needed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

MCP_DIR = CUSTOM_MCPS / "music-gen-mcp"


def _import_server_with_env(monkeypatch, **env):
    """Re-import the server module after setting env vars (module-level
    constants snapshot at import time)."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("AUDIO_SAVE_DIR", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_mcp_server(MCP_DIR)


def _stub_generation(monkeypatch, mod, audio=b"ID3-fake-mp3", hook_ok=True):
    """Stub the ElevenLabs POST + dashboard hook; return the captured calls."""
    calls = {}

    async def fake_post(path, body):
        calls["path"] = path
        calls["body"] = body
        return audio

    async def fake_player(file_path, title):
        calls["played"] = (file_path, title)
        return hook_ok

    monkeypatch.setattr(mod, "_eleven_post", fake_post)
    monkeypatch.setattr(mod, "_push_audio_player", fake_player)
    return calls


# ───────────────────────────── manifest invariants ──────────────────────────


class TestManifest:
    @pytest.fixture()
    def manifest(self):
        return json.loads((MCP_DIR / "manifest.json").read_text())

    def test_identity_and_server(self, manifest):
        assert manifest["name"] == "music-gen-mcp"
        assert manifest["category"] == "custom"
        assert manifest["server"] == {
            "runtime": "python",
            "transport": "stdio",
            "command": "venv/bin/python",
            "args": ["server.py"],
        }

    def test_key_delivered_via_secret_instance_field(self, manifest):
        fields = manifest["instances"]["fields"]
        assert [f["key"] for f in fields] == ["ELEVENLABS_API_KEY"]
        assert fields[0]["secret"] is True
        assert fields[0]["input_type"] == "password"

    def test_workspace_path_role(self, manifest):
        assert manifest["path_env"] == {"AUDIO_SAVE_DIR": {"role": "workspace"}}

    def test_save_paths_declared_for_write_gating(self, manifest):
        for tool in ("compose_music", "sound_effect"):
            assert manifest["tool_arg_paths"][tool]["save_path"] == {"mode": "write"}

    def test_explicit_assignment_no_costs_no_hosted(self, manifest):
        # BYO subscription credits have no deterministic USD per call — a
        # costs block would be fiction. Revisit if a hosted relay prices it.
        assert manifest["assignment_mode"] == "explicit"
        assert "costs" not in manifest
        assert "hosted" not in manifest


# ───────────────────────────── _get_save_path ────────────────────────────────


class TestGetSavePath:
    @pytest.fixture()
    def mod(self, monkeypatch, tmp_path):
        return _import_server_with_env(monkeypatch, AUDIO_SAVE_DIR=str(tmp_path))

    def test_default_lands_in_generated_assets(self, mod, tmp_path):
        path = mod._get_save_path(None, "music")
        assert path.startswith(str(tmp_path / "generated-assets" / "music_"))
        assert path.endswith(".mp3")
        assert (tmp_path / "generated-assets").is_dir()

    def test_bare_filename_drops_into_generated_assets(self, mod, tmp_path):
        assert mod._get_save_path("theme.mp3", "music") == str(
            tmp_path / "generated-assets" / "theme.mp3"
        )

    def test_relative_subdir_honored(self, mod, tmp_path):
        assert mod._get_save_path("projects/launch/theme.mp3", "music") == str(
            tmp_path / "projects" / "launch" / "theme.mp3"
        )
        assert (tmp_path / "projects" / "launch").is_dir()

    def test_absolute_under_workspace_kept(self, mod, tmp_path):
        target = tmp_path / "cuts" / "final.mp3"
        assert mod._get_save_path(str(target), "music") == str(target)

    def test_absolute_outside_reanchored(self, mod, tmp_path):
        assert mod._get_save_path("/etc/evil.mp3", "music") == str(
            tmp_path / "generated-assets" / "evil.mp3"
        )

    def test_parent_traversal_reanchored(self, mod, tmp_path):
        assert mod._get_save_path("../escape.mp3", "music") == str(
            tmp_path / "generated-assets" / "escape.mp3"
        )

    def test_missing_save_dir_fails_loudly(self, monkeypatch):
        mod = _import_server_with_env(monkeypatch)
        with pytest.raises(RuntimeError, match="AUDIO_SAVE_DIR"):
            mod._get_save_path(None, "music")


# ───────────────────────────── input validation ─────────────────────────────


class TestValidation:
    @pytest.fixture()
    def mod(self, monkeypatch, tmp_path):
        return _import_server_with_env(
            monkeypatch, AUDIO_SAVE_DIR=str(tmp_path), ELEVENLABS_API_KEY="xi-test",
        )

    def test_compose_requires_prompt(self, mod):
        (res,) = asyncio.run(mod._handle_compose({}))
        assert res.text == "Error: prompt is required."

    def test_sfx_requires_prompt(self, mod):
        (res,) = asyncio.run(mod._handle_sfx({"prompt": "  "}))
        assert res.text == "Error: prompt is required."

    def test_missing_key_names_the_admin_page(self, monkeypatch, tmp_path):
        mod = _import_server_with_env(monkeypatch, AUDIO_SAVE_DIR=str(tmp_path))
        (res,) = asyncio.run(mod._handle_compose({"prompt": "calm piano"}))
        assert "ELEVENLABS_API_KEY" in res.text
        assert "music-gen-mcp admin page" in res.text

    @pytest.mark.parametrize("duration", [2, 301, 0, -5])
    def test_compose_duration_out_of_range(self, mod, duration):
        (res,) = asyncio.run(mod._handle_compose(
            {"prompt": "calm piano", "duration_seconds": duration}
        ))
        assert "duration_seconds must be between 3 and 300" in res.text

    def test_compose_duration_not_a_number(self, mod):
        (res,) = asyncio.run(mod._handle_compose(
            {"prompt": "calm piano", "duration_seconds": "long"}
        ))
        assert "must be a number" in res.text

    def test_compose_unknown_model(self, mod):
        (res,) = asyncio.run(mod._handle_compose(
            {"prompt": "calm piano", "model": "music_v9"}
        ))
        assert "unknown model" in res.text

    @pytest.mark.parametrize("duration", [0.4, 30.5])
    def test_sfx_duration_out_of_range(self, mod, duration):
        (res,) = asyncio.run(mod._handle_sfx(
            {"prompt": "chime", "duration_seconds": duration}
        ))
        assert "duration_seconds must be between 0.5 and 30" in res.text

    @pytest.mark.parametrize("influence", [-0.1, 1.5])
    def test_sfx_prompt_influence_out_of_range(self, mod, influence):
        (res,) = asyncio.run(mod._handle_sfx(
            {"prompt": "chime", "prompt_influence": influence}
        ))
        assert "prompt_influence must be between 0 and 1" in res.text


# ───────────────────────────── happy paths ──────────────────────────────────


class TestGeneration:
    @pytest.fixture()
    def mod(self, monkeypatch, tmp_path):
        return _import_server_with_env(
            monkeypatch, AUDIO_SAVE_DIR=str(tmp_path), ELEVENLABS_API_KEY="xi-test",
        )

    def test_compose_writes_file_and_reports_path(self, mod, monkeypatch, tmp_path):
        calls = _stub_generation(monkeypatch, mod, audio=b"MP3DATA")
        (res,) = asyncio.run(mod._handle_compose({
            "prompt": "uplifting launch track",
            "duration_seconds": 45,
            "instrumental": True,
        }))
        assert calls["path"] == "/v1/music"
        assert calls["body"] == {
            "prompt": "uplifting launch track",
            "music_length_ms": 45000,
            "model_id": "music_v1",
            "force_instrumental": True,
        }
        saved = calls["played"][0]
        assert saved.startswith(str(tmp_path / "generated-assets" / "music_"))
        assert Path(saved).read_bytes() == b"MP3DATA"
        assert f"Saved to: {saved}" in res.text
        assert "Audio player displayed to user." in res.text

    def test_compose_save_path_and_model_v2(self, mod, monkeypatch, tmp_path):
        calls = _stub_generation(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_compose({
            "prompt": "ambient pad",
            "model": "music_v2",
            "save_path": "launch/theme.mp3",
        }))
        assert calls["body"]["model_id"] == "music_v2"
        assert calls["body"]["music_length_ms"] == 30000  # default duration
        assert f"Saved to: {tmp_path / 'launch' / 'theme.mp3'}" in res.text

    def test_sfx_minimal_body_omits_optional_params(self, mod, monkeypatch, tmp_path):
        calls = _stub_generation(monkeypatch, mod, audio=b"SFXDATA")
        (res,) = asyncio.run(mod._handle_sfx({"prompt": "soft chime"}))
        assert calls["path"] == "/v1/sound-generation"
        assert calls["body"] == {"text": "soft chime", "loop": False}
        saved = calls["played"][0]
        assert saved.startswith(str(tmp_path / "generated-assets" / "sfx_"))
        assert Path(saved).read_bytes() == b"SFXDATA"
        assert f"Saved to: {saved}" in res.text

    def test_sfx_full_body(self, mod, monkeypatch):
        calls = _stub_generation(monkeypatch, mod)
        asyncio.run(mod._handle_sfx({
            "prompt": "rain ambience",
            "duration_seconds": 12,
            "loop": True,
            "prompt_influence": 0.7,
        }))
        assert calls["body"] == {
            "text": "rain ambience",
            "loop": True,
            "duration_seconds": 12.0,
            "prompt_influence": 0.7,
        }

    def test_hook_failure_still_returns_saved_path(self, mod, monkeypatch):
        _stub_generation(monkeypatch, mod, hook_ok=False)
        (res,) = asyncio.run(mod._handle_sfx({"prompt": "chime"}))
        assert "Saved to:" in res.text
        assert "displayed" not in res.text


# ───────────────────────────── API error surfacing ──────────────────────────


def _http_error(status: int, body: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.elevenlabs.io/v1/music")
    resp = httpx.Response(status, json=body or {}, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


class TestApiErrors:
    @pytest.fixture()
    def mod(self, monkeypatch, tmp_path):
        return _import_server_with_env(
            monkeypatch, AUDIO_SAVE_DIR=str(tmp_path), ELEVENLABS_API_KEY="xi-test",
        )

    def _raise_status(self, monkeypatch, mod, err):
        async def fake_post(path, body):
            raise err
        monkeypatch.setattr(mod, "_eleven_post", fake_post)

    def test_401_points_at_the_key(self, mod, monkeypatch):
        self._raise_status(monkeypatch, mod, _http_error(401))
        (res,) = asyncio.run(mod._handle_compose({"prompt": "calm piano"}))
        assert "rejected the API key (401)" in res.text
        assert "ELEVENLABS_API_KEY" in res.text

    def test_422_surfaces_validation_detail(self, mod, monkeypatch):
        err = _http_error(422, {"detail": [{"msg": "music_length_ms too large"}]})
        self._raise_status(monkeypatch, mod, err)
        (res,) = asyncio.run(mod._handle_sfx({"prompt": "chime"}))
        assert "ElevenLabs API error 422" in res.text
        assert "music_length_ms too large" in res.text

    def test_no_file_written_on_api_error(self, mod, monkeypatch, tmp_path):
        self._raise_status(monkeypatch, mod, _http_error(500))
        asyncio.run(mod._handle_compose({"prompt": "calm piano"}))
        assert not (tmp_path / "generated-assets").exists()


# ───────────────────────────── wire-level (_eleven_post) ────────────────────


class TestElevenPost:
    def _client_factory(self, handler):
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient  # captured before the monkeypatch below

        def factory(**kwargs):
            kwargs.pop("transport", None)
            return real_client(transport=transport, **kwargs)

        return factory

    def test_auth_header_and_output_format(self, monkeypatch, tmp_path):
        mod = _import_server_with_env(
            monkeypatch, AUDIO_SAVE_DIR=str(tmp_path), ELEVENLABS_API_KEY="xi-wire",
        )
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["key"] = request.headers.get("xi-api-key")
            seen["format"] = request.url.params.get("output_format")
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=b"AUDIO")

        monkeypatch.setattr(mod.httpx, "AsyncClient", self._client_factory(handler))
        audio = asyncio.run(mod._eleven_post("/v1/music", {"prompt": "x"}))
        assert audio == b"AUDIO"
        assert seen == {
            "key": "xi-wire",
            "format": "mp3_44100_128",
            "path": "/v1/music",
            "body": {"prompt": "x"},
        }

    def test_empty_response_body_raises(self, monkeypatch, tmp_path):
        mod = _import_server_with_env(
            monkeypatch, AUDIO_SAVE_DIR=str(tmp_path), ELEVENLABS_API_KEY="xi-wire",
        )
        monkeypatch.setattr(
            mod.httpx, "AsyncClient",
            self._client_factory(lambda request: httpx.Response(200, content=b"")),
        )
        with pytest.raises(RuntimeError, match="no audio"):
            asyncio.run(mod._eleven_post("/v1/sound-generation", {"text": "x"}))
