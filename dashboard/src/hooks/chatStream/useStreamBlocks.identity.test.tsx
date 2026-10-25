// The streaming bubble is found by its id, so a reply keeps drawing after an
// update that rebuilt the message list: a foreground sub-agent's report, an
// answered question, a delegate result with nothing to show, a meeting's
// start banner. Each of these
// replaced the message object the identity check compared against, and the
// rest of the turn (a permission card included) was dropped until the next
// history load.
import { describe, it, expect, vi } from 'vitest'
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

function renderStream() {
  return renderHook(() => useChatStream({
    agents: [], initialChatId: 'chat-1', queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
  }))
}

const types = (m: any) => m.blocks.map((b: any) => b.type)

describe('the stream target by id', () => {
  it("keeps drawing after a foreground sub-agent's report", () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Looking'))
    act(() => captured.cb.onTaskSpawn({ description: 'scan', subagent_type: 'Explore', tool_use_id: 'a1', run_in_background: false }))
    act(() => captured.cb.onToolResult({ tool_name: 'Task', tool_use_id: 'a1', result_content: 'found it' }))
    act(() => captured.cb.onText(' and then'))
    act(() => captured.cb.onPermissionPrompt({ request_id: 'r1', tool_name: 'Bash', tool_input: {} }))
    expect(result.current.messages).toHaveLength(1)
    const m = result.current.messages[0] as any
    expect(types(m)).toEqual(['text', 'subagent', 'text', 'permission'])
    expect(m.blocks[1].toolResult).toBe('found it')
    expect(m.blocks[2].content).toBe(' and then')
  })

  it('keeps drawing after a meeting starts', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Convening'))
    act(() => captured.cb.onSystem({ type: 'system', subtype: 'meeting_started', participants: [], max_turns: 3 }))
    act(() => captured.cb.onText(' the room'))
    expect(result.current.messages).toHaveLength(1)
    const m = result.current.messages[0] as any
    expect(types(m)).toEqual(['text', 'system', 'text'])
    expect(m.blocks[1].subtype).toBe('meeting_started')
    expect(m.blocks[2].content).toBe(' the room')
  })

  it('keeps drawing after a structured question is answered', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('One thing:'))
    act(() => captured.cb.onQuestion({ tool_name: 'request_user_input', tool_input: {}, request_id: 'q1' }))
    act(() => result.current.handleQuestionAnswerStructured('q1', { color: { answers: ['blue'] } }))
    act(() => captured.cb.onText(' blue it is'))
    expect(result.current.messages).toHaveLength(1)
    const m = result.current.messages[0] as any
    expect(types(m)).toEqual(['text', 'question', 'text'])
    expect(m.blocks[1].answered).toBe(true)
    expect(m.blocks[2].content).toBe(' blue it is')
  })

  it('keeps drawing after a delegate result that shows nothing', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Delegating'))
    act(() => captured.cb.onDelegateSpawn({ task_name: 't', agent: 'worker', task_id: 'd1', prompt_preview: 'p' }))
    act(() => captured.cb.onDelegateResult({ task_name: 't', agent: 'worker', task_id: 'd1', status: 'completed' }))
    act(() => captured.cb.onText(' done'))
    expect(result.current.messages).toHaveLength(1)
    const m = result.current.messages[0] as any
    expect(types(m)).toEqual(['text', 'delegate', 'text'])
    expect(m.blocks[1].status).toBe('completed')
    expect(m.blocks[2].content).toBe(' done')
  })
})
