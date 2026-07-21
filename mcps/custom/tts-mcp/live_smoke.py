"""Live smoke test for tts-mcp — costs a few provider credits, needs a running
proxy with a configured (credentialed) TTS provider.

Usage (from the repo root):

    PROXY_URL=http://127.0.0.1:8400 PROXY_API_KEY=<session-or-api-token> \
        python mcps/custom/tts-mcp/live_smoke.py [voice_id]

Exercises the real chain end-to-end: voices catalog → short generation →
WAV sanity check. Prints the saved path; listen to it to judge the voice.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import wave

import httpx

PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:8400").rstrip("/")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
TEXT = "OtoDock turns any machine into a team of A I agents. This is a voice test."  # "A I" spaced so TTS speaks the letters


async def main() -> int:
    if not PROXY_API_KEY:
        print("Set PROXY_API_KEY (a session token or API key with audio access)")
        return 1
    headers = {"Authorization": f"Bearer {PROXY_API_KEY}"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(f"{PROXY_URL}/v1/audio/tts/voices", headers=headers)
        print(f"voices: HTTP {resp.status_code}")
        if resp.status_code == 200:
            body = resp.json()
            print(f"  provider: {body['provider_name']}, catalog: {len(body['voices'])} voices, "
                  f"configured map: {body['configured']}")
        payload: dict = {"text": TEXT}
        if len(sys.argv) > 1:
            payload["voice_id"] = sys.argv[1]
        resp = await client.post(f"{PROXY_URL}/v1/audio/tts/generate", headers=headers, json=payload)
        print(f"generate: HTTP {resp.status_code} "
              f"({resp.headers.get('X-Provider-Used')}, voice {resp.headers.get('X-Voice-Used')}, "
              f"{resp.headers.get('X-Audio-Seconds')}s)")
        if resp.status_code != 200:
            print(f"  detail: {resp.text[:300]}")
            return 1
        with wave.open(io.BytesIO(resp.content), "rb") as w:
            print(f"  WAV: {w.getframerate()} Hz, {w.getnframes()} frames, "
                  f"{w.getnframes() / w.getframerate():.2f}s")
        out = "/tmp/tts-smoke.wav"
        with open(out, "wb") as f:
            f.write(resp.content)
        print(f"  saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
