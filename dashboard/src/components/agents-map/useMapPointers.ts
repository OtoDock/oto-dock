/** The pointer layer of the 3D map: the gesture refs, `pick`, the link /
 * unlink / move actions, the department tap, the five pointer handlers,
 * `endPointer`, the document capture sweep and the context menu (split out
 * of AgentsMap3D.tsx on 2026-09-10; every dependency array is the
 * component's, verbatim). */
import { useCallback, useEffect, useRef } from 'react'
import type { Dispatch, RefObject, SetStateAction } from 'react'
import * as THREE from 'three'
import type { QueryClient } from '@tanstack/react-query'
import type { useUpdateAgent } from '../../api/agents'
import { apiFetch } from '../../api/auth'
import type { Department } from '../../api/departments'
import {
  newestTwoIds,
  pinchOutPressure,
  pinchSpan,
  prunePointers,
  type PointerMap,
} from './gestures'
import type { MapNode } from './layout'
import {
  STAGE_ZOOM_MAX, STAGE_ZOOM_MIN, WHEEL_EXIT_ACCUM,
} from './mapConstants'
import type { MenuState, PopupState } from './mapConstants'
import type { SceneBag } from './sceneBag'

export function useMapPointers({
  bagRef, containerRef, nodeBySlug, qc, setLinkFrom, setNotice, setMoveFrom,
  setLevelPick, updateAgent, departments, moveFromRef, enterStage,
  exitStageRef, pageDeltaRef, setMenu, setPopup,
}: {
  bagRef: RefObject<SceneBag | null>
  containerRef: RefObject<HTMLDivElement | null>
  nodeBySlug: Map<string, MapNode>
  qc: QueryClient
  setLinkFrom: Dispatch<SetStateAction<string | null>>
  setNotice: Dispatch<SetStateAction<string | null>>
  setMoveFrom: Dispatch<SetStateAction<string | null>>
  setLevelPick: Dispatch<SetStateAction<{
    slug: string
    departmentId: string
    x: number
    y: number
  } | null>>
  updateAgent: ReturnType<typeof useUpdateAgent>
  departments: Department[]
  moveFromRef: RefObject<string | null>
  enterStage: (deptId: string) => void
  exitStageRef: RefObject<() => void>
  pageDeltaRef: RefObject<(dir: 1 | -1) => void>
  setMenu: Dispatch<SetStateAction<MenuState | null>>
  setPopup: Dispatch<SetStateAction<PopupState | null>>
}) {
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
        } else if (bag.mapId !== null && !bag.exitFly) {
          // Whole-map paged: pinch drives the map zoom; the rAF dive
          // check enters the front department past the in-clamp. The
          // exit flight owns the camera until it lands (the pinch that
          // triggered the exit is dropped above, but a new one can start
          // mid-flight).
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

  return {
    activePointers, gestureOwnedIds, attemptLinkRef, onDeptTapRef,
    attemptUnlink, completeMove, beginMoveTo,
    onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onContextMenu,
  }
}
