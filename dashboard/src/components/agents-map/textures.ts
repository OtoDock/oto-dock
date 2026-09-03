// Late-arriving texture application for the 3D map — extracted so it is
// unit-testable without a WebGL context (same reason as layout.ts/gestures.ts).
//
// The failure class this guards (operator live-hit 2026-08-16, "the wooden
// base sometimes never finalizes its texture"): the map loads its heavy
// assets progressively, and the walnut slabs read `tex.wood` at BUILD time.
// The sky swaps itself in from its own loader callback, and the grass rides
// an `assetsTick` that re-runs the environment effect — but the slabs live in
// the dynamic effect, which has no such dep. So a wood texture that lands
// after the slabs are built has nothing to re-run, and every base keeps its
// flat fallback tint until some unrelated rebuild (paging to another
// department, an activity refetch) happens to read the texture again.
//
// Rather than rebuild the whole dynamic scene on a texture arrival — that
// tears down every agent chip's CSS3D DOM mid-load, which is exactly the
// entrance the progressive loading exists to protect — the loader patches the
// slabs that are already standing, the way the sky already patches itself.

import type * as THREE from 'three'

/** Set on the walnut slab meshes; also their raycast key. */
const DEPT_SLAB = 'dept'

/** The tint the slab wears until the texture arrives (a flat walnut brown,
 *  so a slow load still reads as wood rather than as a missing surface). */
export const WOOD_FALLBACK_TINT = 0x6b4f37

/**
 * Give a freshly-loaded wood texture to every department slab that was built
 * before it arrived. A no-op when nothing is built yet (the common warm-cache
 * path — those slabs read the texture at build time instead).
 *
 * Walks the live scene graph rather than a registry of materials on purpose:
 * the dynamic effect disposes and detaches its children on every rebuild, so
 * anything still reachable here is by definition still alive, and there is no
 * second bookkeeping site to keep in sync.
 */
export function applyWoodTexture(root: THREE.Object3D, wood: THREE.Texture): void {
  root.traverse((obj) => {
    if (obj.userData?.type !== DEPT_SLAB) return
    const mat = (obj as THREE.Mesh).material as THREE.MeshStandardMaterial | undefined
    // Already textured ⇒ built after the texture landed; leave it alone.
    if (!mat || Array.isArray(mat) || mat.map) return
    mat.map = wood
    // Drop the fallback tint, or it multiplies the texture into mud.
    mat.color.setHex(0xffffff)
    // Going from no map to a map flips USE_MAP — the program must recompile.
    mat.needsUpdate = true
  })
}
