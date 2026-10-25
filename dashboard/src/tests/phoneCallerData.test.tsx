/**
 * Caller Data card (Admin → Phone): the retention toggle + window, the usage
 * readout, and the confirmed "Forget all caller data now" action.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import * as authApi from '@/api/auth'
import PhoneServersTab from '@/pages/admin/PhoneServersTab'
import { answerDialog, renderWithDialogs } from './dialogsHarness'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const STATUS = {
  enabled: true, days: 90, callers: 3, bytes: 2048,
  agents: { support: { callers: 3, bytes: 2048 } }, phone_chats: 5, call_log_rows: 7,
}

function mockApi() {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (path.startsWith('/v1/admin/phone/external-data/forget')) {
      return ok({ callers_forgotten: 3, callers_busy_skipped: 1, phone_chats_deleted: 5,
                  phone_chats_busy_skipped: 0, call_log_rows_deleted: 7, caller_bytes_freed: 2048 })
    }
    if (path.startsWith('/v1/admin/phone/external-data')) {
      if (init?.method === 'PUT') return ok({ ...STATUS, ...JSON.parse(String(init.body)) })
      return ok(STATUS)
    }
    if (path.startsWith('/v1/admin/phone/routes')) return ok({ routes: [] })
    if (path.startsWith('/v1/admin/phone-servers')) return ok({ servers: [] })
    if (path.startsWith('/v1/agents')) return ok({ agents: [] })
    return ok({})
  })
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<QueryClientProvider client={qc}><PhoneServersTab /></QueryClientProvider>)
}

async function openCard() {
  fireEvent.click(screen.getByText('Caller Data'))
  return screen.findByTestId('caller-data-usage')
}

describe('caller data card', () => {
  afterEach(() => { fetchSpy.mockReset() })

  it('shows the window and what callers left behind', async () => {
    mockApi()
    renderTab()
    const usage = await openCard()
    expect(within(usage).getByText('3')).toBeInTheDocument()       // callers
    expect(within(usage).getByText('5')).toBeInTheDocument()       // conversations
    expect(within(usage).getByText('7')).toBeInTheDocument()       // call-log rows
    expect((screen.getByLabelText('Days to keep caller data') as HTMLInputElement).value).toBe('90')
  })

  it('saves the window on blur and the toggle on change', async () => {
    mockApi()
    renderTab()
    await openCard()
    const days = screen.getByLabelText('Days to keep caller data')
    fireEvent.change(days, { target: { value: '30' } })
    fireEvent.blur(days)
    await waitFor(() => {
      const put = fetchSpy.mock.calls.find(([p, i]) => p === '/v1/admin/phone/external-data' && i?.method === 'PUT')
      expect(put).toBeTruthy()
      expect(JSON.parse(String(put![1]!.body))).toEqual({ days: 30 })
    })
    const row = screen.getByText('Forget callers automatically').closest('div')!.parentElement!
    fireEvent.click(row.querySelector('[role="switch"]')!)
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(([p, i]) =>
        p === '/v1/admin/phone/external-data' && i?.method === 'PUT' && String(i.body) === JSON.stringify({ enabled: false }))).toBe(true)
    })
  })

  it('forget-all asks for confirmation and reports the counts', async () => {
    mockApi()
    renderTab()
    await openCard()
    fireEvent.click(screen.getByText('Forget all caller data now'))
    await act(async () => { await answerDialog('Forget everything', 'Forget all caller data now?') })
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(([p, i]) => p === '/v1/admin/phone/external-data/forget' && i?.method === 'POST')).toBe(true)
    })
    const region = screen.getByRole('region', { name: 'Notifications' })
    await waitFor(() => expect(region.textContent).toContain('3 callers'))
    expect(region.textContent).toContain('Skipped 1 callers')
  })

  it('a declined confirmation forgets nothing', async () => {
    mockApi()
    renderTab()
    await openCard()
    fireEvent.click(screen.getByText('Forget all caller data now'))
    await act(async () => { await answerDialog('Cancel', 'Forget all caller data now?') })
    expect(fetchSpy.mock.calls.some(([p]) => String(p).includes('/forget'))).toBe(false)
  })
})
