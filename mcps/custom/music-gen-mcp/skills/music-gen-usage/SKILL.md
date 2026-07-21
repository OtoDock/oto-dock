---
name: music-gen-usage
description: Generate music tracks from text prompts. Use when a video or project needs background music, a jingle, or a soundtrack.
---

## AI Music & Sound Effects

- **`compose_music`** — Compose an original music track from a text description (ElevenLabs Music). Returns a workspace MP3 path and shows an inline player.
- **`sound_effect`** — Generate a sound effect from a text description (ElevenLabs SFX).

### Guidelines

- Describe music concretely: genre, mood, instrumentation, tempo, and energy arc (e.g. "uplifting electronic track, driving synths, punchy four-on-the-floor beat, builds to an energetic drop"). Vague prompts ("nice music") produce generic results.
- Set `instrumental: true` for video soundtracks and background music — otherwise the model may add vocals.
- Match `duration_seconds` to the actual use: don't compose a 3-minute track to score a 30-second clip. Longer tracks cost more credits.
- For sound effects, describe the sound's character and envelope ("soft UI confirmation chime, glassy, short decay"). Omit `duration_seconds` to let the model pick a natural length; use `loop: true` for ambience beds.
- Generated audio is auto-saved to the workspace under `generated-assets/` (a bare filename passed via `save_path` also lands there). Use a relative path with a subfolder (e.g. `"projects/launch/theme.mp3"`) to organize into your own folder.
- The saved MP3 is web-safe: play it with `display_audio`, or feed it to video-tools as a soundtrack.
- Generation is not instant — a long track can take a minute or more; this is normal.
