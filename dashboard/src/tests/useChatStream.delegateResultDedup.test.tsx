/**
 * One delegate delivery can reach the socket as TWO `delegate_result` frames
 * (the ladder's pump push + the WS handler's own send — the 2026-09-02
 * "CEO response rendered twice" bug). The bubble must render once per
 * distinct result: dedup on task_id + identical content, while a recurring
 * task's later round (same task_id, different output) still renders.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'

const wsMock = vi.hoisted(() => ({
  streaming: false,
  sendMessage: vi.fn(),
  sendPermission: vi.fn(),
  sendPlanReviewResponse: vi.fn(),
  sendQuestionResponse: vi.fn(),
  resumeChat: vi.fn(),
  implementPlan: vi.fn(),
  sendLocationResponse: vi.fn(),
  subscribe: vi.fn(() => () => {}),
}))

const captured = vi.hoisted(() => ({ cbs: null as any }))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cbs: any) => {
    captured.cbs = cbs
    return wsMock
  },
}))

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }),
  )
}

const FRAME = {
  task_id: 'dyn-abc123',
  task_name: 'Banner re-render',
  agent: 'content-creator',
  output_text: 'Banner set delivered — 6 platform sizes.',
  status: 'completed',
}

const delegateBubbles = (msgs: any[]) =>
  msgs.filter((m) => m.id?.startsWith?.('delegate-result-'))

describe('onDelegateResult dedup', () => {
  beforeEach(() => {
    captured.cbs = null
  })

  it('renders one bubble for two identical frames of the same delivery', () => {
    const { result } = renderStream()
    act(() => captured.cbs.onDelegateResult({ ...FRAME }))
    act(() => captured.cbs.onDelegateResult({ ...FRAME }))
    expect(delegateBubbles(result.current.messages)).toHaveLength(1)
  })

  it('still renders a later round with different output (recurring task)', () => {
    const { result } = renderStream()
    act(() => captured.cbs.onDelegateResult({ ...FRAME }))
    act(() =>
      captured.cbs.onDelegateResult({ ...FRAME, output_text: 'Round 2 output.' }),
    )
    expect(delegateBubbles(result.current.messages)).toHaveLength(2)
  })

  it('renders both bubbles for two distinct tasks', () => {
    const { result } = renderStream()
    act(() => captured.cbs.onDelegateResult({ ...FRAME }))
    act(() =>
      captured.cbs.onDelegateResult({
        ...FRAME, task_id: 'dyn-zzz999', output_text: 'Other lane result.',
      }),
    )
    expect(delegateBubbles(result.current.messages)).toHaveLength(2)
  })
})
