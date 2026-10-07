/**
 * A dashboard socket the server closed 4001 (its session no longer holds)
 * asks /auth/me before anything else: a session the server no longer knows
 * sends the tab to the sign-in page; one it still knows (a passing refusal,
 * a race with a refresh) keeps the normal reconnect.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs, WS_CLOSE_SESSION } from '@/hooks/useDashboardWs'

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

const originalLocation = window.location

function setLocation(pathname: string) {
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { href: pathname, pathname, protocol: 'http:', host: '192.168.1.10:8400', reload: vi.fn() },
  })
}

function openSocket() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const hook = renderHook(() => useDashboardWs({}), { wrapper })
  act(() => { hook.result.current.connect() })
  const ws = FakeWebSocket.instances[0]
  act(() => { ws.onopen?.() })
  return ws
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('the session close on the dashboard socket', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
  })

  it('sends the tab to the sign-in page when the session is gone', async () => {
    setLocation('/chat/agent-a')
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })))
    const ws = openSocket()
    act(() => { ws.onclose?.({ code: WS_CLOSE_SESSION, reason: 'Invalid or expired session' }) })
    await flush()
    expect(window.location.href).toBe('/')
  })

  it('reconnects when the session still holds', async () => {
    setLocation('/chat/agent-a')
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true })))
    const ws = openSocket()
    act(() => { ws.onclose?.({ code: WS_CLOSE_SESSION, reason: 'Invalid or expired session' }) })
    await flush()
    expect(window.location.href).toBe('/chat/agent-a')
    act(() => { vi.advanceTimersByTime(2_000) })
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1)
  })
})
