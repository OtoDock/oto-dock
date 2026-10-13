// The release dialog: an eligible admin sees the headline, both highlight
// lists and the release page; Escape, the backdrop and Close mark the shown
// version seen; a member sees the list for everyone and no link, and nothing
// at all when the release carries nothing for everyone (marked silently
// once); a seen version is left alone; a downgrade keeps its headline with no
// cards; a release without cards says so in one line; a failed mark still
// closes it for the load, and a silent mark is posted once per cached card.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({
  card: {} as Record<string, unknown>,
  requests: [] as { url: string; method: string; body?: string }[],
  highlights: {} as Record<string, { everyone: unknown[]; admins: unknown[] }>,
  postFails: false,
}))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'admin', agent_roles: {} }, authConfig: { cloud: false } }),
}))
vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async (url: string, init?: { method?: string; body?: string }) => {
    h.requests.push({ url, method: init?.method ?? 'GET', body: init?.body })
    if (init?.method === 'POST') {
      if (h.postFails) throw new Error('offline')
      return { ok: true, json: async () => ({ status: 'ok' }) }
    }
    return { ok: true, json: async () => h.card }
  }),
}))
vi.mock('@/release/highlights', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/release/highlights')>()
  return { ...mod, highlightsFor: (version: string) => h.highlights[version] ?? { everyone: [], admins: [] } }
})

import { ReleaseDialog } from '@/components/ReleaseDialog'

const EVERYONE = [{ icon: 'picture', title: 'Pictures from tools', text: 'A tool\'s pictures are saved in your workspace.' }]
const ADMINS = [{ icon: 'backup', title: 'Backup by script', text: 'One dated folder holds the install.' }]
const CARD = {
  show_admin: true, eligible: true, version: '1.8.0', previous: '1.7.1', direction: 'up', seen: '',
  release_url: 'https://github.com/OtoDock/oto-dock/releases/tag/v1.8.0',
}

function show(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(<QueryClientProvider client={qc}><ReleaseDialog /></QueryClientProvider>)
}

const posts = () => h.requests.filter((r) => r.method === 'POST').map((r) => r.url)
const postedVersions = () => h.requests.filter((r) => r.method === 'POST').map((r) => JSON.parse(r.body ?? '{}').version)

beforeEach(() => {
  h.card = { ...CARD }
  h.requests = []
  h.highlights = { '1.8.0': { everyone: EVERYONE, admins: ADMINS } }
  h.postFails = false
})

describe('ReleaseDialog', () => {
  it('shows an admin the headline, both lists and the release page, and Close marks the shown version', async () => {
    show()
    const dialog = await screen.findByRole('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(screen.getByRole('heading').textContent).toBe('OtoDock was updated to 1.8.0 (from 1.7.1).')
    expect(screen.getByText('For everyone')).toBeTruthy()
    expect(screen.getByText('Pictures from tools')).toBeTruthy()
    expect(screen.getByText('For admins')).toBeTruthy()
    expect(screen.getByText('Backup by script')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Release notes' }).getAttribute('href')).toBe(CARD.release_url)
    expect(posts()).toEqual([])
    h.card = { ...CARD, eligible: false, show_admin: false, seen: '1.8.0' }
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(posts()).toEqual(['/v1/users/me/release-card/seen']))
    expect(postedVersions()).toEqual(['1.8.0'])
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('Escape and the backdrop dismiss it at once, a click inside does not', async () => {
    const first = show()
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(dialog)
    expect(posts()).toEqual([])
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(posts()).toHaveLength(1))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    first.unmount()
    h.requests = []
    show()
    const again = await screen.findByRole('dialog')
    fireEvent.click(again.parentElement as HTMLElement)
    await waitFor(() => expect(posts()).toHaveLength(1))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('shows a member the list for everyone alone, without a link', async () => {
    h.card = { ...CARD, show_admin: false, release_url: '' }
    show()
    await screen.findByRole('dialog')
    expect(screen.getByText('Pictures from tools')).toBeTruthy()
    expect(screen.queryByText('Backup by script')).toBeNull()
    expect(screen.queryByText('For everyone')).toBeNull()
    expect(screen.queryByRole('link', { name: 'Release notes' })).toBeNull()
  })

  it('a member with nothing for everyone sees nothing and is marked silently, once, whatever was seen before', async () => {
    h.card = { ...CARD, show_admin: false, release_url: '', seen: '1.7.1' }
    h.highlights = { '1.8.0': { everyone: [], admins: ADMINS } }
    const first = show()
    await waitFor(() => expect(posts()).toEqual(['/v1/users/me/release-card/seen']))
    await new Promise((r) => setTimeout(r, 20))
    expect(posts()).toHaveLength(1)
    expect(postedVersions()).toEqual(['1.8.0'])
    expect(screen.queryByRole('dialog')).toBeNull()
    first.unmount()
    h.requests = []
    h.card = { ...CARD, show_admin: false, release_url: '', seen: '' }
    show()
    await waitFor(() => expect(posts()).toEqual(['/v1/users/me/release-card/seen']))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('a person who is not eligible is marked silently once without a seen version, else left alone', async () => {
    h.card = { ...CARD, eligible: false, show_admin: false, seen: '' }
    const first = show()
    await waitFor(() => expect(posts()).toEqual(['/v1/users/me/release-card/seen']))
    first.unmount()
    h.requests = []
    h.card = { ...CARD, eligible: false, show_admin: false, seen: '1.8.0' }
    const second = show()
    await new Promise((r) => setTimeout(r, 30))
    expect(posts()).toEqual([])
    second.unmount()
    // A -dev tree: the server leaves everyone out and the seen version stays what it was.
    h.card = { ...CARD, version: '1.8.0-dev', eligible: false, show_admin: false, seen: '1.7.1' }
    show()
    await new Promise((r) => setTimeout(r, 30))
    expect(posts()).toEqual([])
  })

  it('closes at once even when the seen mark fails; the next load asks again', async () => {
    h.postFails = true
    show()
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(posts()).toHaveLength(1)
  })

  it('a silent mark is posted once for the cached card across remounts', async () => {
    h.card = { ...CARD, show_admin: false, release_url: '', seen: '1.7.1' }
    h.highlights = { '1.8.0': { everyone: [], admins: ADMINS } }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const first = show(qc)
    await waitFor(() => expect(posts()).toHaveLength(1))
    first.unmount()
    show(qc)
    await new Promise((r) => setTimeout(r, 30))
    expect(posts()).toHaveLength(1)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('a release without cards for the viewer says so in one line', async () => {
    h.highlights = {}
    show()
    await screen.findByRole('dialog')
    expect(screen.getByText('Fixes and polish throughout this release.')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Release notes' })).toBeTruthy()
  })

  it('a downgrade keeps its headline with no cards and no link', async () => {
    h.card = { ...CARD, direction: 'down', previous: '1.9.0' }
    show()
    await screen.findByRole('dialog')
    expect(screen.getByRole('heading').textContent).toBe('OtoDock now runs 1.8.0 (it ran 1.9.0 before).')
    expect(screen.queryByText('Pictures from tools')).toBeNull()
    expect(screen.queryByText(/Fixes and polish/)).toBeNull()
    expect(screen.queryByRole('link', { name: 'Release notes' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Close' })).toBeTruthy()
  })
})
