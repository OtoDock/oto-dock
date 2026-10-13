/**
 * Call Prompts (admin Phone tab): Restore Default writes the SERVER's shipped
 * text (GET /v1/admin/phone/settings/defaults), never a copy kept in the
 * dashboard, and stays disabled until that text has loaded.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import * as authApi from '@/api/auth'
import PhoneServersTab from '@/pages/admin/PhoneServersTab'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function mockApi(opts: { defaults: boolean }) {
  const puts: unknown[] = []
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (path === '/v1/admin/phone/settings/defaults') {
      if (!opts.defaults) return new Promise<Response>(() => undefined)   // never answers
      return ok({ context_inbound: 'SHIPPED INBOUND', context_outbound: 'SHIPPED OUTBOUND' })
    }
    if (path === '/v1/admin/phone/settings') {
      if (init?.method === 'PUT') { puts.push(JSON.parse(String(init.body))); return ok({ status: 'updated' }) }
      return ok({ context_inbound: 'my own inbound text', context_outbound: 'my own outbound text' })
    }
    if (path.startsWith('/v1/admin/phone/routes')) return ok({ routes: [] })
    if (path.startsWith('/v1/admin/phone-servers')) return ok({ servers: [] })
    if (path.startsWith('/v1/agents')) return ok({ agents: [] })
    if (path.startsWith('/v1/triggers')) return ok({ triggers: [] })
    return ok({})
  })
  return puts
}

async function openCallPrompts() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><PhoneServersTab /></QueryClientProvider>)
  fireEvent.click(await screen.findByText('Call Prompts'))
  return screen.findAllByText('Restore Default')
}

afterEach(() => { fetchSpy.mockReset() })

describe('Call Prompts', () => {
  it('Restore Default writes the server\'s shipped text', async () => {
    const puts = mockApi({ defaults: true })
    const [inbound, outbound] = await openCallPrompts()
    await waitFor(() => expect(inbound).not.toBeDisabled())
    fireEvent.click(inbound)
    await waitFor(() => expect(puts).toContainEqual({ context_inbound: 'SHIPPED INBOUND' }))
    fireEvent.click(outbound)
    await waitFor(() => expect(puts).toContainEqual({ context_outbound: 'SHIPPED OUTBOUND' }))
    expect(puts).toHaveLength(2)
  })

  it('stays disabled until the shipped text has loaded', async () => {
    const puts = mockApi({ defaults: false })
    const [inbound] = await openCallPrompts()
    expect(inbound).toBeDisabled()
    fireEvent.click(inbound)
    expect(puts).toHaveLength(0)
  })
})
