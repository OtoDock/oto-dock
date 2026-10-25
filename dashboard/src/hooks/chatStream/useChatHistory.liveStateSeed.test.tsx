/**
 * Opening a generating chat paints once: a `chat_history` that says its
 * live attach follows (`live_pending`) holds its rows, and the `live_state`
 * that follows paints them with the live answer in one update, the timer
 * already running. The held rows paint as they are on the next turn end or
 * after a short wait, and a switch to another chat drops them unpainted.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'
import { useChatStore } from '@/store/chatStore'

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

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }),
  )
}

const rows = [
  { id: 1, role: 'user', content: 'earlier question', event_type: '', event_data: '', created_at: '' },
  { id: 2, role: 'assistant', content: 'earlier answer', event_type: '', event_data: '', created_at: '' },
  { id: 3, role: 'user', content: 'the prompt', event_type: '', event_data: '', created_at: '' },
]

const live = {
  type: 'live_state', chat_id: 'chat-1', streaming: true, started_at: 1_700_000_000,
  live_blocks: [{ type: 'text', content: 'answering now' }],
}

const texts = (result: any) => result.current.messages.map(
  (m: any) => m.blocks.find((b: any) => b.type === 'text')?.content ?? '')

describe('the one-paint open of a generating chat', () => {
  afterEach(() => { vi.useRealTimers() })

  it('holds the history and paints it with the live state at once', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: rows,
                                          live_pending: true }))
    expect(result.current.messages).toEqual([])
    act(() => captured.cb.onLiveState(live))
    expect(texts(result)).toEqual(['earlier question', 'earlier answer', 'the prompt', 'answering now'])
    expect(result.current.turnStartTime).toBe(1_700_000_000_000)
  })

  it('paints the held rows after the wait when no live state comes', () => {
    vi.useFakeTimers()
    const { result } = renderStream()
    useChatStore.getState().setStreaming('chat-1')
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: rows,
                                          live_pending: true }))
    expect(result.current.messages).toEqual([])
    act(() => { vi.advanceTimersByTime(1600) })
    expect(texts(result)).toEqual(['earlier question', 'earlier answer', 'the prompt'])
    expect(useChatStore.getState().byChat['chat-1'].status).toBe('ready')
  })

  it('paints the held rows on the turn end', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: rows,
                                          live_pending: true }))
    act(() => captured.cb.onDone())
    expect(texts(result).slice(0, 3)).toEqual(['earlier question', 'earlier answer', 'the prompt'])
  })

  it('drops the held rows on a switch to another chat', () => {
    vi.useFakeTimers()
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: rows,
                                          live_pending: true }))
    act(() => result.current.setChatId('chat-2'))
    act(() => { vi.advanceTimersByTime(2000) })
    expect(result.current.messages).toEqual([])
  })

  it('a history without the flag paints at once, as before', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: rows }))
    expect(texts(result)).toEqual(['earlier question', 'earlier answer', 'the prompt'])
  })
})
