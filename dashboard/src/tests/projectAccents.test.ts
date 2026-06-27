import { describe, it, expect } from 'vitest'

import { rowAccentClass } from '@/components/chat/projectAccents'
import type { Chat } from '@/api/chats'

const NOW = Date.parse('2026-07-08T12:00:00Z')

function chat(over: Partial<Chat>): Chat {
  return {
    id: 'c1', user_sub: 'u', agent: 'a', title: '', session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: '2026-07-08T10:00:00Z', updated_at: '2026-07-08T11:00:00Z',
    ...over,
  } as Chat
}

describe('projectAccents', () => {
  it('no accent for a plain chat', () => {
    expect(rowAccentClass(chat({}), { now: NOW })).toBe('')
    expect(rowAccentClass(chat({ project_id: 'p1' }), { now: NOW })).toBe('')
  })

  it('every delegated worker gets the violet accent — no project-count gating', () => {
    expect(rowAccentClass(chat({ origin: 'delegated' }), { now: NOW }))
      .toContain('violet')
    expect(rowAccentClass(chat({ delegate_role: 'worker' }), { now: NOW }))
      .toContain('violet')
  })

  it('orchestrators get the amber accent', () => {
    const cls = rowAccentClass(chat({ delegate_role: 'orchestrator' }), { now: NOW })
    expect(cls).toContain('amber')
    expect(cls).not.toContain('violet')
  })

  it('the active chat shows no accent — the selection bar is the only left stripe', () => {
    expect(rowAccentClass(chat({ origin: 'delegated' }), { now: NOW, active: true }))
      .toBe('')
  })

  it('accents fade once the chat has been quiet for a day', () => {
    const stale = chat({ origin: 'delegated', updated_at: '2026-07-06T11:00:00Z' })
    expect(rowAccentClass(stale, { now: NOW })).toContain('/40')
    const fresh = chat({ origin: 'delegated' })
    expect(rowAccentClass(fresh, { now: NOW })).not.toContain('/40')
  })
})
