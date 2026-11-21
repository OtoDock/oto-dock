/**
 * The dashboard socket shows a chat's frames only on that chat's view: a
 * delegated result that names another chat or none, and another chat's
 * server turn, change nothing on screen; a reconnect resumes the chat on
 * screen and no other, so the socket cannot end on another live chat; and
 * the view leaving its chat for the new-chat view tells the socket.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from '@testing-library/react'
import { useChatStore } from '@/store/chatStore'
import { FakeWebSocket } from './fakeWebSocket'
import { connectDashboardWs } from './dashboardWsHarness'

function sent(ws: FakeWebSocket): Array<Record<string, unknown>> {
  return ws.sent.map((s) => JSON.parse(s))
}

function frame(ws: FakeWebSocket, msg: Record<string, unknown>) {
  act(() => { ws.onmessage?.({ data: JSON.stringify(msg) }) })
}

const RESULT = {
  type: 'delegate_result', task_id: 't-1', task_name: 'lane', agent: 'worker',
  output_text: "the worker's report", status: 'completed',
}

describe('useDashboardWs: a frame of one chat never changes another chat\'s view', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    useChatStore.setState({ byChat: {} })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows a delegated result only on the chat it names', () => {
    const onDelegateResult = vi.fn()
    const { ws } = connectDashboardWs(() => ({ viewedChatId: 'chat-b', onDelegateResult }))
    frame(ws, RESULT)                                   // names no chat
    frame(ws, { ...RESULT, chat_id: 'chat-a' })         // names another chat
    expect(onDelegateResult).not.toHaveBeenCalled()
    frame(ws, { ...RESULT, chat_id: 'chat-b' })
    expect(onDelegateResult).toHaveBeenCalledTimes(1)
  })

  it('keeps a result away from the new-chat view', () => {
    const onDelegateResult = vi.fn()
    const { ws } = connectDashboardWs(() => ({ viewedChatId: null, onDelegateResult }))
    frame(ws, RESULT)
    frame(ws, { ...RESULT, chat_id: 'chat-a' })
    expect(onDelegateResult).not.toHaveBeenCalled()
  })

  it("lights only the other chat's dot when its server turn starts", () => {
    const onServerTurnStart = vi.fn()
    const { hook, ws } = connectDashboardWs(() => ({ viewedChatId: 'chat-b', onServerTurnStart }))
    frame(ws, { type: 'server_turn_start', chat_id: 'chat-a', cause: 'delegate_result' })
    expect(onServerTurnStart).not.toHaveBeenCalled()
    expect(hook.result.current.streaming).toBe(false)
    expect(useChatStore.getState().byChat['chat-a']?.status).toBe('streaming')
    frame(ws, { type: 'server_turn_start', chat_id: 'chat-b', cause: 'delegate_result' })
    expect(onServerTurnStart).toHaveBeenCalledTimes(1)
    expect(hook.result.current.streaming).toBe(true)
  })

  it('resumes only the chat on screen after a reconnect', () => {
    vi.useFakeTimers()
    try {
      const { hook, ws } = connectDashboardWs(() => ({ viewedChatId: 'chat-x' }))
      act(() => { hook.result.current.resumeChat('chat-x') })
      // Two live chats, the one on screen met first: the socket must not
      // end on the other one.
      act(() => {
        useChatStore.getState().setStreaming('chat-x')
        useChatStore.getState().setStreaming('chat-y')
      })
      act(() => { ws.onclose?.({ code: 1006, reason: '' }) })
      act(() => { vi.advanceTimersByTime(60_000) })
      const fresh = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
      expect(fresh).not.toBe(ws)
      act(() => { fresh.onopen?.() })
      const resumes = sent(fresh).filter((m) => m.type === 'resume_chat').map((m) => m.chat_id)
      expect(resumes).toEqual(['chat-x'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('tells the socket when the view leaves its chat', () => {
    const { hook, ws } = connectDashboardWs(() => ({ viewedChatId: 'chat-x' }))
    act(() => { hook.result.current.leaveChat() })
    expect(sent(ws).filter((m) => m.type === 'leave_chat')).toHaveLength(1)
  })
})
