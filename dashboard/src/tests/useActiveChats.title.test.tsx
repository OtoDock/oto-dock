import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useActiveChats, _reseedAttempts, _reseedPhases } from '@/hooks/useActiveChats'
import { useChatStore } from '@/store/chatStore'

// ─── useActiveChats: title resolution when the seed has no row ──────────────
// The seed lists only chats whose turn the BACKEND sees open; an interactive
// turn the CLI has not journaled yet is invisible there, so the row used to
// read "New chat" for a titled chat — and stayed so once the four re-seed
// attempts were spent (operator screenshots, 2026-09-10).

const seedMock = vi.fn<() => Promise<unknown[]>>(async () => [])
vi.mock('@/api/chats', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/api/chats')>()
  return { ...orig, fetchActiveChats: () => seedMock() }
})

function harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { qc, wrapper }
}

function streamingSlice(id: string, agent: string) {
  useChatStore.getState().setStreaming(id)
  useChatStore.setState((s) => ({
    byChat: { ...s.byChat, [id]: { ...s.byChat[id], agent } },
  }))
}

describe('useActiveChats title fallback', () => {
  beforeEach(() => {
    seedMock.mockReset()
    seedMock.mockResolvedValue([])
    _reseedAttempts.clear()
    _reseedPhases.clear()
    useChatStore.setState({ byChat: {} })
  })

  it('uses the cached chat list title when the seed has no row', async () => {
    const { qc, wrapper } = harness()
    qc.setQueryData(['chats', 'alpha'], [{ id: 'c1', agent: 'alpha', title: 'Release Workflow Improvements' }])
    streamingSlice('c1', 'alpha')
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(result.current.length).toBe(1))
    expect(result.current[0].title).toBe('Release Workflow Improvements')
    expect(result.current[0].agent).toBe('alpha')
  })

  it('falls back to "New chat" only when nothing knows the title', async () => {
    const { wrapper } = harness()
    streamingSlice('c2', 'alpha')
    const { result } = renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(result.current.length).toBe(1))
    expect(result.current[0].title).toBe('New chat')
  })

  it('a phase transition resets the spent re-seed attempts', async () => {
    const { wrapper } = harness()
    _reseedAttempts.set('c3', { count: 4, lastAt: Date.now() })
    _reseedPhases.set('c3', 'warming')
    streamingSlice('c3', 'alpha')  // warming → streaming: a new episode
    renderHook(() => useActiveChats(), { wrapper })
    await waitFor(() => expect(_reseedPhases.get('c3')).toBe('streaming'))
    // The cap is cleared; the spacing from the last attempt still holds, so
    // no burst fires inside the 2 s window.
    await waitFor(() => expect(_reseedAttempts.get('c3')?.count).toBe(0))
  })
})
