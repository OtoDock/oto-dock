/**
 * A new chat's place rides its pre_warmup and its first warmup (never a
 * warmup of an existing chat, never a voice start), a running move's
 * `chat_move_progress` lands in the chat's cached targets for the ring, and a
 * machine that came or went (`machine_link`) makes the page read every place
 * again and ask again about the banner (`target_check`, `target_mismatch`).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { chatTargetsKey, type ChatTargets } from '@/api/remoteMachines'
import { useChatStore } from '@/store/chatStore'
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

describe('a machine that came or went', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  const PLACE_KEYS = [['chat-targets'], ['agent-run-targets'], ['agent-target-status'],
    ['remote-machines'], ['my-remote-machines']]
  const refreshed = (spy: { mock: { calls: unknown[][] } }) =>
    spy.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))

  it('reads every place again once for a burst, and asks about the banner', () => {
    vi.useFakeTimers()
    const { qc, sent, receive } = setup(undefined)
    const spy = vi.spyOn(qc, 'invalidateQueries')
    receive({ type: 'machine_link', machine_id: 'm-1' })
    receive({ type: 'machine_link', machine_id: 'm-2' })
    receive({ type: 'machine_link', machine_id: 'm-1' })
    expect(sent('target_check')).toHaveLength(0)
    act(() => { vi.advanceTimersByTime(300) })
    expect(refreshed(spy).sort()).toEqual(PLACE_KEYS.map((k) => JSON.stringify(k)).sort())
    expect(sent('target_check')).toEqual([{ type: 'target_check' }])
    // The next machine is a refresh of its own.
    receive({ type: 'machine_link', machine_id: 'm-3' })
    act(() => { vi.advanceTimersByTime(300) })
    expect(sent('target_check')).toHaveLength(2)
  })

  it("sets the chat's banner from the answer, and clears it", () => {
    const { receive } = setup(undefined)
    receive({ type: 'target_mismatch', chat_id: 'c-pin', pinned_target: 'm-1', pinned_label: 'Attic PC',
              resolved_target: 'local', resolved_label: 'the OtoDock platform' })
    expect(useChatStore.getState().byChat['c-pin']?.targetMismatch).toEqual({
      pinnedTarget: 'm-1', pinnedLabel: 'Attic PC',
      resolvedTarget: 'local', resolvedLabel: 'the OtoDock platform',
    })
    receive({ type: 'target_mismatch', chat_id: 'c-pin' })
    expect(useChatStore.getState().byChat['c-pin']?.targetMismatch).toBeNull()
  })

  it('a socket that came back reads every place again', () => {
    const { qc, hook } = setup(undefined)
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const first = FakeWebSocket.instances[0]
    first.readyState = FakeWebSocket.CLOSED
    act(() => { hook.result.current.connect() })
    act(() => { FakeWebSocket.instances[1].onopen?.() })
    expect(refreshed(spy)).toEqual(expect.arrayContaining(PLACE_KEYS.map((k) => JSON.stringify(k))))
  })
})
