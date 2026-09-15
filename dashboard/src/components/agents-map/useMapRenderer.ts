/** The renderer lifecycle of the 3D map — renderer, CSS3D layer, camera,
 * controls, texture loads, lights, bloom, wheel and click capture, the rAF
 * `tick`, the ResizeObserver and the teardown — as the one effect that runs
 * once per mount (split out of AgentsMap3D.tsx on 2026-09-10; the body and
 * its empty dependency array are the component's, verbatim). */
import { useEffect } from 'react'
import type { Dispatch, RefObject, SetStateAction } from 'react'
import * as THREE from 'three'
import { MapControls } from 'three/examples/jsm/controls/MapControls.js'
import { CSS3DRenderer } from 'three/examples/jsm/renderers/CSS3DRenderer.js'
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js'
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js'
import type { User } from '../../api/auth'
import {
  ENTER_STAGE_DIST, STAGE_ZOOM_MAX, STAGE_ZOOM_MIN, WHEEL_EXIT_ACCUM,
  WHEEL_HOLD_GAP_MS, WHEEL_HOLD_MS,
} from './mapConstants'
import {
  disposeObject, TMP_FWD, TMP_LOOK, TMP_RIGHT, updateSparks, viewScale,
  WORLD_UP, type SceneBag,
} from './sceneBag'
import {
  applyWoodTexture, ensureMapStyles, makeBackgroundTexture, makeGlowTexture,
} from './textures'
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

/** The stage machine's refs (enterStage / findCluster / exitStage), read
 * LAZILY: `useStageMachine` is called after this hook in the component —
 * hook order is load-bearing there (its pose effects must find the bag this
 * effect creates on a warm re-mount) — so the refs are not bound yet when
 * this hook is called, and are by the time the effect runs. */
export interface StageRefs {
  enterStageRef: RefObject<(deptId: string) => void>
  findClusterRef: RefObject<(x: number, z: number) => string | null>
  exitStageRef: RefObject<() => void>
}

export function useMapRenderer({
  containerRef, bagRef, stageRefs, onUnavailable, setAssetsTick, user,
}: {
  containerRef: RefObject<HTMLDivElement | null>
  bagRef: RefObject<SceneBag | null>
  stageRefs: () => StageRefs
  onUnavailable: () => void
  setAssetsTick: Dispatch<SetStateAction<number>>
  /** The mount-time user (`user?.default_agent` for the pose park) — the
   * effect runs once and reads the argument it was called with. */
  user: User | null
}) {
  // --- renderer lifecycle -------------------------------------------------
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const { enterStageRef, findClusterRef, exitStageRef } = stageRefs()
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
      wheelHoldUntil: 0,
      lastWheelAt: 0,
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
      const now = performance.now()
      if (bag.exitFly) {
        // FIRST: the exit flight owns the camera. stageId is still set
        // until the stage effect commits, so an unguarded pulse would
        // re-accumulate wheelOut and re-trigger the exit; and once the
        // effect lands the paged branch would feed the map zoom while the
        // flight is still in the air.
        ev.preventDefault()
        bag.lastWheelAt = now
        return
      }
      if (bag.stageId === null) {
        if (bag.mapId === null) return // free-roam: MapControls owns it
        // Whole-map paged: wheel drives the gentle map zoom; diving past
        // the in-clamp enters the front department's stage (the dive
        // check lives in the rAF where the eased value crosses it).
        ev.preventDefault()
        if (bag.wheelHoldUntil > 0) {
          const released = ev.deltaY < 0
            || (now >= bag.wheelHoldUntil
              && now - bag.lastWheelAt >= WHEEL_HOLD_GAP_MS)
          if (!released) {
            bag.lastWheelAt = now
            return
          }
          bag.wheelHoldUntil = 0
        }
        bag.lastWheelAt = now
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
            // IS the composed pose (same function as the paged goal), so
            // nothing lerps after the handoff. A swipe that ran during the
            // flight left parallax targets behind — drop them.
            bag.mapCamPos.copy(camera.position)
            bag.mapLook.copy(TMP_LOOK)
            bag.par = { x: 0, y: 0, tx: 0, ty: 0, dragging: false }
          } else {
            controls.target.copy(fly.lookTo)
            controls.enabled = true
          }
          bag.exitFly = null
          bag.wheelHoldUntil = now + WHEEL_HOLD_MS
          bag.lastWheelAt = now
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
            // pose is low too (0.1405 × distance ≈ 16–28) but sits ≥ 112
            // from the center, far outside any cluster's territory, so
            // landing never re-enters (hysteresis by distance, not height).
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
}
