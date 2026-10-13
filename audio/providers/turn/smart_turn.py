"""Smart Turn v3.2 turn classifier (local ONNX prosody model).

Analyzes raw audio features (intonation, pauses, energy) to decide whether a
speaker has finished their turn. Runs locally in ~12-30ms on AVX2.

The bundled ``models/smart-turn-v3.2-cpu.onnx`` is pipecat-ai's open Smart Turn
v3 model, BSD-2-Clause — https://github.com/pipecat-ai/smart-turn (weights:
https://huggingface.co/pipecat-ai/smart-turn-v3).

The mel front-end uses ``transformers.WhisperFeatureExtractor``. Its config
(``openai/whisper-small``'s ``preprocessor_config.json`` — the mel-filterbank
spec, ~180KB, NOT the ~470MB ASR weights) is **vendored** at
``models/whisper_feature_extractor/`` and loaded from disk, so the classifier
runs fully offline with no HuggingFace Hub fetch on the first call. A missing
vendored asset falls back to the Hub id (kept as a safety net).

The loaded model (the ONNX session and the feature extractor) is one
process-wide object per (model path, threads): the first load imports
onnxruntime and transformers and takes seconds, and a call that paid it on
the event loop lost its first two seconds of audio (the first call after
every daemon start). The daemon warms it at start and after
every config push (``prewarm_smart_turn``); a classifier is a light per-call
wrapper carrying the threshold and the window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from audio.providers.turn.base import TurnClassifier

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1)

# One loaded model per process; a load with another key replaces it.
_models: dict[tuple[str, int], _SmartTurnModel] = {}
_models_lock = threading.Lock()


def default_model_path() -> str:
    """Resolve the bundled Smart Turn ONNX model via importlib.resources.

    Robust to cwd and install location (editable vs wheel) — never
    ``__file__``-relative.
    """
    from importlib.resources import files
    return str(files("audio") / "models" / "smart-turn-v3.2-cpu.onnx")


def default_feature_extractor_dir() -> str:
    """Resolve the vendored Whisper feature-extractor config directory.

    The Smart Turn ONNX model takes Whisper mel features as input. We vendor
    ``openai/whisper-small``'s ``preprocessor_config.json`` (the mel-filterbank
    spec) next to the ONNX so the turn classifier runs fully offline — no
    HuggingFace Hub fetch on the first phone call. Robust to cwd / install
    location (editable vs wheel), never ``__file__``-relative.
    """
    from importlib.resources import files
    return str(files("audio") / "models" / "whisper_feature_extractor")


class _SmartTurnModel:
    """The ONNX session and the mel front-end, shared by every classifier
    built on the same path and thread count. Inference holds no state, and
    it serialises on the module's one-worker executor."""

    def __init__(self, model_path: str, num_threads: int) -> None:
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = num_threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name

        # Determine expected mel frames from model input shape
        input_shape = self.session.get_inputs()[0].shape
        self.mel_frames = input_shape[2] if len(input_shape) > 2 and isinstance(input_shape[2], int) else 800

        # Whisper feature extractor for mel spectrogram (no PyTorch needed).
        # Load the VENDORED config from disk → fully offline, no HF Hub fetch on
        # the first call. Fall back to the Hub id only if the vendored asset is
        # missing (e.g. a partial checkout), so behaviour degrades gracefully
        # rather than crashing.
        fe_dir = default_feature_extractor_dir()
        if os.path.isfile(os.path.join(fe_dir, "preprocessor_config.json")):
            self.feature_extractor = WhisperFeatureExtractor.from_pretrained(fe_dir)
        else:
            logger.warning(
                "Vendored Whisper feature-extractor config missing at %s — "
                "falling back to HuggingFace Hub (needs network on first call)",
                fe_dir,
            )
            self.feature_extractor = WhisperFeatureExtractor.from_pretrained("openai/whisper-small")


def load_model(model_path: str | None = None, num_threads: int = 1) -> _SmartTurnModel:
    """The process's loaded model for ``(model_path, num_threads)``, loaded
    on the first call (seconds: the imports, the session, the extractor) and
    reused after. Safe from any thread; the daemon calls it off the loop."""
    key = (model_path or default_model_path(), int(num_threads))
    with _models_lock:
        model = _models.get(key)
        if model is None:
            t0 = time.monotonic()
            model = _SmartTurnModel(*key)
            _models.clear()
            _models[key] = model
            logger.info(
                f"Smart Turn model loaded in {time.monotonic() - t0:.1f}s "
                f"(threads={key[1]})"
            )
    return model


def prewarm_smart_turn(model_path: str | None = None, num_threads: int = 1) -> None:
    """Load the model ahead of the first call (a no-op once loaded)."""
    load_model(model_path, num_threads)


class SmartTurnClassifier(TurnClassifier):
    """Turn classifier using the Smart Turn v3.2 ONNX model (prosody-based)."""

    def __init__(self, model_path: str, threshold: float, num_threads: int, audio_window_s: float) -> None:
        self._threshold = threshold
        self._audio_window_s = audio_window_s
        self._audio_window_samples = int(audio_window_s * 16000)  # 16kHz
        self._model = load_model(model_path, num_threads)
        logger.info(
            f"Smart Turn classifier initialized "
            f"(threshold={threshold}, threads={num_threads}, window={audio_window_s}s)"
        )

    def _predict_sync(self, audio_16k: np.ndarray) -> tuple[bool, float]:
        """Synchronous inference. Returns (is_complete, probability)."""
        # Truncate or pad to audio window
        target_len = self._audio_window_samples
        if len(audio_16k) > target_len:
            # Keep the last N samples (most recent speech)
            audio_16k = audio_16k[-target_len:]
        elif len(audio_16k) < target_len:
            # Prepend zeros (silence before speech)
            pad = np.zeros(target_len - len(audio_16k), dtype=np.float32)
            audio_16k = np.concatenate([pad, audio_16k])

        # Compute mel spectrogram via Whisper feature extractor
        features = self._model.feature_extractor(
            audio_16k,
            sampling_rate=16000,
            return_tensors="np",
        )
        # Truncate to expected frames (model expects 800, extractor produces 3000)
        mel = features.input_features[:, :, :self._model.mel_frames]

        # Run ONNX inference
        logit = self._model.session.run(None, {self._model.input_name: mel})[0]
        # Sigmoid to get probability
        prob = float(1.0 / (1.0 + np.exp(-logit.item())))
        is_complete = prob >= self._threshold

        logger.debug(f"Smart Turn: prob={prob:.3f} → {'complete' if is_complete else 'incomplete'}")
        return is_complete, prob

    async def classify_audio(self, audio_16k: np.ndarray) -> bool:
        """Async wrapper — runs inference in thread pool. Returns True if complete."""
        loop = asyncio.get_running_loop()
        try:
            is_complete, prob = await loop.run_in_executor(
                _executor, self._predict_sync, audio_16k,
            )
            return is_complete
        except Exception as e:
            logger.warning(f"Smart Turn classifier error: {e}")
            return True  # fail-open
