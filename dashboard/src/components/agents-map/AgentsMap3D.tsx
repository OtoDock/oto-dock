/**
 * AgentsMap3D — the 3D company map, round 7: a TWO-LEVEL semantic-zoom map
 * inside a real environment.
 *
 * ENVIRONMENT (round 11 — the NIGHT world, continuity cut): a real 3D
 * near-field under a photographic panorama, the same architecture games
 * use. The distant scenery is a generated ground-level NIGHT panorama —
 * starry sky, milky way band, moonlit pine treeline in the mid-distance
 * (mirror-wrapped equirect — mathematically seamless; rendered SHARP, no
 * background blur: blur read as "outside the world") — doubling as the
 * environment map. There is NO scene fog: the near-field blends into the
 * pano purely via the terrain's radial alpha fade plus color-matching the
 * moonlit meadow. Continuity is geometric too: the pano's bright meadow
 * band is authored ~5° BELOW the equirect midline — exactly where the
 * TERRAIN's far edge appears from typical camera poses — and the terrain
 * itself is large enough (760²) that its edge meets the treeline instead
 * of ending mid-view (round 10's small plane + toy-scale 3D conifers and
 * flat lake broke the illusion; all three are gone). The meadow is real
 * displaced geometry (flattened into clearings around every base)
 * carrying a night grass texture with TWO-SCALE anti-tiling (mirrored
 * repeat + low-frequency vertex-color mottling), color-matched to the
 * measured pano band; instanced crossed-quad GRASS TUFTS sway in a
 * vertex-shader wind (the FPS gate freezes the clock). Lighting is a
 * moonlit hemisphere + cool key bright enough that the ground reads as
 * ALIVE as the photo. Every department sits SUNK into the ground on a
 * premium SQUARE WALNUT slab — brass inlay + corner caps + beveled rim
 * highlight + warm bollard lamps on the corners — and the STAGED
 * department gets a real SpotLight from above: entering a department
 * turns its light on. The slabs are the department tap targets; no
 * colored card shadows anywhere (dept-color spill pools were cut —
 * they read as stains).
 *
 * The map OPENS directly on the favorite agent's department stage (operator
 * round 7) — overview is one zoom-out away.
 *
 * OVERVIEW: agents are compact GLASS PILLS ([chip][name][⋯] — the same
 * glass language as the stage, smaller); spaced-caps dept name + member
 * count floats high over each dais. Manual delegation shows as AGGREGATED
 * dais↔dais lines only.
 *
 * STAGE (tap a dais, or just keep zooming in — wheel/pinch IS the level
 * switch, Google-Maps style, no back buttons): the camera flies to a
 * composed Vision-Pro framing from INSIDE the ring looking outward; members
 * arrange into amphitheater arc rows per level — the HEAD row is FRONT and
 * closest (top role reads biggest; deeper ranks step away and higher) — as
 * ChartHop-flavored two-line cards (avatar + name / level). Row seat count
 * adapts to the screen (2 on phones, up to 8 on desktop) so cards never
 * leave the screen. Camera freedom is locked to rubber-banded parallax on
 * drag; a horizontal SWIPE dollies to the prev/next department around the
 * ring (Independents last), and a subtle bottom pager strip — a
 * center-locked scroller on phones — is the only lateral nav chrome.
 * Zooming out (wheel accumulate / pinch-in) returns to overview with
 * hysteresis.
 *
 * EDGES are DIRECTIONAL (the delegation model: each agent owns only its
 * OUTGOING targets): calm solid Line2 curves — a crisp amber core over a
 * faint white glow underlay — TRIMMED to stop at the panel edges, with a
 * small arrow tip + light halo sitting right at the target's edge (one
 * arrow = one-way, both ends = mutual). WebGL always renders under the
 * CSS3D card layer, so edges never crossing a card is load-bearing. The
 * map's "Link…" mode creates ONE direction (matching config semantics);
 * "Unlink" still severs both directions as a convenience.
 *
 * Camera discipline (the round-3 "pinned on one agent" bug): programmatic
 * overview tweens are cancelled the moment the user grabs the controls and
 * carry a convergence deadline; the favorite-department opening placement
 * is INSTANT, never a tween. On stage MapControls is disabled entirely and
 * the camera pose is owned by the stage state machine.
 *
 * Bloom: UnrealBloomPass behind a runtime quality gate — sustained slow
 * frames permanently drop the composer back to the plain renderer.
 *
 * WebGL discipline: renderer creation is guarded (failure reports up → the
 * page falls back to the grid, which is also why jsdom tests survive), and
 * unmount disposes the scene AND force-loses the context — browsers cap
 * live WebGL contexts and xterm already taught us leaked contexts kill
 * later pages.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import * as THREE from 'three'
import { MapControls } from 'three/examples/jsm/controls/MapControls.js'
import {
  CSS3DRenderer,
  CSS3DSprite,
} from 'three/examples/jsm/renderers/CSS3DRenderer.js'
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js'
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js'
import { Line2 } from 'three/examples/jsm/lines/Line2.js'
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js'
import { useQueryClient } from '@tanstack/react-query'
import { useAgents, useSetDefaultAgent, useUpdateAgent } from '../../api/agents'
import {
  useAgentsActivity,
  useCreateDepartment,
  useDelegationEdges,
  useDepartments,
  useAdminAddUserAgent,
} from '../../api/departments'
import { apiFetch } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import { pushEscHandler } from '../../lib/escStack'
import {
  newestTwoIds,
  pinchOutPressure,
  pinchSpan,
  prunePointers,
  type PointerMap,
} from './gestures'
import {
  computeGrassScatter,
  computeHeat,
  computeMapLayout,
  computeStageLayout,
  terrainHeightAt,
  RING_RADIUS,
  type MapCluster,
  type MapNode,
} from './layout'
import { applyWoodTexture, WOOD_FALLBACK_TINT } from './textures'
// The NIGHT world (operator round 10 — all generated in-house): a
// ground-level starry-night panorama as the distant scenery (mirror-wrapped
// equirect — mathematically seamless), walnut for the square bases, moonlit
// grass for the real 3D ground the bases sit in.
import skyUrl from '../../assets/agents-map/sky-night.jpg'
// 256×128 preview, ~2.5KB → inlined as a data URI by vite: the night is
// on screen the moment the chunk runs, while the 4K pano streams behind.
import skyLowUrl from '../../assets/agents-map/sky-night-low.jpg'
import woodUrl from '../../assets/agents-map/wood-walnut.jpg'
import grassUrl from '../../assets/agents-map/ground-grass.jpg'

export interface AgentsMap3DProps {
  /** WebGL missing / renderer creation failed → parent falls back to grid. */
  onUnavailable: () => void
  /** Admin toggle: hide everything I'm not a member of. */
  hideNonMember: boolean
  /** Flip the admin toggle (rendered INSIDE the map, top-left). */
  onToggleHideNonMember: () => void
  /** Open the classic Departments editor (optionally focused on one). */
  onOpenDepartments: (departmentId?: string) => void
  /** Open the same Create Agent modal the grid view uses (page-hosted). */
  onCreateAgent: () => void
  /** Open the same community browser the grid view uses (page-hosted). */
  onBrowseCommunity: () => void
}

interface PopupState {
  node: MapNode
  x: number
  y: number
}

interface MenuState {
  x: number
  y: number
  departmentId: string | null
  departmentName?: string
}

const FLOOR_Y = -9
// Cards are authored ~2x in DOM px (crisp when the GPU scales them DOWN)
// and shrunk into world units here. Overview pills grew in rounds 12 and
// 16 (operator: "a little bigger" against the night world, twice).
const OVERVIEW_SCALE = 0.048
const STAGE_SCALE = 0.046
const LABEL_SCALE = 0.055
// Spark canvas extension past the card, authored px.
const SPARK_MARGIN = 30
const DEFAULT_TINT = '#146bb5'
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
const EDGE_BODY = 0x161d33
const EDGE_LIGHT = 0xdce8f4
// The light-catch rides this far above the body — tight enough that body
// and sheen fuse into ONE lit-edged ribbon at any viewing distance.
const EDGE_LIGHT_LIFT = 0.12
// The composed stage framing never dollies past this — on a phone the
// widest row simply can't fit, so the row cap adapts instead (see
// stageRowCap) and the arc FILLS the width.
const STAGE_CAM_MAX = 78
const STAGE_CAM_MIN = 30
// Semantic-zoom thresholds: dollying closer than ENTER over/near a cluster
// enters its stage; the exit pose sits far above ENTER so the levels never
// flicker (hysteresis). Stage exit itself is gesture-accumulated because
// MapControls is disabled there.
const ENTER_STAGE_DIST = 30
const WHEEL_EXIT_ACCUM = 320
// Gentle in-stage zoom range (factor on the composed camera distance).
const STAGE_ZOOM_MIN = 0.78
const STAGE_ZOOM_MAX = 1.3

function hexToRgb(hex: string): [number, number, number] {
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
function makeGlowTexture(): THREE.CanvasTexture | null {
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
function makeBackgroundTexture(): THREE.CanvasTexture | null {
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
function makeRadialAlphaTexture(): THREE.CanvasTexture | null {
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
function makeGrassBladeTexture(): THREE.CanvasTexture | null {
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
function ensureMapStyles() {
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

interface SparkParticle {
  x: number; y: number; vx: number; vy: number
  age: number; life: number; size: number
}

/** Per-streaming-card particle field: a small canvas around the card,
 * painted from the main rAF loop (PresenceHalo spark grammar — sparks
 * spawn on the border and fly outward, density scales with heat). */
interface SparkField {
  canvas: HTMLCanvasElement
  ctx: CanvasRenderingContext2D
  pill: HTMLElement
  rgb: [number, number, number]
  heat: number
  particles: SparkParticle[]
  sized: boolean
}

interface StageParallax {
  x: number
  y: number
  tx: number
  ty: number
  dragging: boolean
}

interface SceneBag {
  renderer: THREE.WebGLRenderer
  labels: CSS3DRenderer
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  controls: MapControls
  dynamic: THREE.Group
  raycaster: THREE.Raycaster
  raf: number
  disposed: boolean
  camTarget: THREE.Vector3 | null
  lookTarget: THREE.Vector3 | null
  camDeadline: number
  /** The user has grabbed the controls at least once — after that the map
   * never repositions the camera on its own (favorite centering skips). */
  userMoved: boolean
  frame: number
  lastNow: number
  slowFrames: number
  composer: EffectComposer | null
  bloomOn: boolean
  sparks: SparkField[]
  hitTargets: THREE.Object3D[]
  reducedMotion: boolean
  /** Stage state machine (null = overview). While staged MapControls is
   * disabled and the camera pose = lerp toward the composed goal + the
   * rubber-band parallax offset. */
  stageId: string | null
  stageCamPos: THREE.Vector3 | null
  stageLook: THREE.Vector3 | null
  stageCamGoal: THREE.Vector3 | null
  stageLookGoal: THREE.Vector3 | null
  par: StageParallax
  wheelOut: number
  /** Gentle in-stage zoom: a factor on the composed camera distance
   * (wheel/pinch move the target, the current value chases it). Zooming
   * out past the max is what exits to overview. */
  zoomT: number
  zoomC: number
  /** Set when a swipe just ended with real movement — the very next click
   * (the browser fires it even after a long drag if down+up hit the same
   * element) is swallowed so paging never opens an agent chat. */
  swallowClick: boolean
  /** The next stage enter places the camera INSTANTLY at the composed pose
   * (the map opens ON the favorite's stage — no fly-in on load). */
  snapStage: boolean
  /** True until the FIRST focus lands (or the user grabs the controls):
   * the first-focus decision now happens in RENDER (before the bag can be
   * mutated), so the pose effects read this flag to snap instead of fly —
   * the render-phase replacement for arming snapStage/mapSnap directly. */
  firstFocus: boolean
  /** Stage-exit flight: one continuous eased arc to the whole-map framing
   * (a straight lerp from a desktop stage pose cut through the empty ring
   * center and read as a broken two-step zoom). Controls stay disabled
   * until the flight lands. */
  exitFly: {
    curve: THREE.QuadraticBezierCurve3
    lookFrom: THREE.Vector3
    lookTo: THREE.Vector3
    t: number
  } | null
  /** Whole-map PAGED mode (round 16): the overview composes like the
   * stage — the camera stands behind one department (mapId) and swipes /
   * pager taps rotate around the ring; wheel/pinch drive mapZoom and
   * diving past the clamp enters that department's stage. MapControls
   * only drives the legacy free-roam fallback (mapId === null). */
  mapId: string | null
  mapCamPos: THREE.Vector3 | null
  mapLook: THREE.Vector3 | null
  mapCamGoal: THREE.Vector3 | null
  mapLookGoal: THREE.Vector3 | null
  mapZoomT: number
  mapZoomC: number
  /** Next whole-map pose placement is INSTANT (no-favorite open). */
  mapSnap: boolean
  /** Fat-line materials need the viewport size — refreshed on resize. */
  lineRes: THREE.Vector2
  lineMats: LineMaterial[]
  /** One traveling light glint per delegation edge — advanced in the rAF
   * (skipped under reduced motion). Rebuilt with the scene contents; the
   * phase is a pure function of the edge key + wall clock, so the 15s
   * activity-poll rebuild never visibly restarts the motion. */
  edgeGlints: {
    sprite: THREE.Sprite
    curve: THREE.Curve<THREE.Vector3>
    phase: number
    speed: number
  }[]
  /** The living environment (terrain, grass) — rebuilt only on layout /
   * asset changes, never on heat/edge refetches. */
  envGroup: THREE.Group
  /** The wind clock (uTime uniform of the tuft material) — advanced in the
   * rAF while envAnimOn; the FPS gate freezes it alongside dropping bloom. */
  wind: { value: number } | null
  envAnimOn: boolean
  /** Per-rebuild environment textures (alpha fade, water normals, blades) —
   * material.dispose() never disposes maps, so these are tracked and freed
   * on every env rebuild and at teardown. */
  envTex: THREE.Texture[]
  tex: {
    glow: THREE.Texture | null
    bg: THREE.Texture | null
    wood: THREE.Texture | null
    grass: THREE.Texture | null
    sky: THREE.Texture | null
    skyLow: THREE.Texture | null
  }
}

function disposeObject(obj: THREE.Object3D) {
  obj.traverse((o) => {
    const any = o as unknown as {
      geometry?: { dispose(): void }
      material?: THREE.Material & { map?: THREE.Texture | null }
    }
    any.geometry?.dispose()
    // Shared textures are disposed at teardown; blob textures via dynTex.
    any.material?.dispose()
  })
}

function updateSparks(bag: SceneBag) {
  for (const f of bag.sparks) {
    if (!f.sized) {
      // The card only has layout after the CSS3D renderer attaches it.
      const w = f.pill.offsetWidth
      const h = f.pill.offsetHeight
      if (!w || !h) continue
      f.canvas.width = w + SPARK_MARGIN * 2
      f.canvas.height = h + SPARK_MARGIN * 2
      f.sized = true
    }
    const { ctx } = f
    const W = f.canvas.width
    const H = f.canvas.height
    ctx.clearRect(0, 0, W, H)
    const rx = SPARK_MARGIN
    const ry = SPARK_MARGIN
    const rw = W - SPARK_MARGIN * 2
    const rh = H - SPARK_MARGIN * 2
    const want = Math.min(2, Math.ceil(0.5 + f.heat * 1.8))
    for (let i = 0; i < want && f.particles.length < 36; i++) {
      // A point on one of the four edges + its outward normal.
      const side = (Math.random() * 4) | 0
      const u = Math.random()
      let px = 0, py = 0, nx = 0, ny = 0
      if (side === 0) { px = rx + u * rw; py = ry; ny = -1 }
      else if (side === 1) { px = rx + u * rw; py = ry + rh; ny = 1 }
      else if (side === 2) { px = rx; py = ry + u * rh; nx = -1 }
      else { px = rx + rw; py = ry + u * rh; nx = 1 }
      const speed = (18 + Math.random() * 30) * (0.7 + f.heat * 0.6)
      f.particles.push({
        x: px, y: py,
        vx: nx * speed + (Math.random() - 0.5) * 10,
        vy: ny * speed + (Math.random() - 0.5) * 10,
        age: 0, life: 0.5 + Math.random() * 0.45,
        size: 1.4 + Math.random() * 2,
      })
    }
    const [r, g, b] = f.rgb
    for (let i = f.particles.length - 1; i >= 0; i--) {
      const p = f.particles[i]
      p.age += 1 / 60
      if (p.age >= p.life) { f.particles.splice(i, 1); continue }
      p.x += p.vx / 60
      p.y += p.vy / 60
      const fade = 1 - p.age / p.life
      ctx.beginPath()
      ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2)
      ctx.fillStyle = `rgba(${r},${g},${b},${(0.75 * fade).toFixed(3)})`
      ctx.fill()
    }
  }
}

// Scratch vectors for the per-frame stage camera math (single map instance).
const TMP_FWD = new THREE.Vector3()
const TMP_RIGHT = new THREE.Vector3()
const TMP_LOOK = new THREE.Vector3()
const WORLD_UP = new THREE.Vector3(0, 1, 0)

/** Resolution-aware framing factor (operator ask, 2026-08-25): every fit
 * in this file frames by FOV fraction, which draws the scene at the same
 * RELATIVE size on any screen — a 32" desktop showed the same departments
 * physically huge while the 2D UI around them stayed CSS-px sized. Pull
 * the camera back by the viewport's size relative to a ~13" laptop
 * reference (sqrt of the area ratio keeps it gentle), clamped so phones
 * and small laptops are untouched and big screens zoom out to show MORE
 * map instead of bigger cards. Read fresh at every fit — window resizes
 * self-correct on the next framing. */
const VIEW_REF_W = 1366
const VIEW_REF_H = 800
function viewScale(): number {
  if (typeof window === 'undefined') return 1
  const f = Math.sqrt(
    (window.innerWidth * window.innerHeight) / (VIEW_REF_W * VIEW_REF_H))
  return Math.min(1.45, Math.max(1, f))
}

/** The composed whole-map pose for one department: camera BEHIND it,
 * outside the ring, at the ground-level ~8° elevation, looking across the
 * center — distance from the horizontal FOV (portrait phones pull back to
 * the clamp, desktop sits a little closer; big screens pull back by
 * viewScale on top). Shared by the stage-exit flight and the paged
 * whole-map machinery. */
function mapPoseFor(
  camera: THREE.PerspectiveCamera, cluster: MapCluster,
): { cam: THREE.Vector3; look: THREE.Vector3 } {
  const vfov = (camera.fov * Math.PI) / 180
  const hfov = 2 * Math.atan(
    Math.tan(vfov / 2) * Math.max(0.4, camera.aspect),
  )
  const dist = Math.min(138, Math.max(112, 88 / Math.tan(hfov / 2)))
    * viewScale()
  return {
    cam: new THREE.Vector3(
      cluster.outX * dist, dist * 0.1405, cluster.outZ * dist,
    ),
    look: new THREE.Vector3(0, 0, 0),
  }
}

export default function AgentsMap3D({
  onUnavailable, hideNonMember, onToggleHideNonMember, onOpenDepartments,
  onCreateAgent, onBrowseCommunity,
}: AgentsMap3DProps) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { user, refreshUser } = useAuth()
  const isAdmin = user?.role === 'admin'
  // Admin "Everything" view needs ?all=true — without it the backend
  // filters the list to the admin's own checkbox agents and non-member
  // agents can never appear on the map.
  // keepPrevious: the admin scope toggle flips the query KEY after ui-prefs
  // land — without placeholder data the list drops to [] for a beat and
  // every chip is torn down and rebuilt a second time on load.
  const { data: agents = [], isSuccess: agentsReady, isPending: agentsPending } =
    useAgents({ all: isAdmin && !hideNonMember, keepPrevious: true })
  const {
    data: departments = [], isSuccess: deptsReady, isPending: deptsPending,
  } = useDepartments()
  const { data: activity = [] } = useAgentsActivity()
  const { data: edges = [] } = useDelegationEdges()
  const setDefault = useSetDefaultAgent()
  const addMe = useAdminAddUserAgent()
  const updateAgent = useUpdateAgent()

  const [popup, setPopup] = useState<PopupState | null>(null)
  const [menu, setMenu] = useState<MenuState | null>(null)
  /** Semantic-zoom level: null = overview, else the staged cluster
   * ('' = the Independent cluster — a real page like any department). */
  const [stage, setStage] = useState<{ deptId: string } | null>(null)
  /** Whole-map paged position: the department the camera stands behind at
   * overview (null = legacy free-roam fallback). */
  const [mapDept, setMapDept] = useState<string | null>(null)
  const [linkFrom, setLinkFrom] = useState<string | null>(null)
  const [moveFrom, setMoveFrom] = useState<string | null>(null)
  const [levelPick, setLevelPick] = useState<{
    slug: string
    departmentId: string
    x: number
    y: number
  } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [newDeptName, setNewDeptName] = useState<string | null>(null)

  const containerRef = useRef<HTMLDivElement>(null)
  const bagRef = useRef<SceneBag | null>(null)
  const centeredOnce = useRef(false)
  // Bumped when the grass texture lands, so the ENVIRONMENT effect rebuilds
  // the terrain with it. Deliberately not read by the dynamic effect: its
  // one late asset (wood) is patched onto the standing slabs instead, so a
  // texture arrival never tears down the agent chips (see textures.ts).
  const [assetsTick, setAssetsTick] = useState(0)

  // For admins, grayed = "not a member" (they can ACCESS everything, so the
  // dept feed's accessible flags never gray anything for them).
  const memberOf = useMemo(
    () => (isAdmin ? new Set(user?.agents ?? []) : undefined),
    [isAdmin, user?.agents],
  )
  const layout = useMemo(
    () => computeMapLayout(agents, departments, {
      hideNonMember: isAdmin && hideNonMember,
      memberOf,
    }),
    [agents, departments, hideNonMember, isAdmin, memberOf],
  )
  const heat = useMemo(() => computeHeat(activity), [activity])
  const liveSet = useMemo(
    () => new Set(activity.filter((a) => a.live).map((a) => a.name)),
    [activity],
  )
  const streamingSet = useMemo(
    () => new Set(activity.filter((a) => a.streaming).map((a) => a.name)),
    [activity],
  )
  const nodeBySlug = useMemo(
    () => new Map(layout.nodes.map((n) => [n.slug, n])),
    [layout],
  )
  /** The staged cluster's amphitheater (null at overview or if the staged
   * cluster vanished from the layout — the stage effect exits then).
   * The seat count per row adapts to the screen: how many cards fit the
   * horizontal FOV at the far framing clamp — 2 on a portrait phone,
   * up to 8 on desktop — so no card ever leaves the screen. */
  const stageData = useMemo(() => {
    if (!stage) return null
    const cluster = layout.clusters.find(
      (c) => c.departmentId === stage.deptId,
    )
    if (!cluster) return null
    const members = layout.nodes.filter(
      (n) => n.departmentId === stage.deptId,
    )
    const aspect = bagRef.current?.camera.aspect
      ?? (typeof window !== 'undefined'
        ? window.innerWidth / Math.max(1, window.innerHeight)
        : 1.6)
    const vfov = (48 * Math.PI) / 180
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * Math.max(0.4, aspect))
    // The stage camera may sit viewScale farther back on big screens, so
    // the same screens fit WIDER rows — more seats, like 2D shows more
    // content, instead of bigger cards.
    const maxHalf = (Math.tan(hfov / 2) * STAGE_CAM_MAX * viewScale()) / 1.04 - 5
    const rowCap = Math.max(2, Math.min(8, Math.floor((maxHalf * 2) / 16) + 1))
    return { cluster, arc: computeStageLayout(cluster, members, { rowCap }) }
  }, [stage, layout])

  // The map OPENS directly ON the favorite agent's department stage
  // (operator round 7) — decided in RENDER on purpose (2026-08-15): the old
  // post-paint effect let the chips build once at whole-map scatter, paint,
  // and only then rebuild into the amphitheater — the ~1s load jump. A
  // render-phase setState is the legal derived-state pattern (React
  // re-invokes before commit; no effect ever sees the undecided render), so
  // the FIRST data-bearing chip build is already staged. bag mutations stay
  // out of render — bag.firstFocus (armed at construction) makes the pose
  // effects SNAP instead of fly. Round-15 rule intact: wait for BOTH
  // queries (agents alone files everyone under Independent). Round-3 rule
  // intact: a user who grabbed the controls is never repositioned.
  if (!centeredOnce.current && agentsReady && deptsReady
      && layout.nodes.length > 0 && !bagRef.current?.userMoved) {
    centeredOnce.current = true
    const fav = user?.default_agent ? nodeBySlug.get(user.default_agent) : undefined
    if (fav) {
      setStage({ deptId: fav.departmentId })
    } else if (layout.clusters[0]) {
      // No favorite: open on the paged whole-map view behind the first
      // department (round 16 — free-roam is only the last resort).
      setMapDept(layout.clusters[0].departmentId)
    }
  }
  // The favorite's department (null until resolvable) — the pose-park
  // writer gates on it so the parked pose is BY CONSTRUCTION the
  // favorite's own stage, never the last visited department (live-hit
  // 2026-08-15: "map opens rotated to Engineering").
  const favDeptRef = useRef<string | null>(null)
  useEffect(() => {
    favDeptRef.current = user?.default_agent
      ? nodeBySlug.get(user.default_agent)?.departmentId ?? null
      : null
  }, [nodeBySlug, user?.default_agent])

  // Stable refs for handlers built inside the scene effect / rAF loop.
  const openAgent = useCallback((node: MapNode, x: number, y: number) => {
    if (node.grayed) {
      setPopup({ node, x, y })
    } else {
      navigate(`/chat/${node.slug}`)
    }
  }, [navigate])
  const openAgentRef = useRef(openAgent)
  useEffect(() => { openAgentRef.current = openAgent }, [openAgent])
  const linkFromRef = useRef(linkFrom)
  useEffect(() => { linkFromRef.current = linkFrom }, [linkFrom])
  const moveFromRef = useRef(moveFrom)
  useEffect(() => { moveFromRef.current = moveFrom }, [moveFrom])

  // --- renderer lifecycle -------------------------------------------------
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    ensureMapStyles()
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
      if (!renderer.getContext()) throw new Error('no webgl context')
    } catch {
      onUnavailable()
      return
    }
    const scene = new THREE.Scene()
    // Deliberately NO scene.fog (the round-9 fog band washed the panorama
    // out): the near-to-far blend is the terrain's radial alpha fade plus
    // the color-matched night ground.
    // Near 0.5 (nothing ever gets closer than ~15) buys back the depth
    // precision the 1500 far plane spends — the meadow now runs 1200
    // units so its corners must clear the frustum.
    const camera = new THREE.PerspectiveCamera(
      48, el.clientWidth / Math.max(1, el.clientHeight), 0.5, 1500,
    )
    const vs0 = viewScale()
    camera.position.set(0, 56 * vs0, 92 * vs0)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    renderer.setSize(el.clientWidth, el.clientHeight)
    renderer.domElement.style.position = 'absolute'
    renderer.domElement.style.inset = '0'
    el.appendChild(renderer.domElement)
    renderer.domElement.style.touchAction = 'none'

    // Real-3D DOM layer above the canvas (cards + dept typography):
    // CSS3DSprite billboards each element toward the camera and the
    // perspective scale comes from the browser's own 3D transform, so text
    // stays crisp DOM. Root ignores the pointer; each card opts back in.
    const labels = new CSS3DRenderer()
    labels.setSize(el.clientWidth, el.clientHeight)
    labels.domElement.style.position = 'absolute'
    labels.domElement.style.inset = '0'
    labels.domElement.style.pointerEvents = 'none'
    labels.domElement.style.overflow = 'hidden'
    el.appendChild(labels.domElement)

    // MapControls owns the OVERVIEW level only: one finger / left-drag PANS
    // across the floor, two fingers / right-drag orbit, pinch/wheel zooms —
    // and dollying close enough to a cluster IS the way into its stage.
    const controls = new MapControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08
    controls.screenSpacePanning = false // pan along the floor plane
    controls.minDistance = 20
    // Round 11: 230 let the whole company shrink to a speck against the
    // panorama's huge trees — the world must always fill the frame.
    // viewScale: big screens may dolly out proportionally farther so the
    // whole-map pose (also viewScale-scaled) stays reachable by hand.
    controls.maxDistance = 150 * vs0

    // The map OPENS on the favorite's stage, but that pose needs the
    // agents+departments queries — until they land the camera would show
    // a zoomed-out nowhere and then jump (round 12 complaint). Parking it
    // at the FAVORITE's remembered stage pose makes the wait look like the
    // destination; the real snap lands ≈ where we already are. The writer
    // only persists the favorite's own stage (see the stage-pose effect),
    // and the `f` stamp guards a favorite change between sessions — a
    // mismatch just means the default pose, corrected by the first snap.
    // (v2: v1 entries had no identity and parked the LAST VISITED
    // department — the "opens rotated to Engineering" live-hit.)
    try {
      const raw = localStorage.getItem('odk-map-pose-v2')
      if (raw) {
        const p = JSON.parse(raw) as { c: number[]; t: number[]; f?: string }
        if (Array.isArray(p.c) && p.c.length === 3
          && Array.isArray(p.t) && p.t.length === 3
          && [...p.c, ...p.t].every(Number.isFinite)
          && !!p.f && p.f === user?.default_agent) {
          camera.position.set(p.c[0], Math.max(2, p.c[1]), p.c[2])
          controls.target.set(p.t[0], p.t[1], p.t[2])
        }
      }
    } catch { /* corrupted pref — the default pose is fine */ }
    // Never dip under the meadow: the polar clamp stops at horizon-ish
    // (round 9's 0.52π let the camera dive below the ground plane).
    controls.maxPolarAngle = Math.PI * 0.46
    controls.minPolarAngle = Math.PI * 0.14

    const tex: SceneBag['tex'] = {
      glow: makeGlowTexture(),
      bg: makeBackgroundTexture(),
      wood: null,
      grass: null,
      sky: null,
      skyLow: null,
    }
    for (const t of Object.values(tex)) {
      if (t) t.colorSpace = THREE.SRGBColorSpace
    }
    // Opaque scene background — required for bloom. The night gradient shows
    // until the starry pano arrives, then it takes over as BOTH the sky and
    // the environment map (moonlight bounce on the wood and brass).
    if (tex.bg) scene.background = tex.bg
    // Progressive sky (round 12, operator idea): the inlined preview
    // lights the world instantly (blurred — at 256px chunkiness would
    // show), the full 4K pano swaps in sharp when it lands.
    new THREE.TextureLoader().load(skyLowUrl, (sky) => {
      if (bag.disposed || tex.sky) {
        sky.dispose()
        return
      }
      sky.mapping = THREE.EquirectangularReflectionMapping
      sky.colorSpace = THREE.SRGBColorSpace
      tex.skyLow = sky
      scene.background = sky
      scene.backgroundIntensity = 1
      scene.backgroundBlurriness = 0.12
      scene.environment = sky
      scene.environmentIntensity = 0.9
    })
    new THREE.TextureLoader().load(skyUrl, (sky) => {
      if (bag.disposed) {
        sky.dispose()
        return
      }
      sky.mapping = THREE.EquirectangularReflectionMapping
      sky.colorSpace = THREE.SRGBColorSpace
      sky.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy())
      tex.sky = sky
      scene.background = sky
      scene.backgroundIntensity = 1
      // SHARP on purpose (round 11): the round-10 soft focus read as
      // "outside the world" — a game horizon is crisp.
      scene.backgroundBlurriness = 0
      scene.environment = sky
      scene.environmentIntensity = 0.9
      tex.skyLow?.dispose()
      tex.skyLow = null
    })
    new THREE.TextureLoader().load(woodUrl, (wood) => {
      if (bag.disposed) {
        wood.dispose()
        return
      }
      wood.colorSpace = THREE.SRGBColorSpace
      wood.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy())
      tex.wood = wood
      // Slabs built BEFORE this landed have no rebuild coming (the dynamic
      // effect deliberately does not re-run on asset arrival — see
      // textures.ts), so hand the texture straight to them. Slabs built
      // after read tex.wood themselves. No assetsTick: only the env effect
      // watches it, and the terrain does not use wood.
      applyWoodTexture(bag.dynamic, wood)
    })
    new THREE.TextureLoader().load(grassUrl, (grass) => {
      if (bag.disposed) {
        grass.dispose()
        return
      }
      grass.colorSpace = THREE.SRGBColorSpace
      // Mirrored repeat makes ANY texture tile seamlessly; the coarser
      // repeat + vertex-color mottling is the two-scale anti-tiling mix.
      grass.wrapS = THREE.MirroredRepeatWrapping
      grass.wrapT = THREE.MirroredRepeatWrapping
      grass.repeat.set(16, 16)
      grass.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy())
      tex.grass = grass
      setAssetsTick((t) => t + 1)
    })

    // The photo is BRIGHT for a night shot (full moon) and the operator
    // wants the ground as alive as the pano — a moonlit hemisphere (cool
    // sky over green ground bounce) plus a near-neutral key. Round 10's
    // low ambient rendered our meadow black against the lit photo.
    scene.add(new THREE.HemisphereLight(0x9db8d9, 0x36503a, 1.0))
    const keyLight = new THREE.DirectionalLight(0xdde6f2, 0.9)
    keyLight.position.set(-60, 90, -40)
    scene.add(keyLight)

    // The living environment (terrain + lake) builds in its own effect —
    // this group is its mount point.
    const envGroup = new THREE.Group()
    scene.add(envGroup)

    const dynamic = new THREE.Group()
    scene.add(dynamic)

    const reducedMotion = typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches

    // Bloom (UnrealBloomPass) — the library-grade lift. Threshold sits high
    // so only genuinely bright things bloom; the calm dim lines stay calm.
    // Guarded: composer failure → plain rendering.
    let composer: EffectComposer | null = null
    try {
      composer = new EffectComposer(renderer)
      composer.addPass(new RenderPass(scene, camera))
      composer.addPass(new UnrealBloomPass(
        new THREE.Vector2(el.clientWidth, el.clientHeight), 0.5, 0.45, 0.55,
      ))
      composer.addPass(new OutputPass())
    } catch {
      composer = null
    }

    const bag: SceneBag = {
      renderer, labels, scene, camera, controls, dynamic,
      raycaster: new THREE.Raycaster(), raf: 0, disposed: false,
      camTarget: null, lookTarget: null, camDeadline: Infinity,
      userMoved: false,
      frame: 0, lastNow: 0, slowFrames: 0,
      composer, bloomOn: composer !== null,
      sparks: [],
      hitTargets: [], reducedMotion,
      stageId: null,
      stageCamPos: null, stageLook: null,
      stageCamGoal: null, stageLookGoal: null,
      par: { x: 0, y: 0, tx: 0, ty: 0, dragging: false },
      wheelOut: 0,
      zoomT: 1, zoomC: 1,
      swallowClick: false,
      snapStage: false,
      firstFocus: true,
      exitFly: null,
      mapId: null,
      mapCamPos: null,
      mapLook: null,
      mapCamGoal: null,
      mapLookGoal: null,
      mapZoomT: 1,
      mapZoomC: 1,
      mapSnap: false,
      lineRes: new THREE.Vector2(el.clientWidth, el.clientHeight),
      lineMats: [],
      edgeGlints: [],
      envGroup,
      wind: null,
      envAnimOn: true,
      envTex: [],
      tex,
    }
    bagRef.current = bag

    // The user grabbing the map cancels any programmatic camera tween
    // instantly — the tween must never fight the hand (round-3 bug) —
    // and permanently disarms the favorite auto-centering. Only fires at
    // overview: on stage the controls are disabled.
    controls.addEventListener('start', () => {
      bag.camTarget = null
      bag.lookTarget = null
      bag.userMoved = true
      bag.firstFocus = false // a grabbed map never snap-repositions later
    })

    // On stage the wheel first drives a GENTLE zoom (a factor on the
    // composed camera distance, clamped) — and only once the user is at
    // the zoomed-out limit do further zoom-out pulses accumulate toward
    // the exit to overview (with decay in the rAF). At overview
    // MapControls owns the wheel (dolly) and the rAF's distance check
    // handles level switch UP.
    const onWheel = (ev: WheelEvent) => {
      if (bag.stageId === null) {
        if (bag.mapId === null) return // free-roam: MapControls owns it
        // Whole-map paged: wheel drives the gentle map zoom; diving past
        // the in-clamp enters the front department's stage (the dive
        // check lives in the rAF where the eased value crosses it).
        ev.preventDefault()
        bag.mapZoomT = Math.min(
          1.25, Math.max(0.55, bag.mapZoomT + ev.deltaY * 0.0011),
        )
        return
      }
      ev.preventDefault()
      if (ev.deltaY < 0) {
        bag.zoomT = Math.max(STAGE_ZOOM_MIN, bag.zoomT + ev.deltaY * 0.0009)
        bag.wheelOut = 0
      } else if (bag.zoomT < STAGE_ZOOM_MAX - 0.02) {
        bag.zoomT = Math.min(STAGE_ZOOM_MAX, bag.zoomT + ev.deltaY * 0.0009)
      } else {
        bag.wheelOut += ev.deltaY
        if (bag.wheelOut > WHEEL_EXIT_ACCUM) {
          bag.wheelOut = 0
          exitStageRef.current()
        }
      }
    }
    el.addEventListener('wheel', onWheel, { passive: false })

    // Capture-phase click guard: a swipe that started on a CSS3D card
    // would still fire that card's click on release — swallow it.
    const onClickCapture = (ev: MouseEvent) => {
      if (bag.swallowClick) {
        ev.stopPropagation()
        ev.preventDefault()
        bag.swallowClick = false
      }
    }
    el.addEventListener('click', onClickCapture, true)

    const tick = (now: number) => {
      if (bag.disposed) return
      bag.frame += 1
      const t = bag.frame
      const dt = bag.lastNow ? now - bag.lastNow : 16
      bag.lastNow = now
      // Bloom quality gate: after warmup, sustained slow frames drop the
      // composer for good (cheap devices keep the plain look, no jank).
      if (bag.bloomOn && t > 90) {
        if (dt > 28) bag.slowFrames += 1
        else bag.slowFrames = Math.max(0, bag.slowFrames - 1)
        if (bag.slowFrames > 40) {
          bag.bloomOn = false
          // The wind is the other GPU luxury — freeze it alongside bloom.
          bag.envAnimOn = false
          console.info('[map] quality tier lowered — sustained slow frames')
        }
      }
      updateSparks(bag)
      if (bag.edgeGlints.length > 0 && !reducedMotion) {
        // The glass edges are near-transparent by design — the slow glint
        // sliding along each curve is what keeps them legible.
        const ds = dt * 0.001
        for (const g of bag.edgeGlints) {
          g.phase = (g.phase + ds * g.speed) % 1
          g.curve.getPointAt(g.phase, g.sprite.position)
        }
      }
      if (bag.wind && bag.envAnimOn && !reducedMotion) {
        // The wind lives: the tuft shader bends blades against this clock.
        bag.wind.value += dt * 0.001
      }
      if (bag.stageId !== null && bag.stageCamPos && bag.stageCamGoal
        && bag.stageLook && bag.stageLookGoal) {
        // STAGE: the composed pose lerps toward its goal (enter fly-in and
        // page dollies ride the same lerp) and the rubber-band parallax
        // offset rides on top. MapControls stays out of it entirely.
        bag.stageCamPos.lerp(bag.stageCamGoal, reducedMotion ? 1 : 0.07)
        bag.stageLook.lerp(bag.stageLookGoal, reducedMotion ? 1 : 0.09)
        if (!bag.par.dragging) {
          bag.par.tx *= 0.9
          bag.par.ty *= 0.9
        }
        bag.par.x += (bag.par.tx - bag.par.x) * 0.16
        bag.par.y += (bag.par.ty - bag.par.y) * 0.16
        bag.zoomC += (bag.zoomT - bag.zoomC) * (reducedMotion ? 1 : 0.12)
        TMP_FWD.subVectors(bag.stageLook, bag.stageCamPos)
        TMP_FWD.y = 0
        if (TMP_FWD.lengthSq() < 1e-6) TMP_FWD.set(0, 0, -1)
        TMP_FWD.normalize()
        TMP_RIGHT.crossVectors(TMP_FWD, WORLD_UP)
        // Gentle zoom = scaling the camera's offset from the look target.
        camera.position.copy(bag.stageLook)
          .addScaledVector(
            TMP_LOOK.copy(bag.stageCamPos).sub(bag.stageLook), bag.zoomC,
          )
          .addScaledVector(TMP_RIGHT, bag.par.x)
        camera.position.y += bag.par.y
        TMP_LOOK.copy(bag.stageLook)
          .addScaledVector(TMP_RIGHT, bag.par.x * 0.35)
        camera.lookAt(TMP_LOOK)
        bag.wheelOut *= 0.95
      } else if (bag.exitFly) {
        // STAGE-EXIT FLIGHT: one eased arc to the whole-map framing —
        // the camera is flown manually (controls disabled) so nothing
        // fights the path; lands with the controls re-armed at origin.
        const fly = bag.exitFly
        fly.t = reducedMotion ? 1 : Math.min(1, fly.t + dt / 1100)
        const e = fly.t * fly.t * (3 - 2 * fly.t)
        camera.position.copy(fly.curve.getPoint(e))
        TMP_LOOK.copy(fly.lookFrom).lerp(fly.lookTo, e)
        camera.lookAt(TMP_LOOK)
        if (fly.t >= 1) {
          if (bag.mapId !== null && bag.mapCamPos && bag.mapLook) {
            // Land into the paged whole-map machinery: the flight's end
            // IS the composed pose, so the handoff is seamless.
            bag.mapCamPos.copy(camera.position)
            bag.mapLook.copy(TMP_LOOK)
          } else {
            controls.target.copy(fly.lookTo)
            controls.enabled = true
          }
          bag.exitFly = null
        }
      } else if (bag.mapId !== null && bag.mapCamPos && bag.mapCamGoal
        && bag.mapLook && bag.mapLookGoal) {
        // WHOLE-MAP PAGED (round 16): the overview runs on the stage's
        // exact machinery — the composed pose lerps to its goal (pager
        // taps and swipes swing the ring), rubber-band parallax rides on
        // top, and the gentle zoom scales the offset from the look
        // target. Diving past the in-clamp enters the front department.
        bag.mapCamPos.lerp(bag.mapCamGoal, reducedMotion ? 1 : 0.07)
        bag.mapLook.lerp(bag.mapLookGoal, reducedMotion ? 1 : 0.09)
        if (!bag.par.dragging) {
          bag.par.tx *= 0.9
          bag.par.ty *= 0.9
        }
        bag.par.x += (bag.par.tx - bag.par.x) * 0.16
        bag.par.y += (bag.par.ty - bag.par.y) * 0.16
        bag.mapZoomC += (bag.mapZoomT - bag.mapZoomC)
          * (reducedMotion ? 1 : 0.12)
        TMP_FWD.subVectors(bag.mapLook, bag.mapCamPos)
        TMP_FWD.y = 0
        if (TMP_FWD.lengthSq() < 1e-6) TMP_FWD.set(0, 0, -1)
        TMP_FWD.normalize()
        TMP_RIGHT.crossVectors(TMP_FWD, WORLD_UP)
        camera.position.copy(bag.mapLook)
          .addScaledVector(
            TMP_LOOK.copy(bag.mapCamPos).sub(bag.mapLook), bag.mapZoomC,
          )
          .addScaledVector(TMP_RIGHT, bag.par.x)
        camera.position.y += bag.par.y
        TMP_LOOK.copy(bag.mapLook)
          .addScaledVector(TMP_RIGHT, bag.par.x * 0.35)
        camera.lookAt(TMP_LOOK)
        if (bag.mapZoomC < 0.58) {
          // Dive: enter the front department's stage. Zoom values are NOT
          // reset here — the stage seeds its fly-in from the camera's
          // current (zoomed-in) pose, and the next map enter re-inits
          // them; enterStage is idempotent for the frames until the
          // effects land.
          enterStageRef.current(bag.mapId)
        }
      } else {
        // OVERVIEW: MapControls owns the camera; programmatic tweens lerp
        // with a convergence deadline and die on user grab.
        if (bag.camTarget && bag.lookTarget) {
          camera.position.lerp(bag.camTarget, 0.06)
          controls.target.lerp(bag.lookTarget, 0.08)
          if (camera.position.distanceTo(bag.camTarget) < 0.4
            || t > bag.camDeadline) {
            bag.camTarget = null
            bag.lookTarget = null
          }
        }
        controls.update()
        // Semantic zoom UP: dollying close enough while over/near a cluster
        // enters its stage. The exit pose re-places the camera far above
        // this threshold, so the levels can't flicker.
        if (!bag.camTarget && t > 60) {
          const dist = camera.position.distanceTo(controls.target)
          if (dist < ENTER_STAGE_DIST) {
            const id = findClusterRef.current(
              controls.target.x, controls.target.z,
            )
            if (id !== null) enterStageRef.current(id)
          } else if (camera.position.y < 30) {
            // Round 13: the whole-map framing parks the controls target at
            // the (empty) ring center, so the target test above never
            // fires on a dolly-in — a LOW camera over a department's
            // territory is the real "I zoomed into it" signal. The exit
            // pose sits at y=40, safely above this trigger (hysteresis).
            const id = findClusterRef.current(
              camera.position.x, camera.position.z,
            )
            if (id !== null) enterStageRef.current(id)
          }
        }
      }
      if (bag.composer && bag.bloomOn) bag.composer.render()
      else renderer.render(scene, camera)
      labels.render(scene, camera)
      bag.raf = requestAnimationFrame(tick)
    }
    bag.raf = requestAnimationFrame(tick)

    const ro = typeof ResizeObserver === 'function'
      ? new ResizeObserver(() => {
        const w = el.clientWidth
        const h = Math.max(1, el.clientHeight)
        camera.aspect = w / h
        camera.updateProjectionMatrix()
        renderer.setSize(w, h)
        composer?.setSize(w, h)
        labels.setSize(w, h)
        bag.lineRes.set(w, h)
        for (const m of bag.lineMats) m.resolution.copy(bag.lineRes)
      })
      : null
    ro?.observe(el)

    return () => {
      bag.disposed = true
      cancelAnimationFrame(bag.raf)
      ro?.disconnect()
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('click', onClickCapture, true)
      controls.dispose()
      composer?.dispose()
      disposeObject(scene)
      for (const tx of Object.values(bag.tex)) tx?.dispose()
      for (const tx of bag.envTex) tx.dispose()
      renderer.dispose()
      // Context budget discipline — browsers cap live WebGL contexts.
      renderer.forceContextLoss?.()
      renderer.domElement.remove()
      labels.domElement.remove()
      bagRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // --- the living environment (terrain + lake) ---------------------------
  // Real 3D near-field under the photographic panorama — the game trick:
  // geometry near, photo scenery far. Rebuilt only on layout/asset
  // changes (the water carries GPU render targets), never on heat/edge
  // refetches, and deliberately NOT on stage changes (the world must not
  // morph while paging).
  useEffect(() => {
    const bag = bagRef.current
    if (!bag || bag.disposed) return
    // Settle gate (2026-08-15): agents and departments land in DIFFERENT
    // commits — building on the agents-only commit files everyone under
    // Independent and rebuilds moments later (half the load jump).
    // isPending (not isSuccess) so a FAILED departments fetch still builds,
    // and keepPrevious keeps it false across the admin scope key flip.
    if (agentsPending || deptsPending) return
    const env = bag.envGroup
    for (const child of [...env.children]) {
      env.remove(child)
      disposeObject(child)
    }
    bag.wind = null
    for (const tx of bag.envTex) tx.dispose()
    bag.envTex = []

    // Meadow terrain: gentle deterministic hills (terrainHeightAt — shared
    // with the tree/tuft placement so everything sits ON the ground),
    // flattened into a clearing around every base and sunk into a basin
    // under the lake; edges dissolve via the radial alpha fade into the
    // night. The vertex-color mottling is the second scale of the
    // anti-tiling mix: a low-frequency brightness field the texture repeat
    // can't survive.
    const clearings = layout.clusters.map((c) => ({
      x: c.cx, z: c.cz, r: c.extent + 40,
    }))
    // 1200² (round 12; was 760): the edge must sit at the pano treeline
    // from every pose. Size is free — the cost is the 128² vertex grid,
    // which stays the same; the triangles just get larger, and the tuft
    // detail stays in the central ~270 units where it's visible.
    const geo = new THREE.PlaneGeometry(1200, 1200, 128, 128)
    geo.rotateX(-Math.PI / 2)
    const pos = geo.attributes.position as THREE.BufferAttribute
    const shades = new Float32Array(pos.count * 3)
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i)
      const z = pos.getZ(i)
      pos.setY(i, terrainHeightAt(x, z, clearings))
      const m = 0.5 + 0.5 * Math.sin(
        x * 0.021 + z * 0.017 + Math.sin(x * 0.008 - z * 0.011) * 2.2,
      )
      const shade = 0.8 + 0.25 * m
      shades[i * 3] = shade
      shades[i * 3 + 1] = shade
      shades[i * 3 + 2] = shade
    }
    geo.setAttribute('color', new THREE.BufferAttribute(shades, 3))
    geo.computeVertexNormals()
    const alphaTex = makeRadialAlphaTexture()
    if (alphaTex) bag.envTex.push(alphaTex)
    const terrain = new THREE.Mesh(
      geo,
      new THREE.MeshStandardMaterial({
        map: bag.tex.grass ?? undefined,
        // Warm amber-olive tint: converts the teal night texture into the
        // pano's yellow-green moonlit meadow under the blue-leaning
        // moonlight — the color-match half of the fog-free blend
        // (brightened + greened again in round 12, operator eyeball).
        color: bag.tex.grass ? 0xd2bc6e : 0x2e3d26,
        roughness: 0.95,
        metalness: 0,
        vertexColors: true,
        transparent: true,
        alphaMap: alphaTex ?? undefined,
      }),
    )
    terrain.position.y = FLOOR_Y - 0.1
    env.add(terrain)

    const mtx = new THREE.Matrix4()
    const rotQ = new THREE.Quaternion()
    const posV = new THREE.Vector3()
    const sclV = new THREE.Vector3()

    // Wind grass: instanced crossed-quad tufts across the WHOLE visible
    // meadow (round 12 — the ground read flat where only the clearings
    // had tufts; they ride the hills via terrainHeightAt), never under a
    // slab, bent by a vertex-shader wind against the shared uTime clock.
    // The FPS gate freezes the clock (envAnimOn); reduced-motion never
    // advances it — static tufts. Detail stays inside r=270 — further out
    // a blade is subpixel anyway, which is what keeps this cheap.
    const tufts = computeGrassScatter(
      [{ x: 0, z: 0, r: 270 }],
      layout.clusters.map((c) => ({
        x: c.cx, z: c.cz, r: (c.extent + 6) * 1.25,
      })),
      6000,
    )
    const blade = tufts.length > 0 ? makeGrassBladeTexture() : null
    if (blade) {
      blade.colorSpace = THREE.SRGBColorSpace
      bag.envTex.push(blade)
      const uTime = { value: 0 }
      const tuftMat = new THREE.MeshStandardMaterial({
        map: blade,
        alphaMap: blade,
        alphaTest: 0.38,
        // A notch brighter and warmer than the ground so the tufts read
        // as ALIVE against it (round 11 — they vanished in the dark).
        color: 0x8a9a58,
        emissive: 0x161f0c,
        emissiveIntensity: 0.6,
        roughness: 0.9,
        metalness: 0,
        side: THREE.DoubleSide,
      })
      tuftMat.customProgramCacheKey = () => 'odk-grass-wind'
      tuftMat.onBeforeCompile = (shader) => {
        shader.uniforms.uTime = uTime
        shader.vertexShader = shader.vertexShader
          .replace(
            '#include <common>',
            '#include <common>\nuniform float uTime;',
          )
          .replace('#include <begin_vertex>', [
            '#include <begin_vertex>',
            '#ifdef USE_INSTANCING',
            // Bend grows with blade height; the phase comes from the
            // tuft's world position so the meadow never sways in lockstep.
            'float odkPhase = instanceMatrix[3][0] * 0.53 + instanceMatrix[3][2] * 0.37;',
            'float odkBend = pow(clamp(position.y / 1.7, 0.0, 1.0), 1.6);',
            'transformed.x += sin(uTime * 1.7 + odkPhase) * odkBend * 0.38;',
            'transformed.z += cos(uTime * 1.28 + odkPhase * 1.6) * odkBend * 0.25;',
            '#endif',
          ].join('\n'))
      }
      bag.wind = uTime
      // Bigger blades (round 12): the tuft is the thing that makes the
      // ground read 3D, so it must be visible at overview distance.
      const quadA = new THREE.PlaneGeometry(2.2, 1.7, 1, 3)
      quadA.translate(0, 0.85, 0)
      const quadB = quadA.clone()
      quadB.rotateY(Math.PI / 2)
      for (const quad of [quadA, quadB]) {
        const inst = new THREE.InstancedMesh(quad, tuftMat, tufts.length)
        tufts.forEach((tuft, i) => {
          rotQ.setFromAxisAngle(WORLD_UP, tuft.rot)
          mtx.compose(
            posV.set(
              tuft.x,
              FLOOR_Y - 0.15 + terrainHeightAt(tuft.x, tuft.z, clearings),
              tuft.z,
            ),
            rotQ,
            sclV.setScalar(tuft.scale),
          )
          inst.setMatrixAt(i, mtx)
        })
        inst.instanceMatrix.needsUpdate = true
        env.add(inst)
      }
    }

  }, [layout, assetsTick])

  // --- scene contents (rebuilt on any structural/heat/level change) -------
  useEffect(() => {
    const bag = bagRef.current
    if (!bag || bag.disposed) return
    if (agentsPending || deptsPending) return // settle gate — see env effect
    const { dynamic } = bag
    for (const child of [...dynamic.children]) {
      dynamic.remove(child)
      disposeObject(child)
    }
    bag.lineMats = []
    bag.sparks = []
    bag.hitTargets = []
    bag.edgeGlints = []
    // The teardown above just DETACHED every chip's DOM. Touch pointers
    // hold implicit capture on their pointerdown target, so a finger that
    // was down on a chip will deliver its up/cancel at the detached node —
    // invisible to every listener. Sweep those ghosts now (live gesture
    // owners excluded: their entries are refreshed by moves anyway).
    prunePointers(
      activePointers.current, performance.now(),
      (el) => !el || document.contains(el),
      gestureOwnedIds(),
    )

    const staged: string | null = stage ? stage.deptId : null
    const arc = stageData?.arc ?? null
    const slotBySlug = new Map(
      (arc?.slots ?? []).map((s) => [s.slug, s]),
    )

    // World position per agent: overview scatter, except the staged
    // cluster's members who sit in their amphitheater slots.
    const positions = new Map<string, THREE.Vector3>()
    for (const node of layout.nodes) {
      const slot = staged === node.departmentId
        ? slotBySlug.get(node.slug)
        : undefined
      positions.set(node.slug, slot
        ? new THREE.Vector3(slot.x, slot.y, slot.z)
        : new THREE.Vector3(node.x, node.y, node.z))
    }
    const memberCount = new Map<string, number>()
    for (const n of layout.nodes) {
      memberCount.set(n.departmentId, (memberCount.get(n.departmentId) ?? 0) + 1)
    }

    // Walnut bases: every cluster stands on its OWN square slab sunk into
    // the meadow (the staged slab grows under the amphitheater and its
    // frame brightens: the base literally becomes the stage floor). The
    // slab bodies are the raycast targets for department taps.
    // Overview slabs must never overlap (round 12 — big departments did):
    // the square's circumscribed circle is capped by the ring-slot
    // spacing. The staged slab still grows freely under its amphitheater;
    // its neighbors are dimmed then, so the cap only binds at overview.
    const slotChord = layout.clusters.length > 1
      ? 2 * RING_RADIUS * Math.sin(Math.PI / layout.clusters.length)
      : Infinity
    const daisCap = (slotChord / 2 - 2) / Math.SQRT2
    const daisRadius = new Map<string, number>()
    for (const cluster of layout.clusters) {
      const isStaged = staged === cluster.departmentId
      let reach = cluster.extent
      if (isStaged && arc) {
        reach = 8
        for (const s of arc.slots) {
          reach = Math.max(
            reach, Math.hypot(s.x - cluster.cx, s.z - cluster.cz),
          )
        }
      }
      daisRadius.set(
        cluster.departmentId,
        isStaged ? reach + 6 : Math.min(reach + 6, daisCap),
      )
    }
    for (const cluster of layout.clusters) {
      const isStaged = staged === cluster.departmentId
      const dimOther = staged !== null && !isStaged
      const isIndependent = cluster.departmentId === ''
      const r = daisRadius.get(cluster.departmentId)!
      const accent = new THREE.Color(
        isIndependent ? '#aab6cc' : cluster.accent,
      )
      const size = r * 2
      const dais = new THREE.Group()
      // Premium SQUARE walnut slab (operator round 9): the wood shows in
      // its natural color — the department identity lives in the glowing
      // frame inset on the top face. Rounded corners, real thickness.
      const slab = new THREE.Mesh(
        new RoundedBoxGeometry(size, 1.3, size, 4, 0.16),
        new THREE.MeshStandardMaterial({
          // Still loading ⇒ the flat fallback tint; applyWoodTexture hands
          // this material the map the moment the JPEG lands. The key is
          // omitted rather than set to undefined: three warns per material
          // on an undefined parameter, once per department per rebuild.
          ...(bag.tex.wood ? { map: bag.tex.wood } : {}),
          color: bag.tex.wood ? 0xffffff : WOOD_FALLBACK_TINT,
          metalness: 0.06,
          roughness: 0.52,
          envMapIntensity: 0.8,
          transparent: true,
          opacity: dimOther ? 0.25 : 1,
        }),
      )
      // Sunk slightly into the meadow — the base belongs to the ground.
      slab.position.y = FLOOR_Y + 0.55
      slab.userData = {
        type: 'dept',
        departmentId: cluster.departmentId,
        departmentName: cluster.name || 'Independent',
      }
      dais.add(slab)
      bag.hitTargets.push(slab)
      // The glowing accent frame: four thin light bars inset on the top.
      // At night the frame IS the department's light — it burns brighter
      // than the daylight rounds and spills onto the grass below.
      const frameHalf = size / 2 - 1
      const frameMat = new THREE.MeshBasicMaterial({
        color: accent,
        transparent: true,
        depthWrite: false,
        opacity: dimOther ? 0.12 : isStaged ? 1 : 0.9,
      })
      const frameLen = frameHalf * 2 + 0.18
      for (const [bx, bz, horizontal] of [
        [0, -frameHalf, true], [0, frameHalf, true],
        [-frameHalf, 0, false], [frameHalf, 0, false],
      ] as const) {
        const bar = new THREE.Mesh(
          new THREE.BoxGeometry(
            horizontal ? frameLen : 0.18, 0.07,
            horizontal ? 0.18 : frameLen,
          ),
          frameMat,
        )
        bar.position.set(bx, FLOOR_Y + 1.23, bz)
        dais.add(bar)
      }
      // Brass jewellery (round 10): a thin metallic inlay line just inside
      // the light frame, corner caps where the frame bars meet, and a
      // lighter beveled highlight strip along the outer top rim — the
      // moonlight and the env map do the rest.
      const brassMat = new THREE.MeshStandardMaterial({
        color: 0xd9b36a,
        metalness: 0.9,
        roughness: 0.3,
        envMapIntensity: 1.2,
        transparent: true,
        opacity: dimOther ? 0.2 : 1,
      })
      const inlayHalf = frameHalf - 0.55
      const inlayLen = inlayHalf * 2 + 0.12
      for (const [bx, bz, horizontal] of [
        [0, -inlayHalf, true], [0, inlayHalf, true],
        [-inlayHalf, 0, false], [inlayHalf, 0, false],
      ] as const) {
        const line = new THREE.Mesh(
          new THREE.BoxGeometry(
            horizontal ? inlayLen : 0.12, 0.05,
            horizontal ? 0.12 : inlayLen,
          ),
          brassMat,
        )
        line.position.set(bx, FLOOR_Y + 1.22, bz)
        dais.add(line)
      }
      for (const [cxr, czr] of [
        [-frameHalf, -frameHalf], [frameHalf, -frameHalf],
        [-frameHalf, frameHalf], [frameHalf, frameHalf],
      ] as const) {
        const cap = new THREE.Mesh(
          new THREE.BoxGeometry(0.6, 0.09, 0.6), brassMat,
        )
        cap.position.set(cxr, FLOOR_Y + 1.24, czr)
        dais.add(cap)
      }
      const bevelMat = new THREE.MeshStandardMaterial({
        color: 0x9a7a52,
        metalness: 0.25,
        roughness: 0.5,
        envMapIntensity: 0.9,
        transparent: true,
        opacity: dimOther ? 0.18 : 0.9,
      })
      const bevelHalf = size / 2 - 0.24
      const bevelLen = bevelHalf * 2 + 0.3
      for (const [bx, bz, horizontal] of [
        [0, -bevelHalf, true], [0, bevelHalf, true],
        [-bevelHalf, 0, false], [bevelHalf, 0, false],
      ] as const) {
        const strip = new THREE.Mesh(
          new THREE.BoxGeometry(
            horizontal ? bevelLen : 0.3, 0.06,
            horizontal ? 0.3 : bevelLen,
          ),
          bevelMat,
        )
        strip.position.set(bx, FLOOR_Y + 1.21, bz)
        dais.add(strip)
      }
      if (bag.tex.glow) {
        // Soft contact shadow on the grass — the base sits IN the world,
        // it doesn't float over it.
        const shadow = new THREE.Mesh(
          new THREE.CircleGeometry(size * 0.78, 48),
          new THREE.MeshBasicMaterial({
            map: bag.tex.glow,
            color: 0x0a0f0a,
            transparent: true,
            depthWrite: false,
            opacity: dimOther ? 0.12 : 0.3,
          }),
        )
        shadow.rotation.x = -Math.PI / 2
        shadow.position.set(0, FLOOR_Y + 0.04, 0)
        dais.add(shadow)
        // The base's light ON THE GROUND at every zoom level: one soft
        // warm additive pool under each slab, neutral warm like the
        // bollards, never the dept color. TIGHT (round 13) — the round-12
        // size read as an unfocused glow wash at overview; the focused
        // light is the spot's job.
        const pool = new THREE.Mesh(
          new THREE.PlaneGeometry(size * 1.3, size * 1.3),
          new THREE.MeshBasicMaterial({
            map: bag.tex.glow,
            color: 0xffd9a0,
            transparent: true,
            depthWrite: false,
            blending: THREE.AdditiveBlending,
            opacity: dimOther ? 0.04 : isStaged ? 0.15 : 0.09,
          }),
        )
        pool.rotation.x = -Math.PI / 2
        pool.position.set(0, FLOOR_Y + 0.06, 0)
        dais.add(pool)
      }
      // Corner bollard lamps (round 11 — the operator's "furniture"): a
      // small warm lantern on each corner of the slab, always burning at
      // night. Warm and NEUTRAL — the dept-color spill pools of round 10
      // read as stains and were cut.
      const postMat = new THREE.MeshStandardMaterial({
        color: 0x2a2622, metalness: 0.6, roughness: 0.5,
        transparent: true, opacity: dimOther ? 0.2 : 1,
      })
      const bulbMat = new THREE.MeshBasicMaterial({
        color: 0xffe3b0, transparent: true,
        opacity: dimOther ? 0.15 : 1,
      })
      for (const [bx, bz] of [
        [-frameHalf, -frameHalf], [frameHalf, -frameHalf],
        [-frameHalf, frameHalf], [frameHalf, frameHalf],
      ] as const) {
        const post = new THREE.Mesh(
          new THREE.CylinderGeometry(0.07, 0.1, 1.5, 8), postMat,
        )
        post.position.set(bx, FLOOR_Y + 1.24 + 0.75, bz)
        dais.add(post)
        const bulb = new THREE.Mesh(
          new THREE.SphereGeometry(0.24, 12, 10), bulbMat,
        )
        bulb.position.set(bx, FLOOR_Y + 1.24 + 1.62, bz)
        dais.add(bulb)
        if (bag.tex.glow) {
          const halo = new THREE.Sprite(new THREE.SpriteMaterial({
            map: bag.tex.glow, color: 0xffd9a0,
            transparent: true, depthWrite: false,
            blending: THREE.AdditiveBlending,
            opacity: dimOther ? 0.06 : 0.5,
          }))
          halo.scale.setScalar(2.6)
          halo.position.set(bx, FLOOR_Y + 1.24 + 1.62, bz)
          dais.add(halo)
        }
      }
      // EVERY department's light burns all night (round 14 — the operator
      // kept the look and wanted it everywhere): one warm SpotLight per
      // dais, always on. The light COUNT only changes with the department
      // list, so shader recompiles stay rare; the staged one brightens
      // and the others soften while a stage is up. Local coordinates —
      // the group's rotation carries the inward tilt.
      const spot = new THREE.SpotLight(
        0xfff1d6, dimOther ? 25 : isStaged ? 130 : 100, 170,
        Math.min(1.05, Math.atan((r * 1.5) / 52)), 0.7, 1,
      )
      spot.position.set(0, FLOOR_Y + 52, -10)
      spot.target.position.set(0, FLOOR_Y, r * 0.2)
      dais.add(spot)
      dais.add(spot.target)
      dais.position.set(cluster.cx, 0, cluster.cz)
      // Face the slab's edges toward the composed stage camera.
      dais.rotation.y = Math.atan2(cluster.outX, cluster.outZ)
      dynamic.add(dais)
    }

    // Agents: ONE glass language at both levels (operator round 7) — the
    // same panel anatomy, compact one-liner at overview, two-line ChartHop
    // card on stage; other clusters dim while a stage is up. No colored
    // box-shadows anywhere: activity lives in the border breathe + the
    // chip pulse (+ stage spark particles).
    for (const node of layout.nodes) {
      const pos = positions.get(node.slug)!
      const onStage = staged === node.departmentId
      const dimOther = staged !== null && !onStage
      const h = heat.get(node.slug) ?? 0
      const live = liveSet.has(node.slug)
      const streaming = streamingSet.has(node.slug)
      const [cr, cg, cb] = hexToRgb(node.color || DEFAULT_TINT)
      // Live-but-idle sessions raise the glow floor — activity is the only
      // extra language the cards speak.
      const glow = live ? Math.max(h, 0.45) : h
      const isFav = user?.default_agent === node.slug

      const root = document.createElement('div')
      root.style.pointerEvents = 'none'

      const chip = document.createElement('div')
      const chipSize = onStage ? 44 : 26
      chip.textContent = node.displayName.trim().slice(0, 2).toUpperCase()
      chip.style.cssText =
        `width:${chipSize}px;height:${chipSize}px;flex-shrink:0;` +
        `border-radius:${onStage ? 12 : 8}px;display:flex;align-items:center;` +
        'justify-content:center;font-family:Comfortaa,system-ui,sans-serif;' +
        `font-size:${onStage ? 17 : 11}px;font-weight:700;` +
        (node.grayed
          ? 'background:rgba(122,130,150,0.2);border:1px solid rgba(122,130,150,0.45);color:#8a92a8;'
          : `background:rgba(${cr},${cg},${cb},0.3);` +
            `border:1px solid rgba(${cr},${cg},${cb},0.6);` +
            `color:rgb(${Math.min(255, cr + 90)},${Math.min(255, cg + 90)},${Math.min(255, cb + 90)});`)
      if (!node.grayed) {
        chip.style.setProperty('--odk-glow-lo', `rgba(${cr},${cg},${cb},0.28)`)
        chip.style.setProperty('--odk-glow-hi', `rgba(${cr},${cg},${cb},0.6)`)
        if (streaming && !dimOther) chip.classList.add('odk-chip-live')
      }

      const card = document.createElement('div')
      card.className = 'odk-fade'
      const borderAlpha = onStage ? 0.45 + glow * 0.45 : 0.4 + glow * 0.35
      const borderColor = node.grayed
        ? 'rgba(122,130,150,0.4)'
        : `rgba(${cr},${cg},${cb},${borderAlpha.toFixed(2)})`
      card.style.cssText =
        'position:relative;display:flex;align-items:center;' +
        (onStage
          ? 'gap:11px;padding:10px 6px 10px 13px;border-radius:16px;'
          : 'gap:8px;padding:5px 4px 5px 7px;border-radius:13px;') +
        'touch-action:none;' +
        'background:rgba(15,18,34,0.82);' +
        'backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);' +
        `border:1.5px solid ${borderColor};` +
        'transition:border-color 140ms ease;' +
        `pointer-events:${dimOther ? 'none' : 'auto'};` +
        `opacity:${dimOther ? 0.3 : node.grayed ? 0.6 : 1};` +
        `box-shadow:0 ${onStage ? '6px 22px' : '4px 14px'} rgba(4,6,16,0.5);`
      if (!node.grayed) {
        card.style.setProperty('--odk-b-lo', `rgba(${cr},${cg},${cb},0.4)`)
        card.style.setProperty('--odk-b-hi', `rgba(${cr},${cg},${cb},1)`)
        if (streaming && !dimOther) card.classList.add('odk-border-breathe')
      }

      if (onStage && streaming && !node.grayed && !bag.reducedMotion) {
        const canvas = document.createElement('canvas')
        canvas.style.cssText =
          `position:absolute;left:${-SPARK_MARGIN}px;top:${-SPARK_MARGIN}px;`
          + 'pointer-events:none;'
        const ctx = canvas.getContext('2d')
        if (ctx) {
          root.appendChild(canvas)
          bag.sparks.push({
            canvas, ctx, pill: card, rgb: [cr, cg, cb],
            heat: Math.max(h, 0.3), particles: [], sized: false,
          })
        }
      }
      root.appendChild(card)
      card.appendChild(chip)

      const col = document.createElement('div')
      col.style.cssText = 'min-width:0;display:flex;flex-direction:column;'
      const name = document.createElement('button')
      name.textContent = (isFav ? '★ ' : '') + node.displayName
      name.title = node.grayed
        ? `${node.displayName} (not a member)` : `Open ${node.displayName}`
      name.style.cssText =
        `font-family:Comfortaa,system-ui,sans-serif;font-size:${onStage ? 21 : 18}px;` +
        'font-weight:600;letter-spacing:0.01em;background:none;border:0;' +
        `cursor:${node.grayed ? 'default' : 'pointer'};` +
        `color:${node.grayed ? '#8a92a8' : '#e6ebff'};` +
        `padding:0;text-align:left;white-space:nowrap;max-width:${onStage ? 170 : 150}px;` +
        'overflow:hidden;text-overflow:ellipsis;'
      const openFromCard = (e: MouseEvent) => {
        e.stopPropagation()
        if (moveFromRef.current) return
        const lf = linkFromRef.current
        if (lf) {
          attemptLinkRef.current(lf, node.slug)
          return
        }
        openAgentRef.current(node, e.clientX, e.clientY)
      }
      name.onclick = openFromCard
      col.appendChild(name)
      if (onStage && node.modeLabel) {
        // Second line = the agent's visibility mode (the Grid cards'
        // wording); omitted for grayed dept-mates whose mode is unknown.
        const lvl = document.createElement('div')
        lvl.textContent = node.modeLabel
        lvl.style.cssText =
          'font-size:12px;letter-spacing:0.14em;text-transform:uppercase;' +
          'color:#8fa0c8;margin-top:3px;white-space:nowrap;max-width:170px;' +
          'overflow:hidden;text-overflow:ellipsis;'
        col.appendChild(lvl)
      }
      card.appendChild(col)
      // The whole chip body opens the agent (grayed → info popup via
      // openAgent); only the ⋯ button diverges — its stopPropagation
      // keeps this from firing. Post-swipe clicks are already swallowed
      // by the container's capture-phase guard.
      card.onclick = openFromCard
      card.style.cursor = node.grayed ? 'default' : 'pointer'

      const more = document.createElement('button')
      more.textContent = '⋯'
      more.setAttribute('aria-label', `Options for ${node.displayName}`)
      more.style.cssText =
        'background:none;border:0;border-left:1px solid rgba(255,255,255,0.08);' +
        `cursor:pointer;color:#8b96b8;font-size:${onStage ? 24 : 18}px;line-height:1;` +
        (onStage
          ? 'padding:4px 10px 6px 12px;'
          : 'padding:2px 8px 3px 9px;') +
        'flex-shrink:0;align-self:stretch;'
      more.onclick = (e) => {
        e.stopPropagation()
        setPopup({ node, x: e.clientX, y: e.clientY })
      }
      card.appendChild(more)

      card.oncontextmenu = (e) => {
        e.preventDefault()
        e.stopPropagation()
        setPopup({ node, x: e.clientX, y: e.clientY })
      }
      if (!node.grayed && !dimOther) {
        card.onmouseenter = () => {
          card.style.borderColor = `rgba(${cr},${cg},${cb},0.95)`
        }
        card.onmouseleave = () => { card.style.borderColor = borderColor }
      }

      const obj = new CSS3DSprite(root)
      obj.scale.setScalar(onStage ? STAGE_SCALE : OVERVIEW_SCALE)
      obj.position.copy(pos)
      dynamic.add(obj)
    }

    // Department typography: spaced-caps name + member count floating over
    // each blob; the staged cluster's label rises above its amphitheater.
    for (const cluster of layout.clusters) {
      if (!cluster.name) continue // lone centered scatter needs no label
      const isStaged = staged === cluster.departmentId
      const dimOther = staged !== null && !isStaged
      const holder = document.createElement('div')
      holder.style.pointerEvents = isStaged || dimOther ? 'none' : 'auto'
      holder.style.textAlign = 'center'
      holder.style.cursor = 'pointer'
      holder.style.opacity = dimOther ? '0.25' : isStaged ? '0.9' : '1'
      holder.style.transition = 'opacity 200ms ease'
      // Smaller type + more altitude (operator round 7): long names fit a
      // phone width and the name never crowds the cards.
      const title = document.createElement('div')
      title.textContent = cluster.name.toUpperCase().split('').join(' ')
      title.style.cssText =
        'font-family:Comfortaa,system-ui,sans-serif;font-size:24px;' +
        'font-weight:600;letter-spacing:0.2em;color:rgba(240,244,255,0.95);' +
        // The halo earns its keep against the milky way band too — the
        // starfield is busy enough to eat unhaloed type.
        'text-shadow:0 2px 10px rgba(10,14,22,0.95),0 0 26px rgba(10,14,22,0.7);' +
        'white-space:nowrap;'
      holder.appendChild(title)
      const count = memberCount.get(cluster.departmentId) ?? 0
      const sub = document.createElement('div')
      sub.textContent = `${count} agent${count === 1 ? '' : 's'}`
      sub.style.cssText =
        'font-size:13px;letter-spacing:0.28em;text-transform:uppercase;' +
        'color:rgba(235,240,250,0.85);margin-top:6px;white-space:nowrap;' +
        'text-shadow:0 1px 8px rgba(10,14,22,0.9);'
      holder.appendChild(sub)
      holder.onclick = (e) =>
        onDeptTapRef.current(cluster.departmentId, e.clientX, e.clientY)
      const labelObj = new CSS3DSprite(holder)
      labelObj.scale.setScalar(LABEL_SCALE)
      const labelY = isStaged && arc
        ? 0.8 + Math.max(0, arc.rows - 1) * 4.2 + 9
        : 13
      labelObj.position.set(cluster.cx, labelY, cluster.cz)
      dynamic.add(labelObj)
    }

    // --- delegation edges (DIRECTIONAL — each agent owns its outgoing
    // targets). Dark-glass ribbon, round-19 cut (see EDGE_BODY above for
    // why round 17's additive silver failed on the bright meadow): every
    // edge is a slim near-black navy ribbon — the agent cards' own
    // material language, legible over walnut AND bright grass — carrying
    // one faint light-catch hugging its TOP edge (the glass sheen, and
    // the visibility over the darkest night areas), glassy dart chevrons
    // along the length for direction (alternating on a mutual pair), a
    // small gap before each card, and ONE slow glint traveling the lit
    // edge. WHOLE MAP only since round 22 (the department view's
    // partners sit behind its composed camera — see the edge block),
    // drawn PER-AGENT since round 18 (the aggregated dept bridges threw
    // away exactly the who-talks-to-whom the edges exist to show).
    // Solid curves only (dashes read as broken since round 7); WebGL
    // renders under the CSS3D card layer, so never crossing a card is
    // still the whole trick.
    // Both manual AND department-compiled edges draw (round 21 — the
    // operator's marketing head "talks to" its members through dept
    // compilation, and a map that hid those links looked like missing
    // data). Dept edges only ever join same-department agents, so they
    // all land in the subtle intra tier. Mutuality counts either source.
    const drawnSources = new Set(['manual', 'department'])
    const linkDir = new Set<string>()
    for (const e of edges) {
      if (drawnSources.has(e.source)) linkDir.add(`${e.from}|${e.to}`)
    }
    /** Deterministic [0,1) from an edge key — glint phase + speed seeds. */
    const phaseOf = (s: string) => {
      let h = 0
      for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
      return (h % 997) / 997
    }
    const addLine2 = (
      pts: THREE.Vector3[], lift: number, color: number, width: number,
      opacity: number, additive: boolean,
    ) => {
      const flat: number[] = []
      for (const p of pts) flat.push(p.x, p.y + lift, p.z)
      const geom = new LineGeometry()
      geom.setPositions(flat)
      const mat = new LineMaterial({
        color, linewidth: width,
        // WORLD-unit thickness (round 22): pixel widths made a phone
        // show the same line ~4x fatter relative to its small viewport
        // than a desktop — the operator's "white ropes". A world width
        // is a physical property of the glass rod instead: identical on
        // every screen, hair-thin in the distance, thicker as you zoom
        // in — which is also most of what "looks real" means here.
        worldUnits: true,
        transparent: true, opacity,
        blending: additive ? THREE.AdditiveBlending : THREE.NormalBlending,
        depthWrite: false,
      })
      mat.resolution.copy(bag.lineRes)
      bag.lineMats.push(mat)
      dynamic.add(new Line2(geom, mat))
    }
    /** The ribbon: dark body + a wide FAINT halo + the crisp light-catch,
     * both hugging the top edge. The sheen stays narrower AND dimmer than
     * the body — the glass is the line, the light only catches on it —
     * and the halo is what makes that light READ as light (a bare 1px
     * additive streak looked like a painted white line on phones, and
     * over dark grass the sheen alone was too thin to survive the
     * screen's downscale; the soft falloff fixes both). */
    const addGlassRibbon = (
      pts: THREE.Vector3[], bodyWidth: number, bodyOpacity: number,
      lightOpacity: number,
    ) => {
      addLine2(pts, 0, EDGE_BODY, bodyWidth, bodyOpacity, false)
      addLine2(
        pts, EDGE_LIGHT_LIFT, EDGE_LIGHT, bodyWidth * 1.8,
        lightOpacity * 0.22, true,
      )
      addLine2(
        pts, EDGE_LIGHT_LIFT, EDGE_LIGHT, Math.max(0.07, bodyWidth * 0.4),
        lightOpacity, true,
      )
    }
    /** Glassy dart chevrons repeated along the curve — a dark body cone
     * with a smaller lit cone nested above it, matching the ribbon.
     * One-way edges all point the same way; a mutual pair alternates. */
    const addChevrons = (
      curve: THREE.Curve<THREE.Vector3>,
      dir: 'ab' | 'ba' | 'both', opacity: number, scale: number,
    ) => {
      const len = curve.getLength()
      // Sparse darts (round 22) — a dense train read as a beaded rope
      // at phone scale, and the dots were half the "still too white".
      const count = Math.max(2, Math.min(4, Math.round(len / 16)))
      // Dark glass darts with a faint lit core — the operator's phone
      // showed round 18's bright cones as pure white blobs, so the lit
      // cone now stays well under the dark body's presence.
      const bodyGeom = new THREE.ConeGeometry(0.2 * scale, 0.8 * scale, 10)
      const bodyMat = new THREE.MeshBasicMaterial({
        color: EDGE_BODY, transparent: true,
        opacity: Math.min(0.9, opacity + 0.2),
        depthWrite: false,
      })
      const litGeom = new THREE.ConeGeometry(0.09 * scale, 0.5 * scale, 10)
      const litMat = new THREE.MeshBasicMaterial({
        color: EDGE_LIGHT, transparent: true, opacity: opacity * 0.45,
        blending: THREE.AdditiveBlending, depthWrite: false,
      })
      for (let k = 0; k < count; k++) {
        const t = 0.5
          + ((k - (count - 1) / 2) / Math.max(1, count - 1)) * 0.6
        const fwd = dir === 'ab' || (dir === 'both' && k % 2 === 0)
        const tan = curve.getTangentAt(t)
          .multiplyScalar(fwd ? 1 : -1).normalize()
        const body = new THREE.Mesh(bodyGeom, bodyMat)
        curve.getPointAt(t, body.position)
        body.quaternion.setFromUnitVectors(WORLD_UP, tan)
        dynamic.add(body)
        const lit = new THREE.Mesh(litGeom, litMat)
        lit.position.copy(body.position)
        lit.position.y += EDGE_LIGHT_LIFT * 0.6
        lit.quaternion.copy(body.quaternion)
        dynamic.add(lit)
      }
    }
    /** One glint of light per edge, riding the LIT edge. Phase = f(edge
     * key, wall clock), so the 15s scene rebuild resumes the motion. */
    const addGlint = (curve: THREE.Curve<THREE.Vector3>, seed: string) => {
      if (!bag.tex.glow) return
      // Lift a sampled copy instead of the control points — the edge mix
      // is quadratic AND cubic now, and the spline through 24 samples
      // reproduces either shape to well under a pixel.
      const lifted = new THREE.CatmullRomCurve3(
        curve.getPoints(24).map((p) => {
          p.y += EDGE_LIGHT_LIFT
          return p
        }),
      )
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: bag.tex.glow, color: 0xffffff,
        transparent: true, depthWrite: false,
        blending: THREE.AdditiveBlending, opacity: 0.3,
      }))
      sprite.scale.setScalar(0.7)
      const speed = 0.08 + phaseOf(`${seed}~`) * 0.05
      const phase = (phaseOf(seed) + performance.now() * 0.001 * speed) % 1
      lifted.getPointAt(phase, sprite.position)
      dynamic.add(sprite)
      bag.edgeGlints.push({ sprite, curve: lifted, phase, speed })
    }
    const edgeCurve = (
      a: THREE.Vector3, b: THREE.Vector3, liftFactor: number, liftCap: number,
    ) => {
      const mid = a.clone().lerp(b, 0.5)
      mid.y += Math.min(liftCap, a.distanceTo(b) * liftFactor)
      return new THREE.QuadraticBezierCurve3(a, mid, b)
    }
    /** Cross-platform FLIGHT path: a symmetric ARCH — vertical rise
     * straight above each agent up to a shared cruise height, one bow
     * across, vertical drop onto the other agent. Two hard lessons live
     * here (rounds 19-21): (1) deck agents sit BELOW the opaque
     * grass-tuft tops (~FLOOR_Y + 2.1) and below their own platform's
     * far-lip sightline, so any sloped approach spends its last stretch
     * occluded — the lines visibly "stopped" at the wood; a vertical
     * drop happens INSIDE the platform footprint, where nothing can be
     * in front of it. (2) The cruise height must be relative to the
     * HIGHER endpoint — a stage card sits ~8 units above a remote deck,
     * and a per-endpoint climb sent the whole crossing below the stage's
     * wood horizon (the department view's "no lines at all"). */
    const flightCurve = (a: THREE.Vector3, b: THREE.Vector3) => {
      const span = a.distanceTo(b)
      const cruise = Math.max(a.y, b.y)
        + Math.min(8, Math.max(4, 3.5 + span * 0.04))
      return new THREE.CubicBezierCurve3(
        a,
        new THREE.Vector3(a.x, cruise, a.z),
        new THREE.Vector3(b.x, cruise, b.z),
        b,
      )
    }
    /** Pull an endpoint in toward the other end so the line starts/stops
     * at the panel's visual edge instead of underneath it. */
    const trimToEdge = (
      from: THREE.Vector3, toward: THREE.Vector3, inset: number,
    ) => {
      const dir = TMP_FWD.copy(toward).sub(from)
      dir.y = 0
      const len = dir.length()
      if (len < 0.001) return from.clone()
      dir.multiplyScalar(1 / len)
      return from.clone().addScaledVector(
        new THREE.Vector3(dir.x, 0, dir.z), Math.min(inset, len * 0.35),
      )
    }

    if (staged === null) {
      // Edges draw on the WHOLE MAP ONLY (round 22, operator call). In
      // the department view every cross-department partner sits BEHIND
      // the composed stage camera — the ring is at the viewer's back —
      // so an edge there can never show who talks to whom; hiding them
      // beats drawing unreadable stubs. The whole map is the delegation
      // picture, and draws PER-AGENT edges (round 18 — the aggregated
      // dept bridges lost exactly that).
      const drawnPairs = new Set<string>()
      for (const e of edges) {
        if (!drawnSources.has(e.source)) continue
        const key = [e.from, e.to].sort().join('|')
        if (drawnPairs.has(key)) continue
        drawnPairs.add(key)
        const pa = positions.get(e.from)
        const pb = positions.get(e.to)
        if (!pa || !pb) continue
        const mutual = linkDir.has(`${e.from}|${e.to}`)
          && linkDir.has(`${e.to}|${e.from}`)
        const intra = nodeBySlug.get(e.from)?.departmentId
          === nodeBySlug.get(e.to)?.departmentId
        // Edge shape depends on what it crosses. Same platform: short
        // edges HOP just above the boards, longer ones arc low over
        // their own wood, both trimmed short of the pills. DIFFERENT
        // platforms: flightCurve arches between HOVER PORTS ~pill-top
        // height (round 22) — a deck-level landing spent its last
        // couple of units below the far-lip/grass-top sightline of a
        // low camera and visibly vanished into the wood; the port keeps
        // the whole drop above that horizon and the pill bridges the
        // final bit to its agent.
        const a = pa.clone()
        const b = pb.clone()
        if (intra) {
          a.y += 0.6
          b.y += 0.6
          const span = a.distanceTo(b)
          const aEnd = trimToEdge(a, b, 3.0)
          const bEnd = trimToEdge(b, a, 3.0)
          const curve = span < 12
            ? edgeCurve(aEnd, bEnd, 0.3, 2.4)
            : edgeCurve(aEnd, bEnd, 0.11, 5)
          addGlassRibbon(curve.getPoints(40),
            0.18 + (mutual ? 0.03 : 0), 0.66, 0.26)
          addChevrons(curve, mutual ? 'both' : 'ab', 0.5, 0.85)
          addGlint(curve, key)
        } else {
          a.y += 2.6
          b.y += 2.6
          const curve = flightCurve(a, b)
          addGlassRibbon(curve.getPoints(40),
            0.22 + (mutual ? 0.03 : 0), 0.78, 0.34)
          addChevrons(curve, mutual ? 'both' : 'ab', 0.62, 1)
          addGlint(curve, key)
        }
      }
    }
  }, [layout, heat, liveSet, streamingSet, edges, stage, stageData,
    user?.default_agent, nodeBySlug])

  // --- the semantic-zoom state machine ------------------------------------
  const enterStage = useCallback((deptId: string) => {
    setPopup(null)
    setMenu(null)
    setStage((cur) => (cur?.deptId === deptId ? cur : { deptId }))
  }, [])
  const enterStageRef = useRef(enterStage)
  useEffect(() => { enterStageRef.current = enterStage }, [enterStage])

  /** Zoom-in target test: the cluster whose blob the camera is over. */
  const findCluster = useCallback((x: number, z: number): string | null => {
    let best: string | null = null
    let bestD = Infinity
    for (const c of layout.clusters) {
      const d = Math.hypot(x - c.cx, z - c.cz)
      if (d < c.extent + 16 && d < bestD) {
        bestD = d
        best = c.departmentId
      }
    }
    return best
  }, [layout])
  const findClusterRef = useRef(findCluster)
  useEffect(() => { findClusterRef.current = findCluster }, [findCluster])

  const exitStage = useCallback(() => {
    const bag = bagRef.current
    if (!bag || bag.stageId === null) {
      setStage(null)
      return
    }
    const cluster = layout.clusters.find(
      (c) => c.departmentId === bag.stageId,
    )
    setStage(null)
    if (!cluster) {
      bag.controls.enabled = true
      return
    }
    // Round 15: ONE continuous flight to the whole-map framing. The pose
    // sits BEHIND the exited department looking across the ring (your
    // dais foreground, the company behind it) at the LOWEST allowed
    // angle — ~8°, just inside the polar clamp: the operator wants the
    // whole map read from near ground level. Distance comes from the
    // horizontal FOV (portrait phones pull back to the clamp; desktop a
    // little further back than round 14), capped so the 3D distance
    // stays inside the controls' 150 zoom-out clamp. The PATH arcs
    // around the ring at altitude — round 14's straight lerp from a
    // desktop stage pose cut through the empty center and read as a
    // broken two-step zoom.
    const vfov = (bag.camera.fov * Math.PI) / 180
    const hfov = 2 * Math.atan(
      Math.tan(vfov / 2) * Math.max(0.4, bag.camera.aspect),
    )
    const dist = Math.min(138, Math.max(112, 88 / Math.tan(hfov / 2)))
    const end = new THREE.Vector3(
      cluster.outX * dist, dist * 0.1405, cluster.outZ * dist,
    )
    const start = bag.camera.position.clone()
    const mid = start.clone().lerp(end, 0.5)
    const rHoriz = Math.max(
      Math.hypot(start.x, start.z), Math.hypot(end.x, end.z), 60,
    )
    const midHoriz = Math.hypot(mid.x, mid.z)
    if (midHoriz > 0.001) {
      mid.x *= rHoriz / midHoriz
      mid.z *= rHoriz / midHoriz
    } else {
      mid.x = end.x
      mid.z = end.z
    }
    mid.y = Math.max(start.y, end.y) + 14
    bag.camTarget = null
    bag.lookTarget = null
    bag.controls.enabled = false
    bag.exitFly = {
      curve: new THREE.QuadraticBezierCurve3(start, mid, end),
      lookFrom: (bag.stageLook ?? bag.controls.target).clone(),
      lookTo: new THREE.Vector3(0, 0, 0),
      t: 0,
    }
    // The flight lands into the paged whole-map machinery, standing
    // behind the exited department.
    setMapDept(cluster.departmentId)
  }, [layout])
  const exitStageRef = useRef(exitStage)
  useEffect(() => { exitStageRef.current = exitStage }, [exitStage])

  const pageDelta = useCallback((dir: 1 | -1) => {
    const bag = bagRef.current
    if (!bag) return
    const order = layout.clusters.map((c) => c.departmentId)
    if (order.length < 2) return
    // Both paged levels page the same ring: the stage dollies around it,
    // the whole-map view swings behind the next department (round 16).
    const cur = bag.stageId ?? bag.mapId
    if (cur === null) return
    const i = order.indexOf(cur)
    if (i < 0) return
    const next = order[(i + dir + order.length) % order.length]
    if (bag.stageId !== null) setStage({ deptId: next })
    else setMapDept(next)
  }, [layout])
  const pageDeltaRef = useRef(pageDelta)
  useEffect(() => { pageDeltaRef.current = pageDelta }, [pageDelta])

  // Desktop arrow keys (round 17): ←/→ page the ring on BOTH paged levels
  // (the same ring the pager strip and swipes drive — stage dollies, the
  // whole map swings), ↑ dives into the front department's stage, ↓ exits
  // back to the whole map. Ignored while an editable element has focus so
  // the map never steals keys from a form.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return
      const el = document.activeElement
      if (
        el instanceof HTMLElement
        && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
          || el.isContentEditable)
      ) return
      const bag = bagRef.current
      if (!bag || bag.disposed) return
      switch (e.key) {
        case 'ArrowLeft':
          pageDeltaRef.current(-1)
          break
        case 'ArrowRight':
          pageDeltaRef.current(1)
          break
        case 'ArrowUp':
          if (bag.stageId !== null || bag.mapId === null) return
          setStage({ deptId: bag.mapId })
          break
        case 'ArrowDown':
          if (bag.stageId === null) return
          exitStageRef.current()
          break
        default:
          return
      }
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Stage pose: compute the composed Vision-Pro framing whenever the staged
  // cluster (or its members) change — entering initializes the lerp from
  // the camera's current pose so the fly-in and page dollies are seamless.
  useEffect(() => {
    const bag = bagRef.current
    if (!bag || bag.disposed) return
    if (!stage) {
      bag.stageId = null
      bag.stageCamGoal = null
      bag.stageLookGoal = null
      bag.par = { x: 0, y: 0, tx: 0, ty: 0, dragging: false }
      bag.zoomT = 1
      bag.zoomC = 1
      // An in-flight stage exit owns the camera until it lands, and the
      // paged whole-map machinery owns it after that — free-roam
      // MapControls only when neither is active.
      bag.controls.enabled = bag.exitFly === null && bag.mapId === null
      return
    }
    if (!stageData) {
      // The staged cluster vanished (dept deleted / filtered away).
      setStage(null)
      return
    }
    const { cluster, arc } = stageData
    const entering = bag.stageId === null
    const paged = !entering && bag.stageId !== stage.deptId
    bag.stageId = stage.deptId
    bag.controls.enabled = false
    bag.camTarget = null
    bag.lookTarget = null
    bag.exitFly = null
    bag.wheelOut = 0
    if (entering || paged) bag.zoomT = 1
    if (entering || !bag.stageCamPos || !bag.stageLook) {
      bag.stageCamPos = bag.camera.position.clone()
      bag.stageLook = bag.controls.target.clone()
    }
    // Frame the widest amphitheater row inside the horizontal FOV (the
    // row cap already adapted the seat count to this screen, so the far
    // clamp is only a safety net).
    const vfov = (bag.camera.fov * Math.PI) / 180
    const hfov = 2 * Math.atan(
      Math.tan(vfov / 2) * Math.max(0.4, bag.camera.aspect),
    )
    // viewScale: big screens sit farther back so the amphitheater reads
    // at a laptop-like physical size instead of filling a 32" panel.
    const vs = viewScale()
    const camDist = Math.min(STAGE_CAM_MAX * vs, Math.max(
      STAGE_CAM_MIN, ((arc.halfWidth + 5) / Math.tan(hfov / 2)) * 1.04 * vs,
    ))
    // Higher camera + lower gaze than round 7: the added pitch spreads the
    // amphitheater rows apart on screen instead of stacking them.
    const camY = 12 + arc.rows * 2
    bag.stageCamGoal = new THREE.Vector3(
      cluster.cx - cluster.outX * camDist,
      camY,
      cluster.cz - cluster.outZ * camDist,
    )
    bag.stageLookGoal = new THREE.Vector3(
      cluster.cx + cluster.outX * (arc.depth * 0.35),
      1.5 + arc.rows * 0.6,
      cluster.cz + cluster.outZ * (arc.depth * 0.35),
    )
    if (bag.snapStage || (entering && bag.firstFocus)) {
      // First layout opens ON the stage: no fly-in, the pose IS the start.
      // firstFocus is the render-phase decision's snap signal (bag can't be
      // touched during render); a later user-initiated stage entry from
      // overview still flies (firstFocus cleared below / on grab).
      bag.snapStage = false
      bag.firstFocus = false
      bag.stageCamPos!.copy(bag.stageCamGoal)
      bag.stageLook!.copy(bag.stageLookGoal)
    }
    try {
      // Remembered across sessions: the next open parks the camera here
      // while the data loads (see the renderer-setup note). Written ONLY
      // when this stage IS the favorite's department — the parked pose is
      // by construction the favorite's own stage, so the next open can
      // never park rotated to the last-visited department.
      if (user?.default_agent && stage.deptId === favDeptRef.current) {
        localStorage.setItem('odk-map-pose-v2', JSON.stringify({
          c: bag.stageCamGoal.toArray(),
          t: bag.stageLookGoal.toArray(),
          d: stage.deptId,
          f: user.default_agent,
        }))
      }
    } catch { /* storage blocked — cosmetic only */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, stageData])

  // Whole-map paged pose (round 16): compose the behind-department framing
  // whenever the paged overview is active — paging lerps between poses,
  // exactly like stage paging. A vanished department (deleted / filtered)
  // falls back to free-roam.
  useEffect(() => {
    const bag = bagRef.current
    if (!bag || bag.disposed) return
    // '' is the INDEPENDENT cluster — a real page. Only null means "no
    // paged department" (the round-16 truthiness check made paging to
    // Independent silently drop to free-roam).
    if (stage || mapDept === null) {
      bag.mapId = null
      bag.mapCamGoal = null
      bag.mapLookGoal = null
      if (!stage && !bag.exitFly) bag.controls.enabled = true
      return
    }
    const cluster = layout.clusters.find((c) => c.departmentId === mapDept)
    if (!cluster) {
      setMapDept(null)
      return
    }
    const entering = bag.mapId === null
    const paged = !entering && bag.mapId !== mapDept
    bag.mapId = mapDept
    bag.controls.enabled = false
    bag.camTarget = null
    bag.lookTarget = null
    if (entering || paged) {
      bag.mapZoomT = 1
      bag.mapZoomC = 1
      bag.par = { x: 0, y: 0, tx: 0, ty: 0, dragging: false }
    }
    if (entering || !bag.mapCamPos || !bag.mapLook) {
      bag.mapCamPos = bag.camera.position.clone()
      bag.mapLook = bag.controls.target.clone()
    }
    const pose = mapPoseFor(bag.camera, cluster)
    bag.mapCamGoal = pose.cam
    bag.mapLookGoal = pose.look
    if (bag.mapSnap || (entering && bag.firstFocus)) {
      // firstFocus mirrors the stage effect: the render-phase no-favorite
      // decision must place the whole-map pose instantly too.
      bag.mapSnap = false
      bag.firstFocus = false
      bag.mapCamPos.copy(pose.cam)
      bag.mapLook.copy(pose.look)
    }
  }, [stage, mapDept, layout])

  // Esc backs out one level (keyboard affordance, not a button).
  useEffect(() => {
    if (!stage) return
    return pushEscHandler(() => exitStageRef.current())
  }, [stage])

  // Phones: the pager strip is a center-locked scroller — the active
  // department scrolls itself to the middle on every page change (the
  // strip can hold any number of departments without clipping).
  const pagerRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!stage && mapDept === null) return
    const active = pagerRef.current?.querySelector('[data-active="true"]')
    active?.scrollIntoView?.({
      behavior: 'smooth', inline: 'center', block: 'nearest',
    })
  }, [stage, mapDept])

  // --- pointer interaction ------------------------------------------------
  const downPos = useRef<{ x: number; y: number } | null>(null)
  const longPress = useRef<number | null>(null)
  const swipe = useRef<{
    id: number
    x0: number
    y0: number
    lastX: number
    lastT: number
    vx: number
  } | null>(null)
  /** The pinch OWNS its two pointer ids — distances are measured between
   * exactly those fingers, never "the first two Map entries" (a ghost
   * entry from a cancelled pointer would poison every later pinch). */
  const pinch = useRef<{
    a: number
    b: number
    d0: number
    zoom0: number
    lastT: number
  } | null>(null)
  const activePointers = useRef<PointerMap>(new Map())
  // Pointers a LIVE gesture owns — excluded from ghost prunes (a pinch or
  // slow-drag finger legitimately holds still past the stale window).
  const gestureOwnedIds = () => {
    const ids = new Set<number>()
    if (pinch.current) { ids.add(pinch.current.a); ids.add(pinch.current.b) }
    if (swipe.current) ids.add(swipe.current.id)
    return ids
  }

  const pick = useCallback((clientX: number, clientY: number) => {
    const bag = bagRef.current
    const el = containerRef.current
    if (!bag || !el) return null
    const rect = el.getBoundingClientRect()
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    )
    bag.raycaster.setFromCamera(ndc, bag.camera)
    const hits = bag.raycaster.intersectObjects(bag.hitTargets, false)
    for (const hit of hits) {
      const data = hit.object.userData
      if (data?.type === 'dept') return data as {
        type: 'dept'
        departmentId: string
        departmentName: string
      }
    }
    return null
  }, [])

  /** DIRECTIONAL link (matches the config semantics: each agent owns its
   * OUTGOING targets): merge `to` into `from`'s target list only. Linking
   * back is done from the other agent. */
  const attemptLink = useCallback(async (from: string, to: string) => {
    setLinkFrom(null)
    if (from === to) return
    const fromName = nodeBySlug.get(from)?.displayName ?? from
    const toName = nodeBySlug.get(to)?.displayName ?? to
    try {
      const res = await apiFetch(`/v1/agents/${from}/delegation-targets`)
      if (!res.ok) throw new Error(`No access to ${from}'s delegation config`)
      const data = await res.json()
      const targets: string[] = data.targets ?? []
      if (!targets.includes(to)) {
        const put = await apiFetch(`/v1/agents/${from}/delegation-targets`, {
          method: 'PUT',
          body: JSON.stringify({ targets: [...targets, to] }),
        })
        if (!put.ok) {
          const e = await put.json().catch(() => ({}))
          throw new Error(e.detail || `Failed to link ${from} → ${to}`)
        }
      }
      setNotice(`${fromName} can now delegate to ${toName}`)
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'Link failed')
    } finally {
      qc.invalidateQueries({ queryKey: ['delegation-edges-all'] })
      qc.invalidateQueries({ queryKey: ['delegation-targets'] })
    }
  }, [qc, nodeBySlug])
  const attemptLinkRef = useRef(attemptLink)
  useEffect(() => { attemptLinkRef.current = attemptLink }, [attemptLink])

  /** Sever the link in BOTH directions — deliberate convenience: the config
   * page removes only that agent's outgoing side; "Unlink" on the map means
   * "these two stop delegating to each other". */
  const attemptUnlink = useCallback(async (a: string, b: string) => {
    try {
      const removeDir = async (agent: string, target: string) => {
        const res = await apiFetch(`/v1/agents/${agent}/delegation-targets`)
        if (!res.ok) throw new Error(`No access to ${agent}'s delegation config`)
        const data = await res.json()
        const targets: string[] = data.targets ?? []
        if (!targets.includes(target)) return
        const put = await apiFetch(`/v1/agents/${agent}/delegation-targets`, {
          method: 'PUT',
          body: JSON.stringify({ targets: targets.filter((t) => t !== target) }),
        })
        if (!put.ok) {
          const e = await put.json().catch(() => ({}))
          throw new Error(e.detail || `Failed to unlink ${agent} → ${target}`)
        }
      }
      await removeDir(a, b)
      await removeDir(b, a)
      setNotice(`Unlinked ${a} ↔ ${b}`)
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'Unlink failed')
    } finally {
      qc.invalidateQueries({ queryKey: ['delegation-edges-all'] })
      qc.invalidateQueries({ queryKey: ['delegation-targets'] })
    }
  }, [qc])

  const completeMove = useCallback((
    slug: string, deptId: string, levelId: string,
    deptName: string, levelName: string,
  ) => {
    setMoveFrom(null)
    setLevelPick(null)
    const display = nodeBySlug.get(slug)?.displayName ?? slug
    updateAgent.mutate(
      { name: slug, department_id: deptId, department_level_id: levelId },
      {
        onSuccess: () => {
          setNotice(`Moved ${display} to ${deptName} · ${levelName}`)
          // A department move recompiles edges server-side.
          qc.invalidateQueries({ queryKey: ['departments'] })
          qc.invalidateQueries({ queryKey: ['delegation-edges-all'] })
        },
        onError: (e) => setNotice(e.message),
      },
    )
  }, [updateAgent, qc, nodeBySlug])

  /** Move-mode department tap: the Independent blob/page clears the
   * assignment, single-level depts assign directly, multi-level ones open
   * a level picker (same MapMenu idiom) at the tap point. */
  const beginMoveTo = useCallback((
    slug: string, departmentId: string, x: number, y: number,
  ) => {
    if (departmentId === '') {
      setMoveFrom(null)
      const display = nodeBySlug.get(slug)?.displayName ?? slug
      updateAgent.mutate(
        { name: slug, department_id: '', department_level_id: '' },
        {
          onSuccess: () => {
            setNotice(`${display} is now independent`)
            qc.invalidateQueries({ queryKey: ['departments'] })
            qc.invalidateQueries({ queryKey: ['delegation-edges-all'] })
          },
          onError: (e) => setNotice(e.message),
        },
      )
      return
    }
    const dept = departments.find((d) => d.id === departmentId)
    if (!dept) return
    const levels = [...dept.levels].sort((a, b) => a.rank - b.rank)
    if (levels.length === 0) {
      setMoveFrom(null)
      setNotice(`“${dept.name}” has no levels yet — add levels in the Departments editor first`)
      return
    }
    if (levels.length === 1) {
      completeMove(slug, dept.id, levels[0].id, dept.name, levels[0].name)
      return
    }
    setLevelPick({ slug, departmentId, x, y })
  }, [departments, completeMove, nodeBySlug, updateAgent, qc])

  const onDeptTap = useCallback((
    departmentId: string, x: number, y: number,
  ) => {
    const mover = moveFromRef.current
    if (mover) {
      beginMoveTo(mover, departmentId, x, y)
      return
    }
    enterStage(departmentId)
  }, [beginMoveTo, enterStage])
  const onDeptTapRef = useRef(onDeptTap)
  useEffect(() => { onDeptTapRef.current = onDeptTap }, [onDeptTap])

  const handleTap = useCallback((clientX: number, clientY: number) => {
    // Cards handle their own clicks (real DOM); the canvas picks the
    // territory blobs and clears overlays on empty taps.
    const hit = pick(clientX, clientY)
    setMenu(null)
    if (!hit) {
      setPopup(null)
      return
    }
    onDeptTap(hit.departmentId, clientX, clientY)
  }, [pick, onDeptTap])

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    // Taps/long-presses belong to the canvas only (events bubbling up
    // from the CSS3D cards are the cards' business). On the PAGED levels
    // — stage AND the whole-map view (round 16: it behaves exactly like
    // the stage) — a swipe may START on a card too: cards cover much of
    // a phone screen and a drag is never the card's gesture (the click
    // guard swallows the release-click a real swipe would leak).
    const onCanvas = e.target instanceof HTMLCanvasElement
    const bag = bagRef.current
    const staged = bag != null && bag.stageId !== null
    const paged = staged || (bag != null && bag.mapId !== null)
    if (!onCanvas && !paged) return
    // Ghost sweep at every gesture start — cancelled/detached pointers must
    // never block or poison a new pinch (see gestures.ts).
    prunePointers(
      activePointers.current, performance.now(),
      (el) => !el || document.contains(el),
      gestureOwnedIds(),
    )
    activePointers.current.set(e.pointerId, {
      x: e.clientX, y: e.clientY, t: performance.now(),
      el: e.target instanceof Node ? e.target : null,
    })
    if (paged && !pinch.current && activePointers.current.size >= 2) {
      // Two (or more — extras ignored) fingers on a paged level = pinch on
      // the NEWEST two: gentle zoom within the clamp; past it the level
      // switches (stage→map, map→stage dive).
      const ids = newestTwoIds(activePointers.current)
      const d0 = ids && pinchSpan(activePointers.current, ids[0], ids[1])
      if (ids && d0 != null) {
        pinch.current = {
          a: ids[0], b: ids[1], d0,
          zoom0: staged ? bag.zoomT : bag.mapZoomT,
          lastT: performance.now(),
        }
      }
      swipe.current = null
      downPos.current = null
      if (longPress.current) {
        window.clearTimeout(longPress.current)
        longPress.current = null
      }
      bag.par.dragging = false
      return
    }
    if (paged) {
      swipe.current = {
        id: e.pointerId,
        x0: e.clientX, y0: e.clientY,
        lastX: e.clientX, lastT: performance.now(), vx: 0,
      }
    }
    if (!onCanvas) return // card-origin: swipe/pinch only — never a map tap
    downPos.current = { x: e.clientX, y: e.clientY }
    if (longPress.current) window.clearTimeout(longPress.current)
    longPress.current = window.setTimeout(() => {
      const hit = pick(e.clientX, e.clientY)
      setMenu({
        x: e.clientX, y: e.clientY,
        departmentId: hit?.departmentId || null,
        departmentName: hit?.departmentName,
      })
      downPos.current = null
      swipe.current = null
      const b = bagRef.current
      if (b) { b.par.dragging = false; b.par.tx = 0; b.par.ty = 0 }
    }, 550)
  }, [pick])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const tracked = activePointers.current.get(e.pointerId)
    if (tracked) {
      tracked.x = e.clientX
      tracked.y = e.clientY
      tracked.t = performance.now()
    } else if (e.buttons !== 0 || e.pointerType === 'touch') {
      // A genuinely-down pointer we lost (over-eager prune / missed down):
      // re-register — heals any wrong drop within one frame of movement.
      activePointers.current.set(e.pointerId, {
        x: e.clientX, y: e.clientY, t: performance.now(),
        el: e.target instanceof Node ? e.target : null,
      })
    }
    if (pinch.current) {
      const p = pinch.current
      let d = pinchSpan(activePointers.current, p.a, p.b)
      if (d == null) {
        // A pinch finger vanished (cancel/prune): re-seat on the newest
        // two or drop — never measure against a ghost.
        const ids = newestTwoIds(activePointers.current)
        const bag0 = bagRef.current
        const d0 = ids && pinchSpan(activePointers.current, ids[0], ids[1])
        if (!ids || d0 == null || !bag0) { pinch.current = null; return }
        pinch.current = {
          a: ids[0], b: ids[1], d0,
          zoom0: bag0.stageId !== null ? bag0.zoomT : bag0.mapZoomT,
          lastT: performance.now(),
        }
        return
      }
      const bag = bagRef.current
      if (bag && d > 0) {
        const now = performance.now()
        const dtMs = now - p.lastT
        p.lastT = now
        const want = p.zoom0 / (d / p.d0)
        if (bag.stageId !== null) {
          if (want > STAGE_ZOOM_MAX * 1.35) {
            // Pinched well past the zoom-out clamp → the level switch DOWN.
            pinch.current = null
            bag.wheelOut = 0
            exitStageRef.current()
            return
          }
          bag.zoomT = Math.min(STAGE_ZOOM_MAX, Math.max(STAGE_ZOOM_MIN, want))
          // Repeated-pinch exit: outward pressure past the clamp feeds the
          // wheel's own accumulator (decayed in the rAF) — the single-
          // gesture fast exit above stays, but a user whose zoom sits at
          // the floor can now escape with several small pinches too.
          const pressure = pinchOutPressure(want, STAGE_ZOOM_MAX, dtMs)
          if (pressure > 0) {
            bag.wheelOut += pressure
            if (bag.wheelOut > WHEEL_EXIT_ACCUM) {
              pinch.current = null
              bag.wheelOut = 0
              exitStageRef.current()
              return
            }
          } else if (want < STAGE_ZOOM_MAX) {
            bag.wheelOut = 0 // inward pinch resets, mirroring the wheel
          }
        } else if (bag.mapId !== null) {
          // Whole-map paged: pinch drives the map zoom; the rAF dive
          // check enters the front department past the in-clamp.
          bag.mapZoomT = Math.min(1.25, Math.max(0.55, want))
        }
      }
      return
    }
    if (downPos.current && longPress.current) {
      const dx = e.clientX - downPos.current.x
      const dy = e.clientY - downPos.current.y
      if (dx * dx + dy * dy > 64) {
        window.clearTimeout(longPress.current)
        longPress.current = null
      }
    }
    if (swipe.current && e.pointerId === swipe.current.id) {
      const dx = e.clientX - swipe.current.x0
      const dy = e.clientY - swipe.current.y0
      const bag = bagRef.current
      if (bag) {
        // Rubber-band parallax: the stage follows the finger with
        // resistance (content follows the drag → camera slides opposite).
        bag.par.dragging = true
        bag.par.tx = Math.tanh(dx / 240) * -40
        bag.par.ty = Math.max(-4, Math.min(4, -dy * 0.02))
      }
      const now = performance.now()
      const dtMs = now - swipe.current.lastT
      if (dtMs > 0) {
        swipe.current.vx = (e.clientX - swipe.current.lastX) / dtMs
      }
      swipe.current.lastX = e.clientX
      swipe.current.lastT = now
    }
  }, [])

  const onPointerUp = useCallback((e: React.PointerEvent) => {
    activePointers.current.delete(e.pointerId)
    if (pinch.current && (pinch.current.a === e.pointerId
        || pinch.current.b === e.pointerId
        || activePointers.current.size < 2)) {
      pinch.current = null
    }
    if (longPress.current) {
      window.clearTimeout(longPress.current)
      longPress.current = null
    }
    if (swipe.current && e.pointerId === swipe.current.id) {
      const dx = e.clientX - swipe.current.x0
      const vx = swipe.current.vx
      swipe.current = null
      const bag = bagRef.current
      if (bag) {
        bag.par.dragging = false
        bag.par.tx = 0
        bag.par.ty = 0
        if (Math.abs(dx) > 12) {
          // Real movement: the browser may still fire a click on the card
          // the gesture started on — swallow exactly one.
          bag.swallowClick = true
          window.setTimeout(() => { bag.swallowClick = false }, 150)
        }
      }
      if (Math.abs(dx) > 70 || Math.abs(vx) > 0.45) {
        // A real swipe: page to the prev/next department. The whole-map
        // view sees the ring from OUTSIDE while the stage sits inside
        // it, so screen-left/right maps to the opposite ring direction
        // there (operator caught the mirror).
        const dir: 1 | -1 = dx < 0 ? 1 : -1
        const mapMode = bag != null
          && bag.stageId === null && bag.mapId !== null
        pageDeltaRef.current(mapMode ? (dir === 1 ? -1 : 1) : dir)
        downPos.current = null
        return
      }
    }
    const down = downPos.current
    downPos.current = null
    if (!down) return
    const dx = e.clientX - down.x
    const dy = e.clientY - down.y
    if (dx * dx + dy * dy < 25) handleTap(e.clientX, e.clientY)
  }, [handleTap])

  // Cancelled pointers (system edge-swipe, palm rejection, scroll takeover,
  // a chip torn down under the finger). A cancelled gesture must clean up
  // WITHOUT side effects: no paging, no click-swallow, no tap.
  const endPointer = useCallback((pointerId: number) => {
    activePointers.current.delete(pointerId)
    if (pinch.current && (pinch.current.a === pointerId
        || pinch.current.b === pointerId
        || activePointers.current.size < 2)) {
      pinch.current = null
    }
    if (swipe.current?.id === pointerId) swipe.current = null
    if (longPress.current) {
      window.clearTimeout(longPress.current)
      longPress.current = null
    }
    downPos.current = null
    const bag = bagRef.current
    if (bag && activePointers.current.size === 0) {
      // Kill the parallax latch — while `dragging` is stuck true the rAF
      // never decays par.tx/ty and the camera stays rubber-band-offset.
      bag.par.dragging = false
      bag.par.tx = 0
      bag.par.ty = 0
    }
  }, [])
  const onPointerCancel = useCallback((e: React.PointerEvent) => {
    endPointer(e.pointerId)
  }, [endPointer])

  // Backstop for releases the container never sees: the pager strip and
  // banners are SIBLING overlays (a mouse release over them targets a
  // non-descendant), and a mouse released outside the viewport reports to
  // the document at best. Capture-phase + deferred: the check runs AFTER
  // the container's own bubble handlers, so a normally-delivered pointerup
  // keeps its swipe/tap semantics and this only sweeps what they missed.
  useEffect(() => {
    const sweep = (e: PointerEvent) => {
      const id = e.pointerId
      window.setTimeout(() => {
        if (activePointers.current.has(id)) endPointer(id)
      }, 0)
    }
    document.addEventListener('pointerup', sweep, true)
    document.addEventListener('pointercancel', sweep, true)
    return () => {
      document.removeEventListener('pointerup', sweep, true)
      document.removeEventListener('pointercancel', sweep, true)
    }
  }, [endPointer])

  // A pending long-press must not fire a context menu on an unmounted tree.
  useEffect(() => () => {
    if (longPress.current) window.clearTimeout(longPress.current)
  }, [])

  const onContextMenu = useCallback((e: React.MouseEvent) => {
    if (!(e.target instanceof HTMLCanvasElement)) return
    e.preventDefault()
    const hit = pick(e.clientX, e.clientY)
    setMenu({
      x: e.clientX, y: e.clientY,
      departmentId: hit?.departmentId || null,
      departmentName: hit?.departmentName,
    })
  }, [pick])

  const canCreateDepartments = user?.role === 'admin' || user?.role === 'creator'
  const popupDept = popup
    ? departments.find((d) => d.id === popup.node.departmentId)
    : undefined
  const popupLevel = popup && popupDept
    ? popupDept.levels.find((lv) =>
      popupDept.members.find((m) => m.name === popup.node.slug)?.level_id === lv.id)
    : undefined
  // Manual link partners of the popup agent, with the direction each link
  // runs (→ outgoing / ← incoming / ↔ both). Every partner gets an Unlink
  // action severing BOTH directions.
  const popupPartners = useMemo(() => {
    if (!popup) return [] as { partner: string; glyph: string }[]
    const s = popup.node.slug
    const dirs = new Map<string, { out: boolean; inc: boolean }>()
    for (const e of edges) {
      if (e.source !== 'manual') continue
      if (e.from === s) {
        const d = dirs.get(e.to) ?? { out: false, inc: false }
        d.out = true
        dirs.set(e.to, d)
      } else if (e.to === s) {
        const d = dirs.get(e.from) ?? { out: false, inc: false }
        d.inc = true
        dirs.set(e.from, d)
      }
    }
    return [...dirs.entries()]
      .map(([partner, d]) => ({
        partner,
        glyph: d.out && d.inc ? '↔' : d.out ? '→' : '←',
      }))
      .sort((a, b) => a.partner.localeCompare(b.partner))
  }, [popup, edges])

  const agentActions: MapMenuAction[] = []
  if (popup) {
    const n = popup.node
    if (!n.grayed) {
      agentActions.push({
        key: 'open',
        label: 'Open chat',
        icon: <MenuIcon d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />,
        onClick: () => navigate(`/chat/${n.slug}`),
      })
      agentActions.push({
        key: 'favorite',
        label: user?.default_agent === n.slug ? 'Favorite ★' : 'Set as favorite',
        icon: <MenuIcon d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.196-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.783-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />,
        onClick: () => {
          setDefault.mutate(n.slug, { onSuccess: () => void refreshUser() })
        },
      })
      if (user && canManageAgent(user, n.slug)) {
        agentActions.push({
          key: 'link',
          label: 'Link delegation…',
          icon: <MenuIcon d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />,
          onClick: () => { setLinkFrom(n.slug); setMoveFrom(null) },
        })
        for (const { partner, glyph } of popupPartners) {
          agentActions.push({
            key: `unlink-${partner}`,
            label: `Unlink ${glyph} “${nodeBySlug.get(partner)?.displayName ?? partner}”`,
            icon: <MenuIcon d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1M4 4l16 16" />,
            onClick: () => void attemptUnlink(n.slug, partner),
          })
        }
      }
      // Department assignment is the wider grant (wires N edges at once):
      // manager alone is not enough — the backend field gate needs the
      // platform admin/creator role on top.
      if (user && canManageAgent(user, n.slug) && canCreateDepartments) {
        agentActions.push({
          key: 'move-dept',
          label: 'Move to department…',
          icon: <MenuIcon d="M17 8l4 4m0 0l-4 4m4-4H3" />,
          onClick: () => { setMoveFrom(n.slug); setLinkFrom(null) },
        })
        if (n.departmentId) {
          agentActions.push({
            key: 'remove-dept',
            label: 'Remove from department',
            icon: <MenuIcon d="M15 12H9m12 0a9 9 0 11-18 0 9 9 0 0118 0z" />,
            onClick: () => {
              updateAgent.mutate(
                { name: n.slug, department_id: '', department_level_id: '' },
                {
                  onSuccess: () => {
                    setNotice(`Removed ${n.displayName} from ${popupDept?.name ?? 'its department'}`)
                    qc.invalidateQueries({ queryKey: ['departments'] })
                    qc.invalidateQueries({ queryKey: ['delegation-edges-all'] })
                  },
                  onError: (e) => setNotice(e.message),
                },
              )
            },
          })
        }
      }
    } else if (user?.role === 'admin') {
      agentActions.push({
        key: 'add-me',
        label: 'Add me to this agent',
        icon: <MenuIcon d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z" />,
        onClick: () => {
          addMe.mutate(
            { sub: user.sub, agent: n.slug },
            {
              onSuccess: () => {
                setNotice(`Added you to ${n.displayName}`)
                void refreshUser()
              },
              onError: (e) => setNotice(e.message),
            },
          )
        },
      })
    }
  }

  const mapActions: MapMenuAction[] = []
  if (menu) {
    if (menu.departmentId) {
      mapActions.push({
        key: 'edit-dept',
        label: `Edit “${menu.departmentName}”…`,
        icon: <MenuIcon d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />,
        onClick: () => onOpenDepartments(menu.departmentId!),
      })
    }
    if (canCreateDepartments) {
      mapActions.push({
        key: 'new-dept',
        label: 'New department…',
        icon: <MenuIcon d="M12 4v16m8-8H4" />,
        onClick: () => setNewDeptName(''),
      })
      // Same actions (and the same page-hosted popups) as the grid view's
      // header buttons — the map must not make agent creation harder to
      // reach than the fallback view (operator round 17).
      mapActions.push({
        key: 'create-agent',
        label: 'Create agent…',
        icon: <MenuIcon d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z" />,
        onClick: onCreateAgent,
      })
      mapActions.push({
        key: 'browse-community',
        label: 'Browse community…',
        icon: <MenuIcon d="M21 21l-4.35-4.35m1.85-5.65a7.5 7.5 0 11-15 0 7.5 7.5 0 0115 0z" />,
        onClick: onBrowseCommunity,
      })
    }
    mapActions.push({
      key: 'departments',
      label: 'Departments editor',
      icon: <MenuIcon d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />,
      onClick: () => onOpenDepartments(),
    })
  }

  return (
    <div
      className="relative w-full h-full min-h-0"
      data-testid="agents-map-3d"
      style={{
        background:
          'radial-gradient(120% 90% at 50% 30%, #20313c 0%, #101c24 55%, #070d12 100%)',
      }}
    >
      <div
        ref={containerRef}
        className="absolute inset-0"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onLostPointerCapture={onPointerCancel}
        onContextMenu={onContextMenu}
      />

      {/* Top-left overlay: the admin scope toggle lives INSIDE the map
          (it overflowed the mobile top bar). Level navigation has no
          buttons — zoom IS the navigation. */}
      {isAdmin && (
        <div className="absolute top-3 left-3 z-10">
          <button
            onClick={onToggleHideNonMember}
            title={hideNonMember
              ? 'Showing only agents and departments you are a member of'
              : 'Showing everything on this installation'}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-white/8 text-slate-200 border border-white/12 backdrop-blur-sm hover:bg-white/15 transition-colors"
          >
            {hideNonMember ? (
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
              </svg>
            ) : (
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
              </svg>
            )}
            {hideNonMember ? 'Mine only' : 'Everything'}
          </button>
        </div>
      )}

      {linkFrom && (
        <ModeBanner
          icon={(
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
            </svg>
          )}
          name={nodeBySlug.get(linkFrom)?.displayName ?? linkFrom}
          color={nodeBySlug.get(linkFrom)?.color || DEFAULT_TINT}
          hint="tap an agent it can delegate to"
          onCancel={() => setLinkFrom(null)}
        />
      )}

      {moveFrom && (
        <ModeBanner
          icon={(
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 8l4 4m0 0l-4 4m4-4H3" />
            </svg>
          )}
          name={nodeBySlug.get(moveFrom)?.displayName ?? moveFrom}
          color={nodeBySlug.get(moveFrom)?.color || DEFAULT_TINT}
          hint="tap a department"
          onCancel={() => setMoveFrom(null)}
        />
      )}

      {notice && (
        <div className={`absolute ${stage || mapDept !== null ? 'bottom-16' : 'bottom-3'} left-1/2 -translate-x-1/2 z-10 px-3 py-1.5 text-xs rounded-lg bg-white/8 text-slate-200 border border-white/12 backdrop-blur-sm flex items-center gap-2`}>
          {notice}
          <button onClick={() => setNotice(null)} className="text-slate-400">✕</button>
        </div>
      )}

      {/* The pager strip (both paged levels — round 16): the XCOM
          tab-strip idiom — dept names in ring order, Independents last.
          On stage a tap dollies there; at the whole-map view it swings
          the camera behind that department. In move mode a tap targets
          the department instead. */}
      {(stage !== null || mapDept !== null) && layout.clusters.length > 1 && (
        /* Desktop: one glass strip. Phones: a full-bleed center-locked
           carousel — each chip carries its own glass and the huge side
           padding lets EVERY chip (first and last included) reach the
           exact center when scrollIntoView centers it. */
        <div className="absolute bottom-3 inset-x-0 z-10 flex justify-center pointer-events-none">
          <div
            ref={pagerRef}
            className="pointer-events-auto flex items-center overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden max-sm:w-full max-sm:gap-1.5 max-sm:px-[38vw] max-sm:snap-x max-sm:snap-proximity max-sm:[mask-image:linear-gradient(to_right,transparent,black_24px,black_calc(100%-24px),transparent)] sm:gap-0.5 sm:px-1.5 sm:py-1 sm:rounded-xl sm:bg-[#12152a]/80 sm:border sm:border-white/10 sm:backdrop-blur-md sm:max-w-[94%]"
          >
            {layout.clusters.map((c) => {
              const label = c.name || 'Independent'
              const active = (stage?.deptId ?? mapDept) === c.departmentId
              return (
                <button
                  key={c.departmentId || '·'}
                  data-active={active || undefined}
                  onClick={(e) => {
                    if (moveFromRef.current) {
                      beginMoveTo(
                        moveFromRef.current, c.departmentId,
                        e.clientX, e.clientY,
                      )
                      return
                    }
                    if (stage) setStage({ deptId: c.departmentId })
                    else setMapDept(c.departmentId)
                  }}
                  className={`px-2.5 py-1 text-[11px] rounded-lg whitespace-nowrap transition-colors max-sm:shrink-0 max-sm:snap-center max-sm:px-3.5 max-sm:py-1.5 max-sm:rounded-xl max-sm:border max-sm:backdrop-blur-md ${
                    active
                      ? 'bg-white/14 text-slate-100 max-sm:bg-[#1a2040]/90 max-sm:border-white/25'
                      : 'text-slate-400 hover:text-slate-200 max-sm:bg-[#12152a]/70 max-sm:border-white/10'
                  }`}
                >
                  {label}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {popup && (
        <MapMenu
          x={popup.x}
          y={popup.y}
          onClose={() => setPopup(null)}
          header={{
            title: popup.node.displayName,
            subtitle: [
              popupDept
                ? `${popupDept.name}${popupLevel ? ` · ${popupLevel.name}` : ''}`
                : null,
              popup.node.grayed ? 'not a member' : null,
            ].filter(Boolean).join(' · ') || undefined,
          }}
          actions={agentActions}
        />
      )}

      {menu && (
        <MapMenu
          x={menu.x}
          y={menu.y}
          onClose={() => setMenu(null)}
          header={menu.departmentId
            ? { title: menu.departmentName ?? '', subtitle: 'Department' }
            : undefined}
          actions={mapActions}
        />
      )}

      {levelPick && (() => {
        const dept = departments.find((d) => d.id === levelPick.departmentId)
        if (!dept) return null
        return (
          <MapMenu
            x={levelPick.x}
            y={levelPick.y}
            onClose={() => setLevelPick(null)}
            header={{
              title: `Move to ${dept.name}`,
              subtitle: nodeBySlug.get(levelPick.slug)?.displayName ?? levelPick.slug,
            }}
            actions={[...dept.levels].sort((a, b) => a.rank - b.rank).map((lv) => ({
              key: lv.id,
              label: lv.name,
              icon: <MenuIcon d="M12 5l7 4-7 4-7-4 7-4zM5 15l7 4 7-4" />,
              onClick: () =>
                completeMove(levelPick.slug, dept.id, lv.id, dept.name, lv.name),
            }))}
          />
        )
      })()}

      {newDeptName !== null && (
        <NewDepartmentInline
          name={newDeptName}
          onName={setNewDeptName}
          onClose={() => setNewDeptName(null)}
          onCreated={(id) => { setNewDeptName(null); onOpenDepartments(id) }}
        />
      )}
    </div>
  )
}

/** The mode banner (link / move): the app's glass idiom — dark panel,
 * classic border, the source agent as a color-tinted chip (initials +
 * name), a short hint and a proper cancel button. */
function ModeBanner({ icon, name, color, hint, onCancel }: {
  icon: ReactNode
  name: string
  color: string
  hint: string
  onCancel: () => void
}) {
  const initials = name.trim().slice(0, 2).toUpperCase()
  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2 pl-3 pr-1.5 py-1.5 rounded-xl bg-[#171b30]/90 border border-white/12 backdrop-blur-md shadow-2xl">
      <span className="text-brand-light shrink-0">{icon}</span>
      <span
        className="flex items-center gap-1.5 pl-1 pr-2 py-0.5 rounded-lg border shrink min-w-0"
        style={{ background: `${color}1f`, borderColor: `${color}55` }}
      >
        <span
          className="flex items-center justify-center w-[18px] h-[18px] rounded-md text-[8px] font-bold shrink-0"
          style={{ background: `${color}33`, color }}
        >
          {initials}
        </span>
        <span className="text-xs font-medium text-slate-100 max-w-36 truncate">{name}</span>
      </span>
      <span className="text-xs text-slate-400 whitespace-nowrap">{hint}</span>
      <button
        onClick={onCancel}
        aria-label="Cancel"
        className="p-1 rounded-md text-slate-400 hover:text-slate-200 hover:bg-white/8 shrink-0"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  )
}

function MenuIcon({ d }: { d: string }) {
  return (
    <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={d} />
    </svg>
  )
}

interface MapMenuAction {
  key: string
  label: string
  tone?: 'default' | 'danger'
  icon: ReactNode
  onClick: () => void
}

/** The workspace context-menu idiom (FileContextMenu's exact styling) with
 * an optional header block: agent/department name + subtitle (operator
 * 2026-08-15: no description — the menu is for acting, not reading). */
function MapMenu({ x, y, header, actions, onClose }: {
  x: number
  y: number
  header?: { title: string; subtitle?: string }
  actions: MapMenuAction[]
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => pushEscHandler(onClose), [onClose])
  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', onMouseDown)
    return () => document.removeEventListener('mousedown', onMouseDown)
  }, [onClose])

  // Clamp inside viewport.
  const maxX = typeof window !== 'undefined' ? window.innerWidth - 210 : x
  const maxY = typeof window !== 'undefined' ? window.innerHeight - 240 : y
  const clampedX = Math.min(x, maxX)
  const clampedY = Math.min(y, maxY)

  return (
    <div
      ref={ref}
      style={{ position: 'fixed', top: clampedY, left: clampedX, zIndex: 60 }}
      className="min-w-[190px] max-w-[250px] bg-white dark:bg-p-surface rounded-lg border border-p-border-light shadow-lg py-1"
    >
      {header && (
        <div className={`px-3 pt-1.5 pb-2 ${actions.length ? 'mb-1 border-b border-p-border-light' : ''}`}>
          <div className="text-sm font-medium text-p-text truncate">{header.title}</div>
          {header.subtitle && (
            <div className="text-[11px] text-p-text-secondary mt-0.5">{header.subtitle}</div>
          )}
        </div>
      )}
      {actions.map((a) => (
        <button
          key={a.key}
          onClick={() => {
            a.onClick()
            onClose()
          }}
          className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs ${
            a.tone === 'danger'
              ? 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20'
              : 'text-p-text hover:bg-p-surface-hover'
          }`}
        >
          {a.icon}
          <span>{a.label}</span>
        </button>
      ))}
    </div>
  )
}

function NewDepartmentInline({ name, onName, onClose, onCreated }: {
  name: string
  onName: (v: string) => void
  onClose: () => void
  onCreated: (id: string) => void
}) {
  const create = useCreateDepartment()
  return (
    <div className="absolute z-30 top-14 left-1/2 -translate-x-1/2 rounded-xl bg-[#171b30]/95 border border-white/10 shadow-2xl backdrop-blur-md p-3 flex items-center gap-2">
      <input
        autoFocus
        value={name}
        onChange={(e) => onName(e.target.value)}
        placeholder="Department name"
        className="px-2 py-1 text-sm rounded-md bg-transparent border border-white/12 text-slate-200 outline-none focus:border-brand"
        onKeyDown={(e) => { if (e.key === 'Escape') onClose() }}
      />
      <button
        disabled={!name.trim() || create.isPending}
        className="px-2.5 py-1 text-xs rounded-md bg-brand text-white hover:bg-brand-hover disabled:opacity-50"
        onClick={() =>
          create.mutate({ name: name.trim() }, {
            onSuccess: (d) => onCreated(d.id),
          })}
      >
        Create
      </button>
      <button onClick={onClose} className="text-slate-500 hover:text-slate-300 text-xs">✕</button>
      {create.isError && (
        <span className="text-xs text-p-error">{(create.error as Error).message}</span>
      )}
    </div>
  )
}
