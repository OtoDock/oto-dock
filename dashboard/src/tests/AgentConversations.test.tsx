/**
 * The Conversations tab: the phone conversations of outside people with
 * their outcome (no source filter, phone is the only kind), and the People
 * view behind a two-way switch. Each person is a collapsed card whose
 * details open under it, one at a time: what the agent keeps, an export
 * with the session's own sign-in, and a forget after a confirm; a
 * conversation still running refuses the forget with the server's sentence.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

import * as authApi from '@/api/auth'
import AgentConversations from '@/pages/agent/AgentConversations'
import { answerDialog, findAlertDialog, renderWithDialogs } from './dialogsHarness'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const CONVERSATIONS = [
  {
    id: 'c1', title: 'Order question', source_type: 'phone', total_cost: 0.0123,
    created_at: '2026-11-07T10:00:00Z', updated_at: '2026-11-07T10:05:00Z',
    line: 'caller', direction: 'inbound', person_id: 'p1', outcome: 'Booked a table for four on Friday.',
    follow_ups: [], wrapup: 'done', wrote: [], ended_at: '2026-11-07T10:05:00Z',
  },
  {
    id: 'c2', title: '', source_type: 'phone', total_cost: 0,
    created_at: '2026-11-07T09:00:00Z', updated_at: '2026-11-07T09:01:00Z',
    line: 'agent', direction: 'inbound', person_id: '', outcome: '',
    follow_ups: [], wrapup: 'running', wrote: [], ended_at: null,
  },
]

const PEOPLE = [
  {
    id: 'p1', channel: 'phone', number: '+302101234567', first_seen: '2026-10-01T09:00:00Z',
    last_seen: '2026-11-07T10:05:00Z', verified: true, conversations: 3, last_outcome: 'Booked a table for four on Friday.',
  },
  {
    id: 'p2', channel: 'phone', number: '+302109999999', first_seen: '2026-11-01T09:00:00Z',
    last_seen: '2026-11-01T09:10:00Z', verified: false, conversations: 1, last_outcome: '',
  },
]

const PERSON = {
  id: 'p1', channel: 'phone', number: '+302101234567',
  first_seen: '2026-10-01T09:00:00Z', last_seen: '2026-11-07T10:05:00Z', verified: true,
  memory: [{ name: 'preferences.md', text: 'Likes the window table.\nAllergic to nuts.' }],
  files: [{ path: 'workspace/report.md', size: 2048 }],
  conversations: [{ id: 'c1', started: '2026-11-07T10:00:00Z', line: 'caller', direction: 'inbound', outcome: 'Booked a table for four on Friday.', follow_ups: [] }],
  notes: [{ day: '2026-11-07', text: 'A regular, ask about the usual.' }],
}

const PERSON_2 = {
  id: 'p2', channel: 'phone', number: '+302109999999',
  first_seen: '2026-11-01T09:00:00Z', last_seen: '2026-11-01T09:10:00Z', verified: false,
  memory: [], files: [], conversations: [], notes: [],
}

const BUSY = 'A conversation with this person is still running. Try again when it ends.'

function json(body: unknown, status = 200) {
  return {
    ok: status < 400, status, statusText: '',
    json: async () => body, text: async () => JSON.stringify(body),
    headers: new Headers({ 'Content-Type': 'application/json' }),
  } as unknown as Response
}

function mockApi(opts: { conversations?: unknown[]; people?: unknown[]; forget?: 'ok' | 'busy' } = {}) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.startsWith('/v1/agents/demo/conversations')) {
      const list = opts.conversations ?? CONVERSATIONS
      return json({ conversations: list, total: list.length })
    }
    if (path === '/v1/agents/demo/people') return json({ people: opts.people ?? PEOPLE })
    if (path === '/v1/agents/demo/people/p1/export-url') {
      return json({ download_url: '/v1/agents/demo/people/p1/export?t=signed', filename: 'phone-302101234567.zip' })
    }
    if (path === '/v1/agents/demo/people/p1') {
      if (init?.method === 'DELETE') {
        return opts.forget === 'busy' ? json({ detail: BUSY }, 409) : json({ conversations: 3 })
      }
      return json(PERSON)
    }
    if (path === '/v1/agents/demo/people/p2') return json(PERSON_2)
    return json({})
  })
}

function Where() {
  const loc = useLocation()
  return <p data-testid="where">{loc.pathname}{loc.search}</p>
}

function renderPage(entry = '/agents/demo/conversations') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/agents/:name/conversations" element={<><AgentConversations /><Where /></>} />
          <Route path="/conversations/:id" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function calls(path: string, method = 'GET') {
  return fetchSpy.mock.calls.filter(([p, i]) => p === path && (i?.method ?? 'GET') === method)
}

async function openPeople() {
  fireEvent.click(screen.getByRole('button', { name: 'People' }))
  return screen.findAllByText('+302101234567')
}

/** A person's card button: its name starts with their number. */
function cardButton(number: string) {
  return screen.getByRole('button', { name: new RegExp(`^\\${number}`) })
}

/** The details a card button opens: the element its `aria-controls` names. */
function detailsOf(button: HTMLElement) {
  return document.getElementById(button.getAttribute('aria-controls') ?? '') as HTMLElement
}

async function openPerson() {
  await openPeople()
  const button = cardButton('+302101234567')
  fireEvent.click(button)
  await within(detailsOf(button)).findByText('preferences.md')
  return button
}

describe('AgentConversations', () => {
  afterEach(() => { fetchSpy.mockReset() })

  it('shows the outside people empty state and no source filter', async () => {
    mockApi({ conversations: [] })
    renderPage()
    expect(await screen.findByText(
      "No conversations with outside people yet. A person's own calls stay in their chat history.")).toBeInTheDocument()
    expect(document.querySelector('select')).toBeNull()
    expect(fetchSpy.mock.calls.every(([p]) => !String(p).includes('source_type'))).toBe(true)
  })

  it('lists the conversations with their outcome, a dash when there is none', async () => {
    mockApi()
    renderPage()
    const table = await screen.findByRole('table')
    expect(within(table).getByRole('columnheader', { name: 'Outcome' })).toBeInTheDocument()
    const rows = within(table).getAllByRole('row')
    expect(within(rows[1]).getByText('Booked a table for four on Friday.')).toBeInTheDocument()
    expect(within(rows[2]).getAllByText('—').length).toBeGreaterThan(0)
    fireEvent.click(rows[1])
    expect(screen.getByTestId('where')).toHaveTextContent('/conversations/c1')
  })

  it('switches between the two views, the pressed one marked', async () => {
    mockApi()
    renderPage()
    const group = screen.getByRole('group', { name: 'View' })
    const conv = within(group).getByRole('button', { name: 'Conversations' })
    const people = within(group).getByRole('button', { name: 'People' })
    expect(conv).toHaveAttribute('aria-pressed', 'true')
    expect(people).toHaveAttribute('aria-pressed', 'false')
    await openPeople()
    expect(people).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('where')).toHaveTextContent('view=people')
    // One list of cards at every width, no table.
    expect(screen.queryByRole('table')).toBeNull()
    const items = within(screen.getByRole('list')).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0]).getByText('3 conversations')).toBeInTheDocument()
    expect(within(items[0]).getByText('Booked a table for four on Friday.')).toBeInTheDocument()
    expect(within(items[1]).getByText('1 conversation')).toBeInTheDocument()
    // The PIN badge only on the person who entered it.
    expect(within(items[0]).getByText('PIN')).toBeInTheDocument()
    expect(within(items[1]).queryByText('PIN')).toBeNull()
    fireEvent.click(conv)
    expect(await screen.findByText('Order question', { selector: 'td' })).toBeInTheDocument()
  })

  it('says when nobody from outside reached the agent', async () => {
    mockApi({ people: [] })
    renderPage('/agents/demo/conversations?view=people')
    expect(await screen.findByText('Nobody from outside has reached this agent yet.')).toBeInTheDocument()
  })

  it("opens a person's details under their own card, with what the agent keeps", async () => {
    mockApi()
    renderPage()
    const button = await openPerson()
    expect(button).toHaveAttribute('aria-expanded', 'true')
    const details = detailsOf(button)
    // Directly under the button that opened it, inside the same card.
    expect(button.nextElementSibling).toBe(details)
    expect(details.closest('li')).toBe(button.closest('li'))
    expect(details).toBeVisible()
    expect(within(details).getByText(/^First seen .+\. Last seen .+\.$/)).toBeInTheDocument()
    expect(within(details).getByText('preferences.md')).toBeInTheDocument()
    const pre = details.querySelector('pre')!
    expect(pre.textContent).toBe('Likes the window table.\nAllergic to nuts.')
    expect(within(details).getByText('workspace/report.md')).toBeInTheDocument()
    expect(within(details).getByText('2.0 KB')).toBeInTheDocument()
    expect(within(details).getByRole('heading', { name: 'Notes for the team' })).toBeInTheDocument()
    expect(within(details).getByText('A regular, ask about the usual.')).toBeInTheDocument()
    // The other card stays closed, its details empty.
    const other = cardButton('+302109999999')
    expect(other).toHaveAttribute('aria-expanded', 'false')
    expect(detailsOf(other)).not.toBeVisible()
    expect(detailsOf(other)).toBeEmptyDOMElement()
    fireEvent.click(within(details).getByRole('button', { name: /Booked a table/ }))
    expect(screen.getByTestId('where')).toHaveTextContent('/conversations/c1')
  })

  it('keeps one card open at a time, and a second click closes it', async () => {
    mockApi()
    renderPage()
    const first = await openPerson()
    const second = cardButton('+302109999999')
    fireEvent.click(second)
    expect(await within(detailsOf(second)).findByText('Nothing kept yet.')).toBeInTheDocument()
    expect(second).toHaveAttribute('aria-expanded', 'true')
    expect(first).toHaveAttribute('aria-expanded', 'false')
    expect(detailsOf(first)).toBeEmptyDOMElement()
    expect(screen.getByTestId('where')).toHaveTextContent('person=p2')
    fireEvent.click(second)
    expect(second).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('where')).not.toHaveTextContent('person=')
  })

  it('opens the card the address names, scrolled into view', async () => {
    mockApi()
    const scroll = vi.fn()
    const original = HTMLElement.prototype.scrollIntoView
    HTMLElement.prototype.scrollIntoView = scroll
    try {
      renderPage('/agents/demo/conversations?view=people&person=p2')
      const second = await screen.findByRole('button', { name: /^\+302109999999/ })
      expect(second).toHaveAttribute('aria-expanded', 'true')
      await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1))
      // The card's frame, around its button.
      expect(scroll.mock.contexts[0]).toBe(second.parentElement)
      expect(cardButton('+302101234567')).toHaveAttribute('aria-expanded', 'false')
    } finally {
      HTMLElement.prototype.scrollIntoView = original
    }
  })

  it('shows the empty sections, and no notes section when there are none', async () => {
    mockApi()
    fetchSpy.mockImplementation(async (path: string) => {
      if (path === '/v1/agents/demo/people') return json({ people: PEOPLE })
      if (path === '/v1/agents/demo/people/p1') return json({ ...PERSON, memory: [], files: [], notes: [] })
      return json({ conversations: [], total: 0 })
    })
    renderPage('/agents/demo/conversations?view=people&person=p1')
    const button = await screen.findByRole('button', { name: /^\+302101234567/ })
    const details = detailsOf(button)
    expect(await within(details).findByText('Nothing kept yet.')).toBeInTheDocument()
    expect(within(details).getByText('No files.')).toBeInTheDocument()
    expect(within(details).queryByText('Notes for the team')).toBeNull()
  })

  describe('export and forget', () => {
    let anchorClick: ReturnType<typeof vi.spyOn>
    beforeEach(() => {
      anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    })
    afterEach(() => { anchorClick.mockRestore() })

    it('exports the zip from a signed address under the server name', async () => {
      mockApi()
      renderPage()
      await openPerson()
      fireEvent.click(screen.getByRole('button', { name: 'About Export' }), { detail: 0 })
      expect(screen.getByRole('tooltip')).toHaveTextContent(
        'A zip of what this agent keeps about this person.')
      fireEvent.click(screen.getByRole('button', { name: 'Export' }))
      await waitFor(() => expect(anchorClick).toHaveBeenCalled())
      expect(calls('/v1/agents/demo/people/p1/export-url', 'POST')).toHaveLength(1)
      const a = anchorClick.mock.instances[0] as unknown as HTMLAnchorElement
      expect(a.download).toBe('phone-302101234567.zip')
      expect(a.getAttribute('href')).toBe('/v1/agents/demo/people/p1/export?t=signed')
    })

    it('forgets after the confirm, closes the panel, refreshes the lists and says so', async () => {
      mockApi()
      renderPage()
      await openPerson()
      const peopleLoads = calls('/v1/agents/demo/people').length
      fireEvent.click(screen.getByRole('button', { name: 'Forget' }))
      const ask = await findAlertDialog('Forget +302101234567?')
      expect(ask).toHaveTextContent(
        'Their memory, files and conversations on this agent are deleted. Their notes for the team and their call history go too. This cannot be undone.')
      expect(within(ask).getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
      expect(calls('/v1/agents/demo/people/p1', 'DELETE')).toHaveLength(0)
      await act(async () => { await answerDialog('Forget', 'Forget +302101234567?') })
      await waitFor(() => expect(calls('/v1/agents/demo/people/p1', 'DELETE')).toHaveLength(1))
      expect(await screen.findByText('This person was forgotten.')).toBeInTheDocument()
      await waitFor(() => expect(screen.queryByText('preferences.md')).toBeNull())
      expect(screen.getByTestId('where')).not.toHaveTextContent('person=')
      await waitFor(() => expect(calls('/v1/agents/demo/people').length).toBeGreaterThan(peopleLoads))
    })

    it('cancel sends nothing', async () => {
      mockApi()
      renderPage()
      await openPerson()
      fireEvent.click(screen.getByRole('button', { name: 'Forget' }))
      await act(async () => { await answerDialog('Cancel', 'Forget +302101234567?') })
      expect(calls('/v1/agents/demo/people/p1', 'DELETE')).toHaveLength(0)
      expect(screen.getByText('preferences.md')).toBeInTheDocument()
    })

    it("shows the server's sentence when a conversation still runs", async () => {
      mockApi({ forget: 'busy' })
      renderPage()
      await openPerson()
      fireEvent.click(screen.getByRole('button', { name: 'Forget' }))
      await act(async () => { await answerDialog('Forget', 'Forget +302101234567?') })
      expect(await screen.findByRole('alert')).toHaveTextContent(BUSY)
      expect(cardButton('+302101234567')).toHaveAttribute('aria-expanded', 'true')
      expect(screen.queryByText('This person was forgotten.')).toBeNull()
    })
  })
})
