import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The What the agent may use card: collapsed until opened, one row per
// enabled tool with the outside column read only and the two columns a
// manager sets, each saving on change and sending only what it changed.

let role = 'admin'
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role, agent_roles: {} } }),
}))

import * as authApi from '@/api/auth'
import { ToolLimitsSection } from '@/pages/agent/AgentConfig.limits'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const PAGE = {
  tools: [
    { mcp: 'mail-mcp', label: 'Mail', outside: { access: 'read_only', functions: [], changed: false },
      runs: { access: 'read_only', functions: [], changed: true }, chats: null,
      known_functions: [{ name: 'read_mail', reads_only: true }, { name: 'send_mail', reads_only: false }] },
    { mcp: 'crm-mcp', label: 'CRM', outside: null, runs: null,
      chats: { access: 'custom', functions: ['find'], changed: false }, known_functions: null },
  ],
}

type Call = { path: string; method: string; body: unknown }

function serve(page: Record<string, unknown> = PAGE) {
  const calls: Call[] = []
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    calls.push({ path, method, body })
    return { ok: true, status: 200, json: async () => page } as Response
  })
  return calls
}

const puts = (calls: Call[]) => calls.filter((c) => c.method === 'PUT').map((c) => c.body)

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><ToolLimitsSection name="dev" /></QueryClientProvider>)
}

async function open() {
  fireEvent.click(await screen.findByRole('button', { name: 'What the agent may use' }))
}

describe('ToolLimitsSection', () => {
  afterEach(() => { fetchSpy.mockReset(); role = 'admin' })

  it('is collapsed until opened, then lists each tool with its three columns', async () => {
    serve()
    renderCard()
    const head = await screen.findByRole('button', { name: 'What the agent may use' })
    expect(head).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Mail')).toBeNull()
    fireEvent.click(head)
    expect(screen.getByText('Which tools of each MCP the agent may use. All tools unless you limit it.')).toBeInTheDocument()
    const rows = screen.getAllByTestId('limited-tool')
    expect(rows).toHaveLength(2)
    const mail = rows[0]
    expect(within(mail).getByText('Read only')).toBeInTheDocument()
    expect(within(mail).getByText('Changed')).toBeInTheDocument()
    expect((within(mail).getByRole('combobox', { name: "The agent's own runs: Mail" }) as HTMLSelectElement).value).toBe('read_only')
    expect((within(mail).getByRole('combobox', { name: "People's chats: Mail" }) as HTMLSelectElement).value).toBe('all')
    const crm = rows[1]
    expect(within(crm).getByText('Not added')).toBeInTheDocument()
    expect((within(crm).getByRole('combobox', { name: "People's chats: CRM" }) as HTMLSelectElement).value).toBe('some')
    expect(within(crm).getByText('find')).toBeInTheDocument()
    expect(screen.queryByText(/function/i)).toBeNull()
  })

  it('writes one column alone, and a removal for All tools', async () => {
    const calls = serve()
    renderCard()
    await open()
    fireEvent.change(screen.getByRole('combobox', { name: "People's chats: Mail" }), { target: { value: 'read_only' } })
    await waitFor(() => expect(puts(calls)).toHaveLength(1))
    expect(puts(calls)[0]).toEqual({ tools: [{ mcp: 'mail-mcp', chats: { access: 'read_only', functions: [] } }] })
    fireEvent.change(screen.getByRole('combobox', { name: "The agent's own runs: Mail" }), { target: { value: 'all' } })
    await waitFor(() => expect(puts(calls)).toHaveLength(2))
    expect(puts(calls)[1]).toEqual({ remove: [{ mcp: 'mail-mcp', governs: 'runs' }] })
  })

  it('picks some tools in a dialog and saves the ticked names', async () => {
    const calls = serve()
    renderCard()
    await open()
    fireEvent.change(screen.getByRole('combobox', { name: "The agent's own runs: Mail" }), { target: { value: 'some' } })
    const dialog = await screen.findByRole('dialog', { name: "The agent's own runs: Mail" })
    expect(within(dialog).getByText('Only reads.')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByLabelText('read_mail'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts(calls)).toHaveLength(1))
    expect(puts(calls)[0]).toEqual({ tools: [{ mcp: 'mail-mcp', runs: { access: 'custom', functions: ['read_mail'] } }] })
  })

  it('says when a Read only limit has nothing to give yet', async () => {
    serve({ tools: [{ ...PAGE.tools[1], chats: { access: 'read_only', functions: [], changed: false } }] })
    renderCard()
    await open()
    expect(screen.getByText('Its tools are not listed yet. Save a choice and the list is read.')).toBeInTheDocument()
  })

  it('shows nothing to someone who cannot manage the agent', () => {
    role = 'member'
    serve()
    const { container } = renderCard()
    expect(container).toBeEmptyDOMElement()
  })
})
