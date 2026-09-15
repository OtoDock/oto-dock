"""The Silero VAD model behind ONNX Runtime, one 32 ms window per call.

``models/silero_vad.onnx`` is Silero VAD v5.1.2 (MIT, see ``models/README.md``).
The feed mirrors the binding the phone pipeline was tuned against: exactly one
32 ms window per call (256 samples at 8 kHz, 512 at 16 kHz), no context
prefix, the recurrent ``state`` carried between calls and zeroed on
``reset()``. Upstream's own Python package prepends the previous 64 samples
to every window; that shifts the probabilities the endpointing thresholds
were set against, so this wrapper deliberately does not.
"""

from __future__ import annotations

from importlib.resources import files

SUPPORTED_RATES = (8000, 16000)
WINDOW_MS = 32


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

    def reset(self) -> None:
        """Zero the recurrent state: the next window starts a fresh sequence."""
        self._state = self._np.zeros((2, 1, 128), dtype=self._np.float32)

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
            None, {"input": x.reshape(1, -1), "state": self._state, "sr": self._sr},
        )
        return float(output[0, 0])
