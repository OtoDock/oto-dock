/**
 * Auto-attach on server-side pump start (tour finding 3 + the delegate-echo
 * roadmap item): a `chat_status: streaming` frame for the chat this socket is
 * ALREADY viewing re-issues `resume_chat` so the open page attaches to the
 * new pump — once per streaming episode (the backend echoes the streaming
 * frame straight back to the attaching socket; the episode ref must absorb
 * that and every repeated turn frame).
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

describe('useDashboardWs auto-attach on chat_status streaming', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function connect(viewedChatId: string | null, ptyLive = false) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(
      () => useDashboardWs({ viewedChatId, isViewedChatPtyLive: () => ptyLive }),
      { wrapper },
    )
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    return { hook, ws }
  }

  function frame(ws: FakeWebSocket, msg: Record<string, unknown>) {
    act(() => { ws.onmessage?.({ data: JSON.stringify(msg) }) })
  }

  it('resumes once per streaming episode for the viewed chat', () => {
    const { hook, ws } = connect('task-abc')
    act(() => { hook.result.current.resumeChat('task-abc') })
    expect(resumes(ws, 'task-abc')).toBe(1)

    // Server-side pump starts → exactly one auto-attach resume.
    frame(ws, { type: 'chat_status', chat_id: 'task-abc', status: 'streaming' })
    expect(resumes(ws, 'task-abc')).toBe(2)

    // The backend's direct echo / repeated turn frames don't loop.
    frame(ws, { type: 'chat_status', chat_id: 'task-abc', status: 'streaming' })
    frame(ws, { type: 'chat_status', chat_id: 'task-abc', status: 'streaming' })
    expect(resumes(ws, 'task-abc')).toBe(2)

    // Episode ends → a NEW streaming episode re-attaches.
    frame(ws, { type: 'chat_status', chat_id: 'task-abc', status: 'ready' })
    frame(ws, { type: 'chat_status', chat_id: 'task-abc', status: 'streaming' })
    expect(resumes(ws, 'task-abc')).toBe(3)
  })

  it('ignores streaming frames for other chats', () => {
    const { hook, ws } = connect('task-abc')
    act(() => { hook.result.current.resumeChat('task-abc') })
    frame(ws, { type: 'chat_status', chat_id: 'chat-other', status: 'streaming' })
    expect(resumes(ws, 'chat-other')).toBe(0)
    expect(resumes(ws, 'task-abc')).toBe(1)
  })

  it('does nothing when no chat is viewed', () => {
    const { ws } = connect(null)
    frame(ws, { type: 'chat_status', chat_id: 'task-abc', status: 'streaming' })
    expect(resumes(ws, 'task-abc')).toBe(0)
  })

  it('never resumes a live interactive (PTY) chat', () => {
    // A prompt sent to a live PTY opens the turn → a legit `streaming`
    // broadcast for the viewed chat. Resuming would replay history and
    // re-attach the PTY viewer — a visible terminal reload — so the PTY
    // gate must swallow the auto-attach entirely.
    const { hook, ws } = connect('chat-pty', true)
    act(() => { hook.result.current.resumeChat('chat-pty') })
    expect(resumes(ws, 'chat-pty')).toBe(1)
    frame(ws, { type: 'chat_status', chat_id: 'chat-pty', status: 'streaming' })
    frame(ws, { type: 'chat_status', chat_id: 'chat-pty', status: 'ready' })
    frame(ws, { type: 'chat_status', chat_id: 'chat-pty', status: 'streaming' })
    expect(resumes(ws, 'chat-pty')).toBe(1)  // only the explicit resume
  })
})
