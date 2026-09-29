import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { useInteractiveChat } from '@/hooks/useInteractiveChat'

// ─── The interactive toggle and the cold-start prompt. A toggle on a brand-new
//     chat is local-only: the frame used to go out without a chat_id and the
//     server wrote it onto the connection's previously bound chat (found on
//     T1, 2026-09-20). A cold prompt rides the warmup RAW on every engine —
//     the backend stamps it where it decides the delivery (launch argument or
//     PTY submit), so the hook needs no catalog. ──────────────────────────────

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
  chatId: null,
  sessionId: null,
  warmingUp: false,
  warmupParams: { agentName: 'dev', chatId: undefined, mode: 'default', model: 'm', layer: undefined as string | undefined },
}

describe('useInteractiveChat toggle', () => {
  it('a toggle on a brand-new chat sets the intent and sends no frame', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, ''))
    act(() => { result.current.toggle(true, null) })
    expect(result.current.chatExecMode).toBe('interactive')
    expect(result.current.interactiveMode).toBe(true)
    expect(ws.changeExecutionMode).not.toHaveBeenCalled()
  })

  it('a toggle on an existing chat persists the explicit mode for THAT chat', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
    act(() => { result.current.toggle(false, 'chat-9') })
    expect(result.current.chatExecMode).toBe('-p')
    expect(ws.changeExecutionMode).toHaveBeenCalledWith('-p', 'chat-9')
  })
})

describe('useInteractiveChat cold start', () => {
  it('sends the raw prompt on the warmup whatever the layer — the server stamps it', () => {
    for (const layer of ['acme-tui', 'pty-only', undefined]) {
      const ws = makeWs()
      const { result } = renderHook(() => useInteractiveChat(ws, 'interactive'))
      act(() => {
        result.current.routeSend('hello there', {
          ...CTX, warmupParams: { ...CTX.warmupParams, layer }, onColdStart: vi.fn(),
        })
      })
      const [, , , , sentLayer, prompt, execMode] = ws.warmup.mock.calls[0]
      expect(sentLayer).toBe(layer)
      expect(prompt).toEqual({ text: 'hello there' })
      expect(execMode).toBe('interactive')
    }
  })
})
