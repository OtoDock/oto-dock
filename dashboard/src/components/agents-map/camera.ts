/**
 * Camera-pose math for the 3D company map — pure functions over the
 * viewport, the camera's FOV/aspect and a layout cluster; no DOM, no
 * renderer. The map reads the live window/camera and calls these; the
 * tests feed fixed numbers.
 *
 * ONE pose function serves both the paged whole-map view (paging between
 * departments) and the stage-exit flight's landing — the exit used to keep
 * its own copy of the distance formula, which missed the viewScale factor
 * added by the physical-size framing: the flight landed short and the paged
 * machinery then pulled the camera out again (a visible second zoom-out
 * step on any window bigger than a 13" laptop).
 */
import * as THREE from 'three'
import type { MapCluster } from './layout'
import { STAGE_CAM_MAX } from './mapConstants'

/** Resolution-aware framing factor (operator ask, 2026-08-25): every fit
 * frames by FOV fraction, which draws the scene at the same RELATIVE size
 * on any screen — a 32" desktop showed the same departments physically
 * huge while the 2D UI around them stayed CSS-px sized. Pull the camera
 * back by the viewport's size relative to a ~13" laptop reference (sqrt
 * of the area ratio keeps it gentle), clamped so phones and small laptops
 * are untouched and big screens zoom out to show MORE map instead of
 * bigger cards. */
export const VIEW_REF_W = 1366
export const VIEW_REF_H = 800
export const VIEW_SCALE_MAX = 1.45

export function viewScaleFor(width: number, height: number): number {
  const f = Math.sqrt((width * height) / (VIEW_REF_W * VIEW_REF_H))
  return Math.min(VIEW_SCALE_MAX, Math.max(1, f))
}

/** Horizontal field of view (radians) for a vertical FOV in degrees; the
 * aspect floor keeps portrait phones from pulling the framing to a sliver. */
export function horizontalFov(fovDeg: number, aspect: number): number {
  const vfov = (fovDeg * Math.PI) / 180
  return 2 * Math.atan(Math.tan(vfov / 2) * Math.max(0.4, aspect))
}

/** Seats per amphitheater row for a viewport: how many 16-unit stage cards
 * fit the horizontal FOV of the map camera (48° vertical) at the far stage
 * framing clamp — 2 on a portrait phone, up to 8 on desktop — so no card
 * ever leaves the screen. The stage camera may sit `scale` (viewScale)
 * farther back on big screens, so the same screens fit WIDER rows — more
 * seats, like 2D shows more content, instead of bigger cards. */
export const ROW_CAP_MIN = 2
export const ROW_CAP_MAX = 8

export function rowCapFor(aspect: number, scale: number): number {
  const hfov = horizontalFov(48, aspect)
  const maxHalf = (Math.tan(hfov / 2) * STAGE_CAM_MAX * scale) / 1.04 - 5
  return Math.max(ROW_CAP_MIN, Math.min(ROW_CAP_MAX, Math.floor((maxHalf * 2) / 16) + 1))
}

/** The whole-map camera distance: from the horizontal FOV (portrait phones
 * pull back to the clamp, desktop sits a little closer), capped so the 3D
 * distance stays inside the free-roam zoom-out clamp, then scaled for big
 * screens. */
export const MAP_DIST_MIN = 112
export const MAP_DIST_MAX = 138
export const MAP_DIST_FOV_K = 88
/** Ground-level ~8° elevation: camera height as a fraction of its distance. */
export const MAP_ELEVATION = 0.1405

export function mapDistanceFor(fovDeg: number, aspect: number, scale: number): number {
  const hfov = horizontalFov(fovDeg, aspect)
  return Math.min(MAP_DIST_MAX, Math.max(
    MAP_DIST_MIN, MAP_DIST_FOV_K / Math.tan(hfov / 2),
  )) * scale
}

/** The composed whole-map pose for one department: camera BEHIND it,
 * outside the ring, at the ground-level elevation, looking across the
 * center. Shared by the stage-exit flight (its landing) and the paged
 * whole-map machinery (its goal), so the two can never disagree. */
export function mapPoseFor(
  fovDeg: number, aspect: number, cluster: MapCluster, scale: number,
): { cam: THREE.Vector3; look: THREE.Vector3 } {
  const dist = mapDistanceFor(fovDeg, aspect, scale)
  return {
    cam: new THREE.Vector3(
      cluster.outX * dist, dist * MAP_ELEVATION, cluster.outZ * dist,
    ),
    look: new THREE.Vector3(0, 0, 0),
  }
}

/** The altitude the exit flight's control point rises above both ends. */
export const EXIT_ARC_LIFT = 14
/** The flight's control point never sits closer to the ring center than
 * this — a straight lerp from a desktop stage pose cut through the empty
 * center and read as a broken two-step zoom. */
export const EXIT_ARC_MIN_RADIUS = 60

/** Stage-exit flight: one eased quadratic arc from the camera's current
 * pose to the whole-map pose, its control point pushed out to the wider of
 * the two radii and lifted above both ends — the path arcs AROUND the ring
 * at altitude. `getPoint(1)` is exactly `end`. */
export function stageExitPath(
  start: THREE.Vector3, end: THREE.Vector3,
): THREE.QuadraticBezierCurve3 {
  const mid = start.clone().lerp(end, 0.5)
  const rHoriz = Math.max(
    Math.hypot(start.x, start.z), Math.hypot(end.x, end.z), EXIT_ARC_MIN_RADIUS,
  )
  const midHoriz = Math.hypot(mid.x, mid.z)
  if (midHoriz > 0.001) {
    mid.x *= rHoriz / midHoriz
    mid.z *= rHoriz / midHoriz
  } else {
    mid.x = end.x
    mid.z = end.z
  }
  mid.y = Math.max(start.y, end.y) + EXIT_ARC_LIFT
  return new THREE.QuadraticBezierCurve3(start.clone(), mid, end.clone())
}
