/**
 * A `steered` frame (mid-turn steer accepted by the engine) must render the
 * user bubble at the position the engine actually CONSUMES it — the next
 * sampling-round boundary — not the accept moment: while an assistant text
 * block is still streaming, the split is DEFERRED to the next block
 * (tool/thinking/subagent) or turn end, so sentences never get cut in half.
 * When no message is open it renders immediately. It must NOT touch the
 * queue chips (an accepted steer never entered the queue) nor reset the
 * turn timer (the same turn keeps streaming).
 *
 * Post-abort stragglers: a graceful abort keeps the engine draining for a
 * beat — chunks arriving after finalizeAbortedTurn must NOT reopen a fresh
 * assistant header; the guard disarms only at the terminal aborted/done
 * frame.
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
  subscribe: vi.fn(() => () => {}),  // generic frame registry (ui artifacts)
}))

const captured = vi.hoisted(() => ({ cb: null as any }))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

const addQueued = vi.fn()

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued, clearQueued: vi.fn() },
    }),
  )
}

describe('steered frame', () => {
  beforeEach(() => {
    addQueued.mockClear()
  })

  it('appends the user bubble and a fresh assistant continuation', () => {
    const { result } = renderStream()

    act(() => captured.cb.onSteered({ text: 'also check the logs' }))

    const msgs = result.current.messages
    expect(msgs.length).toBeGreaterThanOrEqual(2)
    const user = msgs[msgs.length - 2]
    const cont = msgs[msgs.length - 1]
    expect(user.role).toBe('user')
    expect(user.blocks).toEqual([{ type: 'text', content: 'also check the logs' }])
    expect(cont.role).toBe('assistant')
    expect(cont.blocks).toEqual([])          // continuation streams below
    expect(addQueued).not.toHaveBeenCalled() // never a queue chip
  })

  it('keeps queue chips for the fallback path (queued frame)', () => {
    const { result } = renderStream()

    act(() => captured.cb.onQueued({ index: 0, text: 'after the turn' }))

    expect(addQueued).toHaveBeenCalledWith(0, { text: 'after the turn' })
    // A queued message renders no bubble — it stays a chip until queue_sent.
    const texts = result.current.messages.flatMap((m: any) => m.blocks)
    expect(texts).not.toContainEqual({ type: 'text', content: 'after the turn' })
  })

  it('defers the split to the next tool block while text is streaming', () => {
    const { result } = renderStream()

    act(() => captured.cb.onText('checking the camera and I'))
    act(() => captured.cb.onSteered({ text: 'also check the alarm' }))

    // Mid-text: no bubble yet, and further deltas continue the SAME message.
    expect(result.current.messages.filter((m: any) => m.role === 'user')).toHaveLength(0)
    act(() => captured.cb.onText(' will report back.'))
    expect(result.current.messages).toHaveLength(1)
    expect((result.current.messages[0].blocks[0] as any).content)
      .toBe('checking the camera and I will report back.')

    // The next block boundary renders the bubble + a fresh continuation,
    // and the tool block lands in the continuation.
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't1' }))
    const msgs = result.current.messages
    expect(msgs.map((m: any) => m.role)).toEqual(['assistant', 'user', 'assistant'])
    expect(msgs[1].blocks).toEqual([{ type: 'text', content: 'also check the alarm' }])
    expect(msgs[2].blocks[0]).toMatchObject({ type: 'tool', name: 'Bash' })
  })

  it('flushes a held steer at turn end without an empty continuation', () => {
    const { result } = renderStream()

    act(() => captured.cb.onText('final words'))
    act(() => captured.cb.onSteered({ text: 'one more thing' }))
    act(() => captured.cb.onDone())

    const msgs = result.current.messages
    expect(msgs[msgs.length - 1].role).toBe('user')
    expect(msgs[msgs.length - 1].blocks)
      .toEqual([{ type: 'text', content: 'one more thing' }])
  })

  it('shows a held steer as pending until the boundary renders it', () => {
    // A steer accepted while a tool call runs sat invisible for the whole
    // call; it is now listed as pending (display only — it already sits in
    // the engine) and leaves the list the moment the bubble renders.
    const { result } = renderStream()

    act(() => captured.cb.onText('running the long command'))
    act(() => captured.cb.onSteered({ text: 'and count the rows', files: [{ path: 'users/u/workspace/uploads/files/a.csv', name: 'a.csv' }] }))
    expect(result.current.pendingSteers).toEqual([
      { text: 'and count the rows', files: [{ path: 'users/u/workspace/uploads/files/a.csv', name: 'a.csv' }] },
    ])
    expect(result.current.messages.filter((m: any) => m.role === 'user')).toHaveLength(0)

    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't1' }))
    expect(result.current.pendingSteers).toEqual([])
    const user = result.current.messages.find((m: any) => m.role === 'user')!
    // The bubble carries the chips the frame named, then the text.
    expect(user.blocks).toEqual([
      { type: 'file_attachments', files: [{ name: 'a.csv', path: 'users/u/workspace/uploads/files/a.csv' }] },
      { type: 'text', content: 'and count the rows' },
    ])
  })

  it('a held steer stays with its chat when the view moves to a new chat', () => {
    const { result } = renderStream()

    act(() => captured.cb.onText('working on A'))
    act(() => captured.cb.onSteered({ text: 'steer meant for A' }))
    expect(result.current.pendingSteers).toHaveLength(1)

    act(() => { result.current.setChatId(null) })
    expect(result.current.pendingSteers).toEqual([])

    act(() => captured.cb.onText('first turn of the new chat'))
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't1' }))
    const userTexts = result.current.messages
      .filter((m: any) => m.role === 'user')
      .flatMap((m: any) => m.blocks.map((b: any) => b.content))
    expect(userTexts).not.toContain('steer meant for A')
  })

  it('an error ending the turn renders the held steer instead of leaving it pending', () => {
    const { result } = renderStream()

    act(() => captured.cb.onText('halfway'))
    act(() => captured.cb.onSteered({ text: 'and the logs' }))
    act(() => captured.cb.onError('engine crashed'))

    expect(result.current.pendingSteers).toEqual([])
    const msgs = result.current.messages
    expect(msgs[msgs.length - 1].role).toBe('user')
    expect(msgs[msgs.length - 1].blocks).toEqual([{ type: 'text', content: 'and the logs' }])
  })

  it('a delegated steer wears the delegating agent badge', () => {
    const { result } = renderStream()
    act(() => captured.cb.onSteered({
      text: 'also X',
      event_data: { agent_slug: 'ceo', agent_display_name: 'CEO', agent_color: '#123', badge: 'delegated by' },
    }))
    const user = result.current.messages.find((m: any) => m.role === 'user')!
    expect(user).toMatchObject({ agentSlug: 'ceo', agentDisplayName: 'CEO', badge: 'delegated by' })
  })
})

describe('queued attachments', () => {
  it('the queue chip and the drained bubble carry the attachment meta', () => {
    const { result } = renderStream()
    const images = [{ name: 'dot.png', path: 'users/u/workspace/uploads/photos/img_1.png' }]

    act(() => captured.cb.onQueued({ index: 0, text: 'look', images }))
    expect(addQueued).toHaveBeenCalledWith(0, { text: 'look', images })

    act(() => captured.cb.onQueueSent({ text: 'look', images }))
    const user = result.current.messages.find((m: any) => m.role === 'user')!
    expect(user.blocks).toEqual([
      { type: 'image_attachments', images: ['dot.png'], paths: ['users/u/workspace/uploads/photos/img_1.png'] },
      { type: 'text', content: 'look' },
    ])
  })

  it('a cancelled queued message hands its attachments back to the composer', () => {
    const restoreAttachments = vi.fn()
    const { result } = renderHook(() =>
      useChatStream({
        agents: [],
        initialChatId: 'chat-1',
        queue: { addQueued, clearQueued: vi.fn(), restoreAttachments },
      }),
    )
    const files = [{ path: 'users/u/workspace/uploads/files/a.csv', name: 'a.csv' }]
    act(() => captured.cb.onQueueEditReturn({ index: 0, text: 'later', files }))
    expect(result.current.editText).toBe('later')
    expect(restoreAttachments).toHaveBeenCalledWith([], files)
  })

  it('a returned message goes after the draft the person is typing', () => {
    useChatStore.getState().setDraftInput('chat-1', 'half typed')
    const { result } = renderHook(() =>
      useChatStream({
        agents: [],
        initialChatId: 'chat-1',
        queue: { addQueued, clearQueued: vi.fn(), restoreAttachments: vi.fn() },
      }),
    )
    act(() => captured.cb.onQueueEditReturn({ index: 0, text: 'came back' }))
    expect(result.current.editText).toBe('half typed\n\ncame back')
    useChatStore.getState().setDraftInput('chat-1', '')
  })

  it('an attachment-only message comes back without touching the draft', () => {
    const restoreAttachments = vi.fn()
    const { result } = renderHook(() =>
      useChatStream({
        agents: [],
        initialChatId: 'chat-1',
        queue: { addQueued, clearQueued: vi.fn(), restoreAttachments },
      }),
    )
    const images = [{ name: 'dot.png', path: 'users/u/workspace/uploads/photos/img_1.png' }]
    act(() => captured.cb.onQueueEditReturn({ index: 0, text: '', images }))
    expect(result.current.editText).toBeNull()
    expect(restoreAttachments).toHaveBeenCalledWith(images, [])
  })
})

describe('mid-turn queue chips (claude fallback)', () => {
  it('shows the chip for a DIFFERENT message while the bubble marker is armed', () => {
    const { result } = renderStream()

    // A turn-opening send added its own bubble → marker holds ITS text.
    act(() => { result.current.sentWithBubbleRef.current = 'first prompt' })

    // A mid-turn send got queued (claude has no steer) → its chip MUST show.
    act(() => captured.cb.onQueued({ index: 0, text: 'second prompt' }))
    expect(addQueued).toHaveBeenCalledWith(0, { text: 'second prompt' })

    // The reconnect/stale-pump dedup still holds for the SAME text.
    addQueued.mockClear()
    act(() => captured.cb.onQueued({ index: 0, text: 'first prompt' }))
    expect(addQueued).not.toHaveBeenCalled()
  })
})

describe('post-abort stragglers', () => {
  it('drops chunks between abort click and the terminal frame', () => {
    const { result } = renderStream()

    act(() => captured.cb.onText('Yes. The alarm is currently active'))
    // The page's handleAbort: arm the guard, seal the turn proactively.
    act(() => {
      result.current.abortedRef.current = true
      result.current.finalizeAbortedTurn()
    })
    expect(result.current.messages).toHaveLength(1)

    // Graceful-abort stragglers: no new header, no text, no blocks.
    act(() => captured.cb.onText(' at'))
    act(() => captured.cb.onThinking({ phase: 'start' }))
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 'x1' }))
    expect(result.current.messages).toHaveLength(1)
    expect(result.current.messages[0].blocks)
      .toEqual([{ type: 'text', content: 'Yes. The alarm is currently active' }])

    // The terminal frame disarms the guard — the NEXT turn streams normally.
    act(() => captured.cb.onAborted({}))
    act(() => captured.cb.onText('fresh turn'))
    expect(result.current.messages).toHaveLength(2)
    expect(result.current.messages[1].blocks)
      .toEqual([{ type: 'text', content: 'fresh turn' }])
  })
})

describe('steered frame naming its row', () => {
  it('splits the streaming answer at once, the bubble keyed by its row', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Working on the first part'))
    act(() => captured.cb.onSteered({ text: 'switch to the second', message_id: 42, queue_id: 'q' }))
    act(() => captured.cb.onText('On it.'))
    const msgs = result.current.messages
    expect(msgs.map((m: any) => m.role)).toEqual(['assistant', 'user', 'assistant'])
    expect(msgs[0].blocks).toEqual([{ type: 'text', content: 'Working on the first part' }])
    expect(msgs[1].id).toBe('db-42')
    expect(msgs[2].blocks).toEqual([{ type: 'text', content: 'On it.' }])
    expect(result.current.pendingSteers).toEqual([])
  })

  it('a frame with no row (a 1.7.0 proxy) still waits for the block boundary', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Working'))
    act(() => captured.cb.onSteered({ text: 'later' }))
    expect(result.current.messages.map((m: any) => m.role)).toEqual(['assistant'])
    expect(result.current.pendingSteers).toEqual([{ text: 'later' }])
  })
})

describe('the queue frames that carry their ids', () => {
  it('a send the proxy queued instead of starting turns its bubble into the chip', () => {
    const { result } = renderStream()
    act(() => {
      result.current.sentWithBubbleRef.current = 'meanwhile'
      result.current.setMessages([
        { id: 'user-1', role: 'user', blocks: [{ type: 'text', content: 'meanwhile' }], createdAt: '' },
        { id: 'stream-1', role: 'assistant', blocks: [], createdAt: '' },
      ])
    })
    act(() => captured.cb.onQueued({ index: 0, queue_id: 'q', text: 'meanwhile', author_sub: 'u' }))
    expect(result.current.messages).toEqual([])
    expect(addQueued).not.toHaveBeenCalled()  // the socket hook filed the chip
  })

  it('a fresh send that took waiting messages shows them first', () => {
    const { result } = renderStream()
    act(() => {
      result.current.sentWithBubbleRef.current = 'fresh'
      result.current.setMessages([
        { id: 'user-1', role: 'user', blocks: [{ type: 'text', content: 'fresh' }], createdAt: '' },
        { id: 'stream-1', role: 'assistant', blocks: [], createdAt: '' },
      ])
    })
    act(() => captured.cb.onQueueSent({ queue_ids: ['q'], message_ids: [5], text: 'waiting' }))
    const texts = result.current.messages.map((m: any) => m.blocks[0]?.content ?? '')
    expect(texts).toEqual(['waiting', 'fresh', ''])
  })
})
