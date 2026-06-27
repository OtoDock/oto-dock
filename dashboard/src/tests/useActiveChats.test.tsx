import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { useActiveChats, FINISHED_LINGER_MS } from '@/hooks/useActiveChats'
import { useChatStore } from '@/store/chatStore'
import * as chatsApi from '@/api/chats'

// ─── useActiveChats (derive logic; real store + mocked seed fetch) ───────────

const seedSpy = vi.spyOn(chatsApi, 'fetchActiveChats')

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function resetStore(ids: string[]) {
  useChatStore.setState((s) => {
    const byChat = { ...s.byChat }
    for (const id of ids) delete byChat[id]
    return { byChat }
  })
}

describe('useActiveChats', () => {
  const IDS = ['h1', 'h2', 'h3']
  beforeEach(() => resetStore(IDS))
  afterEach(() => {
    resetStore(IDS)
    seedSpy.mockReset()
  })

  it('seed-only rows render as streaming with server metadata', async () => {
    seedSpy.mockResolvedValue([
      { id: 'h1', agent: 'beta', title: 'Foreign turn', status: 'streaming' },
    ])
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(result.current).toHaveLength(1))
    expect(result.current[0]).toMatchObject({
      id: 'h1', agent: 'beta', title: 'Foreign turn', phase: 'streaming',
    })
  })

  it('a warming store slice renders from slice agent with New chat title', async () => {
    seedSpy.mockResolvedValue([])
    act(() => useChatStore.getState().beginWarmup('h2', { agent: 'alpha' }))
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(result.current).toHaveLength(1))
    expect(result.current[0]).toMatchObject({
      id: 'h2', agent: 'alpha', title: 'New chat', phase: 'warming',
    })
  })

  it('a ready store slice wins over a stale seed row', async () => {
    seedSpy.mockResolvedValue([
      { id: 'h3', agent: 'beta', title: 'Already done', status: 'streaming' },
    ])
    act(() => {
      useChatStore.getState().setStreaming('h3')
      useChatStore.getState().setReady('h3')
    })
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(seedSpy).toHaveBeenCalled())
    // never surfaces as active; a finished-linger row is acceptable but the
    // phase must not be streaming/warming
    expect(result.current.every((r) => r.phase === 'finished')).toBe(true)
  })

  it('a finished chat lingers briefly, then drops', async () => {
    seedSpy.mockResolvedValue([
      { id: 'h1', agent: 'beta', title: 'Long task', status: 'streaming' },
    ])
    act(() => useChatStore.getState().setStreaming('h1'))
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(result.current).toHaveLength(1))
    act(() => useChatStore.getState().setReady('h1'))
    await waitFor(() => expect(result.current[0]?.phase).toBe('finished'))
    await waitFor(
      () => expect(result.current).toHaveLength(0),
      { timeout: FINISHED_LINGER_MS + 2_000 },
    )
  })
})
