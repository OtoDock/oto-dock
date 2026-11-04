import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'

import { replayableDisplayEvents, MAX_REPLAY_WINDOWS } from '@/lib/displayReplay'
import { loadDismissedPips, recordDismissedPip } from '@/lib/pipDismissals'
import { titleFor, useArtifactWindows } from '@/hooks/useArtifactWindows'
import { fetchChatPage, type ChatMessage } from '@/api/chats'
import { paneChat, useDocumentPaneStore, useDocumentPushStore } from '@/store/documentPaneStore'

// PiP replay-on-open + X-dismiss memory for interactive chats: the drainer
// persists final display artifacts as chat rows; on terminal attach the hook
// re-opens the FINAL TURN's popups (rows after the last user row), keyed and
// deduped by the chat_messages row id, minus per-browser dismissals.

vi.mock('@/api/chats', () => ({ fetchChatPage: vi.fn() }))
const fetchPageMock = vi.mocked(fetchChatPage)

function row(
  id: number,
  role: 'user' | 'assistant' | 'event',
  event_type = '',
  event_data?: Record<string, unknown>,
): ChatMessage {
  return {
    id, chat_id: 'c1', role,
    content: role === 'user' ? 'prompt' : '',
    event_type,
    event_data: event_data ? JSON.stringify(event_data) : '',
    created_at: '2026-07-12T00:00:00Z',
  }
}

const audioEvt = { type: 'audio', src_kind: 'token', token: 't1', media_url: '/v1/media/t1', title: 'Track' }
const imagesEvt = { type: 'images', images: [{ url: 'https://x/i.png', caption: 'Chart' }] }

beforeEach(() => {
  localStorage.clear()
  fetchPageMock.mockReset()
})

// ───────────────────────── recency rule (pure) ──────────────────────────────

describe('replayableDisplayEvents', () => {
  it('replays only display events AFTER the last user row (the final turn)', () => {
    const messages = [
      row(1, 'user'),
      row(2, 'event', 'images', imagesEvt),   // older turn — not replayed
      row(3, 'user'),
      row(4, 'event', 'audio', audioEvt),
      row(5, 'assistant'),
      row(6, 'event', 'images', imagesEvt),
    ]
    const out = replayableDisplayEvents(messages, new Set())
    expect(out.map((r) => r.dbId)).toEqual([4, 6])
    expect(out[0].block.type).toBe('audio')
  })

  it('replays a settings card of the final turn under the label its window is titled by', () => {
    const card = { type: 'settings_card', setting: 'user.general.wake-word', label: 'Wake word', catalog_version: 3 }
    const out = replayableDisplayEvents([row(1, 'user'), row(2, 'event', 'settings_card', card)], new Set())
    expect(out.map((r) => r.dbId)).toEqual([2])
    expect(out[0].block).toEqual({ type: 'setting', settingId: 'user.general.wake-word', label: 'Wake word', catalogVersion: 3 })
    expect(replayableDisplayEvents([row(1, 'user'), row(2, 'event', 'settings_card', card)], new Set([2]))).toEqual([])
  })

  it('replays an action card and a list card of the final turn under their titles', () => {
    const action = { type: 'action_card', card_id: 'card-1', title: 'Move Beta to Eng', text: 'Places Beta.', note: '',
                     buttons: [{ id: 'go', label: 'Move', kind: 'do' }], options: [] }
    const list = { type: 'list_card', kind: 'tasks', filters: { scope: 'mine' }, title: 'Your scheduled tasks', person: 'u1' }
    const out = replayableDisplayEvents(
      [row(1, 'user'), row(2, 'event', 'action_card', action), row(3, 'event', 'list_card', list)], new Set())
    expect(out.map((r) => r.dbId)).toEqual([2, 3])
    expect(out[1].block).toEqual({ type: 'list', kind: 'tasks', filters: { scope: 'mine' }, title: 'Your scheduled tasks', person: 'u1' })
    expect(out.map((r) => titleFor(r.block))).toEqual(['Move Beta to Eng', 'Your scheduled tasks'])
    // An older turn's list card stays in the history.
    expect(replayableDisplayEvents([row(1, 'event', 'list_card', list), row(2, 'user')], new Set())).toEqual([])
  })

  it('a page with no user row replays everything loaded (all newer than the last prompt)', () => {
    const out = replayableDisplayEvents(
      [row(10, 'event', 'audio', audioEvt), row(11, 'event', 'images', imagesEvt)],
      new Set(),
    )
    expect(out.map((r) => r.dbId)).toEqual([10, 11])
  })

  it('nothing replays when the chat ends at the user prompt', () => {
    expect(replayableDisplayEvents(
      [row(1, 'event', 'images', imagesEvt), row(2, 'user')], new Set(),
    )).toEqual([])
  })

  it('ignores non-display events and unparsable/server-dismissed rows', () => {
    const messages = [
      row(1, 'user'),
      row(2, 'event', 'tool', { type: 'tool', name: 'Bash' }),
      row(3, 'event', 'images'),  // no event_data
      row(4, 'event', 'document_preview',
        { type: 'document_preview', wopi_url: 'w', filename: 'f.docx', file_id: 'fid', download_url: '/d', dismissed: true }),
      row(5, 'event', 'audio', audioEvt),
    ]
    expect(replayableDisplayEvents(messages, new Set()).map((r) => r.dbId)).toEqual([5])
  })

  it('skips documents (the document pane opens them) and dedupes ui by path — latest wins', () => {
    const prev = (id: number, wopi: string) =>
      row(id, 'event', 'document_preview',
        { type: 'document_preview', wopi_url: wopi, filename: 'f.docx', file_id: 'fid', download_url: '/d' })
    const ui = (id: number, token: string) =>
      row(id, 'event', 'ui', { type: 'ui', token, ui_url: `/v1/ui/${token}`, path: 'ws/board.html' })
    const out = replayableDisplayEvents(
      [row(1, 'user'), prev(2, 'w1'), ui(3, 'a'), prev(4, 'w2'), ui(5, 'b')],
      new Set(),
    )
    expect(out.map((r) => r.dbId)).toEqual([5])
    expect((out[0].block as any).token).toBe('b')
  })

  it('drops locally dismissed ids and caps at the newest MAX_REPLAY_WINDOWS', () => {
    const messages = [row(1, 'user')]
    for (let i = 0; i < MAX_REPLAY_WINDOWS + 3; i++) {
      messages.push(row(10 + i, 'event', 'images', imagesEvt))
    }
    // Rows 10..18 minus dismissed 18 → the newest MAX_REPLAY_WINDOWS survive.
    const out = replayableDisplayEvents(messages, new Set([18]))
    expect(out).toHaveLength(MAX_REPLAY_WINDOWS)
    expect(out.map((r) => r.dbId)).toEqual([12, 13, 14, 15, 16, 17])
  })
})

// ───────────────────────── dismissal memory ─────────────────────────────────

describe('pipDismissals', () => {
  it('round-trips per chat and never bleeds across chats', () => {
    recordDismissedPip('c1', 7)
    recordDismissedPip('c1', 9)
    recordDismissedPip('c2', 7)
    expect([...loadDismissedPips('c1')].sort()).toEqual([7, 9])
    expect([...loadDismissedPips('c2')]).toEqual([7])
    expect(loadDismissedPips('c3').size).toBe(0)
  })

  it('caps stored ids FIFO and survives corrupt storage', () => {
    for (let i = 0; i < 210; i++) recordDismissedPip('c1', i)
    const ids = loadDismissedPips('c1')
    expect(ids.size).toBe(200)
    expect(ids.has(0)).toBe(false)   // oldest dropped
    expect(ids.has(209)).toBe(true)
    localStorage.setItem('oto.pip.dismissed.cbad', '{not json')
    expect(loadDismissedPips('cbad').size).toBe(0)
  })
})

// ───────────────────────── hook seed + dismiss (integration) ────────────────

function makeWs() {
  const handlers = new Map<string, (msg: any) => void>()
  return {
    ws: {
      subscribe: (type: string, cb: (msg: any) => void) => {
        handlers.set(type, cb)
        return () => handlers.delete(type)
      },
    } as any,
    emit: (event: any) => handlers.get('pty_artifact')?.({ type: 'pty_artifact', chat_id: 'c1', event }),
  }
}

describe('useArtifactWindows replay-on-open', () => {
  const finalTurnPage = {
    messages: [row(1, 'user'), row(4, 'event', 'audio', audioEvt), row(6, 'event', 'images', imagesEvt)],
    has_more: false,
  }

  it('seeds the final turn as windows on attach and dedupes racing live frames by row id', async () => {
    fetchPageMock.mockResolvedValue(finalTurnPage)
    const { ws, emit } = makeWs()
    const { result } = renderHook(() => useArtifactWindows(ws, 'c1'))
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    expect(result.current.windows.map((w) => w.dbId)).toEqual([4, 6])
    expect(result.current.windows[0].block.type).toBe('audio')

    // The same persisted event arriving live (attach race) must not duplicate…
    act(() => { emit({ ...audioEvt, db_message_id: 4 }) })
    expect(result.current.windows).toHaveLength(2)
    // …while a NEW live artifact (fresh row id, or a legacy id-less frame)
    // still opens its own window.
    act(() => { emit({ ...audioEvt, db_message_id: 9 }) })
    act(() => { emit({ type: 'url', url: 'https://x', title: 'X' }) })
    expect(result.current.windows).toHaveLength(4)
  })

  it('an X-dismissed popup stays gone on the next attach; the rest replay', async () => {
    fetchPageMock.mockResolvedValue(finalTurnPage)
    const { ws } = makeWs()
    const { result, rerender } = renderHook(
      ({ cid }: { cid: string }) => useArtifactWindows(ws, cid),
      { initialProps: { cid: 'c1' } },
    )
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    const audioWin = result.current.windows.find((w) => w.block.type === 'audio')!
    act(() => { result.current.close(audioWin.id) })
    expect(result.current.windows).toHaveLength(1)
    expect([...loadDismissedPips('c1')]).toEqual([4])

    // Leave the chat and come back (detach → attach): only images re-opens.
    rerender({ cid: '' })
    expect(result.current.windows).toHaveLength(0)
    rerender({ cid: 'c1' })
    await waitFor(() => expect(result.current.windows).toHaveLength(1))
    expect(result.current.windows[0].dbId).toBe(6)
    expect(result.current.windows[0].block.type).toBe('images')
  })

  it('minimize is NOT a dismissal — the window comes back minimized on the next attach', async () => {
    fetchPageMock.mockResolvedValue(finalTurnPage)
    const { ws } = makeWs()
    const { result, rerender } = renderHook(
      ({ cid }: { cid: string }) => useArtifactWindows(ws, cid),
      { initialProps: { cid: 'c1' } },
    )
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    const first = result.current.windows[0].id
    act(() => { result.current.minimize(first) })
    rerender({ cid: '' })
    rerender({ cid: 'c1' })
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    expect([...result.current.minimized]).toEqual([first])
    expect(loadDismissedPips('c1').size).toBe(0)
  })

  it('opens a live list card as its own window, titled by the list', async () => {
    fetchPageMock.mockResolvedValue({ messages: [row(1, 'user')], has_more: false })
    const { ws, emit } = makeWs()
    const { result } = renderHook(() => useArtifactWindows(ws, 'c1'))
    await act(async () => { await Promise.resolve() })
    act(() => { emit({ type: 'list_card', kind: 'triggers', filters: {}, title: 'Triggers', person: 'u1', db_message_id: 5 }) })
    expect(result.current.windows).toHaveLength(1)
    expect(result.current.windows[0].block).toEqual({ type: 'list', kind: 'triggers', filters: {}, title: 'Triggers', person: 'u1' })
    expect(result.current.windows[0].dbId).toBe(5)
  })

  it('a failed seed fetch leaves live delivery intact', async () => {
    fetchPageMock.mockRejectedValue(new Error('offline'))
    const { ws, emit } = makeWs()
    const { result } = renderHook(() => useArtifactWindows(ws, 'c1'))
    act(() => { emit({ type: 'url', url: 'https://x', title: 'X' }) })
    expect(result.current.windows).toHaveLength(1)
    // Let the rejection settle — nothing crashes, nothing extra appears.
    await act(async () => { await Promise.resolve() })
    expect(result.current.windows).toHaveLength(1)
  })
})

describe('useArtifactWindows: a document push goes to the document pane', () => {
  it('hands the frame to onDocument and opens no window', async () => {
    fetchPageMock.mockResolvedValue({ messages: [], has_more: false })
    const { ws, emit } = makeWs()
    const onDocument = vi.fn()
    const { result } = renderHook(() => useArtifactWindows(ws, 'c1', onDocument))
    const doc = { type: 'document_preview', wopi_url: 'w', filename: 'f.docx', file_id: 'fid', download_url: '/d', generation: 5 }
    act(() => { emit(doc) })
    expect(onDocument).toHaveBeenCalledWith(doc)
    expect(result.current.windows).toHaveLength(0)
  })
})


// ───────────────────── the dock's order, one window on a phone ──────────────

describe('useArtifactWindows: the dock and the phone', () => {
  let narrow = false
  beforeEach(() => {
    narrow = false
    window.matchMedia = ((q: string) => ({
      matches: q.includes('max-width: 767px') && narrow, media: q,
      addEventListener: () => {}, removeEventListener: () => {},
    })) as unknown as typeof window.matchMedia
    useDocumentPaneStore.setState({ owner: 'me', byChat: {} })
    useDocumentPushStore.setState({ pushed: {}, known: {}, dirty: {}, dirtyNames: {}, attended: {} })
    fetchPageMock.mockResolvedValue({ messages: [row(1, 'user')], has_more: false })
  })
  const url = (n: number) => ({ type: 'url', url: `https://x/${n}`, title: `U${n}` })
  const openIds = (r: { current: ReturnType<typeof useArtifactWindows> }) =>
    r.current.windows.filter((w) => !r.current.minimized.has(w.id)).map((w) => w.id)
  const mount = async (cid = 'c1') => {
    const made = makeWs()
    const hook = renderHook(({ c }: { c: string }) => useArtifactWindows(made.ws, c), { initialProps: { c: cid } })
    await act(async () => { await Promise.resolve() })
    return { ...made, ...hook }
  }

  it('keeps a reorder, drops a closed window\'s key, and starts another chat with its own dock', async () => {
    const { emit, result, rerender } = await mount()
    act(() => { emit(url(1)); emit(url(2)) })
    const [a, b] = result.current.windows.map((w) => w.id)
    act(() => { result.current.setDockOrder([`win:${b}`, 'doc:f1', `win:${a}`]) })
    expect(result.current.dockOrder).toEqual([`win:${b}`, 'doc:f1', `win:${a}`])
    act(() => { result.current.close(a) })
    expect(result.current.dockOrder).toEqual([`win:${b}`, 'doc:f1'])
    rerender({ c: 'c2' })
    expect(result.current.dockOrder).toEqual([])
  })

  it('keeps each chat\'s windows across a switch: minimized, open, closed, in their order', async () => {
    fetchPageMock.mockResolvedValue({
      messages: [row(1, 'user'), row(4, 'event', 'audio', audioEvt), row(6, 'event', 'images', imagesEvt)], has_more: false,
    })
    const { emit, result, rerender } = await mount()
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    act(() => { emit(url(7)) })
    const [audio, images, link] = result.current.windows.map((w) => w.id)
    act(() => { result.current.minimize(audio); result.current.minimize(images) })
    act(() => { result.current.close(link) })
    act(() => { result.current.setDockOrder([`win:${images}`, 'doc:f1', `win:${audio}`]) })

    rerender({ c: 'c2' })
    await act(async () => { await Promise.resolve() })
    expect(result.current.windows.map((w) => w.dbId)).toEqual([4, 6])
    rerender({ c: 'c1' })
    await act(async () => { await Promise.resolve() })
    expect(result.current.windows.map((w) => w.id)).toEqual([audio, images])
    expect(openIds(result)).toEqual([])
    expect(result.current.dockOrder).toEqual([`win:${images}`, 'doc:f1', `win:${audio}`])

    act(() => { result.current.restore(images) })
    rerender({ c: '' })
    rerender({ c: 'c1' })
    await act(async () => { await Promise.resolve() })
    expect(openIds(result)).toEqual([images])
  })

  it('on a phone a return opens nothing when every window was minimized', async () => {
    narrow = true
    fetchPageMock.mockResolvedValue({ messages: [row(1, 'user'), row(4, 'event', 'audio', audioEvt)], has_more: false })
    const { result, rerender } = await mount()
    await waitFor(() => expect(result.current.windows).toHaveLength(1))
    act(() => { result.current.minimize(result.current.windows[0].id) })
    rerender({ c: 'c2' })
    rerender({ c: 'c1' })
    await act(async () => { await Promise.resolve() })
    expect(result.current.windows).toHaveLength(1)
    expect(openIds(result)).toEqual([])
  })

  it('opens on return what a turn made while the chat was not on show', async () => {
    const { emit, result, rerender } = await mount()
    act(() => { emit({ ...audioEvt, db_message_id: 4 }) })
    const audio = result.current.windows[0].id
    act(() => { result.current.minimize(audio) })
    rerender({ c: 'c2' })
    fetchPageMock.mockResolvedValue({
      messages: [row(1, 'user'), row(4, 'event', 'audio', audioEvt), row(6, 'event', 'images', imagesEvt)], has_more: false,
    })
    rerender({ c: 'c1' })
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    expect(result.current.minimized.has(audio)).toBe(true)
    expect(openIds(result)).toEqual([result.current.windows[1].id])
    expect(result.current.windows[1].dbId).toBe(6)
  })

  it('a change made for the chat just left never lands on the one on show', async () => {
    const { emit, result, rerender } = await mount()
    act(() => { emit(url(1)) })
    const minimizeOnC1 = result.current.minimize
    const id = result.current.windows[0].id
    rerender({ c: 'c2' })
    act(() => { minimizeOnC1(id) })
    expect(result.current.minimized.size).toBe(0)
    rerender({ c: 'c1' })
    expect(openIds(result)).toEqual([id])
  })

  it('numbers windows apart across chats and mounts', async () => {
    const one = await mount()
    act(() => { one.emit(url(1)) })
    fetchPageMock.mockResolvedValue({ messages: [row(1, 'user'), row(4, 'event', 'audio', audioEvt)], has_more: false })
    const two = await mount('c9')
    await waitFor(() => expect(two.result.current.windows).toHaveLength(1))
    expect(two.result.current.windows[0].id).not.toBe(one.result.current.windows[0].id)
  })

  it('on a desktop every window stays open', async () => {
    const { emit, result } = await mount()
    act(() => { emit(url(1)); emit(url(2)) })
    expect(openIds(result)).toHaveLength(2)
  })

  it('on a phone a new window minimizes the open one, and a restore does the same', async () => {
    narrow = true
    const { emit, result } = await mount()
    act(() => { emit(url(1)) })
    act(() => { emit(url(2)) })
    const [a, b] = result.current.windows.map((w) => w.id)
    expect(openIds(result)).toEqual([b])
    act(() => { result.current.restore(a) })
    expect(openIds(result)).toEqual([a])
    expect(result.current.minimized.has(b)).toBe(true)
  })

  it('on a phone an in-place replace changes nothing', async () => {
    narrow = true
    const { emit, result } = await mount()
    act(() => { emit({ type: 'ui', token: 'a', ui_url: '/v1/ui/a', title: 'Board', path: 'ws/b.html' }) })
    act(() => { emit(url(2)) })
    act(() => { emit({ type: 'ui', token: 'b', ui_url: '/v1/ui/b', title: 'Board 2', path: 'ws/b.html' }) })
    expect(result.current.windows).toHaveLength(2)
    expect(openIds(result)).toEqual([result.current.windows[1].id])
  })

  it('a replay on a phone opens only its newest window, and none when the pane is open', async () => {
    narrow = true
    fetchPageMock.mockResolvedValue({
      messages: [row(1, 'user'), row(4, 'event', 'audio', audioEvt), row(6, 'event', 'images', imagesEvt)], has_more: false,
    })
    const { result } = await mount()
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    expect(openIds(result)).toEqual([result.current.windows[1].id])

    useDocumentPaneStore.getState().openFile('c3', 'fa')
    const other = await mount('c3')
    await waitFor(() => expect(other.result.current.windows).toHaveLength(2))
    expect(openIds(other.result)).toEqual([])
  })

  it('on a phone a new window puts the shown pane away, and waits when the pane holds edits', async () => {
    narrow = true
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    const { emit, result } = await mount()
    act(() => { emit(url(1)) })
    expect(paneChat('c1').minimized).toBe(true)
    expect(openIds(result)).toHaveLength(1)

    act(() => { useDocumentPaneStore.getState().restore('c1') })
    expect(openIds(result)).toHaveLength(0)
    useDocumentPushStore.getState().setDirty('c1', 'fa', 'a.docx')
    act(() => { emit(url(2)) })
    expect(paneChat('c1').minimized).toBe(false)
    expect(openIds(result)).toHaveLength(0)
  })

  it('the pane and a window take turns on a phone, every time', async () => {
    narrow = true
    const { emit, result } = await mount()
    act(() => { emit(url(1)) })
    const a = result.current.windows[0].id
    act(() => { useDocumentPaneStore.getState().openFile('c1', 'fa') })
    expect(openIds(result)).toEqual([])
    act(() => { result.current.restore(a) })
    expect(paneChat('c1').minimized).toBe(true)
    act(() => { useDocumentPaneStore.getState().restore('c1') })
    expect(openIds(result)).toEqual([])
    act(() => { result.current.restore(a) })
    expect(paneChat('c1').minimized).toBe(true)
    expect(openIds(result)).toEqual([a])
  })
})
