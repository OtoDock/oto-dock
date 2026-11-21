// The chat stream on the real socket, from the new-chat view: a warm-up or a
// history of another chat is never adopted (the view is not carried away to
// it), the chat the view's own send minted still is, and leaving a chat for
// the new-chat view tells the socket.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { type ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FakeWebSocket } from '@/tests/fakeWebSocket'
import { useChatStream } from '@/hooks/useChatStream'

beforeEach(() => {
  FakeWebSocket.instances.length = 0
  vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
})

function open(initialChatId: string | null, extra: Record<string, unknown> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const hook = renderHook(() => useChatStream({
    agents: [], initialChatId, queue: { addQueued: vi.fn(), clearQueued: vi.fn() }, ...extra,
  }), { wrapper })
  act(() => { hook.result.current.ws.connect() })
  const ws = FakeWebSocket.instances[0]
  act(() => { ws.onopen?.() })
  return { hook, ws }
}

function frame(ws: FakeWebSocket, msg: Record<string, unknown>) {
  act(() => { ws.onmessage?.({ data: JSON.stringify(msg) }) })
}

const OTHER_HISTORY = {
  type: 'chat_history', chat_id: 'chat-orchestrator', agent: 'developer', has_more: false,
  messages: [{ id: 1, chat_id: 'chat-orchestrator', role: 'assistant', content: 'the orchestrator speaks',
               event_type: null, event_data: null, created_at: '2026-11-18T18:52:04Z' }],
}

describe('useChatStream: the new-chat view keeps to its own chat', () => {
  it("adopts no other chat's warm-up or history and goes nowhere", () => {
    const onWarmupReadyExtra = vi.fn()
    const { hook, ws } = open(null, { onWarmupReadyExtra })
    frame(ws, { type: 'warmup_ready', chat_id: 'chat-orchestrator', session_id: 's-o', mode: 'auto' })
    frame(ws, OTHER_HISTORY)
    expect(hook.result.current.chatId).toBe(null)
    expect(onWarmupReadyExtra).not.toHaveBeenCalled()
    expect(hook.result.current.messages).toEqual([])
  })

  it('still adopts the chat its own send minted', () => {
    const onWarmupReadyExtra = vi.fn()
    const { hook, ws } = open(null, { onWarmupReadyExtra })
    frame(ws, { type: 'warmup_started', chat_id: 'chat-new', agent: 'developer', new_chat: true })
    frame(ws, { type: 'warmup_ready', chat_id: 'chat-new', session_id: 's-n', mode: 'auto' })
    expect(hook.result.current.chatId).toBe('chat-new')
    expect(onWarmupReadyExtra).toHaveBeenCalledTimes(1)
  })

  it('tells the socket when the view leaves its chat for the new-chat view', () => {
    const { hook, ws } = open('chat-a')
    const leaves = () => ws.sent.map((s) => JSON.parse(s)).filter((m) => m.type === 'leave_chat')
    expect(leaves()).toHaveLength(0)
    act(() => { hook.result.current.setChatId(null) })
    expect(leaves()).toHaveLength(1)
    act(() => { hook.result.current.setChatId('chat-b') })
    expect(leaves()).toHaveLength(1)
  })
})
