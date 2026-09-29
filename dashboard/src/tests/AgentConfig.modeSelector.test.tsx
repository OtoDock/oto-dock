import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// Shared mock state — hoisted so the vi.mock factories below can close over it.
const h = vi.hoisted(() => ({
  updateMock: vi.fn(),
  // Partial AgentInfo is fine — AgentConfig guards every field with `|| ''`
  // / `?? false`. Starts as Personal + shared (collaborative + user scope).
  agentInfo: {
    name: 'demo',
    display_name: 'Demo',
    collaborative: true,
    default_scope: 'user' as 'user' | 'agent',
    default_model: '',
    default_effort: '',
    default_execution_mode: '',
    execution_path: 'claude-code-cli',
    execution_paths: ['claude-code-cli'],
  },
  // Execution-layers payload for the effort-gating tests. undefined = not
  // loaded, which is what the mode-selector tests run with.
  layers: undefined as Record<string, import('@/api/agents').LayerCapabilities> | undefined,
}))

vi.mock('@/api/agents', () => ({
  useAgentInfo: () => ({ data: h.agentInfo, isLoading: false }),
  useUpdateAgent: () => ({ mutate: h.updateMock, isPending: false }),
  useDeleteAgent: () => ({ mutate: vi.fn(), isPending: false }),
  useDelegationTargets: () => ({ data: undefined }),
  useSetDelegationTargets: () => ({ mutate: vi.fn(), isPending: false }),
  useExecutionLayers: () => ({ data: h.layers }),
  useSetDefaultForNewUsers: () => ({ mutate: vi.fn() }),
  useKnowledgeAttachments: () => ({ data: undefined }),
  useKnowledgeLibraries: () => ({ data: undefined }),
  useSetKnowledgeLibrary: () => ({ mutate: vi.fn(), isPending: false }),
  useAttachKnowledgeLibrary: () => ({ mutate: vi.fn(), isPending: false }),
  useDetachKnowledgeLibrary: () => ({ mutate: vi.fn(), isPending: false }),
  useAgentFiles: () => ({ data: undefined }),
}))
vi.mock('@/api/remoteMachines', () => ({ useRemoteMachines: () => ({ data: [] }) }))
vi.mock('@/api/departments', () => ({ useDepartments: () => ({ data: [] }) }))
vi.mock('@/api/memory', () => ({
  useAgentMemorySettings: () => ({
    data: {
      user_memory_enabled: true,
      agent_memory_enabled: true,
      master: { user_memory_enabled: true, agent_memory_enabled: true },
    },
  }),
  useSetAgentMemoryToggle: () => ({ mutate: vi.fn() }),
  useClearAgentMemory: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/contexts/AuthContext', () => ({
  // Admin → canManageAgent is true → the selector renders editable.
  useAuth: () => ({ user: { role: 'admin', sub: 'u1', agent_roles: {} } }),
}))

import AgentConfig from '@/pages/agent/AgentConfig'
import { claudeLike, codexLike, directLike } from './fixtures/engines'

function renderConfig() {
  // AgentConfig calls useQueryClient (department-save invalidation), so the
  // harness needs a real provider around it.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/agents/demo/config']}>
        <Routes>
          <Route path="/agents/:name/config" element={<AgentConfig />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const radio = (label: RegExp) => screen.getByRole('radio', { name: label })

describe('AgentConfig — visibility mode selector', () => {
  beforeEach(() => {
    h.updateMock.mockClear()
    h.agentInfo.collaborative = true
    h.agentInfo.default_scope = 'user'
  })

  it('renders all four modes with the current one selected', () => {
    renderConfig()
    expect(radio(/Personal \+ shared/)).toBeChecked()
    expect(radio(/Shared \+ personal/)).not.toBeChecked()
    expect(radio(/Personal only/)).toBeInTheDocument()
    expect(radio(/Shared only/)).toBeInTheDocument()
  })

  it('saves both columns in one PATCH for a non-shared-only switch', () => {
    renderConfig()
    fireEvent.click(radio(/Shared \+ personal/))
    expect(h.updateMock).toHaveBeenCalledTimes(1)
    expect(h.updateMock.mock.calls[0][0]).toMatchObject({
      name: 'demo',
      collaborative: true,
      default_scope: 'agent',
    })
  })

  it('requires a typed confirmation before switching into Shared only', () => {
    renderConfig()
    fireEvent.click(radio(/Shared only/))
    // No write yet — a confirm modal intercepts the shared-only flip.
    expect(h.updateMock).not.toHaveBeenCalled()
    expect(screen.getByText(/Switch to Shared only\?/i)).toBeInTheDocument()

    // Type the confirm word, then commit.
    fireEvent.change(screen.getByPlaceholderText('CONFIRM'), { target: { value: 'CONFIRM' } })
    fireEvent.click(screen.getByRole('button', { name: /Switch mode/i }))
    expect(h.updateMock).toHaveBeenCalledTimes(1)
    expect(h.updateMock.mock.calls[0][0]).toMatchObject({
      name: 'demo',
      collaborative: false,
      default_scope: 'agent',
    })
  })

  it('cancelling the confirm leaves the mode unchanged', () => {
    renderConfig()
    fireEvent.click(radio(/Shared only/))
    fireEvent.click(screen.getByRole('button', { name: /Cancel/i }))
    expect(h.updateMock).not.toHaveBeenCalled()
    expect(radio(/Personal \+ shared/)).toBeChecked()
  })

  it('hides the shared agent-memory controls in Personal only mode', () => {
    h.agentInfo.collaborative = false
    h.agentInfo.default_scope = 'user' // Personal only
    renderConfig()
    // The Memory card keeps the user row but drops the shared-agent row/button.
    expect(screen.getByText('User memory')).toBeInTheDocument()
    expect(screen.queryByText('Agent memory')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Clear shared agent memory/i })).not.toBeInTheDocument()
  })
})

describe('AgentConfig — XHigh effort gating', () => {
  beforeEach(() => {
    h.updateMock.mockClear()
    h.agentInfo.collaborative = true
    h.agentInfo.default_scope = 'user'
    h.agentInfo.default_model = ''
    h.agentInfo.default_effort = ''
    h.agentInfo.default_execution_mode = ''
    h.agentInfo.execution_path = 'claude-code-cli'
    h.agentInfo.execution_paths = ['claude-code-cli']
    h.layers = {
      'claude-code-cli': claudeLike({
        models: [
          { value: '', label: 'System Default' },
          { value: 'claude-fable-5', label: 'Fable 5 (1M context)', provider: 'anthropic', supports_xhigh: true },
          { value: 'claude-haiku-4-5', label: 'Haiku 4.5 (200K)', provider: 'anthropic' },
        ],
        // What the SERVER says Auto resolves to on this engine.
        auto_model: 'claude-fable-5', auto_model_label: 'Fable 5 (1M context)',
      }),
    }
  })

  const xhighOption = () => screen.queryByRole('option', { name: 'XHigh' })
  const autoOption = () => screen.getByRole('option', { name: /^Auto/ })

  it('offers XHigh on Auto when the model the server resolves supports it', () => {
    // Auto ('') is whatever the catalog's auto_model says — Fable 5 here — so
    // the flagless "System Default" placeholder must not hide XHigh.
    renderConfig()
    expect(xhighOption()).toBeInTheDocument()
    expect(autoOption()).toHaveTextContent('Auto — Fable 5 (1M context)')
  })

  it('names the model Auto resolves to as the server says, not the first in the list', () => {
    h.layers!['claude-code-cli'].auto_model = 'claude-haiku-4-5'
    h.layers!['claude-code-cli'].auto_model_label = 'Haiku 4.5 (200K)'
    renderConfig()
    expect(autoOption()).toHaveTextContent('Auto — Haiku 4.5 (200K)')
    // Haiku has no xhigh flag, so Auto offers none — the list's first model is not consulted.
    expect(xhighOption()).not.toBeInTheDocument()
  })

  it('a resolved model the served list filtered out is still named, with no flags', () => {
    h.layers!['claude-code-cli'].auto_model = 'claude-ghost-1'
    h.layers!['claude-code-cli'].auto_model_label = 'Ghost 1'
    renderConfig()
    expect(autoOption()).toHaveTextContent('Auto — Ghost 1')
    expect(xhighOption()).not.toBeInTheDocument()
  })

  it('plain "Auto" when the server resolves nothing', () => {
    h.layers!['claude-code-cli'].auto_model = ''
    h.layers!['claude-code-cli'].auto_model_label = ''
    renderConfig()
    expect(autoOption()).toHaveTextContent(/^Auto$/)
    expect(xhighOption()).not.toBeInTheDocument()
  })

  it('offers XHigh when the selected model supports it', () => {
    h.agentInfo.default_model = 'claude-fable-5'
    renderConfig()
    expect(xhighOption()).toBeInTheDocument()
  })

  it('hides XHigh when the selected model does not support it', () => {
    h.agentInfo.default_model = 'claude-haiku-4-5'
    renderConfig()
    expect(xhighOption()).not.toBeInTheDocument()
  })

  it('writes a stored effort the model no longer offers back as what the select shows', () => {
    h.agentInfo.default_model = 'claude-haiku-4-5'
    h.agentInfo.default_effort = 'xhigh'
    renderConfig()
    expect(screen.getByRole('option', { name: 'High' })).toBeInTheDocument()
    expect(h.updateMock).toHaveBeenCalledWith({ name: 'demo', default_effort: 'high' }, expect.anything())
  })
})

describe('AgentConfig — the effort ladder follows the engines and the provider', () => {
  const option = (name: string) => screen.queryByRole('option', { name })

  beforeEach(() => {
    h.updateMock.mockClear()
    h.agentInfo.collaborative = true
    h.agentInfo.default_scope = 'user'
    h.agentInfo.default_model = ''
    h.agentInfo.default_effort = ''
    h.agentInfo.default_execution_mode = ''
  })

  it('a provider whose ladder tops at xhigh offers no Max, and a stored Max becomes XHigh', () => {
    h.agentInfo.execution_path = 'direct-llm'
    h.agentInfo.execution_paths = ['direct-llm']
    h.agentInfo.default_model = 'gpt-6-luna'
    h.agentInfo.default_effort = 'max'
    h.layers = {
      'direct-llm': directLike({
        models: [
          { value: '', label: 'System Default' },
          { value: 'gpt-6-luna', label: 'GPT-6 Luna', provider: 'openai', supports_xhigh: true },
          { value: 'openai/gpt-oss-120b', label: 'GPT-OSS 120B', provider: 'groq' },
        ],
        auto_model: 'gpt-6-luna', auto_model_label: 'GPT-6 Luna',
      }),
    }
    renderConfig()
    expect(option('XHigh')).toBeInTheDocument()
    expect(option('Max')).not.toBeInTheDocument()
    expect(option('Ultra')).not.toBeInTheDocument()
    expect(h.updateMock).toHaveBeenCalledWith({ name: 'demo', default_effort: 'xhigh' }, expect.anything())
  })

  it('a Groq model offers Low, Medium and High only', () => {
    h.agentInfo.execution_path = 'direct-llm'
    h.agentInfo.execution_paths = ['direct-llm']
    h.agentInfo.default_model = 'openai/gpt-oss-120b'
    h.layers = {
      'direct-llm': directLike({
        models: [
          { value: 'openai/gpt-oss-120b', label: 'GPT-OSS 120B', provider: 'groq', supports_xhigh: true },
        ],
      }),
    }
    renderConfig()
    expect(option('High')).toBeInTheDocument()
    expect(option('XHigh')).not.toBeInTheDocument()
    expect(option('Max')).not.toBeInTheDocument()
    expect(h.updateMock).not.toHaveBeenCalled()
  })

  it('a Codex-only agent whose Auto resolves nothing offers XHigh and Max, never Ultra', () => {
    h.agentInfo.execution_path = 'codex-cli'
    h.agentInfo.execution_paths = ['codex-cli']
    h.layers = {
      'codex-cli': codexLike({
        models: [
          { value: 'gpt-6-sol', label: 'GPT-6 Sol', provider: 'openai', supports_xhigh: true, supports_ultra: true },
        ],
        auto_model: '', auto_model_label: '',
      }),
    }
    const first = renderConfig()
    expect(option('XHigh')).toBeInTheDocument()
    expect(option('Max')).toBeInTheDocument()
    expect(option('Ultra')).not.toBeInTheDocument()
    first.unmount()
    h.agentInfo.default_model = 'gpt-6-sol'
    renderConfig()
    expect(option('Ultra')).toBeInTheDocument()
  })

  it('reconciles nothing before the catalog has answered', () => {
    // A pinned model with a stored xhigh and an interactive default used to be
    // reset and SAVED when the agent row arrived before the catalog.
    h.agentInfo.default_model = 'claude-fable-5'
    h.agentInfo.default_effort = 'xhigh'
    h.agentInfo.default_execution_mode = 'interactive'
    h.layers = undefined
    renderConfig()
    expect(screen.queryByText('Default Effort')).not.toBeInTheDocument()
    expect(h.updateMock).not.toHaveBeenCalled()
  })
})
