import { describe, it, expect, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { MAX_KEPT_DOCKS, forgetDock, keepDock, keptDock, nextWindowId } from '@/store/artifactDockStore'
import type { DockState } from '@/hooks/useArtifactWindows'
import { useDeleteChat } from '@/api/chats'

vi.mock('@/lib/http/apiJson', async (orig) => ({
  ...(await orig<typeof import('@/lib/http/apiJson')>()),
  apiOk: vi.fn(async () => undefined),
}))

const dock = (n: number): DockState => ({
  windows: [{ id: n, title: `W${n}`, block: { type: 'url', url: `https://x/${n}`, title: `W${n}`, description: '' } }],
  minimized: new Set([n]), order: [`win:${n}`], opened: null,
})

describe('the artifact windows kept per chat', () => {
  it('keeps the most recent chats only, and forgets a chat on demand', () => {
    for (let i = 0; i <= MAX_KEPT_DOCKS; i++) keepDock(`chat-${i}`, dock(i))
    expect(keptDock('chat-0')).toBeUndefined()
    expect(keptDock(`chat-${MAX_KEPT_DOCKS}`)?.order).toEqual([`win:${MAX_KEPT_DOCKS}`])
    forgetDock(`chat-${MAX_KEPT_DOCKS}`)
    expect(keptDock(`chat-${MAX_KEPT_DOCKS}`)).toBeUndefined()
    keepDock('', dock(1))
    expect(keptDock('')).toBeUndefined()
  })

  it('counts window ids up for the whole page', () => {
    const a = nextWindowId()
    expect(nextWindowId()).toBe(a + 1)
  })

  it('drops a deleted chat\'s windows', async () => {
    keepDock('gone', dock(3))
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const { result } = renderHook(() => useDeleteChat(), { wrapper })
    await act(async () => { await result.current.mutateAsync('gone') })
    await waitFor(() => expect(keptDock('gone')).toBeUndefined())
  })
})
