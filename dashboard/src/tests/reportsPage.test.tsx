/**
 * Admin → Reports: the reported replies newest first, with who reported,
 * which agent and when, the reason and the note; the reply's text folds open
 * on a click, Open chat goes where the notification goes, and Show more
 * reads the next page from the last row shown. Dismiss moves a report into
 * the collapsed history at the bottom ("Dismissed · N"), Restore brings it
 * back; the count comes with the open list.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

type Row = Record<string, unknown> & { id: number; dismissed_at: string }
const h = vi.hoisted(() => ({
  calls: [] as string[],
  posts: [] as string[],
  pages: {} as Record<string, unknown>,
  rows: null as Row[] | null,
}))

// A fake server: fixed pages by path, or (with `rows`) the two lists and
// the count computed from the rows, which dismiss and restore change.
vi.mock('@/lib/http/apiJson', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/http/apiJson')>()),
  apiJson: async (path: string) => {
    h.calls.push(path)
    if (h.rows) {
      const dismissed = path.includes('state=dismissed')
      const reports = h.rows.filter((r) => !!r.dismissed_at === dismissed).sort((a, b) => b.id - a.id)
      return dismissed
        ? { reports, has_more: false }
        : { reports, has_more: false, dismissed_count: h.rows.filter((r) => r.dismissed_at).length }
    }
    return h.pages[path] ?? { reports: [], has_more: false, dismissed_count: 0 }
  },
  apiOk: async (path: string) => {
    h.posts.push(path)
    const m = /reply-reports\/(\d+)\/(dismiss|restore)$/.exec(path)
    const row = h.rows?.find((r) => r.id === Number(m?.[1]))
    if (row) {
      row.dismissed_at = m?.[2] === 'dismiss' ? '2026-10-22T05:00:00+00:00' : ''
      row.dismissed_by_name = m?.[2] === 'dismiss' ? 'Store Admin' : ''
    }
  },
}))
vi.mock('@/api/agents', () => ({
  useAgents: () => ({ data: [{ name: 'store-proof', display_name: 'Store Proof' }] }),
}))

import ReportsPage from '@/pages/admin/ReportsPage'

const report = (id: number, extra: Record<string, unknown> = {}): Row => ({
  id, created_at: '2026-10-21T21:08:56+00:00', reporter_sub: 'local:m', reporter_name: 'Store Member',
  chat_id: 'c-1', chat_title: 'Kitchen planning', chat_agent: 'store-proof', message_id: 2,
  reply_at: '2026-10-21T21:06:00+00:00', agent: 'store-proof', reason: 'harmful',
  note: 'The tone was rude.', reply_text: 'The concrete pour moves to Thursday.',
  dismissed_at: '', dismissed_by_name: '', ...extra,
})

const OPEN_PAGE = '/v1/admin/reply-reports?state=open&limit=50'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><ReportsPage /></MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  h.calls = []
  h.posts = []
  h.pages = {}
  h.rows = null
})

describe('the Reports page', () => {
  it('says what it lists in the operator\'s words', async () => {
    renderPage()
    expect(await screen.findByText(
      "Replies people reported from the flag under an agent's reply. Admins get a notification.",
    )).toBeInTheDocument()
  })

  it('lists a report and folds its reply open on a click', async () => {
    h.pages[OPEN_PAGE] = { reports: [report(9)], has_more: false, dismissed_count: 0 }
    renderPage()
    const row = await screen.findByTestId('reply-report')
    expect(row).toHaveTextContent('Harmful or offensive')
    expect(row).toHaveTextContent('Store Member reported a reply of Store Proof from')
    expect(row).toHaveTextContent('Kitchen planning')
    expect(row).toHaveTextContent('“The tone was rude.”')
    expect(row).not.toHaveTextContent('The concrete pour')
    fireEvent.click(within(row).getByRole('button', { name: 'Show the reply' }))
    expect(row).toHaveTextContent('The concrete pour moves to Thursday.')
    expect(within(row).getByRole('link', { name: 'Open chat' })).toHaveAttribute('href', '/chat/store-proof/c-1')
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Dismissed/ })).toBeNull()
  })

  it('opens a task run the way its notification does', async () => {
    h.pages[OPEN_PAGE] = { reports: [report(5, { chat_id: 'task-run-1' })], has_more: false, dismissed_count: 0 }
    renderPage()
    const row = await screen.findByTestId('reply-report')
    expect(within(row).getByRole('link', { name: 'Open chat' }))
      .toHaveAttribute('href', '/chat/store-proof/task-run-1?tasks=1')
  })

  it('reads older reports from the last row shown', async () => {
    h.pages[OPEN_PAGE] = { reports: [report(9), report(8)], has_more: true, dismissed_count: 0 }
    h.pages[`${OPEN_PAGE}&before_id=8`] = { reports: [report(3, { reason: 'other' })], has_more: false }
    renderPage()
    await screen.findAllByTestId('reply-report')
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }))
    await waitFor(() => expect(screen.getAllByTestId('reply-report')).toHaveLength(3))
    expect(h.calls).toContain(`${OPEN_PAGE}&before_id=8`)
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull()
  })

  it('says when there are none', async () => {
    renderPage()
    expect(await screen.findByText('No reports yet.')).toBeInTheDocument()
  })
})

describe('the dismissed history', () => {
  const openList = () => screen.getByRole('list', { name: 'Reports' })
  const chip = () => screen.getByRole('button', { name: /^Dismissed · \d+/ })

  it('moves a dismissed report into the strip and restores it', async () => {
    h.rows = [report(9), report(8, { reason: 'private', note: '' })]
    renderPage()
    await screen.findAllByTestId('reply-report')
    expect(screen.queryByRole('button', { name: /^Dismissed/ })).toBeNull()

    const second = within(openList()).getAllByTestId('reply-report')[1]
    fireEvent.click(within(second).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(within(openList()).getAllByTestId('reply-report')).toHaveLength(1))
    expect(h.posts).toEqual(['/v1/admin/reply-reports/8/dismiss'])
    expect(chip()).toHaveTextContent('Dismissed · 1')
    expect(chip()).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('list', { name: 'Dismissed reports' })).toBeNull()

    fireEvent.click(chip())
    const history = await screen.findByRole('list', { name: 'Dismissed reports' })
    const kept = within(history).getByTestId('reply-report')
    expect(kept).toHaveTextContent('Private information')
    expect(kept).toHaveTextContent('dismissed by Store Admin')
    fireEvent.click(within(kept).getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(within(openList()).getAllByTestId('reply-report')).toHaveLength(2))
    expect(h.posts).toEqual(['/v1/admin/reply-reports/8/dismiss', '/v1/admin/reply-reports/8/restore'])
    expect(chip()).toHaveTextContent('Dismissed · 0')
    expect(await screen.findByText('Nothing is dismissed.')).toBeInTheDocument()
  })

  it('counts the history from the open list, before the strip is opened', async () => {
    h.rows = [report(9), report(7, { dismissed_at: '2026-10-22T04:00:00+00:00' }),
      report(6, { dismissed_at: '2026-10-22T04:01:00+00:00' })]
    renderPage()
    await screen.findAllByTestId('reply-report')
    expect(chip()).toHaveTextContent('Dismissed · 2')
    expect(h.calls.some((c) => c.includes('state=dismissed'))).toBe(false)
    fireEvent.click(chip())
    const history = await screen.findByRole('list', { name: 'Dismissed reports' })
    expect(within(history).getAllByTestId('reply-report')).toHaveLength(2)
  })

  it('says there are no open reports when every one is dismissed', async () => {
    h.rows = [report(4, { dismissed_at: '2026-10-22T04:00:00+00:00' })]
    renderPage()
    expect(await screen.findByText('No open reports.')).toBeInTheDocument()
    expect(chip()).toHaveTextContent('Dismissed · 1')
  })
})
