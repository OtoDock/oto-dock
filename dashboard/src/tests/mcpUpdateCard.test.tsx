import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Admin MCP Servers page: what an update offer says (1.8.0) ───
//
// A container still serving the image an update replaced reads "restart to
// switch". An npm/pypi offer says what the weekly job does with it: a
// release younger than seven days waits, a held MCP or switched-off
// automatic updates say so. A git+ offer is built by the weekly job at a
// commit the catalog pins; one at a tag or a branch offers no Update.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/admin/McpInstanceManager', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import { automaticUpdateNote, type McpUpdateInfo } from '@/api/mcps'
import McpServersPage from '@/pages/admin/McpServersPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function mcp(over: Record<string, unknown>) {
  return {
    name: 'video-tools', label: 'Video Tools', description: 'Edit', version: '0.2.1',
    category: 'community', author: '', author_url: '', icon: false, runtime: 'docker',
    transport: 'sse', source: 'docker:video-tools', enabled: true, can_disable: true,
    patched: false, patch_note: '', credential_type: 'none', credential_label: null, skills: [],
    config_fields: [], config_values: {}, assignment_mode: 'auto', agents: [],
    tool_filter_supported: false, tool_filter_arg_name: '', tool_filter_regex: '',
    ...over,
  }
}

function mockApi(mcps: unknown[], updates: Record<string, unknown> = {}, auto = true) {
  fetchSpy.mockImplementation(async (url: string) => {
    if (url.endsWith('/v1/admin/mcps')) return { ok: true, json: async () => ({ mcps }) } as Response
    if (url.includes('/update-state')) {
      return { ok: true, json: async () => ({ updates, checked: 1, checked_at: '2026-10-09T03:00:00Z', auto_update_enabled: auto }) } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpServersPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('McpServersPage — the update card', () => {
  afterEach(() => fetchSpy.mockReset())

  it('says restart to switch while the container serves the replaced image', async () => {
    mockApi([mcp({ docker_status: 'running', docker_image_stale: true })])
    renderPage()
    expect(await screen.findByText('Running, restart to switch')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Video Tools'))
    expect(screen.getByText(/Updated to v0\.2\.1\. The container still runs the previous/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Restart' })).toBeEnabled()
  })

  it('reads plain Running once the container runs the manifest image', async () => {
    mockApi([mcp({ docker_status: 'running', docker_image_stale: false })])
    renderPage()
    expect(await screen.findByText('Running')).toBeInTheDocument()
    expect(screen.queryByText(/restart to switch/)).not.toBeInTheDocument()
  })

  it('says when the weekly job applies a young release', async () => {
    mockApi(
      [mcp({ name: 'pkg-mcp', label: 'Package', runtime: 'node', source: 'npm:pkg@1.0.0', version: '1.0.0' })],
      { 'pkg-mcp': { current: '1.0.0', latest: '1.2.0', registry: 'npm', package: 'pkg', reason: 'package', settled: '1.1.0', auto_after: '2026-10-15T12:00:00+00:00' } },
    )
    renderPage()
    fireEvent.click(await screen.findByText('Package'))
    expect(screen.getByText(/Automatic updates apply 1\.1\.0 now and 1\.2\.0 at the first run after/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update' })).toBeEnabled()
  })

  it('offers no Update for a git source the catalog pins to a tag', async () => {
    const git = { name: 'blender', label: 'Blender', runtime: 'python', source: 'git+https://h/r.git@v1.0.2', version: '1.0.2' }
    mockApi([mcp(git)], { blender: { current: '1.0.2', latest: '1.0.3', registry: 'git', package: 'blender', reason: 'package', pinned_commit: false, catalog_ref: 'v1.0.3' } })
    renderPage()
    fireEvent.click(await screen.findByText('Blender'))
    expect(screen.getByText(/names a tag or a branch, not a commit/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Update' })).not.toBeInTheDocument()
  })

  it('offers the Update for a git source at a catalog commit', async () => {
    const git = { name: 'blender', label: 'Blender', runtime: 'python', source: 'git+https://h/r.git@v1.0.2', version: '1.0.2' }
    mockApi([mcp(git)], { blender: { current: '1.0.2', latest: '1.0.3', registry: 'git', package: 'blender', reason: 'package', pinned_commit: true, catalog_ref: 'a'.repeat(40) } })
    renderPage()
    fireEvent.click(await screen.findByText('Blender'))
    expect(screen.getByText(
      'On the OtoDock platform, the next automatic run builds the commit the community list pins. '
      + 'Paired machines rebuild it at their next sync.',
    )).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('this server')
    expect(screen.getByRole('button', { name: 'Update' })).toBeEnabled()
  })
})

describe('automaticUpdateNote', () => {
  const base: McpUpdateInfo = { current: '1.0.0', latest: '1.2.0', registry: 'npm', package: 'p', reason: 'package' }

  it('names each state', () => {
    expect(automaticUpdateNote({ ...base, settled: '', auto_after: '2026-10-15T12:00:00Z' }, true))
      .toMatch(/^Applies at the first automatic run after .*seven days after its release\.$/)
    expect(automaticUpdateNote({ ...base, settled: '', auto_after: '' }, true))
      .toBe('Its release date is unknown: it applies when an admin updates it.')
    expect(automaticUpdateNote({ ...base, settled: '1.2.0' }, true)).toBeNull()
    // A catalog edit beside the waiting release waits with it.
    expect(automaticUpdateNote({ ...base, reason: 'both', settled: '1.1.0', auto_after: '2026-10-15T12:00:00Z' }, true))
      .toMatch(/^Applies at the first automatic run after /)
    expect(automaticUpdateNote({ ...base, settled: '', auto_after: '', held: true }, true))
      .toBe('Held: automatic updates skip it.')
    expect(automaticUpdateNote({ ...base, settled: '', auto_after: '' }, false))
      .toBe('Automatic updates are off.')
    expect(automaticUpdateNote({ ...base, reason: 'manifest', settled: '' }, true)).toBeNull()
    // A container's row (no `settled`): the job applies it as before.
    expect(automaticUpdateNote({ ...base, registry: 'catalog' }, true)).toBeNull()
    // A git+ offer: built by the weekly job at a commit the catalog pins,
    // never at a tag or a branch (which the catalog install refuses).
    expect(automaticUpdateNote({ ...base, registry: 'git', pinned_commit: true }, true))
      .toMatch(/^On the OtoDock platform, the next automatic run builds the commit the community list pins\./)
    expect(automaticUpdateNote({ ...base, registry: 'git', pinned_commit: true, held: true }, true))
      .toBe('Held: automatic updates skip it.')
    expect(automaticUpdateNote({ ...base, registry: 'git', pinned_commit: true }, false))
      .toBe('Automatic updates are off.')
    expect(automaticUpdateNote({ ...base, registry: 'git', pinned_commit: false }, true))
      .toMatch(/names a tag or a branch, not a commit/)
  })
})


// ─── The account form marks optional fields and names what an account lacks ───

import { PlainCredsAccountForm } from '@/components/accounts/PlainCredsAccountForm'

describe('optional credential fields', () => {
  it('marks an optional field and names what an account lacks', () => {
    const ops = { setIntegration: { mutateAsync: vi.fn(), isPending: false },
                  deleteIntegration: { mutateAsync: vi.fn(), isPending: false } }
    render(
      <PlainCredsAccountForm
        integration={{
          mcp_name: 'email', display_name: 'Email', description: '', configured: true,
          required_keys: ['EMAIL_USER'],
          fields: [
            { key: 'EMAIL_USER', label: 'User', input_type: 'text' },
            { key: 'EMAIL_ADDRESS', label: 'Sender address', input_type: 'email', required: false },
          ],
          supports_multi_account: true, accounts: [], candidate_agents: [],
        } as never}
        account={{ account_label: 'default', display_email: '', is_default: true, created_at: '',
                   configured_keys: ['EMAIL_USER'], connected_services: [], agent_overrides: [],
                   missing_scopes: [], missing_required: ['Password'] }}
        ops={ops as never}
        onDone={() => {}}
      />,
    )
    expect(screen.getByText(', optional')).toBeInTheDocument()
    expect(screen.getByText(/This account needs Password before/)).toBeInTheDocument()
  })
})


describe('a Docker action the server refuses', () => {
  afterEach(() => fetchSpy.mockReset())

  it('shows the server reason under the buttons', async () => {
    const m = mcp({ docker_status: 'running', docker_image_stale: true })
    fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/docker/restart') && init?.method === 'POST') {
        return new Response(JSON.stringify({ detail: 'Failed to restart the container. When an update just changed its image and that image is not on this server, run Update again.' }), { status: 500 })
      }
      if (url.endsWith('/v1/admin/mcps')) return { ok: true, json: async () => ({ mcps: [m] }) } as Response
      if (url.includes('/update-state')) return { ok: true, json: async () => ({ updates: {}, checked: 0, checked_at: '' }) } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    renderPage()
    fireEvent.click(await screen.findByText('Video Tools'))
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))
    expect(await screen.findByText(/run Update again/)).toBeInTheDocument()
  })
})
