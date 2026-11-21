import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── Admin, AI Engines: the pool's accounts keep their places while the admin
//     looks, take the pool's order a quiet spell after a change, and a tick on
//     a row the list keeps for its spell shows its value at once ───

vi.mock('@/pages/admin/ExecutionLayersTab.sections', () => ({
  SetupBanner: () => null,
  ConnectOtodockPrompt: () => null,
  HostedRelaySection: () => null,
}))
vi.mock('@/lib/nativeBridge', () => ({ setNativeAuthInProgress: vi.fn() }))

const { layersRef, saves } = vi.hoisted(() => ({
  layersRef: { current: [] as unknown[] },
  saves: [] as unknown[],
}))

vi.mock('@/lib/http/apiJson', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/lib/http/apiJson')>()
  // A save that never answers: what the page shows while it is pending.
  return { ...mod, apiJson: (path: string, init?: { body?: string }) => {
    saves.push([path, init?.body]); return new Promise(() => {})
  } }
})

vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  const idle = () => ({ mutate: () => {}, mutateAsync: async () => ({}), isPending: false, isError: false, error: null })
  return {
    ...mod,
    useListReading: () => ({ pending: false, reread: () => {} }),
    useAccountsLive: () => ({ data: undefined }),
    useAdminExecutionLayers: () => ({ data: layersRef.current, isLoading: false, error: null }),
    useDiscoverModels: idle,
    useDeleteSubscription: idle,
  }
})

import ExecutionLayersTab from '@/pages/admin/ExecutionLayersTab'
import { SubscriptionRow } from '@/pages/admin/ExecutionLayersTab.rows'
import { ADOPT_WINDOW_MS, REORDER_AFTER_MS } from '@/components/engines/useSettledOrder'
import type { Subscription } from '@/api/executionLayers'
import { claudeLike } from './fixtures/engines'

const row = (id: string, label: string, over: object = {}) => ({
  id, label, provider: 'anthropic', auth_type: 'oauth', status: 'active', oauth_email: '',
  active_sessions: 0, use_personal: true, contribute_platform: true, is_mine: true, owner_sub: 'local:admin',
  layer: 'claude-code-cli', created_at: '', updated_at: '', ...over,
}) as unknown as Subscription

const layer = (rows: Subscription[]) => [{
  name: 'claude-code-cli', display_name: 'Anthropic Claude Code', capabilities: claudeLike(),
  subscriptions: { platform: rows, user_count: 0 }, models: [], pool_stats: {},
}]

function wrapped(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>
}

beforeEach(() => { saves.length = 0 })

describe("Admin AI Engines: the pool's order", () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('a tick moves no row at once; the pool order comes after the quiet spell', () => {
    const a = row('a', 'Account A'), b = row('b', 'Account B')
    layersRef.current = layer([a, b])
    const { rerender } = render(wrapped(<ExecutionLayersTab />))
    fireEvent.click(screen.getByText('Claude Code CLI'))
    const names = () => screen.getAllByText(/^Account [AB]$/).map((el) => el.textContent)
    expect(names()).toEqual(['Account A', 'Account B'])
    act(() => { vi.advanceTimersByTime(ADOPT_WINDOW_MS + 10) })
    fireEvent.click(screen.getAllByLabelText('Personal use')[0])
    layersRef.current = layer([b, { ...a, use_personal: false } as Subscription])
    rerender(wrapped(<ExecutionLayersTab />))
    act(() => { vi.advanceTimersByTime(REORDER_AFTER_MS - 100) })
    expect(names()).toEqual(['Account A', 'Account B'])
    act(() => { vi.advanceTimersByTime(200) })
    expect(names()).toEqual(['Account B', 'Account A'])
  })
})

describe("Admin AI Engines: a tick on a row the list keeps", () => {
  it('shows the ticked value while it saves, though the row has left the list', async () => {
    // The untick took the row out of the pool's list: the list keeps its last data for the spell.
    render(wrapped(<SubscriptionRow sub={row('k', 'Kept', { contribute_platform: false })}
                                    layer="claude-code-cli" vendorId="anthropic" />))
    const shared = screen.getByLabelText('Shared with agents') as HTMLInputElement
    expect(shared.checked).toBe(false)
    fireEvent.click(shared)
    await waitFor(() => expect(saves).toHaveLength(1))     // the save is out and does not answer
    expect((screen.getByLabelText('Shared with agents') as HTMLInputElement).checked).toBe(true)
  })
})
