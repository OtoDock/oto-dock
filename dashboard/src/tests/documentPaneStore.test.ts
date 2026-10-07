import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  MAX_PANE_CHATS, SAME_FLUSH_MS, notePushedDocument, paneChat, pushedDocumentFromFrame,
  registerPaneSave, saveDocumentPane, useDocumentPaneStore, useDocumentPushStore,
} from '@/store/documentPaneStore'

// The document pane's state per chat: a push opens it on the newest file
// unless the document on show has unsaved edits; a close or a minimize
// holds until a newer push; a chat seen for the first time starts closed;
// a push that landed while the page was away opens it; nothing token-like
// is ever persisted.

const store = () => useDocumentPaneStore.getState()
const push = (fileId: string, generation: number, extra: object = {}) =>
  notePushedDocument('c1', {
    fileId, filename: `${fileId}.docx`, wopiUrl: '/cool', accessToken: 'secret-token',
    downloadUrl: '/d', generation, ...extra,
  })

beforeEach(() => {
  localStorage.clear()
  useDocumentPaneStore.setState({ owner: 'me', byChat: {} })
  useDocumentPushStore.setState({ pushed: {}, known: {}, dirty: {}, attended: {} })
})

describe('document pane store', () => {
  it('a push opens the pane live on the newest file', () => {
    push('a', 10)
    push('b', 20)
    expect(paneChat('c1')).toMatchObject({ open: true, minimized: false, active: 'b', seen: 20 })
  })

  it('a close and a minimize hold until a newer push; an older one never reopens', () => {
    push('a', 10)
    store().close('c1')
    push('a', 10)
    expect(paneChat('c1').open).toBe(false)
    push('b', 11)
    expect(paneChat('c1')).toMatchObject({ open: true, active: 'b' })
    store().minimize('c1')
    push('b', 11)
    expect(paneChat('c1').minimized).toBe(true)
    push('a', 12)
    expect(paneChat('c1')).toMatchObject({ minimized: false, active: 'a' })
  })

  it('a push while a version is on show returns that file to Live', () => {
    push('a', 10)
    store().showVersion('c1', 'a', 'snap-1')
    expect(paneChat('c1').view).toEqual({ a: 'snap-1' })
    push('a', 11)
    expect(paneChat('c1').view).toEqual({})
  })

  it('unsaved edits on show keep it on show; the pushed tab gets a dot', () => {
    push('a', 10)
    useDocumentPushStore.getState().setDirty('c1', 'a')
    push('b', 11)
    expect(paneChat('c1')).toMatchObject({ active: 'a', fresh: ['b'], seen: 11 })
    store().select('c1', 'b')
    expect(paneChat('c1')).toMatchObject({ active: 'b', fresh: [] })
  })

  it('every file a turn pushes that does not stay on show gets a dot', () => {
    vi.useFakeTimers()
    try {
      push('a', 10)
      push('b', 20)
      expect(paneChat('c1')).toMatchObject({ active: 'b', fresh: ['a'] })
      // An older push of the same flush arriving last.
      push('c', 15)
      expect(paneChat('c1')).toMatchObject({ active: 'b', fresh: ['a', 'c'] })
      store().select('c1', 'a')
      expect(paneChat('c1').fresh).toEqual(['c'])
      // A later turn: the file the person had on show gets no dot.
      vi.advanceTimersByTime(SAME_FLUSH_MS + 1)
      push('d', 30)
      expect(paneChat('c1')).toMatchObject({ active: 'd', fresh: ['c'] })
    } finally {
      vi.useRealTimers()
    }
  })

  it('a listing with pushes from while the page was away dots the ones not on show', () => {
    store().ensure('c1', { fileId: 'a', generation: 50 })
    store().ensure('c1', { fileId: 'c', generation: 80 }, [
      { fileId: 'b', generation: 70 }, { fileId: 'a', generation: 50 },
    ])
    expect(paneChat('c1')).toMatchObject({ open: true, active: 'c', fresh: ['b'], seen: 80 })
  })

  it('a newer listing never swaps a document with unsaved edits: the newer file gets a dot', () => {
    push('a', 10)
    useDocumentPushStore.getState().setDirty('c1', 'a', 'a.docx')
    store().ensure('c1', { fileId: 'b', generation: 20 }, [], 'a')
    expect(paneChat('c1')).toMatchObject({ open: true, active: 'a', fresh: ['b'], seen: 20 })
    // The edited file itself being the newest stays on show.
    store().ensure('c1', { fileId: 'a', generation: 30 }, [], 'a')
    expect(paneChat('c1')).toMatchObject({ active: 'a', seen: 30 })
    // A clean document follows the listing as before.
    store().ensure('c1', { fileId: 'b', generation: 40 }, [], null)
    expect(paneChat('c1')).toMatchObject({ active: 'b', fresh: [], seen: 40 })
  })

  it('a newer listing under unsaved edits reopens the newer file\'s closed tab on Live', () => {
    push('a', 10)
    push('b', 11)
    store().showVersion('c1', 'a', 'snap-a')
    store().closeFile('c1', 'a', ['b', 'a'])
    useDocumentPushStore.getState().setDirty('c1', 'b', 'b.docx')
    store().ensure('c1', { fileId: 'a', generation: 20 }, [], 'b')
    const pane = paneChat('c1')
    expect(pane).toMatchObject({ active: 'b', fresh: ['a'], seen: 20, view: {} })
    expect(pane.closed).not.toContain('a')
  })

  it('a push while minimized keeps an edited document on show and brings the pane back', () => {
    push('a', 10)
    useDocumentPushStore.getState().setDirty('c1', 'a')
    store().minimize('c1')
    push('b', 11)
    expect(paneChat('c1')).toMatchObject({ minimized: false, active: 'a', fresh: ['b'] })
  })

  it('only a push that opened or switched the window takes Esc back from the person', () => {
    push('a', 10)
    useDocumentPushStore.getState().setAttended('c1', true)
    push('a', 10)
    expect(useDocumentPushStore.getState().attended.c1).toBe(true)
    useDocumentPushStore.getState().setDirty('c1', 'a')
    push('b', 11)
    expect(useDocumentPushStore.getState().attended.c1).toBe(true)
    useDocumentPushStore.getState().setDirty('c1', null)
    push('b', 12)
    expect(useDocumentPushStore.getState().attended.c1).toBe(false)
  })

  it("another tab's write never swaps a chat whose document has unsaved edits here", async () => {
    push('a', 10)
    useDocumentPushStore.getState().setDirty('c1', 'a')
    const local = paneChat('c1')
    const other = { ...local, active: 'b', seen: 99, touched: Date.now() }
    localStorage.setItem('oto-dock-document-pane', JSON.stringify({
      state: { owner: 'me', byChat: { c1: other, c2: { ...other, active: 'z' } } }, version: 1,
    }))
    window.dispatchEvent(new StorageEvent('storage', { key: 'oto-dock-document-pane' }))
    await new Promise((r) => setTimeout(r, 0))
    expect(paneChat('c1').active).toBe('a')
    expect(paneChat('c2').active).toBe('z')
  })

  it('first sight of a chat starts closed; a push that landed while away opens it', () => {
    store().ensure('c1', { fileId: 'a', generation: 50 })
    expect(paneChat('c1')).toMatchObject({ open: false, seen: 50 })
    store().ensure('c1', { fileId: 'a', generation: 50 })
    expect(paneChat('c1').open).toBe(false)
    store().ensure('c1', { fileId: 'b', generation: 60 })
    expect(paneChat('c1')).toMatchObject({ open: true, active: 'b', seen: 60 })
  })

  it('an empty listing seeds a new chat so its first push opens the pane', () => {
    store().ensure('c1', null)
    expect(paneChat('c1')).toMatchObject({ open: false, seen: 0 })
    push('a', 5)
    expect(paneChat('c1').open).toBe(true)
  })

  it('a tab X hands over to the next tab; the last one closes the pane', () => {
    push('a', 10)
    push('b', 20)
    store().closeFile('c1', 'b', ['b', 'a'])
    expect(paneChat('c1')).toMatchObject({ active: 'a', open: true, closed: ['b'] })
    store().closeFile('c1', 'a', ['b', 'a'])
    expect(paneChat('c1')).toMatchObject({ active: null, open: false })
    push('b', 30)
    expect(paneChat('c1')).toMatchObject({ open: true, active: 'b', closed: ['a'] })
  })

  it('a card opens Live or a version; a chip restores on its file', () => {
    store().openFile('c1', 'a', 'snap-2')
    expect(paneChat('c1')).toMatchObject({ open: true, active: 'a', view: { a: 'snap-2' } })
    store().minimize('c1')
    store().restore('c1', 'b')
    expect(paneChat('c1')).toMatchObject({ minimized: false, active: 'b' })
  })

  it('another person signing in drops the data', () => {
    push('a', 10)
    store().setOwner('me')
    expect(paneChat('c1').open).toBe(true)
    store().setOwner('someone-else')
    expect(store().byChat).toEqual({})
  })

  it('keeps the most recently touched chats only', () => {
    for (let i = 0; i < MAX_PANE_CHATS + 5; i++) store().ensure(`chat-${i}`, null)
    expect(Object.keys(store().byChat)).toHaveLength(MAX_PANE_CHATS)
  })

  it('persists ids only, never a token or a URL', () => {
    push('a', 10)
    const raw = localStorage.getItem('oto-dock-document-pane') ?? ''
    expect(raw).toContain('"active":"a"')
    expect(raw).not.toContain('secret-token')
    expect(raw).not.toContain('/cool')
  })

  it('reads a pushed frame off the wire', () => {
    expect(pushedDocumentFromFrame({
      file_id: 'f', filename: 'x.docx', wopi_url: '/w', access_token: 't', access_token_ttl: 3,
      download_url: '/d', snapshot_id: 's', version: 2, generation: 9,
    })).toEqual({
      fileId: 'f', filename: 'x.docx', wopiUrl: '/w', accessToken: 't', accessTokenTtl: 3,
      downloadUrl: '/d', snapshotId: 's', version: 2, generation: 9,
    })
    expect(pushedDocumentFromFrame({ filename: 'x' })).toBeNull()
  })
})

describe('saving the document on show from outside the pane', () => {
  it('resolves true with no pane mounted', async () => {
    await expect(saveDocumentPane('nobody')).resolves.toBe(true)
  })

  it("runs the mounted pane's save until it unregisters", async () => {
    const save = vi.fn(async () => false)
    const unregister = registerPaneSave('c1', save)
    await expect(saveDocumentPane('c1')).resolves.toBe(false)
    expect(save).toHaveBeenCalledTimes(1)
    unregister()
    await expect(saveDocumentPane('c1')).resolves.toBe(true)
    expect(save).toHaveBeenCalledTimes(1)
  })

  it('the dirty file keeps its name for the composer, and loses it when clean', () => {
    useDocumentPushStore.getState().setDirty('c1', 'a', 'report.docx')
    expect(useDocumentPushStore.getState()).toMatchObject({ dirty: { c1: 'a' }, dirtyNames: { c1: 'report.docx' } })
    useDocumentPushStore.getState().setDirty('c1', null, 'report.docx')
    expect(useDocumentPushStore.getState()).toMatchObject({ dirty: { c1: null }, dirtyNames: { c1: '' } })
  })
})
