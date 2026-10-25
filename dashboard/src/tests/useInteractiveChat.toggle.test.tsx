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
  warmupParams: { agentName: 'dev', chatId: undefined, mode: 'ask', model: 'm', layer: undefined as string | undefined },
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

describe('useInteractiveChat and the server\'s answer to a pick', () => {
  it('a refused switch is echoed back: the stored mode applies and "Switching…" ends', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, ''))
    act(() => { result.current.performSwitch(true, 'pool-chat', false) })
    expect(result.current.switching).toBe(true)
    expect(result.current.interactiveMode).toBe(true)
    act(() => { result.current.applyStoredExecMode('') })
    expect(result.current.switching).toBe(false)
    expect(result.current.interactiveMode).toBe(false)
    act(() => { result.current.applyStoredExecMode('interactive') })
    expect(result.current.chatExecMode).toBe('interactive')
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

describe('useInteractiveChat: a live switch the server refuses or fails', () => {
  it('a refusal before the re-warm started ("Access denied") ends "Switching…" and keeps the mode', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, ''))
    act(() => { result.current.restoreFromMeta('interactive') })
    act(() => { result.current.performSwitch(false, 'c1', false) })
    expect(result.current.switching).toBe(true)
    expect(result.current.chatExecMode).toBe('-p')
    act(() => { result.current.switchErrored('Access denied') })
    expect(result.current.switching).toBe(false)
    expect(result.current.chatExecMode).toBe('interactive')
  })

  it('an error once the switched chat\'s re-warm started keeps the lock; its warmup_failed ends it', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, ''))
    act(() => { result.current.performSwitch(true, 'c1', false) })
    act(() => { result.current.switchStarted('other-chat') })
    act(() => { result.current.switchStarted('c1') })
    act(() => { result.current.switchErrored('Access denied') })
    expect(result.current.switching).toBe(true)
    act(() => { result.current.switchFailed() })
    expect(result.current.switching).toBe(false)
  })

  it('an unrelated error during the switch leaves it running and its mode', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, ''))
    act(() => { result.current.restoreFromMeta('interactive') })
    act(() => { result.current.performSwitch(false, 'c1', false) })
    act(() => { result.current.switchErrored('A spawned task failed') })
    expect(result.current.switching).toBe(true)
    expect(result.current.chatExecMode).toBe('-p')
  })

  it('a deferred switch flushed at turn end ends the same way', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, ''))
    act(() => { result.current.performSwitch(false, 'c2', true) })
    expect(result.current.switching).toBe(false)
    act(() => { result.current.flushDeferredSwitch('c2') })
    expect(result.current.switching).toBe(true)
    act(() => { result.current.switchErrored('Access denied') })
    expect(result.current.switching).toBe(false)
  })

  it('no switch in flight: the tails change nothing', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, ''))
    act(() => { result.current.switchErrored('Access denied'); result.current.switchFailed() })
    expect(result.current.switching).toBe(false)
  })
})

describe('useInteractiveChat on a task chat (no terminal toggle there)', () => {
  it('a stored interactive mode never cold-starts a terminal: the warmup asks for headless', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, 'interactive', { taskChat: true }))
    act(() => { result.current.restoreFromMeta('interactive') })
    expect(result.current.interactiveMode).toBe(false)
    expect(result.current.chatExecMode).toBe('-p')
    let routed: unknown = 'unset'
    act(() => {
      routed = result.current.routeSend('follow up', { ...CTX, chatId: 'task-run-1', onColdStart: vi.fn() })
    })
    expect(routed).toBeNull()
    expect(ws.warmup).not.toHaveBeenCalled()
  })

  it('the run\'s own live terminal stays interactive and takes the sends', () => {
    const ws = makeWs()
    const { result } = renderHook(() => useInteractiveChat(ws, '', { taskChat: true }))
    act(() => { result.current.onWarmupReady({ interactive: true, chat_id: 'task-run-1' }, { onDecline: vi.fn() }) })
    expect(result.current.interactiveMode).toBe(true)
    expect(result.current.chatExecMode).toBe('interactive')
    act(() => {
      result.current.routeSend('steer', { ...CTX, chatId: 'task-run-1', sessionId: 's1', onColdStart: vi.fn() })
    })
    expect(ws.sendPtyInput).toHaveBeenCalledTimes(1)
  })
})
