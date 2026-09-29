import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import {
  _resetFocusForTests, appMounted, appUnmounted, currentFocus, focusPreludeLine,
  formatFocusLine, resendFocus, setFocusSender, setFocusSharing, setPageFocus,
} from '@/lib/focus'

describe('viewer focus', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    _resetFocusForTests()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('debounces a fast toggle into one frame carrying the last selection', () => {
    const sent: Record<string, unknown>[] = []
    setFocusSender((f) => sent.push(f))
    setPageFocus({ surface: 'chat', chat_id: 'c1' })
    appMounted({ id: 'a1', title: 'One', slug: 'one' })
    appUnmounted('a1')
    appMounted({ id: 'a2', title: 'Two', slug: 'two' })
    expect(sent).toEqual([])
    vi.advanceTimersByTime(260)
    expect(sent).toEqual([{ type: 'focus', surface: 'app', app_id: 'a2', chat_id: 'c1' }])
    expect(currentFocus()).toEqual({ surface: 'app', app_id: 'a2', chat_id: 'c1' })
  })

  it('falls back to the page when the last frame unmounts, and never repeats a value', () => {
    const sent: Record<string, unknown>[] = []
    setFocusSender((f) => sent.push(f))
    setPageFocus({ surface: 'home' })
    vi.advanceTimersByTime(260)
    appMounted({ id: 'a1', title: 'One', slug: 'one' })
    vi.advanceTimersByTime(260)
    appUnmounted('a1')
    vi.advanceTimersByTime(260)
    setPageFocus({ surface: 'home' })
    vi.advanceTimersByTime(260)
    expect(sent).toEqual([
      { type: 'focus', surface: 'home' },
      { type: 'focus', surface: 'app', app_id: 'a1' },
      { type: 'focus', surface: 'home' },
    ])
  })

  it('prefers the chat-scoped board when the Dock mounts two apps at once', () => {
    setPageFocus({ surface: 'chat', chat_id: 'c9' })
    appMounted({ id: 'chat-board', title: 'Plan', slug: 'plan' }, true)
    appMounted({ id: 'project-board', title: 'Lanes', slug: 'lanes' }, false)
    expect(currentFocus().app_id).toBe('chat-board')
    appUnmounted('chat-board')
    expect(currentFocus().app_id).toBe('project-board')
  })

  it('resends the current value on a reconnect and holds it while no socket is open', () => {
    const first: Record<string, unknown>[] = []
    setFocusSender((f) => first.push(f))
    appMounted({ id: 'a1', title: 'One', slug: 'one' })
    vi.advanceTimersByTime(260)
    expect(first).toHaveLength(1)
    setFocusSender(null)
    appUnmounted('a1')
    setPageFocus({ surface: 'chat', chat_id: 'c9' })
    vi.advanceTimersByTime(260)
    expect(first).toHaveLength(1)
    const second: Record<string, unknown>[] = []
    setFocusSender((f) => second.push(f))
    resendFocus()
    expect(second).toEqual([{ type: 'focus', surface: 'chat', chat_id: 'c9' }])
  })

  it('builds the interactive prelude the way the server does, and honours the setting', () => {
    expect(focusPreludeLine()).toBe('')
    appMounted({ id: 'a1', title: 'Ops [live] \n board', slug: 'ops' })
    expect(focusPreludeLine()).toBe('[The user is looking at the app "Ops live board" (ops) right now.]')
    setFocusSharing(false)
    expect(focusPreludeLine()).toBe('')
    setFocusSharing(true)
    expect(formatFocusLine('', 'ab')).toBe('[The user is looking at the app "ab" (ab) right now.]')
    expect(formatFocusLine('t'.repeat(500), 's').length).toBeLessThan(200)
  })
})
