// The tab's trail of history entries and the two answers the settings areas
// read from it: is the entry behind in this area, and where does "Back to
// Chat" go.
import { describe, it, expect, beforeEach } from 'vitest'
import {
  areaEntryBehind, fallbackChat, planBackToChat, readTrail, recordEntry, saveEntry,
  clearTrail, type Trail,
} from '@/components/settings/history'

const empty: Trail = { entries: {}, lastChat: null }
const trailOf = (paths: (string | undefined)[], lastChat: string | null = null): Trail => ({
  entries: Object.fromEntries(paths.flatMap((p, i) => (p === undefined ? [] : [[i, p]]))),
  lastChat,
})

describe('recordEntry', () => {
  it('a push drops the entries above it, as the browser does', () => {
    const t = recordEntry(trailOf(['/chat/a', '/admin', '/admin/users', '/admin/usage']), 2, '/admin/shares', 'PUSH')
    expect(t.entries).toEqual({ 0: '/chat/a', 1: '/admin', 2: '/admin/shares' })
  })

  it('a replace and a traversal overwrite their own entry only', () => {
    const base = trailOf(['/chat/a', '/user-settings?tab=usage', '/x'])
    expect(recordEntry(base, 1, '/user-settings/usage', 'REPLACE').entries)
      .toEqual({ 0: '/chat/a', 1: '/user-settings/usage', 2: '/x' })
    expect(recordEntry(base, 0, '/chat/a', 'POP').entries[2]).toBe('/x')
  })

  it('keeps the pathname alone and remembers the last chat by it', () => {
    let t = recordEntry(empty, 0, '/chat/a/c1?ws=1', 'PUSH')
    expect(t.entries[0]).toBe('/chat/a/c1')
    expect(t.lastChat).toBe('/chat/a/c1')
    t = recordEntry(t, 1, '/admin', 'PUSH')
    expect(t.lastChat).toBe('/chat/a/c1')
  })

  it('keeps at most two hundred entries below the current one', () => {
    let t = empty
    for (let i = 0; i < 260; i++) t = recordEntry(t, i, `/p${i}`, 'PUSH')
    const keys = Object.keys(t.entries).map(Number)
    expect(Math.min(...keys)).toBe(59)
    expect(Math.max(...keys)).toBe(259)
  })
})

describe('the stored trail', () => {
  beforeEach(() => sessionStorage.clear())

  it('saves, reads back and clears', () => {
    saveEntry(0, '/chat/a', 'PUSH')
    saveEntry(1, '/admin', 'PUSH')
    expect(readTrail()).toEqual({ entries: { 0: '/chat/a', 1: '/admin' }, lastChat: '/chat/a' })
    clearTrail()
    expect(readTrail()).toEqual(empty)
  })

  it('reads an unreadable value as an empty trail', () => {
    sessionStorage.setItem('otodock.settingsTrail', '{not json')
    expect(readTrail()).toEqual(empty)
  })

  it('keeps only string entries at whole numbers', () => {
    sessionStorage.setItem('otodock.settingsTrail', JSON.stringify({
      entries: { 0: '/chat/a', 1: 7, 2: null, x: '/admin', '-1': '/x', 3: '/admin/users' },
      lastChat: 5,
    }))
    expect(readTrail()).toEqual({ entries: { 0: '/chat/a', 3: '/admin/users' }, lastChat: null })
  })
})

describe('areaEntryBehind', () => {
  it('is true only for the same area right behind', () => {
    const t = trailOf(['/chat/a', '/user-settings', '/user-settings/audio', '/admin/users'])
    expect(areaEntryBehind(t, 2, '/user-settings')).toBe(true)
    expect(areaEntryBehind(t, 1, '/user-settings')).toBe(false)
    expect(areaEntryBehind(t, 3, '/user-settings')).toBe(true)
    expect(areaEntryBehind(t, 3, '/admin')).toBe(false)
    expect(areaEntryBehind(t, 0, '/admin')).toBe(false)
  })

  it('tells one agent from another', () => {
    const t = trailOf(['/agents/a/config', '/agents/b/triggers'])
    expect(areaEntryBehind(t, 1, '/agents/b')).toBe(false)
  })
})

describe('planBackToChat', () => {
  it('pops to the chat right behind the area', () => {
    expect(planBackToChat(trailOf(['/chat/a', '/chat/a/c1', '/admin', '/admin/users']), 3, '/'))
      .toEqual({ kind: 'pop', delta: -2 })
  })

  it('walks over two areas to the chat', () => {
    const t = trailOf(['/chat/a', '/admin', '/admin/platform', '/user-settings', '/user-settings/ai-engines'])
    expect(planBackToChat(t, 4, '/')).toEqual({ kind: 'pop', delta: -4 })
  })

  it('travels to the page under the areas and pushes the chat over them', () => {
    expect(planBackToChat(trailOf(['/chat/a', '/agents', '/agents/b']), 2, '/chat/b'))
      .toEqual({ kind: 'push', delta: -1, target: '/chat/b' })
    expect(planBackToChat(trailOf(['/chat/a', '/apps/x', '/user-settings', '/user-settings/usage']), 3, '/chat/a'))
      .toEqual({ kind: 'push', delta: -2, target: '/chat/a' })
  })

  it('replaces the cold root, known or never seen', () => {
    expect(planBackToChat(trailOf(['/', '/admin', '/admin/users']), 2, '/'))
      .toEqual({ kind: 'replace', delta: -2, target: '/' })
    expect(planBackToChat(trailOf([undefined, '/admin/users']), 1, '/chat/a'))
      .toEqual({ kind: 'replace', delta: -1, target: '/chat/a' })
  })

  it('replaces in place at the first entry', () => {
    expect(planBackToChat(trailOf(['/admin/platform']), 0, '/'))
      .toEqual({ kind: 'replace', delta: 0, target: '/' })
  })
})

describe('fallbackChat', () => {
  it('keeps the last chat, or the default agent when the tab showed none', () => {
    expect(fallbackChat(trailOf([], '/chat/a/c1'))).toBe('/chat/a/c1')
    expect(fallbackChat(empty)).toBe('/')
  })

  it("keeps an agent's area on that agent's chats", () => {
    expect(fallbackChat(trailOf([], '/chat/ops/c1'), 'ops')).toBe('/chat/ops/c1')
    expect(fallbackChat(trailOf([], '/chat/ops'), 'ops')).toBe('/chat/ops')
    expect(fallbackChat(trailOf([], '/chat/other/c2'), 'ops')).toBe('/chat/ops')
    expect(fallbackChat(trailOf([], '/chat/ops-2'), 'ops')).toBe('/chat/ops')
    expect(fallbackChat(empty, 'ops')).toBe('/chat/ops')
  })
})
