/**
 * A `list_card` frame appends the list block to the agent's message in the
 * live stream, the way the action card does; it gates nothing, so the
 * composer stays free.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'
import { useChatStore } from '@/store/chatStore'

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

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }),
  )
}

describe('a list card in the stream', () => {
  beforeEach(() => {
    useChatStore.getState().clear('chat-1')
  })

  it('appends the list block with its kind, filters, title and person, gating nothing', () => {
    useChatStore.getState().setStreaming('chat-1')
    const { result } = renderStream()
    act(() => captured.cb.onText('Here are your tasks. '))
    act(() => captured.cb.onListCard({
      type: 'list_card', chat_id: 'chat-1', kind: 'tasks', filters: { scope: 'mine' }, title: 'Your scheduled tasks', person: 'u1',
    }))
    const blocks = result.current.messages.flatMap((b) => b.blocks)
    expect(blocks.find((b) => b.type === 'list')).toEqual({
      type: 'list', kind: 'tasks', filters: { scope: 'mine' }, title: 'Your scheduled tasks', person: 'u1',
    })
    expect(result.current.permissionPending).toBe(false)
  })

  it('reads a frame with no filters as an empty set', () => {
    useChatStore.getState().setStreaming('chat-1')
    const { result } = renderStream()
    act(() => captured.cb.onListCard({ type: 'list_card', chat_id: 'chat-1', kind: 'machines', title: 'Remote machines', person: 'u1' }))
    const block = result.current.messages.flatMap((b) => b.blocks).find((b) => b.type === 'list')
    expect(block).toEqual({ type: 'list', kind: 'machines', filters: {}, title: 'Remote machines', person: 'u1' })
  })
})
