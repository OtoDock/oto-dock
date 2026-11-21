/**
 * A chat's read mark survives a socket that is not open (lib/chat/readMarks):
 * the unread dot clears only for a frame that went out, a mark made while no
 * socket is open goes out once when the next one opens (the first open
 * included), a mark never echoed is sent one last time at the next open, a
 * mark older than five minutes is dropped with its dot kept, at most 50 are
 * held, the held marks outlive a page through sessionStorage, and a sign-out
 * drops them.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, render, act, fireEvent, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { useChatStore } from '@/store/chatStore'
import {
  _reloadReadMarksForTests, _resetReadMarksForTests, markChatRead, setReadSender,
} from '@/lib/chat/readMarks'
import { AuthProvider } from '@/contexts/AuthProvider'
import { useAuth } from '@/contexts/AuthContext'
import { FakeWebSocket } from './fakeWebSocket'

vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  fetchAuthConfig: vi.fn(async () => ({})),
  fetchCurrentUser: vi.fn(async () => null),
  logout: vi.fn(async () => {}),
}))

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const hook = renderHook(() => useDashboardWs({}), { wrapper })
  act(() => { hook.result.current.connect() })
  return hook
}

const latest = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1]

function open(ws: FakeWebSocket) {
  act(() => { ws.onopen?.() })
}

/** The socket drops; the hook's reconnect makes the next one, not yet open. */
function drop(ws: FakeWebSocket) {
  ws.readyState = FakeWebSocket.CLOSED
  act(() => { ws.onclose?.({ code: 1006, reason: '' }) })
  act(() => { vi.advanceTimersByTime(30_000) })
}

function reads(ws: FakeWebSocket): string[] {
  return ws.sent
    .map((raw) => JSON.parse(raw) as { type: string; chat_id?: string })
    .filter((f) => f.type === 'chat_read')
    .map((f) => f.chat_id ?? '')
}

function echo(ws: FakeWebSocket, chatId: string) {
  act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'chat_read', chat_id: chatId }) }) })
}

const unread = (chatId: string) => useChatStore.getState().byChat[chatId]?.unread

describe('read marks across a reconnect', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    sessionStorage.clear()
    _resetReadMarksForTests()
    useChatStore.setState({ byChat: {} })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('sends a mark at once on an open socket, and nothing again once it is echoed', () => {
    const hook = mount()
    const ws = latest()
    open(ws)
    act(() => { useChatStore.getState().setUnread('c1', true) })
    act(() => { hook.result.current.sendChatRead('c1') })
    expect(reads(ws)).toEqual(['c1'])
    expect(unread('c1')).toBe(false)
    echo(ws, 'c1')
    drop(ws)
    const next = latest()
    open(next)
    expect(reads(next)).toEqual([])
  })

  it('keeps the dot while the socket is closed and sends the mark once at the reconnect', () => {
    const hook = mount()
    const ws = latest()
    open(ws)
    act(() => { useChatStore.getState().setUnread('c2', true) })
    drop(ws)
    act(() => { hook.result.current.sendChatRead('c2') })
    act(() => { hook.result.current.sendChatRead('c2') })
    expect(reads(ws)).toEqual([])
    expect(unread('c2')).toBe(true)
    const next = latest()
    expect(next).not.toBe(ws)
    open(next)
    expect(reads(next)).toEqual(['c2'])
    expect(unread('c2')).toBe(false)
    echo(next, 'c2')
    drop(next)
    const third = latest()
    open(third)
    expect(reads(third)).toEqual([])
  })

  it('holds a mark made before the first open and sends it then', () => {
    const hook = mount()
    const ws = latest()
    act(() => { useChatStore.getState().setUnread('c3', true) })
    act(() => { hook.result.current.sendChatRead('c3') })
    expect(reads(ws)).toEqual([])
    expect(unread('c3')).toBe(true)
    open(ws)
    expect(reads(ws)).toEqual(['c3'])
    expect(unread('c3')).toBe(false)
  })

  it('sends a mark never echoed one last time at the next open, then no more', () => {
    const hook = mount()
    const ws = latest()
    open(ws)
    // The socket reads open but is dead (a phone that just woke).
    act(() => { hook.result.current.sendChatRead('c4') })
    expect(reads(ws)).toEqual(['c4'])
    drop(ws)
    const second = latest()
    open(second)
    expect(reads(second)).toEqual(['c4'])
    drop(second)
    const third = latest()
    open(third)
    expect(reads(third)).toEqual([])
  })

  it('does not hold the mark of a chat that is live', () => {
    const hook = mount()
    const ws = latest()
    open(ws)
    act(() => { useChatStore.getState().setStreaming('c5') })
    drop(ws)
    act(() => { hook.result.current.sendChatRead('c5') })
    const next = latest()
    open(next)
    expect(reads(next)).toEqual([])
  })

  it('keeps the held marks for the next page', () => {
    const hook = mount()
    const ws = latest()
    open(ws)
    drop(ws)
    act(() => { hook.result.current.sendChatRead('c6') })
    // A new page reads the tab's stored marks.
    _reloadReadMarksForTests()
    const next = latest()
    open(next)
    expect(reads(next)).toEqual(['c6'])
  })

  it('drops a mark held longer than five minutes and keeps its dot', () => {
    const hook = mount()
    const ws = latest()
    open(ws)
    act(() => { useChatStore.getState().setUnread('c7', true) })
    drop(ws)
    act(() => { hook.result.current.sendChatRead('c7') })
    // A long gap: a reply may have landed on the chat meanwhile.
    act(() => { vi.advanceTimersByTime(6 * 60_000) })
    const next = latest()
    open(next)
    expect(reads(next)).toEqual([])
    expect(unread('c7')).toBe(true)
    drop(next)
    const third = latest()
    open(third)
    expect(reads(third)).toEqual([])
  })

  it('holds at most 50 marks, the newest', () => {
    for (let i = 0; i < 60; i++) markChatRead(`m${i}`)
    const sent: string[] = []
    setReadSender((frame) => { sent.push(String(frame.chat_id)); return true })
    expect(sent).toEqual(Array.from({ length: 50 }, (_, i) => `m${i + 10}`))
  })

  it('stops at the first frame that does not go out and sends the rest at the next open', () => {
    for (const id of ['f1', 'f2', 'f3']) {
      act(() => { useChatStore.getState().setUnread(id, true) })
      markChatRead(id)
    }
    const first: string[] = []
    setReadSender((frame) => {
      if (frame.chat_id === 'f2') return false
      first.push(String(frame.chat_id))
      return true
    })
    expect(first).toEqual(['f1'])
    expect([unread('f1'), unread('f2'), unread('f3')]).toEqual([false, true, true])
    const second: string[] = []
    setReadSender((frame) => { second.push(String(frame.chat_id)); return true })
    // f1 once more (no echo came), f2 and f3 for the first time.
    expect(second).toEqual(['f1', 'f2', 'f3'])
    expect([unread('f2'), unread('f3')]).toEqual([false, false])
  })

  it('drops the held marks at a sign-out', async () => {
    function SignOut() {
      const { logout } = useAuth()
      return <button onClick={logout}>Sign out</button>
    }
    vi.useRealTimers()
    render(<AuthProvider><SignOut /></AuthProvider>)
    markChatRead('s1')
    expect(sessionStorage.getItem('otodock.readMarks')).not.toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(sessionStorage.getItem('otodock.readMarks')).toBeNull()
    _reloadReadMarksForTests()
    const sent: unknown[] = []
    setReadSender((frame) => { sent.push(frame); return true })
    expect(sent).toEqual([])
  })
})
