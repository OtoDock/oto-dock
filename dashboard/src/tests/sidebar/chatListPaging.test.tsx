import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

// ─── The sidebar lists page: the first-page merge, the older pages ───────────

const apiJsonMock = vi.fn()
const apiOkMock = vi.fn()
vi.mock('@/lib/http/apiJson', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/http/apiJson')>(),
  apiJson: (...args: unknown[]) => apiJsonMock(...args),
  apiOk: (...args: unknown[]) => apiOkMock(...args),
}))

import {
  mergeFirstPage, loadOlderChats, useChats, useChatListPaging, useRenameChat, useDeleteChat,
  type Chat, type ChatList,
} from '@/api/chats'

function row(id: string, at: number): Chat {
  return {
    id, user_sub: 'u', agent: 'dev', title: id, session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: '2026-10-01T00:00:00+00:00',
    updated_at: `2026-10-01T10:00:${String(at).padStart(2, '0')}.5+00:00`,
  }
}
const ids = (list?: ChatList) => list?.chats.map((c) => c.id)
const page = (rows: Chat[], has_more: boolean) => ({ chats: rows, has_more })

// Rows c0..c9, newest first: c9 is the newest.
const all = Array.from({ length: 10 }, (_, i) => row(`c${i}`, i)).reverse()

describe('mergeFirstPage', () => {
  const loaded = page(all.slice(0, 7), true)   // c9..c3 loaded, more below

  it('keeps the loaded rows below a fresh page that still touches them', () => {
    const merged = mergeFirstPage(page(all.slice(0, 3), true), loaded)
    expect(ids(merged)).toEqual(['c9', 'c8', 'c7', 'c6', 'c5', 'c4', 'c3'])
    expect(merged.has_more).toBe(true)
  })

  it('lists a chat that moved to the top once', () => {
    const moved = { ...row('c5', 59) }
    const merged = mergeFirstPage(page([moved, all[0], all[1]], true), loaded)
    expect(ids(merged)).toEqual(['c5', 'c9', 'c8', 'c7', 'c6', 'c4', 'c3'])
  })

  it('drops a chat that is gone from the fresh page range', () => {
    const merged = mergeFirstPage(page([all[0], all[2], all[3]], true), loaded)   // c8 deleted
    expect(ids(merged)).toEqual(['c9', 'c7', 'c6', 'c5', 'c4', 'c3'])
  })

  it('starts over when the fresh page no longer touches the loaded rows', () => {
    // More than a page changed while the tab was hidden: the fresh page's
    // last row moved, so the rows between it and the old ones are unknown.
    const fresh = page([row('n1', 59), row('n2', 58), { ...row('c7', 57) }], true)
    expect(mergeFirstPage(fresh, loaded)).toBe(fresh)
    const unknown = page([row('n1', 59), row('n2', 58), row('n3', 57)], true)
    expect(mergeFirstPage(unknown, loaded)).toBe(unknown)
  })

  it('takes a fresh page that holds the whole list as the list', () => {
    const whole = page(all.slice(0, 2), false)
    expect(mergeFirstPage(whole, loaded)).toBe(whole)
    expect(mergeFirstPage(whole, undefined)).toBe(whole)
  })
})

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('loadOlderChats', () => {
  beforeEach(() => apiJsonMock.mockReset())

  it('asks after the last row, the cursor in the URL with its plus, and appends the new rows', async () => {
    const qc = client()
    qc.setQueryData(['chats', 'dev'], page(all.slice(0, 4), true))
    apiJsonMock.mockResolvedValueOnce(page([all[3], ...all.slice(4, 7)], true))
    await loadOlderChats(qc, 'chats', 'dev')
    const url = new URL(apiJsonMock.mock.calls[0][0] as string, 'http://x')
    expect(url.pathname).toBe('/v1/chats')
    expect(url.searchParams.get('agent')).toBe('dev')
    expect(url.searchParams.get('before_chat_id')).toBe('c6')
    expect(url.searchParams.get('before_updated_at')).toBe(all[3].updated_at)
    expect(apiJsonMock.mock.calls[0][0]).toContain('%2B00%3A00')
    expect(ids(qc.getQueryData(['chats', 'dev']))).toEqual(['c9', 'c8', 'c7', 'c6', 'c5', 'c4', 'c3'])
  })

  it('asks once while a page is out, and nothing when no more rows remain', async () => {
    const qc = client()
    qc.setQueryData(['task-chats', 'dev'], page(all.slice(0, 2), true))
    let release: (v: unknown) => void = () => {}
    apiJsonMock.mockReturnValueOnce(new Promise((r) => { release = r }))
    const a = loadOlderChats(qc, 'tasks', 'dev')
    const b = loadOlderChats(qc, 'tasks', 'dev')
    expect(apiJsonMock).toHaveBeenCalledTimes(1)
    expect(new URL(apiJsonMock.mock.calls[0][0] as string, 'http://x').searchParams.get('kind')).toBe('tasks')
    release(page(all.slice(2, 4), false))
    await Promise.all([a, b])
    expect(qc.getQueryData<ChatList>(['task-chats', 'dev'])?.has_more).toBe(false)
    await loadOlderChats(qc, 'tasks', 'dev')
    expect(apiJsonMock).toHaveBeenCalledTimes(1)
  })

  it('drops the page when the list no longer ends at the row it asked after', async () => {
    const qc = client()
    qc.setQueryData(['chats', 'dev'], page(all.slice(0, 4), true))
    let release: (v: unknown) => void = () => {}
    apiJsonMock.mockReturnValueOnce(new Promise((r) => { release = r }))
    const job = loadOlderChats(qc, 'chats', 'dev')
    qc.setQueryData(['chats', 'dev'], page([row('n1', 59), ...all.slice(0, 2)], true))   // a merge started over
    release(page(all.slice(4, 6), true))
    await job
    expect(ids(qc.getQueryData(['chats', 'dev']))).toEqual(['n1', 'c9', 'c8'])
  })

  it('keeps the list and marks the paging failed when the page fails', async () => {
    const qc = client()
    qc.setQueryData(['chats', 'dev'], page(all.slice(0, 2), true))
    const { result } = renderHook(() => useChatListPaging('chats', 'dev'), { wrapper: wrapper(qc) })
    expect(result.current.hasMore).toBe(true)
    apiJsonMock.mockRejectedValueOnce(new Error('offline'))
    await act(async () => { result.current.loadOlder() })
    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(ids(qc.getQueryData(['chats', 'dev']))).toEqual(['c9', 'c8'])
    apiJsonMock.mockResolvedValueOnce(page(all.slice(2, 3), false))
    await act(async () => { result.current.loadOlder() })
    await waitFor(() => expect(result.current.failed).toBe(false))
    expect(result.current.hasMore).toBe(false)
  })
})

describe('useChats', () => {
  beforeEach(() => { apiJsonMock.mockReset(); apiOkMock.mockReset() })

  it('a refetch asks the first page only and keeps the pages below it', async () => {
    const qc = client()
    apiJsonMock.mockResolvedValueOnce(page(all.slice(0, 3), true))
    const { result } = renderHook(() => useChats('dev'), { wrapper: wrapper(qc) })
    await waitFor(() => expect(result.current.data?.length).toBe(3))
    apiJsonMock.mockResolvedValueOnce(page(all.slice(3, 6), true))
    await act(() => loadOlderChats(qc, 'chats', 'dev'))
    await waitFor(() => expect(result.current.data?.map((c) => c.id)).toEqual(['c9', 'c8', 'c7', 'c6', 'c5', 'c4']))
    const moved = row('c5', 59)
    apiJsonMock.mockResolvedValueOnce(page([moved, all[0], all[1]], true))
    await act(async () => { await result.current.refetch() })
    expect(apiJsonMock).toHaveBeenCalledTimes(3)
    expect(apiJsonMock.mock.calls[2][0]).toBe('/v1/chats?agent=dev')
    await waitFor(() => expect(result.current.data?.map((c) => c.id)).toEqual(['c5', 'c9', 'c8', 'c7', 'c6', 'c4']))
  })

  it('a page appended while a poll is out survives a rename that cancels the poll', async () => {
    const qc = client()
    apiJsonMock.mockResolvedValueOnce(page(all.slice(0, 3), true))
    const { result } = renderHook(() => ({ list: useChats('dev'), rename: useRenameChat() }),
      { wrapper: wrapper(qc) })
    await waitFor(() => expect(result.current.list.data?.length).toBe(3))
    apiJsonMock.mockReturnValueOnce(new Promise(() => {}))   // the poll, still out
    void result.current.list.refetch()
    apiJsonMock.mockResolvedValueOnce(page(all.slice(3, 5), false))
    await act(() => loadOlderChats(qc, 'chats', 'dev'))
    // The rename's PATCH answers; the refetch its settle starts stays out.
    apiJsonMock.mockImplementation((_url: string, init?: RequestInit) =>
      init?.method === 'PATCH' ? Promise.resolve({ title: 'Renamed' }) : new Promise(() => {}))
    await act(async () => { result.current.rename.mutate({ chatId: 'c5', title: 'Renamed' }) })
    await waitFor(() =>
      expect(qc.getQueryData<ChatList>(['chats', 'dev'])?.chats.find((c) => c.id === 'c5')?.title).toBe('Renamed'))
    expect(ids(qc.getQueryData(['chats', 'dev']))).toEqual(['c9', 'c8', 'c7', 'c6', 'c5'])
  })

  it('a delete drops the row from a loaded page below the first', async () => {
    const qc = client()
    qc.setQueryData(['chats', 'dev'], page(all.slice(0, 6), true))
    qc.setQueryData(['task-chats', 'dev'], page(all.slice(6, 8), false))
    apiOkMock.mockResolvedValue(undefined)
    apiJsonMock.mockReturnValue(new Promise(() => {}))   // the invalidation's refetch stays out
    const { result } = renderHook(() => useDeleteChat(), { wrapper: wrapper(qc) })
    await act(async () => { await result.current.mutateAsync('c5') })
    expect(ids(qc.getQueryData(['chats', 'dev']))).toEqual(['c9', 'c8', 'c7', 'c6', 'c4'])
    expect(ids(qc.getQueryData(['task-chats', 'dev']))).toEqual(['c3', 'c2'])
  })
})
