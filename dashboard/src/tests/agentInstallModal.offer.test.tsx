// The install dialog's offer (components/AgentInstallModal): when the
// template belongs to a set whose other members stand alone, the dialog
// asks whether to create the department and add them all; ticked, the
// install carries the set and the name; unticked, it carries nothing; a
// preview with no offer shows nothing.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  installMock: vi.fn(),
  preview: { data: undefined as any, isLoading: false },
}))

vi.mock('@/api/agents', () => ({ useCreateAgent: () => ({ mutate: vi.fn(), isPending: false }) }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { role: 'admin', sub: 'u1', agent_roles: {} } }) }))
vi.mock('@/api/communityAgents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/communityAgents')>()),
  useInstallCommunityAgent: () => ({ mutateAsync: h.installMock, isPending: false }),
  useInstallPreview: () => h.preview,
}))

import AgentInstallModal from '@/components/AgentInstallModal'

const template = {
  slug: 'yacht-crew', display_name: 'Yacht Crew', version: '0.2.2', author: 'OtoDock',
  description: 'The crew files.', color: '#1E3A8A',
} as any

function previewData(set: unknown) {
  return { slug_available: true, suggested_slug: '', required_mcps: [], will_create_tasks_agent_scope: 0, set }
}

function renderModal() {
  return render(
    <MemoryRouter>
      <AgentInstallModal open mode="install" template={template} onClose={() => {}} />
    </MemoryRouter>,
  )
}

describe('AgentInstallModal: the department offer', () => {
  beforeEach(() => {
    h.installMock.mockReset()
    h.installMock.mockResolvedValue({ agent_slug: 'yacht-crew', department: { id: 'd', name: 'Lady G' }, department_lines: ['The department Lady G holds Yacht Manager and Yacht Crew.'] })
    h.preview.data = undefined
  })

  it('offers the department for one standalone member, and the install carries the name', async () => {
    h.preview.data = previewData({
      slug: 'yacht-office', name: 'Yacht office', level: 'Team',
      members: [{ template: 'yacht-manager', agent_slug: 'yacht-manager', display_name: 'Yacht Manager', level: 'Head' }],
      existing: [],
    })
    renderModal()
    expect(screen.getByText('Create the Yacht office department and add both agents to it?')).toBeInTheDocument()
    expect(screen.getByTestId('install-offer')).toHaveTextContent('Yacht Manager is installed from the same set and in no department.')
    fireEvent.change(screen.getByLabelText('Department name'), { target: { value: 'Lady G' } })
    fireEvent.click(screen.getByRole('button', { name: /Install agent/ }))
    await waitFor(() => expect(h.installMock).toHaveBeenCalled())
    expect(h.installMock.mock.calls[0][0]).toMatchObject({ template_slug: 'yacht-crew', department_set: { slug: 'yacht-office', name: 'Lady G' } })
    // The lines the placement answered with show before the agent opens,
    // on their own: no model choice was left out, so no such heading.
    await screen.findByText('The department Lady G holds Yacht Manager and Yacht Crew.')
    expect(screen.getByTestId('install-department')).toHaveTextContent('The department Lady G holds Yacht Manager and Yacht Crew.')
    expect(screen.queryByTestId('install-pins-dropped')).toBeNull()
    expect(screen.queryByText('Model choices left out')).toBeNull()
  })

  it('says all N agents for several members, and carries nothing when unticked', async () => {
    h.preview.data = previewData({
      slug: 'yacht-office', name: 'Yacht office', level: 'Team',
      members: [{ template: 'yacht-manager', agent_slug: 'yacht-manager', display_name: 'Yacht Manager', level: 'Head' },
                { template: 'yacht-finance', agent_slug: 'yacht-finance', display_name: 'Yacht Finance', level: 'Team' }],
      existing: [],
    })
    renderModal()
    expect(screen.getByText('Create the Yacht office department and add all 3 agents to it?')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('install-offer-checkbox'))
    expect(screen.queryByLabelText('Department name')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Install agent/ }))
    await waitFor(() => expect(h.installMock).toHaveBeenCalled())
    expect(h.installMock.mock.calls[0][0]).not.toHaveProperty('department_set')
  })

  it('shows no offer when the preview has none', () => {
    h.preview.data = previewData(null)
    renderModal()
    expect(screen.queryByTestId('install-offer')).toBeNull()
  })
})
