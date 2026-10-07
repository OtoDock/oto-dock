import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// ─── AgentConfig — the Template row and the template update banner ──────────
// An agent installed from a community template shows the template and the
// version it runs in the identity card ALWAYS (a manager knows what they
// have), and the update banner only when the catalog has a newer version —
// for a platform admin or creator who manages the agent.

const h = vi.hoisted(() => ({
  agentInfo: {
    name: 'demo',
    display_name: 'Demo',
    collaborative: true,
    default_scope: 'user' as 'user' | 'agent',
    default_model: '',
    execution_path: 'claude-code-cli',
    execution_paths: ['claude-code-cli'],
    department_id: '',
    department_level_id: '',
    community_template: undefined as string | null | undefined,
    community_template_version: undefined as string | null | undefined,
    template_update: null as null | {
      installed_version: string; catalog_version: string; update_available: boolean; compat_ok: boolean
    },
  },
  user: { role: 'admin', sub: 'u1', agent_roles: {} as Record<string, string> },
}))

vi.mock('@/api/agents', () => ({
  useAgentInfo: () => ({ data: h.agentInfo, isLoading: false }),
  useUpdateAgent: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteAgent: () => ({ mutate: vi.fn(), isPending: false }),
  useDelegationTargets: () => ({ data: undefined }),
  useSetDelegationTargets: () => ({ mutate: vi.fn(), isPending: false }),
  useExecutionLayers: () => ({ data: undefined }),
  useSetDefaultForNewUsers: () => ({ mutate: vi.fn() }),
  useAgentUsers: () => ({ data: [] }),
  AgentUpdateError: class AgentUpdateError extends Error {},
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

describe('AgentConfig — the Template row', () => {
  beforeEach(() => {
    h.agentInfo.community_template = 'personal-assistant'
    h.agentInfo.community_template_version = '3.0.0'
    h.agentInfo.template_update = null
    h.user = { role: 'admin', sub: 'u1', agent_roles: {} }
  })

  it('names the template and the installed version, with no update in sight', () => {
    renderConfig()
    const row = screen.getByTestId('template-row')
    expect(row.textContent).toContain('personal-assistant')
    expect(row.textContent).toContain('version 3.0.0')
    expect(screen.queryByTestId('template-update-banner')).toBeNull()
  })

  it('reads "local template" for a locally authored one and "version unknown" without a version', () => {
    h.agentInfo.community_template = 'local:my-agent'
    h.agentInfo.community_template_version = null
    renderConfig()
    const row = screen.getByTestId('template-row')
    expect(row.textContent).toContain('local template')
    expect(row.textContent).not.toContain('local:my-agent')
    expect(row.textContent).toContain('version unknown')
  })

  it('is absent for a hand-made agent', () => {
    h.agentInfo.community_template = null
    h.agentInfo.community_template_version = null
    renderConfig()
    expect(screen.queryByTestId('template-row')).toBeNull()
  })
})

describe('AgentConfig — the template update banner', () => {
  beforeEach(() => {
    h.agentInfo.community_template = 'personal-assistant'
    h.agentInfo.community_template_version = '2.0.0'
    h.agentInfo.template_update = {
      installed_version: '2.0.0', catalog_version: '3.0.0', update_available: true, compat_ok: true,
    }
    h.user = { role: 'admin', sub: 'u1', agent_roles: {} }
  })

  it('shows for an admin when the catalog is newer, and the row keeps the installed version', () => {
    renderConfig()
    const banner = screen.getByTestId('template-update-banner')
    expect(banner.textContent).toContain('Version 3.0.0')
    expect(banner.textContent).toContain('installed: 2.0.0')
    expect((screen.getByRole('button', { name: 'Update' }) as HTMLButtonElement).disabled).toBe(false)
    expect(screen.getByTestId('template-row').textContent).toContain('version 2.0.0')
  })

  it('disables Update when the version needs a newer OtoDock', () => {
    h.agentInfo.template_update = { ...h.agentInfo.template_update!, compat_ok: false }
    renderConfig()
    expect(screen.getByTestId('template-update-banner').textContent).toContain('It needs a newer OtoDock.')
    expect((screen.getByRole('button', { name: 'Update' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows for a platform member who manages the agent (a manager runs the agent entirely)', () => {
    h.user = { role: 'member', sub: 'u1', agent_roles: { demo: 'manager' } }
    renderConfig()
    expect(screen.getByTestId('template-update-banner')).toBeTruthy()
    expect(screen.getByTestId('template-row')).toBeTruthy()
  })

  it('is hidden from a viewer of the agent', () => {
    h.user = { role: 'member', sub: 'u1', agent_roles: { demo: 'viewer' } }
    renderConfig()
    expect(screen.queryByTestId('template-update-banner')).toBeNull()
  })
})
