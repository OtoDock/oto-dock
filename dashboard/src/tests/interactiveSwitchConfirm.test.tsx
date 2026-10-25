// The interactive toggle while a turn runs asks first; what it does after the
// answer follows the chat as it is THEN: the session may have ended (the
// no-session path sets the chat's mode for the next send) or the page may be
// on another chat (nothing), else the live switch with the live turn state.
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import DialogsHost from '@/components/ui/DialogsHost'
import { useInteractiveHandlers } from '@/pages/agent/chat/useInteractiveHandlers'
import { answerDialog, findAlertDialog } from './dialogsHarness'

type Props = { sessionId: string | null; chatId: string | null; streaming: boolean }

function setup(initial: Props) {
  const toggle = vi.fn()
  const performSwitch = vi.fn()
  const interactive = {
    toggle, performSwitch, setSessionInteractive: vi.fn(), showRichView: false, setShowRichView: vi.fn(),
  } as unknown as Parameters<typeof useInteractiveHandlers>[0]['interactive']
  const wrapper = ({ children }: { children: ReactNode }) => <>{children}<DialogsHost /></>
  const hook = renderHook(
    (p: Props) => useInteractiveHandlers({
      agentName: 'dev', sessionId: p.sessionId, chatId: p.chatId, interactive,
      viewedStreaming: p.streaming,
      ws: { streaming: p.streaming, resumeChat: vi.fn(), cancelQueued: vi.fn() } as unknown as Parameters<typeof useInteractiveHandlers>[0]['ws'],
      seedDbHistory: vi.fn(), draftKey: 'k', queuedMessages: [], setEditText: vi.fn(),
    }),
    { initialProps: initial, wrapper },
  )
  return { hook, toggle, performSwitch }
}

describe('the interactive switch after its confirm', () => {
  it('sets the mode for the next send when the session ended while the dialog was open', async () => {
    const { hook, toggle, performSwitch } = setup({ sessionId: 's1', chatId: 'c1', streaming: true })
    let done: Promise<void> | undefined
    act(() => { done = hook.result.current.handleInteractiveToggle(true) })
    await findAlertDialog('Switch anyway?')
    hook.rerender({ sessionId: null, chatId: 'c1', streaming: false })
    await act(async () => { await answerDialog('Switch') })
    await act(async () => { await done })
    expect(toggle).toHaveBeenCalledWith(true, 'c1')
    expect(performSwitch).not.toHaveBeenCalled()
  })

  it('switches with the live turn state, and does nothing on another chat', async () => {
    const { hook, toggle, performSwitch } = setup({ sessionId: 's1', chatId: 'c1', streaming: true })
    let done: Promise<void> | undefined
    act(() => { done = hook.result.current.handleInteractiveToggle(true) })
    await findAlertDialog('Switch anyway?')
    // The turn ended while the dialog was open: no deferred switch.
    hook.rerender({ sessionId: 's1', chatId: 'c1', streaming: false })
    await act(async () => { await answerDialog('Switch') })
    await act(async () => { await done })
    expect(performSwitch).toHaveBeenCalledWith(true, 'c1', false)

    performSwitch.mockClear()
    hook.rerender({ sessionId: 's1', chatId: 'c1', streaming: true })
    act(() => { done = hook.result.current.handleInteractiveToggle(false) })
    await findAlertDialog('Switch anyway?')
    hook.rerender({ sessionId: 's2', chatId: 'c2', streaming: false })
    await act(async () => { await answerDialog('Switch') })
    await act(async () => { await done })
    await waitFor(() => expect(performSwitch).not.toHaveBeenCalled())
    expect(toggle).not.toHaveBeenCalled()
  })
})
