"""Live smoke test against the real video APIs — PAID calls (a few dollars).

Not part of any pytest suite. Run manually with whichever keys exist — each
section is skipped when its key is absent:

    cd mcps/custom/video-gen-mcp
    env GOOGLE_AI_API_KEY=... RUNWAY_API_KEY=... FAL_API_KEY=... \
        VIDEO_SAVE_DIR=/tmp/video-gen-smoke \
        venv/bin/python live_smoke.py

Google section: one ~4s omni-flash generation, then a 4s veo-3.1-fast
transition between two locally-synthesized test clips (also exercises the
bundled-ffmpeg frame extraction). Runway section: one aleph2 edit of a test
clip. fal section: one 4s seedance transition. Approximate cost with all keys
set: ~$3-4. Any 'Error:' output means a key or an API integration is broken.
"""

import asyncio
import os
import subprocess
import sys

import imageio_ffmpeg

import server


def _make_test_clip(path: str, color: str) -> None:
    """Synthesize a 3s test clip with the bundled ffmpeg (no assets needed)."""
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c={color}:s=640x360:d=3:r=24",
         "-pix_fmt", "yuv420p", "-y", path],
        check=True,
    )


async def main() -> int:
    save_dir = os.environ.get("VIDEO_SAVE_DIR", "")
    if not save_dir:
        print("VIDEO_SAVE_DIR is not set", file=sys.stderr)
        return 2
    os.makedirs(save_dir, exist_ok=True)
    results = []

    clip_a = os.path.join(save_dir, "smoke_a.mp4")
    clip_b = os.path.join(save_dir, "smoke_b.mp4")
    _make_test_clip(clip_a, "steelblue")
    _make_test_clip(clip_b, "darkorange")

    if os.environ.get("GOOGLE_AI_API_KEY"):
        gen = await server._handle_generate({
            "prompt": "A paper boat drifting across a calm pond at sunrise, gentle ripples, birdsong.",
            "model": "omni-flash",
            "duration_seconds": 4,
        })
        print(gen[0].text)
        results.append(gen)

        trans = await server._handle_transition({
            "video_a_path": clip_a,
            "video_b_path": clip_b,
            "duration_seconds": 4,
            "model": "veo-3.1-fast",
        })
        print(trans[0].text)
        results.append(trans)
    else:
        print("GOOGLE_AI_API_KEY not set — skipping omni-flash + Veo transition")

    if os.environ.get("RUNWAY_API_KEY"):
        edit = await server._handle_edit({
            "video_path": clip_a,
            "prompt": "Make it look like glittering deep water at night.",
        })
        print(edit[0].text)
        results.append(edit)
    else:
        print("RUNWAY_API_KEY not set — skipping aleph2 edit")

    if os.environ.get("FAL_API_KEY"):
        seed = await server._handle_transition({
            "video_a_path": clip_a,
            "video_b_path": clip_b,
            "duration_seconds": 4,
            "model": "seedance",
        })
        print(seed[0].text)
        results.append(seed)
    else:
        print("FAL_API_KEY not set — skipping seedance transition")

    if not results:
        print("No keys set — nothing was tested", file=sys.stderr)
        return 2
    return 1 if any(r[0].text.startswith("Error") for r in results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
