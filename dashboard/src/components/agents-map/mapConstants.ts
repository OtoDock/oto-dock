/** The 3D company map's tuning constants and the two overlay-state shapes
 * (split out of AgentsMap3D.tsx on 2026-09-10 — the same values, one
 * module every piece of the map imports them from). */
import type { MapNode } from './layout'

export interface PopupState {
  node: MapNode
  x: number
  y: number
}

export interface MenuState {
  x: number
  y: number
  departmentId: string | null
  departmentName?: string
}

export const FLOOR_Y = -9
// Cards are authored ~2x in DOM px (crisp when the GPU scales them DOWN)
// and shrunk into world units here. Overview pills grew in rounds 12 and
// 16 (operator: "a little bigger" against the night world, twice).
export const OVERVIEW_SCALE = 0.048
export const STAGE_SCALE = 0.046
export const LABEL_SCALE = 0.055
// Spark canvas extension past the card, authored px.
export const SPARK_MARGIN = 30
export const DEFAULT_TINT = '#146bb5'
// Delegation edges: DARK-GLASS RIBBON (round 18, thinned in round 19).
// Round 17's additive silver thread died on the bright meadow (additive
// only LIGHTENS — over lit grass there is nothing to add) and read as
// plain white over the walnut. The cards were the answer all along: they
// stay legible on every background because they are a DARK body with a
// lit edge — so the lines are the same material language: a slim
// near-black navy ribbon (normal blending — real contrast on wood AND
// bright grass) with one faint light-catch hugging its TOP edge
// (additive — the glass sheen, and the visibility over the darkest night
// areas), plus the traveling glint. The dark glass is the line; the
// sheen only suggests it (round 18's 0.38 lift split them into a "white
// line with a shadow under it" on phones — the glow must never outrank
// the glass).
// Dark navy-GLASS, not black — pure near-black read as a flat shadow
// stripe on the wood (round 19 phone check); a hint of blue in the body
// is what makes it a material.
export const EDGE_BODY = 0x161d33
export const EDGE_LIGHT = 0xdce8f4
// The light-catch rides this far above the body — tight enough that body
// and sheen fuse into ONE lit-edged ribbon at any viewing distance.
export const EDGE_LIGHT_LIFT = 0.12
// The composed stage framing never dollies past this — on a phone the
// widest row simply can't fit, so the row cap adapts instead (see
// stageRowCap) and the arc FILLS the width.
export const STAGE_CAM_MAX = 78
export const STAGE_CAM_MIN = 30
// Semantic-zoom thresholds: dollying closer than ENTER over/near a cluster
// enters its stage; the exit pose sits far above ENTER so the levels never
// flicker (hysteresis). Stage exit itself is gesture-accumulated because
// MapControls is disabled there.
export const ENTER_STAGE_DIST = 30
export const WHEEL_EXIT_ACCUM = 320
// After the stage-exit flight lands, zoom-OUT wheel pulses are ignored
// until BOTH this long has passed AND the wheel has gone quiet for
// WHEEL_HOLD_GAP_MS — the gesture that triggered the exit keeps
// delivering momentum for 1–2 s on a trackpad, and every pulse would push
// the freshly-landed whole-map pose further out. A zoom-in pulse releases
// the hold at once (a user who wheels in wants the stage back).
export const WHEEL_HOLD_MS = 450
export const WHEEL_HOLD_GAP_MS = 120
// Gentle in-stage zoom range (factor on the composed camera distance).
export const STAGE_ZOOM_MIN = 0.78
export const STAGE_ZOOM_MAX = 1.3
