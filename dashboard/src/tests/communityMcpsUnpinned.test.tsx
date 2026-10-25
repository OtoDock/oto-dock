import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Community MCPs browser: a git+ entry the catalog pins to a tag or a
//     branch gets a chip, never an Update button (the install from the
//     catalog refuses an unpinned ref) ───

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'admin', agent_roles: {} }, loading: false }),
}))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import CommunityMcpsBrowser from '@/components/CommunityMcpsBrowser'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

describe('CommunityMcpsBrowser unpinned git entry', () => {
  afterEach(() => fetchSpy.mockReset())

  it('shows the chip instead of Update', async () => {
    const entry = (name: string, extra: Record<string, unknown>) => ({
      name, label: name, description: 'd', category: 'community', version: '1.0.3', runtime: 'python',
      source: `git+https://example.com/${name}.git@v1.0.3`, tags: [], author: '', author_url: null,
      license: 'MIT', requires_credentials: false, requires_system_packages: [], platform_min_version: null,
      assignment_mode: 'auto', size_bytes: 0, deprecated: false, patched: false, patch_note: null,
      trust: 'third-party', installed: true, installed_version: '1.0.2',
      enabled_for_agents: [], pending_request: null, pending_request_count: 0, ...extra,
    })
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/community/mcps')) {
        return { ok: true, json: async () => ({
          registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: 'x',
          mcps: [entry('blender', { update_available: false, catalog_unpinned: true }),
                 entry('pinned', { update_available: true, catalog_unpinned: false })],
        }) } as Response
      }
      return { ok: true, json: async () => ({ installs: [], jobs: [] }) } as Response
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <CommunityMcpsBrowser open={true} onClose={() => {}} />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByText('No pinned commit')).toHaveAttribute('title', expect.stringMatching(/pins a commit/))
    expect(screen.getAllByRole('button', { name: 'Update' })).toHaveLength(1)
  })
})
