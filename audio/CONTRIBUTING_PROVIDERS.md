# Adding an audio provider

A provider is one file implementing one ABC. The reference is
[`providers/stt/example_provider.py`](providers/stt/example_provider.py) — a
working, heavily-commented STT skeleton. Read it next to this guide.

## The two rules

1. **Never log a transcript at INFO+.** Verbatim caller/user speech is personal
   data. Route every transcript through the gate — `self._log_transcript(label, text)`
   on `STTProvider`, or `audio.log_policy.log_transcript(logger, label, text)`
   elsewhere. It logs at DEBUG unless `OTO_AUDIO_LOG_TRANSCRIPTS=1`. A bare
   `logger.info(...{transcript}...)` fails CI (`providers/tests/test_no_bare_transcript_logs.py`).
2. **Redact secrets in `__repr__`.** The base classes already return a redacted
   repr; if you override it, keep `api_key` (and any token) out. Same for any
   detail/error string you build from a URL that may carry a bearer token.

## The contract

Providers are **pure**: they receive credentials and audio-format parameters as
constructor kwargs and never read the DB, environment, or files. The caller
(proxy → `infra_credentials`; phone → config-push) resolves credentials.

Implement the ABC for your category:

| Category | ABC | Key methods |
|---|---|---|
| STT | `providers/stt/base.py::STTProvider` | streaming surface + `transcribe_file` (if batch) |
| TTS | `providers/tts/base.py::TTSProvider` | `connect` / streaming context / `synthesize` |
| VAD | (no ABC — single impl) | see `providers/vad/silero.py` |
| Turn | `providers/turn/base.py::TurnClassifier` | `close`; routed by the dispatcher |

Every STT/TTS provider also implements:

- `capabilities` — a frozen `STTCapabilities`/`TTSCapabilities` (drives the admin
  pill + feature gates).
- `from_row(cls, row, resolver)` — map an `audio_providers` row to constructor
  kwargs (this keeps the row→provider knowledge next to the provider).
- `billing_unit()` / `cost_per_unit()` / `is_free_tier` — what and how you bill.
  Local models set `is_free_tier = True` (cost reports show "self-hosted").
- `default_advanced_settings()` / `validate_advanced(settings)` — the `advanced`
  JSONB defaults + field-level validation (the admin "Restore defaults" + save).
- TTS only: `default_voices` (class attr, language → voice id) — shipped
  fallbacks consulted by `select_voice` when the admin configured no voice for
  a language, so a fresh row speaks every language out of the box. Use ONLY
  **account-independent** ids (public-library / premade voices — a
  workspace-added id 404s on other accounts). The admin's `voices` map wins.

## Register it

Add a `"name": "module:Class"` entry to `KNOWN_STT_PROVIDERS` /
`KNOWN_TTS_PROVIDERS` in [`providers/registry.py`](providers/registry.py). The
value is a **string** — the class is imported lazily, so your import-time model
load never fires on proxy startup. (`example_provider` is deliberately NOT
registered — it's a reference, not a selectable provider.)

## Heavy / local providers

- Local model imports (`onnxruntime`, `transformers`) go **inside
  `__init__`**, never at module top-level — a cloud-only deployment that never
  instantiates a local provider then never pays the import cost.
- Heavy deps belong in an **optional dependency group** in `pyproject.toml`
  (`[project.optional-dependencies]`), not the core install. Ship an importable
  **stub** whose `__init__` raises with the install hint
  (`pip install 'oto-audio[yourprovider]'`) and set `is_stub = True` — the admin
  "Add provider" menu hides stubs; flip the flag when the implementation lands.
  See `stt/canary.py`.
- **Declare the footprint** in your stub's docstring (image-size impact of the
  dep tree) and the **license** (e.g. Canary = NVIDIA Open Model License;
  Chatterbox = MIT model, voice packs may differ).
- **Offline model assets:** ship everything the first call needs — a fresh /
  air-gapped install must not reach the network. Smart Turn **vendors** its
  `WhisperFeatureExtractor` config at `audio/models/whisper_feature_extractor/`
  (shipped via `pyproject.toml` package-data) and loads it from disk —
  `from_pretrained("openai/whisper-small")` is only a fallback if the vendored
  asset is missing. Silero VAD ships as `audio/models/silero_vad.onnx` the
  same way. Follow the same pattern for any provider whose model/config
  would otherwise fetch from a hub on first use (vendor it; HF-id as fallback),
  and record the file's origin, version and licence in `audio/models/README.md`.

## Testing (no paid credentials)

- Construction + the pure surface (`from_row`, `validate_advanced`,
  `capabilities`, billing, repr redaction) test **offline** — see
  `stt/tests/test_deepgram.py`, `tts/tests/test_cartesia.py`.
- Use `ExampleSTT` (or a small fake subclass) as a test double for pipeline-level
  tests — `feed_transcript()` simulates STT output without a network.
- VAD tests run fully offline (the model is bundled) —
  `vad/tests/test_silero.py` and `test_silero_model.py` need `onnxruntime`
  (the `localmodels` extra) and skip without it; `test_silero_rates.py`
  fakes the model and runs everywhere.
- A 5-second public-domain clip at `audio/tests/fixtures/sample.wav` is the
  intended fixture for `transcribe_file` smoke tests against a real key (kept out
  of the default suite). Drop one in when you wire batch transcription.

Run the suite (any venv with the runtime deps):

```bash
python -m pytest audio/providers -q
```

## Realistic size

Don't aim for a line-count target — aim for one clear responsibility. For
reference: a minimal viable adapter is ~120 LOC (`example_provider.py`); a full
Whisper.cpp / Vosk adapter (model download, runtime, sample-rate normalization,
segmentation, partial streaming) is realistically 600–800 LOC. That's fine if
it's cohesive.
