// A tab whose sessionStorage refuses writes keeps the trail in memory, so
// the settings areas still know what sits behind an entry. Its own file:
// the broken-storage state lives for the module's life.
import { describe, it, expect, vi } from 'vitest'
import { readTrail, saveEntry } from '@/components/settings/history'

describe('the trail without storage', () => {
  it('falls back to the copy in memory once a write fails', () => {
    saveEntry(0, '/chat/a', 'PUSH')
    expect(readTrail().entries).toEqual({ 0: '/chat/a' })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    saveEntry(1, '/admin', 'PUSH')
    saveEntry(2, '/admin/users', 'PUSH')
    expect(readTrail().entries).toEqual({ 0: '/chat/a', 1: '/admin', 2: '/admin/users' })
    vi.restoreAllMocks()
    // Storage is not read again: a stale copy there never wins.
    sessionStorage.setItem('otodock.settingsTrail', JSON.stringify({ entries: { 0: '/x' }, lastChat: null }))
    expect(readTrail().entries).toEqual({ 0: '/chat/a', 1: '/admin', 2: '/admin/users' })
  })
})
