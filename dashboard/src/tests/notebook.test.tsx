import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { answerDialog, renderWithDialogs } from './dialogsHarness'
import type { NotebookEntry, NotebookPage } from '@/api/notebook'

// ─── The Notebook in the dashboard: the entries newest first with where
//     each was written, the filter by agent, the manager's delete that asks
//     first, older pages, and the System Settings budget saved on leaving
//     the field. ──────────────────────────────────────────────────────────

const h = vi.hoisted(() => ({
  remove: vi.fn(),
  savePlatform: vi.fn(),
  settings: { notebook_inline_bytes: '3072', forced_keys: [] as string[] },
  // The two folds' hook calls: [name or department id, agent filter, enabled].
  reads: [] as [string, string, boolean][],
  query: null as unknown,
}))

vi.mock('@/api/notebook', () => ({
  useDeleteNotebookEntry: () => ({ mutateAsync: h.remove, isPending: false }),
  useAgentNotebook: (name: string, agent: string, enabled: boolean) => { h.reads.push([name, agent, enabled]); return h.query },
  useDepartmentNotebook: (id: string, agent: string, enabled: boolean) => { h.reads.push([id, agent, enabled]); return h.query },
}))
vi.mock('@/pages/admin/PlatformPage.hooks', () => ({
  usePlatformSettings: () => ({ data: h.settings, isLoading: false }),
  useSavePlatformSettings: () => ({ mutateAsync: h.savePlatform, isPending: false }),
}))

import NotebookView from '@/components/notebook/NotebookView'
import AgentNotebookCard from '@/components/notebook/AgentNotebookCard'
import NotebookSection from '@/components/departments/NotebookSection'
import NotebookSettingsCard from '@/pages/admin/NotebookSettingsCard'

const entry = (over: Partial<NotebookEntry>): NotebookEntry => ({
  id: 'e1', agent: 'nb-head', created_at: '2026-10-28T07:10:00+00:00', text: 'Template v3 for every project.',
  surface: 'task', author: '', can_delete: false, ...over,
})

const PAGE: NotebookPage = {
  entries: [
    entry({ id: 'e2', agent: 'nb-alpha', surface: 'chat', author: 'Dev Admin', text: 'Importer landed.', can_delete: true }),
    entry({}),
    entry({ id: 'e3', surface: 'meeting', text: 'We pick the Paros house.' }),
    entry({ id: 'e4', surface: '', text: 'Older kind.' }),
  ],
  more: true,
  agents: [{ slug: 'nb-alpha', display_name: 'Alpha' }, { slug: 'nb-head', display_name: 'Head' }],
}

function fakeQuery(pages: NotebookPage[], over: Record<string, unknown> = {}) {
  return {
    data: { pages, pageParams: [] }, isLoading: false, isError: false, error: null,
    hasNextPage: pages[pages.length - 1]?.more ?? false, isFetchingNextPage: false,
    fetchNextPage: vi.fn(), ...over,
  } as unknown as Parameters<typeof NotebookView>[0]['query']
}

function mount(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  h.remove.mockReset().mockResolvedValue(undefined)
  h.savePlatform.mockReset().mockResolvedValue({})
  h.settings = { notebook_inline_bytes: '3072', forced_keys: [] }
  h.reads = []
  h.query = fakeQuery([PAGE])
})

describe('the Notebook folds', () => {
  it('the agent page starts folded, fetches on the first open and shows the entries', () => {
    mount(<AgentNotebookCard name="nb-head" />)
    expect(screen.getByText('Notes from this agent and the agents it works with. The agent reads the latest ones in every session.')).toBeTruthy()
    expect(screen.queryByText('Importer landed.')).toBeNull()
    expect(h.reads[h.reads.length - 1]).toEqual(['nb-head', '', false])
    const fold = screen.getByRole('button', { name: /^Notebook/ })
    expect(fold.getAttribute('aria-expanded')).toBe('false')
    expect(fold.hasAttribute('aria-controls')).toBe(false)
    fireEvent.click(fold)
    expect(fold.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('Importer landed.')).toBeTruthy()
    expect(h.reads[h.reads.length - 1]).toEqual(['nb-head', '', true])
    // The button names the section it opened, which holds the entries.
    const section = document.getElementById(fold.getAttribute('aria-controls') ?? '')
    expect(section?.textContent).toContain('Importer landed.')
  })

  it('the department card folds the same way', () => {
    mount(<NotebookSection departmentId="d1" />)
    expect(screen.getByText("Notes from the department's agents about their work. Each agent reads the ones of the agents it works with.")).toBeTruthy()
    expect(screen.queryByText('Importer landed.')).toBeNull()
    const fold = screen.getByRole('button', { name: /^Notebook/ })
    fireEvent.click(fold)
    expect(screen.getByText('Importer landed.')).toBeTruthy()
    expect(h.reads[h.reads.length - 1]).toEqual(['d1', '', true])
    const section = document.getElementById(fold.getAttribute('aria-controls') ?? '')
    expect(section?.textContent).toContain('Importer landed.')
  })

  it('the chevron turns on the motion tokens', () => {
    mount(<AgentNotebookCard name="nb-head" />)
    const chevron = screen.getByRole('button', { name: /^Notebook/ }).querySelector('svg')
    expect(chevron?.getAttribute('class')).toContain('duration-fast ease-standard')
  })
})

describe('the Notebook list', () => {
  it('shows each entry with its agent and where it was written', () => {
    mount(<NotebookView query={fakeQuery([PAGE])} agent="" onAgentChange={vi.fn()} idPrefix="t" />)
    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(4)
    expect(items[0].textContent).toContain('Alpha')
    expect(items[0].textContent).toContain('In a chat with Dev Admin')
    expect(items[0].textContent).toContain('Importer landed.')
    expect(items[1].textContent).toContain('In a task')
    expect(items[2].textContent).toContain('In a meeting')
    expect(items[3].textContent).toContain('In a session')
  })

  it('offers the delete only where the person may, and asks first', async () => {
    mount(<NotebookView query={fakeQuery([PAGE])} agent="" onAgentChange={vi.fn()} idPrefix="t" />)
    const buttons = screen.getAllByRole('button', { name: /Delete this entry by/ })
    expect(buttons).toHaveLength(1)
    fireEvent.click(buttons[0])
    await answerDialog('Delete', 'Delete this entry?')
    await waitFor(() => expect(h.remove).toHaveBeenCalledWith('e2'))
  })

  it('filters by agent and pages older entries', () => {
    const change = vi.fn()
    const query = fakeQuery([PAGE])
    mount(<NotebookView query={query} agent="" onAgentChange={change} idPrefix="t" />)
    fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'nb-head' } })
    expect(change).toHaveBeenCalledWith('nb-head')
    fireEvent.click(screen.getByRole('button', { name: 'Show older entries' }))
    expect(query.fetchNextPage).toHaveBeenCalled()
  })

  it('says when there is nothing and shows a refusal in words', () => {
    const empty = { entries: [], more: false, agents: [{ slug: 'nb-head', display_name: 'Head' }] }
    const { unmount } = mount(<NotebookView query={fakeQuery([empty])} agent="" onAgentChange={vi.fn()} idPrefix="t" />)
    expect(screen.getByText('No entries yet.')).toBeTruthy()
    expect(screen.queryByLabelText('Agent')).toBeNull()
    unmount()
    const failed = fakeQuery([], { isError: true, error: new Error('Agent not found'), data: undefined })
    mount(<NotebookView query={failed} agent="" onAgentChange={vi.fn()} idPrefix="t" />)
    expect(screen.getByText('Agent not found')).toBeTruthy()
  })
})

describe('the Notebook budget in System Settings', () => {
  it('saves the value when the field is left', async () => {
    mount(<NotebookSettingsCard />)
    const input = screen.getByLabelText('Share of each prompt in bytes') as HTMLInputElement
    expect(input.value).toBe('3072')
    fireEvent.change(input, { target: { value: '1024' } })
    await act(async () => { fireEvent.blur(input) })
    expect(h.savePlatform).toHaveBeenCalledWith({ notebook_inline_bytes: '1024' })
  })

  it('puts the stored value back when the save is refused', async () => {
    h.savePlatform.mockRejectedValue(new Error("The Notebook's share of the prompt is a whole number of bytes from 0 to 16,384."))
    mount(<NotebookSettingsCard />)
    const input = screen.getByLabelText('Share of each prompt in bytes') as HTMLInputElement
    fireEvent.change(input, { target: { value: '99999' } })
    await act(async () => { fireEvent.blur(input) })
    await waitFor(() => expect(input.value).toBe('3072'))
  })

  it('a value the operator forced cannot be changed', () => {
    h.settings = { notebook_inline_bytes: '2048', forced_keys: ['notebook_inline_bytes'] }
    mount(<NotebookSettingsCard />)
    expect((screen.getByLabelText('Share of each prompt in bytes') as HTMLInputElement).disabled).toBe(true)
  })
})
