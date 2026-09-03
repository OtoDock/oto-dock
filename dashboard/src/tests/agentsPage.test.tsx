/**
 * AgentsPage — the map/grid/departments shell over the /agents route.
 *
 * jsdom has no WebGL, so mounting the page exercises the REAL fallback
 * path: AgentsMap3D fails renderer creation → onUnavailable → the page
 * lands on the grid view with the Map toggle disabled. That contract is
 * what keeps every other test that mounts this route from exploding.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AgentsPage from '../pages/AgentsPage'

const mockUser = vi.hoisted(() => ({
  current: {
    sub: 'user-1',
    email: 'a@b.c',
    name: 'A',
    role: 'admin',
    agents: ['alpha'],
    agent_roles: {},
    default_agent: '',
  } as Record<string, unknown>,
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser.current,
    setUser: vi.fn(),
    refreshUser: vi.fn(),
  }),
}))

vi.mock('../api/agents', () => ({
  useAgents: () => ({
    data: [{
      name: 'alpha', display_name: 'Alpha', admin_only: false,
      execution_path: 'claude-code-cli', execution_paths: ['claude-code-cli'],
      execution_target: 'local', collaborative: true, default_model: '',
      default_scope: 'user', color: '', description: '', mcp_count: 0,
      mcp_names: [], schedule_count: 0, trigger_count: 0,
      has_workspace: false, department_id: '', department_level_id: '',
    }],
    isLoading: false,
  }),
  useSetDefaultAgent: () => ({ mutate: vi.fn() }),
  useUpdateAgent: () => ({ mutate: vi.fn() }),
}))

vi.mock('../api/departments', () => ({
  useDepartments: () => ({ data: [] }),
  useAgentsActivity: () => ({ data: [] }),
  useDelegationEdges: () => ({ data: [] }),
  useCreateDepartment: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useUpdateDepartment: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteDepartment: () => ({ mutate: vi.fn(), isPending: false }),
  useSetDepartmentLevels: () => ({ mutate: vi.fn(), isPending: false }),
  useAdminAddUserAgent: () => ({ mutate: vi.fn() }),
}))

vi.mock('../api/userUiPrefs', () => ({
  useMyUiPrefs: () => ({ data: {} }),
  useUpdateMyUiPrefs: () => ({ mutate: vi.fn() }),
}))

vi.mock('../components/AgentCard', () => ({
  default: ({ agent }: { agent: { display_name: string } }) => (
    <div data-testid="agent-card">{agent.display_name}</div>
  ),
}))
vi.mock('../components/AgentInstallModal', () => ({ default: () => null }))
vi.mock('../components/CommunityAgentsBrowser', () => ({ default: () => null }))

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AgentsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AgentsPage', () => {
  it('falls back to the grid when WebGL is unavailable (jsdom)', async () => {
    mount()
    await waitFor(() => {
      expect(screen.getByTestId('agent-card')).toBeInTheDocument()
    })
    const mapBtn = screen.getByRole('button', { name: 'Map' })
    expect(mapBtn).toBeDisabled()
  })

  it('shows the Departments tab for admins', async () => {
    mount()
    expect(screen.getByRole('button', { name: 'Departments' })).toBeInTheDocument()
  })

  it('hides the Departments tab for members', async () => {
    mockUser.current = { ...mockUser.current, role: 'member' }
    mount()
    expect(screen.queryByRole('button', { name: 'Departments' })).toBeNull()
    mockUser.current = { ...mockUser.current, role: 'admin' }
  })
})
