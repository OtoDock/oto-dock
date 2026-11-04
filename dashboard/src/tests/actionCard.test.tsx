// The action card in the chat (components/chat/ActionCard): the state is
// read with the viewer's own sign-in; a `do` button posts the click with
// the options and shows the outcome; a `say` button posts the click and
// nothing else (the server sends its message as the person's own) and
// shows its message as the button's title; one click per card: the
// pressed button stays marked and the others go disabled, after a reload
// too; a refused click reads the card again, so an answer from another tab
// shows; any viewer of the chat presses as themselves and another viewer's
// press is named; a card the viewer may not read shows its words and the
// server's sentence with no button; an expired card says so unless it was
// answered; a running job shows its members.
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import * as authApi from '@/api/auth'
import ActionCard from '@/components/chat/ActionCard'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const BUTTONS = [{ id: 'go', label: 'Move', kind: 'do' }, { id: 'no', label: 'Not now', kind: 'say', message: 'Not now, thanks.' }]
const OPTIONS = [{ id: 'delegation_edge', label: 'Let Alpha hand work to it', default: true }]

function state(over: Record<string, unknown> = {}) {
  return {
    card_id: 'card-1', chat_id: 'chat-1', agent: 'alpha', title: 'Move Beta to Eng',
    text: 'Places Beta at the Team level of Eng.', note: '', buttons: BUTTONS, options: OPTIONS,
    created_at: '2026-10-28T10:00:00Z', resolved: false, clicks: [], outcome: null, expired: null, ...over,
  }
}

const posts: unknown[] = []

function mockRoutes(current: Record<string, unknown>, act: (body: Record<string, unknown>) => Record<string, unknown> | { status: number; detail: string }) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path === '/v1/cards/card-1' && (!init?.method || init.method === 'GET')) {
      if ((current as { status?: number }).status === 403) {
        return { ok: false, status: 403, json: async () => ({ detail: 'This card is in a chat you cannot open.' }), text: async () => JSON.stringify({ detail: 'This card is in a chat you cannot open.' }) } as Response
      }
      return { ok: true, status: 200, json: async () => current } as Response
    }
    if (path === '/v1/cards/card-1/act' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      posts.push(body)
      const answer = act(body)
      if ('status' in answer && typeof answer.status === 'number' && 'detail' in answer) {
        return { ok: false, status: answer.status, json: async () => ({ detail: answer.detail }), text: async () => JSON.stringify({ detail: answer.detail }) } as Response
      }
      return { ok: true, status: 200, json: async () => answer } as Response
    }
    throw new Error(`unexpected ${init?.method ?? 'GET'} ${path}`)
  })
}

function renderCard() {
  return render(
    <ActionCard cardId="card-1" title="Move Beta to Eng" text="Places Beta at the Team level of Eng." buttons={BUTTONS} options={OPTIONS} />,
    { wrapper },
  )
}

describe('ActionCard', () => {
  beforeEach(() => { posts.length = 0 })
  afterEach(() => { fetchSpy.mockReset() })

  it('presses a do button with the options and shows the outcome', async () => {
    const done = state({ resolved: true, clicks: [{ button: 'go', status: 'done', at: 'x', detail: 'Beta is now in Eng as Team.' }],
                         outcome: { status: 'done', text: 'Beta is now in Eng as Team.' } })
    mockRoutes(state(), () => ({ kind: 'do', outcome: done.outcome, state: done }))
    renderCard()
    const go = await screen.findByRole('button', { name: 'Move' })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(go)
    await screen.findByText('Beta is now in Eng as Team.')
    expect(posts).toEqual([{ button: 'go', options: { delegation_edge: false } }])
    // One click per card: the pressed button keeps its mark at full strength
    // (aria-disabled), the other goes quiet (disabled).
    const move = screen.getByRole('button', { name: 'Move' })
    expect(move).not.toBeDisabled()
    expect(move).toHaveAttribute('aria-disabled', 'true')
    expect(move).toHaveAttribute('aria-pressed', 'true')
    expect(move.querySelector('[data-testid="action-card-check"]')).not.toBeNull()
    expect(move.className).toContain('bg-action')
    const no = screen.getByRole('button', { name: 'Not now' })
    expect(no).toBeDisabled()
    expect(no).not.toHaveAttribute('aria-pressed')
    expect(no.querySelector('[data-testid="action-card-check"]')).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('posts a say click and sends nothing itself: the pressed state says what was answered', async () => {
    const after = state({ resolved: true, clicks: [{ button: 'no', status: 'said', at: 'x', detail: 'Not now, thanks.' }] })
    mockRoutes(state(), () => ({ kind: 'say', queued: true, state: after }))
    renderCard()
    const no = await screen.findByRole('button', { name: 'Not now' })
    expect(no).toHaveAttribute('title', 'Not now, thanks.')
    expect(screen.getByRole('button', { name: 'Move' })).not.toHaveAttribute('title')
    fireEvent.click(no)
    await screen.findByTestId('action-card-said')
    expect(posts).toEqual([{ button: 'no', options: { delegation_edge: true } }])
    // Every call went to the card's own routes: no chat message left here.
    const paths = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(paths.every((p) => p.startsWith('/v1/cards/card-1'))).toBe(true)
    // The chosen button keeps full strength: aria-disabled, never the faded disabled state.
    expect(no).not.toBeDisabled()
    expect(no).toHaveAttribute('aria-disabled', 'true')
    expect(no).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(no)
    expect(posts).toHaveLength(1)
    expect(no.querySelector('[data-testid="action-card-check"]')).not.toBeNull()
    expect(no.className).toContain('bg-action')
    const move = screen.getByRole('button', { name: 'Move' })
    expect(move).toBeDisabled()
    expect(move.className).not.toContain('bg-action ')
    expect(screen.getByTestId('action-card-said')).toHaveTextContent('You answered Not now.')
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('shows the say message under its button and as its title', async () => {
    mockRoutes(state({ buttons: [{ id: 'no', label: 'Later', kind: 'say', message: 'Ask me tomorrow.' }] }), () => ({}))
    renderCard()
    expect(await screen.findByRole('button', { name: 'Later' })).toHaveAttribute('title', 'Ask me tomorrow.')
    expect(screen.getByTestId('action-card-message')).toHaveTextContent('Ask me tomorrow.')
  })

  it('shows a refusal and keeps the card when the click is refused', async () => {
    mockRoutes(state(), () => ({ status: 409, detail: 'This card has expired. Ask again.' }))
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Move' }))
    await screen.findByText('This card has expired. Ask again.')
  })

  it('shows the words and the server sentence with no button for a viewer it refuses, and the expiry line', async () => {
    mockRoutes({ status: 403 }, () => ({}))
    const { unmount } = renderCard()
    await screen.findByText('This card is in a chat you cannot open.')
    expect(screen.queryByRole('button', { name: 'Move' })).toBeNull()
    expect(screen.getByText('Places Beta at the Team level of Eng.')).toBeInTheDocument()
    unmount()
    mockRoutes(state({ expired: 'This card has expired. Ask again.' }), () => ({}))
    renderCard()
    await screen.findByText('This card has expired. Ask again.')
    expect(screen.queryByRole('button', { name: 'Move' })).toBeNull()
  })

  it('keeps an answered card answered after it expired, with no expiry line', async () => {
    mockRoutes(state({ resolved: true, expired: 'This card has expired. Ask again.',
                       clicks: [{ button: 'no', status: 'said', at: 'x', detail: 'Not now, thanks.' }] }), () => ({}))
    renderCard()
    expect(await screen.findByTestId('action-card-said')).toHaveTextContent('You answered Not now.')
    expect(screen.queryByText('This card has expired. Ask again.')).toBeNull()
    expect(screen.getByRole('button', { name: 'Not now' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('reloads an answered card answered: the pressed button marked and no button to press', async () => {
    mockRoutes(state({ resolved: true, clicks: [{ button: 'go', status: 'done', at: 'x', detail: 'Beta is now in Eng as Team.' }],
                       outcome: { status: 'done', text: 'Beta is now in Eng as Team.' } }), () => ({}))
    renderCard()
    await screen.findByText('Beta is now in Eng as Team.')
    const move = screen.getByRole('button', { name: 'Move' })
    expect(move).toHaveAttribute('aria-pressed', 'true')
    expect(move).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('button', { name: 'Not now' })).toBeDisabled()
    expect(screen.queryByRole('checkbox')).toBeNull()
    fireEvent.click(move)
    expect(posts).toEqual([])
  })

  it('reads the card again after a refusal, so an answer from another tab shows', async () => {
    const current: Record<string, unknown> = state()
    mockRoutes(current, () => ({ status: 409, detail: 'This card was answered already.' }))
    renderCard()
    const move = await screen.findByRole('button', { name: 'Move' })
    // The other tab answered meanwhile: the server's state moved on.
    Object.assign(current, { resolved: true, clicks: [{ button: 'no', status: 'said', at: 'x', detail: 'Not now, thanks.' }] })
    fireEvent.click(move)
    await screen.findByText('This card was answered already.')
    expect(await screen.findByTestId('action-card-said')).toHaveTextContent('You answered Not now.')
    expect(screen.getByRole('button', { name: 'Not now' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Move' })).toBeDisabled()
    const gets = fetchSpy.mock.calls.filter((c) => String(c[0]) === '/v1/cards/card-1')
    expect(gets).toHaveLength(2)
  })

  it('names another viewer press, and says You for the viewer own', async () => {
    mockRoutes(state({ resolved: true, clicks: [{ button: 'no', status: 'said', at: 'x', detail: 'Not now, thanks.', by: 'Maria', mine: false }] }), () => ({}))
    let view = renderCard()
    expect(await screen.findByTestId('action-card-said')).toHaveTextContent('Maria answered Not now.')
    view.unmount()
    mockRoutes(state({ resolved: true, clicks: [{ button: 'go', status: 'done', at: 'x', detail: 'Beta is now in Eng as Team.', by: 'Nikos', mine: false }],
                       outcome: { status: 'done', text: 'Beta is now in Eng as Team.' } }), () => ({}))
    view = renderCard()
    expect(await screen.findByTestId('action-card-pressed')).toHaveTextContent('Nikos pressed Move.')
    view.unmount()
    // A viewer who did not ask presses as themselves: their own press reads You.
    const done = state({ resolved: true, clicks: [{ button: 'no', status: 'said', at: 'x', detail: 'Not now, thanks.', by: 'Pat', mine: true }] })
    mockRoutes(state(), () => ({ kind: 'say', queued: true, state: done }))
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Not now' }))
    expect(await screen.findByTestId('action-card-said')).toHaveTextContent('You answered Not now.')
    expect(screen.queryByTestId('action-card-pressed')).toBeNull()
  })

  it('lists a running job members and lines', async () => {
    mockRoutes(state({ resolved: true, outcome: {
      status: 'running', text: 'The department Lady G is being installed.', job_id: 'setjob-1',
      members: [{ template: 'yacht-manager', display_name: 'Yacht Manager', slug: 'yacht-manager', status: 'installed', detail: '' },
                { template: 'yacht-crew', display_name: 'Yacht Crew', slug: 'yacht-crew', status: 'installing', detail: '' }],
      lines: ['Installed Yacht Manager as yacht-manager.'],
    } }), () => ({}))
    renderCard()
    await screen.findByText('The department Lady G is being installed.')
    expect(screen.getByText('Installed')).toBeInTheDocument()
    expect(screen.getByText('Installing')).toBeInTheDocument()
    expect(screen.getByText('Installed Yacht Manager as yacht-manager.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Move' })).toBeDisabled()
  })
})
