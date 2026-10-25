/**
 * A watched task chat's turn end arrives as `chat_history_delta`: the rows the
 * view lacks, appended to the rows it holds (deduped by id, in id order) and
 * the view re-derived from all of them. A delta for another chat is dropped,
 * the finished turn's live bubble gives way to its rows, and the post-done
 * refetch asks the server for a delta.
 */
import { describe, it, expect, vi } from 'vitest'
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
  subscribe: vi.fn(() => () => {}),
}))

const captured = vi.hoisted(() => ({ cb: null as any }))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

function renderStream(extra: Record<string, unknown> = {}) {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'task-run-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
      ...extra,
    }),
  )
}

function row(id: number, role: string, content: string, chat = 'task-run-1') {
  return { id, chat_id: chat, role, content, event_type: null, event_data: null, created_at: '2026-10-01T00:00:00Z' }
}

function texts(result: any): string[] {
  return result.current.messages.map((m: any) =>
    m.blocks.filter((b: any) => b.type === 'text').map((b: any) => b.content).join(''))
}

describe('a task chat delta', () => {
  it('appends the rows the view lacks, deduped and in id order', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'task-run-1', agent: 'a',
      messages: [row(1, 'user', 'first'), row(2, 'assistant', 'one')], has_more: false,
    }))
    expect(texts(result)).toEqual(['first', 'one'])
    act(() => captured.cb.onChatHistoryDelta({
      type: 'chat_history_delta', chat_id: 'task-run-1', agent: 'a', since_id: 2,
      // row 2 again (a user row above a cutoff rides twice), a sibling run's
      // row with a lower id than the newest, and the new turn
      messages: [row(2, 'assistant', 'one'), row(4, 'assistant', 'two'), row(3, 'user', 'sib', 'task-run-2')],
    }))
    expect(texts(result)).toEqual(['first', 'one', 'sib', 'two'])
  })

  it('drops a delta for another chat and keeps the view', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'task-run-1', agent: 'a',
      messages: [row(1, 'user', 'first')], has_more: false,
    }))
    act(() => captured.cb.onChatHistoryDelta({
      type: 'chat_history_delta', chat_id: 'other', agent: 'a', since_id: 1,
      messages: [row(9, 'assistant', 'nope')],
    }))
    expect(texts(result)).toEqual(['first'])
  })

  it('replaces the finished turn\'s live bubble with its rows', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'task-run-1', agent: 'a',
      messages: [row(1, 'user', 'first')], has_more: false,
    }))
    act(() => captured.cb.onText('streamed answer'))
    expect(texts(result)).toEqual(['first', 'streamed answer'])
    act(() => captured.cb.onChatHistoryDelta({
      type: 'chat_history_delta', chat_id: 'task-run-1', agent: 'a', since_id: 1,
      messages: [row(2, 'assistant', 'streamed answer')], total_cost: 0.5,
    }))
    expect(texts(result)).toEqual(['first', 'streamed answer'])
    expect(result.current.messages.every((m: any) => m.id.startsWith('db-'))).toBe(true)
    expect(result.current.totalCost).toBe(0.5)
  })

  it('applies the meeting restore as the full history does', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'task-run-1', agent: 'a',
      messages: [row(1, 'user', 'first')], has_more: false,
      restore: { todos: [], meeting: { active: true, participants: ['alpha'], max_turns: 12 } },
    }))
    expect(result.current.meetingActive).toBe(true)
    act(() => captured.cb.onChatHistoryDelta({
      type: 'chat_history_delta', chat_id: 'task-run-1', agent: 'a', since_id: 1,
      messages: [row(2, 'assistant', 'done')],
      restore: { todos: [], meeting: { active: true, participants: [{ slug: 'beta', display_name: 'Beta', color: '#fff' }], max_turns: 8 } },
    }))
    expect(result.current.meetingParticipants).toEqual([{ slug: 'beta', display_name: 'Beta', color: '#fff' }])
    expect(result.current.meetingMaxRounds).toBe(8)
    // The meeting concluded during the turn: the delta's restore says so.
    act(() => captured.cb.onChatHistoryDelta({
      type: 'chat_history_delta', chat_id: 'task-run-1', agent: 'a', since_id: 2,
      messages: [row(3, 'assistant', 'wrap')],
      restore: { todos: [], meeting: null },
    }))
    expect(result.current.meetingActive).toBe(false)
  })

  it('asks for a delta on the post-done refetch of a seeded view', () => {
    vi.useFakeTimers()
    try {
      renderStream({ enableDefensiveRefetch: true })
      act(() => captured.cb.onLiveState({ chat_id: 'task-run-1', streaming: true, live_blocks: [], active_tools: [], active_agents: [] }))
      act(() => captured.cb.onDone())
      act(() => { vi.advanceTimersByTime(500) })
      expect(wsMock.resumeChat).toHaveBeenCalledWith('task-run-1', { delta: true })
    } finally {
      vi.useRealTimers()
    }
  })
})
