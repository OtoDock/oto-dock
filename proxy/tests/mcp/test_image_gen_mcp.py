"""Unit tests for image-gen-mcp: the requests it sends for Nano Banana Pro and
GPT Image 2.6, their prices, and the names it shows.

The wire tests run the tool in hosted mode against a stand-in on loopback (a
threaded ``http.server`` on 127.0.0.1) that records each request and answers
with the vendor's documented answer: OpenAI's Image API returns
``data[].b64_json``, Gemini's ``generateContent`` returns ``inlineData``
parts. No vendor is ever called.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from services.mcp import mcp_cost_engine
from services.mcp.mcp_registry import _parse_costs_block
from tests._paths import CUSTOM_MCPS, load_mcp_server

MCP_DIR = CUSTOM_MCPS / "image-gen-mcp"
RELAY_TOKEN = "relay-token-for-tests"


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (10, 20, 30, 128)).save(buf, format="PNG")
    return buf.getvalue()


PNG = _png()
PNG_B64 = base64.b64encode(PNG).decode()


class _StandIn(BaseHTTPRequestHandler):
    """Answers like the vendors' documented responses, records every request."""

    seen: list[dict]

    def log_message(self, *args):  # the test output stays quiet
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        url = urlsplit(self.path)
        self.server.seen.append({
            "path": url.path, "query": url.query, "headers": dict(self.headers), "body": body,
        })
        if url.path.endswith("/v1/images/generations"):
            n = json.loads(body).get("n", 1)
            answer = {"created": 1, "data": [{"b64_json": PNG_B64}] * n}
        elif url.path.endswith("/v1/images/edits"):
            answer = {"created": 1, "data": [{"b64_json": PNG_B64}]}
        elif url.path.endswith(":generateContent"):
            answer = {"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": PNG_B64}},
            ]}}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        out = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture()
def standin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StandIn)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def mod(monkeypatch, tmp_path, standin):
    """The server module in hosted mode, its relay base pointed at the stand-in."""
    for key in ("GOOGLE_AI_API_KEY", "OPENAI_IMAGE_API_KEY", "PROXY_URL", "OTO_SESSION_ID",
                "OTODOCK_RELAY_ERROR"):
        monkeypatch.delenv(key, raising=False)
    host, port = standin.server_address
    monkeypatch.setenv("OTODOCK_RELAY_BASE", f"http://{host}:{port}/v1/relay")
    monkeypatch.setenv("OTODOCK_RELAY_TOKEN", RELAY_TOKEN)
    monkeypatch.setenv("IMAGE_SAVE_DIR", str(tmp_path))
    return load_mcp_server(MCP_DIR)


def _call(mod, name, args):
    return asyncio.run(mod.call_tool(name, args))


def _multipart(request: dict) -> dict[str, bytes]:
    raw = (f"Content-Type: {request['headers']['Content-Type']}\r\n\r\n").encode() + request["body"]
    msg = BytesParser(policy=HTTP).parsebytes(raw)
    return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in msg.iter_parts()}


# ───────────────────────────── GPT Image 2.6 ─────────────────────────────────


class TestGptImageWire:
    def test_standard_runs_flare_at_medium(self, mod, standin, tmp_path):
        out = _call(mod, "generate_image", {
            "prompt": "a lighthouse", "model": "gpt-image", "aspect_ratio": "16:9", "num_images": 2,
        })
        assert out[0].text.startswith("Generated 2 image(s) with GPT Image 2.6 Flare.")
        (req,) = standin.seen
        assert req["path"] == "/v1/relay/openai-image/v1/images/generations"
        assert req["headers"]["Authorization"] == f"Bearer {RELAY_TOKEN}"
        assert json.loads(req["body"]) == {
            "model": "gpt-image-2.6-flare", "prompt": "a lighthouse",
            "n": 2, "size": "1536x1024", "quality": "medium",
        }
        saved = sorted((tmp_path / "generated-assets").iterdir())
        assert len(saved) == 2 and saved[0].read_bytes() == PNG

    def test_high_runs_sunburst_at_high(self, mod, standin):
        out = _call(mod, "generate_image", {"prompt": "a chart", "model": "gpt-image", "quality": "high"})
        assert "GPT Image 2.6 Sunburst" in out[0].text
        body = json.loads(standin.seen[0]["body"])
        assert body["model"] == "gpt-image-2.6-sunburst"
        assert body["quality"] == "high"
        assert body["size"] == "1024x1024"

    @pytest.mark.parametrize(("ratio", "size"), [
        ("1:1", "1024x1024"), ("9:16", "1024x1536"), ("3:4", "1024x1536"), ("2:3", "1024x1536"),
        ("4:3", "1536x1024"), ("3:2", "1536x1024"), ("21:9", "1536x1024"),
    ])
    def test_sizes_are_the_vendors_recommended_three(self, mod, standin, ratio, size):
        _call(mod, "generate_image", {"prompt": "p", "model": "gpt-image", "aspect_ratio": ratio})
        assert json.loads(standin.seen[0]["body"])["size"] == size

    def test_edit_runs_sunburst_with_the_mask(self, mod, standin, tmp_path):
        src = tmp_path / "in.png"
        mask = tmp_path / "mask.png"
        src.write_bytes(PNG)
        mask.write_bytes(PNG)
        out = _call(mod, "edit_image_ai", {
            "image_path": str(src), "prompt": "add a boat", "model": "gpt-image",
            "mask_path": str(mask),
        })
        assert out[0].text.startswith("Edited image with GPT Image 2.6 Sunburst.")
        (req,) = standin.seen
        assert req["path"] == "/v1/relay/openai-image/v1/images/edits"
        parts = _multipart(req)
        assert parts["model"] == b"gpt-image-2.6-sunburst"
        assert parts["quality"] == b"medium"
        assert parts["size"] == b"1024x1024"
        assert parts["n"] == b"1"
        assert parts["image"] == PNG and parts["mask"] == PNG

    def test_edit_high_quality(self, mod, standin, tmp_path):
        src = tmp_path / "in.png"
        src.write_bytes(PNG)
        _call(mod, "edit_image_ai", {"image_path": str(src), "prompt": "p", "model": "gpt-image",
                                     "quality": "high"})
        assert _multipart(standin.seen[0])["quality"] == b"high"

    def test_vendor_refusal_reads_as_an_error(self, mod, monkeypatch):
        async def refuse(*a, **kw):
            import httpx
            req = httpx.Request("POST", "http://127.0.0.1/v1/images/generations")
            raise httpx.HTTPStatusError("400 Bad Request", request=req,
                                        response=httpx.Response(400, request=req))
        monkeypatch.setattr(mod, "_generate_openai", refuse)
        out = _call(mod, "generate_image", {"prompt": "p", "model": "gpt-image"})
        # An "Error" answer is never charged (the cost engine skips it).
        assert out[0].text.startswith("Error generating image:")

    @pytest.mark.parametrize("tool,helper,args", [
        ("generate_image", "_generate_openai", {"prompt": "p", "model": "gpt-image"}),
        ("edit_image_ai", "_edit_openai", {"prompt": "p", "model": "gpt-image"}),
    ])
    def test_a_refusal_shows_the_answers_own_words(self, mod, monkeypatch, tmp_path, tool, helper, args):
        # The relay names what it refused beside its code: the agent reads it.
        async def refuse(*a, **kw):
            import httpx
            req = httpx.Request("POST", "http://127.0.0.1/v1/images/generations")
            raise httpx.HTTPStatusError("400 Bad Request", request=req, response=httpx.Response(
                400, request=req, json={"detail": "model_not_allowed", "model": "gpt-image-1"}))
        monkeypatch.setattr(mod, helper, refuse)
        if tool == "edit_image_ai":
            src = tmp_path / "in.png"
            src.write_bytes(PNG)
            args = {**args, "image_path": str(src)}
        out = _call(mod, tool, args)
        assert out[0].text.startswith("Error ")
        assert ('The OtoDock relay answered 400: {"detail": "model_not_allowed", "model": "gpt-image-1"}'
                in out[0].text)

    def test_byo_goes_to_openai_with_the_key(self, monkeypatch, tmp_path):
        for key in ("OTODOCK_RELAY_BASE", "OTODOCK_RELAY_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("OPENAI_IMAGE_API_KEY", "sk-test-not-a-key")
        monkeypatch.setenv("IMAGE_SAVE_DIR", str(tmp_path))
        mod = load_mcp_server(MCP_DIR)
        url, headers = mod._vendor_request_args("openai-image", "/v1/images/generations")
        assert url == "https://api.openai.com/v1/images/generations"
        assert headers == {"Authorization": "Bearer sk-test-not-a-key"}

    def test_byo_goes_to_google_with_the_key_in_its_header(self, monkeypatch, tmp_path):
        """Never in the address: where the credential exit carries the key the
        tool holds a placeholder, which the exit fills in this header alone."""
        for key in ("OTODOCK_RELAY_BASE", "OTODOCK_RELAY_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "g-test-not-a-key")
        monkeypatch.setenv("IMAGE_SAVE_DIR", str(tmp_path))
        mod = load_mcp_server(MCP_DIR)
        url, headers = mod._vendor_request_args("google-ai", "/v1beta/models/m:generateContent")
        assert url == "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent"
        assert headers == {"x-goog-api-key": "g-test-not-a-key"}


# ───────────────────────────── Nano Banana Pro ───────────────────────────────


class TestNanoBananaWire:
    def test_generate_calls_the_ga_name(self, mod, standin):
        out = _call(mod, "generate_image", {"prompt": "a cat", "aspect_ratio": "9:16"})
        assert out[0].text.startswith("Generated 1 image(s) with Nano Banana Pro.")
        (req,) = standin.seen
        assert req["path"] == "/v1/relay/google-ai/v1beta/models/gemini-3-pro-image:generateContent"
        body = json.loads(req["body"])
        assert body["generationConfig"] == {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16"}}

    def test_edit_calls_the_ga_name(self, mod, standin, tmp_path):
        src = tmp_path / "in.jpg"
        src.write_bytes(PNG)
        _call(mod, "edit_image_ai", {"image_path": str(src), "prompt": "warmer light"})
        (req,) = standin.seen
        assert req["path"] == "/v1/relay/google-ai/v1beta/models/gemini-3-pro-image:generateContent"
        parts = json.loads(req["body"])["contents"][0]["parts"]
        assert parts[0]["inlineData"]["mimeType"] == "image/jpeg"


# ───────────────────────────── names and prices ──────────────────────────────


class TestTexts:
    def test_every_text_names_gpt_image_2_6(self, mod):
        manifest = json.loads((MCP_DIR / "manifest.json").read_text())
        skill = (MCP_DIR / "skills" / "image-gen.md").read_text()
        tools = json.dumps([t.model_dump() for t in asyncio.run(mod.list_tools())])
        for text in (manifest["description"], skill, tools, (MCP_DIR / "server.py").read_text()):
            assert "GPT Image 1.5" not in text
            assert "gpt-image-1" not in text
        assert "GPT Image 2.6" in manifest["description"]
        assert "GPT Image 2.6" in skill
        assert manifest["version"] == "1.3.1"


class TestCosts:
    @pytest.fixture()
    def block(self):
        manifest = json.loads((MCP_DIR / "manifest.json").read_text())
        return _parse_costs_block(manifest["costs"], "image-gen-mcp")

    @pytest.mark.parametrize(("args", "amount"), [
        ({"model": "gpt-image"}, 0.0435),
        ({"model": "gpt-image", "aspect_ratio": "1:1"}, 0.0435),
        ({"model": "gpt-image", "aspect_ratio": "16:9"}, 0.034),
        ({"model": "gpt-image", "quality": "high"}, 0.0527),
        ({"model": "gpt-image", "quality": "high", "aspect_ratio": "9:16"}, 0.0412),
        ({"model": "gpt-image", "num_images": 3}, 0.1305),
        ({"model": "nano-banana", "num_images": 2}, 0.268),
        ({}, 0.134),
        ({"num_images": 4}, 0.536),
    ])
    def test_generate_rows(self, block, args, amount):
        hit = mcp_cost_engine.evaluate("image-gen-mcp", "generate_image", {"prompt": "p", **args}, block)
        assert hit is not None and hit.amount == pytest.approx(amount)

    @pytest.mark.parametrize(("args", "amount"), [
        ({"model": "gpt-image"}, 0.0435),
        ({"model": "gpt-image", "quality": "high"}, 0.0527),
        ({}, 0.134),
        ({"model": "nano-banana"}, 0.134),
    ])
    def test_edit_rows(self, block, args, amount):
        hit = mcp_cost_engine.evaluate("image-gen-mcp", "edit_image_ai", {"prompt": "p", **args}, block)
        assert hit is not None and hit.amount == pytest.approx(amount)
