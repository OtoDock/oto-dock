import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// ─── AgentConfig — the Execution Target select says a machine's state in
// words ─────────────────────────────────────────────────────────────────────
// The select and the badge beside it read "never connected", "offline",
// "paused": never the stored word with its underscore, never in brackets.

const h = vi.hoisted(() => ({
  agentInfo: {
    name: 'demo',
    display_name: 'Demo',
    collaborative: true,
    default_scope: 'user' as 'user' | 'agent',
    default_model: '',
    execution_path: 'claude-code-cli',
    execution_paths: ['claude-code-cli'],
    execution_target: 'm-new',
  },
  layers: undefined as Record<string, import('@/api/agents').LayerCapabilities> | undefined,
  machines: [] as Array<Record<string, unknown>>,
}))

const updateAgent = vi.hoisted(() => ({ mutate: vi.fn(), mutateAsync: () => Promise.resolve({}) }))
vi.mock('@/api/agents', () => ({
  useAgentInfo: () => ({ data: h.agentInfo, isLoading: false }),
  // One function for every render, as TanStack's own: the page's save
  // effects list it.
  useUpdateAgent: () => ({ mutate: updateAgent.mutate, mutateAsync: updateAgent.mutateAsync, isPending: false }),
  useDeleteAgent: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve()), isPending: false }),
  useDelegationTargets: () => ({ data: undefined }),
  useSetDelegationTargets: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve({})), isPending: false }),
  useExecutionLayers: () => ({ data: h.layers }),
  useSetDefaultForNewUsers: () => ({ mutate: vi.fn() }),
  useAgentUsers: () => ({ data: [] }),
  AgentUpdateError: class AgentUpdateError extends Error {},
  useKnowledgeAttachments: () => ({ data: undefined }),
  useKnowledgeLibraries: () => ({ data: undefined }),
  useSetKnowledgeLibrary: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve({})), isPending: false }),
  useAttachKnowledgeLibrary: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve({})), isPending: false }),
  useDetachKnowledgeLibrary: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve({})), isPending: false }),
  useAgentFiles: () => ({ data: undefined }),
}))
vi.mock('@/api/remoteMachines', async (orig) => ({
  ...(await orig<typeof import('@/api/remoteMachines')>()), useRemoteMachines: () => ({ data: h.machines }),
}))
vi.mock('@/api/departments', () => ({ useDepartments: () => ({ data: [] }) }))
vi.mock('@/api/memory', () => ({
  useAgentMemorySettings: () => ({
    data: {
      user_memory_enabled: true,
      agent_memory_enabled: true,
      master: { user_memory_enabled: true, agent_memory_enabled: true },
    },
  }),
  useSetAgentMemoryToggle: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve({})) }),
  useClearAgentMemory: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve({})), isPending: false }),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'admin', sub: 'u1', agent_roles: {} } }),
}))

import AgentConfig from '@/pages/agent/AgentConfig'
import { machineStateWord, MACHINE_STATE } from '@/lib/status/machine'
import { claudeLike } from './fixtures/engines'

function machine(id: string, name: string, status: string) {
  return { id, name, status, last_seen: null, registered_by: 'u1', pairing_scope: 'admin', capabilities: {} }
}

function renderConfig() {
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

describe('machineStateWord', () => {
  it('says every state in words', () => {
    expect(machineStateWord(MACHINE_STATE.ONLINE)).toBe('online')
    expect(machineStateWord(MACHINE_STATE.STALE)).toBe('slow connection')
    expect(machineStateWord(MACHINE_STATE.PAUSED)).toBe('paused')
    expect(machineStateWord(MACHINE_STATE.DISCONNECTED)).toBe('offline')
    expect(machineStateWord(MACHINE_STATE.NEVER_CONNECTED)).toBe('never connected')
  })

  it('reads a word it does not know as offline', () => {
    expect(machineStateWord('offline')).toBe('offline')
    expect(machineStateWord(undefined)).toBe('offline')
  })
})

describe('AgentConfig — the Execution Target select', () => {
  it('names each machine with its state in words', () => {
    h.layers = { 'claude-code-cli': claudeLike({ configured: true }) }
    h.machines = [
      machine('m-on', 'rack-1', MACHINE_STATE.ONLINE),
      machine('m-new', 'dev-box-t1-rig', MACHINE_STATE.NEVER_CONNECTED),
      machine('m-off', 'desktop', MACHINE_STATE.DISCONNECTED),
      machine('m-pause', 'laptop', MACHINE_STATE.PAUSED),
    ]
    renderConfig()
    const options = screen.getAllByRole('option').map(o => o.textContent)
    expect(options).toContain('rack-1')
    expect(options).toContain('dev-box-t1-rig, never connected')
    expect(options).toContain('desktop, offline')
    expect(options).toContain('laptop, paused')
    for (const text of options) {
      expect(text).not.toMatch(/[[\]_]/)
    }
    // The badge beside the select says the chosen machine's state too.
    expect(screen.getByText('never connected')).toBeTruthy()
    expect(screen.queryByText('never_connected')).toBeNull()
  })
})
