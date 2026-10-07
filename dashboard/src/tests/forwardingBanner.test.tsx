// The admin banner for a reverse-proxy misconfiguration: an admin sees the
// first warning's advice with a link to the Security tab, a member and the
// OtoDock cloud see nothing (the route is not even called), and dismissing
// holds for the tab's session until the set of warnings changes.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({
  role: 'admin',
  cloud: false,
  warnings: [] as unknown[],
  calls: 0,
}))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: h.role, agent_roles: {} }, authConfig: { cloud: h.cloud } }),
}))
vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async () => {
    h.calls += 1
    return { ok: true, json: async () => ({ warnings: h.warnings }) }
  }),
}))

import { ForwardingBanner } from '@/components/admin/ForwardingBanner'

const GATEWAY = { peer: '10.200.0.1', case: 'untrusted_forwarder', gateway: true,
  first_seen: 1790568196, last_seen: 1790568197, count: 3 }
const BOOT = { peer: '10.200.0.1', case: 'no_trusted_proxy', gateway: true,
  first_seen: 1790568196, last_seen: 1790568197, count: 0 }

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><ForwardingBanner /></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  h.role = 'admin'; h.cloud = false; h.warnings = [GATEWAY]; h.calls = 0
  sessionStorage.clear()
})

describe('ForwardingBanner', () => {
  it("names the address to add and links the Security tab", async () => {
    show()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/add 10\.200\.0\.1 to TRUSTED_PROXY in \.env/)
    expect(alert.textContent).toMatch(/PROXY_BIND_IP=127\.0\.0\.1/)
    expect(alert.textContent).not.toMatch(/set TRUSTED_PROXY=/)
    expect(screen.getByRole('link', { name: 'Security settings' }).getAttribute('href'))
      .toBe('/admin/platform?tab=security')
  })

  it('words the boot check and counts the other rows', async () => {
    h.warnings = [BOOT, GATEWAY]
    show()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/public URL is https but no reverse proxy is trusted/)
    // The proxy raises this row for ignored entries too, never only for an empty list.
    expect(alert.textContent).toMatch(/TRUSTED_PROXY is empty, or every entry in it is ignored, such as a loopback address in a container/)
    expect(alert.textContent).toMatch(/1 more in the Security settings/)
  })

  it('shows nothing to a member or on the cloud, without asking', async () => {
    h.role = 'member'
    const { unmount } = show()
    h.cloud = true; h.role = 'admin'
    unmount()
    show()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(h.calls).toBe(0)
  })

  it('stays dismissed until the warnings change', async () => {
    const first = show()
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).toBeNull()
    first.unmount()
    show()
    await waitFor(() => expect(h.calls).toBe(2))
    expect(screen.queryByRole('alert')).toBeNull()
    h.warnings = [GATEWAY, BOOT]
    show()
    expect(await screen.findByRole('alert')).toBeTruthy()
  })
})
