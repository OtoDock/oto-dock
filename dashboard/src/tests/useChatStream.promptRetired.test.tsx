/**
 * A prompt whose wait ended with no answer (the hook's caller went away, the
 * wait ran out, a Stop or a close released it) is retired by the proxy: its
 * card leaves the transcript (a plan review closes as cancelled) and stops
 * gating the composer, so the card a retried hook call raises is the one on
 * screen and one answer resolves it. A stale frame changes nothing.
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

describe('a retired prompt', () => {
  beforeEach(() => {
    wsMock.sendPermission.mockClear()
    useChatStore.getState().clear('chat-1')
  })

  it('takes its permission card away; the retried call raises the one answered', () => {
    useChatStore.getState().setStreaming('chat-1')
    const { result } = renderStream()
    act(() => captured.cb.onPermissionPrompt({ request_id: 'req-dead', tool_name: 'Bash', tool_input: { command: 'ls' } }))
    expect(cards(result, 'permission').map((b) => b.requestId)).toEqual(['req-dead'])
    expect(result.current.permissionPending).toBe(true)
    expect(result.current.turnStartTime).toBeNull()  // paused on the card

    act(() => captured.cb.onPromptRetired({ request_id: 'req-dead' }))
    expect(cards(result, 'permission')).toEqual([])
    expect(result.current.permissionPending).toBe(false)
    expect(result.current.turnStartTime).not.toBeNull()  // the turn goes on

    act(() => captured.cb.onPermissionPrompt({ request_id: 'req-retry', tool_name: 'Bash', tool_input: { command: 'ls' } }))
    act(() => result.current.handlePermissionRespond('req-retry', true))
    expect(wsMock.sendPermission).toHaveBeenCalledTimes(1)
    expect(wsMock.sendPermission).toHaveBeenCalledWith('req-retry', true)
  })

  it('changes nothing for an unknown or answered id', () => {
    useChatStore.getState().setStreaming('chat-1')
    const { result } = renderStream()
    act(() => captured.cb.onPermissionPrompt({ request_id: 'req-a', tool_name: 'Bash', tool_input: {} }))
    const paused = result.current.turnStartTime
    act(() => captured.cb.onPromptRetired({ request_id: 'req-unknown' }))
    expect(cards(result, 'permission').map((b) => b.requestId)).toEqual(['req-a'])
    expect(result.current.permissionPending).toBe(true)
    expect(result.current.turnStartTime).toBe(paused)
    act(() => result.current.handlePermissionRespond('req-a', false))
    const answered = result.current.turnStartTime
    act(() => captured.cb.onPromptRetired({ request_id: 'req-a' }))
    expect(cards(result, 'permission')).toEqual([expect.objectContaining({ requestId: 'req-a', resolved: true, approved: false })])
    expect(result.current.turnStartTime).toBe(answered)
  })

  it('keeps the composer gated while another card waits', () => {
    const { result } = renderStream()
    act(() => captured.cb.onPermissionPrompt({ request_id: 'req-a', tool_name: 'Bash', tool_input: {} }))
    act(() => captured.cb.onQuestion({ request_id: 'req-q', tool_name: 'request_user_input', tool_input: {} }))
    act(() => captured.cb.onPromptRetired({ request_id: 'req-a' }))
    expect(cards(result, 'permission')).toEqual([])
    expect(result.current.permissionPending).toBe(true)  // the held question still gates
  })

  it('closes a plan review card as cancelled and leaves the timer of an idle chat alone', () => {
    const { result } = renderStream()
    act(() => captured.cb.onPlanReview({ request_id: 'req-plan', plan: 'p', tool_input: {}, filename: 'plan.md' }))
    act(() => captured.cb.onPromptRetired({ request_id: 'req-plan' }))
    expect(cards(result, 'plan_review')).toEqual([expect.objectContaining({ requestId: 'req-plan', resolved: true, action: 'reject' })])
    expect(result.current.permissionPending).toBe(false)
    expect(result.current.turnStartTime).toBeNull()  // the chat is not streaming
  })
})
