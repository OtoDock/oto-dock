import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// ─── Agent Skills tab: standalone skills sit in "Installed skills" with an
//     enable toggle (autosave PATCH); MCP-bundled skills sit in a collapsed
//     section with NO toggle and NO exclusion editing — exclusions render as
//     a passive author-declared hint ───

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunitySkillsBrowser', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import AgentSkills from '@/pages/agent/AgentSkills'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const SKILLS_DATA = {
  skills: [
    {
      id: 'memory-usage', mcp_name: 'memory-mcp', mcp_label: 'Memory',
      description: 'How to use memory well', standalone: false,
      loading: 'always', assigned: true, enabled: true,
      exclude_from: ['phone'], default_exclude_from: ['phone'],
    },
    {
      id: 'pdf-forms', mcp_name: 'pdf-skills', mcp_label: 'PDF Skills',
      description: 'Fill PDF forms', standalone: true,
      loading: 'on_demand', assigned: false, enabled: false,
      exclude_from: ['phone'], default_exclude_from: ['phone'],
    },
  ],
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/agents/dev/skills']}>
        <Routes>
          <Route path="/agents/:name/skills" element={<AgentSkills />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AgentSkills', () => {
  afterEach(() => fetchSpy.mockReset())

  it('splits standalone and bundled skills into sections; bundled collapsed by default', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.includes('/skills')) return { ok: true, json: async () => SKILLS_DATA } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    renderPage()
    // Standalone section renders immediately with the package label + badge.
    expect(await screen.findByText('pdf-forms')).toBeInTheDocument()
    expect(screen.getByText('Installed skills')).toBeInTheDocument()
    expect(screen.getByText('PDF Skills')).toBeInTheDocument()
    expect(screen.getByText('on demand')).toBeInTheDocument()
    // Bundled section is collapsed: header with count, rows hidden.
    expect(screen.getByText("From this agent's MCPs")).toBeInTheDocument()
    expect(screen.queryByText('memory-usage')).not.toBeInTheDocument()

    // Expanding reveals bundled rows — with provenance, no checkbox.
    fireEvent.click(screen.getByText("From this agent's MCPs"))
    expect(await screen.findByText('memory-usage')).toBeInTheDocument()
    expect(screen.getByText('from Memory')).toBeInTheDocument()
    expect(screen.getByText('always in context')).toBeInTheDocument()
    // Exactly ONE checkbox on the page: the standalone skill's toggle.
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
  })

  it('a standalone toggle click PATCHes the skill immediately', async () => {
    const patches: { path: string; body: unknown }[] = []
    fetchSpy.mockImplementation(async (path: string, options?: RequestInit) => {
      if (options?.method === 'PATCH') {
        patches.push({ path, body: JSON.parse(String(options.body)) })
        return { ok: true, json: async () => ({ status: 'saved' }) } as Response
      }
      if (path.includes('/skills')) return { ok: true, json: async () => SKILLS_DATA } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    renderPage()
    const row = await screen.findByText('pdf-forms')

    fireEvent.click(row.closest('label')!.querySelector('input')!)
    await waitFor(() => expect(patches).toHaveLength(1))
    expect(patches[0].path).toBe('/v1/agents/dev/skills/pdf-forms')
    // The author-declared exclusions ride along unchanged.
    expect(patches[0].body).toEqual({ enabled: true, exclude_from: ['phone'] })
    // The Saved flash confirms the write landed without any Save click.
    expect(await screen.findByText('Saved')).toBeInTheDocument()
  })

  it('exclusions are a passive hint — no exclude buttons anywhere', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.includes('/skills')) return { ok: true, json: async () => SKILLS_DATA } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    renderPage()
    await screen.findByText('pdf-forms')
    fireEvent.click(screen.getByText("From this agent's MCPs"))
    await screen.findByText('memory-usage')

    // Both rows show the passive hint; no clickable context chips exist.
    expect(screen.getAllByText('Not loaded in: phone sessions')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'phone' })).not.toBeInTheDocument()
    expect(screen.queryByText('Exclude from:')).not.toBeInTheDocument()
  })

  it('a search query surfaces bundled rows even while collapsed', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.includes('/skills')) return { ok: true, json: async () => SKILLS_DATA } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    renderPage()
    await screen.findByText('pdf-forms')
    expect(screen.queryByText('memory-usage')).not.toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('Search skills…'), { target: { value: 'memory' } })
    expect(await screen.findByText('memory-usage')).toBeInTheDocument()
  })
})
