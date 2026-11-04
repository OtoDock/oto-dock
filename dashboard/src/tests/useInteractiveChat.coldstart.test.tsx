import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { useInteractiveChat } from '@/hooks/useInteractiveChat'

// ─── Interactive cold-start routing: the prompt, its photos and files with
// it, must ride the WARMUP (server-owned delivery — survives a chat switch /
// reload mid-warmup, and the backend saves the photos with the message's
// row), never a client stash.

function makeWs() {
  return {
    changeExecutionMode: vi.fn(),
    switchExecutionMode: vi.fn(),
    sendPtyInput: vi.fn(),
    sendPtyAttachments: vi.fn(),
    warmup: vi.fn(),
  }
}

const CTX = {
  chatId: 'chat-1',
  sessionId: null,
  warmingUp: false,
  warmupParams: { agentName: 'dev', chatId: 'chat-1', mode: 'ask', model: 'm', layer: 'claude-code-cli' },
}

describe('useInteractiveChat cold start', () => {
  let ws: ReturnType<typeof makeWs>
  beforeEach(() => { ws = makeWs() })

  it('text-only Claude cold send rides the warmup RAW, no client stash', () => {
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    const onColdStart = vi.fn()

    let routed: string | null = null
    act(() => { routed = result.current.routeSend('hello there', { ...CTX, onColdStart }) })

    expect(routed).toBe('cold')
    expect(onColdStart).toHaveBeenCalledOnce()
    expect(ws.warmup).toHaveBeenCalledOnce()
    const [, , , , , prompt, execMode] = ws.warmup.mock.calls[0]
    // RAW text (the backend stamps at delivery; a pre-stamped prompt would be
    // double-stamped by the declined-to-headless kick's own time injection).
    expect(prompt).toEqual({ text: 'hello there' })
    expect(execMode).toBe('interactive')
  })

  it('warmup_ready{interactive} after a text-only cold send flushes nothing', () => {
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    act(() => { result.current.routeSend('hello there', { ...CTX, onColdStart: vi.fn() }) })

    let status = ''
    act(() => {
      status = result.current.onWarmupReady({ interactive: true, chat_id: 'chat-1' })
    })

    expect(status).toBe('interactive')
    expect(ws.sendPtyInput).not.toHaveBeenCalled()
    expect(ws.sendPtyAttachments).not.toHaveBeenCalled()
  })

  it('a cold send WITH attachments rides the warmup too, and nothing is typed on ready', () => {
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    const images = [{ base64: 'aGk=', name: 'photo.png' }, { path: 'users/u/workspace/uploads/photos/a.jpg', name: 'a.jpg' }]
    const files = [{ path: 'users/u/workspace/uploads/files/plan.pdf', name: 'plan.pdf' }]

    act(() => { result.current.routeSend('see photo', { ...CTX, onColdStart: vi.fn(), images, files }) })

    const [, , , , , prompt, execMode] = ws.warmup.mock.calls[0]
    expect(prompt).toEqual({ text: 'see photo', images, files })
    expect(execMode).toBe('interactive')

    act(() => { result.current.onWarmupReady({ interactive: true, chat_id: 'chat-1' }) })
    expect(ws.sendPtyAttachments).not.toHaveBeenCalled()
    expect(ws.sendPtyInput).not.toHaveBeenCalled()
  })

  it('a cold send of photos alone rides the warmup', () => {
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    const images = [{ base64: 'aGk=', name: 'photo.png' }]
    act(() => { result.current.routeSend('', { ...CTX, onColdStart: vi.fn(), images }) })
    const [, , , , , prompt] = ws.warmup.mock.calls[0]
    expect(prompt).toEqual({ text: '', images })
  })

  it('decline after a text-only cold send does NOT replay client-side (server kicks it)', () => {
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    act(() => { result.current.routeSend('hello there', { ...CTX, onColdStart: vi.fn() }) })

    let status = ''
    act(() => {
      status = result.current.onWarmupReady({ interactive: false, chat_id: 'chat-1' })
    })

    // 'none': the caller's server-kick adoption handles the turn the backend
    // already runs from the warmup's prompt.
    expect(status).toBe('none')
    expect(ws.sendPtyAttachments).not.toHaveBeenCalled()
  })
})
