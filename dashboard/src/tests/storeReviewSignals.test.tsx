/**
 * The store rating's signals from outside its module: the chat list says
 * whether it is at its end (and stops saying so when it goes), and a card
 * press that is refused or fails opens the quiet window.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const h = vi.hoisted(() => ({ answer: null as (() => Promise<unknown>) | null }))
vi.mock('@/lib/device/storeReview', () => ({
  noteChatAtBottom: vi.fn(),
  noteStoreReviewError: vi.fn(),
}))
vi.mock('@/lib/http/apiJson', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/http/apiJson')>()),
  apiJson: vi.fn(async () => (h.answer ? h.answer() : {})),
}))

import { noteChatAtBottom, noteStoreReviewError } from '@/lib/device/storeReview'
import { useActOnCard } from '@/api/cards'
import ChatMessages from '@/components/chat/ChatMessages'
import { OUTCOME } from '@/lib/kinds/card'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)

function withQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.mocked(noteChatAtBottom).mockClear()
  vi.mocked(noteStoreReviewError).mockClear()
  h.answer = null
})

describe('the chat list', () => {
  it('says it is at its end for its chat, and not when it goes', () => {
    const view = render(
      <ChatMessages
        chatId="c1"
        agentName="test-agent"
        messages={[{ id: 'db-1', role: 'assistant', blocks: [{ type: 'text', content: 'ready' }], createdAt: '2026-10-29T10:00:00+00:00' }]}
        onPermissionRespond={() => {}}
      />,
      { wrapper: withQuery() },
    )
    expect(noteChatAtBottom).toHaveBeenLastCalledWith('c1', true)
    view.unmount()
    expect(noteChatAtBottom).toHaveBeenLastCalledWith('c1', false)
  })
})

describe('a card press', () => {
  it('refused, it opens the quiet window', async () => {
    h.answer = async () => { throw new Error('A card shows only in a chat with a person in it.') }
    const { result } = renderHook(() => useActOnCard('card-1'), { wrapper: withQuery() })
    await act(async () => { await result.current.mutateAsync({ button: 'go' }).catch(() => {}) })
    expect(noteStoreReviewError).toHaveBeenCalledTimes(1)
  })

  it('done, it opens nothing; failed, it opens the quiet window', async () => {
    h.answer = async () => ({ kind: 'do', outcome: { status: OUTCOME.DONE, text: 'Done.' }, state: {} })
    const { result } = renderHook(() => useActOnCard('card-1'), { wrapper: withQuery() })
    await act(async () => { await result.current.mutateAsync({ button: 'go' }) })
    expect(noteStoreReviewError).not.toHaveBeenCalled()
    h.answer = async () => ({ kind: 'do', outcome: { status: OUTCOME.FAILED, text: 'It did not run.' }, state: {} })
    await act(async () => { await result.current.mutateAsync({ button: 'go' }) })
    expect(noteStoreReviewError).toHaveBeenCalledTimes(1)
  })
})
