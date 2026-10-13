import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Community MCPs browser: each entry carries the catalog's trust tier
//     (first-party, maintained, third-party; the proxy reads an untagged
//     entry as third-party) ───

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'admin', agent_roles: {} }, loading: false }),
}))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import CommunityMcpsBrowser from '@/components/CommunityMcpsBrowser'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

describe('CommunityMcpsBrowser trust tier', () => {
  afterEach(() => fetchSpy.mockReset())

  it('badges each entry with its tier', async () => {
    const entry = (name: string, trust: string) => ({
      name, label: name, description: 'd', category: 'community', version: '', runtime: 'node',
      source: `npm:${name}`, tags: [], author: '', author_url: null, license: 'MIT',
      requires_credentials: false, requires_system_packages: [], platform_min_version: null,
      assignment_mode: 'auto', size_bytes: 0, deprecated: false, patched: false, patch_note: null,
      trust, installed: false, installed_version: null, update_available: false,
      enabled_for_agents: [], pending_request: null, pending_request_count: 0,
    })
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/community/mcps')) {
        return { ok: true, json: async () => ({
          registry_version: '1', updated_at: '', platform_min_version: null, fetched_from: 'x',
          mcps: [entry('ours', 'first-party'), entry('vendor', 'maintained'), entry('pkg', 'third-party')],
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
    expect(await screen.findByText('first-party')).toBeInTheDocument()
    expect(screen.getByText('maintained')).toBeInTheDocument()
    expect(screen.getByText('third-party')).toHaveAttribute('title', expect.stringMatching(/follows its upstream/))
  })

})
