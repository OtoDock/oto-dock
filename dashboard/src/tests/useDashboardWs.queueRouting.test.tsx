/**
 * The chat-routed queue frames: every socket of the chat's audience gets
 * them, so a chip lands in its chat's slice whichever chat this view shows,
 * the page's callbacks and the socket's own turn state move only for the
 * viewed chat, a clear removes only its listed chips, and the author's
 * returned copy goes back to the composer of the chat it was typed into.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs, mintQueueId } from '@/hooks/useDashboardWs'
import { useChatStore } from '@/store/chatStore'
import { FakeWebSocket } from './fakeWebSocket'

function openSocket(viewedChatId: string, extra: Record<string, unknown> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const cb = { onQueued: vi.fn(), onQueueSent: vi.fn(), onQueueEditReturn: vi.fn(), ...extra }
  const hook = renderHook(() => useDashboardWs({ viewedChatId, ...cb }), { wrapper })
  act(() => { hook.result.current.connect() })
  const ws = FakeWebSocket.instances[0]
  act(() => { ws.onopen?.() })
  return { hook, ws, cb }
}

function frame(ws: FakeWebSocket, obj: Record<string, unknown>) {
  act(() => { ws.onmessage?.({ data: JSON.stringify(obj) }) })
}

const chips = (cid: string) => useChatStore.getState().byChat[cid]?.queuedMessages ?? []

describe('the chat-routed queue frames', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    useChatStore.setState({ byChat: {} })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('files a chip for another chat in that chat and runs no callback', () => {
    const { ws, cb } = openSocket('chat-1')
    frame(ws, { type: 'queued', index: 0, queue_id: 'q', text: 'there', author_sub: 'u', chat_id: 'chat-2' })
    expect(chips('chat-2').map((m) => m.queueId)).toEqual(['q'])
    expect(chips('chat-1')).toEqual([])
    expect(cb.onQueued).not.toHaveBeenCalled()
    frame(ws, { type: 'queued', index: 0, queue_id: 'q1', text: 'here', author_sub: 'u', chat_id: 'chat-1' })
    expect(cb.onQueued).toHaveBeenCalledTimes(1)
  })

  it('a task chat viewed through a sibling run files the chip under the viewed chat', () => {
    const { ws } = openSocket('task-a')
    frame(ws, { type: 'queued', index: 0, queue_id: 'q', text: 'x', author_sub: 'u',
                chat_id: 'task-b', view_chat_id: 'task-a' })
    expect(chips('task-a').map((m) => m.queueId)).toEqual(['q'])
  })

  it('queue_sent for another chat clears its chips and leaves this socket idle', () => {
    const { hook, ws, cb } = openSocket('chat-1')
    frame(ws, { type: 'queued', index: 0, queue_id: 'q', text: 'x', author_sub: 'u', chat_id: 'chat-2' })
    frame(ws, { type: 'queue_sent', queue_ids: ['q'], message_ids: [7], text: 'x', chat_id: 'chat-2' })
    expect(chips('chat-2')).toEqual([])
    expect(hook.result.current.streaming).toBe(false)
    expect(useChatStore.getState().byChat['chat-2'].status).toBe('streaming')
    expect(cb.onQueueSent).not.toHaveBeenCalled()
  })

  it('a queue_sent that names no queued message leaves the chips', () => {
    const { ws } = openSocket('chat-1')
    frame(ws, { type: 'queued', index: 0, queue_id: 'q', text: 'x', author_sub: 'u', chat_id: 'chat-1' })
    frame(ws, { type: 'queue_sent', queue_ids: [], message_ids: [9], text: 'nudge', chat_id: 'chat-1' })
    expect(chips('chat-1').map((m) => m.queueId)).toEqual(['q'])
  })

  it('a clear removes only its listed chips', () => {
    const { ws } = openSocket('chat-1')
    for (const id of ['mine', 'theirs']) {
      frame(ws, { type: 'queued', index: 0, queue_id: id, text: id, author_sub: 'u', chat_id: 'chat-1' })
    }
    frame(ws, { type: 'queue_cleared', queue_ids: ['mine'], reason: 'cancel', chat_id: 'chat-1' })
    expect(chips('chat-1').map((m) => m.queueId)).toEqual(['theirs'])
  })

  it('a returned message goes back to the composer of the chat it was typed into', () => {
    const { ws, cb } = openSocket('chat-1')
    frame(ws, { type: 'queue_cleared', queue_ids: ['q'], reason: 'turn_failed', returned: true,
                text: 'try again', files: [{ path: 'u/f', name: 'f' }], chat_id: 'chat-2' })
    expect(useChatStore.getState().byChat['chat-2'].draftInput).toBe('try again')
    expect(useChatStore.getState().byChat['chat-2'].pendingFiles.map((f) => f.uploadedPath)).toEqual(['u/f'])
    expect(cb.onQueueEditReturn).not.toHaveBeenCalled()
    frame(ws, { type: 'queue_removed', queue_id: 'q2', index: 0, returned: true, text: 'mine',
                chat_id: 'chat-1' })
    expect(cb.onQueueEditReturn).toHaveBeenCalledWith(expect.objectContaining({ text: 'mine' }))
  })

  it('a teammate copy without returned restores nothing', () => {
    const { ws, cb } = openSocket('chat-1')
    frame(ws, { type: 'queued', index: 0, queue_id: 'q', text: 'theirs', author_sub: 'x', chat_id: 'chat-1' })
    frame(ws, { type: 'queue_removed', queue_id: 'q', index: 0, chat_id: 'chat-1' })
    expect(chips('chat-1')).toEqual([])
    expect(cb.onQueueEditReturn).not.toHaveBeenCalled()
  })

  it('a send carries a minted queue id and a cancel names it with its index', () => {
    const { hook, ws } = openSocket('chat-1')
    act(() => { hook.result.current.sendMessage('hi', 'chat-1') })
    const sent = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(sent.queue_id).toMatch(/^[0-9a-f]{32}$/)
    act(() => { hook.result.current.cancelQueued({ queueId: 'q', index: 2 }, 'chat-1') })
    expect(JSON.parse(ws.sent[ws.sent.length - 1])).toEqual(
      { type: 'cancel_queued', index: 2, queue_id: 'q', chat_id: 'chat-1' })
    expect(mintQueueId()).not.toBe(mintQueueId())
  })

  it('a send typed while the chat gets ready is queue-only and keeps its chip id', () => {
    const { hook, ws } = openSocket('chat-1')
    act(() => { hook.result.current.queueMessage('later', 'chat-1', undefined, undefined, 'q-held') })
    expect(JSON.parse(ws.sent[ws.sent.length - 1])).toEqual(
      { type: 'chat', text: 'later', chat_id: 'chat-1', queue_id: 'q-held', queue_only: true })
    act(() => { hook.result.current.sendMessage('now', 'chat-1') })
    expect(JSON.parse(ws.sent[ws.sent.length - 1]).queue_only).toBeUndefined()
  })

  it('a chip of a sibling run remembers the queue chat and the frames that clear it find it', () => {
    const { ws, cb } = openSocket('task-a')
    frame(ws, { type: 'queued', index: 0, queue_id: 'q', text: 'x', author_sub: 'u',
                chat_id: 'task-b', view_chat_id: 'task-a' })
    expect(chips('task-a')).toEqual([{ text: 'x', queueId: 'q', authorSub: 'u', queueChatId: 'task-b' }])
    expect(chips('task-b')).toEqual([])
    // queue_sent names the pump's chat: the chip filed under the view goes.
    frame(ws, { type: 'queue_sent', queue_ids: ['q'], message_ids: [1], text: 'x', chat_id: 'task-b' })
    expect(chips('task-a')).toEqual([])
    // A returned message goes back to the composer of the slice that held
    // the chip, the viewed one here.
    frame(ws, { type: 'queued', index: 0, queue_id: 'q2', text: 'y', author_sub: 'u',
                chat_id: 'task-b', view_chat_id: 'task-a' })
    frame(ws, { type: 'queue_removed', queue_id: 'q2', index: 0, returned: true, text: 'y',
                chat_id: 'task-b' })
    expect(chips('task-a')).toEqual([])
    expect(cb.onQueueEditReturn).toHaveBeenCalledWith(expect.objectContaining({ text: 'y' }))
    expect(useChatStore.getState().byChat['task-b']?.draftInput ?? '').toBe('')
  })

  it("the resume's snapshot of a sibling run files under the view and names the queue chat", () => {
    const { ws } = openSocket('task-a')
    frame(ws, { type: 'queue_snapshot', chat_id: 'task-a', queue_chat_id: 'task-b',
                messages: [{ queue_id: 'q', text: 'x', author_sub: 'u', view_chat_id: 'task-a' }] })
    expect(chips('task-a')).toEqual([{ text: 'x', queueId: 'q', authorSub: 'u', queueChatId: 'task-b' }])
    // The audience's snapshot names the queue's chat and each entry its view.
    frame(ws, { type: 'queue_snapshot', chat_id: 'task-b',
                messages: [{ queue_id: 'q', text: 'x', author_sub: 'u', view_chat_id: 'task-a' },
                           { queue_id: 'q9', text: 'own', author_sub: 'u' }] })
    expect(chips('task-a')).toEqual([{ text: 'x', queueId: 'q', authorSub: 'u', queueChatId: 'task-b' }])
    expect(chips('task-b')).toEqual([{ text: 'own', queueId: 'q9', authorSub: 'u' }])
    // A plain snapshot replaces the slice as before.
    frame(ws, { type: 'queue_snapshot', chat_id: 'task-b', messages: [] })
    expect(chips('task-b')).toEqual([])
  })

  it('a cold warmup with text carries a minted queue id', () => {
    const { hook, ws } = openSocket('chat-1')
    let id: string | undefined
    act(() => { id = hook.result.current.warmup('dev', 'chat-1', 'ask', 'm', undefined, { text: 'first' }) })
    const sent = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(sent.type).toBe('warmup')
    expect(sent.queue_id).toMatch(/^[0-9a-f]{32}$/)
    expect(id).toBe(sent.queue_id)
    act(() => { hook.result.current.warmup('dev', 'chat-1') })
    expect(JSON.parse(ws.sent[ws.sent.length - 1]).queue_id).toBeUndefined()
  })

  it('a refusal naming a send hands what was sent back; the chip or the turn forgets it', () => {
    const onError = vi.fn()
    const { hook, ws, cb } = openSocket('chat-1', { onError })
    let id = ''
    act(() => { id = hook.result.current.queueMessage('later', 'chat-1', undefined, [{ path: 'u/f', name: 'f' }]) })
    frame(ws, { type: 'error', message: 'Too many queued messages.', queue_id: id })
    // The viewed chat's composer gets the send back through the page's
    // hand-back, before the page sees the refusal.
    expect(cb.onQueueEditReturn).toHaveBeenCalledWith(expect.objectContaining(
      { text: 'later', files: [{ path: 'u/f', name: 'f' }] }))
    expect(onError).toHaveBeenCalledWith('Too many queued messages.', undefined,
      { queueId: id, chatId: 'chat-1', returned: true })
    expect(hook.result.current.streaming).toBe(false)
    // Confirmed sends are forgotten: a late refusal returns nothing.
    let id2 = ''
    act(() => { id2 = hook.result.current.queueMessage('again', 'chat-1') })
    frame(ws, { type: 'queued', index: 0, queue_id: id2, text: 'again', author_sub: 'u', chat_id: 'chat-1' })
    frame(ws, { type: 'error', message: 'late', queue_id: id2 })
    expect(onError).toHaveBeenLastCalledWith('late', undefined, { queueId: id2, returned: false })
    // A bare error carries no refusal.
    frame(ws, { type: 'error', message: 'bare' })
    expect(onError).toHaveBeenLastCalledWith('bare', undefined)
  })

  it('a refused send typed into a chat no longer viewed goes to that chat\'s draft', () => {
    const onError = vi.fn()
    const { hook, ws, cb } = openSocket('chat-1', { onError })
    let id = ''
    act(() => {
      id = hook.result.current.queueMessage('elsewhere', 'chat-2',
        [{ name: 'p.png', base64: 'data:image/png;base64,AA==' }])
    })
    // The viewed chat's turn ending forgets only its own sends.
    frame(ws, { type: 'done', chat_id: 'chat-1' })
    frame(ws, { type: 'error', message: 'no photos here', queue_id: id })
    expect(useChatStore.getState().byChat['chat-2'].draftInput).toBe('elsewhere')
    expect(useChatStore.getState().byChat['chat-2'].pendingImages.map((i) => i.base64))
      .toEqual(['data:image/png;base64,AA=='])
    expect(cb.onQueueEditReturn).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledWith('no photos here', undefined,
      { queueId: id, chatId: 'chat-2', returned: true })
  })
})
