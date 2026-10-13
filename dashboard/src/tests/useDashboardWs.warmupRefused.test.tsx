/**
 * A send below the editor role on a Shared-only agent is refused before a
 * chat is minted: `warmup_failed` arrives with no `chat_id`. The page still
 * hears it (the role card), and no store slice is made for an id that does
 * not exist.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { useChatStore } from '@/store/chatStore'

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
  constructor(public url: string) { FakeWebSocket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() { /* keeps reconnect logic out of the test */ }
}

describe('a warmup refused before the chat exists', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('reaches the page and makes no slice for a missing id', () => {
    const onWarmupFailed = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(() => useDashboardWs({ onWarmupFailed }), { wrapper })
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    const before = Object.keys(useChatStore.getState().byChat)
    act(() => {
      ws.onmessage?.({ data: JSON.stringify({
        type: 'warmup_failed', reason: 'below_editor',
        error: 'This agent is set to Shared only, so its chats and tasks run as the agent itself',
      }) })
    })
    expect(onWarmupFailed).toHaveBeenCalledWith(expect.objectContaining({ reason: 'below_editor' }))
    expect(Object.keys(useChatStore.getState().byChat)).toEqual(before)
    expect(useChatStore.getState().byChat['undefined']).toBeUndefined()
  })
})
