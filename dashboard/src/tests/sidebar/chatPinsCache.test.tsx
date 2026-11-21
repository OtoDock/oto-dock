import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

// ─── A person's pinned chats: the view's list and the pin's cache update ────

const apiJsonMock = vi.fn()
const apiOkMock = vi.fn()
vi.mock('@/lib/http/apiJson', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/http/apiJson')>(),
  apiJson: (...args: unknown[]) => apiJsonMock(...args),
  apiOk: (...args: unknown[]) => apiOkMock(...args),
}))

import {
  PINNED_VIEW, loadOlderChats, usePinnedChatList, useSearchChats, useSetChatPinned, useDeleteChat,
  type Chat, type ChatList,
} from '@/api/chats'
import { ApiError } from '@/lib/http/apiJson'

function row(id: string, at: number, over: Partial<Chat> = {}): Chat {
  return {
    id, user_sub: 'u', agent: 'dev', title: id, session_id: null,
    permission_mode: 'default', execution_path: '',
    created_at: '2026-10-01T00:00:00+00:00',
    updated_at: `2026-10-01T10:00:${String(at).padStart(2, '0')}.5+00:00`,
    can_pin: true, pinned: false, ...over,
  }
}
const ids = (list?: ChatList) => list?.chats.map((c) => c.id)
const page = (rows: Chat[], has_more: boolean) => ({ chats: rows, has_more })
const PINNED_KEY = ['chats', 'dev', PINNED_VIEW]

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}
function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

beforeEach(() => {
  apiJsonMock.mockReset()
  apiOkMock.mockReset()
})

describe('the pinned view', () => {
  it('asks the history route with pinned=1 and caches under the chat list key', async () => {
    const qc = client()
    apiJsonMock.mockResolvedValueOnce(page([row('p1', 5, { pinned: true })], false))
    const { result } = renderHook(() => usePinnedChatList('dev'), { wrapper: wrapper(qc) })
    await waitFor(() => expect(result.current.data?.map((c) => c.id)).toEqual(['p1']))
    const url = new URL(apiJsonMock.mock.calls[0][0] as string, 'http://x')
    expect(url.pathname).toBe('/v1/chats')
    expect(url.searchParams.get('agent')).toBe('dev')
    expect(url.searchParams.get('pinned')).toBe('1')
    expect(url.searchParams.get('kind')).toBeNull()
    expect(ids(qc.getQueryData(PINNED_KEY))).toEqual(['p1'])
  })

  it('pages like the history, its cursor after the last pinned row', async () => {
    const qc = client()
    qc.setQueryData(PINNED_KEY, page([row('p9', 9), row('p7', 7)], true))
    apiJsonMock.mockResolvedValueOnce(page([row('p3', 3)], false))
    await loadOlderChats(qc, PINNED_VIEW, 'dev')
    const url = new URL(apiJsonMock.mock.calls[0][0] as string, 'http://x')
    expect(url.searchParams.get('pinned')).toBe('1')
    expect(url.searchParams.get('before_chat_id')).toBe('p7')
    expect(ids(qc.getQueryData(PINNED_KEY))).toEqual(['p9', 'p7', 'p3'])
  })

  it('searches the pinned view with pinned=1', async () => {
    const qc = client()
    apiJsonMock.mockResolvedValueOnce({ chats: [] })
    renderHook(() => useSearchChats('dev', 'quay', PINNED_VIEW), { wrapper: wrapper(qc) })
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled())
    const url = new URL(apiJsonMock.mock.calls[0][0] as string, 'http://x')
    expect(url.pathname).toBe('/v1/chats/search')
    expect(url.searchParams.get('pinned')).toBe('1')
    expect(url.searchParams.get('kind')).toBeNull()
  })

  it('a delete drops the row from the pinned view too', async () => {
    const qc = client()
    qc.setQueryData(['chats', 'dev'], page([row('a', 9), row('b', 8)], false))
    qc.setQueryData(PINNED_KEY, page([row('b', 8, { pinned: true })], false))
    apiOkMock.mockResolvedValue(undefined)
    apiJsonMock.mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useDeleteChat(), { wrapper: wrapper(qc) })
    await act(async () => { await result.current.mutateAsync('b') })
    expect(ids(qc.getQueryData(PINNED_KEY))).toEqual([])
  })
})

describe('useSetChatPinned', () => {
  it('pins: PUT, the flag flips in the lists and the search, nothing inserted in the paged view', async () => {
    const qc = client()
    qc.setQueryData(['chats', 'dev'], page([row('a', 9), row('b', 8)], true))
    qc.setQueryData(PINNED_KEY, page([row('z', 9, { pinned: true })], true))
    qc.setQueryData(['chat-search', 'dev', 'word', 'chats'], [row('b', 8)])
    let answer: (v: unknown) => void = () => {}
    apiJsonMock.mockImplementation((path: string) => (path.endsWith('/pinned')
      ? new Promise((r) => { answer = r })
      : new Promise(() => {})))
    const { result } = renderHook(() => useSetChatPinned(), { wrapper: wrapper(qc) })
    act(() => { result.current.mutate({ chatId: 'b', pinned: true }) })
    await waitFor(() => expect(qc.getQueryData<ChatList>(['chats', 'dev'])?.chats[1].pinned).toBe(true))
    expect(qc.getQueryData<Chat[]>(['chat-search', 'dev', 'word', 'chats'])?.[0].pinned).toBe(true)
    expect(ids(qc.getQueryData(PINNED_KEY))).toEqual(['z'])
    const [path, init] = apiJsonMock.mock.calls.find((c) => (c[0] as string).endsWith('/pinned'))!
    expect(path).toBe('/v1/chats/b/pinned')
    expect((init as RequestInit).method).toBe('PUT')
    await act(async () => { answer({ pinned: true }) })
  })

  it('unpins: DELETE, the row leaves the pinned view and its search', async () => {
    const qc = client()
    qc.setQueryData(['chats', 'dev'], page([row('a', 9, { pinned: true })], false))
    qc.setQueryData(PINNED_KEY, page([row('a', 9, { pinned: true }), row('c', 4, { pinned: true })], false))
    qc.setQueryData(['chat-search', 'dev', 'word', PINNED_VIEW], [row('a', 9, { pinned: true })])
    apiJsonMock.mockImplementation((path: string) => (path.endsWith('/pinned')
      ? Promise.resolve({ pinned: false }) : new Promise(() => {})))
    const { result } = renderHook(() => useSetChatPinned(), { wrapper: wrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ chatId: 'a', pinned: false }) })
    expect(qc.getQueryData<ChatList>(['chats', 'dev'])?.chats[0].pinned).toBe(false)
    expect(ids(qc.getQueryData(PINNED_KEY))).toEqual(['c'])
    expect(qc.getQueryData<Chat[]>(['chat-search', 'dev', 'word', PINNED_VIEW])).toEqual([])
    const call = apiJsonMock.mock.calls.find((c) => (c[0] as string).endsWith('/pinned'))!
    expect((call[1] as RequestInit).method).toBe('DELETE')
  })

  it('puts the rows back and says why when the server refuses', async () => {
    const qc = client()
    qc.setQueryData(['chats', 'dev'], page([row('a', 9)], false))
    apiJsonMock.mockImplementation((path: string) => (path.endsWith('/pinned')
      ? Promise.reject(new ApiError(404, 'Chat not found', 'Chat not found', { detail: 'Chat not found' }))
      : new Promise(() => {})))
    const { result } = renderHook(() => useSetChatPinned(), { wrapper: wrapper(qc) })
    let message = ''
    await act(async () => {
      await result.current.mutateAsync({ chatId: 'a', pinned: true }).catch((e: Error) => { message = e.message })
    })
    expect(message).toBe('Chat not found')
    expect(qc.getQueryData<ChatList>(['chats', 'dev'])?.chats[0].pinned).toBe(false)
  })
})
