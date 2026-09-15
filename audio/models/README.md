# Bundled models

Both files ship inside the `oto-audio` package (`pyproject.toml` package-data)
so a fresh or air-gapped install runs the phone pipeline with no download.

| File | What | Origin | Licence |
|---|---|---|---|
| `smart-turn-v3.2-cpu.onnx` | Smart Turn v3.2 end-of-turn classifier (CPU export) | pipecat-ai, https://github.com/pipecat-ai/smart-turn (weights: https://huggingface.co/pipecat-ai/smart-turn-v3) | BSD-2-Clause |
| `silero_vad.onnx` | Silero VAD v5.1.2 | snakers4/silero-vad, tag `v5.1.2`, `src/silero_vad/data/silero_vad.onnx`, sha256 `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f` | MIT |

`whisper_feature_extractor/` holds the vendored `openai/whisper-small`
preprocessor config Smart Turn's mel features are computed from.

Swapping either model file is a tuning change, not a dependency bump: the
phone's endpointing thresholds were set against these exact weights and the
feed described in `providers/vad/silero_model.py`. Prove equivalence (or
re-tune) against the previous file before shipping a new one.
