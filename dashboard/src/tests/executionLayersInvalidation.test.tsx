// Every engine/subscription/model mutation must refresh the public
// execution-layers query the chat + agent-settings model pickers read (cached
// five minutes), not only the admin tab. A discovered model used to stay
// invisible in the pickers until a full page reload.
import { describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
}))

import {
  useListReading,
  useAddSubscription,
  useBulkAddModels,
  useDeleteSubscription,
  useUpdateModel,
  useUpdateSubscription,
} from '@/api/executionLayers'

// The accounts lists' live numbers too: an older poll's answer never outlives a change (the numbers it shows).
const READER_KEYS = ['execution-layers', 'user-execution-layers', 'admin-execution-layers', 'accounts-live']

function harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const invalidated = () => spy.mock.calls.map((c) => (c[0] as { queryKey?: unknown[] })?.queryKey?.[0])
  return { wrapper, invalidated }
}

describe('engine mutations refresh every reader', () => {
  it('bulk-adding discovered models', async () => {
    const { wrapper, invalidated } = harness()
    const { result } = renderHook(() => useBulkAddModels(), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({
        layer: 'direct-llm', provider: 'openai_compatible',
        models: [{ model_id: 'qwen3.6-35b-a3b', display_name: 'qwen3.6-35b-a3b' }],
      })
    })
    expect(invalidated()).toEqual(expect.arrayContaining(READER_KEYS))
  })

  it('adding, updating and removing a subscription', async () => {
    const { wrapper, invalidated } = harness()
    const add = renderHook(() => useAddSubscription(), { wrapper })
    await act(async () => {
      await add.result.current.mutateAsync({
        layer: 'direct-llm', provider: 'ollama', auth_type: 'local_endpoint',
        endpoint_url: 'http://192.168.1.8:11434',
      })
    })
    expect(invalidated()).toEqual(expect.arrayContaining(READER_KEYS))

    const upd = renderHook(() => useUpdateSubscription(), { wrapper })
    await act(async () => {
      await upd.result.current.mutateAsync({ layer: 'direct-llm', id: 's1', status: 'disabled' })
    })
    expect(invalidated().filter((k) => k === 'execution-layers').length).toBeGreaterThanOrEqual(2)

    const del = renderHook(() => useDeleteSubscription(), { wrapper })
    await act(async () => {
      await del.result.current.mutateAsync({ layer: 'direct-llm', id: 's1' })
    })
    expect(invalidated().filter((k) => k === 'execution-layers').length).toBeGreaterThanOrEqual(3)
  })

  it('toggling a model', async () => {
    const { wrapper, invalidated } = harness()
    const { result } = renderHook(() => useUpdateModel(), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({ layer: 'codex-cli', id: 7, enabled: false })
    })
    expect(invalidated()).toEqual(expect.arrayContaining(READER_KEYS))
  })
})

describe("a list's own read", () => {
  it('reads the live numbers again with the list', () => {
    const { wrapper, invalidated } = harness()
    const { result } = renderHook(() => useListReading('own'), { wrapper })
    act(() => { result.current.reread() })
    expect(invalidated()).toEqual(expect.arrayContaining(['user-execution-layers', 'accounts-live']))
  })
})
