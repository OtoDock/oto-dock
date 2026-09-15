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

import * as THREE from 'three'
import { DEFAULT_TINT } from './mapConstants'

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

// ---------------------------------------------------------------------------
// Canvas-drawn textures, the tint parser and the one-time card stylesheet
// (moved here from AgentsMap3D.tsx on 2026-09-10; the renderer effect, the
// environment builder and the card builder import them).

export function hexToRgb(hex: string): [number, number, number] {
  const t = hex.trim()
  const short = /^#?([0-9a-f]{3})$/i.exec(t)
  if (short) {
    const [r, g, b] = short[1].split('')
    return [parseInt(r + r, 16), parseInt(g + g, 16), parseInt(b + b, 16)]
  }
  const long = /^#?([0-9a-f]{6})$/i.exec(t)
  if (!long) return hexToRgb(DEFAULT_TINT)
  const v = parseInt(long[1], 16)
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255]
}

/** Soft radial glow texture (shared; stars). */
export function makeGlowTexture(): THREE.CanvasTexture | null {
  const c = document.createElement('canvas')
  c.width = c.height = 128
  const ctx = c.getContext('2d')
  if (!ctx) return null
  const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64)
  g.addColorStop(0, 'rgba(255,255,255,0.85)')
  g.addColorStop(0.25, 'rgba(255,255,255,0.28)')
  g.addColorStop(1, 'rgba(255,255,255,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, 128, 128)
  return new THREE.CanvasTexture(c)
}

/** Pre-load background: deep blue-green night (matches the panorama's tones
 * while it fetches — NOT the round-7 indigo). Bloom needs an OPAQUE scene,
 * so this is a texture, not a transparent clear. */
export function makeBackgroundTexture(): THREE.CanvasTexture | null {
  const c = document.createElement('canvas')
  c.width = c.height = 512
  const ctx = c.getContext('2d')
  if (!ctx) return null
  const g = ctx.createRadialGradient(256, 150, 40, 256, 210, 470)
  g.addColorStop(0, '#20313c')
  g.addColorStop(0.55, '#101c24')
  g.addColorStop(1, '#070d12')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, 512, 512)
  return new THREE.CanvasTexture(c)
}

/** Radial alpha fade for the terrain edge — with the fog gone this is the
 * whole near-to-pano blend. The solid plateau runs almost to the rim of
 * the plane: the ground must READ as reaching the panorama treeline,
 * with only a short dissolve where they meet. */
export function makeRadialAlphaTexture(): THREE.CanvasTexture | null {
  const c = document.createElement('canvas')
  c.width = c.height = 512
  const ctx = c.getContext('2d')
  if (!ctx) return null
  const g = ctx.createRadialGradient(256, 256, 0, 256, 256, 256)
  g.addColorStop(0, '#ffffff')
  g.addColorStop(0.8, '#ffffff')
  g.addColorStop(0.98, '#2a2a2a')
  g.addColorStop(1, '#000000')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, 512, 512)
  return new THREE.CanvasTexture(c)
}

/** Grass-blade sprite for the wind tufts (map + alphaMap): a handful of
 * tapered blades, bases dim and tips bright — the moonlight catches the
 * tips. Drawn once per environment build. */
export function makeGrassBladeTexture(): THREE.CanvasTexture | null {
  const c = document.createElement('canvas')
  c.width = c.height = 64
  const ctx = c.getContext('2d')
  if (!ctx) return null
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, 64, 64)
  for (let i = 0; i < 9; i++) {
    const bx = 5 + i * 6.4 + (i % 3) * 1.4
    const lean = (((i * 37) % 11) - 5) * 1.1
    const ht = 40 + ((i * 53) % 21)
    const grad = ctx.createLinearGradient(0, 64, 0, 64 - ht)
    grad.addColorStop(0, 'rgb(110,110,110)')
    grad.addColorStop(1, 'rgb(255,255,255)')
    ctx.beginPath()
    ctx.moveTo(bx - 1.7, 64)
    ctx.quadraticCurveTo(bx + lean * 0.4, 64 - ht * 0.6, bx + lean, 64 - ht)
    ctx.quadraticCurveTo(bx + lean * 0.5 + 1, 64 - ht * 0.55, bx + 1.7, 64)
    ctx.closePath()
    ctx.fillStyle = grad
    ctx.fill()
  }
  return new THREE.CanvasTexture(c)
}

/** One-time stylesheet for card activity — the PresenceHalo grammar on the
 * map, round 7: NO colored box-shadows anywhere (the operator killed the
 * "colored shadow" look) — activity lives in the BORDER (breathing alpha)
 * and the small chip pulse; fresh cards fade in. */
export function ensureMapStyles() {
  const prev = document.getElementById('odk-map-styles')
  if (prev) prev.remove()
  const style = document.createElement('style')
  style.id = 'odk-map-styles'
  style.textContent = `
@keyframes odkBorderBreathe {
  0%, 100% { border-color: var(--odk-b-lo); }
  50% { border-color: var(--odk-b-hi); }
}
.odk-border-breathe {
  animation: odkBorderBreathe 2.6s ease-in-out infinite;
}
@keyframes odkChipPulse {
  0%, 100% { box-shadow: 0 0 8px 2px var(--odk-glow-lo); }
  50% { box-shadow: 0 0 16px 5px var(--odk-glow-hi); }
}
.odk-chip-live {
  animation: odkChipPulse 2.4s ease-in-out infinite;
}
@keyframes odkFadeIn {
  from { opacity: 0; }
}
.odk-fade { animation: odkFadeIn 360ms ease; }
@media (prefers-reduced-motion: reduce) {
  .odk-border-breathe {
    animation: none;
    border-color: var(--odk-b-hi) !important;
  }
  .odk-chip-live { animation: none; }
  .odk-fade { animation: none; }
}`
  document.head.appendChild(style)
}
