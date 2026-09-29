import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import AppFrame, { _resetViewerClaimsForTests } from '@/components/apps/AppFrame'
import { emitFileUpdate } from '@/lib/fileUpdates'
import { emitAppLive } from '@/lib/appLive'
import { _resetFocusForTests, currentFocus } from '@/lib/focus'
import type { AppActionCall, AppActionResult, AppState, PinnedApp } from '@/api/apps'

// The batch executor: by default every call acks `sent`; a test installs a
// one-shot implementation to answer per call.
type OnResult = (callId: string, r: AppActionResult) => void
const fireAppActions = vi.hoisted(() => vi.fn(
  async (_appId: string, calls: AppActionCall[], onResult: OnResult) => {
    for (const c of calls) onResult(c.call_id, { status: 'sent' })
  },
))
const warmApp = vi.hoisted(() => vi.fn())
// The viewer token a folder app's page gets after `ready`.
const mintViewerToken = vi.hoisted(() => vi.fn(async (_appId: string) => ({ token: 'vt-1', exp: 9_999_999_999, ttl: 600 })))
// The state document the frame would fetch — a test sets it before render.
const appStateStub = vi.hoisted(() => ({ current: undefined as AppState | undefined }))
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  fireAppActions,
  warmApp,
  mintViewerToken,
  useAppState: () => ({ data: appStateStub.current }),
}))

// The active_chats feed source — stubbed so feed tests control the rows.
const activeChatsRows = vi.hoisted(() => ({ current: [] as unknown[] }))
vi.mock('@/hooks/useActiveChats', () => ({
  useActiveChats: (enabled = true) => (enabled ? activeChatsRows.current : []),
}))

function mkApp(over: Partial<PinnedApp> = {}): PinnedApp {
  return {
    id: 'app-1', slug: 'brief', title: 'Brief', scope: 'shared', position: 0,
    rel_path: 'workspace/apps/brief.html', updated_at: 't',
    actions: [
      { id: 'go', label: 'Go', type: 'fire_task', task_id: 't-1' },
      { id: 'ask', label: 'Ask', type: 'send_prompt', prompt: 'Analyze {{month}}' },
      { id: 'tool', label: 'Tool', type: 'mcp_tool', mcp: 'ha-mcp', tool: 'toggle' },
    ],
    actions_sig: 'sig', actions_approved: true, approval_stale: false,
    can_approve: true, can_manage: true, hidden_for_me: false,
    ...over,
  }
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

// AppFrame navigates through the router (otodock.open) — every render sits
// in one, with a probe that shows where the viewer went — and keeps the
// state document in the query cache, so a client wraps it too.
let queryClient = new QueryClient()
function renderFrame(ui: ReactElement) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/chat/a1']}>{ui}<LocationProbe /></MemoryRouter>
    </QueryClientProvider>,
  )
}

function getFrame(container: HTMLElement): HTMLIFrameElement {
  const f = container.querySelector('iframe')
  expect(f).toBeTruthy()
  return f as HTMLIFrameElement
}

function ackSpy(f: HTMLIFrameElement) {
  const spy = vi.fn()
  ;(f.contentWindow as any).postMessage = spy
  return spy
}

const post = (f: HTMLIFrameElement, data: Record<string, unknown>, source?: Window | null) =>
  window.dispatchEvent(new MessageEvent('message', {
    data: { source: 'otodock-artifact', v: 1, ...data },
    source: source === undefined ? f.contentWindow : source,
  }))

// A page authored against the older runtime posts one app_action; the
// current runtime coalesces a macrotask's calls into app_actions.
const appAction = (f: HTMLIFrameElement, id: string, args?: unknown, source?: Window | null) =>
  post(f, { type: 'app_action', id, args }, source)
const appActions = (f: HTMLIFrameElement, calls: { call_id: string; id: string; args?: unknown }[]) =>
  post(f, { type: 'app_actions', calls })

// Batches flush on a timer (setTimeout 0, then the one-per-second pace).
const flush = async (ms = 0) => {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

describe('AppFrame', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    fireAppActions.mockClear()
    warmApp.mockClear()
    appStateStub.current = undefined
    queryClient = new QueryClient()
    _resetFocusForTests()
    _resetViewerClaimsForTests()
  })
  afterEach(() => { vi.useRealTimers() })

  it('renders the cookie-authed serve URL in a scripts-only sandbox', () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    expect(f.getAttribute('sandbox')).toBe('allow-scripts')
    expect(f.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(f.getAttribute('allow')).toBe('')
    expect(f.getAttribute('src')).toContain('/v1/apps/app-1/html?theme=')
  })

  it('asks the platform to keep an approved app with tool buttons warm', () => {
    renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    expect(warmApp).toHaveBeenCalledWith('app-1')
    warmApp.mockClear()
    // Nothing to warm: no tool buttons, or an approval still pending.
    renderFrame(<AppFrame app={mkApp({ actions: [] })} agent="a1" />)
    renderFrame(<AppFrame app={mkApp({ actions_approved: false })} agent="a1" />)
    expect(warmApp).not.toHaveBeenCalled()
  })

  it('answers every button itself in the rendered check, with no call', async () => {
    // The platform's render is a synthetic viewer; a refused request would
    // be a console error the report counts against the page.
    const { container } = renderFrame(<AppFrame app={mkApp({ kind: 'folder', release_sha: 'a'.repeat(64) })} agent="a1" renderSha={'b'.repeat(64)} />)
    const f = getFrame(container)
    expect(f.getAttribute('src')).toContain('&render=1')
    const spy = ackSpy(f)
    await act(async () => { appActions(f, [{ call_id: 'r1', id: 'tool' }, { call_id: 'r2', id: 'go' }, { call_id: 'r3', id: 'ask' }]) })
    await flush()
    expect(fireAppActions).not.toHaveBeenCalled()
    expect(warmApp).not.toHaveBeenCalled()
    for (const id of ['r1', 'r2', 'r3']) {
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'action_ack', status: 'unavailable', reason: 'not available in the rendered check', call_id: id }), '*',
      )
    }
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_result', id: 'tool', ok: false, result: 'not available in the rendered check', call_id: 'r1' }), '*',
    )
    expect(spy.mock.calls.filter(([m]) => m.type === 'action_result')).toHaveLength(1)
  })

  it('routes fire_task actions to the batch executor and acks the result', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'go') })
    await flush()
    expect(fireAppActions).toHaveBeenCalledTimes(1)
    expect(fireAppActions.mock.calls[0][0]).toBe('app-1')
    expect(fireAppActions.mock.calls[0][1]).toEqual([
      expect.objectContaining({ action_id: 'go', args: null }),
    ])
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'otodock-host', type: 'action_ack', status: 'sent' }), '*',
    )
    // fire_task never posts a tool result into the frame.
    expect(spy).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_result' }), '*',
    )
  })

  it('coalesced calls go up as ONE batch and come back one result per call_id', async () => {
    fireAppActions.mockImplementationOnce(async (_a, calls, onResult) => {
      for (const c of calls) {
        onResult(c.call_id, c.action_id === 'tool'
          ? { status: 'done', result: `out:${c.call_id}` }
          : { status: 'sent' })
      }
    })
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => {
      appActions(f, [
        { call_id: 'c1', id: 'tool', args: { room: 'office' } },
        { call_id: 'c2', id: 'tool', args: { room: 'hall' } },
        { call_id: 'c3', id: 'go' },
      ])
    })
    await flush()
    expect(fireAppActions).toHaveBeenCalledTimes(1)
    expect(fireAppActions.mock.calls[0][1]).toEqual([
      { call_id: 'c1', action_id: 'tool', args: { room: 'office' } },
      { call_id: 'c2', action_id: 'tool', args: { room: 'hall' } },
      { call_id: 'c3', action_id: 'go', args: null },
    ])
    for (const id of ['c1', 'c2']) {
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'action_result', id: 'tool', call_id: id, ok: true, result: `out:${id}` }), '*',
      )
    }
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'sent', call_id: 'c3' }), '*',
    )
    expect(spy.mock.calls.filter(([m]) => m.type === 'action_result')).toHaveLength(2)
  })

  it('paces batches to one per second — later calls wait, never fail', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    ackSpy(f)
    await act(async () => { appActions(f, [{ call_id: 'c1', id: 'go' }]) })
    await flush()
    expect(fireAppActions).toHaveBeenCalledTimes(1)
    await act(async () => { appActions(f, [{ call_id: 'c2', id: 'tool', args: { a: 1 } }]) })
    await flush(300)
    expect(fireAppActions).toHaveBeenCalledTimes(1)  // inside the window: queued
    await flush(800)
    expect(fireAppActions).toHaveBeenCalledTimes(2)
    expect(fireAppActions.mock.calls[1][1]).toEqual([{ call_id: 'c2', action_id: 'tool', args: { a: 1 } }])
  })

  it('routes mcp_tool actions with args and bridges the result into the frame', async () => {
    fireAppActions.mockImplementationOnce(async (_a, calls, onResult) => {
      onResult(calls[0].call_id, { status: 'done', result: '42' })
    })
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'tool', { room: 'office' }) })
    await flush()
    expect(fireAppActions.mock.calls[0][1]).toEqual([
      expect.objectContaining({ action_id: 'tool', args: { room: 'office' } }),
    ])
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'done' }), '*',
    )
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        source: 'otodock-host', type: 'action_result',
        id: 'tool', ok: true, result: '42',
      }), '*',
    )
  })

  it('bridges mcp_tool failures as an action_result with ok=false', async () => {
    fireAppActions.mockImplementationOnce(async (_a, calls, onResult) => {
      onResult(calls[0].call_id, { status: 'error', reason: 'MCP unavailable' })
    })
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'tool') })
    await flush()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'action_result', id: 'tool', ok: false, result: 'MCP unavailable',
      }), '*',
    )
  })

  it('routes send_prompt actions to the host router with args', async () => {
    const onSendPrompt = vi.fn(async () => ({ status: 'queued' }))
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" onSendPrompt={onSendPrompt} />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'ask', { month: 'May' }) })
    expect(onSendPrompt).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'app-1' }),
      { id: 'ask', label: 'Ask', prompt: 'Analyze {{month}}' },
      { month: 'May' },
    )
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'queued' }), '*',
    )
    expect(fireAppActions).not.toHaveBeenCalled()
  })

  it('denies unapproved manifests, unknown ids, and free-form sends', async () => {
    const onSendPrompt = vi.fn(async () => ({ status: 'sent' }))
    const { container } = renderFrame(
      <AppFrame app={mkApp({ actions_approved: false })} agent="a1" onSendPrompt={onSendPrompt} />,
    )
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'go') })
    await flush()
    expect(fireAppActions).not.toHaveBeenCalled()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'denied', reason: 'actions not approved' }), '*',
    )
    await act(async () => { appAction(f, 'nope') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'denied', reason: 'unknown action' }), '*',
    )
    // otodock.send (free-form backchannel) has no chat binding in app context.
    await act(async () => { post(f, { type: 'action', payload: {} }) })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'unavailable' }), '*',
    )
    expect(onSendPrompt).not.toHaveBeenCalled()
  })

  it('rate-limits rapid action sends client-side', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'go') })
    await act(async () => { appAction(f, 'go') })
    await flush()
    expect(fireAppActions).toHaveBeenCalledTimes(1)
    expect(fireAppActions.mock.calls[0][1]).toHaveLength(1)
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'denied', reason: 'rate limited' }), '*',
    )
  })

  it('mcp_tool refusals still deliver a terminal action_result (no forever-spinner)', async () => {
    // Server-side denial (e.g. 429 min-interval) — the batch executor maps
    // it to {status:'denied'}: the page must still get its result event.
    fireAppActions.mockImplementationOnce(async (_a, calls, onResult) => {
      onResult(calls[0].call_id, { status: 'denied', reason: 'Too fast — try again in a moment' })
    })
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'tool') })
    await flush()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'action_result', id: 'tool', ok: false,
        result: 'Too fast — try again in a moment',
      }), '*',
    )
    // Client-side guard drop (same action+args twice inside 400ms) —
    // terminal result too, not just the ack.
    spy.mockClear()
    await act(async () => { appAction(f, 'tool') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'denied', reason: 'rate limited' }), '*',
    )
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_result', id: 'tool', ok: false }), '*',
    )
    expect(fireAppActions).toHaveBeenCalledTimes(1)
  })

  it('mcp_tool transport failures resolve with a terminal result', async () => {
    // fireAppActions never rejects: a transport failure settles every
    // pending call `denied` with the network reason.
    fireAppActions.mockImplementationOnce(async (_a, calls, onResult) => {
      onResult(calls[0].call_id, { status: 'denied', reason: 'Network error — the call may not have reached the server' })
    })
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'tool') })
    await flush()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'denied' }), '*',
    )
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'action_result', id: 'tool', ok: false,
        result: expect.stringContaining('Network error'),
      }), '*',
    )
  })

  it('rates non-prompt actions PER ACTION — a data panel fires its queries together', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    ackSpy(f)
    // Two DIFFERENT declared actions in one macrotask (the on-load refresh
    // pattern) — both ride the same batch; the server enforces its own
    // per-action interval + in-flight.
    await act(async () => { appActions(f, [{ call_id: 'a', id: 'go' }, { call_id: 'b', id: 'tool' }]) })
    await flush()
    expect(fireAppActions).toHaveBeenCalledTimes(1)
    expect(fireAppActions.mock.calls[0][1]).toHaveLength(2)
  })

  it('ignores messages from foreign windows', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    getFrame(container)
    await act(async () => { appAction(getFrame(container), 'go', undefined, window) })
    await flush()
    expect(fireAppActions).not.toHaveBeenCalled()
  })

  it('answers declared feed subscriptions with a snapshot; refuses the rest', async () => {
    activeChatsRows.current = [{ id: 'c1', agent: 'beta', title: 'Turn', phase: 'streaming' }]
    const app = mkApp({
      actions: [
        { id: 'live', label: 'Live', type: 'data_feed', feed: 'active_chats' },
        { id: 'lanes', label: 'Lanes', type: 'data_feed', feed: 'project_lanes' },
      ],
    })
    const { container } = renderFrame(<AppFrame app={app} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    const subscribe = (feed: string) => post(f, { type: 'feed_subscribe', feed })

    await act(async () => { subscribe('active_chats') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        source: 'otodock-host', type: 'feed_update', feed: 'active_chats',
        rows: activeChatsRows.current,
      }), '*',
    )
    // project_lanes declared but NOT wired by this host view → error, not rows.
    await act(async () => { subscribe('project_lanes') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'feed_update', feed: 'project_lanes',
        error: expect.stringContaining('project dock'),
      }), '*',
    )
    // Undeclared feed → refused.
    await act(async () => { subscribe('secrets') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'feed_update', feed: 'secrets',
        error: expect.stringContaining('not declared'),
      }), '*',
    )
    // otodock.action() on a feed id is a category error, cleanly acked.
    await act(async () => { appAction(f, 'live') })
    await flush()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'denied',
        reason: expect.stringContaining('otodock.feed') }), '*',
    )
    expect(fireAppActions).not.toHaveBeenCalled()
  })

  it('refuses feed subscriptions until the manifest is approved', async () => {
    const app = mkApp({
      actions: [{ id: 'live', label: 'Live', type: 'data_feed', feed: 'active_chats' }],
      actions_approved: false,
    })
    const { container } = renderFrame(<AppFrame app={app} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { post(f, { type: 'feed_subscribe', feed: 'active_chats' }) })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'feed_update', feed: 'active_chats',
        error: 'actions not approved',
      }), '*',
    )
  })

  it('pushes project_lanes to a live subscription when the host rows change', async () => {
    const app = mkApp({
      actions: [{ id: 'lanes', label: 'Lanes', type: 'data_feed', feed: 'project_lanes' }],
    })
    const lanes1 = [{ id: 'l1', status: 'generating' }]
    const lanes2 = [{ id: 'l1', status: 'idle' }]
    const { container, rerender } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><AppFrame app={app} agent="a1" projectLanes={lanes1} /></MemoryRouter>
      </QueryClientProvider>,
    )
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { post(f, { type: 'feed_subscribe', feed: 'project_lanes' }) })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'feed_update', rows: lanes1 }), '*',
    )
    // The iframe's load event fires AFTER the page already subscribed (the
    // subscription happens at script parse time) — it must NOT wipe the
    // subscription (regression: found live on T1, pushes stopped after the
    // initial snapshot).
    await act(async () => { f.dispatchEvent(new Event('load')) })
    await act(async () => {
      rerender(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter><AppFrame app={app} agent="a1" projectLanes={lanes2} /></MemoryRouter>
        </QueryClientProvider>,
      )
    })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'feed_update', rows: lanes2 }), '*',
    )
  })

  it('reloads on a matching file_updated broadcast only', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const before = f.getAttribute('src')
    await act(async () => {
      emitFileUpdate({ agent_slug: 'other-agent', rel_path: 'workspace/apps/brief.html' })
      emitFileUpdate({ agent_slug: 'a1', rel_path: 'workspace/apps/other.html' })
    })
    expect(f.getAttribute('src')).toBe(before)
    await act(async () => {
      emitFileUpdate({ agent_slug: 'a1', rel_path: 'workspace/apps/brief.html' })
    })
    expect(container.querySelector('iframe')!.getAttribute('src')).not.toBe(before)
  })

  it('reloads once per deploy: the deploy frame and file_updated within the window are one reload', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp({ actions_sig: 'sig-1' })} agent="a1" />)
    const src = () => container.querySelector('iframe')!.getAttribute('src')
    const first = src()
    await act(async () => {
      emitAppLive({ type: 'app_deployed', app_id: 'other', release: 2, actions_sig: 'sig-1' })
    })
    expect(src()).toBe(first)
    await act(async () => {
      emitAppLive({ type: 'app_deployed', app_id: 'app-1', release: 2, actions_sig: 'sig-1' })
      emitFileUpdate({ agent_slug: 'a1', rel_path: 'workspace/apps/brief.html' })
    })
    const second = src()
    expect(second).not.toBe(first)
    // Both announcements landed inside the window: one reload.
    expect(second!.endsWith('v=1')).toBe(true)
    await flush(1000)
    await act(async () => {
      emitFileUpdate({ agent_slug: 'a1', rel_path: 'workspace/apps/brief.html' })
    })
    expect(src()!.endsWith('v=2')).toBe(true)
  })

  it('waits for the row to carry the deploy signature before reloading', async () => {
    const { container, rerender } = renderFrame(<AppFrame app={mkApp({ actions_sig: 'sig-1' })} agent="a1" />)
    const src = () => container.querySelector('iframe')!.getAttribute('src')
    const first = src()
    await act(async () => {
      emitAppLive({ type: 'app_deployed', app_id: 'app-1', release: 3, actions_sig: 'sig-2' })
    })
    // The manifest changed: the stale row must not be reloaded against.
    expect(src()).toBe(first)
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={mkApp({ actions_sig: 'sig-2' })} agent="a1" /><LocationProbe /></MemoryRouter>
      </QueryClientProvider>,
    )
    await flush(0)
    expect(src()).not.toBe(first)
  })

  it('reloads once when the approval lands, so the page asks for its feeds again', async () => {
    const { container, rerender } = renderFrame(<AppFrame app={mkApp({ actions_approved: false })} agent="a1" />)
    const src = () => container.querySelector('iframe')!.getAttribute('src')
    const first = src()
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={mkApp({ actions_approved: true })} agent="a1" /><LocationProbe /></MemoryRouter>
      </QueryClientProvider>,
    )
    await flush(0)
    expect(src()).not.toBe(first)
    // Staying approved is not a change.
    const second = src()
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={mkApp({ actions_approved: true, title: 'Renamed' })} agent="a1" /><LocationProbe /></MemoryRouter>
      </QueryClientProvider>,
    )
    await flush(0)
    expect(src()).toBe(second)
  })
})

describe('AppFrame open_url bridge', () => {
  const openUrl = (f: HTMLIFrameElement, url: unknown) => post(f, { type: 'open_url', url })

  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('denies open_url when the app is not approved', async () => {
    const { container } = renderFrame(
      <AppFrame app={mkApp({ actions_approved: false })} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openUrl(f, 'https://example.com/x') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_url_ack', status: 'denied', reason: 'actions not approved' }), '*',
    )
  })

  it('denies same-origin URLs (cookie-carrying dashboard routes)', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openUrl(f, `${window.location.origin}/v1/agents`) })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_url_ack', status: 'denied' }), '*',
    )
    expect(container.querySelector('[data-testid="app-openurl-chip"]')).toBeNull()
  })

  it('first open shows the origin consent chip; Allow opens the tab', async () => {
    const winOpen = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openUrl(f, 'https://docs.example.com/page?x=1') })
    const chip = container.querySelector('[data-testid="app-openurl-chip"]')
    expect(chip).not.toBeNull()
    expect(chip!.textContent).toContain('https://docs.example.com')
    expect(chip!.textContent).not.toContain('/page')  // origin only, never the full URL
    expect(winOpen).not.toHaveBeenCalled()
    const allow = Array.from(chip!.querySelectorAll('button'))
      .find((b) => b.textContent === 'Allow')!
    await act(async () => { allow.click() })
    expect(winOpen).toHaveBeenCalledWith(
      'https://docs.example.com/page?x=1', '_blank', 'noopener,noreferrer')
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_url_ack', status: 'opened' }), '*',
    )
  })

  it('a stored allow opens without the chip; noopener\'s null return still acks opened', async () => {
    // window.open with `noopener` answers null by design; the activation gate
    // before the open is what a popup blocker would have refused, so a null
    // here is an opened tab, not a blocked one (lib/openExternal).
    localStorage.setItem('otodock-app-openurl:app-1', 'allowed')
    const winOpen = vi.spyOn(window, 'open').mockReturnValue(null)
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openUrl(f, 'https://example.com/a') })
    expect(container.querySelector('[data-testid="app-openurl-chip"]')).toBeNull()
    expect(winOpen).toHaveBeenCalled()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_url_ack', status: 'opened' }), '*',
    )
  })

  // APPS.md "External links": a host the manifest declares was approved on
  // the card, so it opens without the chip; the ack echoes the call id of
  // otodock.openExternal; an undeclared host keeps the chip.
  it('opens a declared host without the chip and echoes the call id', async () => {
    const winOpen = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    const declared = mkApp({ external: { links: ['checkout.stripe.com'], challenge: [], session_days: 30 } })
    const { container } = renderFrame(<AppFrame app={declared} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { post(f, { type: 'open_url', url: 'https://checkout.stripe.com/pay/x', call_id: 'o1' }) })
    expect(container.querySelector('[data-testid="app-openurl-chip"]')).toBeNull()
    expect(winOpen).toHaveBeenCalledWith('https://checkout.stripe.com/pay/x', '_blank', 'noopener,noreferrer')
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_url_ack', status: 'opened', call_id: 'o1' }), '*',
    )
    await act(async () => { post(f, { type: 'open_url', url: 'https://other.example.com/', call_id: 'o2' }) })
    expect(container.querySelector('[data-testid="app-openurl-chip"]')).not.toBeNull()
    expect(winOpen).toHaveBeenCalledTimes(1)
  })

  it('burst-guards rapid opens', async () => {
    localStorage.setItem('otodock-app-openurl:app-1', 'allowed')
    vi.spyOn(window, 'open').mockReturnValue({} as Window)
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => {
      for (let i = 0; i < 4; i++) openUrl(f, `https://example.com/${i}`)
    })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_url_ack', status: 'denied', reason: 'rate limited' }), '*',
    )
  })
})

describe('AppFrame open_target navigation', () => {
  const openTarget = (f: HTMLIFrameElement, target: unknown) => post(f, { type: 'open_target', target })
  const setActivation = (active: boolean | null) => {
    if (active === null) {
      delete (navigator as any).userActivation
      return
    }
    Object.defineProperty(navigator, 'userActivation', { value: { isActive: active }, configurable: true })
  }
  const loc = () => document.querySelector('[data-testid="loc"]')!.textContent

  beforeEach(() => {
    localStorage.clear()
    setActivation(true)
  })
  afterEach(() => setActivation(null))

  it('moves the viewer to the route built from the kind, never from a URL', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openTarget(f, { kind: 'chat', id: 'chat-42' }) })
    expect(loc()).toBe('/chat/a1/chat-42')
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_ack', status: 'opened' }), '*',
    )
    await act(async () => { openTarget(f, { kind: 'app', id: 'app-9' }) })
    expect(loc()).toBe('/apps/app-9')
    // A URL, a path or an unknown kind never navigates.
    for (const target of [
      { url: 'https://evil.example/x' }, { kind: 'chat', id: '../../admin' },
      { kind: 'goto', path: '/admin' }, 'chat',
    ]) {
      spy.mockClear()
      await act(async () => { openTarget(f, target) })
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'open_ack', status: 'denied' }), '*',
      )
    }
    expect(loc()).toBe('/apps/app-9')
  })

  it('needs an approved manifest and a real user gesture (missing API fails closed)', async () => {
    const { container, unmount } = renderFrame(
      <AppFrame app={mkApp({ actions_approved: false })} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openTarget(f, { kind: 'chat', id: 'c1' }) })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_ack', status: 'denied', reason: 'actions not approved' }), '*',
    )
    unmount()

    setActivation(null)
    const r2 = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f2 = getFrame(r2.container)
    const spy2 = ackSpy(f2)
    await act(async () => { openTarget(f2, { kind: 'chat', id: 'c1' }) })
    expect(spy2).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_ack', status: 'denied', reason: 'no user gesture' }), '*',
    )
    expect(loc()).toBe('/chat/a1')
  })

  it('settings kinds ask once; Allow navigates and is remembered, Block is acked', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => {
      openTarget(f, { kind: 'user_settings', tab: 'integrations', provider: 'gmail-mcp' })
    })
    const chip = container.querySelector('[data-testid="app-openurl-chip"]')
    expect(chip).not.toBeNull()
    expect(chip!.textContent).toContain('integrations settings')
    expect(loc()).toBe('/chat/a1')
    const allow = Array.from(chip!.querySelectorAll('button')).find((b) => b.textContent === 'Allow')!
    await act(async () => { allow.click() })
    expect(loc()).toBe('/user-settings?tab=integrations&provider=gmail-mcp')
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_ack', status: 'opened' }), '*',
    )
    expect(localStorage.getItem('otodock-app-opensettings:app-1')).toBe('allowed')
    // Remembered: the next settings target opens without the chip.
    await act(async () => { openTarget(f, { kind: 'agent_settings', tab: 'triggers' }) })
    expect(container.querySelector('[data-testid="app-openurl-chip"]')).toBeNull()
    expect(loc()).toBe('/agents/a1/triggers')
  })

  it('read-only hosts ack unavailable', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" readOnly />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { openTarget(f, { kind: 'chat', id: 'c1' }) })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_ack', status: 'unavailable' }), '*',
    )
    expect(loc()).toBe('/chat/a1')
  })

  it('shares the burst window with links', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => {
      for (let i = 0; i < 4; i++) openTarget(f, { kind: 'chat', id: `c${i}` })
    })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'open_ack', status: 'denied', reason: 'rate limited' }), '*',
    )
  })
})

// ── Live apps (APPS.md "Live apps") ─────────────────────────────────
describe('AppFrame live apps', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    appStateStub.current = undefined
    queryClient = new QueryClient()
    _resetFocusForTests()
  })
  afterEach(() => { vi.useRealTimers() })

  it('forwards a push to the matching frame only, as it arrives', () => {
    const { container } = renderFrame(
      <div>
        <AppFrame app={mkApp({ id: 'app-1' })} agent="a1" />
        <AppFrame app={mkApp({ id: 'app-2', slug: 'other' })} agent="a1" />
      </div>,
    )
    const frames = container.querySelectorAll('iframe')
    const spy1 = ackSpy(frames[0] as HTMLIFrameElement)
    const spy2 = ackSpy(frames[1] as HTMLIFrameElement)
    act(() => { emitAppLive({ type: 'app_push', app_id: 'app-1', payload: { lane: 3 }, ts: 5 }) })
    expect(spy1).toHaveBeenCalledWith(
      { source: 'otodock-host', type: 'push', payload: { lane: 3 }, ts: 5 }, '*',
    )
    expect(spy2).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'push' }), '*')
  })

  it('posts the state document on load and again when a newer one arrives', () => {
    appStateStub.current = { doc: { phase: 'build' }, rev: 1 }
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    act(() => { f.dispatchEvent(new Event('load')) })
    expect(spy).toHaveBeenCalledWith(
      { source: 'otodock-host', type: 'state', doc: { phase: 'build' }, rev: 1 }, '*',
    )
    // A newer frame lands in the cache; an older one never turns it back.
    act(() => { emitAppLive({ type: 'app_state', app_id: 'app-1', doc: { phase: 'ship' }, rev: 3 }) })
    expect(queryClient.getQueryData(['app-state', 'app-1'])).toEqual({ doc: { phase: 'ship' }, rev: 3 })
    act(() => { emitAppLive({ type: 'app_state', app_id: 'app-1', doc: { phase: 'old' }, rev: 2 }) })
    expect(queryClient.getQueryData(['app-state', 'app-1'])).toEqual({ doc: { phase: 'ship' }, rev: 3 })
    // Another app's state is not this frame's business.
    act(() => { emitAppLive({ type: 'app_state', app_id: 'app-9', doc: { x: 1 }, rev: 1 }) })
    expect(queryClient.getQueryData(['app-state', 'app-9'])).toBeUndefined()
  })

  it('ignores state and push messages that originate in the frame', () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    act(() => {
      post(f, { type: 'state', doc: { evil: true }, rev: 99 })
      post(f, { type: 'push', payload: 'x' })
    })
    expect(queryClient.getQueryData(['app-state', 'app-1'])).toBeUndefined()
    expect(spy).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'state' }), '*')
    expect(spy).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'push' }), '*')
  })

  it('registers itself as the viewer focus while mounted, unless read-only', () => {
    const { unmount } = renderFrame(<AppFrame app={mkApp({ title: 'Brief' })} agent="a1" />)
    expect(currentFocus()).toEqual({ surface: 'app', app_id: 'app-1' })
    unmount()
    expect(currentFocus()).toEqual({ surface: 'home' })
    renderFrame(<AppFrame app={mkApp()} agent="a1" readOnly />)
    expect(currentFocus()).toEqual({ surface: 'home' })
  })
})

// ── Folder apps (APPS.md "Client files", "Viewer identity") ──────────────────

describe('AppFrame folder apps', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mintViewerToken.mockClear()
    queryClient = new QueryClient()
    _resetFocusForTests()
    _resetViewerClaimsForTests()
  })
  afterEach(() => { vi.useRealTimers() })

  const folderApp = (over: Partial<PinnedApp> = {}) => mkApp({
    kind: 'folder', release_sha: 'abc123', preview_sha: 'pre456', has_server: true, actions: [], ...over,
  })
  const load = async (f: HTMLIFrameElement) => {
    await act(async () => { f.dispatchEvent(new Event('load')) })
  }

  it('addresses the document by the release hash, the preview copy by its own', () => {
    const { container, rerender } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    expect(getFrame(container).getAttribute('src')).toContain('/v1/apps/app-1/client/abc123/?theme=')
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={folderApp()} agent="a1" preview /></MemoryRouter>
      </QueryClientProvider>,
    )
    const src = getFrame(container).getAttribute('src') || ''
    expect(src).toContain('/client/pre456/')
    expect(src).toContain('preview=1')
    // No release yet: the html route serves the placeholder.
    const { container: c2 } = renderFrame(<AppFrame app={folderApp({ release_sha: '' })} agent="a1" />)
    expect(getFrame(c2).getAttribute('src')).toContain('/v1/apps/app-1/html?theme=')
  })

  it('mints the viewer token on ready, re-mints before expiry and on the page\'s word', async () => {
    const { container } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    post(f, { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenCalledWith('app-1', { preview: false })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'otodock-host', type: 'viewer_token', token: 'vt-1' }), '*',
    )
    mintViewerToken.mockClear()
    await flush(8 * 60 * 1000 + 50)
    expect(mintViewerToken).toHaveBeenCalledTimes(1)
    mintViewerToken.mockClear()
    post(f, { type: 'viewer_token_expired' })
    await flush()
    expect(mintViewerToken).toHaveBeenCalledTimes(1)
  })

  it('answers a page\'s session and challenge asks as unavailable (those are a link\'s)', async () => {
    const { container } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    post(f, { type: 'session_set', token: 'abc' })
    post(f, { type: 'session_clear' })
    post(f, { type: 'challenge', call_id: 'h1' })
    await flush()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'app_session', token: null, status: 'unavailable' }), '*',
    )
    expect(spy.mock.calls.filter((c) => (c[0] as { type: string }).type === 'app_session')).toHaveLength(2)
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'challenge_result', call_id: 'h1', token: null, status: 'unavailable' }), '*',
    )
  })

  it('never mints for a file app or in a read-only host', async () => {
    const { container } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    post(getFrame(container), { type: 'ready' })
    await flush()
    const { container: c2 } = renderFrame(<AppFrame app={folderApp()} agent="a1" readOnly />)
    post(getFrame(c2), { type: 'ready' })
    await flush()
    expect(mintViewerToken).not.toHaveBeenCalled()
  })

  it('blanks a page that navigated itself, ignores it, and offers a reload', async () => {
    const { container, getByText } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const f = getFrame(container)
    // The load the host caused.
    await load(f)
    expect(container.querySelector('[data-testid="app-navigated-away"]')).toBeNull()
    // One nobody asked for.
    await load(f)
    expect(container.querySelector('[data-testid="app-navigated-away"]')).not.toBeNull()
    expect(getFrame(container).getAttribute('src')).toBe('about:blank')
    post(getFrame(container), { type: 'ready' })
    await flush()
    expect(mintViewerToken).not.toHaveBeenCalled()
    await act(async () => { getByText('Reload the app').click() })
    expect(container.querySelector('[data-testid="app-navigated-away"]')).toBeNull()
    expect(getFrame(container).getAttribute('src')).toContain('/client/abc123/?theme=')
    expect(getFrame(container).getAttribute('src')).toContain('v=1')
  })

  it('remounts the frame per host reload, so Back and quick reloads never unguard it', async () => {
    // Each src is a fresh element that owes one load: a src change on the
    // same element joined the session history (Back walked the frame to an
    // older document), and two quick changes that raced into one load left
    // a spare expected load a later self-navigation slipped through.
    const { container, rerender } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const first = getFrame(container)
    const as = (sha: string) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={folderApp({ release_sha: sha })} agent="a1" /></MemoryRouter>
      </QueryClientProvider>
    )
    rerender(as('def456'))
    rerender(as('fed789'))
    const current = getFrame(container)
    expect(current).not.toBe(first)
    await load(current)
    expect(container.querySelector('[data-testid="app-navigated-away"]')).toBeNull()
    await load(current)
    expect(container.querySelector('[data-testid="app-navigated-away"]')).not.toBeNull()
  })

  it('shows the server state the page reports', async () => {
    const { container, rerender } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const f = getFrame(container)
    post(f, { type: 'server_status', state: 'starting', retry_after: 2 })
    await flush()
    expect(container.querySelector('[data-testid="app-server-banner"]')?.textContent).toContain('Starting Brief')
    post(f, { type: 'server_status', state: 'up' })
    await flush()
    expect(container.querySelector('[data-testid="app-server-banner"]')).toBeNull()
    post(f, { type: 'server_status', state: 'backoff' })
    await flush()
    expect(container.querySelector('[data-testid="app-server-banner"]')?.textContent).toContain('see Logs')
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={folderApp({ can_manage: false })} agent="a1" /></MemoryRouter>
      </QueryClientProvider>,
    )
    expect(container.querySelector('[data-testid="app-server-banner"]')?.textContent).toContain('not available right now')
  })

  it('reloads a folder app once on an approval of the release it already serves', async () => {
    const as = (over: Partial<PinnedApp>) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={folderApp(over)} agent="a1" /></MemoryRouter>
      </QueryClientProvider>
    )
    const { container, rerender } = renderFrame(<AppFrame app={folderApp({ actions_approved: false })} agent="a1" />)
    const src = () => getFrame(container).getAttribute('src') || ''
    // Approved on the plain card, same release: the page asks again.
    rerender(as({ actions_approved: true }))
    await flush(0)
    expect(src()).toContain('/client/abc123/')
    expect(src()).toContain('v=1')
    // Approved on the deploy card: the new hash is the one reload.
    rerender(as({ actions_approved: false, release_sha: 'abc123' }))
    rerender(as({ actions_approved: true, release_sha: 'def456' }))
    await flush(0)
    expect(src()).toContain('/client/def456/')
    expect(src()).toContain('v=1')
  })

  it('keeps the claim across the reloads a deploy causes and retries a refused mint', async () => {
    const { container } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const f = getFrame(container)
    post(f, { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenCalledTimes(1)
    // The reload after a deploy says ready again: the claim in hand is reused.
    const spy = ackSpy(f)
    post(f, { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ type: 'viewer_token', token: 'vt-1' }), '*')
    // A refused mint: a connecting banner (never "failed to start"), a retry, then the claim.
    mintViewerToken.mockRejectedValueOnce(new Error('too many requests'))
    post(f, { type: 'viewer_token_expired' })
    await flush()
    expect(mintViewerToken).toHaveBeenCalledTimes(2)
    const banner = () => container.querySelector('[data-testid="app-server-banner"]')?.textContent || ''
    expect(banner()).toContain('Connecting Brief')
    expect(banner()).not.toContain('see Logs')
    spy.mockClear()
    await flush(2100)
    expect(mintViewerToken).toHaveBeenCalledTimes(3)
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ type: 'viewer_token', token: 'vt-1' }), '*')
    expect(container.querySelector('[data-testid="app-server-banner"]')).toBeNull()
  })

  it('a preview frame mints a claim for the preview copy; a render loads its own hash and never warms', async () => {
    const { container } = renderFrame(<AppFrame app={folderApp({ actions: [{ id: 't', label: 'T', type: 'mcp_tool', mcp: 'x', tool: 'y' }] })} agent="a1" preview />)
    post(getFrame(container), { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenCalledWith('app-1', { preview: true })
    warmApp.mockClear()
    const sha = 'c'.repeat(64)
    const { container: c2 } = renderFrame(
      <AppFrame app={folderApp({ actions: [{ id: 't', label: 'T', type: 'mcp_tool', mcp: 'x', tool: 'y' }] })} agent="a1" renderSha={sha} />,
    )
    const src = getFrame(c2).getAttribute('src') || ''
    expect(src).toContain(`/client/${sha}/`)
    expect(src).toContain('render=1')
    expect(warmApp).not.toHaveBeenCalled()
  })

  it('reads the app at ready time: a file app turned folder app mints (the overlay bug)', async () => {
    // The message handler is registered once at mount; a host that reuses
    // the instance across tabs (the overlay before it keyed the frame)
    // showed a folder app to a handler that still thought "file app".
    const { container, rerender } = renderFrame(<AppFrame app={mkApp()} agent="a1" />)
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={folderApp()} agent="a1" /></MemoryRouter>
      </QueryClientProvider>,
    )
    const f = getFrame(container)
    const spy = ackSpy(f)
    post(f, { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenCalledWith('app-1', { preview: false })
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ type: 'viewer_token', token: 'vt-1' }), '*')
  })

  it('never posts one app\'s claim into another app\'s frame; a remount reuses its own', async () => {
    const { container, rerender, unmount } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const f = getFrame(container)
    post(f, { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenCalledWith('app-1', { preview: false })
    // The same instance now shows app-2 (a host without a key): its ready
    // mints for app-2 and the app-1 claim stays where it belongs.
    mintViewerToken.mockImplementationOnce(async () => ({ token: 'vt-2', exp: 9_999_999_999, ttl: 600 }))
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}>
          <AppFrame app={folderApp({ id: 'app-2', slug: 'other' })} agent="a1" />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const spy = ackSpy(getFrame(container))
    post(getFrame(container), { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenLastCalledWith('app-2', { preview: false })
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ type: 'viewer_token', token: 'vt-2' }), '*')
    expect(spy).not.toHaveBeenCalledWith(expect.objectContaining({ token: 'vt-1' }), '*')
    unmount()
    // A fresh mount of app-1 within the claim's window posts it without a mint.
    mintViewerToken.mockClear()
    const { container: c2 } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const spy2 = ackSpy(getFrame(c2))
    post(getFrame(c2), { type: 'ready' })
    await flush()
    expect(mintViewerToken).not.toHaveBeenCalled()
    expect(spy2).toHaveBeenCalledWith(expect.objectContaining({ type: 'viewer_token', token: 'vt-1' }), '*')
  })

  it('keeps the live claim and the preview claim apart: each page reaches its own server', async () => {
    const { container, rerender } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    post(getFrame(container), { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenLastCalledWith('app-1', { preview: false })
    // "View the working copy" on the same frame: the live claim names the
    // live process, so the preview page gets a claim of its own.
    mintViewerToken.mockImplementationOnce(async () => ({ token: 'vt-pre', exp: 9_999_999_999, ttl: 600 }))
    const as = (preview: boolean) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={folderApp()} agent="a1" preview={preview} /></MemoryRouter>
      </QueryClientProvider>
    )
    rerender(as(true))
    const spy = ackSpy(getFrame(container))
    post(getFrame(container), { type: 'ready' })
    await flush()
    expect(mintViewerToken).toHaveBeenLastCalledWith('app-1', { preview: true })
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ type: 'viewer_token', token: 'vt-pre' }), '*')
    expect(spy).not.toHaveBeenCalledWith(expect.objectContaining({ token: 'vt-1' }), '*')
    // Back to the release: its own claim again, never the preview's.
    mintViewerToken.mockClear()
    rerender(as(false))
    const spy2 = ackSpy(getFrame(container))
    post(getFrame(container), { type: 'ready' })
    await flush()
    expect(mintViewerToken).not.toHaveBeenCalled()
    expect(spy2).toHaveBeenCalledWith(expect.objectContaining({ type: 'viewer_token', token: 'vt-1' }), '*')
    expect(spy2).not.toHaveBeenCalledWith(expect.objectContaining({ token: 'vt-pre' }), '*')
  })

  it('reloads a folder app through its release hash, never a nonce bump', async () => {
    const { container, rerender } = renderFrame(<AppFrame app={folderApp()} agent="a1" />)
    const before = getFrame(container).getAttribute('src')
    await act(async () => {
      emitAppLive({ type: 'app_deployed', app_id: 'app-1', release: 2, actions_sig: 'sig' })
    })
    await flush(1000)
    expect(getFrame(container).getAttribute('src')).toBe(before)
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/chat/a1']}><AppFrame app={folderApp({ release_sha: 'def456' })} agent="a1" /></MemoryRouter>
      </QueryClientProvider>,
    )
    const after = getFrame(container).getAttribute('src') || ''
    expect(after).toContain('/client/def456/')
    expect(after).toContain('v=0')
  })
})
