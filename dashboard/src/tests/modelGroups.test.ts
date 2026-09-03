import { describe, it, expect } from 'vitest'

import { computeModelGroups, visibleAgentPaths } from '@/lib/modelGroups'

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
