/**
 * Drafts and queued messages across windows. Each window is its own
 * instance of the chat store and its stored drafts (a fresh module graph)
 * over the one `localStorage` of the browser, as two dashboard windows are.
 * A browser hands one window's writes to the others as `storage` events;
 * `settle` and `deliver` do that here. The windows share one document, so
 * `focused` says whether the window acting next has the focus.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

type ChatStoreModule = typeof import('@/store/chatStore')
type DraftsModule = typeof import('@/store/chatDrafts')
interface Win { store: ChatStoreModule; drafts: DraftsModule; open: boolean }

const LEGACY_KEY = 'oto-dock-chat-store'
const draftKey = (owner: string, chatId: string) => `oto-dock-draft:${owner}:${chatId}`
const queueKey = (owner: string, chatId: string) => `oto-dock-queue:${owner}:${chatId}`

let wins: Win[] = []
const written = new Set<string>()
let focused = true

async function openWindow(owner = 'u1'): Promise<Win> {
  vi.resetModules()
  const store = await import('@/store/chatStore')
  const drafts = await import('@/store/chatDrafts')
  drafts.bindChatDrafts(owner)
  const w: Win = { store, drafts, open: true }
  wins.push(w)
  return w
}

const state = (w: Win) => w.store.useChatStore.getState()
const draft = (w: Win, chatId: string) => state(w).byChat[chatId]?.draftInput ?? ''
const queue = (w: Win, chatId: string) => (state(w).byChat[chatId]?.queuedMessages ?? []).map((m) => m.text)

/** The writes made so far reach every other open window, as `storage`
 *  events do. */
function deliver(from: Win) {
  const keys = [...written]
  written.clear()
  for (const other of wins) {
    if (other === from || !other.open) continue
    for (const key of keys) other.drafts.takeStoredChange(key)
  }
}

/** The window's pending writes land and reach the others. */
function settle(w: Win) {
  vi.advanceTimersByTime(1000)
  deliver(w)
}

/** The window closes: the page is hidden, then gone. */
function close(w: Win) {
  window.dispatchEvent(new Event('pagehide'))
  deliver(w)
  w.open = false
}

/** Storage refuses every write (full) until the returned call. */
function refuseWrites(): () => void {
  const setItem = vi.mocked(Storage.prototype.setItem)
  const accepting = setItem.getMockImplementation()
  setItem.mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError') })
  return () => { if (accepting) setItem.mockImplementation(accepting) }
}

/** Every way a window reads all of storage again: hidden and shown,
 *  back from the page cache, another window emptying the storage. */
function rereadStorage(w: Win) {
  const visibility = vi.spyOn(document, 'visibilityState', 'get')
  visibility.mockReturnValue('hidden')
  document.dispatchEvent(new Event('visibilitychange'))
  visibility.mockReturnValue('visible')
  document.dispatchEvent(new Event('visibilitychange'))
  visibility.mockRestore()
  const shown = new Event('pageshow')
  Object.defineProperty(shown, 'persisted', { value: true })
  window.dispatchEvent(shown)
  w.drafts.takeStoredChange(null)
}

/** The page may not use storage at all (a private window that refuses it). */
async function withoutStorage(run: () => Promise<void>) {
  const own = Object.getOwnPropertyDescriptor(window, 'localStorage')
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get() { throw new DOMException('denied', 'SecurityError') },
  })
  try {
    await run()
  } finally {
    if (own) Object.defineProperty(window, 'localStorage', own)
    else delete (window as { localStorage?: Storage }).localStorage
  }
}

const storedDraftKeys = () => Object.keys(localStorage).filter((k) => k.startsWith('oto-dock-draft:') || k.startsWith('oto-dock-queue:'))

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  wins = []
  written.clear()
  focused = true
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused)
  const setItem = Storage.prototype.setItem
  const removeItem = Storage.prototype.removeItem
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    written.add(key)
    setItem.call(this, key, value)
  })
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key: string) {
    written.add(key)
    removeItem.call(this, key)
  })
})

afterEach(() => {
  for (const w of wins) w.open = false
  vi.restoreAllMocks()
  vi.useRealTimers()
  localStorage.clear()
})

describe('a draft typed in one window', () => {
  it('survives the other window\'s activity, in a chat and on the new-chat page', async () => {
    const a = await openWindow()
    const b = await openWindow()
    state(a).setDraftInput('c1', 'hello')
    state(a).setDraftInput('__new__:agent-a', 'a new chat')
    settle(a)
    // Frames every window receives, with no action of its person.
    state(b).setStreaming('c9')
    settle(b)
    state(b).setReady('c9')
    state(b).setUnread('c9', true)
    settle(b)
    const c = await openWindow()
    expect(draft(c, 'c1')).toBe('hello')
    expect(draft(c, '__new__:agent-a')).toBe('a new chat')
    expect(draft(b, 'c1')).toBe('hello')
  })

  it('survives a reload of either window and closing them in any order', async () => {
    const a = await openWindow()
    const b = await openWindow()
    state(a).setDraftInput('c1', 'kept')
    settle(a)
    close(a)
    state(b).setUnread('c9', true)
    settle(b)
    const a2 = await openWindow()
    expect(draft(a2, 'c1')).toBe('kept')
    state(b).setReady('c9')
    settle(b)
    close(b)
    state(a2).setUnread('c8', true)
    settle(a2)
    close(a2)
    const next = await openWindow()
    expect(draft(next, 'c1')).toBe('kept')
  })

  it('is written when the window closes within the debounce', async () => {
    const a = await openWindow()
    state(a).setDraftInput('c1', 'typed just before closing')
    close(a)
    const next = await openWindow()
    expect(draft(next, 'c1')).toBe('typed just before closing')
  })

  it('stays gone once sent, in every window and at the next open', async () => {
    const a = await openWindow()
    state(a).setDraftInput('c1', 'hello')
    settle(a)
    const b = await openWindow()
    expect(draft(b, 'c1')).toBe('hello')
    state(a).clearDraft('c1')
    settle(a)
    expect(draft(b, 'c1')).toBe('')
    state(b).setUnread('c9', true)
    state(b).setStreaming('c9')
    settle(b)
    const c = await openWindow()
    expect(draft(c, 'c1')).toBe('')
    expect(storedDraftKeys()).toEqual([])
  })
})

describe('queued messages', () => {
  it('survive the other window\'s activity and stay gone once sent', async () => {
    const a = await openWindow()
    const b = await openWindow()
    // The resume's snapshot reaches the window that resumed the chat.
    state(a).setQueuedMessages('c1', [{ text: 'next', queueId: 'q1' }])
    settle(a)
    state(b).setUnread('c9', true)
    settle(b)
    const c = await openWindow()
    expect(queue(c, 'c1')).toEqual(['next'])
    expect(queue(b, 'c1')).toEqual(['next'])
    state(a).clearQueuedMessages('c1', ['q1'])
    settle(a)
    state(b).setReady('c9')
    settle(b)
    const d = await openWindow()
    expect(queue(d, 'c1')).toEqual([])
  })

  it('leave the draft the other window is typing alone', async () => {
    const a = await openWindow()
    const b = await openWindow()
    state(a).setDraftInput('c1', 'hel')
    // A queue frame reaches both windows while the person types in A.
    state(b).addQueuedMessage('c1', 0, { text: 'q', queueId: 'q1' })
    state(a).addQueuedMessage('c1', 0, { text: 'q', queueId: 'q1' })
    deliver(b)
    expect(draft(a, 'c1')).toBe('hel')
    settle(a)
    expect(draft(b, 'c1')).toBe('hel')
    expect(queue(b, 'c1')).toEqual(['q'])
  })
})

describe('one chat in two windows', () => {
  it('keeps the edit not yet stored over the other window\'s write', async () => {
    const a = await openWindow()
    state(a).setDraftInput('c1', 'one')
    settle(a)
    const b = await openWindow()
    // The person types on in A; a refused message comes back to the
    // composer in both windows of its author.
    state(a).setDraftInput('c1', 'one two')
    focused = false
    state(b).setDraftInput('c1', `${draft(b, 'c1')}\n\nback`)
    deliver(b)
    focused = true
    state(a).setDraftInput('c1', `${draft(a, 'c1')}\n\nback`)
    expect(draft(a, 'c1')).toBe('one two\n\nback')
    settle(a)
    expect(draft(b, 'c1')).toBe('one two\n\nback')
    expect(localStorage.getItem(draftKey('u1', 'c1'))).toBe('one two\n\nback')
  })

  it('stores at once a change in a window without the focus', async () => {
    const a = await openWindow()
    const b = await openWindow()
    focused = false
    state(b).setDraftInput('c1', 'came back to the composer')
    deliver(b)
    expect(draft(a, 'c1')).toBe('came back to the composer')
  })

  it('stores the edit at once when the window loses the focus', async () => {
    const a = await openWindow()
    const b = await openWindow()
    state(a).setDraftInput('c1', 'switching windows')
    window.dispatchEvent(new Event('blur'))
    deliver(a)
    expect(draft(b, 'c1')).toBe('switching windows')
  })
})

describe('the browser\'s events', () => {
  it('takes another window\'s write from a storage event of this storage only', async () => {
    const a = await openWindow()
    localStorage.setItem(draftKey('u1', 'c1'), 'from the other window')
    window.dispatchEvent(new StorageEvent('storage', { key: draftKey('u1', 'c1'), storageArea: sessionStorage }))
    expect(draft(a, 'c1')).toBe('')
    window.dispatchEvent(new StorageEvent('storage', { key: draftKey('u1', 'c1'), storageArea: localStorage }))
    expect(draft(a, 'c1')).toBe('from the other window')
  })

  it('stores when hidden and reads what it missed when shown again', async () => {
    const a = await openWindow()
    const visibility = vi.spyOn(document, 'visibilityState', 'get')
    state(a).setDraftInput('c1', 'before hiding')
    visibility.mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(localStorage.getItem(draftKey('u1', 'c1'))).toBe('before hiding')
    // Written while this page was frozen: no event reached it.
    localStorage.setItem(draftKey('u1', 'c1'), 'written elsewhere')
    localStorage.setItem(queueKey('u1', 'c2'), JSON.stringify([{ text: 'q' }]))
    visibility.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(draft(a, 'c1')).toBe('written elsewhere')
    expect(queue(a, 'c2')).toEqual(['q'])
  })
})

describe('the person who owns the drafts', () => {
  it('leaves none in storage after a sign-out, also with a second window open', async () => {
    const a = await openWindow('u1')
    const b = await openWindow('u1')
    state(a).setDraftInput('c1', 'private')
    state(a).setQueuedMessages('c1', [{ text: 'q', queueId: 'q1' }])
    settle(a)
    a.drafts.clearChatDrafts()
    deliver(a)
    expect(storedDraftKeys()).toEqual([])
    expect(draft(b, 'c1')).toBe('')
    expect(queue(b, 'c1')).toEqual([])
    state(b).setUnread('c9', true)
    settle(b)
    expect(storedDraftKeys()).toEqual([])
    // The second window's person types again before it learns of the
    // sign-out; another person signs in next.
    state(b).setDraftInput('c2', 'typed after the sign-out')
    settle(b)
    const c = await openWindow('u2')
    expect(draft(c, 'c2')).toBe('')
    expect(storedDraftKeys()).toEqual([])
  })

  it('never loads another person\'s drafts', async () => {
    const a = await openWindow('u1')
    state(a).setDraftInput('__new__:shared-agent', 'mine')
    settle(a)
    const b = await openWindow('u2')
    expect(draft(b, '__new__:shared-agent')).toBe('')
    expect(storedDraftKeys()).toEqual([])
  })
})

describe('the record of an earlier version', () => {
  const legacy = (byChat: Record<string, unknown>, version: number) =>
    localStorage.setItem(LEGACY_KEY, JSON.stringify({ state: { byChat }, version }))

  it('is carried over once and removed', async () => {
    legacy({
      c1: { chatId: 'c1', draftInput: 'old draft', queuedMessages: [{ text: 'q', queueId: 'q1' }] },
      '__new__:agent-a': { chatId: '__new__:agent-a', draftInput: 'new chat draft', queuedMessages: [] },
    }, 3)
    const a = await openWindow()
    expect(draft(a, 'c1')).toBe('old draft')
    expect(queue(a, 'c1')).toEqual(['q'])
    expect(draft(a, '__new__:agent-a')).toBe('new chat draft')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
    expect(localStorage.getItem(draftKey('u1', 'c1'))).toBe('old draft')
    expect(JSON.parse(localStorage.getItem(queueKey('u1', 'c1'))!)).toEqual([{ text: 'q', queueId: 'q1' }])
  })

  it('reads the older queue shapes and never replaces a newer entry', async () => {
    localStorage.setItem(draftKey('u1', 'c1'), 'newer')
    legacy({ c1: { draftInput: 'older', queuedMessages: ['v1 text', { text: 'v2 item' }] } }, 1)
    const a = await openWindow()
    expect(draft(a, 'c1')).toBe('newer')
    expect(queue(a, 'c1')).toEqual(['v1 text', 'v2 item'])
  })

  it('is held in memory when storage refuses its new keys', async () => {
    legacy({ c1: { draftInput: 'old draft', queuedMessages: [{ text: 'q' }] } }, 3)
    refuseWrites()
    const a = await openWindow()
    expect(draft(a, 'c1')).toBe('old draft')
    expect(queue(a, 'c1')).toEqual(['q'])
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
    rereadStorage(a)
    expect(draft(a, 'c1')).toBe('old draft')
    expect(queue(a, 'c1')).toEqual(['q'])
  })

  it('is removed when it cannot be read', async () => {
    localStorage.setItem(LEGACY_KEY, '{not json')
    const a = await openWindow()
    expect(state(a).byChat).toEqual({})
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })
})

describe('storage that refuses', () => {
  it('keeps the draft in memory when a write is refused', async () => {
    const a = await openWindow()
    vi.mocked(Storage.prototype.setItem).mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
    state(a).setDraftInput('c1', 'still here')
    state(a).setQueuedMessages('c1', [{ text: 'q' }])
    expect(() => settle(a)).not.toThrow()
    expect(draft(a, 'c1')).toBe('still here')
  })

  it('works with no storage at all', async () => {
    await withoutStorage(async () => {
      const a = await openWindow()
      state(a).setDraftInput('c1', 'no storage')
      expect(() => settle(a)).not.toThrow()
      expect(draft(a, 'c1')).toBe('no storage')
      expect(() => a.drafts.clearChatDrafts()).not.toThrow()
    })
  })

  it('keeps a draft and a queue it could not store when it reads storage again', async () => {
    const a = await openWindow()
    refuseWrites()
    state(a).setDraftInput('c1', 'not stored')
    state(a).setQueuedMessages('c1', [{ text: 'q' }])
    settle(a)
    rereadStorage(a)
    expect(draft(a, 'c1')).toBe('not stored')
    expect(queue(a, 'c1')).toEqual(['q'])
  })

  it('keeps them with no storage at all when it reads storage again', async () => {
    await withoutStorage(async () => {
      const a = await openWindow()
      state(a).setDraftInput('c1', 'no storage')
      state(a).setQueuedMessages('c1', [{ text: 'q' }])
      settle(a)
      rereadStorage(a)
      expect(draft(a, 'c1')).toBe('no storage')
      expect(queue(a, 'c1')).toEqual(['q'])
    })
  })

  it('stores the text once storage takes it again, then follows storage', async () => {
    const a = await openWindow()
    const accept = refuseWrites()
    state(a).setDraftInput('c1', 'hel')
    settle(a)
    rereadStorage(a)
    expect(draft(a, 'c1')).toBe('hel')
    accept()
    state(a).setDraftInput('c1', 'hell')
    settle(a)
    expect(localStorage.getItem(draftKey('u1', 'c1'))).toBe('hell')
    // Written by a window whose event this page missed: stored once, the
    // draft follows storage again.
    localStorage.setItem(draftKey('u1', 'c1'), 'written elsewhere')
    rereadStorage(a)
    expect(draft(a, 'c1')).toBe('written elsewhere')
  })

  it('still drops a draft another window sent, after its own write was refused', async () => {
    const a = await openWindow()
    const b = await openWindow()
    const accept = refuseWrites()
    state(a).setDraftInput('c1', 'typed in A')
    settle(a)
    accept()
    state(b).setDraftInput('c1', 'typed in B')
    settle(b)
    expect(draft(a, 'c1')).toBe('typed in B')
    state(b).clearDraft('c1')
    deliver(b)
    expect(draft(a, 'c1')).toBe('')
  })
})

describe('writing while the person types', () => {
  it('writes the changed chat\'s text once, after the last keystroke', async () => {
    const a = await openWindow()
    state(a).setDraftInput('c2', 'other chat')
    settle(a)
    const setItem = vi.mocked(Storage.prototype.setItem)
    setItem.mockClear()
    for (const text of ['h', 'he', 'hel', 'hell', 'hello']) {
      state(a).setDraftInput('c1', text)
      vi.advanceTimersByTime(100)
    }
    state(a).setUnread('c9', true)
    expect(setItem).not.toHaveBeenCalled()
    vi.advanceTimersByTime(300)
    expect(setItem.mock.calls).toEqual([[draftKey('u1', 'c1'), 'hello']])
  })
})
