// The entry on the real dashboard socket, under StrictMode: the registry
// channels (the display_ui pages, the check verdicts, the artifact and app
// action backchannels) subscribe once and never again when the socket's
// bundle changes identity on a connected or streaming flip, and a ui frame
// after the flip draws exactly once.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { StrictMode, useMemo, type ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FakeWebSocket } from '@/tests/fakeWebSocket'
import { useChatStream } from '@/hooks/useChatStream'

// The socket's `subscribe` is counted by channel; the wrapper keeps the
// bundle's own identity rules (a new object when the real one changes,
// a stable `subscribe` as the real one is).
const calls = vi.hoisted(() => ({ list: [] as string[] }))
vi.mock('@/hooks/useDashboardWs', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/hooks/useDashboardWs')>()
  return {
    useDashboardWs: (cb: Parameters<typeof real.useDashboardWs>[0]) => {
      const ws = real.useDashboardWs(cb)
      const subscribe = useMemo(() => {
        const inner = ws.subscribe
        return (type: Parameters<typeof inner>[0], fn: Parameters<typeof inner>[1]) => {
          calls.list.push(type)
          return inner(type, fn)
        }
      }, [ws.subscribe])
      return useMemo(() => ({ ...ws, subscribe }), [ws, subscribe])
    },
  }
})

beforeEach(() => {
  FakeWebSocket.instances.length = 0
  calls.list.length = 0
  vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
})

describe('useChatStream on the real socket', () => {
  it('subscribes its channels once and keeps them across the bundle flips', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <StrictMode><QueryClientProvider client={qc}>{children}</QueryClientProvider></StrictMode>
    )
    const { result } = renderHook(() => useChatStream({
      agents: [], initialChatId: 'chat-1', queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }), { wrapper })
    act(() => { result.current.ws.connect() })
    const ws = FakeWebSocket.instances[0]
    act(() => { ws.onopen?.() })
    expect(result.current.ws.connected).toBe(true)
    const channels = ['ui', 'check_verdict', 'artifact_interaction', 'artifact_ack', 'app_action', 'app_action_ack']
    for (const c of channels) expect(calls.list.filter((t) => t === c).length, c).toBeGreaterThanOrEqual(1)
    const before = calls.list.length
    act(() => { result.current.ws.setStreaming(true) })
    act(() => { result.current.ws.setStreaming(false) })
    expect(calls.list.length).toBe(before)
    act(() => captured(ws, { type: 'text', chat_id: 'chat-1', content: 'hello' }))
    act(() => captured(ws, { type: 'ui', chat_id: 'chat-1', token: 't1', html: '<p>x</p>', title: 'page' }))
    const msgs = result.current.messages
    const blocks = msgs[msgs.length - 1]?.blocks ?? []
    expect(blocks.filter((b) => b.type === 'ui')).toHaveLength(1)
    expect(calls.list.length).toBe(before)
  })
})

function captured(ws: FakeWebSocket, frame: Record<string, unknown>) {
  ws.onmessage?.({ data: JSON.stringify(frame) })
}
