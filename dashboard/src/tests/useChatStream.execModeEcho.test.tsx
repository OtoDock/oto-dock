/**
 * `execution_mode_changed` (the ack of a terminal pick, or the echo of a
 * refused one) reaches the page only for the chat in view: it carries a
 * chat id and is not a per-chat frame on the wire, so the stream matches it.
 */
import { describe, it, expect, vi } from 'vitest'
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

const captured = vi.hoisted(() => ({ cb: null as any }))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

describe('execution_mode_changed in useChatStream', () => {
  it('applies the viewed chat\'s stored mode and nothing else', () => {
    const onExecutionModeChanged = vi.fn()
    renderHook(() =>
      useChatStream({
        agents: [],
        initialChatId: 'chat-1',
        queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
        onExecutionModeChanged,
      } as any),
    )
    act(() => captured.cb.onExecutionModeChanged({ type: 'execution_mode_changed', execution_mode: '-p', chat_id: 'chat-2' }))
    act(() => captured.cb.onExecutionModeChanged({ type: 'execution_mode_changed', execution_mode: '' }))
    expect(onExecutionModeChanged).not.toHaveBeenCalled()
    act(() => captured.cb.onExecutionModeChanged({ type: 'execution_mode_changed', execution_mode: 'interactive', chat_id: 'chat-1' }))
    expect(onExecutionModeChanged).toHaveBeenCalledWith('interactive')
  })
})
