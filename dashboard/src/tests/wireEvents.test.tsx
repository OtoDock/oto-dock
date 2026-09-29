/**
 * The wire catalogue's receiver behaviour: `PER_CHAT_FRAMES` is derived
 * from the table, an unknown frame type warns ONCE and still reaches the
 * bus, a catalogued frame without a switch case is silent, and the
 * per-chat gate drops a foreign per-chat frame while a global one passes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { WIRE, FRAMES, PER_CHAT_FRAMES, OUT, isWireType } from '@/api/wireEvents'
import { _resetCatalogSubsForTests, subscribeCatalog } from '@/lib/appLive'

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

describe('the wire catalogue', () => {
  it('derives the per-chat set from the table', () => {
    const fromTable = new Set(Object.entries(FRAMES).filter(([, m]) => m.perChat).map(([k]) => k))
    expect(new Set(PER_CHAT_FRAMES)).toEqual(fromTable)
    expect(PER_CHAT_FRAMES.has(WIRE.TEXT)).toBe(true)
    expect(PER_CHAT_FRAMES.has(WIRE.TURN_COMPLETE)).toBe(false)   // origin-routed, never gated
    expect(PER_CHAT_FRAMES.has(WIRE.CHAT_HISTORY)).toBe(false)    // its own stale-guard
    expect(Object.keys(FRAMES).length).toBe(Object.keys(WIRE).length)
    expect(isWireType('text')).toBe(true)
    expect(isWireType('thinking_changed')).toBe(false)
  })

  it('names every message the dashboard sends', () => {
    expect(OUT.WARMUP).toBe('warmup')
    expect(Object.values(OUT)).not.toContain('thinking_change')
  })
})

describe('the receiver', () => {
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

  function connect(viewedChatId: string | null | undefined, cbs: Record<string, unknown> = {}) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const hook = renderHook(() => useDashboardWs({ viewedChatId, ...cbs }), { wrapper })
    act(() => { hook.result.current.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    return { hook, ws }
  }

  it('warns once per unknown frame type and still hands it to the bus', () => {
    const { hook, ws } = connect(undefined)
    const seen: unknown[] = []
    act(() => { hook.result.current.subscribe('from_the_future' as never, (m) => seen.push(m)) })
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'from_the_future', x: 1 }) }) })
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'from_the_future', x: 2 }) }) })
    expect(seen).toHaveLength(2)
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('from_the_future')
  })

  it('stays silent for a catalogued frame the switch does not act on', () => {
    const { ws } = connect(undefined)
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: WIRE.EXECUTION_MODE_CHANGED, execution_mode: '-p' }) }) })
    expect(warn).not.toHaveBeenCalled()
  })

  it('gates a per-chat frame by chat id and lets a global frame through', () => {
    const onText = vi.fn()
    const onTitleUpdated = vi.fn()
    const { ws } = connect('chat-A', { onText, onTitleUpdated })
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: WIRE.TEXT, content: 'no', chat_id: 'chat-B' }) }) })
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: WIRE.TEXT, content: 'yes', chat_id: 'chat-A' }) }) })
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: WIRE.TITLE_UPDATED, chat_id: 'chat-B', title: 't' }) }) })
    expect(onText).toHaveBeenCalledTimes(1)
    expect(onText).toHaveBeenCalledWith('yes')
    expect(onTitleUpdated).toHaveBeenCalledTimes(1)
  })

  it("an old page's socket closing late leaves the new socket's catalog sender alone", () => {
    // Chat page → app page: the old hook's socket is closed on unmount, but
    // its close event can land after the new page's socket has opened.
    _resetCatalogSubsForTests()
    const qc = new QueryClient()
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const oldPage = renderHook(() => useDashboardWs({}), { wrapper })
    act(() => { oldPage.result.current.connect() })
    const oldWs = FakeWebSocket.instances[0]
    act(() => { oldWs.onopen?.() })
    const newPage = renderHook(() => useDashboardWs({}), { wrapper })
    act(() => { newPage.result.current.connect() })
    const newWs = FakeWebSocket.instances[1]
    act(() => { newWs.onopen?.() })
    oldPage.unmount()
    act(() => { oldWs.onclose?.() })
    subscribeCatalog('dev', 'tasks')
    expect(newWs.sent.map((f) => JSON.parse(f)))
      .toContainEqual({ type: 'catalog_subscribe', agent: 'dev', feed: 'tasks' })
    newPage.unmount()
    _resetCatalogSubsForTests()
  })

  it('hands a cancelled attachment-only queued message back to the composer', () => {
    const onQueueEditReturn = vi.fn()
    const { ws } = connect('chat-A', { onQueueEditReturn })
    const images = [{ name: 'dot.png', path: 'users/u/workspace/uploads/photos/img_1.png' }]
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: WIRE.QUEUE_REMOVED, index: 0, text: '', images, chat_id: 'chat-A' }) }) })
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: WIRE.QUEUE_REMOVED, index: 1, text: '', chat_id: 'chat-A' }) }) })
    expect(onQueueEditReturn).toHaveBeenCalledTimes(1)
    expect(onQueueEditReturn.mock.calls[0][0]).toMatchObject({ text: '', images })
  })
})
