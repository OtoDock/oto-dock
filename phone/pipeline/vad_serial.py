"""SerializedVad — SileroVad behind one worker thread.

Two problems, one mechanism (P2.2 / audit F11):

- Inference off the event loop: Silero runs synchronously per 20 ms inbound
  frame; its p99 spikes on a busy VM block the paced TTS sender mid-frame —
  the live calls logged 6–17 pacing stalls (>50 ms holes) per long playback,
  audible as crackle. silero-vad-lite is ONNX Runtime under the hood, which
  releases the GIL during Run, so a thread genuinely unblocks the loop.
- Mode-swap race: ``set_bargein_mode``/``reset`` replace the ONNX model and
  clear deques while a threaded ``process`` could be mid-inference. ALL
  mutating calls ride the SAME single worker, so they serialize in submit
  order — a mode change lands before the next frame's inference, exactly
  the ordering the event loop had.

Reads (``state``, thresholds) stay on-loop: attribute reads are atomic and
at most one 32 ms chunk stale, which the async event handling already
tolerated. The sync mutator surface is preserved so call sites (playback /
llm / outbound) don't change.
"""

from __future__ import annotations

import contextlib
import asyncio
from concurrent.futures import ThreadPoolExecutor


class SerializedVad:
    """Proxy a VAD instance through a single-thread executor."""

    def __init__(self, vad):
        self._vad = vad
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vad")

    @property
    def state(self):
        return self._vad.state

    @state.setter
    def state(self, value):
        # Forwarded for tests that pin a VAD state; production code never
        # assigns state from outside the VAD itself.
        self._vad.state = value

    def set_bargein_mode(self, enabled: bool) -> None:
        # Fire-and-forget: ordered against process() by the single worker,
        # which is the invariant that matters (mode applies before the next
        # frame's inference). After shutdown (teardown paths like
        # _cleanup → _cancel_tts) a mode swap is meaningless — drop it
        # instead of raising out of teardown.
        with contextlib.suppress(RuntimeError):
            self._exec.submit(self._vad.set_bargein_mode, enabled)

    def reset(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._exec.submit(self._vad.reset)

    async def aprocess(self, audio_bytes: bytes):
        return await asyncio.get_running_loop().run_in_executor(
            self._exec, self._vad.process, audio_bytes,
        )

    def shutdown(self) -> None:
        self._exec.shutdown(wait=False)

    def __getattr__(self, name):
        return getattr(self._vad, name)
