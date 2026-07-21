"""Live smoke test against the real ElevenLabs API — costs a few credits.

Not part of any pytest suite. Run manually once a key exists:

    cd mcps/custom/music-gen-mcp
    env ELEVENLABS_API_KEY=xi-... AUDIO_SAVE_DIR=/tmp/music-gen-smoke \
        venv/bin/python live_smoke.py

Produces one short SFX and one 10s instrumental track under AUDIO_SAVE_DIR
and prints the saved paths. Any 'Error:' output means the key or the API
integration is broken.
"""

import asyncio
import os
import sys

import server


async def main() -> int:
    if not os.environ.get("ELEVENLABS_API_KEY"):
        print("ELEVENLABS_API_KEY is not set", file=sys.stderr)
        return 2
    os.makedirs(os.environ.get("AUDIO_SAVE_DIR", ""), exist_ok=True)

    sfx = await server._handle_sfx({
        "prompt": "soft UI confirmation chime, glassy, short decay",
        "duration_seconds": 2,
    })
    print(sfx[0].text)

    music = await server._handle_compose({
        "prompt": "calm ambient electronic pad, slow, warm, minimal",
        "duration_seconds": 10,
        "instrumental": True,
    })
    print(music[0].text)

    ok = not any(r[0].text.startswith("Error") for r in (sfx, music))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
