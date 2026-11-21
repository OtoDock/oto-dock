/**
 * The "Internal network access" card on an MCP's row (Admin, MCP Servers):
 * the homelab tool's carve and the platform browser's own exit share it, the
 * browser's off by default with its own words, and a hosted install shows
 * none (the server refuses it and reads it as off).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderWithDialogs } from './dialogsHarness'

const auth = vi.hoisted(() => ({ cloud: false }))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: { cloud: auth.cloud }, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/admin/McpInstanceManager', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import McpServersPage from '@/pages/admin/McpServersPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const ROW = {
  description: '', version: '0.0.78', category: 'community', author: 'OtoDock', author_url: '',
  icon: false, runtime: 'docker', transport: 'http', source: '', enabled: true, can_disable: true,
  patched: false, patch_note: '', credential_type: 'none', credential_label: '', skills: [],
  config_fields: [], config_values: {}, assignment_mode: 'auto', agents: [],
  tool_filter_supported: false, tool_filter_arg_name: '', tool_filter_regex: '',
}

function mockApi(mcp: Record<string, unknown>, calls: { url: string; body?: unknown }[]) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (url.endsWith('/v1/admin/mcps')) return ok({ mcps: [{ ...ROW, ...mcp }] })
    if (url.endsWith('/update-state')) return ok({ updates: {}, checked: 0, checked_at: '' })
    if (url.includes('/network-access')) return ok({ status: 'ok', network_access: true, restarted: true })
    return ok({})
  })
}

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<MemoryRouter><QueryClientProvider client={qc}><McpServersPage /></QueryClientProvider></MemoryRouter>)
}

afterEach(() => { fetchSpy.mockReset(); auth.cloud = false })

describe('the internal network access card', () => {
  it('lets an admin open the browser to the local network, off by default', async () => {
    const calls: { url: string; body?: unknown }[] = []
    mockApi({ name: 'camoufox', label: 'Browser (Camoufox)', has_network_exit: true, network_access: false }, calls)
    wrap()
    fireEvent.click(await screen.findByText('Browser (Camoufox)'))
    const box = await screen.findByRole('checkbox', { name: 'Let the browser open pages on your local network' })
    expect((box as HTMLInputElement).checked).toBe(false)
    expect(screen.getByText('Local network')).toBeInTheDocument()
    expect(screen.getByText('Changing this restarts the browser. Callers and outside people lose it while it is on.')).toBeInTheDocument()
    fireEvent.click(box)
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/v1/admin/mcps/camoufox/network-access')
      && (c.body as { enabled: boolean }).enabled === true)).toBe(true))
  })

  it('keeps the homelab tool its own words', async () => {
    mockApi({ name: 'prom', label: 'Prometheus', has_network_targets: true, network_access: true }, [])
    wrap()
    fireEvent.click(await screen.findByText('Prometheus'))
    expect(await screen.findByRole('checkbox', { name: 'Allow this MCP to reach its configured address on your network' })).toBeChecked()
    expect(screen.getByText('Internal network access')).toBeInTheDocument()
    // The switch governs the platform's sandbox alone: a paired computer's
    // sessions run on that computer's network, and the tip says so.
    fireEvent.click(screen.getByRole('button', { name: 'About Allow this MCP to reach its configured address on your network' }))
    // The pinned tip shows the text in its bubble and reads it to the live region.
    expect(await screen.findAllByText("Agents that run on the platform cannot reach local addresses by default. Tick this to let this tool reach its address. Its agents then run on the platform's own compute machine.")).not.toHaveLength(0)
  })

  it('shows nothing on a hosted install', async () => {
    auth.cloud = true
    mockApi({ name: 'camoufox', label: 'Browser (Camoufox)', has_network_exit: true, network_access: false }, [])
    wrap()
    fireEvent.click(await screen.findByText('Browser (Camoufox)'))
    await screen.findAllByText('Browser (Camoufox)')
    expect(screen.queryByText('Internal network access')).not.toBeInTheDocument()
    expect(screen.queryByText('Local network')).not.toBeInTheDocument()
  })
})
