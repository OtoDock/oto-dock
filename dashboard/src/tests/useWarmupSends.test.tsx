import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { useWarmupSends, type HeldSend } from '@/pages/agent/chat/useWarmupSends'

// ─── Sends typed while a chat gets ready: every one kept, in order ──────────

function socket(connected = true) {
  return {
    connected,
    queueMessage: vi.fn((_t: string, _c: string, _i?: unknown, _f?: unknown, queueId?: string) =>
      queueId ?? 'minted'),
  }
}

function setup(chatId: string | null, ws = socket()) {
  const handedBack: HeldSend[][] = []
  const hook = renderHook(
    ({ id }: { id: string | null }) => useWarmupSends(ws, id, (held) => handedBack.push(held)),
    { initialProps: { id: chatId } },
  )
  return { hook, ws, handedBack }
}

describe('useWarmupSends', () => {
  it('queues a send at once when the chat has its id', () => {
    const { hook, ws } = setup('c1')
    act(() => hook.result.current.send('second'))
    expect(ws.queueMessage).toHaveBeenCalledWith('second', 'c1', undefined, undefined)
    expect(hook.result.current.chips).toEqual([])
  })

  it('holds sends until the id arrives, then queues them in order under their chip ids', () => {
    const { hook, ws } = setup(null)
    act(() => hook.result.current.send('one'))
    act(() => hook.result.current.send('two', undefined, [{ path: 'a.txt', name: 'a.txt' }]))
    const chips = hook.result.current.chips
    expect(chips.map((c) => c.text)).toEqual(['one', 'two'])
    expect(ws.queueMessage).not.toHaveBeenCalled()
    hook.rerender({ id: 'c1' })
    expect(ws.queueMessage.mock.calls.map((c) => [c[0], c[1], c[4]])).toEqual([
      ['one', 'c1', chips[0].queueId],
      ['two', 'c1', chips[1].queueId],
    ])
    expect(ws.queueMessage.mock.calls[1][3]).toEqual([{ path: 'a.txt', name: 'a.txt' }])
    expect(hook.result.current.chips).toEqual([])
  })

  it('keeps a later send behind the held ones', () => {
    const ws = socket(false)
    const { hook } = setup('c1', ws)
    act(() => hook.result.current.send('one'))
    expect(hook.result.current.chips.map((c) => c.text)).toEqual(['one'])
    // The socket is back: a send made now goes behind the held one, never
    // ahead of it, and both go in order.
    ws.connected = true
    act(() => hook.result.current.send('two'))
    expect(ws.queueMessage.mock.calls.map((c) => c[0])).toEqual(['one', 'two'])
    expect(hook.result.current.chips).toEqual([])
  })

  it('hands the held sends back when the warmup fails or is stopped', () => {
    const { hook, ws, handedBack } = setup(null)
    act(() => hook.result.current.send('one'))
    act(() => hook.result.current.send('two'))
    act(() => hook.result.current.handBack())
    expect(handedBack.map((h) => h.map((s) => s.text))).toEqual([['one', 'two']])
    expect(hook.result.current.chips).toEqual([])
    hook.rerender({ id: 'c1' })
    expect(ws.queueMessage).not.toHaveBeenCalled()
  })

  it('drops the held sends when the page leaves the chat', () => {
    const { hook, ws, handedBack } = setup(null)
    act(() => hook.result.current.send('one'))
    act(() => hook.result.current.discard())
    hook.rerender({ id: 'c1' })
    expect(ws.queueMessage).not.toHaveBeenCalled()
    expect(handedBack).toEqual([])
  })
})
