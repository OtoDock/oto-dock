"""Unit tests for video-gen-mcp (Omni Flash / Veo 3.1 generation, transitions,
Runway Aleph 2.0 editing, Seedance premium transitions).

The MCP code lives outside the proxy's import path; ``load_mcp_server``
imports it by file location. HTTP is mocked — either by stubbing the module's
provider helpers (handler-level tests) or by swapping in an
``httpx.MockTransport`` with URL routing (wire-level tests) — so no live key
is ever needed. Frame extraction is tested at the logic level by stubbing
``_run_ffmpeg`` (imageio-ffmpeg lives only in the MCP venv), plus one
real-binary test that skips when the package is absent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

MCP_DIR = CUSTOM_MCPS / "video-gen-mcp"

ALL_KEYS = ("GOOGLE_AI_API_KEY", "RUNWAY_API_KEY", "FAL_API_KEY")


def _import_server_with_env(monkeypatch, **env):
    """Re-import the server module after setting env vars (module-level
    constants snapshot at import time)."""
    for key in (*ALL_KEYS, "VIDEO_SAVE_DIR"):
        monkeypatch.delenv(key, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_mcp_server(MCP_DIR)


@pytest.fixture()
def mod(monkeypatch, tmp_path):
    """Server module with a workspace and all three keys configured."""
    return _import_server_with_env(
        monkeypatch, VIDEO_SAVE_DIR=str(tmp_path),
        GOOGLE_AI_API_KEY="g-test", RUNWAY_API_KEY="r-test", FAL_API_KEY="f-test",
    )


@pytest.fixture()
def clips(tmp_path):
    """Two small dummy 'video' files for transition/edit input validation."""
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"FAKEMP4A" * 16)
    b.write_bytes(b"FAKEMP4B" * 16)
    return str(a), str(b)


def _stub_player(monkeypatch, mod, ok=True):
    calls = {}

    async def fake_player(file_path, title):
        calls["played"] = (file_path, title)
        return ok

    monkeypatch.setattr(mod, "_push_video_player", fake_player)
    return calls


# ───────────────────────────── manifest invariants ──────────────────────────


class TestManifest:
    @pytest.fixture()
    def manifest(self):
        return json.loads((MCP_DIR / "manifest.json").read_text())

    def test_identity_and_server(self, manifest):
        assert manifest["name"] == "video-gen-mcp"
        assert manifest["category"] == "custom"
        assert manifest["server"] == {
            "runtime": "python",
            "transport": "stdio",
            "command": "venv/bin/python",
            "args": ["server.py"],
        }

    def test_keys_delivered_via_secret_instance_fields(self, manifest):
        fields = manifest["instances"]["fields"]
        assert [f["key"] for f in fields] == list(ALL_KEYS)
        for f in fields:
            assert f["secret"] is True
            assert f["input_type"] == "password"

    def test_workspace_path_role(self, manifest):
        assert manifest["path_env"] == {"VIDEO_SAVE_DIR": {"role": "workspace"}}

    def test_tool_arg_paths_gate_reads_and_writes(self, manifest):
        tap = manifest["tool_arg_paths"]
        assert tap["generate_video"]["image_path"] == {"mode": "read"}
        assert tap["generate_transition"]["video_a_path"] == {"mode": "read"}
        assert tap["generate_transition"]["video_b_path"] == {"mode": "read"}
        assert tap["edit_video"]["video_path"] == {"mode": "read"}
        for tool in ("generate_video", "generate_transition", "edit_video"):
            assert tap[tool]["save_path"] == {"mode": "write"}

    def test_explicit_assignment_and_task_exclusion(self, manifest):
        assert manifest["assignment_mode"] == "explicit"
        assert manifest["exclude_from"] == ["task"]

    def test_costs_rules_shape(self, manifest):
        costs = manifest["costs"]
        assert costs["provider"] == "video-gen"
        assert costs["currency"] == "USD"
        # Every rule multiplies by duration — per-second PAYG pricing.
        assert all(r["multiply_by"] == "duration_seconds" for r in costs["rules"])
        # edit_video is priced off the optional duration_seconds arg (the
        # schema invites it; missing arg → the engine's ×1 floor).
        edit_rules = [r for r in costs["rules"] if r["tool"] == "edit_video"]
        assert len(edit_rules) == 1
        assert edit_rules[0]["amount"] == 0.28

    def test_edit_video_schema_carries_duration_for_costing(self, mod):
        tools = asyncio.run(mod.list_tools())
        edit = next(t for t in tools if t.name == "edit_video")
        dur = edit.inputSchema["properties"]["duration_seconds"]
        assert dur["type"] == "integer"
        assert "duration_seconds" not in edit.inputSchema["required"]

    def test_transition_veo_rules_priced_at_fal_rates(self, manifest):
        # Veo transitions run via fal when a FAL key exists (the practical
        # route) — price them at fal's audio-on 720p rates: fast $0.15/s,
        # standard $0.40/s (which happens to match Google direct too).
        rules = manifest["costs"]["rules"]
        by_model = {
            r["match"]["model"]: r["amount"]
            for r in rules
            if r["tool"] == "generate_transition" and r.get("match", {}).get("model")
        }
        assert by_model["veo-3.1-fast"] == 0.15
        assert by_model["veo-3.1"] == 0.40

    def test_costs_catch_alls_cover_omitted_model(self, manifest):
        # An absent match key never matches, so each generation tool needs a
        # final catch-all rule (priced at its default model) or an omitted
        # `model` arg records no cost at all.
        rules = manifest["costs"]["rules"]
        for tool, default_amount in (("generate_video", 0.10), ("generate_transition", 0.1814)):
            tool_rules = [r for r in rules if r["tool"] == tool]
            assert tool_rules[-1].get("match", {}) == {}
            assert tool_rules[-1]["amount"] == default_amount
            # Catch-all last — first-match-wins would otherwise shadow the
            # per-model and resolution-only prices.
            assert all(r["match"] for r in tool_rules[:-1])


# ───────────────────────────── _get_save_path ────────────────────────────────


class TestGetSavePath:
    @pytest.fixture()
    def mod(self, monkeypatch, tmp_path):
        return _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path))

    def test_default_lands_in_generated_assets(self, mod, tmp_path):
        path = mod._get_save_path(None, "video")
        assert path.startswith(str(tmp_path / "generated-assets" / "video_"))
        assert path.endswith(".mp4")
        assert (tmp_path / "generated-assets").is_dir()

    def test_bare_filename_drops_into_generated_assets(self, mod, tmp_path):
        assert mod._get_save_path("hero.mp4", "video") == str(
            tmp_path / "generated-assets" / "hero.mp4"
        )

    def test_relative_subdir_honored(self, mod, tmp_path):
        assert mod._get_save_path("projects/launch/hero.mp4", "video") == str(
            tmp_path / "projects" / "launch" / "hero.mp4"
        )

    def test_absolute_under_workspace_kept(self, mod, tmp_path):
        target = tmp_path / "cuts" / "final.mp4"
        assert mod._get_save_path(str(target), "video") == str(target)

    def test_absolute_outside_reanchored(self, mod, tmp_path):
        assert mod._get_save_path("/etc/evil.mp4", "video") == str(
            tmp_path / "generated-assets" / "evil.mp4"
        )

    def test_parent_traversal_reanchored(self, mod, tmp_path):
        assert mod._get_save_path("../escape.mp4", "video") == str(
            tmp_path / "generated-assets" / "escape.mp4"
        )

    def test_missing_save_dir_fails_loudly(self, monkeypatch):
        mod = _import_server_with_env(monkeypatch)
        with pytest.raises(RuntimeError, match="VIDEO_SAVE_DIR"):
            mod._get_save_path(None, "video")


# ───────────────────────────── input validation ─────────────────────────────


class TestValidation:
    def test_generate_requires_prompt(self, mod):
        (res,) = asyncio.run(mod._handle_generate({"duration_seconds": 8}))
        assert res.text == "Error: prompt is required."

    def test_generate_unknown_model(self, mod):
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": 8, "model": "sora-2"}
        ))
        assert "unknown model" in res.text

    @pytest.mark.parametrize("duration", [3, 5, 9, 0])
    def test_generate_veo_duration_enum(self, mod, duration):
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": duration, "model": "veo-3.1"}
        ))
        assert "must be 4, 6, or 8" in res.text

    def test_generate_duration_not_a_number(self, mod):
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": "long"}
        ))
        assert "must be an integer" in res.text

    def test_generate_omni_rejects_high_resolution(self, mod):
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": 8, "resolution": "4k",
             "model": "omni-flash"}
        ))
        assert "omni-flash generates 720p only" in res.text

    def test_generate_missing_image_file(self, mod):
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": 8, "image_path": "/nope.png"}
        ))
        assert "file not found" in res.text

    def test_generate_missing_google_key_names_admin_page(self, monkeypatch, tmp_path):
        mod = _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path))
        (res,) = asyncio.run(mod._handle_generate({"prompt": "x", "duration_seconds": 8}))
        assert "GOOGLE_AI_API_KEY" in res.text
        assert "video-gen-mcp admin page" in res.text

    def test_transition_requires_both_files(self, mod, clips):
        a, _ = clips
        (res,) = asyncio.run(mod._handle_transition(
            {"video_a_path": a, "video_b_path": "/nope.mp4", "duration_seconds": 8}
        ))
        assert "file not found" in res.text

    @pytest.mark.parametrize("duration", [3, 16])
    def test_transition_seedance_duration_range(self, mod, clips, duration):
        a, b = clips
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b,
            "duration_seconds": duration, "model": "seedance",
        }))
        assert "between 4 and 15" in res.text

    def test_transition_seedance_without_fal_key(self, monkeypatch, tmp_path, clips):
        mod = _import_server_with_env(
            monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), GOOGLE_AI_API_KEY="g-test",
        )
        a, b = clips
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b,
            "duration_seconds": 8, "model": "seedance",
        }))
        assert "FAL_API_KEY" in res.text
        assert "video-gen-mcp admin page" in res.text

    def test_transition_seedance_combined_size_cap(self, mod, monkeypatch, clips):
        a, b = clips
        monkeypatch.setattr(mod, "SEEDANCE_MAX_COMBINED_BYTES", 1)
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b,
            "duration_seconds": 8, "model": "seedance",
        }))
        assert "50 MB" in res.text

    def test_edit_requires_prompt_and_file(self, mod):
        (res,) = asyncio.run(mod._handle_edit({"video_path": "", "prompt": "x"}))
        assert "video_path and prompt are required" in res.text
        (res,) = asyncio.run(mod._handle_edit({"video_path": "/nope.mp4", "prompt": "x"}))
        assert "file not found" in res.text

    def test_edit_missing_runway_key_names_admin_page(self, monkeypatch, tmp_path, clips):
        mod = _import_server_with_env(
            monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), GOOGLE_AI_API_KEY="g-test",
        )
        a, _ = clips
        (res,) = asyncio.run(mod._handle_edit({"video_path": a, "prompt": "x"}))
        assert "RUNWAY_API_KEY" in res.text

    def test_edit_upload_size_cap(self, mod, monkeypatch, clips):
        a, _ = clips
        monkeypatch.setattr(mod, "RUNWAY_MAX_UPLOAD_BYTES", 1)
        (res,) = asyncio.run(mod._handle_edit({"video_path": a, "prompt": "x"}))
        assert "200 MB" in res.text


# ───────────────────────────── happy paths ──────────────────────────────────


class TestGeneration:
    def test_default_model_is_veo_fast(self, mod, monkeypatch, tmp_path):
        # Operator verdict 2026-07-19: Veo beats Omni Flash on visual quality —
        # an omitted model must land on veo-3.1-fast.
        calls = {}

        async def fake_veo(model_key, prompt, duration, aspect_ratio, resolution,
                           target_path, first_frame=None, last_frame=None):
            calls["veo"] = (model_key, duration)
            Path(target_path).write_bytes(b"MP4DATA")

        monkeypatch.setattr(mod, "_veo_generate", fake_veo)
        _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_generate({
            "prompt": "a paper boat on a pond", "duration_seconds": 6,
        }))
        assert calls["veo"] == ("veo-3.1-fast", 6)
        assert "Veo 3.1 Fast" in res.text

    def test_omni_selected_explicitly_gets_duration_hint(self, mod, monkeypatch, tmp_path):
        calls = {}

        async def fake_omni(prompt, aspect_ratio, target_path, image=None):
            calls["omni"] = (prompt, aspect_ratio, image)
            Path(target_path).write_bytes(b"MP4DATA")

        monkeypatch.setattr(mod, "_omni_generate", fake_omni)
        player = _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_generate({
            "prompt": "a paper boat on a pond", "duration_seconds": 6,
            "model": "omni-flash",
        }))
        prompt, aspect, image = calls["omni"]
        assert prompt.startswith("a paper boat on a pond")
        assert "about 6 seconds" in prompt  # Omni has no duration param — prompt-steered
        assert aspect == "16:9"
        assert image is None
        saved = player["played"][0]
        assert saved.startswith(str(tmp_path / "generated-assets" / "video_"))
        assert Path(saved).read_bytes() == b"MP4DATA"
        assert "Gemini Omni Flash" in res.text
        assert "Video player displayed to user." in res.text

    def test_veo_image_to_video(self, mod, monkeypatch, tmp_path):
        calls = {}

        async def fake_veo(model_key, prompt, duration, aspect_ratio, resolution,
                           target_path, first_frame=None, last_frame=None):
            calls["veo"] = (model_key, prompt, duration, aspect_ratio, resolution,
                            first_frame, last_frame)
            Path(target_path).write_bytes(b"VEODATA")

        monkeypatch.setattr(mod, "_veo_generate", fake_veo)
        _stub_player(monkeypatch, mod)
        img = tmp_path / "still.jpg"
        img.write_bytes(b"JPGDATA")
        (res,) = asyncio.run(mod._handle_generate({
            "prompt": "camera pushes in", "duration_seconds": 8,
            "model": "veo-3.1", "aspect_ratio": "9:16", "resolution": "1080p",
            "image_path": str(img), "save_path": "launch/hero.mp4",
        }))
        model_key, prompt, duration, aspect, res_p, first, last = calls["veo"]
        assert (model_key, duration, aspect, res_p) == ("veo-3.1", 8, "9:16", "1080p")
        assert prompt == "camera pushes in"  # Veo takes duration as a parameter — no hint
        assert first == ("image/jpeg", b"JPGDATA")
        assert last is None
        assert f"Saved to: {tmp_path / 'launch' / 'hero.mp4'}" in res.text
        assert "from image" in res.text

    def test_transition_veo_routes_via_fal_when_key_present(self, mod, monkeypatch, clips):
        """With a FAL key configured, Veo transitions take the fal route —
        no regional first/last-frame gate there (Google direct blocks EEA keys)."""
        a, b = clips
        calls = {}

        async def fake_flf(model, video_a, video_b, style, duration, aspect_ratio,
                           target_path):
            calls["flf"] = (model, video_a, video_b, style, duration, aspect_ratio)
            Path(target_path).write_bytes(b"BRIDGE")

        monkeypatch.setattr(mod, "_fal_veo_flf_transition", fake_flf)
        _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": 6,
            "model": "veo-3.1-fast", "aspect_ratio": "9:16",
            "prompt": "glide across the water",
        }))
        assert calls["flf"] == (
            "veo-3.1-fast", a, b, "glide across the water", 6, "9:16",
        )
        assert "via fal.ai" in res.text
        assert "bridge clip only" in res.text

    def test_transition_veo_falls_back_to_google_without_fal_key(
        self, monkeypatch, tmp_path, clips,
    ):
        mod = _import_server_with_env(
            monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), GOOGLE_AI_API_KEY="g-test",
        )
        a, b = clips
        calls = {}

        async def fake_extract(video_path, position):
            calls.setdefault("extract", []).append((video_path, position))
            return b"PNG" + position.encode()

        async def fake_veo(model_key, prompt, duration, aspect_ratio, resolution,
                           target_path, first_frame=None, last_frame=None):
            calls["veo"] = (model_key, prompt, duration, aspect_ratio, resolution,
                            first_frame, last_frame)
            Path(target_path).write_bytes(b"BRIDGE")

        monkeypatch.setattr(mod, "_extract_frame", fake_extract)
        monkeypatch.setattr(mod, "_veo_generate", fake_veo)
        _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": 4,
            "model": "veo-3.1-fast", "aspect_ratio": "9:16",
            "prompt": "morph through ink",
        }))
        assert calls["extract"] == [(a, "last"), (b, "first")]
        model_key, prompt, duration, aspect, resolution, first, last = calls["veo"]
        assert (model_key, duration, aspect, resolution) == ("veo-3.1-fast", 4, "9:16", "720p")
        assert first == ("image/png", b"PNGlast")
        assert last == ("image/png", b"PNGfirst")
        assert "morph through ink" in prompt
        assert "via fal.ai" not in res.text
        assert "bridge clip only" in res.text
        assert "video-tools" in res.text

    def test_transition_veo_without_any_key(self, monkeypatch, tmp_path, clips):
        mod = _import_server_with_env(
            monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), RUNWAY_API_KEY="r-test",
        )
        a, b = clips
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": 8,
            "model": "veo-3.1-fast",
        }))
        assert "FAL_API_KEY or GOOGLE_AI_API_KEY" in res.text

    def test_transition_seedance_passes_style_through(self, mod, monkeypatch, clips):
        a, b = clips
        calls = {}

        async def fake_seedance(video_a, video_b, style, duration, aspect_ratio, target_path):
            calls["seed"] = (video_a, video_b, style, duration, aspect_ratio)
            Path(target_path).write_bytes(b"SEED")

        monkeypatch.setattr(mod, "_seedance_transition", fake_seedance)
        _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": 12,
            "model": "seedance", "prompt": "explode into confetti",
        }))
        # The endpoint-anchored template is composed inside _seedance_transition
        # (it references the uploaded junction frames) — the handler passes
        # only the style direction.
        assert calls["seed"] == (a, b, "explode into confetti", 12, "16:9")
        assert "Seedance 2.0" in res.text

    def test_edit_happy_path(self, mod, monkeypatch, clips, tmp_path):
        a, _ = clips
        calls = {}

        async def fake_edit(video_path, prompt, target_path):
            calls["edit"] = (video_path, prompt)
            Path(target_path).write_bytes(b"EDITED")

        monkeypatch.setattr(mod, "_runway_edit", fake_edit)
        player = _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_edit({
            "video_path": a, "prompt": "make it golden hour",
        }))
        assert calls["edit"] == (a, "make it golden hour")
        saved = player["played"][0]
        assert saved.startswith(str(tmp_path / "generated-assets" / "edit_"))
        assert "Runway Aleph 2.0" in res.text

    def test_hook_failure_still_returns_saved_path(self, mod, monkeypatch):
        async def fake_veo(model_key, prompt, duration, aspect_ratio, resolution,
                           target_path, first_frame=None, last_frame=None):
            Path(target_path).write_bytes(b"MP4")

        monkeypatch.setattr(mod, "_veo_generate", fake_veo)
        _stub_player(monkeypatch, mod, ok=False)
        (res,) = asyncio.run(mod._handle_generate({"prompt": "x", "duration_seconds": 4}))
        assert "Saved to:" in res.text
        assert "displayed" not in res.text


# ───────────────────────────── API error surfacing ──────────────────────────


def _http_error(status: int, body: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.test/v1")
    resp = httpx.Response(status, json=body or {}, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


class TestApiErrors:
    def test_401_points_at_the_google_key(self, mod, monkeypatch):
        async def fail(*a, **kw):
            raise _http_error(401)

        monkeypatch.setattr(mod, "_veo_generate", fail)
        (res,) = asyncio.run(mod._handle_generate({"prompt": "x", "duration_seconds": 4}))
        assert "rejected the request (401)" in res.text
        assert "GOOGLE_AI_API_KEY" in res.text

    def test_403_surfaces_provider_detail(self, mod, monkeypatch):
        # A 403 is not always a bad key — Google project-level denials carry
        # an explanatory message that must reach the agent verbatim.
        async def fail(*a, **kw):
            raise _http_error(403, {"error": {"message": "Your project has been denied access."}})

        monkeypatch.setattr(mod, "_veo_generate", fail)
        (res,) = asyncio.run(mod._handle_generate({"prompt": "x", "duration_seconds": 4}))
        assert "rejected the request (403)" in res.text
        assert "denied access" in res.text

    def test_runway_error_detail_surfaces(self, mod, monkeypatch, clips):
        a, _ = clips

        async def fail(*a_, **kw):
            raise _http_error(429, {"error": "rate limited"})

        monkeypatch.setattr(mod, "_runway_edit", fail)
        (res,) = asyncio.run(mod._handle_edit({"video_path": a, "prompt": "x"}))
        assert "Runway API error 429" in res.text
        assert "rate limited" in res.text

    def test_seedance_error_names_fal(self, mod, monkeypatch, clips):
        a, b = clips

        async def fail(*a_, **kw):
            raise _http_error(403)

        monkeypatch.setattr(mod, "_seedance_transition", fail)
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b,
            "duration_seconds": 8, "model": "seedance",
        }))
        assert "FAL_API_KEY" in res.text

    def test_no_file_written_on_api_error(self, mod, monkeypatch, tmp_path):
        async def fail(*a, **kw):
            raise _http_error(500)

        monkeypatch.setattr(mod, "_veo_generate", fail)
        asyncio.run(mod._handle_generate({"prompt": "x", "duration_seconds": 4}))
        assets = tmp_path / "generated-assets"
        assert not assets.exists() or not any(assets.glob("*.mp4"))


# ───────────────────────────── wire-level (per provider) ────────────────────


def _client_factory(handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient  # captured before the monkeypatch below

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    return factory


class TestVeoWire:
    def test_submit_poll_download(self, mod, monkeypatch, tmp_path):
        seen = {"polls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith(":predictLongRunning"):
                assert "veo-3.1-fast-generate-preview" in path
                assert request.headers["x-goog-api-key"] == "g-test"
                body = json.loads(request.content)
                seen["body"] = body
                return httpx.Response(200, json={"name": "models/veo/operations/op1"})
            if path.endswith("/operations/op1"):
                seen["polls"] += 1
                return httpx.Response(200, json={
                    "done": True,
                    "response": {"generateVideoResponse": {"generatedSamples": [
                        {"video": {"uri": "https://dl.test/video.mp4"}}
                    ]}},
                })
            if path == "/video.mp4":
                assert request.headers["x-goog-api-key"] == "g-test"
                return httpx.Response(200, content=b"VIDEOBYTES")
            raise AssertionError(f"unexpected URL: {request.url}")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        target = tmp_path / "out.mp4"
        asyncio.run(mod._veo_generate(
            "veo-3.1-fast", "a pond", 8, "16:9", "1080p", str(target),
        ))
        assert target.read_bytes() == b"VIDEOBYTES"
        assert not target.with_suffix(".mp4.part").exists()
        body = seen["body"]
        assert body["instances"] == [{"prompt": "a pond"}]
        assert body["parameters"] == {
            "aspectRatio": "16:9", "resolution": "1080p", "durationSeconds": 8,
        }

    def test_first_last_frame_uses_predict_image_shape(self, mod, monkeypatch, tmp_path):
        # predictLongRunning takes bytesBase64Encoded image parts — the
        # generateContent inlineData shape is rejected by the live API.
        import base64 as b64
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith(":predictLongRunning"):
                seen["instance"] = json.loads(request.content)["instances"][0]
                return httpx.Response(200, json={"name": "models/veo/operations/op1"})
            if path.endswith("/operations/op1"):
                return httpx.Response(200, json={
                    "done": True,
                    "response": {"generateVideoResponse": {"generatedSamples": [
                        {"video": {"uri": "https://dl.test/video.mp4"}}
                    ]}},
                })
            return httpx.Response(200, content=b"VIDEOBYTES")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        asyncio.run(mod._veo_generate(
            "veo-3.1", "bridge", 6, "16:9", "720p", str(tmp_path / "o.mp4"),
            first_frame=("image/png", b"AAA"), last_frame=("image/png", b"BBB"),
        ))
        inst = seen["instance"]
        assert inst["image"] == {"mimeType": "image/png",
                                "bytesBase64Encoded": b64.b64encode(b"AAA").decode()}
        assert inst["lastFrame"] == {"mimeType": "image/png",
                                     "bytesBase64Encoded": b64.b64encode(b"BBB").decode()}
        assert "inlineData" not in json.dumps(inst)

    def test_operation_error_raises(self, mod, monkeypatch, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(":predictLongRunning"):
                return httpx.Response(200, json={"name": "models/veo/operations/op1"})
            return httpx.Response(200, json={
                "done": True, "error": {"message": "safety block"},
            })

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        with pytest.raises(RuntimeError, match="safety block"):
            asyncio.run(mod._veo_generate(
                "veo-3.1", "x", 8, "16:9", "720p", str(tmp_path / "o.mp4"),
            ))


class TestRunwayWire:
    def test_upload_submit_poll_download(self, mod, monkeypatch, tmp_path, clips):
        a, _ = clips
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/uploads":
                assert request.headers["Authorization"] == "Bearer r-test"
                assert request.headers["X-Runway-Version"] == "2024-11-06"
                return httpx.Response(200, json={
                    "uploadUrl": "https://s3.test/put", "fields": {"k": "v"},
                    "runwayUri": "runway://uploads/u1",
                })
            if path == "/put":
                assert b"FAKEMP4A" in request.content  # multipart carries the file
                return httpx.Response(204)
            if path == "/v1/video_to_video":
                seen["submit"] = json.loads(request.content)
                return httpx.Response(200, json={"id": "task1"})
            if path == "/v1/tasks/task1":
                return httpx.Response(200, json={
                    "status": "SUCCEEDED", "output": ["https://cdn.test/out.mp4"],
                })
            if path == "/out.mp4":
                return httpx.Response(200, content=b"EDITEDBYTES")
            raise AssertionError(f"unexpected URL: {request.url}")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        target = tmp_path / "edit.mp4"
        asyncio.run(mod._runway_edit(a, "golden hour", str(target)))
        assert target.read_bytes() == b"EDITEDBYTES"
        assert seen["submit"] == {
            "model": "aleph2", "videoUri": "runway://uploads/u1",
            "promptText": "golden hour",
        }

    def test_failed_task_raises_with_reason(self, mod, monkeypatch, tmp_path, clips):
        a, _ = clips

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/uploads":
                return httpx.Response(200, json={
                    "uploadUrl": "https://s3.test/put", "fields": {},
                    "runwayUri": "runway://uploads/u1",
                })
            if path == "/put":
                return httpx.Response(204)
            if path == "/v1/video_to_video":
                return httpx.Response(200, json={"id": "task1"})
            return httpx.Response(200, json={
                "status": "FAILED", "failure": "content policy",
            })

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        with pytest.raises(RuntimeError, match="content policy"):
            asyncio.run(mod._runway_edit(a, "x", str(tmp_path / "o.mp4")))


class TestFalWire:
    def test_upload_submit_poll_download(self, mod, monkeypatch, tmp_path, clips):
        a, b = clips
        seen = {"uploads": []}

        async def fake_extract(video_path, position):
            return f"FRAME-{position}".encode()

        monkeypatch.setattr(mod, "_extract_frame", fake_extract)

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            path = request.url.path
            if path == "/storage/auth/token":
                assert request.headers["Authorization"] == "Key f-test"
                return httpx.Response(200, json={
                    "token": "tok", "token_type": "Bearer",
                    "base_url": "https://cdn.fal.test",
                })
            if path == "/files/upload":
                assert request.headers["Authorization"] == "Bearer tok"
                # Public-by-default uploads must always carry a bounded lifecycle.
                life = json.loads(request.headers["X-Fal-Object-Lifecycle"])
                assert life["expiration_duration_seconds"] == 86400
                seen["uploads"].append(request.headers["Content-Type"])
                n = len(seen["uploads"])
                return httpx.Response(200, json={
                    "access_url": f"https://cdn.fal.test/f{n}"
                })
            if url.startswith("https://queue.fal.run/bytedance/seedance-2.0/reference-to-video"):
                assert request.headers["Authorization"] == "Key f-test"
                seen["submit"] = json.loads(request.content)
                return httpx.Response(200, json={
                    "request_id": "r1",
                    "status_url": "https://queue.fal.run/bytedance/seedance-2.0/requests/r1/status",
                    "response_url": "https://queue.fal.run/bytedance/seedance-2.0/requests/r1/response",
                })
            if path.endswith("/r1/status"):
                return httpx.Response(200, json={"status": "COMPLETED"})
            if path.endswith("/r1/response"):
                return httpx.Response(200, json={
                    "video": {"url": "https://cdn.fal.test/out.mp4"}
                })
            if path == "/out.mp4":
                return httpx.Response(200, content=b"SEEDBYTES")
            raise AssertionError(f"unexpected URL: {request.url}")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        target = tmp_path / "seed.mp4"
        asyncio.run(mod._seedance_transition(
            a, b, "morph through spray", 10, "9:16", str(target),
        ))
        assert target.read_bytes() == b"SEEDBYTES"
        # Two videos + the two junction frames that anchor the endpoints.
        assert seen["uploads"] == ["video/mp4", "video/mp4", "image/png", "image/png"]
        submit = seen["submit"]
        assert submit["video_urls"] == ["https://cdn.fal.test/f1", "https://cdn.fal.test/f2"]
        assert submit["image_urls"] == ["https://cdn.fal.test/f3", "https://cdn.fal.test/f4"]
        assert "@Image1" in submit["prompt"] and "@Image2" in submit["prompt"]
        assert submit["prompt"].endswith("morph through spray")
        assert (submit["resolution"], submit["duration"], submit["aspect_ratio"]) == ("720p", 10, "9:16")
        assert submit["generate_audio"] is True

    def test_veo_flf_submit_poll_download(self, mod, monkeypatch, tmp_path, clips):
        a, b = clips
        seen = {"uploads": []}

        async def fake_extract(video_path, position):
            return f"FRAME-{position}".encode()

        monkeypatch.setattr(mod, "_extract_frame", fake_extract)

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            path = request.url.path
            if path == "/storage/auth/token":
                return httpx.Response(200, json={
                    "token": "tok", "token_type": "Bearer",
                    "base_url": "https://cdn.fal.test",
                })
            if path == "/files/upload":
                seen["uploads"].append(request.headers["Content-Type"])
                n = len(seen["uploads"])
                return httpx.Response(200, json={
                    "access_url": f"https://cdn.fal.test/f{n}"
                })
            if url.startswith("https://queue.fal.run/fal-ai/veo3.1/fast/first-last-frame-to-video"):
                assert request.headers["Authorization"] == "Key f-test"
                seen["submit"] = json.loads(request.content)
                return httpx.Response(200, json={
                    "request_id": "v1",
                    "status_url": "https://queue.fal.run/fal-ai/veo3.1/requests/v1/status",
                    "response_url": "https://queue.fal.run/fal-ai/veo3.1/requests/v1/response",
                })
            if path.endswith("/v1/status"):
                return httpx.Response(200, json={"status": "COMPLETED"})
            if path.endswith("/v1/response"):
                return httpx.Response(200, json={
                    "video": {"url": "https://cdn.fal.test/veo.mp4"}
                })
            if path == "/veo.mp4":
                return httpx.Response(200, content=b"VEOBYTES")
            raise AssertionError(f"unexpected URL: {request.url}")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        target = tmp_path / "veo_bridge.mp4"
        asyncio.run(mod._fal_veo_flf_transition(
            "veo-3.1-fast", a, b, "sweep over the bay", 8, "16:9", str(target),
        ))
        assert target.read_bytes() == b"VEOBYTES"
        # FLF uploads ONLY the two junction frames — no source-clip upload
        # (that's the seedance mode), so no 15s/50MB input cap applies here.
        assert seen["uploads"] == ["image/png", "image/png"]
        submit = seen["submit"]
        assert submit["first_frame_url"] == "https://cdn.fal.test/f1"
        assert submit["last_frame_url"] == "https://cdn.fal.test/f2"
        assert "video_urls" not in submit
        # fal's Veo duration is an enum string, not an int.
        assert submit["duration"] == "8s"
        # 1080p: same fal price as 720p, and a 720p bridge pops at a hard
        # cut into native-resolution footage.
        assert (submit["resolution"], submit["aspect_ratio"]) == ("1080p", "16:9")
        assert submit["generate_audio"] is True
        assert submit["prompt"].endswith("sweep over the bay")

    def test_veo_flf_urls_cover_both_tiers(self, mod):
        assert mod.FAL_VEO_FLF_URLS["veo-3.1"].endswith(
            "/fal-ai/veo3.1/first-last-frame-to-video")
        assert mod.FAL_VEO_FLF_URLS["veo-3.1-fast"].endswith(
            "/fal-ai/veo3.1/fast/first-last-frame-to-video")


class TestOmniWire:
    def test_inline_video_response(self, mod, monkeypatch, tmp_path):
        import base64 as b64
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1beta/interactions"
            assert request.headers["x-goog-api-key"] == "g-test"
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"steps": [{"content": [
                {"mime_type": "video/mp4", "data": b64.b64encode(b"OMNIBYTES").decode()}
            ]}]})

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        target = tmp_path / "omni.mp4"
        asyncio.run(mod._omni_generate("a pond", "16:9", str(target)))
        assert target.read_bytes() == b"OMNIBYTES"
        body = seen["body"]
        assert body["model"] == mod.OMNI_MODEL
        assert body["background"] is False and body["stream"] is False
        # URI delivery requires stored interactions (live-verified constraint).
        assert body["store"] is True
        assert body["generation_config"] == {"video_config": {"task": "text_to_video"}}
        assert body["response_format"]["type"] == "video"

    def test_file_uri_response_polls_then_downloads(self, mod, monkeypatch, tmp_path):
        states = iter(["PROCESSING", "ACTIVE"])
        monkeypatch.setattr(mod, "POLL_INTERVAL_SECONDS", 0.0)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1beta/interactions":
                return httpx.Response(200, json={"steps": [{"content": [
                    {"mime_type": "video/mp4",
                     "uri": "https://generativelanguage.googleapis.com/v1beta/files/abc:download?alt=media"}
                ]}]})
            if path == "/v1beta/files/abc":
                return httpx.Response(200, json={"state": next(states)})
            if path == "/v1beta/files/abc:download":
                assert request.headers["x-goog-api-key"] == "g-test"
                return httpx.Response(200, content=b"OMNIFILE")
            raise AssertionError(f"unexpected URL: {request.url}")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        target = tmp_path / "omni.mp4"
        asyncio.run(mod._omni_generate("a pond", "16:9", str(target)))
        assert target.read_bytes() == b"OMNIFILE"


# ───────────────────────────── frame extraction ─────────────────────────────


class TestFrameExtraction:
    def test_last_frame_falls_back_to_full_decode(self, mod, monkeypatch, clips):
        a, _ = clips
        calls = []

        async def fake_ffmpeg(args):
            calls.append(args)
            # First (windowed) attempt yields nothing; the full-decode
            # fallback produces the frame.
            if len(calls) == 2:
                Path(args[-1]).write_bytes(b"PNGDATA")
            return 0

        monkeypatch.setattr(mod, "_run_ffmpeg", fake_ffmpeg)
        frame = asyncio.run(mod._extract_frame(a, "last"))
        assert frame == b"PNGDATA"
        assert calls[0][:2] == ["-sseof", "-0.5"]
        assert "-sseof" not in calls[1]

    def test_unreadable_video_raises(self, mod, monkeypatch, clips):
        a, _ = clips

        async def fake_ffmpeg(args):
            return 1  # never writes the frame

        monkeypatch.setattr(mod, "_run_ffmpeg", fake_ffmpeg)
        with pytest.raises(RuntimeError, match="could not extract"):
            asyncio.run(mod._extract_frame(a, "first"))

    def test_real_ffmpeg_extracts_both_frames(self, mod, tmp_path):
        imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
        import subprocess

        clip = tmp_path / "real.mp4"
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1:r=12",
             "-pix_fmt", "yuv420p", "-y", str(clip)],
            check=True,
        )
        first = asyncio.run(mod._extract_frame(str(clip), "first"))
        last = asyncio.run(mod._extract_frame(str(clip), "last"))
        assert first.startswith(b"\x89PNG")
        assert last.startswith(b"\x89PNG")


# ───────────────────────────── polling budget ───────────────────────────────


class TestPolling:
    def test_poll_budget_exhaustion_raises(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "POLL_BUDGET_SECONDS", 0.0)
        monkeypatch.setattr(mod, "POLL_INTERVAL_SECONDS", 0.0)

        async def never_done():
            return None

        with pytest.raises(RuntimeError, match="did not finish within"):
            asyncio.run(mod._poll_until(never_done, "test job"))
