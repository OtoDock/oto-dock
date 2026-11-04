import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Admin MCP Servers page: an update runs as a job ───
//
// Update starts the job and answers at once; the page reads the job back
// from the update state, polling while one runs. The row shows the job's
// sentence where its button was, its controls wait, the restart note hides,
// and the end refreshes the list and shows the log. A page opened mid-job
// shows the job at once.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/admin/McpInstanceManager', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import McpServersPage from '@/pages/admin/McpServersPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const CAMOUFOX = {
  name: 'camoufox', label: 'Camoufox', description: 'Browser', version: '0.0.77',
  category: 'community', author: '', author_url: '', icon: false, runtime: 'docker',
  transport: 'http', source: 'docker:camoufox', enabled: true, can_disable: true,
  patched: false, patch_note: '', credential_type: 'none', credential_label: null, skills: [],
  config_fields: [], config_values: {}, assignment_mode: 'auto', agents: [],
  tool_filter_supported: false, tool_filter_arg_name: '', tool_filter_regex: '',
  docker_status: 'running', docker_image_stale: true,
}

const OFFER = { camoufox: { current: '0.0.76', latest: '0.0.77', registry: 'docker', package: 'camoufox' } }
const DOWNLOADING = { phase: 'downloading', detail: 'Downloading the new image, 1.2 GB so far.', started_at: '2026-10-29T16:03:00Z' }

type State = Record<string, unknown>
type Call = { url: string; method: string }

function mockApi(states: () => State, calls: Call[], post?: () => Response, check?: State, row = CAMOUFOX) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    calls.push({ url, method })
    if (method === 'POST') return post ? post() : new Response(JSON.stringify({ status: 'started', name: 'camoufox' }), { status: 202 })
    if (url.endsWith('/v1/admin/mcps')) return { ok: true, json: async () => ({ mcps: [row] }) } as Response
    if (check && url.includes('/check-updates')) return { ok: true, json: async () => check } as Response
    if (url.includes('/update-state')) {
      return { ok: true, json: async () => ({ checked: 1, checked_at: '2026-10-29T03:00:00Z', ...states() }) } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

function renderPage(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpServersPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const listReads = (calls: Call[]) => calls.filter(c => c.method === 'GET' && c.url.endsWith('/v1/admin/mcps')).length
const stateReads = (calls: Call[]) => calls.filter(c => c.url.includes('/update-state')).length
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

describe('McpServersPage — an update job', () => {
  afterEach(() => fetchSpy.mockReset())

  it('a page opened mid-job shows the job, its controls wait and the restart note hides', async () => {
    const calls: Call[] = []
    mockApi(() => ({ updates: OFFER, running: { camoufox: DOWNLOADING }, finished: {} }), calls)
    renderPage()
    fireEvent.click(await screen.findByText('Camoufox'))
    const progress = await screen.findByText(DOWNLOADING.detail)
    expect(progress.closest('[role="status"]')).not.toBeNull()
    expect(screen.getByText('Updating')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Update' })).toBeNull()
    for (const name of ['Start', 'Stop', 'Restart', 'Delete']) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }
    expect(screen.getByRole('switch', { name: 'Camoufox enabled' })).toBeDisabled()
    expect(screen.queryByText(/restart to switch/)).toBeNull()
    expect(screen.queryByText(/The container still runs the previous/)).toBeNull()
  })

  it("the tool filter's Save waits for the job too", async () => {
    const calls: Call[] = []
    mockApi(() => ({ updates: OFFER, running: { camoufox: DOWNLOADING }, finished: {} }), calls, undefined, undefined,
      { ...CAMOUFOX, tool_filter_supported: true, tool_filter_arg_name: 'tools' })
    renderPage()
    fireEvent.click(await screen.findByText('Camoufox'))
    await screen.findByText(DOWNLOADING.detail)
    fireEvent.change(screen.getByPlaceholderText('^(mail|calendar)_.*'), { target: { value: 'mail' } })
    expect(screen.getByRole('button', { name: 'Save filter' })).toBeDisabled()
  })

  it('follows the job to its end: the list refreshes, the offer clears and the log shows', async () => {
    const calls: Call[] = []
    let ended = false
    mockApi(() => ended
      ? { updates: {}, running: {}, finished: { camoufox: { status: 'done', log: 'Updated to v0.0.77. The container runs the new version.', ended_at: '2026-10-29T16:10:02Z' } } }
      : { updates: OFFER, running: { camoufox: DOWNLOADING }, finished: {} }, calls)
    renderPage()
    fireEvent.click(await screen.findByText('Camoufox'))
    await screen.findByText(DOWNLOADING.detail)
    const before = listReads(calls)
    ended = true
    // The next poll (every two seconds while a job runs) reads the end.
    await waitFor(() => expect(screen.getByText(/The container runs the new version\./)).toBeInTheDocument(), { timeout: 5000 })
    expect(screen.queryByText('Update Available')).toBeNull()
    expect(screen.queryByText(DOWNLOADING.detail)).toBeNull()
    await waitFor(() => expect(listReads(calls)).toBeGreaterThan(before))
  })

  it('stops polling once the job ended', async () => {
    const calls: Call[] = []
    let ended = false
    mockApi(() => ended
      ? { updates: {}, running: {}, finished: { camoufox: { status: 'done', log: 'Updated to v0.0.77.', ended_at: '2026-10-29T16:10:02Z' } } }
      : { updates: OFFER, running: { camoufox: DOWNLOADING }, finished: {} }, calls)
    renderPage()
    fireEvent.click(await screen.findByText('Camoufox'))
    await screen.findByText(DOWNLOADING.detail)
    ended = true
    await waitFor(() => expect(screen.getByText('Updated to v0.0.77.')).toBeInTheDocument(), { timeout: 5000 })
    // The end's own refresh lands, then nothing more for longer than a poll.
    await sleep(300)
    const settled = stateReads(calls)
    await sleep(2500)
    expect(stateReads(calls)).toBe(settled)
  }, 15000)

  it('a check run while the job runs keeps the job', async () => {
    const calls: Call[] = []
    const check = { updates: { ...OFFER, other: { current: '1', latest: '2', registry: 'npm', package: 'other' } },
      checked: 2, checked_at: '2026-10-29T16:05:00Z' }
    mockApi(() => ({ updates: OFFER, running: { camoufox: DOWNLOADING }, finished: {} }), calls, undefined, check)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderPage(qc)
    fireEvent.click(await screen.findByText('Camoufox'))
    await screen.findByText(DOWNLOADING.detail)
    fireEvent.click(screen.getByRole('button', { name: /Check Updates/ }))
    // The moment the check's answer is copied over the state, before any poll.
    await waitFor(() => expect(qc.getQueryData<{ checked_at: string }>(['mcp-update-state'])?.checked_at).toBe(check.checked_at))
    expect(qc.getQueryData<{ running?: Record<string, unknown> }>(['mcp-update-state'])?.running?.camoufox).toBeDefined()
    expect(screen.getByText(DOWNLOADING.detail)).toBeInTheDocument()
  })

  it('a failed job keeps the offer and opens its log', async () => {
    const calls: Call[] = []
    mockApi(() => ({
      updates: OFFER, running: {},
      finished: { camoufox: { status: 'failed', log: 'The new version is installed but its container did not start. Press Update to try again.', ended_at: '2026-10-29T16:10:02Z' } },
    }), calls)
    renderPage()
    fireEvent.click(await screen.findByText('Camoufox'))
    expect(await screen.findByText(/Error: The new version is installed but its container did not start/)).toBeVisible()
    expect(screen.getByText('Update Available')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update' })).toBeEnabled()
  })

  it('Update starts the job and the row follows it at once', async () => {
    const calls: Call[] = []
    let started = false
    mockApi(() => started
      ? { updates: OFFER, running: { camoufox: { phase: 'pulling', detail: 'Looking for the new image.', started_at: 'x' } }, finished: {} }
      : { updates: OFFER, running: {}, finished: {} }, calls,
    () => { started = true; return new Response(JSON.stringify({ status: 'started', name: 'camoufox' }), { status: 202 }) })
    renderPage()
    fireEvent.click(await screen.findByText('Camoufox'))
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    expect(await screen.findByText('Looking for the new image.')).toBeInTheDocument()
    expect(calls.filter(c => c.method === 'POST').map(c => c.url)).toEqual(['/v1/admin/mcps/camoufox/update'])
  })

  it('a second press elsewhere shows the server sentence', async () => {
    const calls: Call[] = []
    mockApi(() => ({ updates: OFFER, running: {}, finished: {} }), calls,
      () => new Response(JSON.stringify({ detail: 'An update of this MCP is already running.' }), { status: 409 }))
    renderPage()
    fireEvent.click(await screen.findByText('Camoufox'))
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    const log = await screen.findByText(/Error: An update of this MCP is already running\./)
    expect(within(log.closest('details') as HTMLElement).getByText('Install log')).toBeInTheDocument()
  })
})
