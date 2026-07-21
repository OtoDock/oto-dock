import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// ─── Agent MCP toggles autosave on click (the Save button lived at the top
//     of a long list, scrolled out of view, and toggles got silently lost) ───

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
    { name: 'memory', label: 'Memory', description: '', category: 'core', assignment_mode: 'auto', credential_type: '', has_service_account: false, enabled: true, authorized_by: 'auto' },
    { name: 'camoufox', label: 'Browser', description: '', category: 'community', assignment_mode: 'explicit', credential_type: '', has_service_account: false, enabled: false, authorized_by: 'admin' },
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

describe('AgentMcps — autosave', () => {
  afterEach(() => fetchSpy.mockReset())

  it('a toggle click PUTs the new set immediately — no Save button involved', async () => {
    const puts: unknown[] = []
    fetchSpy.mockImplementation(async (path: string, options?: RequestInit) => {
      if (options?.method === 'PUT') {
        puts.push(JSON.parse(String(options.body)))
        return { ok: true, json: async () => ({}) } as Response
      }
      if (path.includes('/mcps')) return { ok: true, json: async () => MCP_DATA } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    renderPage()
    const camoufox = await screen.findByText('Browser')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()

    fireEvent.click(camoufox.closest('label')!.querySelector('input')!)
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0]).toEqual({ mcps: ['memory', 'camoufox'] })
    // The Saved flash confirms the write landed without any Save click.
    expect(await screen.findByText('Saved')).toBeTruthy()
  })
})
