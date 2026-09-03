import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { useActiveChats, FINISHED_LINGER_MS, _reseedAttempts } from '@/hooks/useActiveChats'
import { useDashboardWs } from '@/hooks/useDashboardWs'
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
  const IDS = ['h1', 'h2', 'h3', 'task-run-77']
  beforeEach(() => {
    resetStore(IDS)
    _reseedAttempts.clear() // module-scoped retry state must not leak across tests
  })
  afterEach(() => {
    resetStore(IDS)
    _reseedAttempts.clear()
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

  it('copies the seed owner_is_shared flag onto the derived row; absent stays undefined', async () => {
    seedSpy.mockResolvedValue([
      { id: 'h1', agent: 'beta', title: 'Legacy shared', status: 'streaming', owner_is_shared: true },
      { id: 'h2', agent: 'beta', title: 'Plain chat', status: 'streaming' },
    ])
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(result.current).toHaveLength(2))
    const byId = new Map(result.current.map((r) => [r.id, r]))
    expect(byId.get('h1')?.ownerIsShared).toBe(true)
    expect(byId.get('h2')?.ownerIsShared).toBeUndefined()
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

  it('a warming seed row is inert metadata — no phantom row without a store slice', async () => {
    // Warmup frames are per-socket, never broadcast: a non-initiating tab has
    // no store slice to retire a seed-asserted warming row with, so the seed's
    // 'warming' status must never count as active on its own (and never enter
    // the finished-linger path it was never live in).
    seedSpy.mockResolvedValue([
      { id: 'h1', agent: 'beta', title: 'Cold start', status: 'warming' },
    ])
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(seedSpy).toHaveBeenCalled())
    expect(result.current).toHaveLength(0)
    // …but its metadata is live: the INITIATING tab's store slice renders the
    // row with the seed-supplied title instead of the placeholder.
    act(() => useChatStore.getState().beginWarmup('h1', { agent: 'beta' }))
    await waitFor(() => expect(result.current[0]).toMatchObject({
      id: 'h1', agent: 'beta', title: 'Cold start', phase: 'warming',
    }))
  })

  it('classifies task-run- ids as tasks before any seed metadata exists', async () => {
    seedSpy.mockResolvedValue([])
    act(() => {
      useChatStore.getState().beginWarmup('task-run-77', { agent: 'beta' })
      useChatStore.getState().setStreaming('task-run-77')
    })
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(result.current).toHaveLength(1))
    expect(result.current[0]).toMatchObject({
      id: 'task-run-77', sourceType: 'task', phase: 'streaming',
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

  it('re-seeds for a store-known chat so its real title replaces New chat', async () => {
    // Regression (2026-07-16): a chat that went live AFTER the seed has its
    // agent in the store (WS events) but no seed metadata — the old re-seed
    // condition was vetoed by the store agent, leaving the row permanently
    // titled "New chat" in the sidebar's task mode.
    seedSpy
      .mockResolvedValueOnce([]) // initial seed predates the chat
      .mockResolvedValue([
        { id: 'h2', agent: 'alpha', title: 'Stripe Live Mode Migration', status: 'streaming' },
      ])
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(seedSpy).toHaveBeenCalledTimes(1))
    act(() => {
      useChatStore.getState().beginWarmup('h2', { agent: 'alpha' })
      useChatStore.getState().setStreaming('h2')
    })
    // Renderable immediately from the store (title still the placeholder)…
    await waitFor(() => expect(result.current).toHaveLength(1))
    // …and the re-seed supplies the real title in ONE attempt here — the
    // retry cap never inflates the call count once metadata resolves.
    await waitFor(() =>
      expect(result.current[0]).toMatchObject({ id: 'h2', title: 'Stripe Live Mode Migration' }),
    )
    expect(seedSpy).toHaveBeenCalledTimes(2)
  })

  it('retries past an empty warming-window fetch instead of burning the id (Bug A)', async () => {
    // The exact live-observed regression: the refetch fired while the chat
    // was still WARMING returned an empty seed, and the old one-shot
    // `reseededIdsRef` burn meant the id never got another chance — the strip
    // showed "New chat" forever while the sidebar had the real title.
    vi.useFakeTimers()
    try {
      seedSpy
        .mockResolvedValueOnce([]) // initial seed predates the chat
        .mockResolvedValueOnce([]) // warming-window refetch races empty
        .mockResolvedValue([
          { id: 'h2', agent: 'alpha', title: 'Stripe Live Mode Migration', status: 'streaming' },
        ])
      const { result } = renderHook(() => useActiveChats(), { wrapper })
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(seedSpy).toHaveBeenCalledTimes(1)

      act(() => useChatStore.getState().beginWarmup('h2', { agent: 'alpha' }))
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      // Attempt 1 (status transition) got the empty warming-window seed.
      expect(seedSpy).toHaveBeenCalledTimes(2)
      expect(result.current[0]).toMatchObject({ id: 'h2', title: 'New chat', phase: 'warming' })

      act(() => useChatStore.getState().setStreaming('h2'))
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      // The streaming transition lands inside the 2s spacing — no burst.
      expect(seedSpy).toHaveBeenCalledTimes(2)

      // The short timer carries attempt 2, which now finds the open turn.
      await act(async () => { await vi.advanceTimersByTimeAsync(2_200) })
      expect(seedSpy).toHaveBeenCalledTimes(3)
      expect(result.current[0]).toMatchObject({
        id: 'h2', title: 'Stripe Live Mode Migration', phase: 'streaming',
      })
    } finally {
      vi.useRealTimers()
    }
  })
})

// ─── useDashboardWs title_updated fallback invalidation (per-id bounds) ──────

class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static instances: FakeWebSocket[] = []
  readyState = FakeWebSocket.OPEN
  sent: string[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  send(data: string) { this.sent.push(data) }
  close() { /* keep reconnect logic out of the test */ }
}

describe('useDashboardWs title_updated seed invalidation', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('unmatched frames for two chats within 5s BOTH invalidate the seed', () => {
    // Regression: the throttle was one shared timestamp — a rename burst on
    // chat X swallowed chat Y's only title frame, leaving Y's widget row
    // permanently untitled. The cooldown is per chat id now.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const wsWrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(() => useDashboardWs({}), { wrapper: wsWrapper })
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })

    const seedInvalidates = () =>
      invalidateSpy.mock.calls.filter(
        (c) => (c[0] as { queryKey?: unknown[] } | undefined)?.queryKey?.[0] === 'active-chats',
      ).length

    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'title_updated', chat_id: 'chat-x', title: 'X' }) }) })
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'title_updated', chat_id: 'chat-y', title: 'Y' }) }) })
    expect(seedInvalidates()).toBe(2)

    // Once-per-id bound: a repeat frame for an already-invalidated id stays
    // swallowed (the retry map in useActiveChats is the empty-race backstop).
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'title_updated', chat_id: 'chat-x', title: 'X2' }) }) })
    expect(seedInvalidates()).toBe(2)
  })
})
