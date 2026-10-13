/**
 * A send below the editor role on a Shared-only agent is refused before a
 * chat is minted (`warmup_failed` with no `chat_id`): the new-chat page shows
 * the role card under the person's bubble; a page already on a chat (it was
 * switched in the moment) leaves its own chat alone.
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

const REFUSAL = 'This agent is set to Shared only, so its chats and tasks run as the agent itself, which takes the editor role or above (this one would run as contributor).'

function render(initialChatId: string | null) {
  const onWarmupFailedReset = vi.fn()
  const hook = renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId,
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
      onWarmupFailedReset,
    } as any),
  )
  return { ...hook, onWarmupFailedReset }
}

const systemBlocks = (result: { current: ReturnType<typeof useChatStream> }) =>
  result.current.messages.flatMap((m) => m.blocks).filter((b: any) => b.type === 'system') as any[]

describe('a warmup refused before the chat exists', () => {
  it('shows the role card on the new-chat page', () => {
    const { result, onWarmupFailedReset } = render(null)
    act(() => {
      result.current.setMessages([
        { id: 'user-1', role: 'user', blocks: [{ type: 'text', content: 'my question' }], createdAt: '' },
        { id: 'asst-1', role: 'assistant', blocks: [], createdAt: '' },
      ] as any)
    })
    act(() => captured.cb.onWarmupFailed({ type: 'warmup_failed', error: REFUSAL, reason: 'below_editor' }))
    expect(onWarmupFailedReset).toHaveBeenCalled()
    expect(systemBlocks(result)).toEqual([expect.objectContaining({ subtype: 'below_editor', message: REFUSAL })])
  })

  it('leaves a chat the page is already on alone', () => {
    const { result, onWarmupFailedReset } = render('chat-9')
    act(() => captured.cb.onWarmupFailed({ type: 'warmup_failed', error: REFUSAL, reason: 'below_editor' }))
    expect(onWarmupFailedReset).not.toHaveBeenCalled()
    expect(systemBlocks(result)).toEqual([])
  })
})
