import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { answerDialog, renderWithDialogs } from './dialogsHarness'

// ─── The archive of removed people: its own setting (0 keeps for ever) and
//     an admin list with a purge that asks first. ────────────────────────

const h = vi.hoisted(() => ({
  calls: [] as Array<{ url: string; method: string }>,
  savePlatform: vi.fn(),
  toast: vi.fn(),
  // One object for every render, as the query's own data is.
  settings: { offboarded_retention_enabled: true, offboarded_retention_days: '180', forced_keys: [], cloud: false },
}))

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

// The System settings tab around the card: its hooks stand still, its save
// is the test's.
vi.mock('@/pages/admin/PlatformPage.hooks', () => ({
  usePlatformSettings: () => ({ data: h.settings, isLoading: false }),
  useSavePlatformSettings: () => ({ mutateAsync: h.savePlatform, isPending: false }),
  useConcurrencyStats: () => ({ data: undefined }),
  useStorageUsage: () => ({ data: undefined }),
}))
vi.mock('@/api/memory', () => ({
  useMemorySettings: () => ({ data: undefined }),
  useUpdateMemorySettings: () => ({ mutateAsync: vi.fn() }),
}))
vi.mock('@/api/titleGeneration', () => ({
  useTitleGeneration: () => ({ data: undefined, isLoading: true }),
  useSaveTitleGeneration: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useCheckTitleGeneration: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))
vi.mock('@/api/mcps', () => ({ useMcpAutoUpdateLog: () => ({ data: { runs: [], last_run_at: '' } }) }))
vi.mock('@/lib/appToasts', () => ({ emitAppToast: h.toast, onAppToast: () => () => {} }))

import OffboardedArchivesCard from '@/pages/admin/OffboardedArchivesCard'
import SystemSettingsTab from '@/pages/admin/SystemSettingsTab'

function mount(over: Partial<Parameters<typeof OffboardedArchivesCard>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(
    <QueryClientProvider client={qc}>
      <OffboardedArchivesCard
        enabled days="180" onEnabledChange={vi.fn()} onDaysChange={vi.fn()} onSaveDays={vi.fn()}
        stateOf={() => null} forcedKeys={[]} {...over}
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
    expect(screen.getByText(/undated, the person was restored · kept/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete the archive of old-pat now' })).toBeInTheDocument()
    const days = screen.getByLabelText('Days to keep archives') as HTMLInputElement
    expect(days.min).toBe('0')
    expect(days.max).toBe('3650')
  })

  it('purges only after a confirmation', async () => {
    mount()
    await screen.findByText('old-pat')
    fireEvent.click(screen.getByRole('button', { name: 'Delete the archive of old-pat now' }))
    await act(async () => { await answerDialog('Cancel', 'Delete the archive of old-pat now?') })
    expect(h.calls.some((c) => c.method === 'DELETE')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Delete the archive of old-pat now' }))
    await act(async () => { await answerDialog('Delete', 'Delete the archive of old-pat now?') })
    await waitFor(() => expect(h.calls).toContainEqual({ url: '/v1/admin/offboarded/old-pat', method: 'DELETE' }))
    expect(await screen.findByText(/Deleted the archive of old-pat/)).toBeInTheDocument()
  })

  it('shows a forced setting as disabled', async () => {
    mount({ forcedKeys: ['offboarded_retention_days'] })
    await screen.findByText('old-pat')
    expect((screen.getByLabelText('Days to keep archives') as HTMLInputElement).disabled).toBe(true)
  })

  it('re-reads the list once a retention setting is saved', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const ui = (days: 'saved' | null) => (
      <QueryClientProvider client={qc}>
        <OffboardedArchivesCard
          enabled days="180" onEnabledChange={vi.fn()} onDaysChange={vi.fn()} onSaveDays={vi.fn()}
          stateOf={(k) => (k === 'offboarded_retention_days' ? days : null)} forcedKeys={[]}
        />
      </QueryClientProvider>
    )
    const reads = () => h.calls.filter((c) => c.url === '/v1/admin/offboarded').length
    const { rerender } = render(ui(null))
    await screen.findByText('old-pat')
    const before = reads()
    rerender(ui('saved'))
    await waitFor(() => expect(reads()).toBeGreaterThan(before))
    expect(screen.getAllByRole('status').some((r) => r.textContent === 'Saved')).toBe(true)
  })
})

describe('the archive days on the System settings tab', () => {
  it('a refused number toasts the server\'s reason and stays in the field as typed', async () => {
    h.savePlatform.mockRejectedValueOnce(new Error('offboarded_retention_days must be at most 3650'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderWithDialogs(<QueryClientProvider client={qc}><SystemSettingsTab /></QueryClientProvider>)
    const days = await screen.findByLabelText('Days to keep archives') as HTMLInputElement
    fireEvent.change(days, { target: { value: '5000' } })
    fireEvent.blur(days)
    expect(h.savePlatform).toHaveBeenCalledWith({ offboarded_retention_days: '5000' })
    await waitFor(() => expect(h.toast).toHaveBeenCalledWith(expect.objectContaining({
      title: 'The days to keep were not saved',
      body: 'offboarded_retention_days must be at most 3650',
    })))
    expect(days.value).toBe('5000')
  })
})
