import { describe, it, expect } from 'vitest'

import { computeModelGroups, pickSeedModel, visibleAgentPaths } from '@/lib/modelGroups'

// ─── Model-dropdown layer visibility (the chat page's engine rules) ─────────
// Locked to the chat's engine while its session process is alive; a DEAD
// chat's dropdown adds the agent's other USER-ACCESSIBLE engines (the
// cross-engine switch flow); a fresh chat filters to accessible engines with
// an unfiltered fallback when the user can run none of them.

const LAYERS = {
  'claude-code-cli': {
    display_name: 'Claude Code CLI',
    models: [
      { value: '', label: 'System Default' },
      { value: 'claude-sonnet-5', label: 'Sonnet 5' },
    ],
  },
  'codex-cli': {
    display_name: 'OpenAI Codex',
    models: [{ value: 'gpt-5', label: 'GPT-5' }],
  },
  'direct-llm': {
    display_name: 'Direct LLM API',
    models: [{ value: 'claude-haiku-4-5', label: 'Haiku 4.5' }],
  },
}
const ALL = ['claude-code-cli', 'codex-cli', 'direct-llm']

const base = {
  layers: LAYERS,
  agentPaths: ALL,
  chatActiveLayer: null as string | null,
  processAlive: true,
  canRun: {} as Record<string, boolean>,
  streaming: false,
  warming: false,
}

const layersOf = (args: typeof base) =>
  (computeModelGroups(args) ?? []).map(g => g.layer)

describe('visibleAgentPaths', () => {
  it('filters to what the user can run', () => {
    expect(visibleAgentPaths(ALL, { 'codex-cli': false }))
      .toEqual(['claude-code-cli', 'direct-llm'])
  })

  it('absent layers count as runnable (older proxy without can_run)', () => {
    expect(visibleAgentPaths(ALL, {})).toEqual(ALL)
  })

  it('zero accessible → unfiltered fallback (never an empty selector)', () => {
    const none = { 'claude-code-cli': false, 'codex-cli': false, 'direct-llm': false }
    expect(visibleAgentPaths(ALL, none)).toEqual(ALL)
  })
})

describe('computeModelGroups', () => {
  it('fresh chat: accessible engines only, System Default rows dropped', () => {
    const groups = computeModelGroups({ ...base, canRun: { 'codex-cli': false } })!
    expect(groups.map(g => g.layer)).toEqual(['claude-code-cli', 'direct-llm'])
    expect(groups[0].models).toEqual([
      { value: 'claude-code-cli::claude-sonnet-5', label: 'Sonnet 5' },
    ])
  })

  it('committed + alive: locked to the single chat layer', () => {
    expect(layersOf({ ...base, chatActiveLayer: 'codex-cli' }))
      .toEqual(['codex-cli'])
  })

  it('committed + dead: chat layer FIRST, then other accessible engines', () => {
    expect(layersOf({
      ...base, chatActiveLayer: 'codex-cli', processAlive: false,
    })).toEqual(['codex-cli', 'claude-code-cli', 'direct-llm'])
  })

  it('committed + dead: inaccessible engines stay hidden from the expansion', () => {
    expect(layersOf({
      ...base, chatActiveLayer: 'codex-cli', processAlive: false,
      canRun: { 'claude-code-cli': false },
    })).toEqual(['codex-cli', 'direct-llm'])
  })

  it("the chat's own engine shows even when the viewer can't run it", () => {
    // A shared chat on an engine the viewer lacks: hiding the committed
    // layer would be misleading — it stays, locked or first.
    expect(layersOf({
      ...base, chatActiveLayer: 'codex-cli', processAlive: false,
      canRun: { 'codex-cli': false },
    })).toEqual(['codex-cli', 'claude-code-cli', 'direct-llm'])
  })

  it('streaming or warming re-locks a dead-believed chat', () => {
    expect(layersOf({
      ...base, chatActiveLayer: 'codex-cli', processAlive: false, streaming: true,
    })).toEqual(['codex-cli'])
    expect(layersOf({
      ...base, chatActiveLayer: 'codex-cli', processAlive: false, warming: true,
    })).toEqual(['codex-cli'])
  })

  it('orders a group by capability tier and carries the tier fields', () => {
    const layers = {
      'claude-code-cli': {
        display_name: 'Claude Code CLI',
        models: [
          { value: 'local-x', label: 'Local X' },
          { value: 'claude-sonnet-5', label: 'Sonnet 5', tier: 3, tier_label: 'balanced', good_at: 'everyday coding' },
          { value: 'claude-fable-5-1', label: 'Fable 5.1', tier: 1, tier_label: 'frontier', good_at: 'judgement' },
        ],
      },
    }
    const groups = computeModelGroups({ ...base, layers, agentPaths: ['claude-code-cli'] })!
    expect(groups[0].models.map(m => m.value)).toEqual([
      'claude-code-cli::claude-fable-5-1', 'claude-code-cli::claude-sonnet-5', 'claude-code-cli::local-x',
    ])
    // The label stays bare: the dropdown draws the tier as the four-dot
    // mark and puts the word in the tooltip.
    expect(groups[0].models[0]).toEqual({
      value: 'claude-code-cli::claude-fable-5-1', label: 'Fable 5.1',
      tier: 1, tierLabel: 'frontier', goodAt: 'judgement',
    })
    // Untiered rows keep a bare label and no tier fields.
    expect(groups[0].models[2]).toEqual({ value: 'claude-code-cli::local-x', label: 'Local X' })
  })

  it('returns undefined while the catalog is loading or empty', () => {
    expect(computeModelGroups({ ...base, layers: undefined })).toBeUndefined()
    expect(computeModelGroups({ ...base, agentPaths: [] })).toBeUndefined()
  })

  // 1.5: task chats flow through these same rules (the FE lock is
  // interactive-only now) — a chat whose CURRENT model was retired from
  // the catalog still needs its check row.
  it('prepends an unlisted active model to the chat layer group only', () => {
    const groups = computeModelGroups({
      ...base, chatActiveLayer: 'claude-code-cli', processAlive: false,
      activeModel: 'claude-opus-4-6',
    })!
    const claude = groups.find(g => g.layer === 'claude-code-cli')!
    expect(claude.models[0]).toEqual({
      value: 'claude-code-cli::claude-opus-4-6', label: 'claude-opus-4-6',
    })
    // Foreign groups untouched; a served model is NOT duplicated.
    const codex = groups.find(g => g.layer === 'codex-cli')!
    expect(codex.models.some(m => m.label === 'claude-opus-4-6')).toBe(false)
    const served = computeModelGroups({
      ...base, chatActiveLayer: 'claude-code-cli',
      activeModel: 'claude-sonnet-5',
    })!
    expect(served[0].models.filter(m => m.value.endsWith('claude-sonnet-5')))
      .toHaveLength(1)
  })
})

// ─── The model a new chat opens on when its seed is not offered ────────────
// A sticky pick or an agent default can name a retired id (the browser keeps
// its own choice outside any boot walk): it opens on the model that id runs
// as today, else the agent's default, else the engine's own default — never
// the first row, which is the tier-ordered list's most expensive model.

describe('pickSeedModel', () => {
  const catalog = {
    'engine-a': {
      display_name: 'Engine A',
      models: [
        { value: 'frontier-2', label: 'Frontier', tier: 1 },
        { value: 'strong-2', label: 'Strong', tier: 2 },
        { value: 'balanced-2', label: 'Balanced', tier: 3 },
      ],
      auto_model: 'strong-2',
      model_successors: { 'strong-1': 'strong-2', 'gone-1': 'not-offered' },
    },
    'engine-b': {
      display_name: 'Engine B',
      models: [{ value: 'b-1', label: 'B one' }],
      auto_model: 'b-1',
    },
  }
  const groups = computeModelGroups({
    layers: catalog, agentPaths: ['engine-a', 'engine-b'], chatActiveLayer: null,
    processAlive: true, canRun: {}, streaming: false, warming: false,
  })!

  it('keeps an offered pick on its own engine', () => {
    expect(pickSeedModel(groups, catalog, ['b-1', 'strong-2'])).toEqual({ layer: 'engine-b', model: 'b-1' })
  })

  it('maps a retired sticky pick to the model it runs as today', () => {
    expect(pickSeedModel(groups, catalog, ['strong-1', 'balanced-2'])).toEqual({ layer: 'engine-a', model: 'strong-2' })
  })

  it('falls to the agent default when the pick is not offered at all', () => {
    expect(pickSeedModel(groups, catalog, ['someone-elses', 'balanced-2'])).toEqual({ layer: 'engine-a', model: 'balanced-2' })
  })

  it('falls to the engine default, never the first (most expensive) row', () => {
    expect(pickSeedModel(groups, catalog, ['gone-1', ''])).toEqual({ layer: 'engine-a', model: 'strong-2' })
  })

  it('takes the first row only when no engine offers its own default', () => {
    const bare = { 'engine-a': { ...catalog['engine-a'], auto_model: 'not-offered' } }
    const g = computeModelGroups({
      layers: bare, agentPaths: ['engine-a'], chatActiveLayer: null,
      processAlive: true, canRun: {}, streaming: false, warming: false,
    })!
    expect(pickSeedModel(g, bare, [''])).toEqual({ layer: 'engine-a', model: 'frontier-2' })
    expect(pickSeedModel([], bare, ['x'])).toBeNull()
  })
})
