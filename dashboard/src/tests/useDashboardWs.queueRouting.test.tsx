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

class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static instances: FakeWebSocket[] = []
  readyState = FakeWebSocket.OPEN
  sent: string[] = []
  onopen: (() => void) | null = null
  onclose: ((e?: { code: number; reason: string }) => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  constructor(public url: string) { FakeWebSocket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() { /* the test drives onclose */ }
}

function openSocket(viewedChatId: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const cb = { onQueued: vi.fn(), onQueueSent: vi.fn(), onQueueEditReturn: vi.fn() }
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
})
