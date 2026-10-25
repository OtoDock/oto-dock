import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// Shared mock state — hoisted so the vi.mock factories below can close over it.
const h = vi.hoisted(() => ({
  installMock: vi.fn(),
  // useInstallPreview result; tests swap `data` per case.
  preview: { data: undefined as any, isLoading: false },
}))

vi.mock('@/api/agents', () => ({
  useCreateAgent: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'admin', sub: 'u1', agent_roles: {} } }),
}))
vi.mock('@/api/communityAgents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/communityAgents')>()),
  useInstallCommunityAgent: () => ({ mutateAsync: h.installMock, isPending: false }),
  useInstallPreview: () => h.preview,
}))

import AgentInstallModal from '@/components/AgentInstallModal'

const template = {
  slug: 'personal-assistant-pro',
  display_name: 'Personal Assistant Pro',
  version: '1.2.0',
  author: 'OtoDock',
  description: 'Full personal assistant',
  color: '#3B82F6',
} as any

function previewData(mcps: { name: string; needs_request: boolean; blocked: boolean }[]) {
  return {
    slug_available: true,
    suggested_slug: '',
    required_mcps: mcps.map(m => ({ ...m, reason: m.blocked ? 'NOT in any catalog' : 'ok' })),
    will_create_tasks_agent_scope: 0,
  }
}

function renderModal() {
  return render(
    <MemoryRouter>
      <AgentInstallModal open mode="install" template={template} onClose={() => {}} />
    </MemoryRouter>,
  )
}

const installButton = () => screen.getByRole('button', { name: /Install agent/ })

describe('AgentInstallModal — blocked-MCP gating', () => {
  beforeEach(() => {
    h.installMock.mockReset()
    h.preview.data = undefined
    h.preview.isLoading = false
  })

  it('disables Install and explains when a required MCP is in no catalog', () => {
    h.preview.data = previewData([
      { name: 'task-mcp', needs_request: false, blocked: false },
      { name: 'phone-mcp', needs_request: true, blocked: true },
    ])
    renderModal()
    expect(installButton()).toBeDisabled()
    expect(
      screen.getByText(/phone-mcp: NOT in any catalog/),
    ).toBeInTheDocument()
  })

  it('keeps Install enabled when MCPs merely need admin approval', () => {
    h.preview.data = previewData([
      { name: 'task-mcp', needs_request: false, blocked: false },
      { name: 'camoufox', needs_request: true, blocked: false },
    ])
    renderModal()
    expect(installButton()).toBeEnabled()
  })

  it('does not block install while the preview is still loading', () => {
    h.preview.isLoading = true
    renderModal()
    expect(installButton()).toBeEnabled()
  })

  it('shows the apps and checks a template ships and sends the consent when ticked', async () => {
    h.preview.data = {
      ...previewData([{ name: 'task-mcp', needs_request: false, blocked: false }]),
      consent_scope: 'everyone',
      apps: [{
        slug: 'home', title: 'Home', visibility: 'user', sig: 'sig-home', owner_approval: false,
        blueprint_tasks: [],
        row: {
          id: '', slug: 'home', title: 'Home', scope: 'personal', position: 0, rel_path: '', updated_at: '',
          actions: [{ id: 's', label: 'State', type: 'platform', method: 'setup.status' }],
          actions_sig: '', actions_approved: false, approval_stale: false, can_approve: false, can_manage: false,
          hidden_for_me: false, egress: ['api.open-meteo.com'], files: { read: [], write: [] },
          external: { links: [], challenge: [], session_days: 30 }, manifest_empty: false,
        },
      }],
      checks: [{ name: 'lint', description: 'Lint it', mandatory: true, applies: ['chats'], script: 'lint.sh',
                 sig: 'sig-lint', words: 'a judge run per turn' }],
    }
    h.installMock.mockResolvedValue({ agent_slug: 'pa' })
    renderModal()
    expect(screen.getByTestId('consent-app-home')).toHaveTextContent('one copy for each member')
    expect(screen.getByTestId('manifest-summary')).toHaveTextContent('api.open-meteo.com')
    expect(screen.getByTestId('consent-check-lint')).toHaveTextContent(', mandatory')
    expect(screen.getByText(/for everyone who gets a copy/)).toBeInTheDocument()
    const button = screen.getByRole('button', { name: /Install and approve/ })
    fireEvent.click(button)
    await waitFor(() => expect(h.installMock).toHaveBeenCalled())
    expect(h.installMock.mock.calls[0][0]).toMatchObject({
      template_slug: 'personal-assistant-pro',
      approve_apps: { home: 'sig-home' },
      approve_checks: { lint: 'sig-lint' },
    })
    // Unticked: the plain install, no consent in the body.
    h.installMock.mockClear()
    fireEvent.click(screen.getByTestId('consent-checkbox'))
    fireEvent.click(screen.getByRole('button', { name: /Install agent/ }))
    await waitFor(() => expect(h.installMock).toHaveBeenCalled())
    expect(h.installMock.mock.calls[0][0]).not.toHaveProperty('approve_apps')
  })

  it('tells a non-admin their consent covers the shared apps and their own copy', () => {
    h.preview.data = {
      ...previewData([]),
      consent_scope: 'own',
      apps: [],
      checks: [{ name: 'lint', description: '', mandatory: false, applies: [], script: '', sig: 's', words: '' }],
    }
    renderModal()
    expect(screen.getByText(/Members approve theirs/)).toBeInTheDocument()
  })

  it('surfaces the server 400 detail message inline on install failure', async () => {
    h.preview.data = previewData([{ name: 'task-mcp', needs_request: false, blocked: false }])
    h.installMock.mockRejectedValue({
      status: 400,
      message: 'Bad Request',
      body: { detail: { error: 'missing_mcps', message: 'Template requires 1 MCP that are not in the platform OR the community catalog: phone-mcp' } },
    })
    renderModal()
    fireEvent.click(installButton())
    await waitFor(() =>
      expect(screen.getByText(/Template requires 1 MCP/)).toBeInTheDocument(),
    )
  })
})

describe('AgentInstallModal — what an install left out', () => {
  beforeEach(() => {
    h.installMock.mockReset()
    h.preview.data = previewData([])
    h.preview.isLoading = false
  })

  function mountRouted(onClose = vi.fn()) {
    render(
      <MemoryRouter initialEntries={['/agents']}>
        <Routes>
          <Route path="/agents" element={<AgentInstallModal open mode="install" template={template} onClose={onClose} />} />
          <Route path="/agents/:slug/config" element={<p>config page</p>} />
        </Routes>
      </MemoryRouter>,
    )
    return onClose
  }

  it('holds the dialog on the tasks whose model choice was left out, then opens the agent', async () => {
    h.installMock.mockResolvedValue({ agent_slug: 'personal-assistant-pro', task_pins_dropped: ["The task 'digest' runs on the agent's usual model and engine. The model gpt-x is not turned on for OpenAI Codex."] })
    const onClose = mountRouted()
    fireEvent.click(installButton())
    expect(await screen.findByText('Agent installed')).toBeInTheDocument()
    const box = screen.getByTestId('install-pins-dropped')
    expect(box.textContent).toContain('Model choices left out')
    expect(box.textContent).toContain("The task 'digest' runs on the agent's usual model and engine.")
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open the agent' }))
    expect(onClose).toHaveBeenCalled()
    expect(await screen.findByText('config page')).toBeInTheDocument()
  })

  it('closes the "Agent installed" dialog with Close and stays on the page', async () => {
    h.installMock.mockResolvedValue({ agent_slug: 'personal-assistant-pro', task_pins_dropped: ["The task 'digest' runs on the agent's usual model and engine. The model gpt-x is not turned on for OpenAI Codex."] })
    const onClose = mountRouted()
    fireEvent.click(installButton())
    expect(await screen.findByText('Agent installed')).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('config page')).toBeNull()
  })

  it('opens the agent at once when nothing was left out, an older proxy included', async () => {
    h.installMock.mockResolvedValue({ agent_slug: 'personal-assistant-pro' })
    const onClose = mountRouted()
    fireEvent.click(installButton())
    expect(await screen.findByText('config page')).toBeInTheDocument()
    expect(onClose).toHaveBeenCalled()
    expect(screen.queryByText('Agent installed')).toBeNull()
  })

  it("lists a refused template's problems apart, and a plain refusal as its sentence", async () => {
    h.installMock.mockRejectedValueOnce(Object.assign(new Error('a; b'), {
      status: 400,
      body: { detail: { error: 'invalid_template', message: 'a; b', problems: ['agent.json: name is missing', 'tasks/x.json: cron is not valid'] } },
    }))
    mountRouted()
    fireEvent.click(installButton())
    const box = await screen.findByTestId('install-problems')
    expect(box.querySelectorAll('li')).toHaveLength(2)
    expect(box.textContent).toContain('This template cannot be installed')

    h.installMock.mockRejectedValueOnce(Object.assign(new Error('Invalid catalog template: agent.json: name is missing'), {
      status: 400, body: { detail: 'Invalid catalog template: agent.json: name is missing' },
    }))
    fireEvent.click(installButton())
    expect(await screen.findByText('Invalid catalog template: agent.json: name is missing')).toBeInTheDocument()
    expect(screen.queryByTestId('install-problems')).toBeNull()
  })
})
