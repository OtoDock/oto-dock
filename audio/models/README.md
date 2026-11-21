# Bundled models

The three model files ship inside the `oto-audio` package (`pyproject.toml`
package-data). So a fresh or air-gapped install runs the phone pipeline and
the language detection with no download.

| File | What | Origin | Licence |
|---|---|---|---|
| `smart-turn-v3.2-cpu.onnx` | Smart Turn v3.2 end-of-turn classifier (CPU export) | pipecat-ai, https://github.com/pipecat-ai/smart-turn (weights: https://huggingface.co/pipecat-ai/smart-turn-v3) | BSD-2-Clause |
| `silero_vad.onnx` | Silero VAD v5.1.2 | snakers4/silero-vad, tag `v5.1.2`, `src/silero_vad/data/silero_vad.onnx`, sha256 `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f` | MIT |
| `lang-id-ecapa-voxlingua107.onnx` | ECAPA-TDNN spoken-language identifier, 107 languages, INT8 weights (22 MB). A 16 kHz waveform in, log-probabilities in the label order of its metadata | SpeechBrain, https://huggingface.co/speechbrain/lang-id-voxlingua107-ecapa (snapshot `0253049a`), exported here (recipe below), sha256 `5a9d5daa8a7aba4d1aec92db603377db0443f6869ce8a6cbc339252690190c9d` | Apache-2.0 (the training set, VoxLingua107, is CC BY 4.0 and not shipped) |

`whisper_feature_extractor/` holds the vendored `openai/whisper-small`
preprocessor config Smart Turn's mel features are computed from.

Swapping a model file is a tuning change, not a dependency bump. The
phone's endpointing thresholds were set against these exact weights and the
feed described in `providers/vad/silero_model.py`. The language rule's
thresholds (`providers/lid/rule.py`) were set against the identifier's. Prove
equivalence (or re-tune) against the previous file before shipping a new one.

## The language identifier's export

`lang-id-ecapa-voxlingua107.onnx` is SpeechBrain's `EncoderClassifier` for
`speechbrain/lang-id-voxlingua107-ecapa`: the Fbank front end, the sentence
normalisation, the ECAPA embedding model and the classifier head. The export:

- It is wrapped as one `torch.nn.Module`. The module takes a
  `[batch, samples]` float32 waveform at 16 kHz and returns the head's
  `[batch, 107]` log-probabilities.
- It is exported with `torch.onnx.export(..., dynamo=True, opset_version=18)`,
  with a dynamic sample axis. The legacy exporter has no STFT, and the dynamo
  one keeps it in the graph.
- The exporter's sidecar weights are merged into the file.
- The label list, the source, the licence and the export notes are written to
  the model's `metadata_props`. The labels are VoxLingua107's own codes in
  output order: ISO 639-1 for most, `iw` for Hebrew, `jw` for Javanese, and
  four three-letter codes.
- Then it is quantized with onnxruntime's `quantize_dynamic(QInt8)`, from
  86 MB to 22 MB. The restricted decisions agree with the fp32 graph on 98.5
  per cent of the benchmark windows. The rule's rows agree within half a
  point.

Torch 2.14, SpeechBrain 1.1.1, onnxruntime 1.27. The loader
(`providers/lid/ecapa.py`) reads the labels from the metadata, so a
re-export must write them again.
