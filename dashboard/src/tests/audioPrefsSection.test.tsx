import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { AudioPrefsSection } from '@/components/audio/AudioPrefsSection'
import { probeWithTimeout } from '@/audio/types'
import * as authApi from '@/api/auth'

// ─── AudioPrefsSection availability states (a failed capability resolve must
//     never render as "turned off by the administrator") ─────────────────────

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function mockApi(capability: () => Response | Promise<Response>) {
  fetchSpy.mockImplementation(async (path: string) => {
    if (path.startsWith('/v1/audio/capability')) return capability()
    return { ok: true, json: async () => ({}) } as Response
  })
}

const CAP_OK = {
  tts: 'either', stt: 'either', tts_provider_id: 1, stt_provider_id: 1,
  reason: 'TTS: user choice. STT: user choice.', icons_enabled: true,
}

describe('AudioPrefsSection', () => {
  afterEach(() => fetchSpy.mockReset())

  it('a failed capability resolve reads as a failure, not admin-off', async () => {
    mockApi(() => ({ ok: false, status: 500 } as Response))
    render(<AudioPrefsSection />, { wrapper })
    await waitFor(() =>
      expect(screen.getByText(/Couldn't check chat audio availability/)).toBeInTheDocument())
    expect(screen.queryByText(/turned off by the administrator/)).toBeNull()
  })

  it('admin-off message renders only when the server disables chat audio', async () => {
    mockApi(() => ({
      ok: true,
      json: async () => ({ ...CAP_OK, tts: 'unavailable', stt: 'unavailable', icons_enabled: false }),
    } as Response))
    render(<AudioPrefsSection />, { wrapper })
    await waitFor(() =>
      expect(screen.getByText(/turned off by the administrator/)).toBeInTheDocument())
  })

  it('user-choice policy renders the TTS/STT mode radios', async () => {
    mockApi(() => ({ ok: true, json: async () => CAP_OK } as Response))
    render(<AudioPrefsSection />, { wrapper })
    await waitFor(() => expect(screen.getByText('Play voice (TTS)')).toBeInTheDocument())
    expect(screen.getByText('Dictation (STT)')).toBeInTheDocument()
    expect(screen.queryByText(/turned off by the administrator/)).toBeNull()
  })
})

describe('probeWithTimeout', () => {
  it('answers false when the probe hangs', async () => {
    await expect(probeWithTimeout(new Promise<boolean>(() => {}), 20)).resolves.toBe(false)
  })

  it('passes a settled probe through', async () => {
    await expect(probeWithTimeout(Promise.resolve(true), 1000)).resolves.toBe(true)
  })
})
