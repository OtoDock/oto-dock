/**
 * A held message the drive gate refused as its chat was viewed again
 * arrives as a live `system` frame (`undelivered_input`, `reason: queued`):
 * the live branch keeps its `reason`, so the card says "This message was
 * not sent" as the reloaded row does.
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

describe('a live undelivered-input card', () => {
  beforeEach(() => {
    useChatStore.getState().clear('chat-1')
  })

  it('keeps its reason', () => {
    const { result } = renderHook(() =>
      useChatStream({
        agents: [],
        initialChatId: 'chat-1',
        queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
      }),
    )
    act(() => captured.cb.onSystem({
      type: 'system', subtype: 'undelivered_input', reason: 'queued',
      message: 'for the agent', chat_id: 'chat-1',
    }))
    const blocks = result.current.messages.flatMap((m) => m.blocks).filter((b: any) => b.type === 'system') as any[]
    expect(blocks).toEqual([
      expect.objectContaining({ subtype: 'undelivered_input', reason: 'queued', message: 'for the agent' }),
    ])
  })
})
