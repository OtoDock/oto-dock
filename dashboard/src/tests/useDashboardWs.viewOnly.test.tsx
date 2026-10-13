/**
 * A chat a phone call drives: the resume is answered with `chat_view_only`
 * before its read-only history. While it holds, a `streaming` status never
 * auto-attaches (the refusal would repeat); the `ready` status re-resumes
 * the chat once, whole, and the consumer is told the chat is its own again.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'

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

function resumes(ws: FakeWebSocket, chatId: string): number {
  return ws.sent
    .map((s) => JSON.parse(s))
    .filter((m) => m.type === 'resume_chat' && m.chat_id === chatId)
    .length
}

describe('useDashboardWs on a chat a call drives', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function connect(viewedChatId: string | null) {
    const onChatViewOnly = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(
      () => useDashboardWs({ viewedChatId, onChatViewOnly }),
      { wrapper },
    )
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    return { hook, ws, onChatViewOnly }
  }

  function frame(ws: FakeWebSocket, msg: Record<string, unknown>) {
    act(() => { ws.onmessage?.({ data: JSON.stringify(msg) }) })
  }

  it('tells the consumer, holds the auto-attach, and re-resumes once on ready', () => {
    const { hook, ws, onChatViewOnly } = connect('chat-call')
    act(() => { hook.result.current.resumeChat('chat-call') })
    expect(resumes(ws, 'chat-call')).toBe(1)

    frame(ws, { type: 'chat_view_only', chat_id: 'chat-call', kind: 'phone' })
    frame(ws, { type: 'chat_history', chat_id: 'chat-call', messages: [], agent: 'a' })
    expect(onChatViewOnly).toHaveBeenCalledTimes(1)
    expect(onChatViewOnly.mock.calls[0][0]).toMatchObject({ chat_id: 'chat-call', kind: 'phone' })

    // The call's turns light the chat: no resume, the refusal would repeat.
    // Each turn's queue_sent leaves this socket's own flag alone (no done
    // would ever clear it: the socket never attached).
    frame(ws, { type: 'chat_status', chat_id: 'chat-call', status: 'streaming' })
    frame(ws, { type: 'queue_sent', chat_id: 'chat-call', queue_ids: [] })
    frame(ws, { type: 'chat_status', chat_id: 'chat-call', status: 'streaming' })
    expect(resumes(ws, 'chat-call')).toBe(1)
    expect(hook.result.current.streaming).toBe(false)
    // The pump poll's deltas repeat the frame: the state holds, told once.
    frame(ws, { type: 'chat_view_only', chat_id: 'chat-call', kind: 'phone' })
    frame(ws, { type: 'chat_history_delta', chat_id: 'chat-call', messages: [], since_id: 1 })
    frame(ws, { type: 'chat_view_only', chat_id: 'chat-call', kind: 'phone' })
    frame(ws, { type: 'chat_history_delta', chat_id: 'chat-call', messages: [], since_id: 1 })
    expect(onChatViewOnly).toHaveBeenCalledTimes(3)
    expect(onChatViewOnly).not.toHaveBeenCalledWith(null)

    // The call ended: one full resume, the consumer told the chat is its own.
    frame(ws, { type: 'chat_status', chat_id: 'chat-call', status: 'ready' })
    expect(resumes(ws, 'chat-call')).toBe(2)
    expect(onChatViewOnly).toHaveBeenLastCalledWith(null)
    // The reloaded history announces no view-only state and clears nothing twice.
    frame(ws, { type: 'chat_history', chat_id: 'chat-call', messages: [], agent: 'a' })
    expect(onChatViewOnly).toHaveBeenCalledTimes(4)
    // A later ready is an ordinary turn's end: no second reload.
    frame(ws, { type: 'chat_status', chat_id: 'chat-call', status: 'ready' })
    expect(resumes(ws, 'chat-call')).toBe(2)
  })

  it('drops the frame for a chat the socket is not viewing', () => {
    const { ws, onChatViewOnly } = connect('chat-mine')
    frame(ws, { type: 'chat_view_only', chat_id: 'chat-other', kind: 'phone' })
    frame(ws, { type: 'chat_status', chat_id: 'chat-other', status: 'ready' })
    expect(onChatViewOnly).not.toHaveBeenCalled()
    expect(resumes(ws, 'chat-other')).toBe(0)
  })

  it('a history that no frame announced ends the state', () => {
    const { hook, ws, onChatViewOnly } = connect('chat-call')
    act(() => { hook.result.current.resumeChat('chat-call') })
    frame(ws, { type: 'chat_view_only', chat_id: 'chat-call', kind: 'phone' })
    frame(ws, { type: 'chat_history', chat_id: 'chat-call', messages: [], agent: 'a' })
    // The user navigated away and back: a plain history for the same chat.
    frame(ws, { type: 'chat_history', chat_id: 'chat-call', messages: [], agent: 'a' })
    expect(onChatViewOnly).toHaveBeenLastCalledWith(null)
  })

  it('a queue_sent before the first frame does not hold the reload on ready', () => {
    // The page was still ending its own turn when the call announced itself:
    // no auto-attach ran, the call's first turn flipped the socket's flag,
    // and the pump poll's delta is what brought the frame.
    const { hook, ws, onChatViewOnly } = connect('chat-call')
    act(() => { hook.result.current.resumeChat('chat-call') })
    frame(ws, { type: 'chat_history', chat_id: 'chat-call', messages: [], agent: 'a' })
    frame(ws, { type: 'queue_sent', chat_id: 'chat-call', queue_ids: [] })
    expect(hook.result.current.streaming).toBe(true)
    frame(ws, { type: 'chat_view_only', chat_id: 'chat-call', kind: 'phone' })
    frame(ws, { type: 'chat_history_delta', chat_id: 'chat-call', messages: [], since_id: 1 })
    expect(onChatViewOnly).toHaveBeenCalledTimes(1)
    frame(ws, { type: 'chat_status', chat_id: 'chat-call', status: 'ready' })
    expect(resumes(ws, 'chat-call')).toBe(2)
    expect(hook.result.current.streaming).toBe(false)
    expect(onChatViewOnly).toHaveBeenLastCalledWith(null)
  })

  it('a delta without its frame, or another chat\'s history, ends the state', () => {
    const { hook, ws, onChatViewOnly } = connect('chat-call')
    act(() => { hook.result.current.resumeChat('chat-call') })
    frame(ws, { type: 'chat_view_only', chat_id: 'chat-call', kind: 'phone' })
    frame(ws, { type: 'chat_history_delta', chat_id: 'chat-call', messages: [], since_id: 1 })
    expect(onChatViewOnly).toHaveBeenCalledTimes(1)
    // The call ended between two polls: the next delta carries no frame.
    frame(ws, { type: 'chat_history_delta', chat_id: 'chat-call', messages: [], since_id: 1 })
    expect(onChatViewOnly).toHaveBeenLastCalledWith(null)

    frame(ws, { type: 'chat_view_only', chat_id: 'chat-call', kind: 'phone' })
    frame(ws, { type: 'chat_history', chat_id: 'chat-call', messages: [], agent: 'a' })
    expect(onChatViewOnly).toHaveBeenCalledTimes(3)
    // The user opened another chat while the call ran.
    frame(ws, { type: 'chat_history', chat_id: 'chat-other', messages: [], agent: 'a' })
    expect(onChatViewOnly).toHaveBeenCalledTimes(4)
    expect(onChatViewOnly).toHaveBeenLastCalledWith(null)
  })
})
