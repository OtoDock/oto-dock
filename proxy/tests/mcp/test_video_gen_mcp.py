"""Unit tests for video-gen-mcp (Omni Flash generation, Veo 3.1 through fal.ai,
transitions, Runway Aleph 2.0 editing, Seedance premium transitions).

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
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

MCP_DIR = CUSTOM_MCPS / "video-gen-mcp"

ALL_KEYS = ("GOOGLE_AI_API_KEY", "RUNWAY_API_KEY", "FAL_API_KEY")
VEO_REFUSAL = (
    "Error: Veo runs only through fal.ai since Google shut it down on its own API. "
    "Ask an admin to set FAL_API_KEY in the video-gen-mcp admin page, or use model omni-flash."
)


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
            "tool_timeout_sec": 600,   # a generation past Codex's 60 s default
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

    def test_explicit_assignment_and_meeting_exclusion(self, manifest):
        assert manifest["assignment_mode"] == "explicit"
        assert manifest["exclude_from"] == ["meeting"]

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

    @pytest.mark.parametrize(("args", "amount"), [
        ({"model": "omni-flash"}, 0.07 * 8),
        ({"model": "omni-flash", "resolution": "1080p"}, 0.104 * 8),
        ({"model": "omni-flash", "resolution": "4k"}, 0.209 * 8),
        ({}, 0.07 * 8),
        ({"resolution": "1080p"}, 0.104 * 8),
        ({"resolution": "4k"}, 0.209 * 8),
        ({"model": "veo-3.1-fast"}, 0.15 * 8),
        ({"model": "veo-3.1-fast", "resolution": "1080p"}, 0.15 * 8),
        ({"model": "veo-3.1-fast", "resolution": "4k"}, 0.35 * 8),
        ({"model": "veo-3.1"}, 0.40 * 8),
        ({"model": "veo-3.1", "resolution": "4k"}, 0.60 * 8),
    ])
    def test_generate_video_rows(self, manifest, args, amount):
        # Google's Omni rate ($12 per million tokens at 5,792 tokens a second
        # of 720p) and fal's Veo rates with audio on; the default is Omni.
        from services.mcp import mcp_cost_engine
        from services.mcp.mcp_registry import _parse_costs_block
        block = _parse_costs_block(manifest["costs"], "video-gen-mcp")
        hit = mcp_cost_engine.evaluate(
            "video-gen-mcp", "generate_video", {"prompt": "p", "duration_seconds": 8, **args}, block)
        assert hit is not None and hit.amount == pytest.approx(amount)

    def test_identity_names_the_current_models(self, manifest):
        assert manifest["version"] == "1.2.1"
        assert "Veo 3.1 through fal.ai" in manifest["description"]
        fal = next(f for f in manifest["instances"]["fields"] if f["key"] == "FAL_API_KEY")
        assert fal["label"] == "fal.ai API Key (for Veo and Seedance)"

    def test_costs_catch_alls_cover_omitted_model(self, manifest):
        # An absent match key never matches, so each generation tool needs a
        # final catch-all rule (priced at its default model) or an omitted
        # `model` arg records no cost at all.
        rules = manifest["costs"]["rules"]
        for tool, default_amount in (("generate_video", 0.07), ("generate_transition", 0.1814)):
            tool_rules = [r for r in rules if r["tool"] == tool]
            assert tool_rules[-1].get("match", {}) == {}
            assert tool_rules[-1]["amount"] == default_amount
            # Catch-all last — first-match-wins would otherwise shadow the
            # per-model and resolution-only prices.
            assert all(r.get("match") or r.get("result_has") for r in tool_rules[:-1])

    @pytest.mark.parametrize(("args", "answer", "amount"), [
        ({"model": "omni-flash"}, "Generated 6s transition with Gemini Omni Flash.", 0.07 * 6),
        ({}, "Generated 6s transition with Gemini Omni Flash.", 0.07 * 6),
        ({}, "Generated 6s transition with Seedance 2.0.", 0.1814 * 6),
        ({"model": "seedance"}, "Generated 6s transition with Seedance 2.0.", 0.1814 * 6),
        ({"model": "veo-3.1-fast"}, "Generated 6s transition with Veo 3.1 Fast via fal.ai.", 0.15 * 6),
        ({"model": "veo-3.1"}, "Generated 6s transition with Veo 3.1 via fal.ai.", 0.40 * 6),
    ])
    def test_transition_rows(self, manifest, args, answer, amount):
        # A transition that names no model runs Seedance with the fal.ai key and
        # Omni Flash without it, so its price follows the model the answer names.
        from services.mcp import mcp_cost_engine
        from services.mcp.mcp_registry import _parse_costs_block
        block = _parse_costs_block(manifest["costs"], "video-gen-mcp")
        hit = mcp_cost_engine.evaluate(
            "video-gen-mcp", "generate_transition",
            {"video_a_path": "a", "video_b_path": "b", "duration_seconds": 6, **args},
            block, tool_result=answer)
        assert hit is not None and hit.amount == pytest.approx(amount)

    def test_the_listing_is_the_same_with_any_keys(self, monkeypatch, tmp_path):
        bare = _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path))
        bare_tools = [t.model_dump() for t in asyncio.run(bare.list_tools())]
        full = _import_server_with_env(
            monkeypatch, VIDEO_SAVE_DIR=str(tmp_path),
            GOOGLE_AI_API_KEY="g-test", RUNWAY_API_KEY="r-test", FAL_API_KEY="f-test")
        assert [t.model_dump() for t in asyncio.run(full.list_tools())] == bare_tools


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

    @pytest.mark.parametrize("duration", [2, 16, 0])
    def test_generate_omni_duration_range(self, mod, duration):
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": duration, "model": "omni-flash"}
        ))
        assert res.text == (
            f"Error: duration_seconds must be between 3 and 15 for omni-flash (got {duration})."
        )

    def test_generate_veo_without_fal_key_is_refused(self, monkeypatch, tmp_path):
        mod = _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), GOOGLE_AI_API_KEY="g-test")
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": 8, "model": "veo-3.1-fast"}
        ))
        # "Error" first: a refused call is never charged.
        assert res.text.startswith("Error: Veo runs only through fal.ai")
        assert "FAL_API_KEY" in res.text and "omni-flash" in res.text

    def test_generate_veo_needs_no_google_key(self, monkeypatch, tmp_path):
        mod = _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), FAL_API_KEY="f-test")
        seen = {}

        async def fake_fal(model, prompt, duration, aspect, resolution, path, image=None):
            seen["model"] = model
            Path(path).write_bytes(b"mp4")  # noqa: ASYNC240  # a test's own temporary files

        monkeypatch.setattr(mod, "_fal_veo_generate", fake_fal)
        _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": 8, "model": "veo-3.1"}
        ))
        assert seen == {"model": "veo-3.1"}
        assert "with Veo 3.1 via fal.ai" in res.text

    def test_veo_is_always_listed_and_its_refusal_names_the_fal_key(self, monkeypatch, tmp_path):
        # The saved tool listing is keyed by the environment before secrets, so
        # a listing that hid Veo without the fal.ai key would stay hidden after
        # an admin sets it. Veo is listed always, and a call without the key
        # reaches the tool's own sentence through the argument check.
        mod = _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), GOOGLE_AI_API_KEY="g-test")
        tools = {t.name: t for t in asyncio.run(mod.list_tools())}
        gen = tools["generate_video"].inputSchema["properties"]["model"]
        assert gen["enum"] == ["omni-flash", "veo-3.1-fast", "veo-3.1"]
        assert gen["default"] == "omni-flash"
        transition = tools["generate_transition"].inputSchema["properties"]["model"]
        assert transition["enum"] == ["seedance", "omni-flash", "veo-3.1-fast", "veo-3.1"]
        # Its default hangs on the fal.ai key, so the listing names none.
        assert "default" not in transition
        out = asyncio.run(mod.call_tool("generate_video", {
            "prompt": "x", "duration_seconds": 8, "model": "veo-3.1-fast"}))
        assert out[0].text == VEO_REFUSAL
        a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
        a.write_bytes(b"A")
        b.write_bytes(b"B")
        out = asyncio.run(mod.call_tool("generate_transition", {
            "video_a_path": str(a), "video_b_path": str(b), "duration_seconds": 8, "model": "veo-3.1"}))
        assert out[0].text.startswith("Error: Veo transitions (fal.ai) not configured.")

    def test_over_the_wire_an_agent_reads_the_fal_sentence(self, tmp_path):
        # The engines reach the tool over stdio: list, then call, as they do.
        import os
        import sys

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = {"PATH": os.environ.get("PATH", ""), "VIDEO_SAVE_DIR": str(tmp_path),
               "GOOGLE_AI_API_KEY": "g-test"}
        params = StdioServerParameters(command=sys.executable, args=[str(MCP_DIR / "server.py")],
                                       env=env, cwd=str(MCP_DIR))

        async def run():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    called = await session.call_tool("generate_video", {
                        "prompt": "x", "duration_seconds": 8, "model": "veo-3.1-fast"})
                    return listed, called
        listed, called = asyncio.run(run())
        gen = next(t for t in listed.tools if t.name == "generate_video")
        assert "veo-3.1-fast" in gen.inputSchema["properties"]["model"]["enum"]
        assert called.content[0].text == VEO_REFUSAL

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

    @pytest.mark.parametrize(("duration", "ok"), [(2, False), (3, True), (10, True), (11, False)])
    def test_transition_omni_duration_range(self, mod, monkeypatch, clips, duration, ok):
        # Google: "First and last frame interpolation supports video durations
        # from 3 to 10 seconds."
        async def fake_omni(*a):
            Path(a[-1]).write_bytes(b"BRIDGE")  # noqa: ASYNC240  # a test's own temporary files

        monkeypatch.setattr(mod, "_omni_transition", fake_omni)
        _stub_player(monkeypatch, mod)
        a, b = clips
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": duration, "model": "omni-flash"}))
        if ok:
            assert res.text.startswith(f"Generated {duration}s transition with Gemini Omni Flash.")
        else:
            assert res.text == (f"Error: duration_seconds must be between 3 and 10 for omni-flash "
                                f"(got {duration}).")

    def test_transition_omni_without_google_key(self, monkeypatch, tmp_path, clips):
        mod = _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), FAL_API_KEY="f-test")
        a, b = clips
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": 6, "model": "omni-flash"}))
        assert res.text.startswith("Error: Omni Flash transitions (Google AI) not configured.")
        assert "GOOGLE_AI_API_KEY" in res.text

    def test_transition_with_no_key_at_all_names_the_google_key(self, monkeypatch, tmp_path, clips):
        mod = _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path))
        a, b = clips
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": 6}))
        assert "GOOGLE_AI_API_KEY" in res.text

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
    def test_default_model_is_omni_flash(self, mod, monkeypatch, tmp_path):
        # Google shut Veo down on its own API and names Gemini Omni Flash as
        # its replacement there: an omitted model lands on omni-flash.
        calls = {}

        async def fake_omni(prompt, aspect_ratio, duration, resolution, target_path, image=None):
            calls["omni"] = (prompt, aspect_ratio, duration, resolution, image)
            Path(target_path).write_bytes(b"MP4DATA")  # noqa: ASYNC240  # a test's own temporary files

        monkeypatch.setattr(mod, "_omni_generate", fake_omni)
        player = _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_generate({
            "prompt": "a paper boat on a pond", "duration_seconds": 6,
        }))
        # The duration is a parameter now: the prompt goes as written.
        assert calls["omni"] == ("a paper boat on a pond", "16:9", 6, "720p", None)
        saved = player["played"][0]
        assert saved.startswith(str(tmp_path / "generated-assets" / "video_"))
        assert Path(saved).read_bytes() == b"MP4DATA"
        assert res.text.startswith("Generated 6s video with Gemini Omni Flash (720p, 16:9).")
        assert "Video player displayed to user." in res.text

    def test_omni_takes_high_resolutions_and_an_image(self, mod, monkeypatch, tmp_path):
        calls = {}

        async def fake_omni(prompt, aspect_ratio, duration, resolution, target_path, image=None):
            calls["omni"] = (duration, resolution, image)
            Path(target_path).write_bytes(b"MP4DATA")  # noqa: ASYNC240  # a test's own temporary files

        monkeypatch.setattr(mod, "_omni_generate", fake_omni)
        _stub_player(monkeypatch, mod)
        img = tmp_path / "still.png"
        img.write_bytes(b"PNGDATA")
        (res,) = asyncio.run(mod._handle_generate({
            "prompt": "x", "duration_seconds": 15, "model": "omni-flash",
            "resolution": "4k", "image_path": str(img),
        }))
        assert calls["omni"] == (15, "4k", ("image/png", b"PNGDATA"))
        assert "from image" in res.text

    def test_veo_image_to_video_goes_through_fal(self, mod, monkeypatch, tmp_path):
        calls = {}

        async def fake_fal(model, prompt, duration, aspect_ratio, resolution, target_path, image=None):
            calls["veo"] = (model, prompt, duration, aspect_ratio, resolution, image)
            Path(target_path).write_bytes(b"VEODATA")  # noqa: ASYNC240  # a test's own temporary files

        monkeypatch.setattr(mod, "_fal_veo_generate", fake_fal)
        _stub_player(monkeypatch, mod)
        img = tmp_path / "still.jpg"
        img.write_bytes(b"JPGDATA")
        (res,) = asyncio.run(mod._handle_generate({
            "prompt": "camera pushes in", "duration_seconds": 8,
            "model": "veo-3.1", "aspect_ratio": "9:16", "resolution": "1080p",
            "image_path": str(img), "save_path": "launch/hero.mp4",
        }))
        assert calls["veo"] == ("veo-3.1", "camera pushes in", 8, "9:16", "1080p",
                                ("image/jpeg", b"JPGDATA"))
        assert f"Saved to: {tmp_path / 'launch' / 'hero.mp4'}" in res.text
        assert "with Veo 3.1 via fal.ai" in res.text
        assert "from image" in res.text

    def test_transition_veo_routes_via_fal_when_key_present(self, mod, monkeypatch, clips):
        """With a FAL key configured, Veo transitions take the fal route —
        no regional first/last-frame gate there (Google direct blocks EEA keys)."""
        a, b = clips
        calls = {}

        async def fake_flf(model, video_a, video_b, style, duration, aspect_ratio,
                           target_path):
            calls["flf"] = (model, video_a, video_b, style, duration, aspect_ratio)
            Path(target_path).write_bytes(b"BRIDGE")  # noqa: ASYNC240  # a test's own temporary files

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

    def test_transition_veo_without_fal_key_is_refused(self, monkeypatch, tmp_path, clips):
        # Google's own Veo route is gone; a Google key alone runs no Veo.
        mod = _import_server_with_env(
            monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), GOOGLE_AI_API_KEY="g-test",
            RUNWAY_API_KEY="r-test",
        )
        a, b = clips
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": 8,
            "model": "veo-3.1-fast",
        }))
        assert res.text.startswith("Error: Veo transitions (fal.ai) not configured.")
        assert "FAL_API_KEY" in res.text
        assert "GOOGLE_AI_API_KEY" not in res.text

    def test_transition_seedance_passes_style_through(self, mod, monkeypatch, clips):
        a, b = clips
        calls = {}

        async def fake_seedance(video_a, video_b, style, duration, aspect_ratio, target_path):
            calls["seed"] = (video_a, video_b, style, duration, aspect_ratio)
            Path(target_path).write_bytes(b"SEED")  # noqa: ASYNC240  # a test's own temporary files

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
            Path(target_path).write_bytes(b"EDITED")  # noqa: ASYNC240  # a test's own temporary files

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
        async def fake_omni(prompt, aspect_ratio, duration, resolution, target_path, image=None):
            Path(target_path).write_bytes(b"MP4")  # noqa: ASYNC240  # a test's own temporary files

        monkeypatch.setattr(mod, "_omni_generate", fake_omni)
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

        monkeypatch.setattr(mod, "_omni_generate", fail)
        (res,) = asyncio.run(mod._handle_generate({"prompt": "x", "duration_seconds": 4}))
        assert "rejected the request (401)" in res.text
        assert "GOOGLE_AI_API_KEY" in res.text

    def test_veo_error_names_fal(self, mod, monkeypatch):
        async def fail(*a, **kw):
            raise _http_error(401)

        monkeypatch.setattr(mod, "_fal_veo_generate", fail)
        (res,) = asyncio.run(mod._handle_generate(
            {"prompt": "x", "duration_seconds": 4, "model": "veo-3.1-fast"}))
        assert res.text.startswith("Error: fal.ai rejected the request (401)")
        assert "FAL_API_KEY" in res.text and "GOOGLE_AI_API_KEY" not in res.text

    def test_403_surfaces_provider_detail(self, mod, monkeypatch):
        # A 403 is not always a bad key — Google project-level denials carry
        # an explanatory message that must reach the agent verbatim.
        async def fail(*a, **kw):
            raise _http_error(403, {"error": {"message": "Your project has been denied access."}})

        monkeypatch.setattr(mod, "_omni_generate", fail)
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

    def test_an_omni_transition_error_names_the_google_key(self, mod, monkeypatch, clips):
        a, b = clips

        async def fail(*a_, **kw):
            raise _http_error(401)

        monkeypatch.setattr(mod, "_omni_transition", fail)
        (res,) = asyncio.run(mod._handle_transition({
            "video_a_path": a, "video_b_path": b, "duration_seconds": 6, "model": "omni-flash"}))
        assert "GOOGLE_AI_API_KEY" in res.text and "FAL_API_KEY" not in res.text

    def test_no_file_written_on_api_error(self, mod, monkeypatch, tmp_path):
        async def fail(*a, **kw):
            raise _http_error(500)

        monkeypatch.setattr(mod, "_omni_generate", fail)
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


def _fal_veo_handler(seen: dict, queue_url: str):
    """fal's storage, queue and download, recording the upload and the submit."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if path == "/storage/auth/token":
            return httpx.Response(200, json={
                "token": "tok", "token_type": "Bearer", "base_url": "https://cdn.fal.test",
            })
        if path == "/files/upload":
            seen.setdefault("uploads", []).append(
                (request.headers["Content-Type"], request.headers["X-Fal-File-Name"]))
            return httpx.Response(200, json={"access_url": "https://cdn.fal.test/src"})
        if url == queue_url:
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
            return httpx.Response(200, json={"video": {"url": "https://cdn.fal.test/veo.mp4"}})
        if path == "/veo.mp4":
            return httpx.Response(200, content=b"VEOBYTES")
        raise AssertionError(f"unexpected URL: {request.url}")
    return handler


class TestFalVeoGenerationWire:
    def test_text_to_video(self, mod, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(
            _fal_veo_handler(seen, "https://queue.fal.run/fal-ai/veo3.1/fast")))
        target = tmp_path / "out.mp4"
        asyncio.run(mod._fal_veo_generate("veo-3.1-fast", "a pond", 6, "9:16", "4k", str(target)))
        assert target.read_bytes() == b"VEOBYTES"
        assert "uploads" not in seen
        assert seen["submit"] == {
            "prompt": "a pond", "duration": "6s", "resolution": "4k", "aspect_ratio": "9:16",
            "generate_audio": True, "auto_fix": False,
        }

    def test_image_to_video_uploads_the_image(self, mod, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(
            _fal_veo_handler(seen, "https://queue.fal.run/fal-ai/veo3.1/image-to-video")))
        target = tmp_path / "out.mp4"
        asyncio.run(mod._fal_veo_generate("veo-3.1", "push in", 8, "16:9", "1080p", str(target),
                                          image=("image/jpeg", b"JPG")))
        assert target.read_bytes() == b"VEOBYTES"
        assert seen["uploads"] == [("image/jpeg", "source.jpg")]
        assert seen["submit"]["image_url"] == "https://cdn.fal.test/src"
        assert seen["submit"]["duration"] == "8s"

    def test_routes_cover_both_tiers_and_three_modes(self, mod):
        for model, tier in (("veo-3.1", "fal-ai/veo3.1"), ("veo-3.1-fast", "fal-ai/veo3.1/fast")):
            urls = mod.FAL_VEO_URLS[model]
            assert urls["text"] == f"https://queue.fal.run/{tier}"
            assert urls["image"] == f"https://queue.fal.run/{tier}/image-to-video"
            assert urls["frames"] == f"https://queue.fal.run/{tier}/first-last-frame-to-video"


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
        asyncio.run(mod._omni_generate("a pond", "16:9", 12, "1080p", str(target)))
        assert target.read_bytes() == b"OMNIBYTES"
        body = seen["body"]
        assert body["model"] == "gemini-omni-1.2-flash"
        assert body["background"] is False and body["stream"] is False
        # URI delivery requires stored interactions (live-verified constraint).
        assert body["store"] is True
        # Google: set `task` only as a fallback; text alone carries none.
        assert "generation_config" not in body
        assert body["input"] == [{"type": "text", "text": "a pond"}]
        assert body["response_format"] == {
            "type": "video", "aspect_ratio": "16:9", "delivery": "uri",
            "duration": "12s", "resolution": "1080p",
        }

    def test_an_image_pins_image_to_video(self, mod, monkeypatch, tmp_path):
        import base64 as b64
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"steps": [{"content": [
                {"mime_type": "video/mp4", "data": b64.b64encode(b"OMNIBYTES").decode()}
            ]}]})

        monkeypatch.setattr(mod.httpx, "AsyncClient", _client_factory(handler))
        asyncio.run(mod._omni_generate("move", "9:16", 4, "720p", str(tmp_path / "o.mp4"),
                                       image=("image/png", b"PNG")))
        body = seen["body"]
        assert body["generation_config"] == {"video_config": {"task": "image_to_video"}}
        assert body["input"][1] == {"type": "image", "data": b64.b64encode(b"PNG").decode(),
                                    "mime_type": "image/png"}

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
        asyncio.run(mod._omni_generate("a pond", "16:9", 8, "720p", str(target)))
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
                Path(args[-1]).write_bytes(b"PNGDATA")  # noqa: ASYNC240  # a test's own temporary files
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


# ───────────────────────────── loopback stand-in ─────────────────────────────
# The whole handler over real HTTP against a stand-in on 127.0.0.1 that answers
# like the vendors' documentation: Google's Interactions API with URI delivery
# (`steps[].content[]` with `uri`, the file polled to ACTIVE, then downloaded)
# and fal's queue (submit, status, response `{video: {url}}`, storage upload).


class _VideoStandIn(BaseHTTPRequestHandler):
    def log_message(self, *args):  # the test output stays quiet
        pass

    def _answer(self, status=200, body=None, raw=None):
        out = raw if raw is not None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _record(self, body=b""):
        self.server.seen.append({"method": self.command, "path": self.path,
                                 "headers": dict(self.headers), "body": body})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._record(body)
        base = self.server.base
        if self.path == "/v1beta/interactions":
            return self._answer(body={"steps": [{"type": "model_output", "content": [
                {"type": "video", "mime_type": "video/mp4",
                 "uri": f"{base}/v1beta/files/abc:download?alt=media"}]}],
                "status": "completed", "model": "gemini-omni-1.2-flash", "object": "interaction"})
        if self.path == "/storage/auth/token?storage_type=fal-cdn-v3":
            return self._answer(body={"token": "t", "token_type": "Bearer", "base_url": base})
        if self.path == "/files/upload":
            return self._answer(body={"access_url": f"{base}/cdn/source"})
        if self.path.startswith("/fal-ai/veo3.1"):
            return self._answer(body={"request_id": "r1", "status_url": f"{base}/requests/r1/status",
                                      "response_url": f"{base}/requests/r1"})
        return self._answer(404, {"error": "not found"})

    def do_GET(self):
        self._record()
        base = self.server.base
        if self.path == "/v1beta/files/abc":
            return self._answer(body={"name": "files/abc", "state": "ACTIVE"})
        if self.path == "/v1beta/files/abc:download?alt=media":
            return self._answer(raw=b"OMNI-MP4")
        if self.path == "/requests/r1/status":
            return self._answer(body={"status": "COMPLETED"})
        if self.path == "/requests/r1":
            return self._answer(body={"video": {"url": f"{base}/cdn/veo.mp4"}})
        if self.path == "/cdn/veo.mp4":
            return self._answer(raw=b"VEO-MP4")
        return self._answer(404, {"error": "not found"})


@pytest.fixture()
def video_standin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VideoStandIn)
    server.seen = []
    server.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class TestLoopbackStandIn:
    def test_the_default_call_is_omni_flash(self, mod, monkeypatch, tmp_path, video_standin):
        monkeypatch.setattr(mod, "GOOGLE_BASE", video_standin.base)
        monkeypatch.setattr(mod, "POLL_INTERVAL_SECONDS", 0.0)
        _stub_player(monkeypatch, mod)
        (res,) = asyncio.run(mod._handle_generate({"prompt": "a pond at dawn", "duration_seconds": 12,
                                                   "resolution": "1080p", "aspect_ratio": "9:16"}))
        assert res.text.startswith("Generated 12s video with Gemini Omni Flash (1080p, 9:16).")
        submit = video_standin.seen[0]
        assert (submit["method"], submit["path"]) == ("POST", "/v1beta/interactions")
        assert submit["headers"]["x-goog-api-key"] == "g-test"
        body = json.loads(submit["body"])
        assert body["model"] == "gemini-omni-1.2-flash"
        assert body["response_format"] == {"type": "video", "aspect_ratio": "9:16", "delivery": "uri",
                                           "duration": "12s", "resolution": "1080p"}
        saved = sorted((tmp_path / "generated-assets").glob("video_*.mp4"))
        assert len(saved) == 1 and saved[0].read_bytes() == b"OMNI-MP4"

    def _transition(self, mod, monkeypatch, video_standin, clips, **args):
        monkeypatch.setattr(mod, "GOOGLE_BASE", video_standin.base)
        monkeypatch.setattr(mod, "POLL_INTERVAL_SECONDS", 0.0)
        _stub_player(monkeypatch, mod)

        async def fake_extract(path, position):
            return f"{Path(path).stem}-{position}".encode()

        monkeypatch.setattr(mod, "_extract_frame", fake_extract)
        a, b = clips
        (res,) = asyncio.run(mod._handle_transition(
            {"video_a_path": a, "video_b_path": b, "duration_seconds": 6, **args}))
        return res

    def test_a_transition_without_the_fal_key_runs_on_omni_flash(
            self, monkeypatch, tmp_path, clips, video_standin):
        mod = _import_server_with_env(monkeypatch, VIDEO_SAVE_DIR=str(tmp_path), GOOGLE_AI_API_KEY="g-test")
        res = self._transition(mod, monkeypatch, video_standin, clips, aspect_ratio="9:16",
                               prompt="through the mist")
        assert res.text.startswith("Generated 6s transition with Gemini Omni Flash.")
        submit = video_standin.seen[0]
        assert (submit["method"], submit["path"]) == ("POST", "/v1beta/interactions")
        assert submit["headers"]["x-goog-api-key"] == "g-test"
        body = json.loads(submit["body"])
        assert body["model"] == "gemini-omni-1.2-flash"
        # Google: "Provide two images in the input list and describe the
        # desired transition in your prompt": the first clip's last frame,
        # the second clip's first frame, then the words. No `task`: none
        # names interpolation, and Google says to set one only as a fallback.
        first, last, words = body["input"]
        assert first == {"type": "image", "mime_type": "image/png",
                         "data": base64.b64encode(b"a-last").decode()}
        assert last == {"type": "image", "mime_type": "image/png",
                        "data": base64.b64encode(b"b-first").decode()}
        assert words["type"] == "text" and words["text"].endswith(" through the mist")
        assert "generation_config" not in body
        assert body["response_format"] == {"type": "video", "aspect_ratio": "9:16", "delivery": "uri",
                                           "duration": "6s", "resolution": "720p"}
        saved = sorted((tmp_path / "generated-assets").glob("transition_*.mp4"))
        assert len(saved) == 1 and saved[0].read_bytes() == b"OMNI-MP4"
        # The answer carries the words the manifest's price for it reads.
        manifest = json.loads((MCP_DIR / "manifest.json").read_text())
        (row,) = [r for r in manifest["costs"]["rules"]
                  if r["tool"] == "generate_transition" and r.get("result_has")]
        assert row["result_has"] in res.text and row["amount"] == 0.07

    def test_omni_flash_named_runs_on_it_with_the_fal_key_too(self, mod, monkeypatch, clips, video_standin):
        res = self._transition(mod, monkeypatch, video_standin, clips, model="omni-flash")
        assert res.text.startswith("Generated 6s transition with Gemini Omni Flash.")
        assert [r["path"] for r in video_standin.seen][0] == "/v1beta/interactions"

    def test_a_transition_with_the_fal_key_stays_on_seedance(self, mod, monkeypatch, clips, video_standin):
        calls = {}

        async def fake_seedance(video_a, video_b, style, duration, aspect_ratio, target_path):
            calls["seed"] = duration
            Path(target_path).write_bytes(b"SEED")  # noqa: ASYNC240  # a test's own temporary files

        monkeypatch.setattr(mod, "_seedance_transition", fake_seedance)
        res = self._transition(mod, monkeypatch, video_standin, clips)
        assert calls == {"seed": 6} and video_standin.seen == []
        assert res.text.startswith("Generated 6s transition with Seedance 2.0.")

    def test_veo_fast_runs_through_fal(self, mod, monkeypatch, tmp_path, video_standin):
        base = video_standin.base
        monkeypatch.setattr(mod, "FAL_REST_BASE", base)
        monkeypatch.setattr(mod, "FAL_VEO_URLS", {
            m: {k: v.replace("https://queue.fal.run", base) for k, v in routes.items()}
            for m, routes in mod.FAL_VEO_URLS.items()})
        monkeypatch.setattr(mod, "POLL_INTERVAL_SECONDS", 0.0)
        _stub_player(monkeypatch, mod)
        img = tmp_path / "still.png"
        img.write_bytes(b"PNG")
        (res,) = asyncio.run(mod._handle_generate({"prompt": "push in", "duration_seconds": 6,
                                                   "model": "veo-3.1-fast", "image_path": str(img)}))
        assert res.text.startswith("Generated 6s video with Veo 3.1 Fast via fal.ai (720p, 16:9, from image).")
        submit = next(r for r in video_standin.seen if r["path"].startswith("/fal-ai/"))
        assert submit["path"] == "/fal-ai/veo3.1/fast/image-to-video"
        assert submit["headers"]["Authorization"] == "Key f-test"
        assert json.loads(submit["body"]) == {
            "prompt": "push in", "duration": "6s", "resolution": "720p", "aspect_ratio": "16:9",
            "generate_audio": True, "auto_fix": False, "image_url": f"{base}/cdn/source",
        }
        saved = sorted((tmp_path / "generated-assets").glob("video_*.mp4"))
        assert len(saved) == 1 and saved[0].read_bytes() == b"VEO-MP4"
