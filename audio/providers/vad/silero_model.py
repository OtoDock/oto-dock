"""The Silero VAD model behind ONNX Runtime, one 32 ms window per call.

``models/silero_vad.onnx`` is Silero VAD v5.1.2 (MIT, see ``models/README.md``).
One window per call (256 samples at 8 kHz, 512 at 16 kHz), the recurrent
``state`` carried between calls and zeroed on ``reset()``. This module is the
one place the model's input is built (``SileroModel._input``), and it has
one feed: each window with the 4 ms of audio just before it in front, 32
samples at 8 kHz and 64 at 16 kHz, zeros after a reset. That is the model's
contract: upstream's own Python package feeds it so, and silero-vad-lite
does from 0.3.0, which calls that context required. An older binding fed the
window alone, and at 8 kHz the model then scored far fewer windows of speech
as speech.

Every detector reads this feed: a phone call's, a voice conversation's, and
the second listen's speech check. No caller can ask for another, and a test
fails on a module of the product that reaches around this one.
"""

from __future__ import annotations

from importlib.resources import files

SUPPORTED_RATES = (8000, 16000)
WINDOW_MS = 32
#: The audio in front of each window in the model's contract.
CONTEXT_MS = 4


def default_model_path() -> str:
    """Resolve the bundled model via importlib.resources (never ``__file__``-relative)."""
    return str(files("audio") / "models" / "silero_vad.onnx")


class SileroModel:
    """One ONNX Runtime session at a fixed sample rate.

    ``process`` returns the speech probability of one window and carries the
    model state to the next call. Heavy imports live in ``__init__`` so a
    lean install never pays for them.
    """

    def __init__(self, sample_rate: int, model_path: str | None = None) -> None:
        if sample_rate not in SUPPORTED_RATES:
            raise ValueError(f"Silero VAD runs at 8000 or 16000 Hz, not {sample_rate}")
        import numpy as np
        import onnxruntime as ort

        self._np = np
        self.sample_rate = sample_rate
        self.window_size_samples = sample_rate * WINDOW_MS // 1000
        self._context_samples = sample_rate * CONTEXT_MS // 1000
        self._context = np.zeros(self.context_samples, dtype=np.float32)
        # Single-threaded inference: the phone serialises VAD on one worker
        # thread and runs several sessions per host. ONNX Runtime releases the
        # GIL during run(), which is what lets that worker unblock the loop.
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            model_path or default_model_path(), sess_options=opts, providers=["CPUExecutionProvider"],
        )
        self._sr = np.array(sample_rate, dtype=np.int64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    @property
    def context_samples(self) -> int:
        """The samples in front of each window, the rate's 4 ms. Read only:
        the feed is the model's, never a caller's to set."""
        return self._context_samples

    def reset(self) -> None:
        """Zero the recurrent state and the kept context: the next window
        starts a fresh sequence, with silence in front of it."""
        self._state = self._np.zeros((2, 1, 128), dtype=self._np.float32)
        self._context = self._np.zeros(self.context_samples, dtype=self._np.float32)

    def _input(self, x):
        """What the session reads for one window: the kept samples in front,
        then the window. The joined array's own tail is kept for the next
        window: an array this model owns, never a view of the caller's
        buffer, which the caller may reuse."""
        joined = self._np.concatenate([self._context, x])
        self._context = joined[-self.context_samples:]
        return joined.reshape(1, -1)

    def process(self, samples) -> float:
        """Speech probability (0..1) of one window of float32 PCM in [-1, 1].

        Accepts anything exposing a float32 buffer (``array.array("f")``,
        ``bytes``, ``memoryview``), a numpy array, or a sequence of floats.
        The window must be exactly ``window_size_samples`` long.
        """
        np = self._np
        if isinstance(samples, np.ndarray):
            x = samples.astype(np.float32, copy=False).reshape(-1)
        else:
            try:
                x = np.frombuffer(samples, dtype=np.float32)
            except TypeError:
                x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if x.size != self.window_size_samples:
            raise ValueError(
                f"Silero VAD at {self.sample_rate} Hz takes {self.window_size_samples} samples per window, got {x.size}"
            )
        output, self._state = self._session.run(
            None, {"input": self._input(x), "state": self._state, "sr": self._sr},
        )
        return float(output[0, 0])
