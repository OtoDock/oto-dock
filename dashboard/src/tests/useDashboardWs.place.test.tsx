/**
 * A new chat's place rides its pre_warmup and its first warmup (never a
 * warmup of an existing chat, never a voice start), and a running move's
 * `chat_move_progress` lands in the chat's cached targets for the ring.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { chatTargetsKey, type ChatTargets } from '@/api/remoteMachines'
import { FakeWebSocket } from './fakeWebSocket'

const TARGETS: ChatTargets = {
  current: { target: 'local', label: 'OtoDock platform', pairing: '', state: '', owner: '' },
  default_target: 'local',
  targets: [],
  can_move: true, refusal: '', lock: null, moving: null, place_chosen: false,
} as unknown as ChatTargets

function setup(place: string | undefined) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const newChatTarget = vi.fn(() => place)
  const hook = renderHook(() => useDashboardWs({ newChatTarget }), { wrapper })
  act(() => { hook.result.current.connect() })
  const ws = FakeWebSocket.instances[0]
  act(() => { ws.onopen?.() })
  const sent = (type: string) => ws.sent.map((s) => JSON.parse(s)).filter((m) => m.type === type)
  const receive = (frame: object) => act(() => { ws.onmessage?.({ data: JSON.stringify(frame) }) })
  return { qc, hook, sent, receive, newChatTarget }
}

describe("a new chat's place on the socket", () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('the pre-warm and the first warmup carry the picked place', () => {
    const { hook, sent, newChatTarget } = setup('m-attic')
    act(() => { hook.result.current.preWarmup('dev') })
    act(() => { hook.result.current.warmup('dev') })
    expect(sent('pre_warmup')[0]).toMatchObject({ agent: 'dev', target: 'm-attic' })
    expect(sent('warmup')[0]).toMatchObject({ agent: 'dev', target: 'm-attic' })
    expect(newChatTarget).toHaveBeenCalledWith('dev')
  })

  it('no pick sends no target', () => {
    const { hook, sent } = setup(undefined)
    act(() => { hook.result.current.preWarmup('dev') })
    act(() => { hook.result.current.warmup('dev') })
    expect(sent('pre_warmup')[0]).not.toHaveProperty('target')
    expect(sent('warmup')[0]).not.toHaveProperty('target')
  })

  it("an existing chat's warmup and a voice start carry no place", () => {
    const { hook, sent } = setup('m-attic')
    act(() => { hook.result.current.warmup('dev', 'c1') })
    act(() => { hook.result.current.warmup('dev', undefined, undefined, undefined, undefined, undefined,
      undefined, undefined, { place: false }) })
    const [existing, voice] = sent('warmup')
    expect(existing).toMatchObject({ chat_id: 'c1' })
    expect(existing).not.toHaveProperty('target')
    expect(voice).not.toHaveProperty('target')
  })
})

describe("a move's progress", () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })
  afterEach(() => vi.unstubAllGlobals())

  const frame = { type: 'chat_move_progress', chat_id: 'c1', move_id: 'mv1', to_label: 'Attic PC',
                  done: 40, total: 100, phase: 'copy' }

  it("goes into the chat's cached targets", () => {
    const { qc, receive } = setup(undefined)
    qc.setQueryData(chatTargetsKey('c1'), { ...TARGETS, moving: { to_label: 'Attic PC' } })
    receive(frame)
    expect(qc.getQueryData<ChatTargets>(chatTargetsKey('c1'))?.moving).toEqual({
      to_label: 'Attic PC', progress: { done: 40, total: 100, phase: 'copy' },
    })
  })

  it('names the place from the frame when the cached answer predates the move', () => {
    const { qc, receive } = setup(undefined)
    qc.setQueryData(chatTargetsKey('c1'), TARGETS)
    receive(frame)
    expect(qc.getQueryData<ChatTargets>(chatTargetsKey('c1'))?.moving?.to_label).toBe('Attic PC')
  })

  it('makes no entry for a chat nobody shows', () => {
    const { qc, receive } = setup(undefined)
    receive(frame)
    expect(qc.getQueryData(chatTargetsKey('c1'))).toBeUndefined()
  })
})
