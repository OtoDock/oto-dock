/**
 * The build id reaches lib/buildId from the two socket signals
 * (`server_info` on connect, `build_id` inside every `pong`) and, for pages
 * without a socket, from the foreground watch that asks /health. The same
 * `server_info` carries the platform's update flag (lib/session/serverInfo).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { useBuildWatch } from '@/hooks/useBuildWatch'
import { noteServerUpdating, serverUpdating, useServerUpdating } from '@/lib/session/serverInfo'

vi.mock('@/lib/buildId', () => ({ noteServerBuild: vi.fn() }))
import { noteServerBuild } from '@/lib/buildId'
import { FakeWebSocket } from './fakeWebSocket'

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

describe('the update flag over the dashboard socket', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    noteServerUpdating(false)
  })
  afterEach(() => vi.unstubAllGlobals())

  /** An open socket, as the function that hands it one `server_info`. */
  function openSocket() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(() => useDashboardWs({}), { wrapper })
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    return (over: object) => act(() => {
      ws.onmessage?.({ data: JSON.stringify({ type: 'server_info', build_id: 'b2', version: '2.0.0', ...over }) })
    })
  }

  it('a server_info with updating true sets it, one with false clears it', () => {
    const info = openSocket()
    const flag = renderHook(() => useServerUpdating())
    expect(flag.result.current).toBe(false)
    info({ updating: true })
    expect(flag.result.current).toBe(true)
    expect(serverUpdating()).toBe(true)
    info({ updating: false })
    expect(flag.result.current).toBe(false)
    expect(serverUpdating()).toBe(false)
  })

  it('a server_info without the key clears it: the frame a socket gets when it reconnects', () => {
    const info = openSocket()
    const flag = renderHook(() => useServerUpdating())
    info({ updating: true })
    expect(flag.result.current).toBe(true)
    info({})
    expect(flag.result.current).toBe(false)
  })

  it('only true sets it', () => {
    const info = openSocket()
    info({ updating: 'yes' })
    expect(serverUpdating()).toBe(false)
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
