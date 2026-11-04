/**
 * Admin → Reports: the reported replies newest first, with who reported,
 * which agent and when, the reason and the note; the reply's text folds open
 * on a click, Open chat goes where the notification goes (at the reply), and
 * Show more reads the next page from the last row shown. Handled and Dismiss
 * move a report into the collapsed list at the bottom ("Closed · N"),
 * Restore brings it back, Delete asks first; the count comes with the open
 * list. A report whose chat was deleted says so.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

type Row = Record<string, unknown> & { id: number; state: string }
const h = vi.hoisted(() => ({
  calls: [] as string[],
  posts: [] as string[],
  pages: {} as Record<string, unknown>,
  rows: null as Row[] | null,
  deleteStatus: 200,
}))

// A fake server: fixed pages by path, or (with `rows`) the two lists and
// the count computed from the rows, which the state routes and delete change.
vi.mock('@/lib/http/apiJson', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/lib/http/apiJson')>()
  return {
    ...real,
    apiJson: async (path: string) => {
      h.calls.push(path)
      if (h.rows) {
        const closed = path.includes('state=closed')
        const reports = h.rows.filter((r) => (r.state !== 'open') === closed).sort((a, b) => b.id - a.id)
        return closed
          ? { reports, has_more: false }
          : { reports, has_more: false, closed_count: h.rows.filter((r) => r.state !== 'open').length }
      }
      return h.pages[path] ?? { reports: [], has_more: false, closed_count: 0 }
    },
    apiOk: async (path: string, init: { method?: string } = {}) => {
      h.posts.push(`${init.method ?? 'GET'} ${path}`)
      const del = /reply-reports\/(\d+)$/.exec(path)
      if (del && init.method === 'DELETE') {
        if (h.deleteStatus !== 200) throw new real.ApiError(h.deleteStatus, 'Report not found', 'Report not found', null)
        if (h.rows) h.rows = h.rows.filter((r) => r.id !== Number(del[1]))
        return
      }
      const m = /reply-reports\/(\d+)\/(handled|dismiss|restore)$/.exec(path)
      const row = h.rows?.find((r) => r.id === Number(m?.[1]))
      if (row) {
        row.state = m?.[2] === 'restore' ? 'open' : m?.[2] === 'handled' ? 'handled' : 'dismissed'
        row.closed_at = m?.[2] === 'restore' ? '' : '2026-10-22T05:00:00+00:00'
        row.closed_by_name = m?.[2] === 'restore' ? '' : 'Store Admin'
      }
    },
  }
})
vi.mock('@/api/agents', () => ({
  useAgents: () => ({ data: [{ name: 'store-proof', display_name: 'Store Proof' }] }),
}))

import ReportsPage from '@/pages/admin/ReportsPage'

const report = (id: number, extra: Record<string, unknown> = {}): Row => ({
  id, created_at: '2026-10-21T21:08:56+00:00', reporter_sub: 'local:m', reporter_name: 'Store Member',
  chat_id: 'c-1', chat_title: 'Kitchen planning', chat_agent: 'store-proof', message_id: 2,
  reply_at: '2026-10-21T21:06:00+00:00', agent: 'store-proof', reason: 'harmful',
  note: 'The tone was rude.', reply_text: 'The concrete pour moves to Thursday.',
  state: 'open', closed_at: '', closed_by_name: '', ...extra,
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
  h.deleteStatus = 200
})

describe('the Reports page', () => {
  it('says what it lists in the operator\'s words', async () => {
    renderPage()
    expect(await screen.findByText(
      "Replies people reported from the flag under an agent's reply. Admins get a notification.",
    )).toBeInTheDocument()
  })

  it('lists a report and folds its reply open on a click', async () => {
    h.pages[OPEN_PAGE] = { reports: [report(9)], has_more: false, closed_count: 0 }
    renderPage()
    const row = await screen.findByTestId('reply-report')
    expect(row).toHaveTextContent('Harmful or offensive')
    expect(row).toHaveTextContent('Store Member reported a reply of Store Proof from')
    expect(row).toHaveTextContent('Kitchen planning')
    expect(row).toHaveTextContent('“The tone was rude.”')
    expect(row).not.toHaveTextContent('The concrete pour')
    fireEvent.click(within(row).getByRole('button', { name: 'Show the reply' }))
    expect(row).toHaveTextContent('The concrete pour moves to Thursday.')
    expect(within(row).getByRole('link', { name: 'Open chat' }))
      .toHaveAttribute('href', '/chat/store-proof/c-1?message=2')
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Closed/ })).toBeNull()
  })

  it('opens a task run the way its notification does', async () => {
    h.pages[OPEN_PAGE] = { reports: [report(5, { chat_id: 'task-run-1' })], has_more: false, closed_count: 0 }
    renderPage()
    const row = await screen.findByTestId('reply-report')
    expect(within(row).getByRole('link', { name: 'Open chat' }))
      .toHaveAttribute('href', '/chat/store-proof/task-run-1?tasks=1&message=2')
  })

  it('reads older reports from the last row shown', async () => {
    h.pages[OPEN_PAGE] = { reports: [report(9), report(8)], has_more: true, closed_count: 0 }
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

  it('says the chat was deleted instead of linking it', async () => {
    h.pages[OPEN_PAGE] = { reports: [report(4, { chat_id: '', chat_agent: '', reply_at: '' })], has_more: false, closed_count: 0 }
    renderPage()
    const row = await screen.findByTestId('reply-report')
    expect(row).toHaveTextContent('The chat was deleted.')
    expect(row).toHaveTextContent('Kitchen planning')
    expect(within(row).queryByRole('link', { name: 'Open chat' })).toBeNull()
  })
})

describe('the closed list', () => {
  const openList = () => screen.getByRole('list', { name: 'Reports' })
  const chip = () => screen.getByRole('button', { name: /^Closed · \d+/ })

  it('moves a dismissed report into the strip and restores it', async () => {
    h.rows = [report(9), report(8, { reason: 'private', note: '' })]
    renderPage()
    await screen.findAllByTestId('reply-report')
    expect(screen.queryByRole('button', { name: /^Closed/ })).toBeNull()

    const second = within(openList()).getAllByTestId('reply-report')[1]
    fireEvent.click(within(second).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(within(openList()).getAllByTestId('reply-report')).toHaveLength(1))
    expect(h.posts).toEqual(['POST /v1/admin/reply-reports/8/dismiss'])
    expect(chip()).toHaveTextContent('Closed · 1')
    expect(chip()).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('list', { name: 'Closed reports' })).toBeNull()

    fireEvent.click(chip())
    const closed = await screen.findByRole('list', { name: 'Closed reports' })
    const kept = within(closed).getByTestId('reply-report')
    expect(kept).toHaveTextContent('Private information')
    expect(kept).toHaveTextContent('dismissed by Store Admin')
    expect(within(kept).queryByRole('button', { name: 'Dismiss' })).toBeNull()
    fireEvent.click(within(kept).getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(within(openList()).getAllByTestId('reply-report')).toHaveLength(2))
    expect(h.posts).toEqual(['POST /v1/admin/reply-reports/8/dismiss', 'POST /v1/admin/reply-reports/8/restore'])
    expect(chip()).toHaveTextContent('Closed · 0')
    expect(await screen.findByText('No closed reports.')).toBeInTheDocument()
  })

  it('marks a report handled, which leaves the open list with its state', async () => {
    h.rows = [report(9), report(8)]
    renderPage()
    await screen.findAllByTestId('reply-report')
    const first = within(openList()).getAllByTestId('reply-report')[0]
    fireEvent.click(within(first).getByRole('button', { name: 'Handled' }))
    await waitFor(() => expect(within(openList()).getAllByTestId('reply-report')).toHaveLength(1))
    expect(h.posts).toEqual(['POST /v1/admin/reply-reports/9/handled'])
    fireEvent.click(chip())
    const closed = await screen.findByRole('list', { name: 'Closed reports' })
    expect(within(closed).getByTestId('reply-report')).toHaveTextContent('handled by Store Admin')
  })

  it('counts the closed list from the open one, before the strip is opened', async () => {
    h.rows = [report(9), report(7, { state: 'dismissed', closed_at: '2026-10-22T04:00:00+00:00' }),
      report(6, { state: 'handled', closed_at: '2026-10-22T04:01:00+00:00' })]
    renderPage()
    await screen.findAllByTestId('reply-report')
    expect(chip()).toHaveTextContent('Closed · 2')
    expect(h.calls.some((c) => c.includes('state=closed'))).toBe(false)
    fireEvent.click(chip())
    const closed = await screen.findByRole('list', { name: 'Closed reports' })
    expect(within(closed).getAllByTestId('reply-report')).toHaveLength(2)
  })

  it('says there are no open reports when every one is closed', async () => {
    h.rows = [report(4, { state: 'handled', closed_at: '2026-10-22T04:00:00+00:00' })]
    renderPage()
    expect(await screen.findByText('No open reports.')).toBeInTheDocument()
    expect(chip()).toHaveTextContent('Closed · 1')
  })
})

describe('deleting a report', () => {
  it('asks first, and Cancel keeps it', async () => {
    h.rows = [report(9)]
    renderPage()
    const row = await screen.findByTestId('reply-report')
    fireEvent.click(within(row).getByRole('button', { name: 'Delete' }))
    const confirm = within(row).getByTestId('report-delete-confirm')
    expect(confirm).toHaveTextContent('Delete the report and its copy of the reply? This cannot be undone.')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    expect(within(row).queryByTestId('report-delete-confirm')).toBeNull()
    expect(h.posts).toEqual([])
  })

  it('deletes it once confirmed', async () => {
    h.rows = [report(9), report(8)]
    renderPage()
    await screen.findAllByTestId('reply-report')
    const row = screen.getAllByTestId('reply-report')[1]
    fireEvent.click(within(row).getByRole('button', { name: 'Delete' }))
    fireEvent.click(within(row).getByRole('button', { name: 'Delete the report' }))
    await waitFor(() => expect(screen.getAllByTestId('reply-report')).toHaveLength(1))
    expect(h.posts).toEqual(['DELETE /v1/admin/reply-reports/8'])
  })

  it('deletes a closed report from the closed list', async () => {
    h.rows = [report(9), report(7, { state: 'dismissed', closed_at: '2026-10-22T04:00:00+00:00', closed_by_name: 'Store Admin' })]
    renderPage()
    await screen.findAllByTestId('reply-report')
    fireEvent.click(screen.getByRole('button', { name: /^Closed · 1/ }))
    const closed = await screen.findByRole('list', { name: 'Closed reports' })
    const row = within(closed).getByTestId('reply-report')
    fireEvent.click(within(row).getByRole('button', { name: 'Delete' }))
    fireEvent.click(within(row).getByRole('button', { name: 'Delete the report' }))
    await waitFor(() => expect(h.posts).toEqual(['DELETE /v1/admin/reply-reports/7']))
    expect(await screen.findByText('No closed reports.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Closed · 0/ })).toBeInTheDocument()
    expect(screen.getAllByTestId('reply-report')).toHaveLength(1)
  })

  it('takes one another admin deleted first as deleted, with no error', async () => {
    h.rows = [report(9)]
    h.deleteStatus = 404
    renderPage()
    const row = await screen.findByTestId('reply-report')
    fireEvent.click(within(row).getByRole('button', { name: 'Delete' }))
    fireEvent.click(within(row).getByRole('button', { name: 'Delete the report' }))
    await waitFor(() => expect(h.posts).toEqual(['DELETE /v1/admin/reply-reports/9']))
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
