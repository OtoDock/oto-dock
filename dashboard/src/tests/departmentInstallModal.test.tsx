// The set install dialog (components/departments/DepartmentInstallModal):
// the department's name with the library sentence, each member's plan
// (new, used, or new under another name), the "install a new one instead"
// choice, each new member's tools in the agent dialog's block, the consent
// checkbox, the install disabled on a blocked MCP or a bad name, the job's rows and lines until it ends, the open button.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const h = vi.hoisted(() => ({
  preview: { data: undefined as any, isLoading: false, isError: false, isPlaceholderData: false, error: null as unknown },
  /** When set, the plan's answer for the hook's arguments (slug, name, choices). */
  planFor: null as null | ((args: unknown[]) => unknown),
  previewArgs: [] as unknown[][],
  install: vi.fn(),
  status: { data: undefined as any },
}))

vi.mock('@/api/communityAgents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/communityAgents')>()),
  useSetPreview: (...args: unknown[]) => { h.previewArgs.push(args); return h.planFor ? h.planFor(args) : h.preview },
  useInstallDepartmentSet: () => ({ mutate: h.install, isPending: false }),
  useSetInstallStatus: (jobId: string | null) => (jobId ? h.status : { data: undefined }),
}))

import DepartmentInstallModal from '@/components/departments/DepartmentInstallModal'
import { ApiError } from '@/lib/http/apiJson'

const office = {
  slug: 'yacht-office', name: 'Yacht office', description: 'The five agents of a yacht office.',
} as any

const member = (over: Record<string, unknown>) => ({
  template: 'yacht-crew', level: 'Team', display_name: 'Yacht Crew', version: '0.2.2', platform_compat_ok: true,
  in_catalog: true, kept: false, options: [], choice: 'new', action: 'new', slug: 'yacht-crew', apps: [], checks: [],
  required_mcps: [], blocked_mcps: [], ...over,
})

/** An agent the person may use for Yacht Crew: on its own, and in a department made by hand. */
const own = { slug: 'yacht-crew', display_name: 'Yacht Crew', department: '' }
const placedOne = { slug: 'crew-b', display_name: 'Crew B', department: 'Marketing' }

/** A tool row of a member's preview, ready unless told otherwise. */
const tool = (over: Record<string, unknown>) => ({
  name: 'schedules-mcp', label: (over.name as string) ?? 'schedules-mcp', installed: true, request_type: null,
  blocked: false, needs_request: false, core: false, reason: '', ...over,
})

function preview(over: Record<string, unknown> = {}) {
  return {
    set_slug: 'yacht-office', department_name: 'Lady G', name_problem: null, mode: 'down_across', reach: 'adjacent',
    levels: ['Head', 'Team'],
    members: [member({ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager',
                       apps: [{ slug: 'owner-report', title: 'Owner report', visibility: 'agent', sig: 's', owner_approval: false,
                                blueprint_tasks: [], row: { actions: [], files: { read: [], write: [] } } }] }),
              member({ action: 'adopt', choice: 'yacht-crew', options: [own] }),
              member({ template: 'yacht-finance', display_name: 'Yacht Finance', slug: 'yacht-finance-2' })],
    library: { source: 'yacht-manager', subdir: 'vessel', writable: false },
    schedules: [{ member: 'yacht-manager', task: 'weekly-bulletin' }],
    consent_scope: 'everyone', existing: [], running_job: null, ...over,
  }
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>
}

function renderModal() {
  return render(<DepartmentInstallModal open set={office} onClose={() => {}} />, { wrapper })
}

describe('DepartmentInstallModal', () => {
  beforeEach(() => {
    h.install.mockReset()
    h.preview.data = undefined
    h.preview.isLoading = false
    h.preview.isError = false
    h.preview.isPlaceholderData = false
    h.preview.error = null
    h.planFor = null
    h.previewArgs.length = 0
    h.status.data = undefined
  })

  it('shows the store panel: what it is, its text, the team, then the name and the folded lines', () => {
    h.preview.data = preview()
    render(<DepartmentInstallModal open set={office} onClose={() => {}} />, { wrapper })
    const panel = screen.getByTestId('template-panel')
    expect(within(panel).getByTestId('panel-what').textContent).toBe('3 agents, installed together as one team.')
    expect(within(panel).getByTestId('panel-text').textContent).toBe('The five agents of a yacht office.')
    // No rows on how the department works inside: its library, its schedules, how work is handed on.
    expect(screen.queryByTestId('panel-features')).toBeNull()
    expect(panel).not.toHaveTextContent(/shared library|on a schedule|delegates to/)
    // The team in its small tree, the head on top; a choice only where the person has an agent to use.
    const rows = within(screen.getByTestId('department-team')).getAllByRole('listitem').map((li) => li.getAttribute('data-testid'))
    expect(rows[0]).toBe('team-yacht-manager')
    expect(screen.queryByTestId('set-member-yacht-manager')).toBeNull()
    expect(screen.queryByTestId('set-member-yacht-finance')).toBeNull()
    expect(screen.getByTestId('set-new-ids').textContent).toBe('New ID: yacht-finance-2.')
    expect((screen.getByLabelText('The agent for Yacht Crew') as HTMLSelectElement).value).toBe('yacht-crew')
    // The name sits between the panel's own parts and the folded lines.
    const name = screen.getByLabelText('Department name')
    expect(screen.getByTestId('department-team').compareDocumentPosition(name) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(name.compareDocumentPosition(screen.getByTestId('panel-folded')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.change(name, { target: { value: 'Lady G' } })
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    expect(h.install).toHaveBeenCalledWith(
      { slug: 'yacht-office', department_name: 'Lady G', consent: true, use: { 'yacht-crew': 'yacht-crew' } }, expect.anything())
  })

  it('shows no choice for a member the person has no agent for', () => {
    h.preview.data = preview({ members: [
      member({ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager' }),
      member({}),
    ] })
    renderModal()
    expect(screen.queryByTestId('set-notes')).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it("offers each agent the person may use by its name and where it is, as the plan preselects", () => {
    // Two agents: nothing is guessed.
    h.preview.data = preview({ members: [
      member({ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager' }),
      member({ options: [own, placedOne], slug: 'yacht-crew-2' }),
    ] })
    renderModal()
    const select = screen.getByLabelText('The agent for Yacht Crew') as HTMLSelectElement
    expect(within(screen.getByTestId('set-member-yacht-crew')).getByText('Yacht Crew')).toBeTruthy()
    expect([...select.options].map((o) => o.textContent)).toEqual([
      'Install a new one', 'Use Yacht Crew (yacht-crew)', 'Use Crew B (crew-b), from Marketing'])
    expect(select.value).toBe('new')
    expect(screen.getByTestId('set-new-ids').textContent).toBe('New ID: yacht-crew-2.')
  })

  it('preselects one agent on its own, and a new one for one that sits in a department', () => {
    h.preview.data = preview({ members: [member({ options: [placedOne] })] })
    const { unmount } = renderModal()
    expect((screen.getByLabelText('The agent for Yacht Crew') as HTMLSelectElement).value).toBe('new')
    unmount()
    h.preview.data = preview({ members: [member({ action: 'adopt', options: [own], choice: 'yacht-crew' })] })
    renderModal()
    expect((screen.getByLabelText('The agent for Yacht Crew') as HTMLSelectElement).value).toBe('yacht-crew')
  })

  it("plans again on a choice and installs what the dialog shows", () => {
    h.preview.data = preview({ members: [member({ options: [own, placedOne], slug: 'yacht-crew-2' })] })
    renderModal()
    fireEvent.change(screen.getByLabelText('The agent for Yacht Crew'), { target: { value: 'crew-b' } })
    expect(h.previewArgs[h.previewArgs.length - 1]?.[2]).toEqual({ 'yacht-crew': 'crew-b' })
    // The plan answers the move; the install sends the choice it shows.
    h.preview.data = preview({ members: [member({ options: [own, placedOne], action: 'move', slug: 'crew-b', choice: 'crew-b',
                                                  moves_from: { id: 'd-1', name: 'Marketing' } })] })
    fireEvent.change(screen.getByLabelText('Department name'), { target: { value: 'Lady G' } })
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    expect(h.install).toHaveBeenCalledWith(expect.objectContaining({ use: { 'yacht-crew': 'crew-b' } }), expect.anything())
  })

  it('waits for the plan of the choice it shows, then installs that choice', () => {
    h.preview.data = preview({ members: [member({ options: [own, placedOne], slug: 'yacht-crew-2' })] })
    const view = renderModal()
    // The plan for the new choice is still on its way: the rows are the earlier plan's.
    h.preview.isPlaceholderData = true
    fireEvent.change(screen.getByLabelText('The agent for Yacht Crew'), { target: { value: 'crew-b' } })
    expect(screen.getByLabelText('The agent for Yacht Crew')).toHaveValue('crew-b')
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    expect(screen.getByRole('button', { name: 'Install the department' })).toBeDisabled()
    expect(h.install).not.toHaveBeenCalled()
    // The plan arrives.
    h.preview.isPlaceholderData = false
    h.preview.data = preview({ members: [member({ options: [own, placedOne], action: 'move', slug: 'crew-b', choice: 'crew-b',
                                                  moves_from: { id: 'd-1', name: 'Marketing' } })] })
    view.rerender(<DepartmentInstallModal open set={office} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    expect(h.install).toHaveBeenCalledWith(
      expect.objectContaining({ use: { 'yacht-crew': (screen.getByLabelText('The agent for Yacht Crew') as HTMLSelectElement).value } }),
      expect.anything())
    expect(h.install.mock.calls[0][0].use).toEqual({ 'yacht-crew': 'crew-b' })
  })

  it("drops a choice the plan refuses and shows the plan's own again", () => {
    const own0 = preview({ members: [member({ options: [own, placedOne], slug: 'yacht-crew-2' })] })
    const chose = preview({ members: [member({ options: [own, placedOne], action: 'move', slug: 'crew-b', choice: 'crew-b' })] })
    const answer = (data: unknown, error: unknown = null) =>
      ({ data, isLoading: false, isError: Boolean(error), isPlaceholderData: false, error })
    // Under the name "Lady H" the chosen agent can no longer be used.
    h.planFor = ([, name, use]) => {
      if (Object.keys(use as object).length === 0) return answer(own0)
      return name === 'Lady H' ? answer(undefined, new ApiError(400, 'Crew B changed. Choose again.', null, null)) : answer(chose)
    }
    renderModal()
    fireEvent.change(screen.getByLabelText('The agent for Yacht Crew'), { target: { value: 'crew-b' } })
    // A new name keeps the choice.
    fireEvent.change(screen.getByLabelText('Department name'), { target: { value: 'Lady G' } })
    expect(h.previewArgs[h.previewArgs.length - 1]?.[2]).toEqual({ 'yacht-crew': 'crew-b' })
    fireEvent.change(screen.getByLabelText('Department name'), { target: { value: 'Lady H' } })
    expect(h.previewArgs[h.previewArgs.length - 1]?.[2]).toEqual({})
    expect(screen.queryByText(/could not be read/)).toBeNull()
    expect(screen.getByLabelText('The agent for Yacht Crew')).toHaveValue('new')
    expect(screen.getByLabelText('Department name')).toHaveValue('Lady H')
    expect(screen.getByRole('button', { name: 'Install the department' })).toBeEnabled()
  })

  it('still says the catalog could not be read on a failure that is not a refused choice', () => {
    const own0 = preview({ members: [member({ options: [own, placedOne], slug: 'yacht-crew-2' })] })
    h.planFor = ([, , use]) => (Object.keys(use as object).length === 0
      ? { data: own0, isLoading: false, isError: false, isPlaceholderData: false, error: null }
      : { data: undefined, isLoading: false, isError: true, isPlaceholderData: false,
          error: new ApiError(500, 'Internal Server Error', null, null) })
    renderModal()
    fireEvent.change(screen.getByLabelText('The agent for Yacht Crew'), { target: { value: 'crew-b' } })
    expect(screen.getByText('The catalog could not be read. Try again in a minute.')).toBeInTheDocument()
    expect(h.previewArgs[h.previewArgs.length - 1]?.[2]).toEqual({ 'yacht-crew': 'crew-b' })
  })

  it('folds the new IDs into one line', () => {
    h.preview.data = preview({ members: ['yacht-manager', 'yacht-finance', 'yacht-crew'].map((t) =>
      member({ template: t, display_name: t, slug: `${t}-2` })) })
    renderModal()
    expect(screen.getByTestId('set-new-ids').textContent).toBe('New IDs: yacht-manager-2, yacht-finance-2 and yacht-crew-2.')
  })

  it("shows the department's own highlights and its badge in the dialog's header", () => {
    h.preview.data = preview()
    const rows = [{ icon: 'document', text: 'Owner reports, written for you' }, { icon: 'calendar', text: 'Maintenance planned ahead' }]
    render(<DepartmentInstallModal open set={{ ...office, highlights: rows }} onClose={() => {}} />, { wrapper })
    const panel = within(screen.getByTestId('template-panel'))
    expect(panel.getByTestId('panel-badge').textContent).toBe('Department')
    expect(panel.getByTestId('panel-team-title').textContent).toBe('3 agents in this department')
    expect(panel.queryByText('The team')).toBeNull()
    expect(within(panel.getByTestId('panel-features')).getAllByRole('listitem').map((li) => li.textContent))
      .toEqual(['Owner reports, written for you', 'Maintenance planned ahead'])
  })

  it('folds how the department is set up into one line, closed until asked', () => {
    h.preview.data = preview({ schedules: [{ member: 'yacht-manager', task: 'a' }, { member: 'yacht-manager', task: 'b' }] })
    renderModal()
    const line = screen.getByTestId('setup-facts')
    // After the folded tools and apps, in the same box.
    expect(screen.getByTestId('template-apps').compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(screen.getByTestId('panel-folded')).getByTestId('setup-facts')).toBe(line)
    expect(within(line).getByText('How it is set up')).toBeTruthy()
    expect(line).not.toHaveTextContent('vessel')
    fireEvent.click(within(line).getByTestId('details-toggle'))
    expect(within(line).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      "The team can read the head's vessel folder.",
      '2 briefings stay paused until you start them.',
      'Each level hands work down and across.',
    ])
  })

  it('unticks the consent', () => {
    h.preview.data = preview()
    renderModal()
    fireEvent.click(screen.getByTestId('set-consent-checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    expect(h.install).toHaveBeenCalledWith(expect.objectContaining({ consent: false }), expect.anything())
  })

  it('refuses a blocked member, a bad name and a running job', () => {
    h.preview.data = preview({ members: [member({ blocked_mcps: ['navis-mcp'], required_mcps: [tool({ name: 'navis-mcp', installed: false, blocked: true, needs_request: true })] })] })
    const { unmount } = renderModal()
    expect(screen.getByTestId('tool-navis-mcp')).toHaveTextContent('navis-mcp It is not in the community list, so it cannot be added.')
    expect(screen.getByRole('button', { name: 'Install the department' })).toBeDisabled()
    unmount()
    h.preview.data = preview({ name_problem: 'Library name may not contain / (it names the library file).' })
    const second = renderModal()
    expect(screen.getByText(/may not contain/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Install the department' })).toBeDisabled()
    second.unmount()
    h.preview.data = preview({ running_job: 'setjob-1' })
    renderModal()
    expect(screen.getByText('This set is being installed already. Wait for it to finish.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Install the department' })).toBeDisabled()
  })

  it("names the department's tools once, in one line, at what the neediest agent needs", () => {
    const ready = tool({ name: 'github-mcp', label: 'GitHub' })
    const waits = tool({ name: 'github-mcp', label: 'GitHub', request_type: 'access', needs_request: true })
    h.preview.data = preview({ members: [
      member({ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager',
               required_mcps: [tool({ name: 'schedules-mcp', label: 'Schedules', core: true }), ready] }),
      member({ template: 'yacht-finance', display_name: 'Yacht Finance', slug: 'yacht-finance', required_mcps: [waits] }),
      member({ action: 'adopt', choice: 'yacht-crew', options: [own], required_mcps: [tool({ name: 'crew-mcp', label: 'Crew' })] }),
    ] })
    renderModal()
    expect(screen.getAllByTestId('required-tools')).toHaveLength(1)
    expect(screen.getByTestId('tools-count').textContent).toBe("2 tools: 1 ready, 1 needs an admin's approval")
    expect(screen.getByTestId('tool-github-mcp')).toHaveTextContent("GitHub It waits for an admin's approval.")
    // A used agent installs nothing, so its tools are not counted.
    expect(screen.getByTestId('required-tools')).not.toHaveTextContent('Crew')
  })

  it("counts every new agent's apps and checks in one line, the program in view, Details under each agent's name", () => {
    const check = { name: 'read-report', description: 'Reads each owner report as an owner would.', mandatory: true,
                    applies: ['chats'], script: '', sig: 's', words: 'a judge run per turn' }
    const app = (slug: string, title: string, server: boolean) => ({
      slug, title, visibility: 'agent', sig: 's', owner_approval: false, blueprint_tasks: [], adds_server: server,
      row: { actions: [], files: { read: [], write: [] }, manifest_empty: true, has_server: server } })
    h.preview.data = preview({ members: [
      member({ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager',
               apps: [app('owner-report', 'Owner report', true)], checks: [check] }),
      member({ template: 'yacht-finance', display_name: 'Yacht Finance', slug: 'yacht-finance',
               apps: [app('finance-board', 'Finance board', false)], checks: [{ ...check, name: 'filed' }] }),
      member({ action: 'adopt', choice: 'yacht-crew', options: [own], apps: [app('crew-board', 'Crew board', true)] }),
    ] })
    renderModal()
    expect(screen.getAllByTestId('template-apps')).toHaveLength(1)
    expect(screen.getByTestId('apps-count').textContent).toBe('2 apps and 2 checks')
    expect(screen.getByTestId('apps-server-count').textContent).toBe('One of them comes with its own program on your OtoDock.')
    expect(screen.queryByTestId('consent-app-owner-report')).toBeNull()
    // One checkbox for the department, in the agent install dialog's words.
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
    expect(screen.getByText('Approve for every member.')).toBeInTheDocument()
    fireEvent.click(within(screen.getByTestId('template-apps')).getByTestId('details-toggle'))
    const head = within(screen.getByTestId('template-apps-group-yacht-manager'))
    expect(head.getByText('Yacht Manager')).toBeInTheDocument()
    expect(head.getByTestId('consent-app-owner-report')).toHaveTextContent('Owner report is shared by everyone who uses this agent, with its own program on your OtoDock.')
    expect(head.getByTestId('consent-check-read-report')).toHaveTextContent('Reads each owner report as an owner would.')
    expect(screen.getByTestId('template-apps-group-yacht-finance')).toHaveTextContent('Finance board is shared by everyone who uses this agent.')
    expect(screen.queryByTestId('template-apps-group-yacht-crew')).toBeNull()
  })

  it('counts one app and one check in the singular', () => {
    const check = { name: 'read-report', description: 'Reads each owner report.', mandatory: true,
                    applies: ['chats'], script: '', sig: 's', words: 'a judge run per turn' }
    h.preview.data = preview({ members: [member({
      template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager', checks: [check],
      apps: [{ slug: 'owner-report', title: 'Owner report', visibility: 'agent', sig: 's', owner_approval: false,
               blueprint_tasks: [], row: { actions: [], files: { read: [], write: [] } } }] })] })
    renderModal()
    expect(screen.getByTestId('apps-count').textContent).toBe('1 app and 1 check')
  })

  it('says the approval is for you and the agents to a non-admin', () => {
    h.preview.data = preview({ consent_scope: 'own' })
    renderModal()
    expect(screen.getByText('Approve for you and the agents. Each member approves their own copy.')).toBeInTheDocument()
  })

  it('follows the job to its end and offers to open the department', async () => {
    h.preview.data = preview()
    h.install.mockImplementation((_body: unknown, opts: { onSuccess: (r: { job_id: string }) => void }) => opts.onSuccess({ job_id: 'setjob-1' }))
    h.status.data = {
      id: 'setjob-1', set_slug: 'yacht-office', set_version: '0.1.0', department_name: 'Lady G', department_id: '',
      started_by: 'u1', status: 'running', lines: ['Installed Yacht Manager as yacht-manager.'], not_done: [],
      members: [{ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager', action: 'new', status: 'installed', detail: '' },
                { template: 'yacht-crew', level: 'Team', display_name: 'Yacht Crew', slug: 'yacht-crew', action: 'new', status: 'installing', detail: '' }],
      started_at: '', ended_at: null,
    }
    const { rerender } = renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    await screen.findByRole('heading', { name: 'Installing Yacht office' })
    expect(screen.getByTestId('set-install-members')).toHaveTextContent('Yacht Manager (Head)Installed')
    expect(screen.getByTestId('set-install-members')).toHaveTextContent('Yacht Crew (Team)Installing')
    expect(screen.queryByText('Installed Yacht Manager as yacht-manager.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Open the department' })).toBeNull()
    h.status.data = { ...h.status.data, status: 'done', department_id: 'dept-1',
                      lines: [...h.status.data.lines, 'The department Lady G holds Yacht Manager and Yacht Crew.'],
                      members: h.status.data.members.map((m: any) => ({ ...m, status: 'installed' })) }
    rerender(<DepartmentInstallModal open set={office} onClose={() => {}} />)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Lady G is installed' })).toBeInTheDocument())
    // A clean install: the title and the rows, every line folded under one closed Details.
    expect(screen.queryByTestId('set-install-lines')).toBeNull()
    expect(screen.queryByTestId('set-install-not-done')).toBeNull()
    fireEvent.click(within(screen.getByTestId('set-install-details')).getByTestId('details-toggle'))
    expect(screen.getByTestId('set-install-lines')).toHaveTextContent('The department Lady G holds Yacht Manager and Yacht Crew.')
    expect(screen.getByRole('button', { name: 'Open the department' })).toBeInTheDocument()
  })

  it("says Moved for a moved agent and keeps a line that says something was not done above Details", async () => {
    h.preview.data = preview()
    h.install.mockImplementation((_body: unknown, opts: { onSuccess: (r: { job_id: string }) => void }) => opts.onSuccess({ job_id: 'setjob-2' }))
    h.status.data = {
      id: 'setjob-2', set_slug: 'yacht-office', set_version: '0.1.0', department_name: 'Lady G', department_id: 'dept-1',
      started_by: 'u1', status: 'done',
      lines: ['Yacht Finance moved here from Walk Office.', 'These schedules were not found: Weekly (Yacht Crew).',
              'The department Lady G holds Yacht Manager, Yacht Finance and Yacht Crew.'],
      not_done: [1],
      members: [{ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager', action: 'new', status: 'installed', detail: '' },
                { template: 'yacht-finance', level: 'Team', display_name: 'Yacht Finance', slug: 'fin-b', action: 'move', status: 'adopted', detail: '' },
                { template: 'yacht-crew', level: 'Team', display_name: 'Yacht Crew', slug: 'yacht-crew', action: 'adopt', status: 'adopted', detail: '' }],
      started_at: '', ended_at: '',
    }
    renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    await screen.findByRole('heading', { name: 'Lady G is installed' })
    const rows = screen.getByTestId('set-install-members')
    expect(rows).toHaveTextContent('Yacht Finance (Team)Moved')
    expect(rows).toHaveTextContent('Yacht Crew (Team)Used')
    expect(screen.getByTestId('set-install-not-done').textContent).toBe('These schedules were not found: Weekly (Yacht Crew).')
    expect(screen.queryByText('Yacht Finance moved here from Walk Office.')).toBeNull()
    fireEvent.click(within(screen.getByTestId('set-install-details')).getByTestId('details-toggle'))
    expect(screen.getByTestId('set-install-lines')).toHaveTextContent('Yacht Finance moved here from Walk Office.')
    expect(screen.getByTestId('set-install-lines')).not.toHaveTextContent('were not found')
  })
})
