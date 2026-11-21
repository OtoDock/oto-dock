import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

import { useChatUrlEntry, type ChatUrlEntryParams } from '@/pages/agent/chat/useChatUrlEntry'

// The chat page is one route element for `/chat/:agent` and
// `/chat/:agent/:chatId`, so a navigation from outside the page reuses it.
// Whatever the URL names is what the page shows: a chat that leaves the
// address leaves the view with all its state, or it stays on screen under the
// new-chat address with its terminal gone (a notification's button that
// names the agent alone did that on a chat with a live terminal).

type Props = { agentName?: string; urlChatId?: string; chatId: string | null; connected?: boolean }

function fakes() {
  const resumeChat = vi.fn()
  const calls: Record<string, ReturnType<typeof vi.fn>> = {}
  const fn = (name: string) => (calls[name] ??= vi.fn())
  const refs = {
    currentMsgRef: { current: { id: 'old' } },
    thinkingBufRef: { current: 'old thinking' },
    discardingRef: { current: false },
    meetingSpeakerRef: { current: 'someone' },
    preWarmedRef: { current: 'prewarm-1' },
    lastResumedChatIdRef: { current: null as string | null },
    heldForAgentsRef: { current: { text: 'held' } as unknown },
    newChatModeSeededRef: { current: null as string | null },
  }
  const discard = vi.fn()
  const build = (p: Props) => ({
    ws: { connected: p.connected ?? true, resumeChat },
    agentName: p.agentName ?? 'dev',
    urlChatId: p.urlChatId,
    chatId: p.chatId,
    agentDefaultModel: 'model-a',
    warmupSends: { discard },
    ...refs,
    ...Object.fromEntries([
      'resetSession', 'seedExecMode', 'setMessages', 'setChatId', 'setSessionId',
      'setChatActiveLayer', 'setSelectedLayer', 'setWarmingUp', 'setTotalCost', 'setBilledCost',
      'setToolCost', 'setLimitReached', 'setLimitWarning', 'setContextUsed', 'setContextMax',
      'setSessionPlans', 'setCurrentTodos', 'setCurrentGoal', 'setMeetingActive',
      'setMeetingParticipants', 'setMeetingSpeaker', 'setMeetingRound', 'setTurnStartTime',
      'setThinkingActive', 'setCompressingActive', 'setWorkflows', 'setPermissionPending',
      'setAborting', 'setFindBarOpen', 'setFindInput', 'setFindQuery', 'setModel', 'setMode',
    ].map((name) => [name, fn(name)])),
  }) as unknown as ChatUrlEntryParams
  const reset = () => {
    for (const f of Object.values(calls)) f.mockClear()
    resumeChat.mockClear()
    discard.mockClear()
  }
  return { build, calls, refs, resumeChat, discard, reset }
}

function mount(f: ReturnType<typeof fakes>, initial: Props) {
  return renderHook((p: Props) => useChatUrlEntry(f.build(p)), { initialProps: initial })
}

describe('useChatUrlEntry', () => {
  it('opens the chat the URL names: a full reset, then a resume', () => {
    const f = fakes()
    mount(f, { urlChatId: 'c1', chatId: null })
    expect(f.calls.setChatId).toHaveBeenCalledWith('c1')
    expect(f.calls.setMessages).toHaveBeenCalledWith([])
    expect(f.calls.setSessionId).toHaveBeenCalledWith(null)
    expect(f.resumeChat).toHaveBeenCalledWith('c1')
    expect(f.refs.lastResumedChatIdRef.current).toBe('c1')
  })

  it('a chat that leaves the address for the new-chat view leaves the page with its state', () => {
    const f = fakes()
    const { rerender } = mount(f, { urlChatId: 'c1', chatId: null })
    f.reset()
    // The page shows c1 (its id is state now); a notification's button that
    // names the agent alone navigates to /chat/dev?recover=1.
    rerender({ urlChatId: undefined, chatId: 'c1' })
    expect(f.calls.setChatId).toHaveBeenCalledWith(null)
    expect(f.calls.setMessages).toHaveBeenCalledWith([])
    expect(f.calls.setSessionId).toHaveBeenCalledWith(null)
    expect(f.calls.setWarmingUp).toHaveBeenCalledWith(false)
    expect(f.discard).toHaveBeenCalled()
    expect(f.refs.currentMsgRef.current).toBeNull()
    expect(f.refs.heldForAgentsRef.current).toBeNull()
    expect(f.refs.discardingRef.current).toBe(true)
    // The terminal state and the sticky pick, as before.
    expect(f.calls.resetSession).toHaveBeenCalled()
    expect(f.calls.seedExecMode).toHaveBeenCalled()
    // Nothing is resumed for a new chat.
    expect(f.resumeChat).not.toHaveBeenCalled()
    expect(f.refs.lastResumedChatIdRef.current).toBeNull()
  })

  it('a chat of another agent leaves the page when its new-chat view opens', () => {
    const f = fakes()
    const { rerender } = mount(f, { agentName: 'dev', urlChatId: 'c1', chatId: null })
    f.reset()
    rerender({ agentName: 'ceo', urlChatId: undefined, chatId: 'c1' })
    expect(f.calls.setChatId).toHaveBeenCalledWith(null)
    expect(f.calls.setMessages).toHaveBeenCalledWith([])
    expect(f.resumeChat).not.toHaveBeenCalled()
  })

  it('a first visit to the new-chat view with no chat on screen clears nothing', () => {
    const f = fakes()
    mount(f, { urlChatId: undefined, chatId: null })
    expect(f.calls.resetSession).toHaveBeenCalled()
    expect(f.calls.setChatId).not.toHaveBeenCalled()
    expect(f.calls.setMessages).not.toHaveBeenCalled()
    expect(f.resumeChat).not.toHaveBeenCalled()
  })

  it('a reconnect on the new-chat view never clears a chat a first send just minted', () => {
    const f = fakes()
    const { rerender } = mount(f, { urlChatId: undefined, chatId: null })
    // The send minted a chat; its address has not landed yet and the socket
    // drops and comes back.
    rerender({ urlChatId: undefined, chatId: 'minted', connected: false })
    f.reset()
    rerender({ urlChatId: undefined, chatId: 'minted', connected: true })
    expect(f.calls.setChatId).not.toHaveBeenCalled()
    expect(f.calls.setMessages).not.toHaveBeenCalled()
    expect(f.calls.resetSession).not.toHaveBeenCalled()
  })

  it('the New chat button stamps the visit itself: the entry adds no second reset', () => {
    const f = fakes()
    const { rerender } = mount(f, { urlChatId: 'c1', chatId: null })
    f.reset()
    // handleNewChat cleared the chat and stamped the visit before its navigate.
    f.refs.newChatModeSeededRef.current = 'dev'
    rerender({ urlChatId: undefined, chatId: null })
    expect(f.calls.setChatId).not.toHaveBeenCalled()
    expect(f.calls.resetSession).not.toHaveBeenCalled()
  })

  it('the chat already open is not resumed again', () => {
    const f = fakes()
    const { rerender } = mount(f, { urlChatId: 'c1', chatId: null })
    f.reset()
    rerender({ urlChatId: 'c1', chatId: 'c1', connected: false })
    rerender({ urlChatId: 'c1', chatId: 'c1', connected: true })
    expect(f.resumeChat).not.toHaveBeenCalled()
    expect(f.calls.setChatId).not.toHaveBeenCalled()
  })
})
