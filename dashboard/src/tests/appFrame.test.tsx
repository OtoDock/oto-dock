import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render } from '@testing-library/react'

import AppFrame from '@/components/apps/AppFrame'
import { emitFileUpdate } from '@/lib/fileUpdates'
import type { PinnedApp } from '@/api/apps'

const fireAppAction = vi.hoisted(() => vi.fn(async () => ({ status: 'sent' })))
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  fireAppAction,
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
    can_approve: true, can_manage: true,
    ...over,
  }
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

const appAction = (f: HTMLIFrameElement, id: string, args?: unknown, source?: Window | null) =>
  window.dispatchEvent(new MessageEvent('message', {
    data: { source: 'otodock-artifact', v: 1, type: 'app_action', id, args },
    source: source === undefined ? f.contentWindow : source,
  }))

describe('AppFrame', () => {
  beforeEach(() => fireAppAction.mockClear())

  it('renders the cookie-authed serve URL in a scripts-only sandbox', () => {
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    expect(f.getAttribute('sandbox')).toBe('allow-scripts')
    expect(f.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(f.getAttribute('allow')).toBe('')
    expect(f.getAttribute('src')).toContain('/v1/apps/app-1/html?theme=')
  })

  it('routes fire_task actions to the REST executor and acks the result', async () => {
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'go') })
    expect(fireAppAction).toHaveBeenCalledWith('app-1', 'go', undefined)
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'otodock-host', type: 'action_ack', status: 'sent' }), '*',
    )
    // fire_task never posts a tool result into the frame.
    expect(spy).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_result' }), '*',
    )
  })

  it('routes mcp_tool actions with args and bridges the result into the frame', async () => {
    fireAppAction.mockResolvedValueOnce({ status: 'done', result: '42' } as any)
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'tool', { room: 'office' }) })
    expect(fireAppAction).toHaveBeenCalledWith('app-1', 'tool', { room: 'office' })
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
    fireAppAction.mockResolvedValueOnce({ status: 'error', reason: 'MCP unavailable' } as any)
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'tool') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'action_result', id: 'tool', ok: false, result: 'MCP unavailable',
      }), '*',
    )
  })

  it('routes send_prompt actions to the host router with args', async () => {
    const onSendPrompt = vi.fn(async () => ({ status: 'queued' }))
    const { container } = render(<AppFrame app={mkApp()} agent="a1" onSendPrompt={onSendPrompt} />)
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
  })

  it('denies unapproved manifests, unknown ids, and free-form sends', async () => {
    const onSendPrompt = vi.fn(async () => ({ status: 'sent' }))
    const { container } = render(
      <AppFrame app={mkApp({ actions_approved: false })} agent="a1" onSendPrompt={onSendPrompt} />,
    )
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'go') })
    expect(fireAppAction).not.toHaveBeenCalled()
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'denied', reason: 'actions not approved' }), '*',
    )
    await act(async () => { appAction(f, 'nope') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'denied', reason: 'unknown action' }), '*',
    )
    // otodock.send (free-form backchannel) has no chat binding in app context.
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { source: 'otodock-artifact', v: 1, type: 'action', payload: {} },
        source: f.contentWindow,
      }))
    })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'unavailable' }), '*',
    )
    expect(onSendPrompt).not.toHaveBeenCalled()
  })

  it('rate-limits rapid action sends client-side', async () => {
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'go') })
    await act(async () => { appAction(f, 'go') })
    expect(fireAppAction).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'denied', reason: 'rate limited' }), '*',
    )
  })

  it('mcp_tool refusals still deliver a terminal action_result (no forever-spinner)', async () => {
    // Server-side denial (e.g. 429 min-interval) — fireAppAction maps HTTP
    // errors to {status:'denied'}: the page must still get its result event.
    fireAppAction.mockResolvedValueOnce({ status: 'denied', reason: 'Too fast — try again in a moment' } as any)
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'tool') })
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
    expect(fireAppAction).toHaveBeenCalledTimes(1)
  })

  it('mcp_tool network failures resolve with an error ack + terminal result', async () => {
    fireAppAction.mockRejectedValueOnce(new Error('boom'))
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => { appAction(f, 'tool') })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'error' }), '*',
    )
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'action_result', id: 'tool', ok: false,
        result: expect.stringContaining('Network error'),
      }), '*',
    )
  })

  it('rates non-prompt actions PER ACTION — a data panel fires its queries together', async () => {
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    const f = getFrame(container)
    ackSpy(f)
    // Two DIFFERENT declared actions back-to-back (the on-load refresh
    // pattern) — both reach the REST executor; the server enforces its own
    // per-action interval + in-flight.
    await act(async () => { appAction(f, 'go') })
    await act(async () => { appAction(f, 'tool') })
    expect(fireAppAction).toHaveBeenCalledTimes(2)
  })

  it('ignores messages from foreign windows', async () => {
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
    getFrame(container)
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { source: 'otodock-artifact', v: 1, type: 'app_action', id: 'go' },
        source: window,
      }))
    })
    expect(fireAppAction).not.toHaveBeenCalled()
  })

  it('answers declared feed subscriptions with a snapshot; refuses the rest', async () => {
    activeChatsRows.current = [{ id: 'c1', agent: 'beta', title: 'Turn', phase: 'streaming' }]
    const app = mkApp({
      actions: [
        { id: 'live', label: 'Live', type: 'data_feed', feed: 'active_chats' },
        { id: 'lanes', label: 'Lanes', type: 'data_feed', feed: 'project_lanes' },
      ],
    })
    const { container } = render(<AppFrame app={app} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    const subscribe = (feed: string) => window.dispatchEvent(new MessageEvent('message', {
      data: { source: 'otodock-artifact', v: 1, type: 'feed_subscribe', feed },
      source: f.contentWindow,
    }))

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
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'action_ack', status: 'denied',
        reason: expect.stringContaining('otodock.feed') }), '*',
    )
    expect(fireAppAction).not.toHaveBeenCalled()
  })

  it('refuses feed subscriptions until the manifest is approved', async () => {
    const app = mkApp({
      actions: [{ id: 'live', label: 'Live', type: 'data_feed', feed: 'active_chats' }],
      actions_approved: false,
    })
    const { container } = render(<AppFrame app={app} agent="a1" />)
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { source: 'otodock-artifact', v: 1, type: 'feed_subscribe', feed: 'active_chats' },
        source: f.contentWindow,
      }))
    })
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
      <AppFrame app={app} agent="a1" projectLanes={lanes1} />,
    )
    const f = getFrame(container)
    const spy = ackSpy(f)
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { source: 'otodock-artifact', v: 1, type: 'feed_subscribe', feed: 'project_lanes' },
        source: f.contentWindow,
      }))
    })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'feed_update', rows: lanes1 }), '*',
    )
    // The iframe's load event fires AFTER the page already subscribed (the
    // subscription happens at script parse time) — it must NOT wipe the
    // subscription (regression: found live on T1, pushes stopped after the
    // initial snapshot).
    await act(async () => { f.dispatchEvent(new Event('load')) })
    await act(async () => {
      rerender(<AppFrame app={app} agent="a1" projectLanes={lanes2} />)
    })
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'feed_update', rows: lanes2 }), '*',
    )
  })

  it('reloads on a matching file_updated broadcast only', async () => {
    const { container } = render(<AppFrame app={mkApp()} agent="a1" />)
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
})
