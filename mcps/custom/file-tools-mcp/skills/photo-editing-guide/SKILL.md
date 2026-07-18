---
name: photo-editing-guide
description: Professional Lightroom-style photo editing workflow and parameter recipes for edit_image. Use when editing, retouching, enhancing, or color-grading photos.
---

## Photo Editing Guidelines

When editing photos, **always call `analyze_image` first** to see the histogram, zone balance, and color data before choosing adjustment values. This prevents over-editing.

Then apply edits with `edit_image` following this professional Lightroom-style workflow in order:

1. **Exposure & Light** (get the tones right first — check the analysis for current exposure):
   - `exposure` — correct overall brightness. **Direction: gamma > 1 BRIGHTENS,
     < 1 DARKENS.** Midtone-weighted — prefer it over `brightness` (a linear
     multiply that clips highlights faster). **Scale the move to the miss**
     (read mean luminance from `analyze_image`; a decent daylight photo lands
     around mean 100-130):
     - mildly off (mean within ~30 of target): gamma 1.05-1.2 (or 0.85-0.95 to darken)
     - clearly dark (mean 60-90): gamma 1.3-1.6
     - severely underexposed (mean < 60): gamma 1.8-2.2 is legitimate — the
       0.85-1.2 range will barely move it (measured: gamma 1.2 + shadows +30
       moved a mean-32 photo only to 42)
   - `highlights` — recover blown highlights (amount -15 to -50 for recovery, conservative)
   - `shadows` — open up dark areas (amount +10 to +35 for recovery, conservative)
   - Response curve note: `highlights`/`shadows` effects are progressive — below
     ~20 the change is nearly invisible and it steepens toward 50. Move in steps
     of 10-15 and read the Tone check line after each pass.
   - `whites` — white point (level is RELATIVE: positive brightens highlights, negative
     compresses them; 0 = no change)
   - `blacks` — black point (level is RELATIVE: **negative deepens toward pure black**
     (-20 to -40 to restore depth after dehaze or on faded scans), positive lifts
     (10-20 for the faded/matte look), **0 = no change** — it does NOT mean "pure black")
   - `contrast` — overall contrast (factor 1.03-1.15, subtle is better)

2. **Presence** (add punch and clarity):
   - `clarity` — midtone local contrast (amount 15-35 on 0-100 scale, don't exceed 40)
   - `dehaze` — cut through haze in outdoor shots (strength 0.15-0.35, don't exceed 0.5).
     Dehaze lowers the mean — on a genuinely hazy photo that drop IS the fix
     (the veil was false brightness), so judge by the median: compensate with a
     small exposure lift only if the median fell well below ~100 on a daylight
     shot. If shadows stay at 0% after dehazing (blacks still lifted), finish
     with `blacks` -20 to -40 to restore depth.

3. **Color** (warmth and tone):
   - `saturation` — overall color intensity (factor 1.03-1.12 for boost, 0.85-0.95 for muted)
   - `hsl_adjust` — fine-tune individual colors (values -100 to +100, but use -30 to +30 typically):
     - Boost orange/yellow saturation (+10 to +20) for warm skin tones
     - Shift greens toward teal (hue -10 to -15) for modern landscape look
     - Desaturate blues slightly (saturation -10 to -20) for cleaner skies
   - `split_tone` — cinematic color grading (keep strength LOW: 0.08-0.15):
     - Shadows: blue tint (hue 210-230, saturation 30-50, strength 0.08-0.15)
     - Highlights: warm tint (hue 25-40, saturation 30-50, strength 0.05-0.12)
     - Classic "orange and teal": shadows blue (hue 220), highlights orange (hue 30)

4. **Detail** (always denoise before sharpening):
   - `denoise` — reduce noise first (strength 2-3 for most photos, 4-5 for high-ISO)
   - `sharpness` — add sharpness after denoising (factor 1.15-1.4)

5. **Finishing touches**:
   - Slight `blacks` lift (level 10-20) for the modern faded/film look
   - Consider `color_temperature` for overall warmth (>5500 warm, <5500 cool)

**Critical rules for natural results:**
- Always use `analyze_image` first — never guess values blindly
- **Clipping in the SOURCE is unrecoverable.** If `analyze_image` shows more than
  a few % clipped whites (≥250) or blacks (≤5), that detail is gone — no
  `highlights`/`shadows` amount brings it back (measured: highlights -40 on a
  30%-blown sky left 28% clipped). Contain instead: darken/brighten the mids so
  the rest of the frame reads well, and say so rather than stacking recovery passes.
- **Verify numerically after: every tonal edit result ends with a "Tone check"
  line (mean/median luminance, zone %, clipping — before → after). Read it.
  If the mean moved further than planned, or clipped whites/blacks grew, fix it
  with ONE small follow-up edit — do not stack another full editing pass, and
  do not judge brightness from the inline preview alone.**
- The effects compound: 6 subtle adjustments > 1 heavy adjustment
- If shadows + highlights are both pushed hard, the image will look flat/HDR — be conservative
- Never exceed: highlights ±50, shadows ±35, clarity 40, dehaze 0.5, split_tone strength 0.15
- The "professional" look comes from restraint, not from maxing out sliders

**Tips:**
- Less is more — subtle adjustments compound. A 5% change in 6 things beats a 30% change in one.
- Always denoise BEFORE sharpening — sharpening amplifies noise.
- The faded blacks look (lifted black point) is a hallmark of professional editing.
- Split toning with complementary colors (blue shadows + orange highlights) creates depth.
