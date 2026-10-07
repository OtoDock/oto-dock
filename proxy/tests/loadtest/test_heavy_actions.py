"""Check 5: the single actions that once held the loop for seconds (a 120 MB
zip, a 128 MB satellite pull, a 12 MP pasted photo) now hold it under 20 ms,
the photo under 35 ms (its frame is inflated and parsed on the loop, about
25 ms, accepted). Three runs each; the check passes on the median of the
three runs' loop maxima, and every run is recorded."""

import asyncio
import base64
import hashlib
import io
import json
import os
import statistics
import time

import pytest

pytestmark = [pytest.mark.loadtest, pytest.mark.timeout(600, method="thread")]

RUNS = 3
MB = 1024 * 1024


async def _settle() -> None:
    """Dirty pages written, garbage collected, then a moment of quiet: the
    previous run's disk writes must not land in this run's window."""
    await asyncio.to_thread(os.sync)
    await asyncio.sleep(0.5)


def _median_of_maxima(runs: list[dict]) -> float:
    return statistics.median(r["loop"]["max_ms"] for r in runs)


def test_a_120_mb_zip_download_holds_the_loop(tmp_path):
    import config
    from api.agents.files import _create_zip_token
    from storage.agents import agent_store
    from tests.loadtest import _harness as h

    agent = "lt-zip"
    agent_store.create_agent(agent, "Zip", created_by="user-admin")
    media = config.AGENTS_DIR / agent / "knowledge" / "media"
    media.mkdir(parents=True, exist_ok=True)
    for i in range(12):
        (media / f"f{i:02d}.bin").write_bytes(os.urandom(10 * MB))
    tokens = [_create_zip_token(agent, ["knowledge/media"], "user-admin", "viewer", "admin")
              for _ in range(RUNS)]

    async def main():
        runs = []
        async with h.production_loop(tmp_path):
            async with h.serve_app() as (host, port, _router):
                spec = tmp_path / "download.json"
                spec.write_text(json.dumps({"host": host, "port": port, "paths": [
                    f"/v1/agents/{agent}/zip-download?t={t}" for t in tokens]}))
                client = await h.spawn(h.CLIENT, "download", str(spec))
                try:
                    await h.read_line(client, 60)
                    for _ in range(RUNS):
                        await _settle()
                        async with h.Window() as window:
                            await h.tell(client, "go")
                            got = await h.read_line(client, 120)
                        runs.append({"download": got, **window.summary()})
                finally:
                    await h.end(client)
        return runs

    try:
        runs = h.run_loop(main, 500)
    finally:
        for f in media.iterdir():
            f.unlink()
    h.record("zip", runs=runs, median_of_maxima_ms=_median_of_maxima(runs))

    for r in runs:
        assert r["download"]["status"] == 200, r["download"]
        assert r["download"]["bytes"] >= 120 * MB, r["download"]
    assert _median_of_maxima(runs) < h.HEAVY_MAX_S * 1000, [r["loop"] for r in runs]


def test_a_128_mb_satellite_pull_holds_the_loop(tmp_path):
    import config
    from core.remote.satellite_connection import SatelliteConnectionManager
    from services.path_policy_v2 import PathRef
    from tests.loadtest import _harness as h

    chunk = 512 * 1024
    blocks = [os.urandom(chunk) for _ in range(8)]
    encoded = [base64.b64encode(b).decode() for b in blocks]
    total = 256
    digest = hashlib.sha256()
    for i in range(total):
        digest.update(blocks[i % 8])
    whole_hash = f"sha256:{digest.hexdigest()}"

    class _Satellite:
        def __init__(self):
            self.sent: list[dict] = []

        async def enqueue_send(self, msg: dict, *, bulk: bool = False) -> None:
            self.sent.append(msg)

    async def one_pull(dest):
        mgr = SatelliteConnectionManager()
        sat = _Satellite()
        mgr._connections["m1"] = sat
        task = asyncio.create_task(mgr.pull_file_to_path(
            "m1", PathRef("agent_tree", "workspace/pulled.bin"), dest, agent_slug="lt-pull",
            stall_s=120))
        async with asyncio.timeout(10):
            while not sat.sent:
                await asyncio.sleep(0.005)
        rid = sat.sent[0]["request_id"]
        frames = [json.dumps({
            "type": "file_content", "request_id": rid, "path": "workspace/pulled.bin",
            "chunk_index": i, "total_chunks": total, "content_b64": encoded[i % 8],
            "hash": whole_hash if i == total - 1 else ""}) for i in range(total)]
        await _settle()
        async with h.Window() as window:
            first = 0.0
            for i, raw in enumerate(frames):
                t0 = time.monotonic()
                await mgr.handle_message("m1", json.loads(raw))
                if i == 0:
                    first = time.monotonic() - t0
                await asyncio.sleep(0)
            ok = await task
        del frames
        return ok, first, window

    async def main():
        runs = []
        async with h.production_loop(tmp_path):
            for n in range(RUNS):
                dest = config.AGENTS_DIR / "lt-pull" / "workspace" / f"pulled-{n}.bin"
                dest.parent.mkdir(parents=True, exist_ok=True)
                ok, first, window = await one_pull(dest)
                size = dest.stat().st_size if dest.exists() else 0
                sha = await asyncio.to_thread(lambda: "sha256:" + hashlib.sha256(dest.read_bytes()).hexdigest()) \
                    if size else ""
                runs.append({"ok": ok, "bytes": size, "hash_ok": sha == whole_hash,
                             "first_chunk_ms": h.ms(first), **window.summary()})
                if dest.exists():
                    dest.unlink()
        return runs

    runs = h.run_loop(main, 500)
    h.record("satellite-pull", runs=runs, median_of_maxima_ms=_median_of_maxima(runs))

    for r in runs:
        assert r["ok"] and r["bytes"] == total * chunk and r["hash_ok"], r
    assert _median_of_maxima(runs) < h.HEAVY_MAX_S * 1000, [r["loop"] for r in runs]


def _photo(target_bytes: int) -> bytes:
    """A 4000 x 3000 JPEG of about ``target_bytes``: a smooth scene with
    noise, the quality searched until the size fits."""
    from PIL import Image, ImageFilter

    noise = Image.effect_noise((4000, 3000), 40).convert("L")
    base = Image.linear_gradient("L").resize((4000, 3000))
    img = Image.merge("RGB", (base, noise.filter(ImageFilter.GaussianBlur(2)), noise))
    lo, hi, best = 10, 95, b""
    while lo <= hi:
        q = (lo + hi) // 2
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q)
        if buf.tell() <= target_bytes:
            best, lo = buf.getvalue(), q + 1
        else:
            hi = q - 1
    return best


def test_a_12_mp_pasted_photo_holds_the_loop(tmp_path):
    from PIL import Image

    import ws.dashboard  # noqa: F401  (assembles the support mixin first)
    from ws import dashboard_chat_support as support
    from tests.loadtest import _harness as h

    # The dashboard sends a photo of 2 MB or less as it is and scales a
    # larger one down to 1568 px, so 2 MB is the largest it pastes.
    sizes = {"dashboard": 2 * MB - 64 * 1024, "raw_client": 4 * MB}
    frame_files = []
    for label, size in sizes.items():
        jpeg = _photo(size)
        frame = json.dumps({"type": "message", "text": "what is in this photo?",
                            "images": ["data:image/jpeg;base64," + base64.b64encode(jpeg).decode()]})
        path = tmp_path / f"photo-{label}.json"
        path.write_text(frame)
        sizes[label] = len(jpeg)
        frame_files.append(str(path))
    order = ["dashboard"] * RUNS + ["raw_client"]
    save_dir = tmp_path / "photos"
    save_dir.mkdir()

    async def photo_route(websocket):
        await websocket.accept()
        msg = json.loads(await websocket.receive_text())
        saved = await support._save_photo_off_loop(support._save_base64_image, msg["images"][0],
                                                   save_dir=save_dir)
        await websocket.send_json({"path": (saved or {}).get("path", "")})
        await websocket.close()

    async def main():
        runs = []
        async with h.production_loop(tmp_path):
            async with h.serve_app(ws_routes={"/__loadtest/photo": photo_route}) as (host, port, _r):
                spec = tmp_path / "photo.json"
                spec.write_text(json.dumps({"url": f"ws://{host}:{port}/__loadtest/photo", "frame_files": [
                    frame_files[0 if label == "dashboard" else 1] for label in order]}))
                client = await h.spawn(h.CLIENT, "photo", str(spec))
                try:
                    await h.read_line(client, 60)
                    for label in order:
                        await _settle()
                        async with h.Window() as window:
                            await h.tell(client, "go")
                            saved = await h.read_line(client, 120)
                        runs.append({"photo": label, "jpeg_bytes": sizes[label], "saved": saved,
                                     **window.summary()})
                finally:
                    await h.end(client)
        return runs

    runs = h.run_loop(main, 500)
    for r in runs:
        path = r["saved"].get("path")
        r["saved_size"] = list(Image.open(path).size) if path else None
    asserted = [r for r in runs if r["photo"] == "dashboard"]
    h.record("photo", runs=runs, median_of_maxima_ms=_median_of_maxima(asserted))

    for r in runs:
        assert r["saved_size"] and max(r["saved_size"]) == 1568, r
    assert sizes["dashboard"] > 1.5 * MB, sizes
    assert _median_of_maxima(asserted) < h.PHOTO_MAX_S * 1000, [r["loop"] for r in asserted]
