// The set install dialog (components/departments/DepartmentInstallModal):
// the department's name with the library sentence, each member's plan
// (new, used, or new under another name), the "install a new one instead"
// choice, the consent checkbox, the install disabled on a blocked MCP or a
// bad name, the job's rows and lines until it ends, the open button.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const h = vi.hoisted(() => ({
  preview: { data: undefined as any, isLoading: false, isError: false },
  previewArgs: [] as unknown[][],
  install: vi.fn(),
  status: { data: undefined as any },
}))

vi.mock('@/api/communityAgents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/communityAgents')>()),
  useSetPreview: (...args: unknown[]) => { h.previewArgs.push(args); return h.preview },
  useInstallDepartmentSet: () => ({ mutate: h.install, isPending: false }),
  useSetInstallStatus: (jobId: string | null) => (jobId ? h.status : { data: undefined }),
}))

import DepartmentInstallModal from '@/components/departments/DepartmentInstallModal'

const office = {
  slug: 'yacht-office', name: 'Yacht office', description: 'The five agents of a yacht office.',
} as any

const member = (over: Record<string, unknown>) => ({
  template: 'yacht-crew', level: 'Team', display_name: 'Yacht Crew', version: '0.2.2', platform_compat_ok: true,
  in_catalog: true, placed: [], standalone: [], action: 'new', slug: 'yacht-crew', apps: [], checks: [], required_mcps: [],
  blocked_mcps: [], ...over,
})

function preview(over: Record<string, unknown> = {}) {
  return {
    set_slug: 'yacht-office', department_name: 'Lady G', name_problem: null, mode: 'down_across', reach: 'adjacent',
    levels: ['Head', 'Team'],
    members: [member({ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager',
                       apps: [{ slug: 'owner-report', sig: 's' }] }),
              member({ action: 'adopt', standalone: ['yacht-crew'] }),
              member({ template: 'yacht-finance', display_name: 'Yacht Finance', slug: 'yacht-finance-2', placed: ['yacht-finance'] })],
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
    h.previewArgs.length = 0
    h.status.data = undefined
  })

  it('shows each member plan, the library and the schedules, and installs with the name and the consent', () => {
    h.preview.data = preview()
    renderModal()
    expect(screen.getByTestId('set-member-yacht-manager')).toHaveTextContent('New')
    expect(screen.getByTestId('set-member-yacht-crew')).toHaveTextContent('Uses Yacht Crew (yacht-crew)')
    expect(screen.getByTestId('set-member-yacht-finance')).toHaveTextContent('New, named yacht-finance-2')
    expect(screen.getByTestId('set-member-yacht-finance')).toHaveTextContent('Already in another department: yacht-finance.')
    expect(screen.getByText(/The library: the head/)).toHaveTextContent('vessel folder, shared with the others read only.')
    expect(screen.getByText('1 briefing schedules, every one paused until you start it.')).toBeInTheDocument()
    expect(screen.getByText(/Delegation runs down and across/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Department name'), { target: { value: 'Lady G' } })
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    expect(h.install).toHaveBeenCalledWith({ slug: 'yacht-office', department_name: 'Lady G', consent: true, new: [] }, expect.anything())
  })

  it('asks for a new agent instead of the installed one, and unticks the consent', () => {
    h.preview.data = preview()
    renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Install a new one instead' }))
    expect(h.previewArgs[h.previewArgs.length - 1]?.[2]).toEqual(['yacht-crew'])
    fireEvent.click(screen.getByTestId('set-consent-checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    expect(h.install).toHaveBeenCalledWith(expect.objectContaining({ consent: false, new: ['yacht-crew'] }), expect.anything())
  })

  it('refuses a blocked member, a bad name and a running job', () => {
    h.preview.data = preview({ members: [member({ blocked_mcps: ['navis-mcp'] })] })
    const { unmount } = renderModal()
    expect(screen.getByText('Not installed and not in the community list: navis-mcp.')).toBeInTheDocument()
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

  it('follows the job to its end and offers to open the department', async () => {
    h.preview.data = preview()
    h.install.mockImplementation((_body: unknown, opts: { onSuccess: (r: { job_id: string }) => void }) => opts.onSuccess({ job_id: 'setjob-1' }))
    h.status.data = {
      id: 'setjob-1', set_slug: 'yacht-office', set_version: '0.1.0', department_name: 'Lady G', department_id: '',
      started_by: 'u1', status: 'running', lines: ['Installed Yacht Manager as yacht-manager.'],
      members: [{ template: 'yacht-manager', level: 'Head', display_name: 'Yacht Manager', slug: 'yacht-manager', action: 'new', status: 'installed', detail: '' },
                { template: 'yacht-crew', level: 'Team', display_name: 'Yacht Crew', slug: 'yacht-crew', action: 'new', status: 'installing', detail: '' }],
      started_at: '', ended_at: null,
    }
    const { rerender } = renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Install the department' }))
    await screen.findByRole('heading', { name: 'Installing Yacht office' })
    expect(screen.getByTestId('set-install-members')).toHaveTextContent('Yacht Manager (Head)Installed')
    expect(screen.getByTestId('set-install-members')).toHaveTextContent('Yacht Crew (Team)Installing')
    expect(screen.queryByRole('button', { name: 'Open the department' })).toBeNull()
    h.status.data = { ...h.status.data, status: 'done', department_id: 'dept-1',
                      lines: [...h.status.data.lines, 'The department Lady G holds Yacht Manager and Yacht Crew.'],
                      members: h.status.data.members.map((m: any) => ({ ...m, status: 'installed' })) }
    rerender(<DepartmentInstallModal open set={office} onClose={() => {}} />)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Lady G is installed' })).toBeInTheDocument())
    expect(screen.getByTestId('set-install-lines')).toHaveTextContent('The department Lady G holds Yacht Manager and Yacht Crew.')
    expect(screen.getByRole('button', { name: 'Open the department' })).toBeInTheDocument()
  })
})
