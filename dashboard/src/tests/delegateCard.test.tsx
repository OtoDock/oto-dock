/**
 * Delegated messages, minimized: a delegate's report in
 * the delegating chat and a worker lane's incoming prompt render as cards,
 * collapsed by default, a click opening them; the preview strips only the
 * platform's own wrappers; the deliverables stay visible; a card never
 * hides a live control; find-in-chat opens a card it matches.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, renderHook, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatMessages from '@/components/chat/ChatMessages'
import type { DisplayMessage } from '@/components/chat/types'
import { SearchProvider } from '@/contexts/SearchContext'
import { useDelegateCards } from '@/hooks/useDelegateCards'
import {
  dbMessagesToDisplay, delegateCardOf, delegatePreviewText, REPORT_MISSING_BADGE,
} from '@/lib/messageBlocks'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)

const AGENTS = [{ name: 'worker', display_name: 'Worker Agent', color: '#2a6df4' },
  { name: 'boss', display_name: 'Boss Agent', color: '#0d9488' }]

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++, role, content, event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '',
    created_at: '2026-10-16T09:00:00+00:00',
  }
}

const REPORT = 'I read the plan and started.\n\nThe cards collapse and the tests pass.\n\nBlocked on: nothing'

function resultRow(over: object = {}) {
  return row('event', '', 'delegate_result', {
    task_id: 'dyn-1', task_name: 'cards', agent: 'worker', output_text: REPORT,
    status: 'completed', ...over,
  })
}

const ENVELOPE = "[DELEGATED_WORK]\nDelegated by agent 'boss'. When you finish, write a report.\n[/DELEGATED_WORK]\n\n"
  + '[TASK_OUTPUT_DIR]\nSave any output files under out/.\n[/TASK_OUTPUT_DIR]\n\n'

function promptRow(text: string) {
  return row('user', text, '', {
    agent_slug: 'boss', agent_display_name: 'Boss Agent', agent_color: '#0d9488', badge: 'delegated by',
  })
}

/** The card's own toggle: its accessible name carries the header. */
const toggleOf = (card: HTMLElement) => within(card).getByRole('button', { name: /Worker Agent/ })

function renderList(messages: DisplayMessage[], query = '') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SearchProvider query={query}>
        <ChatMessages messages={messages} agentName="boss" chatId="c1" onPermissionRespond={() => {}} />
      </SearchProvider>
    </QueryClientProvider>,
  )
}

describe('the delegate card', () => {
  it('a result is collapsed by default and a click opens and closes it', () => {
    renderList(dbMessagesToDisplay([resultRow()], AGENTS))
    const card = screen.getByTestId('delegate-card')
    const toggle = within(card).getByRole('button', { expanded: false })
    expect(toggle.tagName).toBe('BUTTON')            // Enter and Space are the button's own
    expect(toggle).toHaveAttribute('type', 'button')
    expect(within(card).getByText('Worker Agent')).toBeInTheDocument()
    expect(within(card).getByText('delegate response')).toBeInTheDocument()
    // The report's end, not its start.
    expect(within(card).getByTestId('delegate-card-preview').textContent)
      .toBe('The cards collapse and the tests pass. Blocked on: nothing')
    expect(within(card).queryByText(/I read the plan/)).toBeNull()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(within(card).getByText(/I read the plan/)).toBeInTheDocument()
    expect(within(card).queryByTestId('delegate-card-preview')).toBeNull()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('a worker\'s prompt is a card whose preview drops the platform\'s wrappers', () => {
    const msgs = dbMessagesToDisplay([promptRow(`${ENVELOPE}Review the plan, then report.`)], AGENTS)
    expect(msgs[0].delegateCard).toEqual({ kind: 'prompt', key: `db-${nextId - 1}` })
    renderList(msgs)
    const card = screen.getByTestId('delegate-card')
    expect(card).toHaveAttribute('data-kind', 'prompt')
    expect(within(card).getByText('delegated by')).toBeInTheDocument()
    expect(within(card).getByTestId('delegate-card-preview').textContent).toBe('Review the plan, then report.')
    fireEvent.click(within(card).getByRole('button', { expanded: false }))
    // Expanded, the stored text is whole.
    expect(card.textContent).toContain('[DELEGATED_WORK]')
  })

  it('strips only a leading, closed wrapper', () => {
    const block = (content: string) => [{ type: 'text' as const, content }]
    expect(delegatePreviewText('prompt', block('[DELEGATED_WORK] a word I typed'))).toBe('[DELEGATED_WORK] a word I typed')
    expect(delegatePreviewText('prompt', block('Fix it. [DELEGATED_WORK]x[/DELEGATED_WORK]')))
      .toBe('Fix it. [DELEGATED_WORK]x[/DELEGATED_WORK]')
    // Nothing but the envelope: the text shows as it is.
    expect(delegatePreviewText('prompt', block('[DELEGATED_WORK]only[/DELEGATED_WORK]')))
      .toBe('[DELEGATED_WORK]only[/DELEGATED_WORK]')
    expect(delegatePreviewText('result', block('… [output truncated — showing the last 12000 chars; open the worker chat for the full run]\n\n## Done\n- **all** `green`')))
      .toBe('Done all green')
  })

  it('a plain user message is never a card, whatever it says', () => {
    const msgs = dbMessagesToDisplay([row('user', `${ENVELOPE}typed by a person`), row('assistant', 'ok')], AGENTS)
    renderList(msgs)
    expect(screen.queryByTestId('delegate-card')).toBeNull()
    expect(screen.getByText(/typed by a person/)).toBeInTheDocument()
  })

  it('keeps the deliverables visible while collapsed', () => {
    const msgs = dbMessagesToDisplay([resultRow({
      files: [{ path: 'workspace/inbox/worker/report.md', bytes: 120 }],
      files_skipped: [{ path: 'huge.bin', reason: 'too big' }],
    })], AGENTS)
    renderList(msgs)
    const chips = screen.getByTestId('delegate-file-chips')
    expect(within(chips).getByText('inbox/worker/report.md')).toBeInTheDocument()
    expect(within(chips).queryByText('huge.bin')).toBeNull()
  })

  it('never collapses a message that holds a live control', () => {
    const msg: DisplayMessage = {
      id: 'x', role: 'assistant', createdAt: '2026-10-16T09:00:00+00:00', agentSlug: 'worker',
      blocks: [{ type: 'text', content: 'hi' }, { type: 'permission', requestId: 'r', toolName: 'Bash', toolInput: {} } as never],
      delegateCard: { kind: 'result', key: 'r:x:1' },
    }
    expect(delegateCardOf(msg)).toBeNull()
  })

  it('a result that ended without a report wears its own badge', () => {
    const [msg] = dbMessagesToDisplay([resultRow({ status: 'failed', report_missing: true })], AGENTS)
    expect(msg.badge).toBe(REPORT_MISSING_BADGE)
  })

  it('find-in-chat shows a card its query matches, and only that one', () => {
    const msgs = dbMessagesToDisplay([resultRow(), resultRow({ task_id: 'dyn-2', output_text: 'another lane' })], AGENTS)
    renderList(msgs, 'read the plan')
    const [first, second] = screen.getAllByTestId('delegate-card')
    expect(toggleOf(first)).toHaveAttribute('aria-expanded', 'true')
    expect(toggleOf(second)).toHaveAttribute('aria-expanded', 'false')
  })

  it('two cards with one key toggle on their own', () => {
    const msgs = dbMessagesToDisplay([resultRow(), resultRow()], AGENTS)
    expect(msgs[0].delegateCard?.key).toBe(msgs[1].delegateCard?.key)
    renderList(msgs)
    const [first, second] = screen.getAllByTestId('delegate-card')
    fireEvent.click(toggleOf(first))
    expect(toggleOf(first)).toHaveAttribute('aria-expanded', 'true')
    expect(toggleOf(second)).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('two cards with one key and an older page', () => {
  it('the card the person opened stays open when older messages load', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const page = dbMessagesToDisplay([resultRow(), resultRow()], AGENTS)
    const older = dbMessagesToDisplay([resultRow()], AGENTS)
    function List({ messages }: { messages: DisplayMessage[] }) {
      const cards = useDelegateCards('c1')
      return (
        <QueryClientProvider client={qc}>
          <ChatMessages messages={messages} agentName="boss" chatId="c1" onPermissionRespond={() => {}}
            openCards={cards.openCards} onToggleCard={cards.toggleCard} />
        </QueryClientProvider>
      )
    }
    const { rerender } = render(<List messages={page} />)
    const newest = () => {
      const cards = screen.getAllByTestId('delegate-card')
      return cards[cards.length - 1]
    }
    fireEvent.click(toggleOf(newest()))
    expect(toggleOf(newest())).toHaveAttribute('aria-expanded', 'true')
    rerender(<List messages={[...older, ...page]} />)
    const all = screen.getAllByTestId('delegate-card')
    expect(all.map((c) => toggleOf(c).getAttribute('aria-expanded'))).toEqual(['false', 'false', 'true'])
  })
})

describe('the open cards are the page\'s', () => {
  it('another chat, then back: every card is closed', () => {
    const { result, rerender } = renderHook(({ chat }) => useDelegateCards(chat), { initialProps: { chat: 'a' } })
    act(() => result.current.toggleCard('k'))
    expect(result.current.openCards.has('k')).toBe(true)
    rerender({ chat: 'b' })
    expect(result.current.openCards.size).toBe(0)
    rerender({ chat: 'a' })
    expect(result.current.openCards.size).toBe(0)
  })
})
