import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { useInteractiveChat } from '@/hooks/useInteractiveChat'

// ─── Interactive cold-start routing: the text must ride the WARMUP (server-
// owned delivery — survives a chat switch / reload mid-warmup), never the
// volatile client stash that dies with resetSession. Attachments keep the
// stash+flush path (their photos ride a separate pty_attachments message).

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
  warmupParams: { agentName: 'dev', chatId: 'chat-1', mode: 'default', model: 'm', layer: 'claude-code-cli' },
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
    // No stash — a later warmup_ready must not paste a duplicate into the PTY.
    expect(result.current.pendingPtyTextRef.current).toBeNull()
  })

  it('warmup_ready{interactive} after a text-only cold send flushes nothing', () => {
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    act(() => { result.current.routeSend('hello there', { ...CTX, onColdStart: vi.fn() }) })

    let status = ''
    act(() => {
      status = result.current.onWarmupReady(
        { interactive: true, chat_id: 'chat-1' },
        { onDecline: vi.fn() },
      )
    })

    expect(status).toBe('interactive')
    expect(ws.sendPtyInput).not.toHaveBeenCalled()
    expect(ws.sendPtyAttachments).not.toHaveBeenCalled()
  })

  it('cold send WITH attachments keeps the stash + flush path', () => {
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    const images = [{ base64: 'aGk=', name: 'photo.png' }]

    act(() => { result.current.routeSend('see photo', { ...CTX, onColdStart: vi.fn(), images }) })

    // The warmup carries NO text (delivery happens via the flush below).
    const [, , , , , prompt] = ws.warmup.mock.calls[0]
    expect(prompt).toBeUndefined()
    expect(result.current.pendingPtyTextRef.current).toBe('see photo')

    act(() => {
      result.current.onWarmupReady(
        { interactive: true, chat_id: 'chat-1' },
        { onDecline: vi.fn() },
      )
    })
    expect(ws.sendPtyAttachments).toHaveBeenCalledOnce()
    const [cid, typed] = ws.sendPtyAttachments.mock.calls[0]
    expect(cid).toBe('chat-1')
    expect(typed).toContain('see photo')  // stamped at delivery
    expect(result.current.pendingPtyTextRef.current).toBeNull()
  })

  it('decline after a text-only cold send does NOT replay client-side (server kicks it)', () => {
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    act(() => { result.current.routeSend('hello there', { ...CTX, onColdStart: vi.fn() }) })

    const onDecline = vi.fn()
    let status = ''
    act(() => {
      status = result.current.onWarmupReady(
        { interactive: false, chat_id: 'chat-1' },
        { onDecline },
      )
    })

    // No stash → 'none': the caller's server-kick adoption handles the turn
    // the backend already runs from the warmup text.
    expect(status).toBe('none')
    expect(onDecline).not.toHaveBeenCalled()
  })
})
