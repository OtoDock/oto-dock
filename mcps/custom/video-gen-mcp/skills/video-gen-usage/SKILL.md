---
name: video-gen-usage
description: Generate AI videos, transitions between clips, and AI video edits. Use when a task needs new video footage, an AI transition bridge between two clips, or an object-level edit of an existing video.
---

## AI Video Generation, Transitions & Editing

- **`generate_video`** — Generate a video from a text description, optionally animating a source image (`image_path`). Saves an MP4 to the workspace and shows an inline player.
- **`generate_transition`** — Bridge two existing clips with an AI-generated transition. Returns the BRIDGE CLIP ONLY.
- **`edit_video`** — Edit an existing video with a text instruction (add/remove objects, restyle, relight, wardrobe, camera).

### Model selection

- **`veo-3.1-fast`** (default) — exact 4/6/8s clips, native audio, 1080p/4k, 9:16 vertical, ~$0.10/s (4k $0.30/s). In our hands-on testing this is clearly the best quality per cost — use it for most generations.
- **`veo-3.1`** — same controls at ~$0.40/s (4k $0.60/s), higher fidelity. Re-generate keepers on it when the deliverable demands top quality.
- **`omni-flash`** — 720p only, picks its own length (`duration_seconds` is a target, max ~10s), ~$0.10/s. Noticeably weaker visuals than Veo in our testing — use only when the flexible clip length is specifically wanted.
- Transitions — two complementary modes:
  - **`seedance`** (default, ~$0.18/s) watches both source clips and invents a *creative* transition — the impressive pick, but not pixel-continuous and occasionally cuts mid-bridge. Source clips are capped at 15s/50MB combined, so trim to the junction segments first (video-tools) when inputs are longer.
  - **`veo-3.1-fast`** (~$0.15/s) / **`veo-3.1`** (~$0.40/s) animate between the first clip's last frame and the second clip's first frame — *seamless by construction* (both joins are guaranteed to match, no internal cuts). Use these when clean continuity matters more than spectacle, or as the fix when a seedance bridge won't stop cutting. With a FAL key configured these run via fal.ai and work everywhere; without one they fall back to Google direct, which region-gates first/last-frame ("use case not supported"; EEA keys affected).

### Guidelines

- Describe videos like a shot brief: scene, subject, motion, camera work, lighting, mood, and what should be heard (all models generate audio). Vague prompts produce generic results.
- These are paid per-second calls — pick the shortest duration that serves the purpose, iterate on `veo-3.1-fast` or `omni-flash`, and only use 1080p/4k when the deliverable needs it.
- Generation takes 1–6 minutes per clip; this is normal — don't retry a call that is simply still running.
- `generate_transition` returns only the bridge clip. Assemble first clip + bridge + second clip with **video-tools** (declarative timeline, proper re-encode); never try to concatenate mismatched MP4s by hand. Set `aspect_ratio: "9:16"` for vertical footage.
- **Judge the bridge before assembling — is it worth what it cost?** If the generated transition reads as a plain dissolve between the two scenes (Veo first/last-frame tends to when the scenes are very different), don't pay to regenerate: assemble with a free video-tools `fade`/`dissolve` instead. AI bridges earn their cost when generated motion adds something a crossfade can't — a camera move, a morph, an invented traveling shot. Veo shines on same-scene/continuation joins; seedance on creative scene changes.
- **Verify the joins before delivering an assembled transition.** Compare the bridge's first/last frames against the neighbouring clips' junction frames (video-tools `render_frames`). Veo bridges are frame-anchored, so prefer HARD CUTS at the joins — but trim the bridge's duplicated endpoint frame first (its first frame ≈ clip A's last frame; showing both holds the image for 2 frames and reads as a stall). If a residual color/exposure or framing mismatch survives, fix the mismatch — grade the bridge to match its neighbours (video-tools) — rather than hiding it; a short crossfade at the joins is the last resort (and the norm for seedance, which is not pixel-continuous). A style `prompt` asking to "end exactly on the framing of the second clip's opening" also helps.
- **Verify the bridge has no internal hard cut.** Seedance is natively multi-shot and sometimes "transitions" by cutting mid-clip — render a few frames across the bridge and check the motion is continuous. If it cut, regenerate with a style prompt that hammers "one unbroken shot, no cuts" (the built-in template already demands this, but the model occasionally ignores it) — or switch to a `veo-3.1-fast` transition, which is frame-anchored and cannot cut.
- `edit_video` accepts 2–30 second inputs up to 1080p (~$0.28/s of output). Pass `duration_seconds` (the input clip's length, from `probe_media`) so the edit is priced correctly in the platform's cost tracking. For longer videos, cut out the segment to edit with video-tools, edit it, then splice it back. Aleph returns SILENT video — re-mux the original audio with video-tools when sound matters.
- Generated MP4s land in the workspace under `generated-assets/` (a bare filename via `save_path` also lands there). Use a relative path with a subfolder (e.g. `"projects/launch/hero.mp4"`) to organize into your own folder.
- The saved MP4 is web-safe: the inline player is pushed automatically; feed the file to video-tools for assembly, captions, or grading.
