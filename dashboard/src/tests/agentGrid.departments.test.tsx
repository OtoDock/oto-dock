/**
 * Grid view departments (operator 2026-08-15): one collapsible group per
 * department (house pill idiom, default EXPANDED) with member cards
 * carrying their department role, independents as the plain grid below.
 * In-group order matches the 3D map's stage (level rank, then slug);
 * members with no level sort last with no badge; an agent whose department
 * isn't rendered falls back to the independents (the map's two-part test).
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import AgentGrid from '../pages/AgentGrid'

const agent = (name: string, dept = '', level = '') => ({
  name, display_name: name.toUpperCase(), admin_only: false,
  execution_path: 'claude-code-cli', execution_paths: ['claude-code-cli'],
  execution_target: 'local', collaborative: true, default_model: '',
  default_scope: 'user', color: '', description: '', mcp_count: 0,
  mcp_names: [], schedule_count: 0, trigger_count: 0,
  has_workspace: false, department_id: dept, department_level_id: level,
})

const mockUser = vi.hoisted(() => ({
  current: { sub: 'u1', role: 'member', agents: [], agent_roles: {}, default_agent: 'zeta' },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: mockUser.current, setUser: vi.fn() }),
}))

vi.mock('../api/agents', () => ({
  useAgents: () => ({
    data: [
      agent('astro', 'd-eng', 'lv-senior'),
      agent('boss', 'd-eng', 'lv-head'),
      agent('drifter', 'd-eng', ''),          // no level → last, no badge
      agent('ghost', 'd-hidden', 'lv-x'),     // dept not visible → independent
      agent('omega'),
      agent('zeta'),                          // the favorite → first independent
    ],
    isLoading: false,
  }),
  useSetDefaultAgent: () => ({ mutate: vi.fn() }),
}))

vi.mock('../api/departments', () => ({
  useDepartments: () => ({
    data: [{
      id: 'd-eng', name: 'Engineering', created_by_sub: 'u1',
      auto_delegation: false, reach: 'adjacent', position_hint: '',
      levels: [
        { id: 'lv-senior', rank: 1, name: 'Senior' },
        { id: 'lv-head', rank: 0, name: 'Head' },
      ],
      members: [], can_edit: false,
    }],
  }),
}))

vi.mock('../api/remoteMachines', () => ({
  useRemoteMachines: () => ({ data: [] }),
}))

// Modals pull half the agents API surface — not under test here.
vi.mock('../components/AgentInstallModal', () => ({ default: () => null }))
vi.mock('../components/CommunityAgentsBrowser', () => ({ default: () => null }))

function mount() {
  return render(
    <MemoryRouter>
      <AgentGrid embedded />
    </MemoryRouter>,
  )
}

describe('AgentGrid departments', () => {
  it('renders the department group with count, roles, and map ordering', () => {
    mount()
    const group = screen.getByText('Engineering').closest('section')!
    expect(within(group).getByText('3 agents')).toBeInTheDocument()
    // Head (rank 0) before Senior (rank 1) before the level-less member.
    const cards = within(group).getAllByRole('heading', { level: 3 })
    expect(cards.map(h => h.textContent)).toEqual(['BOSS', 'ASTRO', 'DRIFTER'])
    expect(within(group).getByText('Head')).toBeInTheDocument()
    expect(within(group).getByText('Senior')).toBeInTheDocument()
  })

  it('collapses on the header toggle and expands back', () => {
    mount()
    const toggle = screen.getByRole('button', { name: /Engineering/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(toggle)
    expect(screen.queryByText('BOSS')).not.toBeInTheDocument()
    fireEvent.click(toggle)
    expect(screen.getByText('BOSS')).toBeInTheDocument()
  })

  it('independents render below: favorite first, unrendered-department agents included', () => {
    mount()
    const independents = screen.getAllByRole('heading', { level: 3 })
      .map(h => h.textContent)
      .filter(t => !['BOSS', 'ASTRO', 'DRIFTER'].includes(t ?? ''))
    // zeta (favorite) first; ghost's department isn't rendered → shown here.
    expect(independents).toEqual(['ZETA', 'GHOST', 'OMEGA'])
  })
})
