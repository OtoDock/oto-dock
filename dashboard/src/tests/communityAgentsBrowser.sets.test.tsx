// The Browse page's department sets (components/CommunityAgentsBrowser):
// a card per set with its members and levels, the install button
// disabled when a member is not in the catalog, a template card's "Apps
// it brings" and "Part of the set" lines; the sets section follows the
// search and the installed filter.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({
  agents: { data: undefined as any, isLoading: false, isError: false },
  sets: { data: undefined as any, isLoading: false, isError: false },
}))

vi.mock('@/api/agents', () => ({ useCreateAgent: () => ({ mutate: vi.fn(), isPending: false }) }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { role: 'admin', sub: 'u1', agent_roles: {} } }) }))
vi.mock('@/api/communityAgents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/communityAgents')>()),
  useCommunityAgents: () => h.agents,
  useCommunitySets: () => h.sets,
  useInstallCommunityAgent: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useInstallPreview: () => ({ data: undefined, isLoading: false }),
  useSetPreview: () => ({ data: undefined, isLoading: true, isError: false }),
  useInstallDepartmentSet: () => ({ mutate: vi.fn(), isPending: false }),
  useSetInstallStatus: () => ({ data: undefined }),
}))

import CommunityAgentsBrowser from '@/components/CommunityAgentsBrowser'

const crew = {
  slug: 'yacht-crew', display_name: 'Yacht Crew', description: 'The crew files.', long_description_url: '', color: '#1E3A8A',
  version: '0.2.2', category: 'productivity', tags: ['yacht'], author: 'OtoDock', author_url: null, license: 'Apache-2.0',
  icon_url: null, readme_url: '', manifest_url: '', required_mcps: [], has_triggers: false, has_tasks: true,
  has_notifications: false, has_setup: true, has_context: true, platform_min_version: '1.9.0', deprecated: false,
  deprecation_note: null, installed_as: [], installed: [],
  apps: [{ slug: 'crew-board', title: 'Crew board', visibility: 'agent', description: 'Who is on board and their papers.', picture_url: '' }],
}
const office = {
  slug: 'yacht-office', name: 'Yacht office', description: 'The five agents of a yacht office.', version: '0.1.0', color: '#1E3A8A',
  category: 'productivity', tags: ['yacht'], author: 'OtoDock', author_url: null, platform_min_version: '1.9.1',
  mode: 'down_across', reach: 'adjacent', levels: ['Head', 'Team'],
  members: [{ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', color: '', in_catalog: true },
            { template: 'yacht-crew', level: 'Team', display_name: 'Yacht Crew', color: '', in_catalog: true }],
  library: { source: 'yacht-manager', subdir: 'vessel' }, schedules: [], readme_url: '', deprecated: false,
  problems: [], missing_members: [], platform_compat_ok: true, installed: [],
}

function renderBrowser(section: 'agents' | 'sets' = 'agents') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CommunityAgentsBrowser open onClose={() => {}} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('the Browse page with department sets', () => {
  it('shows a set card with its members, and a template card with its apps and its set', () => {
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [office] }
    renderBrowser()
    const card = screen.getByTestId('set-card-yacht-office')
    expect(card).toHaveTextContent('Yacht office')
    expect(card).toHaveTextContent('Yacht Manager')
    expect(card).toHaveTextContent('Head')
    expect(screen.getByRole('button', { name: 'Install the department' })).not.toBeDisabled()
    expect(screen.getByTestId('apps-of-yacht-crew')).toHaveTextContent('Crew board Who is on board and their papers.')
    expect(screen.getByTestId('sets-of-yacht-crew')).toHaveTextContent('Part of the Yacht office department set.')
  })

  it('disables the install of a set whose member is not in the catalog, and marks an installed one', () => {
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [
      { ...office, slug: 'broken', name: 'Broken', missing_members: ['ghost'], installed: [] },
      { ...office, slug: 'faulty', name: 'Faulty', problems: ['department.json: levels must name 1 to 8 levels'], installed: [] },
      { ...office, installed: [{ id: 'dept-1', name: 'Lady G', version: '0.1.0', members: ['yacht-crew'] }] },
    ] }
    renderBrowser()
    const broken = screen.getByTestId('set-card-broken')
    expect(broken).toHaveTextContent('A template of this set is not in the catalog.')
    expect(screen.getByTestId('set-card-faulty')).toHaveTextContent('This set has errors in the catalog.')
    const buttons = screen.getAllByRole('button', { name: 'Install the department' })
    expect(buttons[0]).toBeDisabled()
    expect(buttons[1]).toBeDisabled()
    expect(buttons[2]).not.toBeDisabled()
    expect(screen.getByTestId('set-card-yacht-office')).toHaveTextContent('Installed as: Lady G')
  })

  it('opens the set dialog from the card and filters the sets with the search', () => {
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [office] }
    renderBrowser('sets')
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    expect(screen.getByRole('heading', { name: 'Install the Yacht office department' })).toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText(/Search by name/), { target: { value: 'zebra' } })
    expect(screen.queryByTestId('community-sets')).toBeNull()
  })
})
