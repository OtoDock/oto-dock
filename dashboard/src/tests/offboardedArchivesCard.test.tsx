import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── The archive of removed people: its own setting (0 keeps for ever) and
//     an admin list with a purge that asks first. ────────────────────────

const h = vi.hoisted(() => ({ calls: [] as Array<{ url: string; method: string }> }))

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async (url: string, init?: RequestInit) => {
    h.calls.push({ url, method: init?.method || 'GET' })
    if (url === '/v1/admin/offboarded') {
      return new Response(JSON.stringify({
        archives: [
          { username: 'old-pat', agents: ['site', 'office'], bytes: 2048, retired_at: '2026-01-01T00:00:00+00:00',
            archived_at: '2026-01-01T00:08:00+00:00', purge_after: '2026-06-30T00:08:00+00:00' },
          { username: 'fresh', agents: ['site'], bytes: 10, retired_at: '2026-09-30T08:00:00+00:00',
            archived_at: '', purge_after: '' },
          { username: 'restored', agents: ['site'], bytes: 10, retired_at: '', archived_at: '', purge_after: '' },
        ],
        retention: { enabled: true, days: 180 },
      }))
    }
    if (url === '/v1/admin/offboarded/old-pat') return new Response(JSON.stringify({ username: 'old-pat', bytes: 2048 }))
    return new Response('{}')
  }),
}))

import OffboardedArchivesCard from '@/pages/admin/OffboardedArchivesCard'

function mount(over: Partial<Parameters<typeof OffboardedArchivesCard>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <OffboardedArchivesCard
        enabled days="180" onEnabledChange={vi.fn()} onDaysChange={vi.fn()} onSaveDays={vi.fn()}
        savedField="" daysError={null} forcedKeys={[]} {...over}
      />
    </QueryClientProvider>,
  )
}

describe('the archive of removed people', () => {
  beforeEach(() => { h.calls = [] })

  it('lists each archive with its folders, size and fate', async () => {
    mount()
    expect(await screen.findByText('old-pat')).toBeInTheDocument()
    expect(screen.getByText(/2 agent folders · 2\.0 KB · archived .* · deleted after /)).toBeInTheDocument()
    expect(screen.getByText(/1 agent folder · .* · still being archived · kept/)).toBeInTheDocument()
    expect(screen.getByText(/undated \(the person was restored\) · kept/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete the archive of old-pat now' })).toBeInTheDocument()
    const days = screen.getByLabelText('Keep archives for (days)') as HTMLInputElement
    expect(days.min).toBe('0')
    expect(days.max).toBe('3650')
  })

  it('purges only after a confirmation', async () => {
    mount()
    await screen.findByText('old-pat')
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    fireEvent.click(screen.getByRole('button', { name: 'Delete the archive of old-pat now' }))
    expect(h.calls.some((c) => c.method === 'DELETE')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Delete the archive of old-pat now' }))
    await waitFor(() => expect(h.calls).toContainEqual({ url: '/v1/admin/offboarded/old-pat', method: 'DELETE' }))
    expect(await screen.findByText(/Deleted the archive of old-pat/)).toBeInTheDocument()
    confirm.mockRestore()
  })

  it('shows a forced setting as disabled and the server\'s refusal of a value', async () => {
    mount({ forcedKeys: ['offboarded_retention_days'], daysError: 'offboarded_retention_days must be a whole number' })
    await screen.findByText('old-pat')
    expect((screen.getByLabelText('Keep archives for (days)') as HTMLInputElement).disabled).toBe(true)
    expect(screen.getByText(/must be a whole number/)).toBeInTheDocument()
  })
})
