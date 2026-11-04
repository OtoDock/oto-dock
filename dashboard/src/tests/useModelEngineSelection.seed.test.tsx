import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'

import { useModelEngineReconcile } from '@/pages/agent/chat/useModelEngineSelection'
import { useAgentPrefsStore } from '@/store/agentPrefsStore'
import type { AgentSummary, LayerCapabilities } from '@/api/agents'
import { claudeLike, codexLike } from './fixtures/engines'

// ─── Engines resolve per person: a new chat opens on the engine the server
//     resolved for the person (engine_for_me, model_for_me), the person's
//     own sticky pick on an engine they may run keeps winning, and without
//     a resolution the agent's default model's engine seeds as before. ──────

const LAYERS: Record<string, LayerCapabilities> = {
  'claude-code-cli': claudeLike({ models: [{ value: '', label: 'System Default' }, { value: 'claude-sonnet-5', label: 'Sonnet 5' }] }),
  'codex-cli': codexLike({ models: [{ value: 'gpt-5', label: 'GPT-5' }, { value: 'gpt-mini', label: 'GPT mini' }] }),
}

function renderReconcile(over: Partial<Parameters<typeof useModelEngineReconcile>[0]> = {}) {
  const setModel = vi.fn()
  const setSelectedLayer = vi.fn()
  const { result } = renderHook(() => useModelEngineReconcile({
    layers: LAYERS,
    agentExecutionPaths: ['claude-code-cli', 'codex-cli'],
    chatActiveLayer: null, processAlive: true,
    canRunLayer: { 'claude-code-cli': true, 'codex-cli': true },
    viewedStreaming: false, warming: false,
    model: 'claude-sonnet-5',
    setPendingEngineSwitch: vi.fn(), setEngineSwitchBusy: vi.fn(), setEngineSwitchError: vi.fn(),
    chatId: null, agentName: 'dev', sessionId: null, setProcessAlive: vi.fn(),
    urlChatId: undefined, agentDefaultModel: 'claude-sonnet-5',
    setModel, setMode: vi.fn(), selectedLayer: null, setSelectedLayer,
    currentAgent: { name: 'dev' } as AgentSummary,
    ...over,
  }))
  return { setModel, setSelectedLayer, groups: () => (result.current.modelGroups ?? []).map(g => g.layer) }
}

describe('the seed of a new chat', () => {
  beforeEach(() => { useAgentPrefsStore.setState({ lastModel: {} }) })

  it('opens on the engine resolved for the person, with its model', () => {
    const { setModel, setSelectedLayer } = renderReconcile({ engineForMe: 'codex-cli', modelForMe: 'gpt-mini' })
    expect(setSelectedLayer).toHaveBeenLastCalledWith('codex-cli')
    expect(setModel).toHaveBeenLastCalledWith('gpt-mini')
  })

  it("keeps the person's own pick when its engine is one they may run", () => {
    useAgentPrefsStore.setState({ lastModel: { dev: 'claude-sonnet-5' } })
    const { setSelectedLayer } = renderReconcile({ engineForMe: 'codex-cli', modelForMe: 'gpt-mini' })
    expect(setSelectedLayer).toHaveBeenLastCalledWith('claude-code-cli')
  })

  it("a pick on an engine they may not run gives way to the resolved engine", () => {
    useAgentPrefsStore.setState({ lastModel: { dev: 'claude-sonnet-5' } })
    const { setSelectedLayer } = renderReconcile({
      canRunLayer: { 'claude-code-cli': false, 'codex-cli': true },
      engineForMe: 'codex-cli', modelForMe: 'gpt-mini',
    })
    expect(setSelectedLayer).toHaveBeenLastCalledWith('codex-cli')
  })

  it('without a resolution the agent default model names the engine, as before', () => {
    const { setSelectedLayer } = renderReconcile()
    expect(setSelectedLayer).toHaveBeenLastCalledWith('claude-code-cli')
  })

  it('a resolved engine whose model is not offered seeds the engine own default', () => {
    const { setModel, setSelectedLayer } = renderReconcile({ engineForMe: 'codex-cli', modelForMe: 'gone' })
    expect(setSelectedLayer).toHaveBeenLastCalledWith('codex-cli')
    expect(setModel).toHaveBeenLastCalledWith('gpt-5')
  })
})

// ─── The groups follow the person's order (engine_order_for_me, the server's
//     answer): the engines they use most on the agent first ─────────────────

describe("the model menu's engine groups", () => {
  beforeEach(() => { useAgentPrefsStore.setState({ lastModel: {} }) })

  it("come in the person's order", () => {
    const { groups } = renderReconcile({
      currentAgent: { name: 'dev', engine_order_for_me: ['codex-cli', 'claude-code-cli'] } as AgentSummary,
    })
    expect(groups()).toEqual(['codex-cli', 'claude-code-cli'])
  })

  it("keep the agent's order when the server sent none", () => {
    const { groups } = renderReconcile()
    expect(groups()).toEqual(['claude-code-cli', 'codex-cli'])
  })
})
