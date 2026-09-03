import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// ─── AgentConfig — department assignment row + compiled delegation rows ─────
// The Department row is a platform-org control: visible to admins + creators
// only (backend 403s everyone else) and its two ids ALWAYS ship together in
// ONE PATCH — a lone department_id would leave the agent half-assigned.
// Department-compiled delegation edges render as locked rows ("via <dept>")
// and must NEVER be included in the manual-targets PUT payload.

const h = vi.hoisted(() => ({
  updateMock: vi.fn(),
  setTargetsMock: vi.fn(),
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
  },
  departments: [
    {
      id: 'd1',
      name: 'Engineering',
      created_by_sub: 'u1',
      auto_delegation: true,
      reach: 'adjacent' as const,
      position_hint: '',
      // Deliberately OUT of rank order — selecting the department must pick
      // the rank-0 level, not the first array entry.
      levels: [
        { id: 'lvl-staff', rank: 1, name: 'Staff' },
        { id: 'lvl-lead', rank: 0, name: 'Lead' },
      ],
      members: [],
      can_edit: true,
    },
  ],
  delegationData: undefined as undefined | {
    targets: string[]
    compiled?: { target: string; department_id: string; department_name: string }[]
    available: { name: string; display_name: string; color: string }[]
  },
  user: { role: 'admin', sub: 'u1', agent_roles: {} as Record<string, string> },
}))

vi.mock('@/api/agents', () => ({
  useAgentInfo: () => ({ data: h.agentInfo, isLoading: false }),
  useUpdateAgent: () => ({ mutate: h.updateMock, isPending: false }),
  useDeleteAgent: () => ({ mutate: vi.fn(), isPending: false }),
  useDelegationTargets: () => ({ data: h.delegationData }),
  useSetDelegationTargets: () => ({ mutate: h.setTargetsMock, isPending: false }),
  useExecutionLayers: () => ({ data: undefined }),
  useSetDefaultForNewUsers: () => ({ mutate: vi.fn() }),
  useKnowledgeAttachments: () => ({ data: undefined }),
  useKnowledgeLibraries: () => ({ data: undefined }),
  useSetKnowledgeLibrary: () => ({ mutate: vi.fn(), isPending: false }),
  useAttachKnowledgeLibrary: () => ({ mutate: vi.fn(), isPending: false }),
  useDetachKnowledgeLibrary: () => ({ mutate: vi.fn(), isPending: false }),
  useAgentFiles: () => ({ data: undefined }),
}))
vi.mock('@/api/remoteMachines', () => ({ useRemoteMachines: () => ({ data: [] }) }))
vi.mock('@/api/departments', () => ({ useDepartments: () => ({ data: h.departments }) }))
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

describe('AgentConfig — department row', () => {
  beforeEach(() => {
    h.updateMock.mockClear()
    h.setTargetsMock.mockClear()
    h.agentInfo.department_id = ''
    h.agentInfo.department_level_id = ''
    h.delegationData = undefined
    h.user = { role: 'admin', sub: 'u1', agent_roles: {} }
  })

  it('is hidden for a member-role manager', () => {
    // Per-agent manager but platform role member → canManage (page editable)
    // yet the department row must not render.
    h.user = { role: 'member', sub: 'u1', agent_roles: { demo: 'manager' } }
    renderConfig()
    expect(screen.queryByLabelText('Department')).toBeNull()
    expect(screen.queryByText(/Auto-wires delegation within the department/)).toBeNull()
  })

  it('is visible for an admin', () => {
    renderConfig()
    expect(screen.getByLabelText('Department')).toBeTruthy()
    expect(screen.getByText(/Auto-wires delegation within the department/)).toBeTruthy()
  })

  it('selecting a department sends BOTH ids in one PATCH (first level by rank)', () => {
    renderConfig()
    fireEvent.change(screen.getByLabelText('Department'), { target: { value: 'd1' } })
    expect(h.updateMock).toHaveBeenCalledTimes(1)
    expect(h.updateMock.mock.calls[0][0]).toEqual({
      name: 'demo',
      department_id: 'd1',
      department_level_id: 'lvl-lead', // rank 0, despite array order
    })
  })

  it('changing the level re-sends BOTH ids in one PATCH', () => {
    h.agentInfo.department_id = 'd1'
    h.agentInfo.department_level_id = 'lvl-lead'
    renderConfig()
    fireEvent.change(screen.getByLabelText('Level'), { target: { value: 'lvl-staff' } })
    expect(h.updateMock).toHaveBeenCalledTimes(1)
    expect(h.updateMock.mock.calls[0][0]).toEqual({
      name: 'demo',
      department_id: 'd1',
      department_level_id: 'lvl-staff',
    })
  })

  it('selecting "None" clears both ids together', () => {
    h.agentInfo.department_id = 'd1'
    h.agentInfo.department_level_id = 'lvl-lead'
    renderConfig()
    fireEvent.change(screen.getByLabelText('Department'), { target: { value: '' } })
    expect(h.updateMock).toHaveBeenCalledTimes(1)
    expect(h.updateMock.mock.calls[0][0]).toEqual({
      name: 'demo',
      department_id: '',
      department_level_id: '',
    })
    // Level select disappears once unassigned.
    expect(screen.queryByLabelText('Level')).toBeNull()
  })
})

describe('AgentConfig — compiled delegation targets', () => {
  beforeEach(() => {
    h.updateMock.mockClear()
    h.setTargetsMock.mockClear()
    h.user = { role: 'admin', sub: 'u1', agent_roles: {} }
    h.delegationData = {
      // 'ops' appears in BOTH manual targets and compiled (defensive case):
      // compiled must win — locked row, never re-submitted as manual.
      targets: ['helper', 'ops'],
      compiled: [{ target: 'ops', department_id: 'd1', department_name: 'Engineering' }],
      available: [
        { name: 'helper', display_name: 'Helper', color: '#111111' },
        { name: 'ops', display_name: 'Ops', color: '#222222' },
        { name: 'extra', display_name: 'Extra', color: '#333333' },
      ],
    }
  })

  it('renders a compiled target as disabled with its department note', () => {
    renderConfig()
    const opsRow = screen.getByRole('button', { name: /via Engineering/ }) as HTMLButtonElement
    expect(opsRow.disabled).toBe(true)
    expect(screen.getByText('via Engineering')).toBeTruthy()
  })

  it('excludes compiled targets from the PUT payload when toggling another box', () => {
    renderConfig()
    fireEvent.click(screen.getByRole('button', { name: /Extra/ }))
    expect(h.setTargetsMock).toHaveBeenCalledTimes(1)
    const payload = h.setTargetsMock.mock.calls[0][0]
    expect(payload.agent).toBe('demo')
    expect([...payload.targets].sort()).toEqual(['extra', 'helper'])
    expect(payload.targets).not.toContain('ops')
  })
})

describe('AgentConfig — merged edge semantics (no separate observe toggle)', () => {
  beforeEach(() => {
    h.updateMock.mockClear()
    h.setTargetsMock.mockClear()
    h.user = { role: 'admin', sub: 'u1', agent_roles: {} }
    h.delegationData = {
      targets: ['helper'],
      compiled: [{ target: 'ops', department_id: 'd1', department_name: 'Engineering' }],
      available: [
        { name: 'helper', display_name: 'Helper', color: '#111111' },
        { name: 'ops', display_name: 'Ops', color: '#222222' },
        { name: 'extra', display_name: 'Extra', color: '#333333' },
      ],
    }
  })

  it('renders no eye toggle — the wired edge itself is the read grant', () => {
    renderConfig()
    expect(screen.queryByLabelText('Observe Helper')).toBeNull()
    expect(screen.queryByLabelText('Observe Ops')).toBeNull()
    // The card subtitle states the merged semantics.
    expect(screen.getByText(/readable by this agent's autonomous runs/i)).toBeTruthy()
  })
})
