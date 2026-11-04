import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Admin MCP Servers page: an MCP the platform attaches ───
//
// `assignment_mode: "platform"` (the call controls): the platform attaches
// it to every call and nobody assigns it. The row says so in the neutral
// tone, with no assignment badge, and its "Enabled for" reads "Every call".

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/admin/McpInstanceManager', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import McpServersPage from '@/pages/admin/McpServersPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const CALL_CONTROL = {
  name: 'call-control-mcp', label: 'Call controls', description: 'The call tools of a phone session.',
  version: '1.0.0', category: 'core', author: '', author_url: '', icon: false, runtime: 'python',
  transport: 'stdio', source: '', enabled: true, can_disable: false, patched: false, patch_note: '',
  credential_type: 'none', credential_label: null, skills: [], config_fields: [], config_values: {},
  assignment_mode: 'platform', agents: [], tool_filter_supported: false, tool_filter_arg_name: '',
  tool_filter_regex: '',
}

describe('McpServersPage — an MCP the platform attaches', () => {
  afterEach(() => fetchSpy.mockReset())

  it('reads "Attached by the platform" and is enabled for every call', async () => {
    fetchSpy.mockImplementation(async (url: string) => {
      if (url.endsWith('/v1/admin/mcps')) return { ok: true, json: async () => ({ mcps: [CALL_CONTROL] }) } as Response
      if (url.includes('/update-state')) return { ok: true, json: async () => ({ updates: {}, checked: 0, checked_at: '' }) } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <McpServersPage />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByText('Attached by the platform')).toBeInTheDocument()
    expect(screen.queryByText('Explicit assignment')).toBeNull()
    fireEvent.click(screen.getByText('Call controls'))
    expect(screen.getByText('Enabled for')).toBeInTheDocument()
    expect(screen.getByText('Every call')).toBeInTheDocument()
  })
})
