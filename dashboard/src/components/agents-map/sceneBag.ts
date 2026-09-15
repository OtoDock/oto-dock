/** The scene bag — everything the renderer effect builds once and every
 * other piece of the 3D map mutates through `bagRef` — plus the per-frame
 * helpers that only need the bag (split out of AgentsMap3D.tsx on
 * 2026-09-10). The four `Vector3` scratch objects stay module-level
 * singletons: there is one map instance. */
import * as THREE from 'three'
import type { MapControls } from 'three/examples/jsm/controls/MapControls.js'
import type { CSS3DRenderer } from 'three/examples/jsm/renderers/CSS3DRenderer.js'
import type { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js'
import type { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'
import type { MapCluster } from './layout'
import { mapPoseFor as composeMapPose, viewScaleFor } from './camera'
import { SPARK_MARGIN } from './mapConstants'

export interface SparkParticle {
  x: number; y: number; vx: number; vy: number
  age: number; life: number; size: number
}

/** Per-streaming-card particle field: a small canvas around the card,
 * painted from the main rAF loop (PresenceHalo spark grammar — sparks
 * spawn on the border and fly outward, density scales with heat). */
export interface SparkField {
  canvas: HTMLCanvasElement
  ctx: CanvasRenderingContext2D
  pill: HTMLElement
  rgb: [number, number, number]
  heat: number
  particles: SparkParticle[]
  sized: boolean
}

export interface StageParallax {
  x: number
  y: number
  tx: number
  ty: number
  dragging: boolean
}

export interface SceneBag {
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
  /** Post-landing wheel hold (see WHEEL_HOLD_MS): the DOMHighResTimeStamp
   * before which zoom-out pulses are swallowed (0 = no hold), and the last
   * wheel event's time — the rAF timestamp and performance.now() share
   * the same origin. */
  wheelHoldUntil: number
  lastWheelAt: number
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

export function disposeObject(obj: THREE.Object3D) {
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

export function updateSparks(bag: SceneBag) {
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
export const TMP_FWD = new THREE.Vector3()
export const TMP_RIGHT = new THREE.Vector3()
export const TMP_LOOK = new THREE.Vector3()
export const WORLD_UP = new THREE.Vector3(0, 1, 0)

/** Resolution-aware framing factor (camera.ts): big screens pull the
 * camera back so the map shows MORE instead of bigger cards. Read fresh at
 * every fit — window resizes self-correct on the next framing. */
export function viewScale(): number {
  if (typeof window === 'undefined') return 1
  return viewScaleFor(window.innerWidth, window.innerHeight)
}

/** The composed whole-map pose for one department (camera.ts) for the
 * live camera and viewport. Shared by the stage-exit flight (its landing)
 * and the paged whole-map machinery (its goal) — the same numbers, so the
 * flight lands exactly where paging between departments would put the
 * camera and nothing moves after it. */
export function mapPoseFor(
  camera: THREE.PerspectiveCamera, cluster: MapCluster,
): { cam: THREE.Vector3; look: THREE.Vector3 } {
  return composeMapPose(camera.fov, camera.aspect, cluster, viewScale())
}
