import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Admin MCP Requests page: the needs-instance state (2026-08-16) ───
//
// A request whose only blocker is admin instance work is NOT a failure:
// the row renders an amber "Needs instance" chip instead of the red
// "Install failed", its retry button reads "Assign instance…" and routes
// through the resolve dialog, where an instance selector (or, with zero
// instances, create-instance guidance) is offered. The chosen instance
// rides the approve POST as `instance_id`.

import * as authApi from '@/api/auth'
import McpRequestsPage from '@/pages/admin/McpRequestsPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const baseRow = {
  requested_by: 'u1', requested_by_name: 'Alice', requested_by_email: null,
  reason: '', admin_note: '', install_log: 'Create an instance via Admin.',
  batch_id: null, created_at: '2026-08-16T10:00:00Z',
  updated_at: '2026-08-16T10:00:00Z', resolved_at: null,
  resolved_by: null, resolved_by_name: null, resolved_by_email: null,
  kind: 'mcp', assignment_mode: 'explicit', instance_count: 0,
}

function requestsPayload(instanceCount: number) {
  return {
    pending_count: 0,
    requests: [{
      ...baseRow, id: 7, mcp_name: 'prometheus', agent_slug: 'dev',
      status: 'install_failed', needs_instance: true,
      instance_count: instanceCount,
    }],
  }
}

const INSTANCES = {
  instances: [
    { id: 11, mcp_name: 'prometheus', instance_name: 'first', field_values: {}, agents: [], assigned_to_all: false },
    { id: 12, mcp_name: 'prometheus', instance_name: 'second', field_values: {}, agents: [], assigned_to_all: false },
  ],
  fields: [], delivery: 'env', max_instances: 5,
}

function mockApi(instanceCount: number, posts: { url: string; body: unknown }[]) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      posts.push({ url, body: JSON.parse(String(init.body)) })
      return { ok: true, json: async () => ({}) } as Response
    }
    if (url.includes('/instances')) {
      return {
        ok: true,
        json: async () => (instanceCount > 0 ? INSTANCES : { ...INSTANCES, instances: [] }),
      } as Response
    }
    return { ok: true, json: async () => requestsPayload(instanceCount) } as Response
  })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpRequestsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('McpRequestsPage — needs-instance flow', () => {
  afterEach(() => fetchSpy.mockReset())

  it('renders the amber chip, routes retry through the dialog, and posts the chosen instance_id', async () => {
    const posts: { url: string; body: unknown }[] = []
    mockApi(2, posts)
    renderPage()

    // The state is presented as needing an instance, not as a failure.
    expect(await screen.findByText('Needs instance')).toBeInTheDocument()
    expect(screen.queryByText('Install failed')).not.toBeInTheDocument()

    // Retry is relabeled and opens the dialog instead of firing blind.
    fireEvent.click(screen.getByText('Assign instance…'))
    expect(await screen.findByText('Approve request')).toBeInTheDocument()

    // Selector defaults to Automatic and lists both instances.
    const select = await screen.findByRole('combobox')
    expect(screen.getByText('Automatic (catch-all or first instance)')).toBeInTheDocument()
    fireEvent.change(select, { target: { value: '12' } })

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].url).toContain('/v1/admin/mcp-requests/7/approve')
    expect(posts[0].body).toMatchObject({ instance_id: 12 })
  })

  it('shows create-instance guidance with the MCP Servers link when no instances exist', async () => {
    mockApi(0, [])
    renderPage()

    fireEvent.click(await screen.findByText('Assign instance…'))
    expect(await screen.findByText(/has no instances yet/)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'MCP Servers' })
    expect(link).toHaveAttribute('href', '/admin/mcp-servers')
    // No selector to render without instances.
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })
})
