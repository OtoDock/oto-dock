import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Admin Skills page: a package ahead of the catalog (1.7.1) ───
//
// The update check reports a skill package installed past the catalog as
// `reason: ahead`. The page never counts it or offers an Update for it (that
// would move the package back): it shows an explicit revert behind a confirm,
// and an ordinary offer still gets its Update button.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunitySkillsBrowser', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import SkillsPage from '@/pages/admin/SkillsPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')
let confirmSpy: ReturnType<typeof vi.spyOn> | undefined

function pkg(name: string, version: string) {
  return {
    name, label: name, description: '', version, category: 'skill', author: '', author_url: '',
    icon: false, runtime: 'none', transport: 'none', source: '', enabled: true, can_disable: true,
    patched: false, patch_note: '', credential_type: 'none', credential_label: null,
    skills: [{ id: `${name}-skill`, description: '' }], config_fields: [], config_values: {},
    assignment_mode: 'auto', agents: [], tool_filter_supported: false, tool_filter_arg_name: '',
    tool_filter_regex: '', has_scripts: false,
  }
}

const UPDATES = {
  updates: {
    'theme-factory': { current: '2.0.0', latest: '1.0.0', registry: 'skills-catalog', package: 'theme-factory', reason: 'ahead' },
    'doc-writer': { current: '1.0.0', latest: '1.1.0', registry: 'skills-catalog', package: 'doc-writer', reason: 'package' },
  },
  checked: 2, checked_at: '2026-10-01T10:00:00Z',
}

function mockApi(calls: string[]) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    if (method !== 'GET') {
      calls.push(`${method} ${url}`)
      return { ok: true, json: async () => ({ status: 'updated', install_log: 'converged' }) } as Response
    }
    if (url.endsWith('/v1/admin/mcps')) {
      return { ok: true, json: async () => ({ mcps: [pkg('theme-factory', '2.0.0'), pkg('doc-writer', '1.0.0')] }) } as Response
    }
    if (url.includes('/check-updates') || url.includes('/update-state')) {
      return { ok: true, json: async () => UPDATES } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SkillsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('SkillsPage — a package ahead of the catalog', () => {
  afterEach(() => {
    fetchSpy.mockReset()
    confirmSpy?.mockRestore()
    confirmSpy = undefined
  })

  it('is a revert behind a confirm, never an update offer', async () => {
    const calls: string[] = []
    mockApi(calls)
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderPage()
    // The Skills page reads the check it runs itself.
    fireEvent.click(await screen.findByRole('button', { name: /Check Updates/ }))
    const revert = await screen.findByRole('button', { name: 'Revert to catalog 1.0.0' })
    // One offer (doc-writer), the ahead package not counted.
    expect(screen.getAllByRole('button', { name: 'Update' })).toHaveLength(1)
    expect(screen.queryByText('2 updates')).not.toBeInTheDocument()
    fireEvent.click(revert)
    await waitFor(() => expect(calls).toEqual(['POST /v1/admin/mcps/theme-factory/update']))
  })

  it('a cancelled confirm reverts nothing', async () => {
    const calls: string[] = []
    mockApi(calls)
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /Check Updates/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Revert to catalog 1.0.0' }))
    expect(calls).toEqual([])
  })
})
