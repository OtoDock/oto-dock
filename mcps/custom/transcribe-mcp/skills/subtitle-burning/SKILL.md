---
name: subtitle-burning
description: Transcribe audio and burn styled subtitles into videos. Use when adding subtitles or captions to a video.
---

## Transcription & Subtitle Burning

The transcribe tools turn audio (or a video's audio track) into text with
**word-level timing**, then build SRT subtitle files. Use them for accurate
captions — including punchy per-word "karaoke" captions for social/TikTok video.

### Tools

- `transcribe_audio_file(file_path, language?, provider_id?)` — transcribe a local
  audio/video file. Writes a `<name>.transcript.json` (text + per-word timings) to
  the workspace and returns its path.
- `transcribe_audio_url(url, language?, provider_id?)` — same, for a remote URL.
- `generate_subtitles(transcript_json, output_path, format, per_word,
  max_words_per_cue, …, and for ASS: position, active_color, inactive_color,
  font_size)` — turn a transcript JSON into an `.srt` or `.ass` subtitle file.

**Language.** If you don't know what language the audio is in, **ask the user first**,
then pass it as `language` (e.g. `"el"` for Greek). Omit it to auto-detect a single
language; pass `"multi"` if the user says the file MIXES languages (e.g. English +
Greek in one recording). Auto-detect picks one dominant language and can mangle a mixed
file. The detected language is in the returned JSON. Word timings (and so the subtitles)
are provider-agnostic — same whatever STT engine is used — and render any script
(Greek included).

### Subtitle-burning workflow (video)

```
1. Extract the audio track (small + fast vs the full video):
   ffmpeg -i input.mp4 -vn -ar 16000 -ac 1 audio.wav
   (You can also transcribe the video directly — the engine reads the audio
   track — but extracting audio first is the efficient path.)

2. Transcribe:
   transcribe_audio_file(file_path="audio.wav")
   → writes audio.transcript.json, returns its path

3. Build subtitles:
   - Standard captions (SRT):
     generate_subtitles(transcript_json="audio.transcript.json",
                        output_path="subs.srt")
   - TikTok / per-word pop (SRT, one word per cue):
     generate_subtitles(transcript_json="audio.transcript.json",
                        output_path="subs.srt", per_word=true)
   - Karaoke word-highlight (ASS — words light up as spoken):
     generate_subtitles(transcript_json="audio.transcript.json",
                        output_path="subs.ass", format="ass")
   - TikTok / Hormozi karaoke (centred, short chunks):
     generate_subtitles(transcript_json="audio.transcript.json",
                        output_path="subs.ass", format="ass",
                        position="center", max_words_per_cue=3)

4. Burn into the video:
   - SRT:  ffmpeg -i input.mp4 -vf subtitles=subs.srt output.mp4
   - ASS:  ffmpeg -i input.mp4 -vf ass=subs.ass output.mp4   (keeps the styling)
   (For fully custom animated captions, read the raw words[] from the transcript
   JSON and render them yourself.)
```

### Choosing a format

- **SRT** — universal, plain text. Use `per_word=true` for the one-word-at-a-time
  "pop" look. No intra-line styling.
- **ASS** — supports the **karaoke highlight** (each word changes colour exactly
  when it's spoken) via `\k` timing, which SRT cannot do. Style it with the tool
  params: `position` (`center` = the TikTok/Hormozi look; default `lower_third`),
  `active_color` / `inactive_color` (`#RRGGBB`; default yellow-on-white), and
  `font_size`. For TikTok use `format="ass", position="center"` with a short
  `max_words_per_cue` (3–4) — the ASS default chunk is already 4.
- **Not a subtitle file:** per-word bounce/scale animation, typewriter motion, and
  speech-synced emoji can't live in an SRT/ASS file — those need frame-by-frame
  video rendering (a video-editing tool's job, not this one).

### Grouping knobs (`generate_subtitles`)

- `per_word=true` — one cue per word (TikTok word-pop). Ignores the grouping caps.
- `max_words_per_cue` (default 7), `max_chars_per_cue` (default 42),
  `max_duration_s` (default 3.0) — caps that flush a cue.
- `split_on_pause_ms` (default 400) — a silence gap this long starts a new cue
  (natural sentence/breath boundaries).

### Notes

- Prerecorded transcription accepts whole multi-hour files and common
  video containers directly — no client-side chunking needed.
- The transcript JSON is the durable artifact: `{text, language, audio_seconds,
  provider_used, words: [{word, start, end}], source}`. Re-run `generate_subtitles`
  on it with different knobs without re-transcribing.
