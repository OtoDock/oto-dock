---
name: voiceover
description: "Produce narrated voice-overs: choose the right voice per video style and language, generate segments, hand them to video tools. Use when a video needs narration or a voice-over track."
---

# Voice-overs — generating narration with the TTS tools

Use the tts-mcp tools to produce narrated voice-over audio (video narration,
explainers, promos). Synthesis runs on the platform's configured TTS provider
(Cartesia, ElevenLabs, …) — you pick the voice and delivery per call.

## Workflow: find the voice first, then produce

1. **Browse what's available**: `list_voices` shows the provider's catalog
   with voice ids and preview links. The per-language defaults configured by
   the admin also work with no `voice_id` at all.
2. **Search the library for a style** (providers with a shared library, e.g.
   ElevenLabs): `search_voice_library(search="warm documentary narrator",
   language="en")` — describe the *style*, filter by language/gender/age.
   Library voices need a one-time `add_library_voice` (admin-only) before use.
3. **Audition with short samples**: generate one representative sentence with
   2-3 candidate voices (`generate_speech(text=…, voice_id=…)`) — the inline
   players let the user compare and pick. Never narrate a full script with an
   unauditioned voice.
4. **Produce the final segments** with the winning voice: one
   `generate_speech` call per scene/paragraph, with descriptive `save_path`
   names (`projects/promo/vo-01-intro.wav`, `vo-02-features.wav`, …).

## Model choice (ElevenLabs providers)

- `eleven_v3` — maximum expressiveness for hero narration; supports inline
  audio tags in the text like `[whispers]`, `[sighs]`, `[excited]`.
- `eleven_multilingual_v2` — most consistent for long-form reads.
- `eleven_flash_v2_5` (typical configured default) — fast + cheap, good for
  auditioning; switch up for the final render.
- Cartesia providers: leave `model_id` alone (the configured Sonic model is
  already the right one).

## Delivery settings

- `stability` low (0.3-0.4) = more emotional range; high (0.7+) = steady
  corporate read. `speed` 0.9-1.1 covers most needs.
- Write for the ear: spell out numbers/abbreviations you want pronounced,
  add commas where a breath belongs. Short sentences read better.

## Handing off to video

Output is WAV (24 kHz mono by default) — video tools consume it directly and
convert/mux as needed (AAC/MP3 conversion happens there, not here). Per-scene
segments beat one long file: they align to cuts, and a re-record replaces one
scene instead of the whole narration.

Costs are real (per character, recorded platform-side): audition with short
samples, not full scripts, and reuse generated segments instead of
regenerating them.
