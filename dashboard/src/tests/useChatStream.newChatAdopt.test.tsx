/**
 * `warmup_started` adoption on the new-chat page: the hook adopts a chat id
 * (and lets the page navigate to it) only for a chat THAT warmup minted.
 * The same frame fires for an existing chat re-warmed by a send, and a
 * reconnect on the new-chat page replays every warming chat's frame —
 * adopting a chat still spawning elsewhere walked the user into it.
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

function renderNewChat(extra: (data: any) => void) {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: null,
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
      onWarmupStartedExtra: extra,
    }),
  )
}

describe('warmup_started adoption on the new-chat page', () => {
  it('ignores a replayed frame of a chat this warmup did not mint', () => {
    const extra = vi.fn()
    const { result } = renderNewChat(extra)
    act(() => captured.cb.onWarmupStarted({ chat_id: 'chat-elsewhere', agent: 'a', new_chat: false }))
    expect(result.current.chatId).toBeNull()
    expect(extra).not.toHaveBeenCalled()
    // A frame with no flag at all is not ours either.
    act(() => captured.cb.onWarmupStarted({ chat_id: 'chat-unknown', agent: 'a' }))
    expect(result.current.chatId).toBeNull()
    expect(extra).not.toHaveBeenCalled()
  })

  it('adopts the chat this warmup minted and lets the page navigate', () => {
    const extra = vi.fn()
    const { result } = renderNewChat(extra)
    act(() => captured.cb.onWarmupStarted({ chat_id: 'chat-minted', agent: 'a', new_chat: true }))
    expect(result.current.chatId).toBe('chat-minted')
    expect(extra).toHaveBeenCalledWith(expect.objectContaining({ chat_id: 'chat-minted' }))
  })
})
