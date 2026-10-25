/**
 * The end-of-turn "empty bubble" recovery (core-seams phase 9): a bubble
 * whose only block is a ui artifact is content — no refetch from the DB at
 * `done`, so its iframe is not remounted; an empty bubble is refetched 400 ms
 * later, as before.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'

const subs = vi.hoisted(() => ({} as Record<string, (msg: any) => void>))

const wsMock = vi.hoisted(() => ({
  streaming: false,
  sendMessage: vi.fn(),
  sendPermission: vi.fn(),
  sendPlanReviewResponse: vi.fn(),
  sendQuestionResponse: vi.fn(),
  resumeChat: vi.fn(),
  implementPlan: vi.fn(),
  sendLocationResponse: vi.fn(),
  subscribe: vi.fn((type: string, cb: (msg: any) => void) => { subs[type] = cb; return () => {} }),
}))

const captured = vi.hoisted(() => ({ cb: null as any }))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      enableDefensiveRefetch: true,
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }),
  )
}

describe('the empty-bubble recovery and a ui-only turn', () => {
  afterEach(() => {
    vi.useRealTimers()
    wsMock.resumeChat.mockClear()
  })

  it('a turn whose only block is a ui artifact is not refetched', () => {
    vi.useFakeTimers()
    renderStream()
    act(() => subs['ui']({ type: 'ui', chat_id: 'chat-1', token: 't', ui_url: '/v1/ui/t', title: 'Board', path: 'ws/board.html' }))
    act(() => captured.cb.onDone())
    act(() => { vi.advanceTimersByTime(1000) })
    expect(wsMock.resumeChat).not.toHaveBeenCalled()
  })

  it('an empty bubble is refetched 400 ms after done', () => {
    vi.useFakeTimers()
    renderStream()
    act(() => captured.cb.onText(''))
    act(() => captured.cb.onDone())
    act(() => { vi.advanceTimersByTime(1000) })
    expect(wsMock.resumeChat).toHaveBeenCalledWith('chat-1', { delta: true })
  })
})
