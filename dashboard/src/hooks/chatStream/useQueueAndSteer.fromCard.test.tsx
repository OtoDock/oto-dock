/**
 * A `queue_sent` or `steered` frame with the card mark: the live bubble of
 * the person's message wears `fromCard`, as its stored row will; a frame
 * without the mark draws a plain bubble.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
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

const CARD = { card_id: 'card-1', button: 'no' }

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }),
  )
}

describe("a card's message in the live stream", () => {
  beforeEach(() => {
    useChatStore.getState().clear('chat-1')
  })

  it('marks the bubble a queue_sent frame with the card draws, and no other', () => {
    const { result } = renderStream()
    act(() => captured.cb.onQueueSent({ type: 'queue_sent', chat_id: 'chat-1', queue_ids: ['q1'], message_ids: [7], text: 'Not now, thanks.', card: CARD }))
    act(() => captured.cb.onQueueSent({ type: 'queue_sent', chat_id: 'chat-1', queue_ids: ['q2'], message_ids: [8], text: 'And this.' }))
    const users = result.current.messages.filter((m) => m.role === 'user')
    expect(users.map((m) => [m.blocks[0], m.fromCard])).toEqual([
      [{ type: 'text', content: 'Not now, thanks.' }, true],
      [{ type: 'text', content: 'And this.' }, undefined],
    ])
  })

  it('marks a steered message the frame places by its row', () => {
    const { result } = renderStream()
    act(() => captured.cb.onSteered({ type: 'steered', chat_id: 'chat-1', text: 'Not now, thanks.', message_id: 9, card: CARD }))
    const user = result.current.messages.find((m) => m.role === 'user')
    expect(user?.id).toBe('db-9')
    expect(user?.steered).toBe(true)
    expect(user?.fromCard).toBe(true)
  })
})
