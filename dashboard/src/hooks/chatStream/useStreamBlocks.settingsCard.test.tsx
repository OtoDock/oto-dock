/**
 * A `settings_card` frame appends the card to the agent's message in the
 * live stream, the way the question card does; it gates nothing, so the
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

describe('a settings card in the stream', () => {
  beforeEach(() => {
    useChatStore.getState().clear('chat-1')
  })

  it('appends the card block with the id and the label, gating nothing', () => {
    useChatStore.getState().setStreaming('chat-1')
    const { result } = renderStream()
    act(() => captured.cb.onText('Here it is. '))
    act(() => captured.cb.onSettingsCard({ type: 'settings_card', chat_id: 'chat-1', setting: 'user.general.wake-word', label: 'Wake word', catalog_version: 1 }))
    const blocks = result.current.messages.flatMap((m) => m.blocks)
    expect(blocks.find((b) => b.type === 'setting')).toEqual({
      type: 'setting', settingId: 'user.general.wake-word', label: 'Wake word', catalogVersion: 1,
    })
    expect(result.current.permissionPending).toBe(false)
  })
})
