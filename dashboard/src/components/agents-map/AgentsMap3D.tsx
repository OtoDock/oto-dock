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
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAgents, useSetDefaultAgent, useUpdateAgent } from '../../api/agents'
import {
  useAgentsActivity,
  useDelegationEdges,
  useDepartments,
  useAdminAddUserAgent,
} from '../../api/departments'
import { useAuth } from '../../contexts/AuthContext'
import {
  computeHeat,
  computeMapLayout,
  computeStageLayout,
  type MapNode,
} from './layout'
import { DEFAULT_TINT, STAGE_CAM_MAX } from './mapConstants'
import type { MenuState, PopupState } from './mapConstants'
import { viewScale, type SceneBag } from './sceneBag'
import { buildEnvironment } from './buildEnvironment'
import { buildScene } from './buildScene'
import { buildAgentActions, buildMapActions } from './mapActions'
import { MapMenu, MenuIcon, ModeBanner, NewDepartmentInline } from './MapOverlays'
import { useMapRenderer } from './useMapRenderer'
import { useStageMachine } from './useStageMachine'
import { useMapPointers } from './useMapPointers'

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

  // --- renderer lifecycle (useMapRenderer.ts) ------------------------------
  // Runs once; the stage-machine refs are handed over lazily because that
  // hook is called below (hook order unchanged from the single-file map).
  useMapRenderer({
    containerRef, bagRef,
    stageRefs: () => ({ enterStageRef, findClusterRef, exitStageRef }),
    onUnavailable, setAssetsTick, user,
  })

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
    buildEnvironment(bag, layout)

  }, [layout, assetsTick])

  // --- scene contents (rebuilt on any structural/heat/level change) -------
  useEffect(() => {
    const bag = bagRef.current
    if (!bag || bag.disposed) return
    if (agentsPending || deptsPending) return // settle gate — see env effect
    buildScene({
      bag, layout, stage, stageData, heat, liveSet, streamingSet, edges,
      nodeBySlug, user, activePointers, gestureOwnedIds, moveFromRef,
      linkFromRef, attemptLinkRef, openAgentRef, onDeptTapRef, setPopup,
    })
  }, [layout, heat, liveSet, streamingSet, edges, stage, stageData,
    user?.default_agent, nodeBySlug])

  // --- the semantic-zoom state machine (useStageMachine.ts) ---------------
  const {
    enterStage, enterStageRef, findClusterRef, exitStageRef, pageDeltaRef,
    pagerRef,
  } = useStageMachine({
    bagRef, layout, stage, setStage, mapDept, setMapDept, stageData,
    setPopup, setMenu, user, favDeptRef,
  })

  // --- pointer interaction (useMapPointers.ts) ----------------------------
  const {
    activePointers, gestureOwnedIds, attemptLinkRef, onDeptTapRef,
    attemptUnlink, completeMove, beginMoveTo,
    onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onContextMenu,
  } = useMapPointers({
    bagRef, containerRef, nodeBySlug, qc, setLinkFrom, setNotice, setMoveFrom,
    setLevelPick, updateAgent, departments, moveFromRef, enterStage,
    exitStageRef, pageDeltaRef, setMenu, setPopup,
  })

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

  const agentActions = buildAgentActions({
    popup, user, navigate, setDefault, refreshUser, popupPartners, nodeBySlug,
    attemptUnlink, canCreateDepartments, setLinkFrom, setMoveFrom, updateAgent,
    setNotice, popupDept, qc, addMe,
  })
  const mapActions = buildMapActions({
    menu, onOpenDepartments, canCreateDepartments, setNewDeptName,
    onCreateAgent, onBrowseCommunity,
  })

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
