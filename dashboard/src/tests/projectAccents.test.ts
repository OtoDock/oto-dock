import { describe, it, expect } from 'vitest'

import { rowAccentClass } from '@/components/chat/projectAccents'
import type { Chat } from '@/api/chats'

function chat(over: Partial<Chat>): Chat {
  return {
    id: 'c1', user_sub: 'u', agent: 'a', title: '', session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: '2026-07-08T10:00:00Z', updated_at: '2026-07-06T11:00:00Z',
    ...over,
  } as Chat
}

describe('projectAccents', () => {
  it('a plain chat gets the transparent edge', () => {
    expect(rowAccentClass(chat({}))).toBe('border-l-2 border-l-transparent')
    expect(rowAccentClass(chat({ project_id: 'p1' }))).toBe('border-l-2 border-l-transparent')
  })

  it('every delegated worker gets the agent accent at half strength', () => {
    for (const c of [chat({ origin: 'delegated' }), chat({ delegate_role: 'worker' })]) {
      expect(rowAccentClass(c)).toBe('border-l-2 border-l-accent-agent-solid/50')
    }
  })

  it('an orchestrator gets the full accent on a wider edge, and never a row background', () => {
    const c = chat({ delegate_role: 'orchestrator' })
    const cls = rowAccentClass(c)
    expect(cls).toBe('border-l-[3px] border-l-accent-agent-solid')
    expect(cls).not.toContain('bg-')
  })

  it('the orchestrator reads stronger than its workers in the same family', () => {
    const orchestrator = rowAccentClass(chat({ delegate_role: 'orchestrator' }))
    const worker = rowAccentClass(chat({ origin: 'delegated' }))
    expect(orchestrator).toContain('accent-agent')
    expect(worker).toContain('accent-agent')
    expect(worker).toContain('/50')
    expect(orchestrator).not.toContain('/')
  })

  it('a quiet chat keeps its accent: the row time says the age', () => {
    expect(rowAccentClass(chat({ origin: 'delegated', updated_at: '2026-01-01T00:00:00Z' })))
      .toBe('border-l-2 border-l-accent-agent-solid/50')
  })

  it('the active chat shows no accent: the selection bar is the only stripe', () => {
    const c = chat({ delegate_role: 'orchestrator' })
    expect(rowAccentClass(c, { active: true })).toBe('border-l-2 border-l-transparent')
  })
})
