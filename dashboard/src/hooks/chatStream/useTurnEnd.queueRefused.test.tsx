/**
 * A queued or busy send the proxy refused arrives as an `error` naming its
 * queue id: no turn ended, so the warmup, the bubble and the markers stay as
 * they are, and a card says what was refused and whether the send is back
 * in the composer (the socket hook handed it back already). A refusal for a
 * send typed into another chat adds no card here. A bare error during the
 * warmup still resets it.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'

const wsMock = vi.hoisted(() => ({
  streaming: false,
  sendMessage: vi.fn(),
  resumeChat: vi.fn(),
  subscribe: vi.fn(() => () => {}),
}))
const captured = vi.hoisted(() => ({ cb: null as any }))
vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

function render() {
  const onWarmupReset = vi.fn()
  const hook = renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
      isWarmingUp: true,
      onWarmupReset,
    } as any),
  )
  act(() => {
    hook.result.current.setMessages([
      { id: 'user-1', role: 'user', blocks: [{ type: 'text', content: 'first' }], createdAt: '' },
      { id: 'asst-1', role: 'assistant', blocks: [], createdAt: '' },
    ] as any)
  })
  return { ...hook, onWarmupReset }
}

describe('a refused queued send', () => {
  it('keeps the warmup and the bubble and shows the card', () => {
    const { result, onWarmupReset } = render()
    act(() => captured.cb.onError('Too many queued messages.', undefined,
                                  { queueId: 'q-1', chatId: 'chat-1', returned: true }))
    expect(onWarmupReset).not.toHaveBeenCalled()
    const msgs = result.current.messages
    // The bubble pair stays (a bare error would remove it); the card lands
    // in the open assistant bubble.
    expect(msgs[0].id).toBe('user-1')
    expect(msgs[msgs.length - 1].blocks).toEqual([
      { type: 'system', subtype: 'queue_refused', message: 'Too many queued messages.', returned: true },
    ])
  })

  it('a send the socket no longer remembered is marked as not returned', () => {
    const { result } = render()
    act(() => captured.cb.onError('late', undefined, { queueId: 'q-2', returned: false }))
    const msgs = result.current.messages
    expect(msgs[msgs.length - 1].blocks).toEqual([
      { type: 'system', subtype: 'queue_refused', message: 'late', returned: false },
    ])
  })

  it('a refusal of a send typed into another chat adds no card here', () => {
    const { result, onWarmupReset } = render()
    act(() => captured.cb.onError('no photos', undefined,
                                  { queueId: 'q-3', chatId: 'chat-2', returned: true }))
    expect(onWarmupReset).not.toHaveBeenCalled()
    expect(result.current.messages[1].blocks).toEqual([])
  })

  it('a bare error during the warmup still resets it', () => {
    const { result, onWarmupReset } = render()
    act(() => captured.cb.onError('Unknown agent'))
    expect(onWarmupReset).toHaveBeenCalledTimes(1)
    expect(result.current.messages).toHaveLength(0)
  })
})
