# Background ambience loops

Looped ambience beds mixed under call audio (see `phone/ambience.py`). A
route selects one via its **Background Ambience** dropdown (Phone Servers
tab); volume is the `phone_background_sound_gain` platform setting.

## Format (enforced at load)

- WAV, **8 kHz / 16-bit signed / mono** — the AudioSocket call format, so
  no resampling happens on the call path.
- 20–60 s long, **seamless loop** (the end must flow into the start without
  a click — crossfade the tail into the head when preparing a file).
- Mastered quiet-ish; the mixer applies the global gain (default 0.3) on
  top, and clips safely either way.

Convert any source with:

```
ffmpeg -i src.ext -ar 8000 -ac 1 -sample_fmt s16 -t 40 out.wav
```

## Files

| File | Template id | Licence |
|---|---|---|
| `call-center.wav` | `call_center` | CC0 — see SOURCES.md |
| `office.wav` | `office` | CC0 — see SOURCES.md |
| `city.wav` | `city` | CC0 — see SOURCES.md |
| `nature.wav` | `nature` | CC0 — see SOURCES.md |

A missing or malformed file degrades to no ambience for that template —
never a call failure.
