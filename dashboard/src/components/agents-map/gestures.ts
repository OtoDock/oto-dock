// Pure pointer-tracking bookkeeping for the 3D map's gestures — extracted so
// the stuck-pinch machinery is unit-testable without WebGL (the same reason
// the map math lives in layout.ts).
//
// The failure class this guards (live-hit 2026-08-15, "pinch randomly stops
// working on the phone"): touch pointers get IMPLICIT pointer capture to
// their pointerdown target, so when a CSS3D chip is torn down mid-gesture
// (the 15s activity-poll rebuild) the browser delivers pointercancel at the
// DETACHED node — it reaches neither the container nor document, and the
// tracker keeps a ghost entry forever. A ghost then corrupts every later
// pinch (distances measured against a dead point) or blocks it outright.

export interface TrackedPointer {
  x: number
  y: number
  /** last activity (ms clock) — the stale belt-and-braces guard. */
  t: number
  /** the pointerdown target — detached ⇒ the pointer can never report
   *  up/cancel here again (implicit touch capture), safe to drop. */
  el: Node | null
}

export type PointerMap = Map<number, TrackedPointer>

/** Ghost entries older than this are dropped at gesture starts. Kept well
 *  above any human pinch cadence; genuinely down-but-stationary pointers
 *  are re-registered by the move handler, so an over-eager prune heals on
 *  the pointer's next movement. */
export const STALE_POINTER_MS = 2000

/** Drop ghosts: detached pointerdown targets always; stale entries unless
 *  excluded (the active pinch's own fingers may legitimately hold still). */
export function prunePointers(
  map: PointerMap,
  now: number,
  isAttached: (el: Node | null) => boolean,
  exclude?: ReadonlySet<number>,
): void {
  for (const [id, p] of map) {
    if (exclude?.has(id)) continue
    if (!isAttached(p.el) || now - p.t > STALE_POINTER_MS) map.delete(id)
  }
}

/** The two most recently tracked pointer ids (Map preserves insertion
 *  order) — a pinch is always the newest two fingers, never "the first two
 *  entries" (which a ghost would poison). */
export function newestTwoIds(map: PointerMap): [number, number] | null {
  if (map.size < 2) return null
  const ids = [...map.keys()]
  return [ids[ids.length - 2], ids[ids.length - 1]]
}

/** Distance between two OWNED pinch pointers; null when either vanished
 *  (cancelled/pruned) — the caller re-seats or drops, never measures a
 *  ghost. */
export function pinchSpan(
  map: PointerMap, a: number, b: number,
): number | null {
  const pa = map.get(a)
  const pb = map.get(b)
  if (!pa || !pb) return null
  return Math.hypot(pa.x - pb.x, pa.y - pb.y)
}

/** Frame-normalized outward pressure past the stage zoom clamp. Feeds the
 *  SAME accumulator the wheel uses (decay 0.95/frame in the rAF, exit at
 *  WHEEL_EXIT_ACCUM), so repeated small pinches escape the stage exactly
 *  like repeated wheel notches — the single-gesture 44%-finger-travel
 *  requirement was physically unreachable once zoom sat at the floor.
 *  Proportional by design: sustained over-pinch crosses the threshold in a
 *  few hundred ms, a soft touch never does. dt-normalized so a 120 Hz
 *  phone doesn't get double the exit sensitivity of a 60 Hz one. */
export function pinchOutPressure(
  want: number, zoomMax: number, dtMs: number, gain = 200,
): number {
  const excess = want - zoomMax
  if (excess <= 0) return 0
  return excess * gain * Math.min(2, dtMs / 16.7)
}
