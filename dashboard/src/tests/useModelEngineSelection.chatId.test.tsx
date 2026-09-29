import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { useModelEngineHandlers, usePermissionModeInvariant } from '@/pages/agent/chat/useModelEngineSelection'

// ─── A model pick NAMES its chat. Without a chat_id the server treats the pick
//     as the new-chat state (deferred for the chat the first warmup mints) and
//     leaves the chat the socket is still bound to alone — the previous chat,
//     whose live session and row a pick made right after "+ New Chat" used to
//     change (found on T1, 2026-09-24). ──────────────────────────────────────

function renderHandlers(chatId: string | null) {
  const ws = { changeModel: vi.fn(), streaming: false } as any
  const setModel = vi.fn()
  const setSelectedLayer = vi.fn()
  const hook = renderHook(() => useModelEngineHandlers({
    ws, parseModelValue: (v: string) => {
      const [layer, model] = v.split('::')
      return { layer, model }
    },
    agentName: 'dev', chatId, chatActiveLayer: null, viewedStreaming: false, warming: false,
    isTaskChat: false, setEngineSwitchBusy: vi.fn(), setEngineSwitchError: vi.fn(),
    setPendingEngineSwitch: vi.fn(), setModel, setSelectedLayer, pendingEngineSwitch: null,
  }))
  return { ws, setModel, hook }
}

describe('useModelEngineHandlers chat_id', () => {
  it('a pick on an open chat names that chat', () => {
    const { ws, setModel, hook } = renderHandlers('chat-9')
    act(() => { hook.result.current.handleModelChange('claude-code-cli::m2') })
    expect(ws.changeModel).toHaveBeenCalledWith('m2', 'chat-9')
    expect(setModel).toHaveBeenCalledWith('m2')
  })

  it('a pick in the new-chat state goes out without a chat (the server defers it)', () => {
    const { ws, hook } = renderHandlers(null)
    act(() => { hook.result.current.handleModelChange('claude-code-cli::m2') })
    expect(ws.changeModel).toHaveBeenCalledWith('m2', null)
  })
})

// ─── The mode invariant corrects a mode the engine does not declare, but a
//     task chat's stored `auto` is the run's fact: flipping it to the first
//     declared mode showed a task run as Default instead of Don't Ask. ───────

describe('usePermissionModeInvariant', () => {
  const declared = ['default', 'acceptEdits', 'plan', 'dontAsk']

  it('keeps a task chat on its stored auto mode', () => {
    const setMode = vi.fn()
    renderHook(() => usePermissionModeInvariant('auto', declared, setMode))
    expect(setMode).not.toHaveBeenCalled()
  })

  it('keeps a check chat on its judge profile', () => {
    const setMode = vi.fn()
    renderHook(() => usePermissionModeInvariant('judge', declared, setMode))
    expect(setMode).not.toHaveBeenCalled()
  })

  it('moves an undeclared pick to the first declared mode', () => {
    const setMode = vi.fn()
    renderHook(() => usePermissionModeInvariant('plan', ['default', 'acceptEdits', 'dontAsk'], setMode))
    expect(setMode).toHaveBeenCalledWith('default')
  })

  it('waits for the catalog before correcting anything', () => {
    const setMode = vi.fn()
    renderHook(() => usePermissionModeInvariant('plan', undefined, setMode))
    expect(setMode).not.toHaveBeenCalled()
  })
})
