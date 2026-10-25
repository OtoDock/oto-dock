# Bundled models

The three model files ship inside the `oto-audio` package (`pyproject.toml`
package-data) so a fresh or air-gapped install runs the phone pipeline and
the language detection with no download.

| File | What | Origin | Licence |
|---|---|---|---|
| `smart-turn-v3.2-cpu.onnx` | Smart Turn v3.2 end-of-turn classifier (CPU export) | pipecat-ai, https://github.com/pipecat-ai/smart-turn (weights: https://huggingface.co/pipecat-ai/smart-turn-v3) | BSD-2-Clause |
| `silero_vad.onnx` | Silero VAD v5.1.2 | snakers4/silero-vad, tag `v5.1.2`, `src/silero_vad/data/silero_vad.onnx`, sha256 `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f` | MIT |
| `lang-id-ecapa-voxlingua107.onnx` | ECAPA-TDNN spoken-language identifier, 107 languages, INT8 weights (22 MB); a 16 kHz waveform in, log-probabilities in the label order of its metadata | SpeechBrain, https://huggingface.co/speechbrain/lang-id-voxlingua107-ecapa (snapshot `0253049a`), exported here (recipe below), sha256 `5a9d5daa8a7aba4d1aec92db603377db0443f6869ce8a6cbc339252690190c9d` | Apache-2.0 (the training set, VoxLingua107, is CC BY 4.0 and not shipped) |

`whisper_feature_extractor/` holds the vendored `openai/whisper-small`
preprocessor config Smart Turn's mel features are computed from.

Swapping a model file is a tuning change, not a dependency bump: the
phone's endpointing thresholds were set against these exact weights and the
feed described in `providers/vad/silero_model.py`, and the language rule's
thresholds (`providers/lid/rule.py`) against the identifier's. Prove
equivalence (or re-tune) against the previous file before shipping a new one.

## The language identifier's export

`lang-id-ecapa-voxlingua107.onnx` is SpeechBrain's `EncoderClassifier` for
`speechbrain/lang-id-voxlingua107-ecapa` (the Fbank front end, the sentence
normalisation, the ECAPA embedding model and the classifier head) wrapped as
one `torch.nn.Module` taking a `[batch, samples]` float32 waveform at 16 kHz
and returning the head's `[batch, 107]` log-probabilities, exported with
`torch.onnx.export(..., dynamo=True, opset_version=18)` (the legacy exporter
has no STFT; the dynamo one keeps it in the graph) with a dynamic sample axis,
the exporter's sidecar weights merged into the file, the label list
(VoxLingua107's own codes in output order: ISO 639-1 for most, `iw` for
Hebrew, `jw` for Javanese, and four three-letter codes), the source, the
licence and the export notes written to the model's `metadata_props`,
then quantized with onnxruntime's
`quantize_dynamic(QInt8)` (86 MB to 22 MB; the restricted decisions agree with
the fp32 graph on 98.5 per cent of the benchmark windows and the rule's rows
within half a point). Torch 2.14, SpeechBrain 1.1.1, onnxruntime 1.27. The
loader (`providers/lid/ecapa.py`) reads the labels from the metadata, so a
re-export must write them again.
