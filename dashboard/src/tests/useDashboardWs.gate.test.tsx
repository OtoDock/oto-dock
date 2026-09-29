/**
 * A dashboard socket held by the forced password change or 2FA enrolment
 * closes with 4403 and the gate's name: the tab goes to that screen and the
 * hook does not reconnect into the same refusal.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs, WS_CLOSE_GATE } from '@/hooks/useDashboardWs'

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

describe('the gate close on the dashboard socket', () => {
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

  it('sends the tab to the password change and does not reconnect', () => {
    setLocation('/chat/agent-a')
    const ws = openSocket()
    act(() => { ws.onclose?.({ code: WS_CLOSE_GATE, reason: 'must_change_password' }) })
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(window.location.href).toBe('/change-password')
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('sends the tab to the 2FA enrolment, the default for any other reason', () => {
    setLocation('/chat/agent-a')
    const ws = openSocket()
    act(() => { ws.onclose?.({ code: WS_CLOSE_GATE, reason: '' }) })
    expect(window.location.href).toBe('/setup-2fa')
  })

  it('stays put on the forced screen itself', () => {
    setLocation('/setup-2fa')
    const ws = openSocket()
    act(() => { ws.onclose?.({ code: WS_CLOSE_GATE, reason: 'must_enroll_2fa' }) })
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(window.location.href).toBe('/setup-2fa')
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('reconnects after any other close', () => {
    setLocation('/chat/agent-a')
    const ws = openSocket()
    act(() => { ws.onclose?.({ code: 1006, reason: '' }) })
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1)
  })
})
