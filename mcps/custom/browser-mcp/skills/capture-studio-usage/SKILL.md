---
name: capture-studio-usage
description: Record scripted browser sessions into video clips with the capture studio tools. Use when filming browser walkthroughs, site demos, or web capture footage.
---

# Capture studio — staged screen recordings with the studio tools

The `studio_*` tools (`mcp__local__studio_*`) turn this machine into a small
film set for browser demos: a **dedicated virtual display** + kiosk Chrome at
exact geometry, recorded by ffmpeg at 30fps, driven like a human — curved
minimum-jerk pointer glides, real clicks, jittered typing cadence. Optional
audio capture muxes what the page plays into the take.

This is a SEPARATE browser from the everyday `browser_*` tools: its own
display, its own persistent profile, its own lifecycle. Recording never
touches the inspection browser, and the display allocator can never collide
with other work on the machine (it probes its own reserved range).

## The shape of a take

1. `studio_start({width, height, fps?, pointer?, url?, audio?})` — bring the
   set up. Returns the display, the profile and the takes directory.
2. Stage the scene: `studio_goto`, log in if the site needs it (the profile
   PERSISTS across studio sessions — stage logins once), `studio_set_zoom`
   AFTER navigating (zoom sticks per-origin), `studio_wait_text` until the
   page is ready, `studio_screenshot` to check the frame.
3. `studio_record_start({take_name})`.
4. Perform: `studio_glide` / `studio_click` / `studio_type` / `studio_press` /
   `studio_scroll` / `studio_wait_text`. Every verb is paced like a person —
   don't add artificial sleeps between actions unless the story needs a beat.
5. `studio_record_stop` — finalizes the mp4 and reports its probed duration
   and streams (trust that line, it comes from ffprobe, not intent).
6. Repeat 3–5 per take. `studio_stop` when wrapped.
7. QC: `studio_screenshot` for single frames; for motion, extract frames from
   the mp4. Then cut it with **video-tools** (trim, join, overlays, music) —
   the takes are plain H.264 yuv420p mp4s made for editing (CRF 18).

## Pointer styles (pick per video, on `studio_start`)

- `xtest` (default) — the REAL X cursor moves through the X server: hover
  states fire, context cursors render (arrow / pointer / I-beam), and the
  cursor is composited into the recording. Use for desktop takes.
- `overlay-arrow` — a DOM arrow follows CDP input. For re-staged captures
  where the desktop cursor is wrong, or when a page swallows the real one.
- `overlay-touch` — a soft translucent fingertip dot (~28px) with press-shrink
  and an expanding tap ripple; fades to faint when idle. Use with a portrait
  geometry (e.g. `width: 720, height: 1280`) for mobile-look 9:16 takes.
  Overlay takes record with the real cursor hidden entirely.

## Targeting

`studio_glide` / `studio_click` accept:

- `target` — a CSS selector; the glide lands slightly off-centre like a hand.
- `x`/`y` — page CSS coordinates.
- `x`/`y` + `screen: true` (xtest only) — RAW physical screen pixels, for
  things OUTSIDE the page: Chrome's own bubbles (save-password, notifications)
  and cross-origin iframe chrome. Read the coords off a `studio_screenshot`.

Selectors and typing act on the newest open page, so OAuth-style popups are
followed automatically; `screen: true` reaches anything visible regardless.

## What the studio already knows (so you don't relearn it)

- Zoom is REAL `Ctrl+=` / `Ctrl+-` keypresses. `--force-device-scale-factor`
  and CDP device-metrics emulation are both broken on a kiosk X display —
  never try to scale the UI any other way.
- Coordinates stay correct under zoom (CSS→physical mapping with the
  DIP-vs-CSS fix), and the device pixel ratio is re-read on every action.
- The overlay cursor is positioned imperatively every step — it does not
  freeze over iframes.
- Recording refuses to start while the machine's film rig display (:98) is
  recording, so a parallel production lane never drops frames. On that error:
  wait, retry.
- Kiosk window placement, root cursor, profile locks, stale displays from
  crashed sessions — all handled.

## Audio takes

Start with `studio_start({audio: true})` (sets up a dedicated PulseAudio null
sink and routes the studio Chrome into it — nothing plays out loud), then
`studio_record_start({take_name, audio: true})`. The page's sound is captured,
AAC-encoded, and muxed with end-anchored A/V alignment (reported as
`audio muxed (start +X.XXs)` — sync is good to ~0.2s). Needs `pulseaudio` +
`pulseaudio-utils` on the machine; without them, video capture still works and
audio start fails with a clear message.

## Fake microphone (voice-input takes)

The studio display has no real mic. `studio_start({fake_mic_wav: path})`
serves a WAV as the microphone: getUserMedia/STT hears the file, and the mic
permission auto-accepts (no bubble on camera). Rules:

- **16-bit PCM WAV** — Chrome's fake capture ignores other encodings; 48 kHz
  is the safe rate (`ffmpeg -i in.wav -ar 48000 -ac 1 -c:a pcm_s16le out.wav`).
- The path is a Chrome LAUNCH flag, but the file content is read at each
  capture start — **overwrite the same file between turns** to change what
  the next mic session hears (multi-utterance dialogues without a relaunch).
- Default is play-once-then-silence: after the utterance the stream feeds
  silence, so silence-based STT endpointing fires naturally. `fake_mic_loop:
  true` loops instead (ambience/soak tests — never for speech prompts).
- Leave ~0.5s of leading silence in the WAV: capture may connect a beat after
  the UI starts listening, and a hard-cut first word gets clipped.
- The mic WAV is what the page HEARS, not what the take plays — to let the
  viewer hear the spoken prompt, mix the same WAV into the edit as a
  foreground track aligned to the listening UI.

## Staging tips

- `studio_eval` is the staging escape hatch: set a site's localStorage theme
  (`localStorage.setItem('theme','dark')` then `studio_goto` again), seed
  state, read values back (it returns the JSON result).
- `theme: "dark"` on start only emulates `prefers-color-scheme` — sites with
  their own theme toggle need the localStorage route.
- After `studio_set_zoom`, Chrome shows its zoom bubble for a moment — wait
  ~3s (or navigate) before recording so it isn't in frame.
- `wipe_profile: true` gives a clean-slate first-run take, at the cost of any
  staged logins. Default keeps the profile.
- A crashed session leaves nothing behind that the next `studio_start` can't
  recover, but a take that was still recording when the session died is lost —
  always `studio_record_stop` before ending a session.

## Requirements

Linux with `Xvfb` and `xsetroot`, a Chromium-family browser, and ffmpeg/ffprobe
(`~/tools/bin` or `$FFMPEG`). Audio additionally needs PulseAudio (or
PipeWire's pulse shim) with `pactl`/`parec`/`paplay`.
