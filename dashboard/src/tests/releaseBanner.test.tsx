// The admin banner for a newer release: an admin sees the version, the notes
// link and the upgrading guide; a member and the cloud make no call;
// dismissing holds for that version and a newer one shows again.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({
  role: 'admin',
  cloud: false,
  notice: {} as Record<string, unknown>,
  calls: 0,
}))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: h.role, agent_roles: {} }, authConfig: { cloud: h.cloud } }),
}))
vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async () => {
    h.calls += 1
    return { ok: true, json: async () => h.notice }
  }),
}))

import { ReleaseBanner } from '@/components/admin/ReleaseBanner'

const NEWER = {
  enabled: true, reason: '', running: '1.8.0', latest: '1.9.0',
  url: 'https://github.com/OtoDock/oto-dock/releases/tag/v1.9.0',
  published_at: '2026-10-20T10:00:00Z', checked_at: '2026-10-21T03:00:00+00:00', error: '', newer: true,
}

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><ReleaseBanner /></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  h.role = 'admin'; h.cloud = false; h.notice = { ...NEWER }; h.calls = 0
  localStorage.clear()
})

describe('ReleaseBanner', () => {
  it('names the release with the notes and the upgrading guide', async () => {
    show()
    const status = await screen.findByRole('status')
    expect(status.textContent).toMatch(/OtoDock 1\.9\.0 is available\./)
    expect(status.textContent).toMatch(/This install runs 1\.8\.0\./)
    expect(screen.getByRole('link', { name: 'Release notes' }).getAttribute('href')).toBe(NEWER.url)
    const guide = screen.getByRole('link', { name: 'How to upgrade' })
    expect(guide.getAttribute('href')).toBe('https://docs.otodock.io/administration/upgrading')
    expect(guide.getAttribute('target')).toBe('_blank')
  })

  it('shows nothing when the install is up to date or the check is off', async () => {
    h.notice = { ...NEWER, newer: false, latest: '1.8.0' }
    const { unmount } = show()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByRole('status')).toBeNull()
    unmount()
    h.notice = { ...NEWER, enabled: false, newer: false, latest: '' }
    show()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('shows nothing to a member or on the cloud, without asking', async () => {
    h.role = 'member'
    const { unmount } = show()
    h.cloud = true; h.role = 'admin'
    unmount()
    show()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByRole('status')).toBeNull()
    expect(h.calls).toBe(0)
  })

  it('a dismissed version stays dismissed and a newer one shows again', async () => {
    const first = show()
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('status')).toBeNull()
    expect(localStorage.getItem('otodock.releaseBanner.dismissed')).toBe('1.9.0')
    first.unmount()
    show()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByRole('status')).toBeNull()
    h.notice = { ...NEWER, latest: '1.9.1' }
    show()
    const again = await screen.findByRole('status')
    expect(again.textContent).toMatch(/1\.9\.1 is available/)
  })
})
