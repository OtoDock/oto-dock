"""The vendored ECAPA-TDNN language identifier (SpeechBrain's VoxLingua107
weights, Apache-2.0) as one ONNX graph: a 16 kHz waveform in, 107
log-probabilities out, the Fbank front end and the sentence normalisation
inside the graph, the label list in the file's metadata. Numpy and
onnxruntime only.

One loaded model per process (the Smart Turn pattern): the first load reads
the 22 MB file and builds the session, so the daemon warms it at start when
detection is on, and the proxy when detection is on and a chat STT provider
is configured; a dictation socket that lands before that pays the load once,
off the loop, and a call runs without detection until the model is there
(``loaded_model``). ``models/README.md`` records the export recipe.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from audio.providers.lid.base import LanguageIdentifier

logger = logging.getLogger(__name__)

MODEL_FILE = "lang-id-ecapa-voxlingua107.onnx"
#: Half a second, the shortest input a run gets: a run on less (the tracker
#: never sends under half a second) is padded with silence to it. The
#: graph's own reflect padding raises under about 40 ms.
MIN_SAMPLES = 8000

_models: dict[tuple[str, int], _EcapaModel] = {}
_models_lock = threading.Lock()
_load_failed: set[str] = set()


def default_model_path() -> str:
    from importlib.resources import files
    return str(files("audio") / "models" / MODEL_FILE)


def identifier_available(model_path: str | None = None) -> bool:
    """The runtime imports and the model file is there (a cheap check for
    the capability report and the surfaces' construction; the load itself
    may still fail and is reported once)."""
    import os
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    path = model_path or default_model_path()
    return os.path.isfile(path) and path not in _load_failed


class _EcapaModel:
    """The ONNX session and the label list, shared by every identifier built
    on the same path and thread count. ``run`` holds no state and is safe
    from any thread."""

    def __init__(self, model_path: str, num_threads: int) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = max(1, int(num_threads))
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        meta = self.session.get_modelmeta().custom_metadata_map
        labels = (meta.get("labels") or "").split(",")
        if len(labels) < 2:
            raise RuntimeError(f"{model_path}: no label list in the model metadata")
        self.labels = [label.strip() for label in labels]

    def scores(self, pcm_16k: np.ndarray) -> np.ndarray:
        x = np.ascontiguousarray(pcm_16k, dtype=np.float32).reshape(-1)
        if len(x) < MIN_SAMPLES:
            x = np.concatenate([x, np.zeros(MIN_SAMPLES - len(x), dtype=np.float32)])
        out = self.session.run(None, {self.input_name: x.reshape(1, -1)})[0]
        return out[0].astype(np.float64)


def load_model(model_path: str | None = None, num_threads: int = 1) -> _EcapaModel:
    """The process's loaded model for ``(model_path, num_threads)``, loaded
    on the first call and reused after. Raises on a missing runtime or
    file; ``prewarm_language_identifier`` is the forgiving front."""
    key = (model_path or default_model_path(), max(1, int(num_threads)))
    with _models_lock:
        model = _models.get(key)
        if model is None:
            t0 = time.monotonic()
            try:
                model = _EcapaModel(*key)
            except Exception as e:
                # Recorded and reported ONCE per process here, whoever loads
                # (the pre-warm or a dictation socket that beat it), so no
                # later consumer retries it: ``identifier_available`` answers
                # False from now on.
                if key[0] not in _load_failed:
                    _load_failed.add(key[0])
                    logger.warning("Language identifier unavailable (%s): %s", key[0], e)
                raise
            _models.clear()
            _models[key] = model
            logger.info("Language identifier loaded in %.1fs (%d languages, threads=%d)",
                        time.monotonic() - t0, len(model.labels), key[1])
    return model


def loaded_model(model_path: str | None = None, num_threads: int = 1) -> _EcapaModel | None:
    """The process's loaded model for ``(model_path, num_threads)`` if it is
    there NOW, else None: never loads, never waits for a load under way (a
    call's constructor runs on the loop and must not pay the load)."""
    key = (model_path or default_model_path(), max(1, int(num_threads)))
    if not _models_lock.acquire(blocking=False):
        return None
    try:
        return _models.get(key)
    finally:
        _models_lock.release()


def prewarm_language_identifier(model_path: str | None = None, num_threads: int = 1) -> bool:
    """Load the model ahead of the first use. A failure (no runtime, no
    file, a broken file) is recorded and logged once by the loader and
    answered False here; the surfaces then run without identification."""
    try:
        load_model(model_path, num_threads)
        return True
    except Exception:  # noqa: BLE001  # a missing runtime, file or a broken graph all mean "no identifier"
        return False


class EcapaIdentifier(LanguageIdentifier):
    """A light per-consumer handle on the process's loaded model (loaded
    here when none is given: the proxy builds it off the loop)."""

    def __init__(self, model_path: str | None = None, num_threads: int = 1, *,
                 model: _EcapaModel | None = None) -> None:
        self._model = model or load_model(model_path, num_threads)
        self.labels = list(self._model.labels)

    def scores(self, pcm_16k: np.ndarray) -> np.ndarray:
        return self._model.scores(pcm_16k)
