import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { PinnedApp } from '@/api/apps'

// The platform catalog on the host side (APPS.md "Platform catalog"):
// a server feed's snapshot over REST, deltas applied by id with a sequence,
// a gap re-requesting the snapshot, and platform methods answered as the
// viewer through one terminal result per call.
const snapshots = vi.hoisted(() => ({ calls: [] as string[], rows: [{ id: 'c1', title: 'One', status: 'idle' }] as Record<string, unknown>[], seq: 3 }))
const methodCalls = vi.hoisted(() => ({ calls: [] as unknown[][], result: { ok: true, result: { sub: 'u1' } } as { ok: boolean; result?: unknown; reason?: string } }))
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  useAppState: () => ({ data: undefined }),
  warmApp: vi.fn(),
  fetchCatalogFeed: vi.fn(async (_id: string, feed: string) => {
    snapshots.calls.push(feed)
    return { rows: snapshots.rows, seq: snapshots.seq }
  }),
  callPlatformMethod: vi.fn(async (...args: unknown[]) => {
    methodCalls.calls.push(args)
    return methodCalls.result
  }),
}))

import AppFrame from '@/components/apps/AppFrame'
import { _resetCatalogSubsForTests, emitAppLive, resendCatalogSubscriptions, setCatalogSender } from '@/lib/appLive'

// What the tab sent on the socket (`catalog_subscribe` / `catalog_unsubscribe`).
const socket: Record<string, unknown>[] = []

function mkApp(over: Partial<PinnedApp> = {}): PinnedApp {
  return {
    id: 'app-1', slug: 'board', title: 'Board', scope: 'shared', position: 0,
    rel_path: 'workspace/apps/board.html', updated_at: '',
    actions: [
      { id: 'sessions', label: 'Sessions', type: 'data_feed', feed: 'sessions' },
      { id: 'me', label: 'Me', type: 'platform', method: 'viewer.me' },
    ],
    actions_sig: 'sig', actions_approved: true, approval_stale: false,
    can_approve: true, can_manage: true, hidden_for_me: false, viewer_role: 'manager', ...over,
  }
}

function renderFrame(app = mkApp()) {
  const qc = new QueryClient()
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={app} agent="a1" /></MemoryRouter>
    </QueryClientProvider>,
  )
  const frame = utils.container.querySelector('iframe') as HTMLIFrameElement
  const posted: Record<string, unknown>[] = []
  ;(frame.contentWindow as any).postMessage = (m: Record<string, unknown>) => { posted.push(m) }
  const post = (data: Record<string, unknown>) =>
    window.dispatchEvent(new MessageEvent('message', { data: { source: 'otodock-artifact', v: 1, ...data }, source: frame.contentWindow }))
  return { ...utils, frame, posted, post }
}

const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }
const feedUpdates = (posted: Record<string, unknown>[], feed: string) =>
  posted.filter((m) => m.type === 'feed_update' && m.feed === feed)
const lastUpdate = (posted: Record<string, unknown>[], feed: string) => {
  const all = feedUpdates(posted, feed)
  return all[all.length - 1]
}

describe('AppFrame — platform catalog', () => {
  beforeEach(() => {
    snapshots.calls = []
    snapshots.rows = [{ id: 'c1', title: 'One', status: 'idle' }]
    snapshots.seq = 3
    methodCalls.calls = []
    methodCalls.result = { ok: true, result: { sub: 'u1' } }
    socket.length = 0
    _resetCatalogSubsForTests()
    setCatalogSender((f) => { socket.push(f) })
  })
  afterEach(() => vi.useRealTimers())

  it('answers a server feed with the REST snapshot, applies deltas by id and re-requests on a gap', async () => {
    const { posted, post, unmount } = renderFrame()
    post({ type: 'feed_subscribe', feed: 'sessions' })
    await flush()
    // The socket learns the feed before the snapshot is asked for.
    expect(socket).toEqual([{ type: 'catalog_subscribe', agent: 'a1', feed: 'sessions' }])
    expect(snapshots.calls).toEqual(['sessions'])
    expect(lastUpdate(posted, 'sessions').rows).toEqual([{ id: 'c1', title: 'One', status: 'idle' }])
    // The next delta in sequence updates the row in place.
    await act(async () => {
      emitAppLive({ type: 'catalog', agent: 'a1', feed: 'sessions', seq: 4, delta: { id: 'c1', status: 'streaming' } })
    })
    expect(lastUpdate(posted, 'sessions').rows).toEqual([{ id: 'c1', title: 'One', status: 'streaming' }])
    // A new id lands at the top; another agent's frame is ignored.
    await act(async () => {
      emitAppLive({ type: 'catalog', agent: 'other', feed: 'sessions', seq: 99, delta: { id: 'zz' } })
      emitAppLive({ type: 'catalog', agent: 'a1', feed: 'sessions', seq: 5, delta: { id: 'c2', title: 'Two', created: true } })
    })
    expect((lastUpdate(posted, 'sessions').rows as any[]).map((r) => r.id)).toEqual(['c2', 'c1'])
    // A gap (seq 7 after 5) re-requests the snapshot instead of trusting the rows.
    snapshots.rows = [{ id: 'fresh' }]
    snapshots.seq = 7
    await act(async () => {
      emitAppLive({ type: 'catalog', agent: 'a1', feed: 'sessions', seq: 7, delta: { id: 'c3' } })
    })
    await flush()
    expect(snapshots.calls).toEqual(['sessions', 'sessions'])
    expect(lastUpdate(posted, 'sessions').rows).toEqual([{ id: 'fresh' }])
    // A reopened socket re-sends the subscriptions and re-requests every
    // live server feed; an unmounted frame drops its subscription.
    socket.length = 0
    resendCatalogSubscriptions()
    await act(async () => { emitAppLive({ type: 'catalog_resync' }) })
    await flush()
    expect(socket).toEqual([{ type: 'catalog_subscribe', agent: 'a1', feed: 'sessions' }])
    expect(snapshots.calls).toHaveLength(3)
    unmount()
    expect(socket[socket.length - 1]).toEqual({ type: 'catalog_unsubscribe', agent: 'a1', feed: 'sessions' })
  })

  it('refuses an undeclared feed and answers platform methods as the viewer', async () => {
    const { posted, post } = renderFrame()
    post({ type: 'feed_subscribe', feed: 'tasks' })
    await flush()
    expect(lastUpdate(posted, 'tasks').error).toMatch(/not declared/)
    post({ type: 'platform_call', call_id: 'p1', method: 'viewer.me', args: null })
    await flush()
    expect(methodCalls.calls).toEqual([['app-1', 'viewer.me', null]])
    const result = posted.find((m) => m.type === 'platform_result' && m.call_id === 'p1')!
    expect(result).toMatchObject({ ok: true, result: { sub: 'u1' } })
    // Undeclared: refused without a request.
    post({ type: 'platform_call', call_id: 'p2', method: 'tasks.run_result', args: {} })
    await flush()
    expect(methodCalls.calls).toHaveLength(1)
    expect(posted.find((m) => m.type === 'platform_result' && m.call_id === 'p2')).toMatchObject({ ok: false })
  })

  it('a server feed is unavailable in a read-only host, a declared floor is judged on the viewer', async () => {
    const { posted, post } = renderFrame(mkApp({
      viewer_role: 'viewer',
      actions: [{ id: 'sessions', label: 'Sessions', type: 'data_feed', feed: 'sessions', min_role: 'editor' }],
    }))
    post({ type: 'feed_subscribe', feed: 'sessions' })
    await flush()
    expect(lastUpdate(posted, 'sessions').error).toMatch(/editor role/)
    expect(snapshots.calls).toEqual([])
    expect(socket).toEqual([])
  })
})
