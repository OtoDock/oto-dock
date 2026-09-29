// Admin → Platform → Security: the forwarding warnings the proxy records
// (headers from an untrusted address, a trusted proxy that appends none)
// show as a banner naming the address to trust, and a refused save shows
// the server's reason instead of a generic failure. The container's gateway
// may be trusted only while the port is published on 127.0.0.1 (direct
// connections to a published port arrive from it too), TRUSTED_PROXY is a
// list to add to, and on the OtoDock cloud the admin edits no config file.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  mutate: vi.fn((_data: unknown, opts?: { onError?: (e: unknown) => void }) => {
    opts?.onError?.(new Error('Turn on a second factor for your own account first'))
  }),
  settings: {
    cloud: false, forced_keys: [], require_2fa: false, passkey_login_mode: 'passwordless',
    password_min_score: '3', password_min_length: '8',
    sharing_external_enabled: false, sharing_public_links_enabled: false,
    sharing_max_expiry_days: '', user_directory_visible_to_members: false,
    forwarding_warnings: [
      { peer: '10.200.0.1', case: 'untrusted_forwarder', gateway: true,
        first_seen: 1790568196.1, last_seen: 1790568196.2, count: 3 },
      { peer: '192.168.1.9', case: 'untrusted_forwarder', gateway: false,
        first_seen: 1790568196.1, last_seen: 1790568196.2, count: 2 },
      { peer: '192.168.1.5', case: 'edge_without_xff', gateway: false,
        first_seen: 1790568196.1, last_seen: 1790568196.2, count: 1 },
    ],
  },
}))

vi.mock('@/api/auth', () => ({ apiFetch: vi.fn(async () => ({ ok: true, json: async () => ({}) })) }))
vi.mock('@/api/oauth', () => ({
  useBearerAllowlist: () => ({ data: [], isLoading: false }),
  useAddBearerAllowlist: () => ({ mutateAsync: vi.fn() }),
  useRemoveBearerAllowlist: () => ({ mutate: vi.fn() }),
  useRestoreBearerAllowlist: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/pages/admin/PlatformPage.hooks', () => ({
  usePlatformSettings: () => ({ data: h.settings, isLoading: false }),
  useSavePlatformSettings: () => ({ mutate: h.mutate, isPending: false }),
}))

import SecurityTab from '@/pages/admin/SecurityTab'

afterEach(() => { h.settings.cloud = false })

describe('SecurityTab forwarding warnings and refused saves', () => {
  it('trusts the gateway only with the port published on 127.0.0.1', () => {
    render(<SecurityTab />)
    expect(screen.getByText(new RegExp(
      "Forwarding headers arrive from 10\\.200\\.0\\.1, the container's gateway, which is not "
      + 'a trusted proxy\\. Trust it only with PROXY_BIND_IP=127\\.0\\.0\\.1 \\(the port '
      + "published on this host's loopback only\\): direct connections to a published port "
      + 'arrive from the gateway too\\. Then add 10\\.200\\.0\\.1 to TRUSTED_PROXY in \\.env '
      + '\\(a comma-separated list: that address, never a subnet\\)\\.',
    ))).toBeTruthy()
    // Never the unconditional advice, and never "set", which reads as
    // replacing the list.
    expect(screen.queryByText(/set TRUSTED_PROXY=/)).toBeNull()
  })

  it('names another address to add to the list', () => {
    render(<SecurityTab />)
    const line = screen.getByText(/arrive from 192\.168\.1\.9/)
    expect(line.textContent).toMatch(
      /if it is your reverse proxy, add 192\.168\.1\.9 to TRUSTED_PROXY in config\.env/)
    expect(line.textContent).toMatch(/a comma-separated list: that address, never a subnet/)
    expect(line.textContent).not.toMatch(/PROXY_BIND_IP/)
    expect(screen.getByText(/192\.168\.1\.5 does not append X-Forwarded-For/)).toBeTruthy()
    // The epoch stamp renders as a time, never as a raw number.
    expect(screen.queryByText(/1790568196/)).toBeNull()
  })

  it('gives no config instruction on the OtoDock cloud', () => {
    h.settings.cloud = true
    render(<SecurityTab />)
    expect(screen.getByText(/arrive from 10\.200\.0\.1/)).toBeTruthy()
    expect(screen.queryByText(/TRUSTED_PROXY/)).toBeNull()
    expect(screen.queryByText(/PROXY_BIND_IP/)).toBeNull()
  })

  it('shows the server reason when a save is refused', async () => {
    render(<SecurityTab />)
    fireEvent.click(screen.getAllByRole('switch')[0])
    await waitFor(() => {
      expect(screen.getByText('Turn on a second factor for your own account first')).toBeTruthy()
    })
  })
})
