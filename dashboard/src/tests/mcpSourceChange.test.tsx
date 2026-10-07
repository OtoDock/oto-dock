import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Admin MCP Servers page: a catalog source change (1.7.1) ───
//
// The page loads the last update check as the proxy persisted it, so a
// pending source change shows without Check Updates pressed. The row's card
// names the old and new source, says whether the catalog entry declares the
// change (an undeclared one carries the possible-compromise warning), lists
// the credentials that carry, get renamed or need a reconnect, and the
// Switch posts exactly the pair the card showed. A finished switch shows
// its result until dismissed; an install ahead of the catalog is a note
// with an explicit revert, never an offer.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/admin/McpInstanceManager', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import McpServersPage from '@/pages/admin/McpServersPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')
let confirmSpy: ReturnType<typeof vi.spyOn> | undefined

function confirmAnswers(value: boolean) {
  confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(value)
}

const MCPS = {
  mcps: [{
    name: 'nextcloud', label: 'Nextcloud', description: 'Files', version: '1.0.0',
    category: 'community', author: 'abdullahMASHUK', author_url: 'https://github.com/x/y',
    icon: false, runtime: 'node', transport: 'stdio', source: 'npm:nextcloud-mcp-server@1.0.0',
    enabled: true, can_disable: true, patched: false, patch_note: '', credential_type: 'none',
    credential_label: null, skills: [], config_fields: [], config_values: {},
    assignment_mode: 'auto', agents: [], tool_filter_supported: false, tool_filter_arg_name: '',
    tool_filter_regex: '',
  }],
}

function change(over: Partial<Record<string, unknown>> = {}) {
  return {
    status: 'pending', declared: false,
    from: { kind: 'npm', identity: 'nextcloud-mcp-server', url: 'https://www.npmjs.com/package/nextcloud-mcp-server', runtime: 'node' },
    to: { kind: 'npm', identity: 'nextcloud-mcp', url: 'https://www.npmjs.com/package/nextcloud-mcp', runtime: 'node', version: '' },
    plan: {
      runtime: { from: 'node', to: 'node' },
      credentials: { carry: ['NEXTCLOUD_URL'], rename: { NEXTCLOUD_USER: 'NEXTCLOUD_USERNAME' }, reconnect: ['NEXTCLOUD_TOKEN'], oauth: 'none' },
    },
    to_manifest_hash: 'h-catalog',
    detected_at: '2026-10-01T10:00:00Z', accepted_at: '', result: {},
    ...over,
  }
}

function sourceState(declared: boolean) {
  return {
    updates: {
      nextcloud: {
        current: '1.0.0', latest: '', registry: 'npm', package: 'nextcloud',
        reason: 'source', source_change: change({ declared }),
      },
    },
    checked: 1, checked_at: '2026-10-01T10:00:00Z',
  }
}

type Call = { url: string; method: string; body?: unknown }

function mockApi(state: unknown, calls: Call[], onState?: () => unknown) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    if (method !== 'GET') {
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      return { ok: true, json: async () => ({ status: 'switched', install_log: 'npm install ok', result: {} }) } as Response
    }
    if (url.endsWith('/v1/admin/mcps')) return { ok: true, json: async () => MCPS } as Response
    if (url.includes('/update-state')) return { ok: true, json: async () => (onState ? onState() : state) } as Response
    if (url.includes('/check-updates')) return { ok: true, json: async () => state } as Response
    calls.push({ url, method })
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

async function expandRow() {
  fireEvent.click(await screen.findByText('Nextcloud'))
}

describe('McpServersPage — source changes', () => {
  afterEach(() => {
    fetchSpy.mockReset()
    confirmSpy?.mockRestore()
    confirmSpy = undefined
  })

  it('shows a pending change from the persisted state and switches with the exact pair', async () => {
    const calls: Call[] = []
    mockApi(sourceState(false), calls)
    confirmAnswers(true)
    renderPage()

    // The badge and the header chip come from the persisted state, no check pressed.
    expect(await screen.findByText('1 update')).toBeInTheDocument()
    expect(screen.getByText('source changed')).toBeInTheDocument()
    expect(fetchSpy.mock.calls.some(c => String(c[0]).includes('/check-updates'))).toBe(false)

    await expandRow()
    expect(screen.getByText('Source change')).toBeInTheDocument()
    expect(screen.getByText(/possible compromise/)).toBeInTheDocument()
    expect(screen.getByText('https://www.npmjs.com/package/nextcloud-mcp-server')).toHaveAttribute('href')
    expect(screen.getByText('https://www.npmjs.com/package/nextcloud-mcp')).toHaveAttribute('href')
    expect(screen.getByText('NEXTCLOUD_URL')).toBeInTheDocument()
    expect(screen.getByText('NEXTCLOUD_USER')).toBeInTheDocument()
    expect(screen.getByText('NEXTCLOUD_USERNAME')).toBeInTheDocument()
    expect(screen.getByText('NEXTCLOUD_TOKEN')).toBeInTheDocument()
    expect(screen.queryByText('Update Available')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Switch' }))
    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0]).toEqual({
      url: '/v1/admin/mcps/nextcloud/accept-source', method: 'POST',
      body: { from: 'https://www.npmjs.com/package/nextcloud-mcp-server', to: 'https://www.npmjs.com/package/nextcloud-mcp', manifest_hash: 'h-catalog' },
    })
    expect(await screen.findByText('npm install ok')).toBeInTheDocument()
  })

  it('an image reference or a host is text, never a link into this origin', async () => {
    const calls: Call[] = []
    mockApi(sourceState(false), calls, () => ({
      ...sourceState(false),
      updates: { nextcloud: { ...sourceState(false).updates.nextcloud, source_change: change({
        from: { kind: 'image', identity: 'ghcr.io/otodock/nextcloud', url: 'ghcr.io/otodock/nextcloud', runtime: 'docker' },
        to: { kind: 'image', identity: 'localhost/mirror/nextcloud', url: 'localhost/mirror/nextcloud', runtime: 'docker', version: '1.2.0' },
      }) } },
    }))
    renderPage()
    await expandRow()
    expect(await screen.findByText('ghcr.io/otodock/nextcloud')).not.toHaveAttribute('href')
    expect(screen.getByText('localhost/mirror/nextcloud')).not.toHaveAttribute('href')
    expect(screen.getByText('ghcr.io/otodock/nextcloud').tagName).toBe('SPAN')
  })

  it('a declared change reads as such and a cancelled confirm posts nothing', async () => {
    const calls: Call[] = []
    mockApi(sourceState(true), calls)
    confirmAnswers(false)
    renderPage()
    await expandRow()
    expect(await screen.findByText(/declares that it replaces/)).toBeInTheDocument()
    expect(screen.queryByText(/possible compromise/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Switch' }))
    expect(calls).toEqual([])
  })

  it('a failed attempt is shown on the pending card', async () => {
    const calls: Call[] = []
    const state = sourceState(false)
    state.updates.nextcloud.source_change = change({ result: { error: 'npm said no', failed_at: '2026-10-01T10:05:00Z' } })
    mockApi(state, calls)
    renderPage()
    await expandRow()
    expect(await screen.findByText(/Last attempt failed/)).toBeInTheDocument()
    expect(screen.getByText(/npm said no/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Switch' })).toBeEnabled()
  })

  it('a finished switch shows its result, is not counted, and is dismissed', async () => {
    const calls: Call[] = []
    const state = {
      updates: {
        nextcloud: {
          current: '1.0.0', latest: '1.0.0', registry: 'npm', package: 'nextcloud', reason: 'switched',
          source_change: change({
            status: 'switched',
            result: { version: '1.0.0', renamed: { NEXTCLOUD_USER: 'NEXTCLOUD_USERNAME' }, kept: [], reconnect_needed: ['NEXTCLOUD_TOKEN'], oauth: 'none', container_started: null, switched_at: '2026-10-01T10:10:00Z' },
          }),
        },
      },
      checked: 0, checked_at: '2026-10-01T10:00:00Z',
    }
    mockApi(state, calls)
    renderPage()
    await screen.findByText('Nextcloud')
    expect(screen.queryByText(/update/)).not.toBeInTheDocument()
    await expandRow()
    expect(await screen.findByText(/Switched to/)).toBeInTheDocument()
    expect(screen.getByText('NEXTCLOUD_TOKEN')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(calls).toEqual([{ url: '/v1/admin/mcps/nextcloud/source-change', method: 'DELETE' }]))
  })

  it('a finished switch whose later half failed shows the recorded error', async () => {
    const state = {
      updates: {
        nextcloud: {
          current: '1.0.0', latest: '1.0.0', registry: 'npm', package: 'nextcloud', reason: 'switched',
          source_change: change({
            status: 'switched',
            result: { version: '1.0.0', renamed: {}, kept: [], reconnect_needed: [], oauth: 'none', container_started: null, switched_at: '2026-10-01T10:10:00Z', error: 'the old venv could not be removed' },
          }),
        },
      },
      checked: 0, checked_at: '2026-10-01T10:00:00Z',
    }
    mockApi(state, [])
    renderPage()
    await screen.findByText('Nextcloud')
    await expandRow()
    expect(await screen.findByText(/Switched to/)).toBeInTheDocument()
    expect(screen.getByText(/Finished with an error: the old venv could not be removed/)).toBeInTheDocument()
  })

  it('an install ahead of the catalog is a note with an explicit revert, never an offer', async () => {
    const calls: Call[] = []
    const state = {
      updates: { nextcloud: { current: '2.0.0', latest: '1.0.0', registry: 'npm', package: 'nextcloud', reason: 'ahead' } },
      checked: 1, checked_at: '2026-10-01T10:00:00Z',
    }
    mockApi(state, calls)
    confirmAnswers(true)
    renderPage()
    await screen.findByText('Nextcloud')
    expect(screen.queryByText('1 update')).not.toBeInTheDocument()
    expect(screen.queryByText(/available/)).not.toBeInTheDocument()
    await expandRow()
    expect(await screen.findByText(/ahead of the catalog/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Revert to catalog 1.0.0' }))
    await waitFor(() => expect(calls).toEqual([{ url: '/v1/admin/mcps/nextcloud/update', method: 'POST', body: undefined }]))
  })

  it('Check Updates runs a check and shows its result', async () => {
    const calls: Call[] = []
    mockApi({ updates: {}, checked: 0, checked_at: '' }, calls)
    renderPage()
    await screen.findByText('Nextcloud')
    expect(screen.queryByText('1 update')).not.toBeInTheDocument()
    fetchSpy.mockImplementation(async (url: string) => {
      if (url.endsWith('/v1/admin/mcps')) return { ok: true, json: async () => MCPS } as Response
      if (url.includes('/check-updates')) return { ok: true, json: async () => sourceState(true) } as Response
      return { ok: true, json: async () => ({ updates: {}, checked: 0, checked_at: '' }) } as Response
    })
    fireEvent.click(screen.getByRole('button', { name: /Check Updates/ }))
    expect(await screen.findByText('1 update')).toBeInTheDocument()
    expect(screen.getByText('source changed')).toBeInTheDocument()
  })
})
