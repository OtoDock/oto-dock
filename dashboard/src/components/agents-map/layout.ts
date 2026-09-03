/**
 * Pure layout math for the 3D agents map — no three.js, no DOM.
 *
 * Round 6 is a two-level semantic-zoom map:
 *  - OVERVIEW (the Stellaris layer): each department is a soft tinted
 *    territory blob on the glass floor around a ring slot; agents are
 *    micro-cards placed by the same branching-tree math as before (level
 *    rank steps outward from the hub), which keeps the scatter organic and
 *    deterministic. No tree lines are drawn anymore — membership is the
 *    blob, structure is the stage.
 *  - STAGE (the Vision Pro layer): `computeStageLayout` arranges one
 *    cluster's members into shallow amphitheater arc rows facing the
 *    camera — rank 0 (Head) rearmost and highest, each deeper rank a step
 *    forward and lower, oversized tiers wrapped into balanced sub-rows.
 *
 * All placement is deterministic (per-slug hash jitter) so re-renders never
 * reshuffle the sky. Kept separate from the renderer so the placement rules
 * are testable in jsdom (the renderer no-ops without WebGL).
 */
import type { AgentSummary } from '../../api/agents'
import type { Department } from '../../api/departments'
import { MODE_LABEL, modeOfAgent } from '../../lib/visibility'

export interface MapNode {
  slug: string
  displayName: string
  color: string
  description: string
  x: number
  y: number
  z: number
  departmentId: string // '' = standalone cluster
  levelRank: number // -1 for standalone
  /** Level display name ('' for standalone) — menu subtitle + stage order. */
  levelName: string
  /** Visibility-mode label (the Grid cards' wording) — the stage card's
   * second line. Undefined for grayed dept-mates outside the caller's
   * agents payload (modeOfAgent would soft-default, mislabeling them). */
  modeLabel?: string
  /** Inaccessible dept-mate: grayed, info-popup only, never enterable. */
  grayed: boolean
}

export interface MapCluster {
  /** '' = the standalone cluster (no department behind it). */
  departmentId: string
  name: string
  /** Cluster anchor (blob center, stage center). */
  cx: number
  cy: number
  cz: number
  /** Unit radial direction (map center → cluster) the layout grows along. */
  outX: number
  outZ: number
  /** Level names by rank (empty for standalone). */
  levelNames: string[]
  /** Cluster accent color (stable per department). */
  accent: string
  /** Furthest node distance from the anchor — sizes the territory blob. */
  extent: number
}

export interface MapLayout {
  nodes: MapNode[]
  clusters: MapCluster[]
}

export const RING_RADIUS = 46

/** Hub accent palette — reference pastels, assigned by stable dept order. */
export const HUB_ACCENTS = [
  '#2dd4bf', '#a78bfa', '#f472b6', '#fbbf24', '#7dd3fc', '#fb7185',
  '#86efac', '#c4b5fd',
]

/** Deterministic -0.5..0.5 from the slug so re-renders never reshuffle. */
function jitter(slug: string, salt: number): number {
  let h = 2166136261 ^ salt
  for (let i = 0; i < slug.length; i++) {
    h = Math.imul(h ^ slug.charCodeAt(i), 16777619)
  }
  return ((h >>> 0) % 1000) / 1000 - 0.5
}

export function computeMapLayout(
  agents: AgentSummary[],
  departments: Department[],
  opts?: {
    /** Admin "hide everything I'm not a member of" toggle. */
    hideNonMember?: boolean
    /** Admin viewers: the set of agents they are a MEMBER of. Admins can
     * ACCESS everything (so the dept feed's `accessible` flag is always
     * true for them) — grayed-ness on an admin's map means "not mine",
     * which only this set can decide. Non-admin viewers omit it and the
     * accessible flags drive graying as before. */
    memberOf?: Set<string>
  },
): MapLayout {
  const hide = opts?.hideNonMember ?? false
  const accessible = new Set(agents.map((a) => a.name))
  const memberSet = opts?.memberOf ?? accessible
  const bySlug = new Map(agents.map((a) => [a.name, a]))

  const depts = departments.filter(
    (d) => !hide || d.members.some((m) => memberSet.has(m.name)),
  )
  const visibleDeptIds = new Set(depts.map((d) => d.id))
  const standalone = agents.filter(
    (a) => !a.department_id || !visibleDeptIds.has(a.department_id),
  )

  const slotCount = depts.length + (standalone.length > 0 ? 1 : 0)
  const clusters: MapCluster[] = []
  const nodes: MapNode[] = []

  const slotDir = (slot: number): [number, number] => {
    if (slotCount <= 1) return [0, 1]
    const angle = (slot / slotCount) * Math.PI * 2 - Math.PI / 2
    return [Math.cos(angle), Math.sin(angle)]
  }

  depts.forEach((dept, slot) => {
    const [ox, oz] = slotDir(slot)
    const cx = ox * RING_RADIUS
    const cz = oz * RING_RADIUS
    const accent = HUB_ACCENTS[slot % HUB_ACCENTS.length]
    // Tangent (perpendicular to outward) for fanning tiers sideways.
    const tx = -oz
    const tz = ox

    const levels = [...dept.levels].sort((a, b) => a.rank - b.rank)
    const rankOf = new Map(levels.map((lv) => [lv.id, lv.rank]))
    const nameOfRank = new Map(levels.map((lv) => [lv.rank, lv.name]))
    const members = hide
      ? dept.members.filter((m) => memberSet.has(m.name))
      : dept.members
    const tiers = new Map<number, typeof members>()
    for (const m of members) {
      const rank = rankOf.get(m.level_id)
      if (rank === undefined) continue // dangling level — compiler-skipped too
      const tier = tiers.get(rank) ?? []
      tier.push(m)
      tiers.set(rank, tier)
    }

    // Overview scatter: rank 0 fans off the anchor; each deeper rank steps
    // further out with deterministic sideways/vertical drift — an organic
    // tree-shaped cloud (no lines drawn over it since round 6).
    let extent = 6
    let prevRing: { x: number; y: number; z: number }[] = []
    const occupiedRanks = [...tiers.keys()].sort((a, b) => a - b)
    occupiedRanks.forEach((rank, depth) => {
      const tier = tiers.get(rank)!
      const ring: typeof prevRing = []
      tier.forEach((m, i) => {
        const parent = prevRing.length
          ? prevRing[i % prevRing.length]
          : null
        const spread = (i - (tier.length - 1) / 2) * (prevRing.length ? 9 : 12)
          + jitter(m.name, 1) * 3.5
        const step = depth === 0 ? 11 : 10
        const baseX = parent ? parent.x : cx
        const baseZ = parent ? parent.z : cz
        const baseY = parent ? parent.y : 0
        const x = baseX + ox * step + tx * spread
        const z = baseZ + oz * step + tz * spread
        const y = baseY + jitter(m.name, 2) * 4.5
        ring.push({ x, y, z })
        extent = Math.max(
          extent,
          Math.hypot(x - cx, z - cz) + 3,
        )
        const summary = bySlug.get(m.name)
        nodes.push({
          slug: m.name,
          displayName: m.display_name,
          color: summary?.color || m.color || '',
          description: summary?.description || m.description || '',
          x, y, z,
          departmentId: dept.id,
          levelRank: rank,
          levelName: nameOfRank.get(rank) ?? '',
          modeLabel: summary ? MODE_LABEL[modeOfAgent(summary)] : undefined,
          grayed: !memberSet.has(m.name),
        })
      })
      prevRing = ring
    })

    clusters.push({
      departmentId: dept.id,
      name: dept.name,
      cx, cy: 0, cz,
      outX: ox, outZ: oz,
      levelNames: levels.map((lv) => lv.name),
      accent,
      extent,
    })
  })

  if (standalone.length > 0) {
    // No departments → the whole company IS this cluster: spread it across
    // the CENTER of the floor with a golden-angle spiral (organic, even,
    // centered — never a clump at one edge). With departments present it
    // takes a ring slot like everyone else — but NOT the spiral: the
    // ring-slot camera stands along the slot's outward axis, and a spiral
    // built in world X/Z ignores that entirely, so billboard cards could
    // line up along the DEPTH axis and eclipse each other (three
    // independents left "Jarvis" fully hidden behind another card). Ring
    // slots therefore fan SIDEWAYS in the cluster's outward/tangent basis,
    // exactly like department tiers — lateral separation is guaranteed on
    // screen.
    const only = depts.length === 0
    const [ox, oz] = only ? [0, 1] as const : slotDir(depts.length)
    const cx = only ? 0 : ox * RING_RADIUS
    const cz = only ? 0 : oz * RING_RADIUS
    // Golden-angle spiral (centered case only), radial constant sized for
    // the CSS3D cards. The spiral starts at r=c, NEVER r=0: an agent at
    // the exact origin put the camera tween's target directly above the
    // polar clamp's forbidden pole, where the lerp could never converge —
    // the round-3 "map pinned on one agent" bug. The center stays empty
    // by design.
    const GOLDEN = Math.PI * (3 - Math.sqrt(5))
    const tx = -oz
    const tz = ox
    let extent = 6
    standalone.forEach((a, i) => {
      let x: number
      let z: number
      if (only) {
        const r = 13 * Math.sqrt(i + 1)
        const angle = i * GOLDEN + jitter(a.name, 5) * 0.35
        x = cx + Math.cos(angle) * r
        z = cz + Math.sin(angle) * r
      } else {
        const row = Math.floor(i / 3)
        const rowLen = Math.min(3, standalone.length - row * 3)
        // 13-unit pitch: the overview pills are ~11 world units wide, and
        // the 11-unit pitch let jittered neighbors kiss.
        const lat = ((i % 3) - (rowLen - 1) / 2) * 13 + jitter(a.name, 1) * 1.5
        const back = 8 + row * 10
        x = cx + ox * back + tx * lat
        z = cz + oz * back + tz * lat
      }
      const y = jitter(a.name, 2) * 4
      extent = Math.max(extent, Math.hypot(x - cx, z - cz) + 3)
      nodes.push({
        slug: a.name,
        displayName: a.display_name,
        color: a.color || '',
        description: a.description || '',
        x, y, z,
        departmentId: '',
        levelRank: -1,
        levelName: '',
        modeLabel: MODE_LABEL[modeOfAgent(a)],
        grayed: !memberSet.has(a.name),
      })
    })
    clusters.push({
      departmentId: '',
      // The label exists to DISTINGUISH the cluster from departments — a
      // lone centered scatter needs no name floating beside it.
      name: only ? '' : 'Independent',
      cx, cy: 0, cz,
      outX: ox, outZ: oz,
      levelNames: [],
      accent: '#94a3b8',
      extent,
    })
  }

  return { nodes, clusters }
}

// --- stage layout (the Vision Pro layer) -----------------------------------

export interface StageSlot {
  slug: string
  x: number
  y: number
  z: number
}

export interface StageLayout {
  slots: StageSlot[]
  /** Half-extent of the widest row (world units) — frames the camera. */
  halfWidth: number
  /** Arc row count — the camera rises a little for deeper amphitheaters. */
  rows: number
  /** Rear (Head) row distance from the anchor along the outward axis. */
  depth: number
}

const STAGE_ROW_CAP = 8
// Seat pitch sized for the stage cards (~290 authored px × 0.046 world
// scale ≈ 13.3 world units wide) — cards must never touch.
const STAGE_LAT_SPACING = 16
// Row separation is generous on purpose: at the composed camera angle the
// perspective compresses depth, and round 7's 7/2.2 steps left the rear
// row visually touching the front one on phones.
const STAGE_ROW_STEP = 10
const STAGE_ROW_RISE = 4.2
const STAGE_FRONT_BACK = 6

/**
 * Amphitheater arc rows per level, composed to face a camera positioned
 * INSIDE the ring looking outward (-out): rank 0 (Head) is the FRONT row —
 * closest to the camera, so the top role reads biggest — and each deeper
 * rank steps away and slightly higher (visible over the rows in front,
 * shrinking naturally with distance: the perspective now AGREES with the
 * hierarchy — the round-6 build had it inverted and a Senior card rendered
 * bigger than the Head). Edge seats bow toward the camera. Oversized tiers
 * wrap into balanced sub-rows; `rowCap` comes from the screen (2 on
 * phones, up to 8 on desktop) so no card ever leaves the screen. Row
 * order within a tier is alphabetical — deterministic, never reshuffles.
 */
export function computeStageLayout(
  cluster: MapCluster,
  members: MapNode[],
  opts?: { rowCap?: number },
): StageLayout {
  const rowCap = Math.max(1, Math.min(STAGE_ROW_CAP, opts?.rowCap ?? STAGE_ROW_CAP))
  const tiers = new Map<number, MapNode[]>()
  for (const n of members) {
    const t = tiers.get(n.levelRank) ?? []
    t.push(n)
    tiers.set(n.levelRank, t)
  }
  const ranks = [...tiers.keys()].sort((a, b) => a - b)
  const rows: MapNode[][] = []
  for (const rank of ranks) {
    const tier = [...tiers.get(rank)!]
      .sort((a, b) => a.slug.localeCompare(b.slug))
    const chunks = Math.ceil(tier.length / rowCap)
    const per = Math.ceil(tier.length / chunks)
    for (let i = 0; i < tier.length; i += per) {
      rows.push(tier.slice(i, i + per))
    }
  }
  const { cx, cz, outX, outZ } = cluster
  const tx = -outZ
  const tz = outX
  const slots: StageSlot[] = []
  let halfWidth = 8
  let prevLats: number[] = []
  rows.forEach((row, i) => {
    // i = 0 is the Head row: FRONT (smallest outward offset) + lowest.
    const back = STAGE_FRONT_BACK + i * STAGE_ROW_STEP
    const y = 0.8 + i * STAGE_ROW_RISE
    // Theater-brick stagger, clearance-checked (round 17): the fixed
    // alternating quarter-pitch of round 12 broke exactly when adjacent
    // rows had DIFFERENT length parity — a 2-seat front row shifted +4
    // with a single rear seat at 0 left them 4 units apart, and on the
    // phone rowCap the rear card hid behind the front one (the
    // Independent stage bug). Each row now tries both quarter-pitch
    // shifts and keeps the one that clears the row in front the most;
    // ties keep the old alternating sign, so the arc stays centered and
    // deterministic.
    const latsFor = (offset: number) =>
      row.map((_, j) => (j - (row.length - 1) / 2) * STAGE_LAT_SPACING + offset)
    let stagger = 0
    if (row.length > 1 || prevLats.length > 0) {
      const candidates = [
        (i % 2 === 1 ? -1 : 1) * (STAGE_LAT_SPACING / 4),
        (i % 2 === 1 ? 1 : -1) * (STAGE_LAT_SPACING / 4),
      ]
      const clearance = (offset: number) => Math.min(
        ...latsFor(offset).map(
          (lat) => Math.min(...prevLats.map((p) => Math.abs(lat - p))),
        ),
      )
      stagger = candidates[0]
      if (prevLats.length > 0
        && clearance(candidates[1]) > clearance(candidates[0])) {
        stagger = candidates[1]
      }
    }
    const lats = latsFor(stagger)
    row.forEach((n, j) => {
      const lat = lats[j]
      // Amphitheater bow: edge seats wrap toward the camera (inward).
      const bow = Math.max(-9, -0.01 * lat * lat)
      slots.push({
        slug: n.slug,
        x: cx + outX * (back + bow) + tx * lat,
        y,
        z: cz + outZ * (back + bow) + tz * lat,
      })
      halfWidth = Math.max(halfWidth, Math.abs(lat) + STAGE_LAT_SPACING / 2)
    })
    prevLats = lats
  })
  return {
    slots,
    halfWidth,
    rows: rows.length,
    depth: STAGE_FRONT_BACK + Math.max(0, rows.length - 1) * STAGE_ROW_STEP,
  }
}

// --- environment scatter (round 10: the night world) ------------------------

export interface EnvZone {
  x: number
  z: number
  r: number
}

export interface EnvPlacement {
  x: number
  z: number
  /** Uniform scale jitter. */
  scale: number
  /** Y rotation, radians. */
  rot: number
}

/** Deterministic 0..1 from an integer key — env scatter must never reshuffle
 * between renders (same discipline as the per-slug jitter above). */
function hash01(i: number, salt: number): number {
  let h = (2166136261 ^ salt) >>> 0
  h = Math.imul(h ^ i, 16777619)
  h = Math.imul(h ^ (h >>> 13), 16777619)
  return ((h >>> 0) % 100000) / 100000
}

/** Terrain height at a world point: gentle sine-noise hills, flattened to 0
 * inside every clearing (ramp over 22 units past its radius). Shared by the
 * terrain mesh displacement and the tuft placement so everything sits ON
 * the ground. */
export function terrainHeightAt(
  x: number, z: number, clearings: EnvZone[],
): number {
  let h = (
    Math.sin(x * 0.045 + z * 0.03)
    + Math.sin(x * 0.021 - z * 0.052) * 0.7
    + Math.sin((x + z) * 0.013) * 1.2
  ) * 0.55
  let flatten = 1
  for (const f of clearings) {
    const d = Math.hypot(x - f.x, z - f.z)
    flatten = Math.min(flatten, Math.max(0, Math.min(1, (d - f.r) / 22)))
  }
  return h * flatten
}

/** Wind-tuft scatter: deterministic points across the clearings (the count
 * splits by clearing area), skipping the slabs and the lake. */
export function computeGrassScatter(
  clearings: EnvZone[],
  exclusions: EnvZone[],
  total = 2400,
): EnvPlacement[] {
  const out: EnvPlacement[] = []
  const area = clearings.reduce((s, c) => s + c.r * c.r, 0)
  if (area <= 0) return out
  let key = 0
  for (const c of clearings) {
    const n = Math.round((total * c.r * c.r) / area)
    for (let i = 0; i < n; i++) {
      key += 1
      const r = c.r * Math.sqrt(hash01(key, 11)) * 0.96
      const a = hash01(key, 23) * Math.PI * 2
      const x = c.x + Math.cos(a) * r
      const z = c.z + Math.sin(a) * r
      if (exclusions.some((e) => Math.hypot(x - e.x, z - e.z) < e.r)) continue
      out.push({
        x, z,
        scale: 0.7 + hash01(key, 37) * 0.6,
        rot: hash01(key, 51) * Math.PI,
      })
    }
  }
  return out
}

/** Heat 0..1 per agent from the 7-day counters (log-scaled against the
 * hottest visible agent so one busy agent doesn't flatten everyone else).
 * The renderer maps this to card glow intensity (and the particle density
 * while streaming) — the PresenceHalo activity grammar. */
export function computeHeat(
  activity: { name: string; messages_7d: number; task_runs_7d: number }[],
): Map<string, number> {
  const raw = new Map<string, number>()
  for (const a of activity) {
    raw.set(a.name, Math.log1p(a.messages_7d + a.task_runs_7d * 3))
  }
  const max = Math.max(...raw.values(), 0)
  const out = new Map<string, number>()
  for (const [name, v] of raw) out.set(name, max > 0 ? v / max : 0)
  return out
}
