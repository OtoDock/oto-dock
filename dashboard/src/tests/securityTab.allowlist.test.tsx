// Admin, Security, the OAuth bearer allowlist: a row an MCP's manifest put
// there names the MCP and has no Remove (the MCP's disable or removal takes
// it away), the hosts an installed MCP waits on show with an Allow that sends
// back exactly what is shown, and "Remove custom addresses" asks
// first, saying it keeps the MCPs' rows, and shows only while an admin row
// exists.

import { beforeEach, describe, it, expect, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import { answerDialog, personClick, renderWithDialogs } from './dialogsHarness'

const ADMIN_ROW = { id: 1, provider_id: 'slack', host_pattern: 'mcp.slack.com', added_by: 'system', added_at: '', source: 'admin' }
const MCP_ROW = { id: 2, provider_id: 'facebook', host_pattern: 'mcp.facebook.com', added_by: 'catalog', added_at: '',
                  source: 'meta-ads-mcp' }

const h = vi.hoisted(() => ({
  allow: vi.fn(async (_p: unknown) => ({ status: 'ok' })),
  removeAdmin: vi.fn(async () => ({ entries: [] })),
  remove: vi.fn(async (_id: number) => ({ status: 'ok' })),
  entries: [] as unknown[],
}))

vi.mock('@/api/auth', () => ({ apiFetch: vi.fn(async () => ({ ok: true, json: async () => ({}) })) }))
vi.mock('@/api/oauth', () => ({
  useBearerAllowlist: () => ({ isLoading: false, data: h.entries }),
  useBearerPending: () => ({
    data: [{ mcp_name: 'zip-mcp', mcp_label: 'Zip MCP', provider: 'zipco', hosts: ['api.zipco.example'],
             shared_with: ['Zip Two'], digest: 'abc' }],
  }),
  useAllowMcpHosts: () => ({ mutateAsync: h.allow, isPending: false }),
  useAddBearerAllowlist: () => ({ mutateAsync: vi.fn() }),
  useRemoveBearerAllowlist: () => ({ mutateAsync: h.remove, isPending: false }),
  useRemoveAdminBearerRows: () => ({ mutateAsync: h.removeAdmin, isPending: false }),
}))
vi.mock('@/pages/admin/PlatformPage.hooks', () => ({
  usePlatformSettings: () => ({ data: { forced_keys: [] }, isLoading: false }),
  useSavePlatformSettings: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
}))

import SecurityTab from '@/pages/admin/SecurityTab'

describe('the bearer allowlist on the Security tab', () => {
  beforeEach(() => { h.entries = [ADMIN_ROW, MCP_ROW] })

  it('names the MCP of its own rows and offers no Remove there', () => {
    renderWithDialogs(<SecurityTab />)
    const mcpRow = screen.getByText('mcp.facebook.com').closest('div.flex.items-center.justify-between') as HTMLElement
    expect(within(mcpRow).getByText('Added by meta-ads-mcp')).toBeTruthy()
    expect(within(mcpRow).queryByRole('button', { name: 'Remove' })).toBeNull()
    const adminRow = screen.getByText('mcp.slack.com').closest('div.flex.items-center.justify-between') as HTMLElement
    expect(within(adminRow).getByRole('button', { name: 'Remove' })).toBeTruthy()
  })

  it('allows the waiting hosts exactly as shown', async () => {
    renderWithDialogs(<SecurityTab />)
    expect(screen.getByText(/Zip MCP sends people's zipco sign-in to/)).toBeTruthy()
    expect(screen.getByText('The same sign-in also serves Zip Two.')).toBeTruthy()
    personClick(screen.getByRole('button', { name: 'Allow these addresses' }))
    await waitFor(() => expect(screen.getByText('Allowed.')).toBeTruthy())
    expect(h.allow).toHaveBeenCalledWith(expect.objectContaining({ mcp_name: 'zip-mcp', digest: 'abc',
                                                                    hosts: ['api.zipco.example'] }))
  })

  it('asks before removing the admin rows and says what stays', async () => {
    renderWithDialogs(<SecurityTab />)
    personClick(screen.getByRole('button', { name: 'Remove custom addresses' }))
    expect(await screen.findByText('Custom addresses are removed. Addresses an MCP added stay.')).toBeTruthy()
    await act(async () => { await answerDialog('Remove', /Remove the custom addresses/) })
    await waitFor(() => expect(h.removeAdmin).toHaveBeenCalled())
  })

  it('offers no removal when only MCPs own rows', () => {
    h.entries = [MCP_ROW]
    renderWithDialogs(<SecurityTab />)
    expect(screen.queryByRole('button', { name: 'Remove custom addresses' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Restore defaults' })).toBeNull()
  })
})
