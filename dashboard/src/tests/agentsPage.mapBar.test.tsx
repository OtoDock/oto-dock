/**
 * AgentsPage — the top bar over the map. The 3D scene is always dark, so in
 * map view the bar sits inside a scoped `.dark` wrapper; the grid and
 * departments views get no wrapper box at all.
 * jsdom has no WebGL, so the map is stubbed to keep the page in map view.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AgentsPage from '../pages/AgentsPage'

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      sub: 'user-1', email: 'a@b.c', name: 'A', role: 'admin',
      agents: ['alpha'], agent_roles: {}, default_agent: '',
    },
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

// The map never reports WebGL unavailable here, so the page stays in map view.
vi.mock('../components/agents-map/AgentsMap3D', () => ({
  default: () => <div data-testid="map-stub" />,
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

const classesOf = (el: HTMLElement) => el.className.split(/\s+/)

describe('AgentsPage top bar over the map', () => {
  it('is dark in map view and boxless in grid view', () => {
    mount()
    expect(screen.getByTestId('map-stub')).toBeInTheDocument()
    const bar = screen.getByTestId('agents-top-bar')
    expect(classesOf(bar)).toContain('dark')
    expect(classesOf(bar)).toContain('bg-p-bg')
    expect(bar).toContainElement(screen.getByRole('button', { name: 'Map' }))

    fireEvent.click(screen.getByRole('button', { name: 'Grid' }))
    expect(screen.queryByTestId('map-stub')).toBeNull()
    expect(screen.getByTestId('agent-card')).toBeInTheDocument()
    const gridBar = screen.getByTestId('agents-top-bar')
    expect(classesOf(gridBar)).not.toContain('dark')
    expect(classesOf(gridBar)).toContain('contents')

    fireEvent.click(screen.getByRole('button', { name: 'Map' }))
    expect(classesOf(screen.getByTestId('agents-top-bar'))).toContain('dark')
  })
})
