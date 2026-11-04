// Edit (ArrowUp in an empty composer) takes back the person's own queued
// messages through one cancel-all: the proxy returns their own alone, so a
// teammate's chip and a card's answer stay queued (input_queue.cancel_own).
// No chip is cancelled one by one.
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import DialogsHost from '@/components/ui/DialogsHost'
import { useInteractiveHandlers } from '@/pages/agent/chat/useInteractiveHandlers'
import type { QueuedMessage } from '@/store/types'

function setup(queuedMessages: QueuedMessage[]) {
  const cancelQueued = vi.fn()
  const cancelAllQueued = vi.fn()
  const interactive = {
    toggle: vi.fn(), performSwitch: vi.fn(), setSessionInteractive: vi.fn(), showRichView: false, setShowRichView: vi.fn(),
  } as unknown as Parameters<typeof useInteractiveHandlers>[0]['interactive']
  const wrapper = ({ children }: { children: ReactNode }) => <>{children}<DialogsHost /></>
  const hook = renderHook(
    () => useInteractiveHandlers({
      agentName: 'dev', sessionId: 's1', chatId: 'c1', interactive, viewedStreaming: true,
      ws: { streaming: true, resumeChat: vi.fn(), cancelQueued, cancelAllQueued } as unknown as Parameters<typeof useInteractiveHandlers>[0]['ws'],
      seedDbHistory: vi.fn(), draftKey: 'k', queuedMessages, setEditText: vi.fn(),
    }),
    { wrapper },
  )
  return { hook, cancelQueued, cancelAllQueued }
}

const mine: QueuedMessage = { text: 'mine', queueId: 'q-mine', authorSub: 'me' }
const teammate: QueuedMessage = { text: 'theirs', queueId: 'q-mate', authorSub: 'mate' }
const answer: QueuedMessage = { text: 'Yes.', queueId: 'q-card', authorSub: 'me', card: { card_id: 'card-1', button: 'yes' } }

describe('Edit with a teammate and a card answer waiting', () => {
  it('sends one cancel-all and never cancels a chip by its id', () => {
    const { hook, cancelQueued, cancelAllQueued } = setup([mine, teammate, answer])
    hook.result.current.handleEditQueued()
    expect(cancelAllQueued).toHaveBeenCalledTimes(1)
    expect(cancelAllQueued).toHaveBeenCalledWith('c1')
    expect(cancelQueued).not.toHaveBeenCalled()
  })

  it('sends one cancel-all with a teammate and a card answer alone, the proxy keeping both', () => {
    const { hook, cancelQueued, cancelAllQueued } = setup([teammate, answer])
    hook.result.current.handleEditQueued()
    expect(cancelAllQueued).toHaveBeenCalledTimes(1)
    expect(cancelQueued).not.toHaveBeenCalled()
  })

  it('does nothing when only card answers wait', () => {
    const { hook, cancelQueued, cancelAllQueued } = setup([answer])
    hook.result.current.handleEditQueued()
    expect(cancelAllQueued).not.toHaveBeenCalled()
    expect(cancelQueued).not.toHaveBeenCalled()
  })
})
