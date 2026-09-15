import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// ─── The agent MCPs tab filters by category through the funnel menu ───

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/ServiceAccountBindingDropdown', () => ({
  ServiceAccountBindingDropdown: () => null,
}))

import * as authApi from '@/api/auth'
import AgentMcps from '@/pages/agent/AgentMcps'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const MCP_DATA = {
  mcps: [
    { name: 'memory-mcp', label: 'Memory', description: '', category: 'core', author: '', author_url: '', icon: true, assignment_mode: 'auto', credential_type: '', has_service_account: false, enabled: true, authorized_by: 'auto' },
    { name: 'ssh-hosts', label: 'SSH', description: '', category: 'custom', author: '', author_url: '', icon: true, assignment_mode: 'auto', credential_type: '', has_service_account: false, enabled: false, authorized_by: 'auto' },
    { name: 'github-mcp', label: 'GitHub', description: '', category: 'community', author: 'GitHub', author_url: 'https://github.com/github/github-mcp-server', icon: false, assignment_mode: 'auto', credential_type: '', has_service_account: false, enabled: false, authorized_by: 'auto' },
  ],
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/agents/dev/mcps']}>
        <Routes>
          <Route path="/agents/:name/mcps" element={<AgentMcps />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AgentMcps — category filter and icons', () => {
  afterEach(() => fetchSpy.mockReset())

  it('shows every row with its icon, then only the community rows once Community is picked', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.includes('/mcps')) return { ok: true, json: async () => MCP_DATA } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    const { container } = renderPage()
    await screen.findByText('Memory')
    expect(screen.getByText('GitHub')).toBeTruthy()
    // Bundled rows load their icon.png; the community row tries the image too
    // (the proxy falls back to the catalog's icon).
    const sources = Array.from(container.querySelectorAll('img')).map(i => i.getAttribute('src'))
    expect(sources).toEqual([
      '/v1/mcps/memory-mcp/icon.png',
      '/v1/mcps/ssh-hosts/icon.png',
      '/v1/mcps/github-mcp/icon.png',
    ])

    fireEvent.click(screen.getByLabelText('Filter MCPs by category'))
    expect(screen.getByText('All MCPs (3)')).toBeTruthy()
    fireEvent.click(screen.getByText('Community (1)'))

    // The menu closed on selection; core and custom rows are gone.
    expect(screen.queryByText('All MCPs (3)')).toBeNull()
    expect(screen.queryByText('Memory')).toBeNull()
    expect(screen.queryByText('SSH')).toBeNull()
    expect(screen.getByText('GitHub')).toBeTruthy()

    // Back to everything.
    fireEvent.click(screen.getByLabelText('Filter MCPs by category'))
    fireEvent.click(screen.getByText('All MCPs (3)'))
    expect(await screen.findByText('Memory')).toBeTruthy()
  })
})
