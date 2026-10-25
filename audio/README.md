# oto-audio

Provider-agnostic **STT / TTS / VAD / turn-classifier / language-identifier** package for OtoDock.

It is imported by the **proxy** for the chat audio surface (`/v1/audio/tts`,
`/ws/audio/stt`, `/v1/audio/transcribe`), and by the **phone service** for
its telephony call pipeline. The package is deliberately consumer-agnostic:

The package contains **no telephony glue and no HTTP surface** — only reusable
provider implementations behind small ABCs.

## Design rules

1. **Providers are pure.** A provider receives its credentials and audio-format
   parameters as constructor kwargs. It never reads the DB, environment, or
   files. The *caller* resolves credentials (proxy → `infra_credentials`) and
   instantiates. This keeps the package testable with trivial mocks and free
   of process-specific bootstrap.
2. **One provider per file**, one category per folder (`stt/`, `tts/`, `vad/`,
   `turn/`), each with a `base.py` ABC that is the contributor contract. `lid/`
   holds the language identifier's ABC beside the rule and the tracker that
   drive it.
3. **No `import config`.** Audio-format constants live in
   [`constants.py`](constants.py); the transcript-logging gate lives in
   [`log_policy.py`](log_policy.py).
4. **Discovery is centralized** in [`providers/registry.py`](providers/registry.py)
   (`KNOWN_*` maps name → `"module:Class"`, lazily imported so a provider's
   import-time model load never fires on proxy startup).

## Layout

```
audio/
  constants.py        audio-format constants (SAMPLE_RATE, FRAME_SIZE, …)
  log_policy.py       transcript-logging gate (Rule #1: never bare-log transcripts)
  capabilities.py     STT/TTS capability descriptors (drives the admin pill + validation)
  providers/
    registry.py       KNOWN_STT / KNOWN_TTS provider maps + build/cache
    credential_resolver.py   CredentialResolver Protocol (caller-supplied)
    model_succession.py   default model ids, retired ids → successors
    stt/  { base, deepgram, elevenlabs, example_provider }
    tts/  { base, cartesia, elevenlabs }
    vad/  { base, silero (state machine), silero_model (the ONNX session) }
    turn/ { base, smart_turn, groq, dispatcher }
    lid/  { base, ecapa (the ONNX identifier), rule (the language rule), tracker (the driver), evidence (two streams' words compared over the audio both covered) }
    stt/streams.py    LanguageStreams: one STT provider over the default, the active and at most one more stream
  streaming/          lang.py (language registry, the candidate list, the TTS detector), text_chunks.py (sentence chunking), tts_stream.py (streaming-TTS orchestration)
  models/             smart-turn-v3.2-cpu.onnx, silero_vad.onnx, lang-id-ecapa-voxlingua107.onnx + vendored whisper feature-extractor config (README.md: origin + licences)
```

## Local development

Install editable into **each** venv that consumes it:

```bash
pip install -e /path/to/oto-dock/audio
```

Re-run after dependency changes. (Docker deployments instead bake it in:
`COPY audio/ /opt/otodock/audio/` + `pip install -e /opt/otodock/audio` in
each consumer's Dockerfile.)

**Editing caveat:** `pip install -e` mounts the source, but Python caches
imported modules — editing `audio/*.py` requires restarting the consuming
process. Inside a long-lived chat session the provider subprocess is also
cached; close and reopen the chat to pick up new code.

See [`CONTRIBUTING_PROVIDERS.md`](CONTRIBUTING_PROVIDERS.md) to add a provider.
