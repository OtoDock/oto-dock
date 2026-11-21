/**
 * The Phone page offers the server types this install offers: a hosted
 * install (and a self-host set to Twilio only) names Twilio as a fact, not a
 * one-choice list, and hides the AudioSocket port, which only a PBX of your
 * own dials. A server before
 * 2.0 names no types: every type shows.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import * as authApi from '@/api/auth'
import PhoneServersTab from '@/pages/admin/PhoneServersTab'
import { renderWithDialogs } from './dialogsHarness'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function mockApi(types?: string[]) {
  fetchSpy.mockImplementation(async (path: string) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (path.startsWith('/v1/admin/phone-servers')) return ok({ servers: [], ...(types ? { types } : {}) })
    if (path.startsWith('/v1/admin/phone/routes')) return ok({ routes: [] })
    if (path.startsWith('/v1/agents')) return ok({ agents: [] })
    return ok({})
  })
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<QueryClientProvider client={qc}><PhoneServersTab /></QueryClientProvider>)
}

async function serverTypes(): Promise<string[]> {
  fireEvent.click(await screen.findByText('+ Add phone server'))
  const select = screen.getByText('Server type').parentElement!.querySelector('select')!
  return within(select).getAllByRole('option').map(o => o.textContent || '')
}

afterEach(() => fetchSpy.mockReset())

describe('the phone page on an install that offers Twilio alone', () => {
  it('names Twilio as the type, offers no choice and hides the AudioSocket port', async () => {
    mockApi(['twilio'])
    renderTab()
    fireEvent.click(await screen.findByText('+ Add phone server'))
    expect(screen.getByText('Server type:').parentElement!.textContent).toBe('Server type: Twilio')
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Infrastructure'))
    expect(await screen.findByText('HTTP API Port')).toBeInTheDocument()
    expect(screen.queryByText('AudioSocket Port')).not.toBeInTheDocument()
  })

  it('offers every type when the server names none', async () => {
    mockApi()
    renderTab()
    expect(await serverTypes()).toEqual(['Twilio', 'FreePBX, automated', 'Asterisk, manual'])
    fireEvent.click(screen.getByText('Infrastructure'))
    expect(await screen.findByText('AudioSocket Port')).toBeInTheDocument()
  })
})
