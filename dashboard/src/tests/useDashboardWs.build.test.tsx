/**
 * The build id reaches lib/buildId from the two socket signals
 * (`server_info` on connect, `build_id` inside every `pong`) and, for pages
 * without a socket, from the foreground watch that asks /health.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { useBuildWatch } from '@/hooks/useBuildWatch'

vi.mock('@/lib/buildId', () => ({ noteServerBuild: vi.fn() }))
import { noteServerBuild } from '@/lib/buildId'

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

describe('build id over the dashboard socket', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    vi.mocked(noteServerBuild).mockClear()
  })
  afterEach(() => vi.unstubAllGlobals())

  it('hands server_info and pong build ids to the comparison', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(() => useDashboardWs({}), { wrapper })
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'server_info', build_id: 'b2', version: '1.6.1' }) }) })
    expect(noteServerBuild).toHaveBeenCalledWith('b2')
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'pong', build_id: 'b3' }) }) })
    expect(noteServerBuild).toHaveBeenLastCalledWith('b3')
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'pong' }) }) })
    expect(noteServerBuild).toHaveBeenCalledTimes(2) // a bare pong (older server) is inert
  })
})

describe('useBuildWatch (pages without a socket)', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  beforeEach(() => {
    vi.mocked(noteServerBuild).mockClear()
    fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ status: 'ok', build: 'b9' }) }))
    vi.stubGlobal('fetch', fetchMock)
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('asks /health on the foreground signals, once per burst, and compares', async () => {
    renderHook(() => useBuildWatch(true))
    await act(async () => {
      window.dispatchEvent(new Event('otodock:force-health-check'))
      document.dispatchEvent(new Event('visibilitychange'))
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('/health', expect.objectContaining({ cache: 'no-store' }))
    expect(noteServerBuild).toHaveBeenCalledWith('b9')
  })

  it('is inert while disabled', async () => {
    renderHook(() => useBuildWatch(false))
    await act(async () => { window.dispatchEvent(new Event('otodock:force-health-check')) })
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
