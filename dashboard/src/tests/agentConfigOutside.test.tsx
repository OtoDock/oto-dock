import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// The External identities card on the agent's configuration page: the added
// tools with their choice, the person's files, web search, the wrap-up and
// its two rows, the knowledge folders and their tree, the apps and their
// dialog. Each control saves on change and sends only what it changed.

let role = 'admin'
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role, agent_roles: {} } }),
}))

import * as authApi from '@/api/auth'
import { ExternalIdentitiesSection } from '@/pages/agent/AgentConfig.outside'
import { OUTSIDE_ACCESS } from '@/lib/outside'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const PAGE = {
  tools: [
    { mcp: 'crm-mcp', label: 'CRM', access: OUTSIDE_ACCESS.READ_ONLY, functions: [],
      known_functions: [{ name: 'find', reads_only: true }, { name: 'update', reads_only: false }], changed: true },
    { mcp: 'mail-mcp', label: 'Mail', access: OUTSIDE_ACCESS.CUSTOM, functions: ['list', 'read'],
      known_functions: null, changed: false },
    { mcp: 'calendar-mcp', label: 'Calendar', access: '', functions: [], known_functions: null, changed: false },
    { mcp: 'notes-mcp', label: 'Notes', access: '', functions: [], known_functions: null, changed: false },
  ],
  files: OUTSIDE_ACCESS.READ_ONLY,
  web: false,
  knowledge: ['faq'],
  knowledge_choices: ['faq', 'projects', 'shared/lib-a'],
  wrapup_enabled: true,
  shared_notes: false,
  wrapup_model: '',
  wrapup_models: [{ id: 'claude-haiku-4-5', label: 'Haiku 4.5' }],
  apps: [{ id: 'app-1', slug: 'booking', title: 'Booking', methods: ['book'], changed: false, problem: '' }],
  app_choices: [
    { id: 'app-1', slug: 'booking', title: 'Booking', methods: ['book'] },
    { id: 'app-2', slug: 'board', title: 'Team board', methods: [] },
    { id: 'app-3', slug: 'orders', title: 'Orders', methods: ['status'] },
  ],
}

const dir = (name: string, path: string, children: unknown[] = []) =>
  ({ name, type: 'dir', path, size: 0, modified: '', children })
const TREE = [
  dir('knowledge', 'knowledge', [
    dir('faq', 'knowledge/faq'),
    dir('memory', 'knowledge/memory'),
    dir('projects', 'knowledge/projects', [dir('alpha', 'knowledge/projects/alpha'), dir('beta', 'knowledge/projects/beta')]),
    dir('shared', 'knowledge/shared', [dir('lib-a', 'knowledge/shared/lib-a', [dir('guides', 'knowledge/shared/lib-a/guides')])]),
  ]),
  dir('workspace', 'workspace'),
]

type Call = { path: string; method: string; body: unknown }

/** A server that answers each change with the page it makes (only the
 *  fields these tests change). */
function serve(page: Record<string, unknown> = PAGE) {
  const calls: Call[] = []
  let state = { ...page } as Record<string, unknown> & { knowledge: string[] }
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    calls.push({ path, method, body })
    if (path.endsWith('/files')) return { ok: true, status: 200, json: async () => ({ tree: TREE }) } as Response
    if (method === 'PUT') {
      const { remove_knowledge: gone = [], add_knowledge: more = [], add_apps: addApps = [], remove_apps: dropApps = [],
        ...rest } = body
      const scalars = Object.fromEntries(Object.entries(rest).filter(([k]) => !['tools', 'remove_tools'].includes(k)))
      const choices = state.app_choices as { id: string }[]
      const apps = [...(state.apps as { id: string }[]).filter((a) => !dropApps.includes(a.id)),
        ...choices.filter((a) => addApps.includes(a.id)).map((a) => ({ ...a, changed: false, problem: '' }))]
      state = { ...state, ...scalars, apps, knowledge: [...state.knowledge.filter((k) => !gone.includes(k)), ...more] }
    }
    return { ok: true, status: 200, json: async () => state } as Response
  })
  return calls
}

const puts = (calls: Call[]) => calls.filter((c) => c.method === 'PUT').map((c) => c.body)

function renderCard(entry = '/agents/dev/config') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/agents/:name/config" element={<ExternalIdentitiesSection name="dev" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ExternalIdentitiesSection', () => {
  afterEach(() => { fetchSpy.mockReset(); role = 'admin' })

  it('says what a caller gets in a few short lines, and lists only the added tools', async () => {
    serve()
    renderCard()
    expect(await screen.findByText('External identities')).toBeInTheDocument()
    expect(screen.getByText(/A caller on this agent's phone line gets nothing until you pick it here\./)).toBeInTheDocument()
    expect(screen.getByText(/After each conversation, the agent remembers what happened\./)).toBeInTheDocument()
    const tools = screen.getAllByTestId('outside-tool')
    expect(tools.map((t) => within(t).getByRole('combobox').id)).toEqual(['outside-crm-mcp', 'outside-mail-mcp'])
    expect(screen.queryByText('Calendar')).toBeNull()
    expect(screen.getByText('Read only tools are used.')).toBeInTheDocument()
    expect(screen.getByText('Changed')).toBeInTheDocument()
    expect(screen.getByText("Read and write lets the agent save files during the conversation.")).toBeInTheDocument()
    expect(screen.getByText("The engine's own web search and page reading.")).toBeInTheDocument()
    expect(screen.queryByText(/function/i)).toBeNull()
    expect(screen.queryByText(/Direct LLM/)).toBeNull()
  })

  it('reads no tools when none is added', async () => {
    serve({ ...PAGE, tools: PAGE.tools.map((t) => ({ ...t, access: '', functions: [] })) })
    renderCard()
    expect(await screen.findByText('No tools are used in conversations.')).toBeInTheDocument()
  })

  it('adds the ticked tools as Read only, in one request naming them alone', async () => {
    const calls = serve()
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Add tools' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add tools' })
    expect(within(dialog).getByText('Each tool you add starts as Read only.')).toBeInTheDocument()
    expect(within(dialog).queryByText('CRM')).toBeNull()
    expect(within(dialog).getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.click(within(dialog).getByLabelText('Calendar'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(puts(calls)).toEqual([
      { tools: [{ mcp: 'calendar-mcp', access: OUTSIDE_ACCESS.READ_ONLY, functions: [] }] },
    ]))
    expect(calls.find((c) => c.method === 'PUT')!.path).toBe('/v1/agents/dev/outside')
  })

  it('changes and removes a tool, each alone', async () => {
    const calls = serve()
    renderCard()
    fireEvent.change(await screen.findByLabelText('CRM'), { target: { value: OUTSIDE_ACCESS.READ_WRITE } })
    expect(await screen.findByText('Read and write tools are used.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove Mail' }))
    await waitFor(() => expect(puts(calls)).toEqual([
      { tools: [{ mcp: 'crm-mcp', access: OUTSIDE_ACCESS.READ_WRITE, functions: [] }] },
      { remove_tools: ['mail-mcp'] },
    ]))
  })

  it('shows a custom list as Custom, which a change elsewhere never sends, and can switch it back', async () => {
    const calls = serve()
    renderCard()
    const mail = await screen.findByLabelText<HTMLSelectElement>('Mail')
    expect(mail.value).toBe(OUTSIDE_ACCESS.CUSTOM)
    expect(screen.getByText('The agent set which parts of this tool are used.')).toBeInTheDocument()
    expect(Array.from(screen.getByLabelText<HTMLSelectElement>('CRM').options).map((o) => o.value))
      .toEqual([OUTSIDE_ACCESS.READ_ONLY, OUTSIDE_ACCESS.READ_WRITE])
    fireEvent.click(screen.getByRole('switch', { name: 'Web search' }))
    fireEvent.change(mail, { target: { value: OUTSIDE_ACCESS.READ_ONLY } })
    await waitFor(() => expect(puts(calls)).toEqual([
      { web: true },
      { tools: [{ mcp: 'mail-mcp', access: OUTSIDE_ACCESS.READ_ONLY, functions: [] }] },
    ]))
  })

  it('warns when Read only gives nothing of a tool', async () => {
    serve({ ...PAGE, tools: [{ ...PAGE.tools[0], known_functions: [{ name: 'update', reads_only: false }] }] })
    renderCard()
    expect(await screen.findByText('This tool has nothing that only reads, so nothing is used.')).toBeInTheDocument()
  })

  it('saves the files choice alone', async () => {
    const calls = serve()
    renderCard()
    fireEvent.change(await screen.findByLabelText("This person's files"), { target: { value: OUTSIDE_ACCESS.READ_WRITE } })
    await waitFor(() => expect(puts(calls)).toEqual([{ files: OUTSIDE_ACCESS.READ_WRITE }]))
  })

  it('turns the wrap-up off, which hides its two rows and the memory line', async () => {
    const calls = serve()
    renderCard()
    const wrapup = await screen.findByRole('switch', { name: 'Remember each conversation' })
    expect(wrapup).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('The agent runs once more after each conversation.')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Notes for the team' })).toBeInTheDocument()
    expect(screen.getByLabelText('Model after a conversation')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'About Remember each conversation' }), { detail: 0 })
    expect(screen.getByRole('tooltip')).toHaveTextContent(
      "Off, nothing runs after a conversation with an external identity. Calls on the agent's own line are still written down.")
    fireEvent.click(wrapup)
    await waitFor(() => expect(puts(calls)).toEqual([{ wrapup_enabled: false }]))
    expect(screen.queryByRole('switch', { name: 'Notes for the team' })).toBeNull()
    expect(screen.queryByLabelText('Model after a conversation')).toBeNull()
    expect(screen.queryByText(/the agent remembers what happened/)).toBeNull()
  })

  it('reads a saved model the engine no longer lists, and saves a new one alone', async () => {
    const calls = serve({ ...PAGE, wrapup_model: 'old-model-1' })
    renderCard()
    const select = await screen.findByLabelText<HTMLSelectElement>('Model after a conversation')
    expect(select.value).toBe('old-model-1')
    fireEvent.change(select, { target: { value: 'claude-haiku-4-5' } })
    fireEvent.click(screen.getByRole('switch', { name: 'Notes for the team' }))
    await waitFor(() => expect(puts(calls)).toEqual([{ wrapup_model: 'claude-haiku-4-5' }, { shared_notes: true }]))
  })

  it('adds a subfolder from the tree, a folder inside a ticked one read with it', async () => {
    const calls = serve()
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Add folders' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add folders' })
    const tree = await within(dialog).findByRole('listbox', { name: 'Knowledge folders' })
    const names = within(tree).getAllByRole('option').map((o) => o.textContent)
    expect(names).toEqual(['faq', 'projects', 'projects/alpha', 'projects/beta', 'shared/lib-a', 'shared/lib-a/guides'])
    const faq = within(tree).getByRole('option', { name: 'faq' })
    expect(faq).toHaveAttribute('aria-disabled', 'true')
    expect(faq).toHaveAttribute('title', 'This folder is added.')
    fireEvent.click(within(tree).getByRole('option', { name: 'projects/alpha' }))
    fireEvent.click(within(tree).getByRole('option', { name: 'shared/lib-a/guides' }))
    // Ticking the parent drops the child it now covers.
    fireEvent.click(within(tree).getByRole('option', { name: 'projects' }))
    const alpha = within(tree).getByRole('option', { name: 'projects/alpha' })
    expect(alpha).toHaveAttribute('aria-selected', 'true')
    expect(alpha).toHaveAttribute('title', 'Its parent folder is added.')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(puts(calls)).toEqual([{ add_knowledge: ['shared/lib-a/guides', 'projects'] }]))
  })

  it('removes a folder alone', async () => {
    const calls = serve()
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove faq' }))
    await waitFor(() => expect(puts(calls)).toEqual([{ remove_knowledge: ['faq'] }]))
    expect(await screen.findByText('No folders are read in conversations.')).toBeInTheDocument()
  })

  it('says in its InfoTips that a link reads the folders added here', async () => {
    serve()
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'About External identities' }))
    expect(screen.getByRole('tooltip').textContent).toContain("Visitors of a link shared from an app's menu are external identities too.")
    fireEvent.click(screen.getByRole('button', { name: 'About Apps' }))
    const tip = screen.getByRole('tooltip').textContent ?? ''
    expect(tip).toContain('This holds for its shared links too.')
    expect(tip).toContain('Removing that folder stops those links.')
    expect(document.body.textContent).not.toContain('works on its own')
  })

  it('lists the added apps and adds others alone from the dialog', async () => {
    const calls = serve()
    renderCard()
    const rows = await screen.findAllByTestId('outside-app')
    expect(rows.map((r) => r.textContent)).toEqual(['Booking'])
    expect(screen.getByText('Only the apps you add are used in conversations.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add apps' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add apps' })
    expect(within(dialog).getByText('Each app gives only what it opens to external identities.')).toBeInTheDocument()
    // Booking is added already; the board opens nothing yet but can be added.
    expect(within(dialog).queryByLabelText('Booking')).toBeNull()
    expect(within(dialog).getByText('Opens nothing to external identities yet.')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.click(within(dialog).getByLabelText('Team board'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(puts(calls)).toEqual([{ add_apps: ['app-2'] }]))
    expect((await screen.findAllByTestId('outside-app')).map((r) => r.textContent)).toContain('Team board')
  })

  it('removes an app alone and says when none is used', async () => {
    const calls = serve()
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove Booking' }))
    await waitFor(() => expect(puts(calls)).toEqual([{ remove_apps: ['app-1'] }]))
    expect(await screen.findByText('No apps are used in conversations.')).toBeInTheDocument()
  })

  it('shows why an added app gives nothing yet, and a release that changed what it offers', async () => {
    serve({ ...PAGE, apps: [{ id: 'app-2', slug: 'board', title: 'Team board', methods: [], changed: true,
      problem: 'This app opens nothing to external identities yet.' }] })
    renderCard()
    expect(await screen.findByText('This app opens nothing to external identities yet.')).toBeInTheDocument()
    const row = screen.getByTestId('outside-app')
    expect(within(row).getByText('Changed')).toHaveAttribute('title', 'A new release changed what this app opens to external identities.')
  })

  it('scrolls into view when a notice links to it', async () => {
    serve()
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    renderCard('/agents/dev/config#external-identities')
    await screen.findByText('External identities')
    await waitFor(() => expect(scroll).toHaveBeenCalled())
  })

  it('shows nothing to everyone but a manager, without a request', async () => {
    role = 'member'
    serve()
    const { container } = renderCard()
    expect(container).toBeEmptyDOMElement()
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
