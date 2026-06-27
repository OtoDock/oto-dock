import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

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
vi.mock('@/api/communityAgents', () => ({
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
      screen.getByText(/phone-mcp is not installed and not in any catalog/),
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
