/**
 * A card answered on another socket of the chat (another tab, another
 * device) arrives as `permission_resolved`: the card closes here the way its
 * own answer would close it (a permission resolved with the answer, a plan
 * review with its action, a held question answered), the composer frees once
 * no card still gates, and the timer runs again while the chat streams. A
 * frame for an unknown or already-answered id changes nothing.
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

function cards(result: { current: ReturnType<typeof useChatStream> }, type: string) {
  return result.current.messages.flatMap((m) => m.blocks).filter((b: any) => b.type === type) as any[]
}

describe('a card answered on another socket', () => {
  beforeEach(() => {
    wsMock.sendPermission.mockClear()
    useChatStore.getState().clear('chat-1')
  })

  it('closes the permission card with the answer and frees the composer', () => {
    useChatStore.getState().setStreaming('chat-1')
    const { result } = renderStream()
    act(() => captured.cb.onPermissionPrompt({ request_id: 'req-a', tool_name: 'Bash', tool_input: { command: 'ls' } }))
    expect(result.current.permissionPending).toBe(true)
    expect(result.current.turnStartTime).toBeNull()

    act(() => captured.cb.onPermissionResolved({ request_id: 'req-a', approved: false }))
    expect(cards(result, 'permission')).toEqual([
      expect.objectContaining({ requestId: 'req-a', resolved: true, approved: false }),
    ])
    expect(result.current.permissionPending).toBe(false)
    expect(result.current.turnStartTime).not.toBeNull()
    // Nothing was sent: the other socket answered.
    expect(wsMock.sendPermission).not.toHaveBeenCalled()
  })

  it('changes nothing for an unknown or answered id', () => {
    useChatStore.getState().setStreaming('chat-1')
    const { result } = renderStream()
    act(() => captured.cb.onPermissionPrompt({ request_id: 'req-a', tool_name: 'Bash', tool_input: {} }))
    act(() => captured.cb.onPermissionResolved({ request_id: 'req-unknown', approved: true }))
    expect(cards(result, 'permission').map((b) => [b.requestId, b.resolved])).toEqual([['req-a', undefined]])
    expect(result.current.permissionPending).toBe(true)
    act(() => result.current.handlePermissionRespond('req-a', true))
    const answered = result.current.turnStartTime
    act(() => captured.cb.onPermissionResolved({ request_id: 'req-a', approved: false }))
    // The own answer stands: the frame for an answered card is idempotent.
    expect(cards(result, 'permission')).toEqual([expect.objectContaining({ requestId: 'req-a', resolved: true, approved: true })])
    expect(result.current.turnStartTime).toBe(answered)
  })

  it('keeps the composer gated while another card waits', () => {
    const { result } = renderStream()
    act(() => captured.cb.onPermissionPrompt({ request_id: 'req-a', tool_name: 'Bash', tool_input: {} }))
    act(() => captured.cb.onQuestion({ request_id: 'req-q', tool_name: 'request_user_input', tool_input: {} }))
    act(() => captured.cb.onPermissionResolved({ request_id: 'req-a', approved: true }))
    expect(cards(result, 'permission')).toEqual([expect.objectContaining({ requestId: 'req-a', resolved: true })])
    expect(result.current.permissionPending).toBe(true)
    act(() => captured.cb.onPermissionResolved({ request_id: 'req-q' }))
    expect(cards(result, 'question')).toEqual([expect.objectContaining({ requestId: 'req-q', answered: true })])
    expect(result.current.permissionPending).toBe(false)
  })

  it('closes a plan review with its action', () => {
    const { result } = renderStream()
    act(() => captured.cb.onPlanReview({ request_id: 'req-plan', plan: 'p', tool_input: {}, filename: 'plan.md' }))
    act(() => captured.cb.onPermissionResolved({ request_id: 'req-plan', action: 'implement_ask' }))
    expect(cards(result, 'plan_review')).toEqual([
      expect.objectContaining({ requestId: 'req-plan', resolved: true, action: 'implement_ask' }),
    ])
    expect(result.current.permissionPending).toBe(false)
    expect(result.current.turnStartTime).toBeNull()  // the chat is not streaming
  })
})
