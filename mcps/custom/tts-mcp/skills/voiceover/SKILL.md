---
name: voiceover
description: "Produce narrated voice-overs: choose the right voice per video style and language, generate segments, hand them to video tools. Use when a video needs narration or a voice-over track."
---

# Voice-overs — generating narration with the TTS tools

Use the tts-mcp tools to produce narrated voice-over audio (video narration,
explainers, promos). Synthesis runs on the platform's configured TTS provider
(Cartesia, ElevenLabs, …) — you pick the voice and delivery per call.

## Workflow: find the voice first, then produce

1. **Browse what's available**: `list_voices` shows the engine's catalog
   with voice ids. On Cartesia: the account's own voices, then public ones.
   On ElevenLabs: the workspace's voices, each with a preview link. The
   per-language defaults configured by the admin also work with no
   `voice_id` at all.
2. **Search the library for a style**: `search_voice_library(search="warm
   documentary narrator", language="en")`. Describe the *style*, filter by
   language and gender (age and category filter on ElevenLabs only). On
   Cartesia a voice found is used at once: pass its `voice_id` to
   `generate_speech`. On ElevenLabs a shared-library voice needs a one-time
   `add_library_voice` (admin-only) before use.
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

## Cartesia providers

- Leave `model_id` alone: the configured Sonic model is the right one.
- The delivery controls are `speed` and `emotion`. `emotion` takes one of
  neutral, calm, content, sad, angry or scared, and works on English speech
  only. Omit it and the voice reads the feeling from the text.
  `stability`, `similarity_boost` and `style` are ElevenLabs' only.
- Cartesia's catalog has no preview links: hear a candidate with a short
  `generate_speech` sample (step 3).

## Delivery settings

- `stability` low (0.3-0.4) = more emotional range; high (0.7+) = steady
  corporate read (ElevenLabs). `speed` 0.9-1.1 covers most needs on both
  engines.
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
