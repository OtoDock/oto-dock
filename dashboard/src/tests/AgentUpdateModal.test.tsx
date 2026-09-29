import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({
  plan: { data: undefined as any, isLoading: false, isError: false, error: null as any },
  applyMock: vi.fn(),
  takeMock: vi.fn(),
  status: { data: undefined as any },
}))

vi.mock('@/api/communityAgents', () => ({
  useTemplateUpdatePlan: () => h.plan,
  useApplyTemplateUpdate: () => ({ mutateAsync: h.applyMock, isPending: false }),
  useTemplateUpdateStatus: () => h.status,
  useTakeNewVersion: () => ({ mutateAsync: h.takeMock, isPending: false }),
}))

import AgentUpdateModal from '@/components/AgentUpdateModal'

const row = {
  id: '', slug: 'home', title: 'Home', scope: 'personal', pin_scope: 'standing', position: 0, rel_path: '',
  updated_at: '', kind: 'folder', actions: [{ id: 'me', label: 'Me', type: 'platform', method: 'viewer.me' }],
  actions_sig: '', actions_approved: false, approval_stale: false, can_approve: false, can_manage: false,
  viewer_role: 'viewer', hidden_for_me: false, files: null, egress: ['api.open-meteo.com'], handlers: null,
  exports: null, bindings: [], requires: null, manifest_empty: false, steps: {}, secrets: [], inbound: {},
  external: null,
} as any

const PLAN = {
  agent_slug: 'pa', from_version: '2.0.0', to_version: '3.0.0', template_slug: 'personal-assistant',
  display_name: 'Personal Assistant', replaced: ['the persona (config/agent.md)'], added: ['the check \'style\''],
  kept: [{ what: 'context/notes.md', reason: 'edited locally', path: 'config/context/notes.md',
           new_path: 'config/community/3.0.0/context/notes.md' }],
  unchanged: 2, pending_apps: [], offered_checks: [], members: {}, mcps: { new: [] },
  apps: [{ slug: 'home', title: 'Home', visibility: 'user', sig: 'sig-home', owner_approval: false, row,
           blueprint_tasks: [], change: 'new' },
         { slug: 'old', title: 'Old', visibility: 'user', sig: 'sig-old', owner_approval: false, row,
           blueprint_tasks: [], change: 'same' }],
  checks: [{ name: 'style', description: 'House style', mandatory: false, applies: ['tasks'], script: '',
             sig: 'sig-style', words: 'a judge run per turn', change: 'new' }],
  consent_scope: 'everyone', has_baseline: true,
}

function renderModal() {
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <AgentUpdateModal open agentSlug="pa" onClose={() => {}} />
    </QueryClientProvider>,
  )
}

describe('AgentUpdateModal', () => {
  beforeEach(() => {
    h.applyMock.mockReset()
    h.takeMock.mockReset()
    h.plan.data = PLAN
    h.plan.isLoading = false
    h.plan.isError = false
    h.status.data = undefined
  })

  it('shows the plan, consents only to the new or changed apps and checks, and starts the job', async () => {
    h.applyMock.mockResolvedValue({ job_id: 'j1', to_version: '3.0.0' })
    renderModal()
    expect(screen.getByText(/Will be replaced/)).toBeTruthy()
    expect(screen.getByText('the persona (config/agent.md)')).toBeTruthy()
    expect(screen.getByText(/Kept as you have them/)).toBeTruthy()
    expect(screen.getByTestId('consent-app-home')).toBeTruthy()
    expect(screen.queryByTestId('consent-app-old')).toBeNull()
    expect(screen.getByTestId('consent-check-style')).toBeTruthy()
    fireEvent.click(screen.getByTestId('update-submit'))
    await waitFor(() => expect(h.applyMock).toHaveBeenCalled())
    const body = h.applyMock.mock.calls[0][0]
    expect(body.from_version).toBe('2.0.0')
    expect(body.approve_apps).toEqual({ home: 'sig-home' })
    expect(body.approve_checks).toEqual({ style: 'sig-style' })
  })

  it('sends no consent when the checkbox is unticked', async () => {
    h.applyMock.mockResolvedValue({ job_id: 'j1', to_version: '3.0.0' })
    renderModal()
    fireEvent.click(screen.getByTestId('update-consent-checkbox'))
    fireEvent.click(screen.getByTestId('update-submit'))
    await waitFor(() => expect(h.applyMock).toHaveBeenCalled())
    const body = h.applyMock.mock.calls[0][0]
    expect(body.approve_apps).toBeUndefined()
    expect(body.approve_checks).toBeUndefined()
  })

  it('renders the report with a "Take the new version" button per kept piece', async () => {
    h.applyMock.mockResolvedValue({ job_id: 'j1', to_version: '3.0.0' })
    h.takeMock.mockResolvedValue({ status: 'replaced', path: 'config/context/notes.md' })
    // The status hook answers "done" as soon as the job is started.
    h.status.data = { running: false, status: 'done', report: { ...PLAN, replaced: ['the persona (config/agent.md)'] } }
    renderModal()
    expect(screen.queryByTestId('update-report')).toBeNull()
    fireEvent.click(screen.getByTestId('update-submit'))
    await waitFor(() => expect(h.applyMock).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByTestId('update-report')).toBeTruthy())
    const take = screen.getByRole('button', { name: /Take the new version/ })
    fireEvent.click(take)
    await waitFor(() => expect(h.takeMock).toHaveBeenCalledWith({ agent_slug: 'pa', new_path: 'config/community/3.0.0/context/notes.md' }))
  })

  it('offers Update again after a failed job', async () => {
    h.applyMock.mockResolvedValue({ job_id: 'j1', to_version: '3.0.0' })
    h.status.data = { running: false, status: 'failed', error: 'the tarball is gone' }
    renderModal()
    fireEvent.click(screen.getByTestId('update-submit'))
    await waitFor(() => expect(h.applyMock).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText(/the tarball is gone/)).toBeTruthy())
    expect(screen.getByTestId('update-submit')).toBeTruthy()
  })

  it('says a job the platform lost failed and offers Update again', async () => {
    h.applyMock.mockResolvedValue({ job_id: 'j1', to_version: '3.0.0' })
    h.status.data = { running: false }
    renderModal()
    fireEvent.click(screen.getByTestId('update-submit'))
    await waitFor(() => expect(h.applyMock).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText(/the platform restarted while it ran/)).toBeTruthy())
    expect(screen.getByTestId('update-submit')).toBeTruthy()
  })

  it('lists the pieces the manager removed apart, with nothing to take', () => {
    h.plan.data = {
      ...PLAN,
      kept: [...PLAN.kept, { what: "the task 'daily'", reason: 'removed locally', path: '', new_path: '' }],
    }
    renderModal()
    const removed = screen.getByTestId('removed-list')
    expect(removed.textContent).toContain('Left out, because you removed them')
    expect(removed.textContent).toContain("the task 'daily'")
    expect(screen.getAllByTestId('kept-row')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: /Take the new version/ })).toBeNull()
  })
})
