import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'

import { replayableDisplayEvents, MAX_REPLAY_WINDOWS } from '@/lib/displayReplay'
import { loadDismissedPips, recordDismissedPip } from '@/lib/pipDismissals'
import { useArtifactWindows } from '@/hooks/useArtifactWindows'
import { fetchChatPage, type ChatMessage } from '@/api/chats'

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

  it('dedupes document_preview by file_id and ui by path — latest wins', () => {
    const prev = (id: number, wopi: string) =>
      row(id, 'event', 'document_preview',
        { type: 'document_preview', wopi_url: wopi, filename: 'f.docx', file_id: 'fid', download_url: '/d' })
    const ui = (id: number, token: string) =>
      row(id, 'event', 'ui', { type: 'ui', token, ui_url: `/v1/ui/${token}`, path: 'ws/board.html' })
    const out = replayableDisplayEvents(
      [row(1, 'user'), prev(2, 'w1'), ui(3, 'a'), prev(4, 'w2'), ui(5, 'b')],
      new Set(),
    )
    expect(out.map((r) => r.dbId)).toEqual([4, 5])
    expect((out[0].block as any).wopiUrl).toBe('w2')
    expect((out[1].block as any).token).toBe('b')
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
    emit: (event: any) => handlers.get('pty_artifact')?.({ chat_id: 'c1', event }),
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

  it('minimize is NOT a dismissal — the window replays on the next attach', async () => {
    fetchPageMock.mockResolvedValue(finalTurnPage)
    const { ws } = makeWs()
    const { result, rerender } = renderHook(
      ({ cid }: { cid: string }) => useArtifactWindows(ws, cid),
      { initialProps: { cid: 'c1' } },
    )
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    act(() => { result.current.minimize(result.current.windows[0].id) })
    rerender({ cid: '' })
    rerender({ cid: 'c1' })
    await waitFor(() => expect(result.current.windows).toHaveLength(2))
    expect(loadDismissedPips('c1').size).toBe(0)
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
