/**
 * Pure layout math for the 3D agents map (components/agents-map/layout.ts):
 * overview scatter placement (level rank steps outward), ring slots,
 * grayed-ness via accessible flags AND the admin memberOf override, the
 * hide toggle, the stage amphitheater (computeStageLayout) and heat
 * normalization — everything testable without WebGL.
 */
import { describe, expect, it } from 'vitest'
import {
  computeGrassScatter,
  computeHeat,
  computeMapLayout,
  computeStageLayout,
  terrainHeightAt,
  RING_RADIUS,
} from '../components/agents-map/layout'
import type { AgentSummary } from '../api/agents'
import type { Department } from '../api/departments'

const agent = (
  name: string, dept = '', level = '',
  overrides: Partial<AgentSummary> = {},
): AgentSummary => ({
  name,
  display_name: name,
  admin_only: false,
  execution_path: 'claude-code-cli',
  execution_paths: ['claude-code-cli'],
  execution_target: 'local',
  collaborative: true,
  default_model: '',
  default_scope: 'user',
  color: '',
  description: '',
  mcp_count: 0,
  mcp_names: [],
  schedule_count: 0,
  trigger_count: 0,
  has_workspace: false,
  department_id: dept,
  department_level_id: level,
  ...overrides,
})

const dept = (
  id: string,
  name: string,
  members: { name: string; level_id: string; accessible?: boolean }[],
): Department => ({
  id,
  name,
  created_by_sub: '',
  auto_delegation: true,
  reach: 'adjacent',
  position_hint: '',
  levels: [
    { id: `${id}-l0`, rank: 0, name: 'Head' },
    { id: `${id}-l1`, rank: 1, name: 'Team' },
  ],
  members: members.map((m) => ({
    name: m.name,
    display_name: m.name,
    color: '',
    description: '',
    level_id: m.level_id,
    accessible: m.accessible ?? true,
  })),
  can_edit: false,
})

describe('computeMapLayout', () => {
  it('steps deeper ranks outward and carries level names', () => {
    const d = dept('d1', 'Eng', [
      { name: 'head', level_id: 'd1-l0' },
      { name: 'peer', level_id: 'd1-l1' },
      { name: 'ghost', level_id: 'd1-l1', accessible: false },
    ])
    // 'ghost' is NOT in the accessible agents list (server scoping).
    const layout = computeMapLayout(
      [agent('head', 'd1', 'd1-l0'), agent('peer', 'd1', 'd1-l1')],
      [d],
    )
    const bySlug = Object.fromEntries(layout.nodes.map((n) => [n.slug, n]))
    expect(bySlug.head.levelName).toBe('Head')
    expect(bySlug.peer.levelName).toBe('Team')
    expect(bySlug.ghost.grayed).toBe(true)
    expect(bySlug.head.grayed).toBe(false)
    // Mode label rides the node (the stage card's second line, Grid
    // wording); a grayed dept-mate outside the agents payload has NONE —
    // modeOfAgent(undefined) would soft-default and mislabel it.
    expect(bySlug.head.modeLabel).toBe('Personal + shared')
    expect(bySlug.ghost.modeLabel).toBeUndefined()
    // Deeper ranks sit further from the anchor (the scatter grows outward).
    const cluster = layout.clusters[0]
    const dist = (n: { x: number; z: number }) =>
      Math.hypot(n.x - cluster.cx, n.z - cluster.cz)
    expect(dist(bySlug.peer)).toBeGreaterThan(dist(bySlug.head))
    expect(cluster.extent).toBeGreaterThan(0)
    expect(cluster.accent).toMatch(/^#/)
    // Clusters live on the ring, not the origin.
    expect(Math.hypot(cluster.cx, cluster.cz)).toBeCloseTo(RING_RADIUS)
  })

  it('maps visibility modes to the Grid wording per node', () => {
    const layout = computeMapLayout(
      [
        agent('shared-only', '', '', { collaborative: true, default_scope: 'agent' }),
        agent('personal-only', '', '', { collaborative: false, default_scope: 'user' }),
      ],
      [],
    )
    const bySlug = Object.fromEntries(layout.nodes.map((n) => [n.slug, n]))
    expect(bySlug['shared-only'].modeLabel).toBe('Shared + personal')
    expect(bySlug['personal-only'].modeLabel).toBe('Personal only')
  })

  it('centers department-less installations on the floor, unlabeled', () => {
    const layout = computeMapLayout(
      [agent('solo'), agent('solo2')], [],
    )
    expect(layout.clusters).toHaveLength(1)
    const cluster = layout.clusters[0]
    expect(cluster.departmentId).toBe('')
    // The whole company IS this cluster: it sits at the CENTER of the
    // floor (the round-3 fix — never a clump at one ring edge), and a
    // lone scatter carries no "Independent" label.
    expect(cluster.cx).toBe(0)
    expect(cluster.cz).toBe(0)
    expect(cluster.name).toBe('')
    const bySlug = Object.fromEntries(layout.nodes.map((n) => [n.slug, n]))
    expect(bySlug.solo.levelRank).toBe(-1)
    expect(bySlug.solo.levelName).toBe('')
    // The spiral starts at r=c, never the exact origin: an agent at (0,0)
    // is where the round-3 camera tween could never converge.
    for (const n of layout.nodes) {
      expect(Math.hypot(n.x, n.z)).toBeGreaterThan(5)
    }
  })

  it('keeps the labeled ring-slot standalone cluster when departments exist', () => {
    const d = dept('d1', 'Eng', [{ name: 'a1', level_id: 'd1-l0' }])
    const layout = computeMapLayout(
      [agent('a1', 'd1', 'd1-l0'), agent('solo')], [d],
    )
    const standalone = layout.clusters.find((c) => c.departmentId === '')!
    expect(standalone.name).toBe('Independent')
    expect(Math.hypot(standalone.cx, standalone.cz)).toBeGreaterThan(1)
  })

  it('fans ring-slot independents SIDEWAYS so billboard cards never eclipse', () => {
    // The old world-axis golden spiral could line independents up along the
    // camera's depth axis (three independents left one card fully hidden
    // behind another). Ring slots now fan in the cluster's outward/tangent
    // basis like department tiers: same-row seats keep real LATERAL
    // separation from the paged camera's point of view.
    const d = dept('d1', 'Eng', [{ name: 'a1', level_id: 'd1-l0' }])
    const layout = computeMapLayout(
      [
        agent('a1', 'd1', 'd1-l0'),
        agent('solo-a'), agent('solo-b'), agent('solo-c'),
      ],
      [d],
    )
    const cluster = layout.clusters.find((c) => c.departmentId === '')!
    const tx = -cluster.outZ
    const tz = cluster.outX
    const lats = layout.nodes
      .filter((n) => n.departmentId === '')
      .map((n) => (n.x - cluster.cx) * tx + (n.z - cluster.cz) * tz)
      .sort((a, b) => a - b)
    expect(lats).toHaveLength(3)
    for (let i = 1; i < lats.length; i++) {
      expect(lats[i] - lats[i - 1]).toBeGreaterThan(5)
    }
  })

  it('skips members on dangling levels (compiler-consistent)', () => {
    const d = dept('d1', 'Eng', [{ name: 'lost', level_id: 'nope' }])
    const layout = computeMapLayout([], [d])
    expect(layout.nodes).toHaveLength(0)
    expect(layout.clusters).toHaveLength(1) // the (empty) dept still shows
  })

  it('memberOf overrides graying for admin viewers', () => {
    // Admin fetched ?all=true: the list contains a non-member agent and the
    // dept feed flags everything accessible (admin bypass) — memberOf is
    // what decides grayed-ness.
    const d = dept('d1', 'Eng', [
      { name: 'mine', level_id: 'd1-l0', accessible: true },
      { name: 'theirs', level_id: 'd1-l1', accessible: true },
    ])
    const layout = computeMapLayout(
      [agent('mine', 'd1', 'd1-l0'), agent('theirs', 'd1', 'd1-l1'),
        agent('lone-other')],
      [d],
      { memberOf: new Set(['mine']) },
    )
    const bySlug = Object.fromEntries(layout.nodes.map((n) => [n.slug, n]))
    expect(bySlug.mine.grayed).toBe(false)
    expect(bySlug.theirs.grayed).toBe(true)
    expect(bySlug['lone-other'].grayed).toBe(true) // standalone non-member
  })

  it('hideNonMember drops departments with no member agent', () => {
    const mine = dept('d1', 'Mine', [{ name: 'me', level_id: 'd1-l0' }])
    const other = dept('d2', 'Other', [
      { name: 'them', level_id: 'd2-l0', accessible: false },
    ])
    const layout = computeMapLayout(
      [agent('me', 'd1', 'd1-l0')],
      [mine, other],
      { hideNonMember: true },
    )
    expect(layout.clusters.map((c) => c.departmentId)).toEqual(['d1'])
    expect(layout.nodes.map((n) => n.slug)).toEqual(['me'])
  })

  it('spreads clusters on distinct ring slots', () => {
    const d1 = dept('d1', 'A', [{ name: 'a1', level_id: 'd1-l0' }])
    const d2 = dept('d2', 'B', [{ name: 'b1', level_id: 'd2-l0' }])
    const layout = computeMapLayout(
      [agent('a1', 'd1', 'd1-l0'), agent('b1', 'd2', 'd2-l0'), agent('solo')],
      [d1, d2],
    )
    expect(layout.clusters).toHaveLength(3)
    const centers = layout.clusters.map((c) => `${c.cx.toFixed(1)},${c.cz.toFixed(1)}`)
    expect(new Set(centers).size).toBe(3)
    // Distinct hub accents for the two departments.
    expect(layout.clusters[0].accent).not.toBe(layout.clusters[1].accent)
    // Independents take the LAST ring slot — the last stage page.
    expect(layout.clusters[layout.clusters.length - 1].departmentId).toBe('')
  })

  it('is deterministic (same inputs, same positions)', () => {
    const d = dept('d1', 'Eng', [
      { name: 'a', level_id: 'd1-l0' },
      { name: 'b', level_id: 'd1-l1' },
    ])
    const agents = [agent('a', 'd1', 'd1-l0'), agent('b', 'd1', 'd1-l1')]
    const one = computeMapLayout(agents, [d])
    const two = computeMapLayout(agents, [d])
    expect(one).toEqual(two)
  })
})

describe('computeStageLayout', () => {
  const stageFixture = (memberNames: { head: string[]; team: string[] }) => {
    const d = dept('d1', 'Eng', [
      ...memberNames.head.map((name) => ({ name, level_id: 'd1-l0' })),
      ...memberNames.team.map((name) => ({ name, level_id: 'd1-l1' })),
    ])
    const agents = [
      ...memberNames.head.map((n) => agent(n, 'd1', 'd1-l0')),
      ...memberNames.team.map((n) => agent(n, 'd1', 'd1-l1')),
    ]
    const layout = computeMapLayout(agents, [d])
    const cluster = layout.clusters[0]
    const members = layout.nodes.filter((n) => n.departmentId === 'd1')
    return { cluster, arc: computeStageLayout(cluster, members) }
  }

  it('puts the Head row FRONT and closest, deeper ranks behind and higher', () => {
    const { cluster, arc } = stageFixture({
      head: ['boss'], team: ['w1', 'w2'],
    })
    const slot = Object.fromEntries(arc.slots.map((s) => [s.slug, s]))
    // "Front" = smaller offset along the outward axis; the camera sits on
    // the inward side looking out, so the front row is the closest — the
    // top role reads BIGGEST (round 7 fixed the inverted perspective).
    const along = (s: { x: number; z: number }) =>
      (s.x - cluster.cx) * cluster.outX + (s.z - cluster.cz) * cluster.outZ
    expect(along(slot.boss)).toBeLessThan(along(slot.w1))
    expect(slot.boss.y).toBeLessThan(slot.w1.y) // rear rows rise to stay visible
    // Row mates share height.
    expect(slot.w1.y).toBe(slot.w2.y)
    expect(arc.rows).toBe(2)
    expect(arc.depth).toBeGreaterThan(0)
  })

  it('respects the screen-driven row cap (2 per row on phones)', () => {
    const d = dept('d1', 'Eng', [
      { name: 'boss', level_id: 'd1-l0' },
      { name: 'w1', level_id: 'd1-l1' },
      { name: 'w2', level_id: 'd1-l1' },
      { name: 'w3', level_id: 'd1-l1' },
    ])
    const agents = [
      agent('boss', 'd1', 'd1-l0'),
      agent('w1', 'd1', 'd1-l1'), agent('w2', 'd1', 'd1-l1'),
      agent('w3', 'd1', 'd1-l1'),
    ]
    const layout = computeMapLayout(agents, [d])
    const cluster = layout.clusters[0]
    const members = layout.nodes.filter((n) => n.departmentId === 'd1')
    const arc = computeStageLayout(cluster, members, { rowCap: 2 })
    // 1 head row + the 3-member tier wrapped into 2 rows of ≤2.
    expect(arc.rows).toBe(3)
    expect(arc.slots).toHaveLength(4)
  })

  it('wraps oversized tiers into balanced sub-rows', () => {
    const team = Array.from({ length: 10 }, (_, i) => `agent-${i}`)
    const { arc } = stageFixture({ head: ['boss'], team })
    // 10 team members > the 8-seat row cap → two balanced team rows.
    expect(arc.rows).toBe(3)
    expect(arc.slots).toHaveLength(11)
    // Every slot is far enough from its row mates to fit a card.
    const heights = new Set(arc.slots.map((s) => s.y.toFixed(2)))
    expect(heights.size).toBe(3)
    expect(arc.halfWidth).toBeGreaterThan(8)
  })

  it('is deterministic and alphabetical within a tier', () => {
    const a = stageFixture({ head: ['boss'], team: ['zeta', 'alpha'] })
    const b = stageFixture({ head: ['boss'], team: ['zeta', 'alpha'] })
    expect(a.arc).toEqual(b.arc)
    const teamSlots = a.arc.slots.filter((s) => s.slug !== 'boss')
    expect(teamSlots.map((s) => s.slug)).toEqual(['alpha', 'zeta'])
  })

  it('keeps a lone rear seat laterally CLEAR of the front row (phone cap)', () => {
    // The round-12 fixed stagger left a single rear seat 4 units from a
    // front card on the 2-seat phone cap — visually hidden behind it (the
    // Independent stage bug). The clearance-checked stagger must keep
    // every rear seat at least half a pitch from every front seat.
    const d = dept('d1', 'Ind', [
      { name: 'home-assistant', level_id: 'd1-l0' },
      { name: 'jarvis', level_id: 'd1-l0' },
      { name: 'personal-assistant', level_id: 'd1-l0' },
    ])
    const agents = [
      agent('home-assistant', 'd1', 'd1-l0'),
      agent('jarvis', 'd1', 'd1-l0'),
      agent('personal-assistant', 'd1', 'd1-l0'),
    ]
    const layout = computeMapLayout(agents, [d])
    const cluster = layout.clusters[0]
    const members = layout.nodes.filter((n) => n.departmentId === 'd1')
    const arc = computeStageLayout(cluster, members, { rowCap: 2 })
    expect(arc.rows).toBe(2)
    const tx = -cluster.outZ
    const tz = cluster.outX
    const latOf = (s: { x: number; z: number }) =>
      (s.x - cluster.cx) * tx + (s.z - cluster.cz) * tz
    const byHeight = new Map<string, number[]>()
    for (const s of arc.slots) {
      const k = s.y.toFixed(2)
      byHeight.set(k, [...(byHeight.get(k) ?? []), latOf(s)])
    }
    const [front, rear] = [...byHeight.entries()]
      .sort(([a1], [b1]) => Number(a1) - Number(b1))
      .map(([, lats]) => lats)
    expect(front).toHaveLength(2)
    expect(rear).toHaveLength(1)
    for (const f of front) {
      expect(Math.abs(rear[0] - f)).toBeGreaterThanOrEqual(8)
    }
  })
})

describe('environment scatter (the night world)', () => {
  const clearings = [
    { x: 0, z: -RING_RADIUS, r: 60 },
    { x: 0, z: RING_RADIUS, r: 50 },
  ]

  it('terrainHeightAt is flat inside clearings and hilly outside', () => {
    // Dead center of a clearing: the flatten ramp forces 0 (±-0).
    expect(terrainHeightAt(0, -RING_RADIUS, clearings)).toBeCloseTo(0, 10)
    // Far outside every clearing the sine hills are alive somewhere.
    const rim = Array.from({ length: 24 }, (_, i) => {
      const a = (i / 24) * Math.PI * 2
      return Math.abs(
        terrainHeightAt(Math.cos(a) * 150, Math.sin(a) * 150, clearings),
      )
    })
    expect(Math.max(...rim)).toBeGreaterThan(0.3)
  })

  it('grass scatter stays in the clearings and out of the exclusions', () => {
    const exclusions = [
      { x: 0, z: -RING_RADIUS, r: 20 },
      { x: 0, z: RING_RADIUS + 20, r: 24 },
    ]
    const tufts = computeGrassScatter(clearings, exclusions, 800)
    expect(tufts.length).toBeGreaterThan(400) // exclusions thin, never empty
    for (const t of tufts) {
      expect(clearings.some(
        (c) => Math.hypot(t.x - c.x, t.z - c.z) <= c.r,
      )).toBe(true)
      for (const e of exclusions) {
        expect(Math.hypot(t.x - e.x, t.z - e.z)).toBeGreaterThanOrEqual(e.r)
      }
      expect(t.scale).toBeGreaterThan(0.5)
      expect(t.scale).toBeLessThan(1.5)
    }
    // Deterministic — the meadow never reshuffles between renders.
    expect(computeGrassScatter(clearings, exclusions, 800)).toEqual(tufts)
  })
})

describe('computeHeat', () => {
  it('normalizes to the hottest agent with log scaling', () => {
    const heat = computeHeat([
      { name: 'hot', messages_7d: 400, task_runs_7d: 10 },
      { name: 'warm', messages_7d: 40, task_runs_7d: 0 },
      { name: 'cold', messages_7d: 0, task_runs_7d: 0 },
    ])
    expect(heat.get('hot')).toBe(1)
    expect(heat.get('cold')).toBe(0)
    const warm = heat.get('warm')!
    expect(warm).toBeGreaterThan(0.3) // log scale keeps mid-activity visible
    expect(warm).toBeLessThan(1)
  })

  it('handles the empty installation', () => {
    expect(computeHeat([]).size).toBe(0)
  })
})
