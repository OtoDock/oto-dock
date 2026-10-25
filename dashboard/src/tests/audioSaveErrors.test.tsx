// The audio saves the server refuses with a sentence show that sentence (it
// names the key and its rule), the way the phone settings save does; a
// refusal that carries none keeps the hook's own sentence.
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))

import {
  useDeleteProviderCredential, useSaveAudioSettings, useUpdateAudioPolicy, useUpdateAudioProvider,
} from '@/api/audio'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const refused = (status: number, body: string) => new Response(body, { status })

/** The message a mutation is refused with (a mutation that succeeds fails the test). */
async function messageOf(run: () => Promise<unknown>): Promise<string> {
  return run().then(() => { throw new Error('the save was not refused') }, (e: unknown) => (e as Error).message)
}

beforeEach(() => h.apiFetch.mockReset())

describe('audio saves', () => {
  it('the settings save shows the key the server refused', async () => {
    const { result } = renderHook(() => useSaveAudioSettings(), { wrapper })
    h.apiFetch.mockResolvedValueOnce(refused(400, '{"detail":"language_identify_threads must be a whole number"}'))
    expect(await messageOf(() => result.current.mutateAsync({ language_identify_threads: '1.5' })))
      .toBe('language_identify_threads must be a whole number')
    h.apiFetch.mockResolvedValueOnce(refused(500, 'Internal Server Error'))
    expect(await messageOf(() => result.current.mutateAsync({ vad_threshold: '0.4' })))
      .toBe('Failed to save audio settings')
  })

  it('the policy, provider and credential saves show the server sentence too', async () => {
    const policy = renderHook(() => useUpdateAudioPolicy(), { wrapper }).result
    h.apiFetch.mockResolvedValueOnce(refused(400, '{"detail":"Invalid policy. Allowed: [\'native_only\']"}'))
    expect(await messageOf(() => policy.current.mutateAsync({ chat_user_policy: 'native_only' })))
      .toBe("Invalid policy. Allowed: ['native_only']")
    h.apiFetch.mockResolvedValueOnce(refused(502, ''))
    expect(await messageOf(() => policy.current.mutateAsync({}))).toBe('Failed to update audio policy')

    const provider = renderHook(() => useUpdateAudioProvider(), { wrapper }).result
    h.apiFetch.mockResolvedValueOnce(refused(400, '{"detail":"endpointing_ms must be a number"}'))
    expect(await messageOf(() => provider.current.mutateAsync({ id: 3, data: {} })))
      .toBe('endpointing_ms must be a number')

    const credential = renderHook(() => useDeleteProviderCredential(), { wrapper }).result
    h.apiFetch.mockResolvedValueOnce(refused(404, '{"detail":"No credential stored for this provider"}'))
    expect(await messageOf(() => credential.current.mutateAsync(3)))
      .toBe('No credential stored for this provider')
    h.apiFetch.mockResolvedValueOnce(refused(500, ''))
    expect(await messageOf(() => credential.current.mutateAsync(3))).toBe('Failed to delete credential')
  })
})
