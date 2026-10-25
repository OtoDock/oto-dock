// The stream under React StrictMode, which invokes every state updater
// twice: the second text frame draws, and a permission card after text
// draws. An updater that moved `currentMsgRef` while it ran lost both (the
// second run saw a ref no longer in the list and kept the old list).
import { describe, it, expect, vi } from 'vitest'
import { StrictMode, type ReactNode } from 'react'
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
  useDashboardWs: (cb: any) => { captured.cb = cb; return wsMock },
}))

function renderStrict() {
  const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>
  return renderHook(() => useChatStream({
    agents: [], initialChatId: 'chat-1', queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
  }), { wrapper })
}

describe('the stream under StrictMode', () => {
  it('draws every text frame', () => {
    const { result } = renderStrict()
    act(() => captured.cb.onText('the'))
    act(() => captured.cb.onText(' quick brown fox'))
    act(() => captured.cb.onText(' jumps'))
    expect(result.current.messages).toHaveLength(1)
    expect((result.current.messages[0].blocks[0] as any).content).toBe('the quick brown fox jumps')
  })

  it('draws a permission card after text, and resolves it', () => {
    const { result } = renderStrict()
    act(() => captured.cb.onText('the'))
    act(() => captured.cb.onPermissionPrompt({ request_id: 'r1', tool_name: 'Bash', tool_input: { command: 'ls' } }))
    const blocks = result.current.messages[0].blocks
    expect(blocks.map((b: any) => b.type)).toEqual(['text', 'permission'])
    act(() => result.current.handlePermissionRespond('r1', true))
    expect((result.current.messages[0].blocks[1] as any).resolved).toBe(true)
    act(() => captured.cb.onText(' and more'))
    const after = result.current.messages[0].blocks as any[]
    expect(after.map((b) => b.type)).toEqual(['text', 'permission', 'text'])
    expect(after[2].content).toBe(' and more')
  })

  it('draws a tool block and its end after text', () => {
    const { result } = renderStrict()
    act(() => captured.cb.onText('running'))
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't1' }))
    act(() => captured.cb.onToolEnd({ name: 'Bash', tool_id: 't1' }))
    const blocks = result.current.messages[0].blocks as any[]
    expect(blocks.map((b) => b.type)).toEqual(['text', 'tool'])
    expect(blocks[1].status).toBe('done')
  })
})
