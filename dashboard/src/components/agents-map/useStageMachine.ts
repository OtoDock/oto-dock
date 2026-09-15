/** The semantic-zoom state machine of the 3D map: enterStage / findCluster
 * / exitStage / pageDelta with their stable refs, the desktop arrow keys,
 * the stage-pose and whole-map-pose effects, Esc and the pager-strip
 * centering (split out of AgentsMap3D.tsx on 2026-09-10; every dependency
 * array is the component's, verbatim). */
import { useCallback, useEffect, useRef } from 'react'
import type { Dispatch, RefObject, SetStateAction } from 'react'
import * as THREE from 'three'
import type { User } from '../../api/auth'
import { pushEscHandler } from '../../lib/escStack'
import { stageExitPath } from './camera'
import type { MapCluster, MapLayout, StageLayout } from './layout'
import { STAGE_CAM_MAX, STAGE_CAM_MIN } from './mapConstants'
import type { MenuState, PopupState } from './mapConstants'
import { mapPoseFor, viewScale, type SceneBag } from './sceneBag'

export function useStageMachine({
  bagRef, layout, stage, setStage, mapDept, setMapDept, stageData,
  setPopup, setMenu, user, favDeptRef,
}: {
  bagRef: RefObject<SceneBag | null>
  layout: MapLayout
  stage: { deptId: string } | null
  setStage: Dispatch<SetStateAction<{ deptId: string } | null>>
  mapDept: string | null
  setMapDept: Dispatch<SetStateAction<string | null>>
  stageData: { cluster: MapCluster; arc: StageLayout } | null
  setPopup: Dispatch<SetStateAction<PopupState | null>>
  setMenu: Dispatch<SetStateAction<MenuState | null>>
  user: User | null
  favDeptRef: RefObject<string | null>
}) {
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
    // whole map read from near ground level. The landing is THE paged
    // whole-map pose (camera.ts, one function with the paged goal — the
    // exit's own copy of the distance once lacked the viewScale factor,
    // so the flight landed short and the paged lerp zoomed out again
    // after it). The PATH arcs around the ring at altitude — round 14's
    // straight lerp from a desktop stage pose cut through the empty
    // center and read as a broken two-step zoom.
    const pose = mapPoseFor(bag.camera, cluster)
    bag.camTarget = null
    bag.lookTarget = null
    bag.controls.enabled = false
    bag.exitFly = {
      curve: stageExitPath(bag.camera.position, pose.cam),
      lookFrom: (bag.stageLook ?? bag.controls.target).clone(),
      lookTo: pose.look,
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

  return {
    enterStage, enterStageRef, findClusterRef, exitStageRef, pageDeltaRef,
    pagerRef,
  }
}
