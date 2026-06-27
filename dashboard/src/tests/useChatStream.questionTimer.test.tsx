/**
 * Answering an AskUserQuestion must arm the turn timer.
 *
 * Every path that flips the chat to streaming (Stop button) also sets
 * turnStartTime — handleSendMessage, handlePermissionRespond,
 * resolvePlanReview — EXCEPT handleQuestionAnswer, which sent the answer but
 * left the timer null: Stop showed, no timer rendered (ChatStatusBar requires
 * streaming AND startTime). No later frame repairs it either — a mid-turn
 * answer is consumed by the question hook, so no queue_sent/live_state ever
 * re-arms the timer.
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
  subscribe: vi.fn(() => () => {}),  // generic frame registry (ui artifacts)
}))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: () => wsMock,
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

describe('handleQuestionAnswer turn timer', () => {
  beforeEach(() => {
    wsMock.streaming = false
    wsMock.sendMessage.mockClear()
  })

  it('arms the timer when answering between turns', () => {
    const { result } = renderStream()
    expect(result.current.turnStartTime).toBeNull()

    act(() => result.current.handleQuestionAnswer('Option A'))

    expect(wsMock.sendMessage).toHaveBeenCalledWith('Option A', 'chat-1')
    expect(result.current.turnStartTime).not.toBeNull()
  })

  it('arms the timer when answering mid-turn (hook-consumed answer)', () => {
    wsMock.streaming = true
    const { result } = renderStream()

    act(() => result.current.handleQuestionAnswer('Option B'))

    expect(wsMock.sendMessage).toHaveBeenCalledWith('Option B', 'chat-1')
    expect(result.current.turnStartTime).not.toBeNull()
  })

  it('does nothing without a chat id', () => {
    const { result } = renderStream()
    act(() => result.current.setChatId(null))
    act(() => result.current.handleQuestionAnswer('Option C'))

    expect(wsMock.sendMessage).not.toHaveBeenCalled()
    expect(result.current.turnStartTime).toBeNull()
  })
})
