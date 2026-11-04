/**
 * An `action_card` frame appends the card to the agent's message in the
 * live stream, the way the settings card does; it gates nothing, so the
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

describe('an action card in the stream', () => {
  beforeEach(() => {
    useChatStore.getState().clear('chat-1')
  })

  it('appends the card block with its words and buttons, gating nothing', () => {
    useChatStore.getState().setStreaming('chat-1')
    const { result } = renderStream()
    act(() => captured.cb.onText('Here is my proposal. '))
    act(() => captured.cb.onActionCard({
      type: 'action_card', chat_id: 'chat-1', card_id: 'card-1', title: 'Install Yacht Crew',
      text: 'Installs the Yacht Crew template as the agent yacht-crew.', note: '',
      buttons: [{ id: 'go', label: 'Install', kind: 'do' }], options: [],
    }))
    const blocks = result.current.messages.flatMap((b) => b.blocks)
    expect(blocks.find((b) => b.type === 'card')).toEqual({
      type: 'card', cardId: 'card-1', title: 'Install Yacht Crew',
      text: 'Installs the Yacht Crew template as the agent yacht-crew.', note: '',
      buttons: [{ id: 'go', label: 'Install', kind: 'do' }], options: [],
    })
    expect(result.current.permissionPending).toBe(false)
  })
})
