import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Admin MCP Requests page: rows render a kind badge ("MCP" / "Skill")
//     from the request's `kind` column; absent kind defaults to MCP ───

import * as authApi from '@/api/auth'
import McpRequestsPage from '@/pages/admin/McpRequestsPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const baseRow = {
  requested_by: 'u1', requested_by_name: 'Alice', requested_by_email: null,
  reason: '', status: 'pending', admin_note: '', install_log: '',
  batch_id: null, created_at: '2026-07-16T10:00:00Z',
  updated_at: '2026-07-16T10:00:00Z', resolved_at: null,
  resolved_by: null, resolved_by_name: null, resolved_by_email: null,
}

const REQUESTS = {
  pending_count: 3,
  requests: [
    { ...baseRow, id: 1, mcp_name: 'camoufox', agent_slug: 'dev', kind: 'mcp' },
    { ...baseRow, id: 2, mcp_name: 'pdf-skills', agent_slug: 'dev', kind: 'skill' },
    // Legacy row from before the skills feature — no kind column.
    { ...baseRow, id: 3, mcp_name: 'notion-mcp', agent_slug: 'ops' },
  ],
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <McpRequestsPage />
    </QueryClientProvider>,
  )
}

describe('McpRequestsPage — kind badge', () => {
  afterEach(() => fetchSpy.mockReset())

  it('shows a Skill badge for skill requests and MCP otherwise (incl. legacy rows)', async () => {
    fetchSpy.mockImplementation(async () => (
      { ok: true, json: async () => REQUESTS } as Response
    ))
    renderPage()
    expect(await screen.findByText('pdf-skills')).toBeInTheDocument()
    expect(screen.getAllByText('Skill')).toHaveLength(1)
    // kind:'mcp' row + legacy row without kind both badge as MCP.
    expect(screen.getAllByText('MCP')).toHaveLength(2)
  })
})
