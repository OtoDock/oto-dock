/**
 * A `steered` frame (mid-turn steer accepted by the engine) must render the
 * user bubble at the position the engine actually CONSUMES it — the next
 * sampling-round boundary — not the accept moment: while an assistant text
 * block is still streaming, the split is DEFERRED to the next block
 * (tool/thinking/subagent) or turn end, so sentences never get cut in half.
 * When no message is open it renders immediately. It must NOT touch the
 * queue chips (an accepted steer never entered the queue) nor reset the
 * turn timer (the same turn keeps streaming).
 *
 * Post-abort stragglers: a graceful abort keeps the engine draining for a
 * beat — chunks arriving after finalizeAbortedTurn must NOT reopen a fresh
 * assistant header; the guard disarms only at the terminal aborted/done
 * frame.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'

const wsMock = vi.hoisted(() => ({
  streaming: true,
  sendMessage: vi.fn(),
  sendPermission: vi.fn(),
  sendPlanReviewResponse: vi.fn(),
  sendQuestionResponse: vi.fn(),
  resumeChat: vi.fn(),
  implementPlan: vi.fn(),
  sendLocationResponse: vi.fn(),
  subscribe: vi.fn(() => () => {}),  // generic frame registry (ui artifacts)
}))

const captured = vi.hoisted(() => ({ cb: null as any }))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

const addQueued = vi.fn()

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued, clearQueued: vi.fn() },
    }),
  )
}

describe('steered frame', () => {
  beforeEach(() => {
    addQueued.mockClear()
  })

  it('appends the user bubble and a fresh assistant continuation', () => {
    const { result } = renderStream()

    act(() => captured.cb.onSteered({ text: 'also check the logs' }))

    const msgs = result.current.messages
    expect(msgs.length).toBeGreaterThanOrEqual(2)
    const user = msgs[msgs.length - 2]
    const cont = msgs[msgs.length - 1]
    expect(user.role).toBe('user')
    expect(user.blocks).toEqual([{ type: 'text', content: 'also check the logs' }])
    expect(cont.role).toBe('assistant')
    expect(cont.blocks).toEqual([])          // continuation streams below
    expect(addQueued).not.toHaveBeenCalled() // never a queue chip
  })

  it('keeps queue chips for the fallback path (queued frame)', () => {
    const { result } = renderStream()

    act(() => captured.cb.onQueued({ index: 0, text: 'after the turn' }))

    expect(addQueued).toHaveBeenCalledWith(0, 'after the turn')
    // A queued message renders no bubble — it stays a chip until queue_sent.
    const texts = result.current.messages.flatMap((m: any) => m.blocks)
    expect(texts).not.toContainEqual({ type: 'text', content: 'after the turn' })
  })

  it('defers the split to the next tool block while text is streaming', () => {
    const { result } = renderStream()

    act(() => captured.cb.onText('checking the camera and I'))
    act(() => captured.cb.onSteered({ text: 'also check the alarm' }))

    // Mid-text: no bubble yet, and further deltas continue the SAME message.
    expect(result.current.messages.filter((m: any) => m.role === 'user')).toHaveLength(0)
    act(() => captured.cb.onText(' will report back.'))
    expect(result.current.messages).toHaveLength(1)
    expect((result.current.messages[0].blocks[0] as any).content)
      .toBe('checking the camera and I will report back.')

    // The next block boundary renders the bubble + a fresh continuation,
    // and the tool block lands in the continuation.
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't1' }))
    const msgs = result.current.messages
    expect(msgs.map((m: any) => m.role)).toEqual(['assistant', 'user', 'assistant'])
    expect(msgs[1].blocks).toEqual([{ type: 'text', content: 'also check the alarm' }])
    expect(msgs[2].blocks[0]).toMatchObject({ type: 'tool', name: 'Bash' })
  })

  it('flushes a held steer at turn end without an empty continuation', () => {
    const { result } = renderStream()

    act(() => captured.cb.onText('final words'))
    act(() => captured.cb.onSteered({ text: 'one more thing' }))
    act(() => captured.cb.onDone())

    const msgs = result.current.messages
    expect(msgs[msgs.length - 1].role).toBe('user')
    expect(msgs[msgs.length - 1].blocks)
      .toEqual([{ type: 'text', content: 'one more thing' }])
  })
})

describe('mid-turn queue chips (claude fallback)', () => {
  it('shows the chip for a DIFFERENT message while the bubble marker is armed', () => {
    const { result } = renderStream()

    // A turn-opening send added its own bubble → marker holds ITS text.
    act(() => { result.current.sentWithBubbleRef.current = 'first prompt' })

    // A mid-turn send got queued (claude has no steer) → its chip MUST show.
    act(() => captured.cb.onQueued({ index: 0, text: 'second prompt' }))
    expect(addQueued).toHaveBeenCalledWith(0, 'second prompt')

    // The reconnect/stale-pump dedup still holds for the SAME text.
    addQueued.mockClear()
    act(() => captured.cb.onQueued({ index: 0, text: 'first prompt' }))
    expect(addQueued).not.toHaveBeenCalled()
  })
})

describe('post-abort stragglers', () => {
  it('drops chunks between abort click and the terminal frame', () => {
    const { result } = renderStream()

    act(() => captured.cb.onText('Yes. The alarm is currently active'))
    // The page's handleAbort: arm the guard, seal the turn proactively.
    act(() => {
      result.current.abortedRef.current = true
      result.current.finalizeAbortedTurn()
    })
    expect(result.current.messages).toHaveLength(1)

    // Graceful-abort stragglers: no new header, no text, no blocks.
    act(() => captured.cb.onText(' at'))
    act(() => captured.cb.onThinking({ phase: 'start' }))
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 'x1' }))
    expect(result.current.messages).toHaveLength(1)
    expect(result.current.messages[0].blocks)
      .toEqual([{ type: 'text', content: 'Yes. The alarm is currently active' }])

    // The terminal frame disarms the guard — the NEXT turn streams normally.
    act(() => captured.cb.onAborted({}))
    act(() => captured.cb.onText('fresh turn'))
    expect(result.current.messages).toHaveLength(2)
    expect(result.current.messages[1].blocks)
      .toEqual([{ type: 'text', content: 'fresh turn' }])
  })
})
