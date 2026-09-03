import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// ─── AgentConfig — platform-configured gate on the engine cards ─────────────
// A non-admin manager can only NEWLY enable an engine the platform has a
// subscription for (`configured` on /v1/execution-layers; server mirror in
// PATCH /v1/agents). Already-enabled engines stay uncheckable-only
// (grandfathering); admins toggle freely; a loading catalog must never flash
// a locked card.

const h = vi.hoisted(() => ({
  updateMock: vi.fn(),
  agentInfo: {
    name: 'demo',
    display_name: 'Demo',
    collaborative: true,
    default_scope: 'user' as 'user' | 'agent',
    default_model: '',
    execution_path: 'claude-code-cli',
    execution_paths: ['claude-code-cli'],
  },
  layers: undefined as Record<string, {
    display_name?: string
    configured?: boolean
    models: { value: string; label: string }[]
  }> | undefined,
  user: { role: 'member', sub: 'u1', agent_roles: { demo: 'manager' } } as {
    role: string; sub: string; agent_roles: Record<string, string>
  },
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
  useAuth: () => ({ user: h.user }),
}))

import AgentConfig from '@/pages/agent/AgentConfig'

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

const LAYERS = {
  'claude-code-cli': {
    display_name: 'Claude Code CLI', configured: true,
    models: [{ value: 'claude-sonnet-5', label: 'Sonnet 5' }],
  },
  'codex-cli': {
    display_name: 'OpenAI Codex', configured: false,
    models: [{ value: 'gpt-5', label: 'GPT-5' }],
  },
}

// The engine cards are <button>s whose text includes the engine label.
const engineCard = (label: string) =>
  screen.getAllByRole('button').find(b =>
    b.textContent?.includes(label)
    && b.className.includes('items-start')) as HTMLButtonElement

describe('AgentConfig — engine enable gate', () => {
  beforeEach(() => {
    h.updateMock.mockClear()
    h.user = { role: 'member', sub: 'u1', agent_roles: { demo: 'manager' } }
    h.layers = { ...LAYERS }
    h.agentInfo.execution_paths = ['claude-code-cli']
  })

  it('manager: unconfigured + unchecked engine renders disabled with the hint', () => {
    renderConfig()
    const codex = engineCard('Codex')
    expect(codex.disabled).toBe(true)
    expect(codex.textContent).toContain(
      'No Codex subscription is connected on this platform')
    // No admin tail on the manager's hint.
    expect(codex.textContent).not.toContain('Platform → AI Engines')
  })

  it('manager: configured engine stays toggleable', () => {
    renderConfig()
    expect(engineCard('Claude Code').disabled).toBe(false)
  })

  it('manager: an ENABLED engine stays uncheckable-only when its platform sub vanished', () => {
    h.agentInfo.execution_paths = ['claude-code-cli', 'codex-cli']
    renderConfig()
    // Grandfathered: still enabled (uncheck allowed) → not disabled.
    expect(engineCard('Codex').disabled).toBe(false)
  })

  it('admin: unconfigured engine stays enabled, hint points at Platform → AI Engines', () => {
    h.user = { role: 'admin', sub: 'u1', agent_roles: {} }
    renderConfig()
    const codex = engineCard('Codex')
    expect(codex.disabled).toBe(false)
    expect(codex.textContent).toContain('Platform → AI Engines')
  })

  it('loading catalog (no configured field) never flashes a locked card', () => {
    h.layers = undefined
    renderConfig()
    expect(engineCard('Claude Code').disabled).toBe(false)
  })
})
