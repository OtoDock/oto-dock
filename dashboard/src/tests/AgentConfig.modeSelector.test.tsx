import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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
  },
  // Execution-layers payload for the effort-gating tests. undefined = not
  // loaded, which is what the mode-selector tests run with.
  layers: undefined as
    | Record<string, { models: { value: string; label: string; provider?: string; supports_xhigh?: boolean }[] }>
    | undefined,
}))

vi.mock('@/api/agents', () => ({
  useAgentInfo: () => ({ data: h.agentInfo, isLoading: false }),
  useUpdateAgent: () => ({ mutate: h.updateMock, isPending: false }),
  useDeleteAgent: () => ({ mutate: vi.fn(), isPending: false }),
  useDelegationTargets: () => ({ data: undefined }),
  useSetDelegationTargets: () => ({ mutate: vi.fn(), isPending: false }),
  useExecutionLayers: () => ({ data: h.layers }),
  useSetDefaultForNewUsers: () => ({ mutate: vi.fn() }),
}))
vi.mock('@/api/remoteMachines', () => ({ useRemoteMachines: () => ({ data: [] }) }))
vi.mock('@/api/memory', () => ({
  useMemorySettings: () => ({ data: { user_memory_enabled: true, agent_memory_enabled: true } }),
  useAgentMemorySettings: () => ({ data: { user_memory_enabled: true, agent_memory_enabled: true } }),
  useSetAgentMemoryToggle: () => ({ mutate: vi.fn() }),
  useClearAgentMemory: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/contexts/AuthContext', () => ({
  // Admin → canManageAgent is true → the selector renders editable.
  useAuth: () => ({ user: { role: 'admin', sub: 'u1', agent_roles: {} } }),
}))

import AgentConfig from '@/pages/agent/AgentConfig'

function renderConfig() {
  return render(
    <MemoryRouter initialEntries={['/agents/demo/config']}>
      <Routes>
        <Route path="/agents/:name/config" element={<AgentConfig />} />
      </Routes>
    </MemoryRouter>,
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
    h.layers = {
      'claude-code-cli': {
        models: [
          { value: '', label: 'System Default' },
          { value: 'claude-fable-5', label: 'Fable 5 (1M context)', provider: 'anthropic', supports_xhigh: true },
          { value: 'claude-haiku-4-5', label: 'Haiku 4.5 (200K)', provider: 'anthropic' },
        ],
      },
    }
  })

  const xhighOption = () => screen.queryByRole('option', { name: 'XHigh' })

  it('offers XHigh on Auto when the auto-resolved model supports it', () => {
    // Auto ('') resolves to the first real model of the first engine — Fable 5
    // here — so the flagless "System Default" placeholder must not hide XHigh.
    renderConfig()
    expect(xhighOption()).toBeInTheDocument()
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
})
