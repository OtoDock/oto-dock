// Where the MCP Servers page shows the addresses an MCP sends people's
// sign-in to that nobody allowed yet: the ZIP install card's result, and the
// MCP's own row (a badge while it is closed, the Allow card once opened).
// The page reads the waiting list once for every row.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { personClick, renderWithDialogs } from './dialogsHarness'

const h = vi.hoisted(() => ({ allow: vi.fn(async (_p: unknown) => ({ status: 'ok' })) }))

const WAITING = { mcp_name: 'zip-mcp', mcp_label: 'Zip MCP', provider: 'zipco',
                  hosts: ['api.zipco.example'], shared_with: [], digest: 'abc' }

vi.mock('@/api/mcps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/mcps')>()),
  useInstallMcp: () => ({
    isPending: false,
    mutate: (_f: File, o: { onSuccess: (r: unknown) => void }) => o.onSuccess({
      status: 'installed', name: 'zip-mcp', version: '1.0.0', old_version: null, runtime: 'remote',
      install_log: '', hosts_to_confirm: WAITING,
    }),
  }),
}))
vi.mock('@/api/oauth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/oauth')>()),
  useAllowMcpHosts: () => ({ mutateAsync: h.allow, isPending: false }),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/admin/McpInstanceManager', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import { InstallModal } from '@/pages/admin/McpServersPage.installModal'
import McpServersPage from '@/pages/admin/McpServersPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function mcp(name: string, label: string) {
  return {
    name, label, description: 'd', version: '1.0.0', category: 'community', author: '', author_url: '',
    icon: false, runtime: 'remote', transport: 'streamable_http', source: '', enabled: true, can_disable: true,
    patched: false, patch_note: '', credential_type: 'none', credential_label: null, skills: [], config_fields: [],
    config_values: {}, assignment_mode: 'auto', agents: [], tool_filter_supported: false, tool_filter_arg_name: '',
    tool_filter_regex: '',
  }
}

describe('the ZIP install card', () => {
  it('names the addresses that wait and offers Allow', () => {
    const { container } = renderWithDialogs(<InstallModal onClose={() => {}} />)
    const input = container.ownerDocument.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'zip-mcp.zip')] } })
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    expect(screen.getByText(/Zip MCP sends people's zipco sign-in to/)).toBeTruthy()
    expect(screen.getByText('api.zipco.example')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Allow these addresses' })).toBeTruthy()
  })
})

describe("the MCP's own row", () => {
  afterEach(() => fetchSpy.mockReset())

  function renderPage() {
    const allowlistReads: string[] = []
    fetchSpy.mockImplementation(async (url: string) => {
      if (url.endsWith('/v1/admin/mcps')) {
        return { ok: true, json: async () => ({ mcps: [mcp('zip-mcp', 'Zip MCP'), mcp('calm-mcp', 'Calm MCP')] }) } as Response
      }
      if (url.endsWith('/v1/admin/oauth-bearer-allowlist')) {
        allowlistReads.push(url)
        return { ok: true, json: async () => ({ entries: [], pending: [WAITING] }) } as Response
      }
      if (url.includes('/update-state')) return { ok: true, json: async () => ({ updates: {}, checked: 0, checked_at: '' }) } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderWithDialogs(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <McpServersPage />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    return allowlistReads
  }

  it('shows a badge while closed and the Allow card once opened', async () => {
    const reads = renderPage()
    expect(await screen.findByText('Addresses wait')).toBeTruthy()
    expect(screen.getAllByText('Addresses wait')).toHaveLength(1)  // the calm row has none
    expect(screen.queryByRole('button', { name: 'Allow these addresses' })).toBeNull()
    fireEvent.click(screen.getByText('Zip MCP'))
    expect(screen.getByText(/Zip MCP sends people's zipco sign-in to/)).toBeTruthy()
    personClick(screen.getByRole('button', { name: 'Allow these addresses' }))
    await waitFor(() => expect(h.allow).toHaveBeenCalledWith(WAITING))
    expect(reads).toHaveLength(1)  // one read for every row
  })

  it('shows no card in a row that waits on nothing', async () => {
    renderPage()
    fireEvent.click(await screen.findByText('Calm MCP'))
    expect(screen.queryByText(/sends people's/)).toBeNull()
  })
})
