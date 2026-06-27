import { describe, it, expect } from 'vitest'
import {
  type VisibilityMode,
  modeOf,
  columnsOf,
  modeOfAgent,
  availableScopes,
  hasUserScope,
  hasAgentScope,
  isCollaborative,
  isSharedOnly,
  isPersonalOnly,
  showsUserMemory,
  showsAgentMemory,
  MODE_LABEL,
  MODE_SUMMARY,
  MODE_OPTION_HINT,
  MODE_GROUPS,
} from '@/lib/visibility'

const ALL_MODES: VisibilityMode[] = ['personal_shared', 'shared_personal', 'personal_only', 'shared_only']

describe('modeOf — the 2×2', () => {
  it('maps each (collaborative, scope) pair to the right mode', () => {
    expect(modeOf(true, 'user')).toBe('personal_shared')
    expect(modeOf(true, 'agent')).toBe('shared_personal')
    expect(modeOf(false, 'user')).toBe('personal_only')
    expect(modeOf(false, 'agent')).toBe('shared_only')
  })
})

describe('columnsOf — round-trips with modeOf', () => {
  it('inverts modeOf for every mode', () => {
    for (const mode of ALL_MODES) {
      const cols = columnsOf(mode)
      expect(modeOf(cols.collaborative, cols.default_scope)).toBe(mode)
    }
  })

  it('produces the documented columns', () => {
    expect(columnsOf('personal_shared')).toEqual({ collaborative: true, default_scope: 'user' })
    expect(columnsOf('shared_personal')).toEqual({ collaborative: true, default_scope: 'agent' })
    expect(columnsOf('personal_only')).toEqual({ collaborative: false, default_scope: 'user' })
    expect(columnsOf('shared_only')).toEqual({ collaborative: false, default_scope: 'agent' })
  })
})

describe('modeOfAgent — soft defaults', () => {
  it('reads explicit columns', () => {
    expect(modeOfAgent({ collaborative: false, default_scope: 'agent' })).toBe('shared_only')
    expect(modeOfAgent({ collaborative: true, default_scope: 'agent' })).toBe('shared_personal')
  })

  it('soft-falls to Personal + shared when columns are missing', () => {
    expect(modeOfAgent(null)).toBe('personal_shared')
    expect(modeOfAgent(undefined)).toBe('personal_shared')
    expect(modeOfAgent({})).toBe('personal_shared')
    // collaborative present but scope missing → user default
    expect(modeOfAgent({ collaborative: false })).toBe('personal_only')
  })
})

describe('availableScopes + scope predicates', () => {
  it('collaborative modes offer both scopes', () => {
    expect(availableScopes('personal_shared')).toEqual(['user', 'agent'])
    expect(availableScopes('shared_personal')).toEqual(['user', 'agent'])
  })

  it('private modes offer exactly one scope', () => {
    expect(availableScopes('personal_only')).toEqual(['user'])
    expect(availableScopes('shared_only')).toEqual(['agent'])
  })

  it('hasUserScope / hasAgentScope agree with availableScopes', () => {
    for (const mode of ALL_MODES) {
      expect(hasUserScope(mode)).toBe(availableScopes(mode).includes('user'))
      expect(hasAgentScope(mode)).toBe(availableScopes(mode).includes('agent'))
    }
  })

  it('classifies collaborative / shared-only / personal-only', () => {
    expect(isCollaborative('personal_shared')).toBe(true)
    expect(isCollaborative('shared_personal')).toBe(true)
    expect(isCollaborative('personal_only')).toBe(false)
    expect(isCollaborative('shared_only')).toBe(false)
    expect(isSharedOnly('shared_only')).toBe(true)
    expect(isSharedOnly('personal_only')).toBe(false)
    expect(isPersonalOnly('personal_only')).toBe(true)
    expect(isPersonalOnly('shared_only')).toBe(false)
  })
})

describe('memory-row gating mirrors scope availability', () => {
  it('shows a memory row only when the mode offers that scope', () => {
    // Personal only → user memory only; Shared only → agent memory only.
    expect(showsUserMemory('personal_only')).toBe(true)
    expect(showsAgentMemory('personal_only')).toBe(false)
    expect(showsUserMemory('shared_only')).toBe(false)
    expect(showsAgentMemory('shared_only')).toBe(true)
    // Collaborative → both rows.
    expect(showsUserMemory('personal_shared')).toBe(true)
    expect(showsAgentMemory('personal_shared')).toBe(true)
  })
})

describe('UI metadata is complete', () => {
  it('has a label, summary, and option hint for every mode', () => {
    for (const mode of ALL_MODES) {
      expect(MODE_LABEL[mode]).toBeTruthy()
      expect(MODE_SUMMARY[mode]).toBeTruthy()
      expect(MODE_OPTION_HINT[mode]).toBeTruthy()
    }
  })

  it('the two groups cover all four modes exactly once', () => {
    const grouped = MODE_GROUPS.flatMap(g => g.modes)
    expect(grouped.sort()).toEqual([...ALL_MODES].sort())
    expect(MODE_GROUPS).toHaveLength(2)
  })
})
