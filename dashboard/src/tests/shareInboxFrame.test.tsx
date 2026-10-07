/**
 * The `share_inbox` frame (SHARING.md): the socket hands it to the
 * `onShareInbox` callback (a page that owns its socket) and to the generic
 * registry (the chat page subscribes there, since its options ride another
 * hook); the notifications hook's handler refetches the section and the
 * Apps panels the frame names rather than applying it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { WIRE } from '@/api/wireEvents'

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

describe('the share_inbox frame', () => {
  let warn: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    warn.mockRestore()
  })

  it('reaches the callback and the registry, with no warning', () => {
    const onShareInbox = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(() => useDashboardWs({ onShareInbox }), { wrapper })
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    const seen: unknown[] = []
    act(() => { hook.result.current.subscribe(WIRE.SHARE_INBOX, (m) => seen.push(m)) })
    const frame = { type: WIRE.SHARE_INBOX, pending: 2, agents: ['alpha'] }
    act(() => { ws.onmessage?.({ data: JSON.stringify(frame) }) })
    expect(onShareInbox).toHaveBeenCalledTimes(1)
    expect(onShareInbox.mock.calls[0][0]).toMatchObject({ pending: 2, agents: ['alpha'] })
    expect(seen).toHaveLength(1)
    expect(warn).not.toHaveBeenCalled()
  })
})

describe('the notifications hook on a share_inbox frame', () => {
  it('refetches the section and the Apps panels the frame names', async () => {
    vi.doMock('@/api/agents', () => ({ useAgents: () => ({ data: [] }) }))
    vi.doMock('@/hooks/usePushSubscription', () => ({ usePushSubscription: () => {} }))
    vi.doMock('@/hooks/useNotificationSound', () => ({
      useNotificationSound: () => ({ stopDangerAlarm: () => {}, playForSeverity: () => {}, playPing: () => {} }),
    }))
    vi.doMock('react-router-dom', () => ({ useNavigate: () => () => {} }))
    vi.doMock('@/api/shares', async (importOriginal) => ({
      ...(await importOriginal<typeof import('@/api/shares')>()),
      useShareInbox: () => ({ data: { items: [], pending: 0 }, refetch: vi.fn() }),
      useAcceptShare: () => ({ mutateAsync: vi.fn() }),
      useDeclineShare: () => ({ mutateAsync: vi.fn() }),
      useHideShare: () => ({ mutateAsync: vi.fn() }),
    }))
    const { useChatNotifications } = await import('@/hooks/useChatNotifications')
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(() => useChatNotifications(), { wrapper })
    act(() => { hook.result.current.onShareInbox({ type: WIRE.SHARE_INBOX, pending: 1, agents: ['alpha', 'lite'] }) })
    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['shares', 'inbox']))
    expect(keys).toContain(JSON.stringify(['apps', 'alpha']))
    expect(keys).toContain(JSON.stringify(['apps', 'lite']))
    // A share delivery refreshes the section too.
    invalidate.mockClear()
    act(() => { hook.result.current.onNotificationSilent({ type: WIRE.NOTIFICATION_SILENT, delivery: { id: 'd', source: 'share', severity: 'info' } }) })
    expect(invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))).toContain(JSON.stringify(['shares', 'inbox']))
    vi.doUnmock('@/api/agents')
    vi.doUnmock('@/hooks/usePushSubscription')
    vi.doUnmock('@/hooks/useNotificationSound')
    vi.doUnmock('react-router-dom')
    vi.doUnmock('@/api/shares')
  })
})
