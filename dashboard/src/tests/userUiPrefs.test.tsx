import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { useMyUiPrefs, useUpdateMyUiPrefs, useHydrateUiPrefs } from '@/api/userUiPrefs'
import {
  useAgentPrefsStore,
  hydrateLastInteractiveFromServer,
  __resetUiPrefsRoamingForTests,
} from '@/store/agentPrefsStore'
import * as authApi from '@/api/auth'

// ─── /v1/users/me/ui-prefs hooks + the once-per-session hydration into
//     agentPrefsStore.lastInteractive (Item 5d-3 roaming) ────────────────────

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

function mockApi(bag: () => Record<string, unknown>) {
  fetchSpy.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === '/v1/users/me/ui-prefs') {
      if (options?.method === 'PUT') return { ok: true, json: async () => JSON.parse(options.body as string) } as Response
      return { ok: true, json: async () => bag() } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

beforeEach(() => {
  __resetUiPrefsRoamingForTests()
  useAgentPrefsStore.getState().reset()
})
afterEach(() => fetchSpy.mockReset())

describe('useMyUiPrefs / useUpdateMyUiPrefs', () => {
  it('GET returns the server bag', async () => {
    mockApi(() => ({ last_execution_mode: { helper: 'interactive' } }))
    const { result } = renderHook(() => useMyUiPrefs(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.data).toBeDefined())
    expect(result.current.data).toEqual({ last_execution_mode: { helper: 'interactive' } })
  })

  it('PUT sends the payload and invalidates the query (refetch fires)', async () => {
    mockApi(() => ({}))
    const wrapper = makeWrapper()
    const q = renderHook(() => useMyUiPrefs(), { wrapper })
    await waitFor(() => expect(q.result.current.data).toBeDefined())
    const gets = () => fetchSpy.mock.calls.filter(([, o]) => !(o as RequestInit | undefined)?.method).length
    const before = gets()

    const m = renderHook(() => useUpdateMyUiPrefs(), { wrapper })
    await act(async () => { await m.result.current.mutateAsync({ last_execution_mode: { helper: '-p' } }) })
    const put = fetchSpy.mock.calls.find(([, o]) => (o as RequestInit | undefined)?.method === 'PUT')
    expect(put).toBeDefined()
    expect(JSON.parse((put![1] as RequestInit).body as string)).toEqual({ last_execution_mode: { helper: '-p' } })
    await waitFor(() => expect(gets()).toBeGreaterThan(before))
  })
})

describe('useHydrateUiPrefs', () => {
  it('fills the sticky store from the server map once resolved', async () => {
    mockApi(() => ({ last_execution_mode: { helper: 'interactive', coder: '-p' } }))
    renderHook(() => useHydrateUiPrefs(), { wrapper: makeWrapper() })
    await waitFor(() =>
      expect(useAgentPrefsStore.getState().lastInteractive).toEqual({ helper: 'interactive', coder: '-p' }))
  })

  it('an empty bag still spends the once-per-session shot (no later clobber)', async () => {
    mockApi(() => ({}))
    const { result } = renderHook(
      () => { useHydrateUiPrefs(); return useMyUiPrefs() },
      { wrapper: makeWrapper() },
    )
    await waitFor(() => expect(result.current.data).toBeDefined())
    // A second (late) hydration attempt with data must be a no-op now.
    hydrateLastInteractiveFromServer({ helper: 'interactive' })
    expect(useAgentPrefsStore.getState().lastInteractive).toEqual({})
  })
})
