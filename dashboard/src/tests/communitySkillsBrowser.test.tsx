import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Community Skills browser: an unreachable skills registry degrades to
//     an empty catalog + banner (never an error page) ───

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'admin', agent_roles: {} }, loading: false }),
}))

import * as authApi from '@/api/auth'
import CommunitySkillsBrowser from '@/components/CommunitySkillsBrowser'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function renderBrowser() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <CommunitySkillsBrowser open={true} onClose={() => {}} />
    </QueryClientProvider>,
  )
}

describe('CommunitySkillsBrowser', () => {
  afterEach(() => fetchSpy.mockReset())

  it('renders the catalog_unreachable banner instead of an error', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/community/skills')) {
        return {
          ok: true,
          json: async () => ({
            registry_version: '', updated_at: '', platform_min_version: null,
            fetched_from: 'https://example.invalid/registry.json',
            catalog_unreachable: true, skills: [],
          }),
        } as Response
      }
      return { ok: true, json: async () => ({ installs: [] }) } as Response
    })
    renderBrowser()
    expect(
      await screen.findByText(/Skills catalog unreachable — showing nothing/),
    ).toBeInTheDocument()
    // No spurious "no matches" empty-state alongside the banner.
    expect(screen.queryByText(/No skill packages match/)).toBeNull()
  })

  it('renders catalog cards when the registry is reachable', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/community/skills')) {
        return {
          ok: true,
          json: async () => ({
            registry_version: '1', updated_at: '2026-07-16T00:00:00Z',
            platform_min_version: null, fetched_from: 'x',
            catalog_unreachable: false,
            skills: [{
              name: 'pdf-skills', label: 'PDF Skills', description: 'Fill forms',
              version: '1.0.0', tags: ['pdf'], installed: false,
              installed_version: null, update_available: false,
              enabled_for_agents: [], pending_request: null, pending_request_count: 0,
            }],
          }),
        } as Response
      }
      return { ok: true, json: async () => ({ installs: [] }) } as Response
    })
    renderBrowser()
    expect(await screen.findByText('PDF Skills')).toBeInTheDocument()
    // Admin/global mode offers Install for a not-installed package.
    expect(screen.getByRole('button', { name: 'Install' })).toBeInTheDocument()
  })

  it('shows the trusted-source author badge on catalog cards', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/community/skills')) {
        return {
          ok: true,
          json: async () => ({
            registry_version: '1', updated_at: '2026-07-17T00:00:00Z',
            platform_min_version: null, fetched_from: 'x',
            catalog_unreachable: false,
            skills: [{
              name: 'frontend-design', label: 'Frontend Design', description: 'Design guidance',
              version: '1.0.0', tags: ['design'], installed: false,
              author: 'Anthropic', author_url: 'https://github.com/anthropics/skills',
              license: 'Apache-2.0',
              installed_version: null, update_available: false,
              enabled_for_agents: [], pending_request: null, pending_request_count: 0,
            }],
          }),
        } as Response
      }
      return { ok: true, json: async () => ({ installs: [] }) } as Response
    })
    renderBrowser()
    const badge = await screen.findByText('by Anthropic')
    expect(badge).toBeInTheDocument()
    expect(badge.closest('a')).toHaveAttribute('href', 'https://github.com/anthropics/skills')
  })

  it('enabled-for-agent but files-missing package offers Reinstall (agent scope)', async () => {
    // Enablement rows survive in the DB when the package folder is lost (e.g.
    // a container recreate before the skills volume existed) — the card must
    // surface a reinstall action, never a dead "Enabled" state.
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/community/skills')) {
        return {
          ok: true,
          json: async () => ({
            registry_version: '1', updated_at: '2026-07-17T00:00:00Z',
            platform_min_version: null, fetched_from: 'x',
            catalog_unreachable: false,
            skills: [{
              name: 'frontend-design', label: 'Frontend Design', description: 'Design guidance',
              version: '1.0.0', tags: [], installed: false,
              installed_version: null, update_available: false,
              enabled_for_agents: ['dev'], pending_request: null, pending_request_count: 0,
            }],
          }),
        } as Response
      }
      if (path.startsWith('/v1/agents/dev/skills')) {
        return { ok: true, json: async () => ({ skills: [] }) } as Response
      }
      return { ok: true, json: async () => ({ installs: [] }) } as Response
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <CommunitySkillsBrowser open={true} onClose={() => {}} agentSlug="dev" />
      </QueryClientProvider>,
    )
    expect(
      await screen.findByText('Enabled — package files missing, reinstall needed'),
    ).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Reinstall' })).toBeInTheDocument()
  })
})
