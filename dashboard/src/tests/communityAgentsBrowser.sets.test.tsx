// The Browse page's department sets (components/CommunityAgentsBrowser):
// a card per set with its members and levels, the install button
// disabled when a member is not in the catalog, a template card's "Apps
// it brings" and "Part of the set" lines; the sets section follows the
// search and the installed filter.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
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
import { departmentAgentsTitle } from '@/lib/status/departmentSet'

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
    expect(screen.getByTestId('apps-of-yacht-crew').textContent).toBe('1 app: Crew board')
    expect(screen.getByTestId('sets-of-yacht-crew')).toHaveTextContent('Part of the Yacht office department.')
  })

  it('shows a department as a team on a raised card: how many agents, its text, the head over the team', () => {
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [{ ...office, summary: 'Runs a yacht office as one team.' }] }
    renderBrowser()
    expect(screen.getByRole('heading', { name: 'Departments' })).toBeTruthy()
    expect(screen.getByText('A department is several agents installed together as one team.')).toBeTruthy()
    const el = screen.getByTestId('set-card-yacht-office')
    expect(el.className).toContain('bg-surface-card')
    expect(el.className).toContain('shadow-elevation-1')
    const card = within(el)
    expect(card.getByTestId('panel-what').textContent).toBe('2 agents, installed together as one team.')
    expect(card.getByTestId('panel-text').textContent).toBe('Runs a yacht office as one team.')
    expect(card.getByTestId('panel-text').className).toContain('line-clamp-2')
    // No rows on how the department works inside (its library, its schedules, how work is handed on).
    expect(card.queryByTestId('panel-features')).toBeNull()
    expect(el).not.toHaveTextContent('A shared library')
    expect(card.getAllByRole('listitem').map((li) => li.getAttribute('data-testid')).filter(Boolean))
      .toEqual(['team-yacht-manager', 'team-yacht-crew'])
    expect(card.getByTestId('team-yacht-manager')).toHaveTextContent('Yacht ManagerHead')
    expect(card.getByRole('button', { name: 'Install the department' })).toBeTruthy()
  })

  it("shows a department's own highlights as an agent's, and a badge that says it is a department", () => {
    const rows = ['Owner reports, written for you', 'Invoices and budgets kept', 'Crew files and certificates',
      'Maintenance planned ahead', 'Stock and supplies tracked'].map((text, i) => (
      { icon: ['document', 'document', 'shield', 'calendar', 'app'][i], text }))
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [{ ...office, highlights: rows }] }
    renderBrowser()
    const card = within(screen.getByTestId('set-card-yacht-office'))
    expect(card.getByTestId('panel-badge').textContent).toBe('Department')
    expect(card.getAllByTestId('panel-features').length).toBe(1)
    expect(within(card.getByTestId('panel-features')).getAllByRole('listitem').map((li) => li.textContent))
      .toEqual(rows.slice(0, 4).map((r) => r.text))
    fireEvent.click(card.getByRole('button', { name: 'Show details' }))
    expect(card.getByTestId('panel-badge').textContent).toBe('Department')
    expect(within(card.getByTestId('panel-features')).getAllByRole('listitem')).toHaveLength(5)
    // How it is set up is the install dialog's alone.
    expect(card.queryByTestId('setup-facts')).toBeNull()
    // An agent is no department.
    expect(within(screen.getByTestId('agent-card-yacht-crew')).queryByTestId('panel-badge')).toBeNull()
  })

  it('names the agents above the team, on the closed card and once opened', () => {
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [office] }
    renderBrowser()
    const card = within(screen.getByTestId('set-card-yacht-office'))
    expect(card.getByTestId('panel-team-title').textContent).toBe('2 agents in this department')
    fireEvent.click(card.getByRole('button', { name: 'Show details' }))
    expect(card.getByTestId('panel-team-title').textContent).toBe('2 agents in this department')
    expect(card.queryByText('The team')).toBeNull()
    expect(departmentAgentsTitle(1)).toBe('1 agent in this department')
  })

  it("opens a department in full, and an agent of its team in place with a way back", () => {
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [office] }
    renderBrowser()
    const card = within(screen.getByTestId('set-card-yacht-office'))
    fireEvent.click(card.getByRole('button', { name: 'Show details' }))
    expect(card.getByTestId('panel-folded')).toBeTruthy()
    expect(card.queryByTestId('panel-features')).toBeNull()
    expect(card.getByTestId('apps-count').textContent).toBe('1 app')
    fireEvent.click(card.getByRole('button', { name: 'Yacht Crew' }))
    const panel = within(card.getByTestId('set-member-panel-yacht-crew'))
    expect(panel.getByTestId('panel-what').textContent).toBe('One agent, installed on its own.')
    expect(panel.getByText('Part of the Yacht office department.')).toBeTruthy()
    expect(panel.getByRole('button', { name: 'Install only this agent' })).toBeTruthy()
    fireEvent.click(panel.getByRole('button', { name: 'Back to Yacht office' }))
    expect(card.queryByTestId('set-member-panel-yacht-crew')).toBeNull()
    expect(card.getByTestId('panel-what').textContent).toBe('2 agents, installed together as one team.')
  })

  it('lists departments first, filters by kind, and opens a member agent\'s department from its card', () => {
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [office] }
    renderBrowser()
    const view = within(screen.getByTestId('store-view'))
    expect(view.getAllByRole('button').map((b) => [b.textContent, b.getAttribute('aria-pressed')]))
      .toEqual([['All', 'true'], ['Departments', 'false'], ['Agents', 'false']])
    const set = screen.getByTestId('set-card-yacht-office')
    const agent = screen.getByTestId('agent-card-yacht-crew')
    expect(set.compareDocumentPosition(agent) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Agents' }))
    expect(screen.queryByTestId('set-card-yacht-office')).toBeNull()
    fireEvent.click(screen.getByTestId('sets-of-yacht-crew'))
    // The link brings the department back into view and opens it.
    expect(within(screen.getByTestId('set-card-yacht-office')).getByRole('button', { name: 'Hide details' })).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Departments' }))
    expect(screen.queryByTestId('agent-card-yacht-crew')).toBeNull()
  })

  it("says what a card brings in plain words, and folds its tools and apps under Show details", () => {
    const named = { ...crew, has_apps: true, has_user_apps: true, has_checks: true, has_notifications: true, has_triggers: true,
                    required_mcps: [{ name: 'schedules-mcp', label: 'Schedules', min_version: '1.0.0' }, { name: 'navis-mcp' }] }
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [named] }
    h.sets.data = { departments: [] }
    renderBrowser()
    expect(screen.getByText(/^Ready-made agents and departments to install\./)).toBeTruthy()
    const stats = screen.getByText((_, el) => el?.tagName === 'SPAN' && el.textContent === '2 tools').parentElement!
    expect(stats.textContent).toBe('2 tools· a setup guide· scheduled tasks· triggers· reminders· shared apps'
      + '· an app for each member· checks of its work· notes it always reads')
    const card = within(screen.getByTestId('agent-card-yacht-crew'))
    fireEvent.click(card.getByRole('button', { name: 'Show details' }))
    // Folded by default, each line with its own toggle.
    expect(card.getByTestId('tools-count').textContent).toBe('2 tools')
    expect(card.queryByText('Schedules')).toBeNull()
    expect(card.getByTestId('apps-count').textContent).toBe('1 app: Crew board')
    expect(card.queryByTestId('consent-app-crew-board')).toBeNull()
    fireEvent.click(within(card.getByTestId('required-tools')).getByTestId('details-toggle'))
    expect(card.getByText('Schedules')).toBeTruthy()
    expect(card.getByText('navis-mcp')).toBeTruthy()
    fireEvent.click(within(card.getByTestId('template-apps')).getByTestId('details-toggle'))
    expect(card.getByTestId('consent-app-crew-board')).toHaveTextContent('Crew board is shared by everyone who uses this agent.')
    expect(card.getByTestId('consent-app-about-crew-board').textContent).toBe('Who is on board and their papers.')
    expect(screen.queryByText(/Bundled MCPs|≥/)).toBeNull()
  })

  it("names a closed card's apps in one line, and each app's line and picture only under Show details", () => {
    const two = { ...crew, apps: [
      { slug: 'crew-board', title: 'Crew board', visibility: 'agent', description: 'Who is on board and their papers.',
        picture_url: 'pictures/crew-board.png' },
      { slug: 'rota', title: 'Rota', visibility: 'user', description: 'Your own watches.', picture_url: '' },
    ] }
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [two] }
    h.sets.data = { departments: [] }
    renderBrowser()
    const card = within(screen.getByTestId('agent-card-yacht-crew'))
    expect(card.getByTestId('apps-of-yacht-crew').textContent).toBe('2 apps: Crew board and Rota')
    expect(card.queryByText('Who is on board and their papers.')).toBeNull()
    expect(card.queryByRole('img')).toBeNull()
    fireEvent.click(card.getByRole('button', { name: 'Show details' }))
    expect(card.queryByTestId('apps-of-yacht-crew')).toBeNull()
    fireEvent.click(within(card.getByTestId('template-apps')).getByTestId('details-toggle'))
    const about = card.getByTestId('consent-app-about-crew-board')
    expect(about).toHaveTextContent('Who is on board and their papers.')
    expect(about.querySelector('img')?.getAttribute('src')).toBe('/v1/community/agents/yacht-crew/pictures/crew-board.png')
    expect(card.getByTestId('consent-app-about-rota').querySelector('img')).toBeNull()
  })

  it('shows a card by its summary and first highlights, and the rest once opened', () => {
    const highlights = ['Shorts', 'Full videos', 'AI generation', 'AI voice', 'A calendar'].map((text, i) => (
      { icon: ['shorts', 'video', 'sparkle', 'mic', 'calendar'][i], text }))
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '',
                      agents: [{ ...crew, summary: "Runs your company's whole content workflow.", highlights }] }
    h.sets.data = { departments: [] }
    renderBrowser()
    const panel = screen.getByTestId('template-panel')
    expect(within(panel).getByTestId('panel-text').textContent).toBe("Runs your company's whole content workflow.")
    expect(panel).not.toHaveTextContent('The crew files.')
    expect(within(screen.getByTestId('panel-features')).getAllByRole('listitem').map(li => li.textContent))
      .toEqual(['Shorts', 'Full videos', 'AI generation', 'AI voice'])
    // Highlights say what it does: the tags step aside.
    expect(screen.queryByText('yacht')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }))
    expect(within(screen.getByTestId('panel-features')).getAllByRole('listitem')).toHaveLength(5)
    expect(screen.getByTestId('panel-description')).toHaveTextContent('The crew files.')
  })

  it('shows the description and the tags of a card with no summary, as before', () => {
    h.agents.data = { registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: '', agents: [crew] }
    h.sets.data = { departments: [] }
    renderBrowser()
    expect(screen.getByTestId('panel-text').textContent).toBe('The crew files.')
    expect(screen.queryByTestId('panel-features')).toBeNull()
    expect(screen.getByText('yacht')).toBeTruthy()
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
    expect(broken).toHaveTextContent('An agent of this department is not in the catalog.')
    expect(screen.getByTestId('set-card-faulty')).toHaveTextContent('This department has errors in the catalog.')
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
