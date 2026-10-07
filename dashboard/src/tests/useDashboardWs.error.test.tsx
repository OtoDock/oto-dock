/**
 * An `error` frame that names the viewed chat ends its turn on the client:
 * the socket-level streaming flag drops and the chat goes ready, so the next
 * send starts a new turn instead of steering into one the client still
 * believes is running. A bare error (one refused message, no chat_id) and an
 * error for another chat change nothing.
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
  onclose: ((e?: { code: number; reason: string }) => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  constructor(public url: string) { FakeWebSocket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() { /* the test drives onclose */ }
}

function openSocket(viewedChatId: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const onError = vi.fn()
  const hook = renderHook(() => useDashboardWs({ viewedChatId, onError }), { wrapper })
  act(() => { hook.result.current.connect() })
  const ws = FakeWebSocket.instances[0]
  act(() => { ws.onopen?.() })
  return { hook, ws, onError }
}

function frame(ws: FakeWebSocket, obj: Record<string, unknown>) {
  act(() => { ws.onmessage?.({ data: JSON.stringify(obj) }) })
}

describe('an error frame ending the viewed chat', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    useChatStore.setState({ byChat: {} })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('clears streaming and sets the chat ready, like done', () => {
    const { hook, ws, onError } = openSocket('chat-1')
    act(() => { hook.result.current.sendMessage('hi', 'chat-1') })
    expect(hook.result.current.streaming).toBe(true)
    expect(useChatStore.getState().byChat['chat-1'].status).toBe('streaming')
    frame(ws, { type: 'error', message: 'the engine went silent', reason: 'silent', resets_at: '', chat_id: 'chat-1' })
    expect(hook.result.current.streaming).toBe(false)
    expect(useChatStore.getState().byChat['chat-1'].status).toBe('ready')
    expect(onError).toHaveBeenCalledWith('the engine went silent', { reason: 'silent', resets_at: '' })
  })

  it('a bare error leaves the turn running', () => {
    const { hook, ws } = openSocket('chat-1')
    act(() => { hook.result.current.sendMessage('hi', 'chat-1') })
    frame(ws, { type: 'error', message: 'Too many queued messages' })
    expect(hook.result.current.streaming).toBe(true)
    expect(useChatStore.getState().byChat['chat-1'].status).toBe('streaming')
  })

  it('an error for another chat is dropped by the gate', () => {
    const { hook, ws, onError } = openSocket('chat-1')
    act(() => { hook.result.current.sendMessage('hi', 'chat-1') })
    frame(ws, { type: 'error', message: 'lost', reason: 'lost', chat_id: 'chat-2' })
    expect(hook.result.current.streaming).toBe(true)
    expect(onError).not.toHaveBeenCalled()
  })
})
