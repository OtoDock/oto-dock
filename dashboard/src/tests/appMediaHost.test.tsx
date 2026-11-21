import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { AppActionCall, AppActionResult, PinnedApp } from '@/api/apps'
import type { User } from '@/api/auth'

// The host side of an app's media: a media link stays in the app, a workspace file opens over the
// app, a refused platform call says how long to wait, and a prompt button
// that starts a new chat is a person's real click.
const method = vi.hoisted(() => ({
  calls: [] as unknown[][],
  result: { ok: true, result: {} } as { ok: boolean; result?: unknown; reason?: string; retry_after_ms?: number },
}))
const fireAppActions = vi.hoisted(() => vi.fn(
  async (_appId: string, calls: AppActionCall[], onResult: (id: string, r: AppActionResult) => void) => {
    for (const c of calls) onResult(c.call_id, { status: 'sent' })
  },
))
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  useAppState: () => ({ data: undefined }),
  warmApp: vi.fn(),
  fireAppActions,
  callPlatformMethod: vi.fn(async (...args: unknown[]) => {
    method.calls.push(args)
    return method.result
  }),
}))
// The platform's own preview: a stub that shows what it was given.
vi.mock('@/components/workspace/FilePreviewBody', () => ({
  default: (p: { agent: string; node: { path: string; name: string }; canWrite: boolean; onClose: () => void }) => (
    <div data-testid="file-preview" data-agent={p.agent} data-path={p.node.path} data-name={p.node.name}
      data-write={String(p.canWrite)}>
      <button onClick={p.onClose}>Close preview</button>
    </div>
  ),
}))

import AppFrame from '@/components/apps/AppFrame'
import { MEDIA_STAYS, carriesMediaSecret, fileCanWrite, rememberMediaSecrets } from '@/lib/appMedia'

// Built, not written out: a long random-looking literal reads as a key to the secret scan.
const SECRET = 'link-part-'.repeat(4)
const LINK = `/v1/apps/app-1/client/media?t=${SECRET}`

function mkApp(over: Partial<PinnedApp> = {}): PinnedApp {
  return {
    id: 'app-1', slug: 'studio', title: 'Studio', scope: 'shared', position: 0,
    rel_path: 'workspace/apps/studio', updated_at: '',
    actions: [
      { id: 'media', label: 'Show', type: 'platform', method: 'files.media' },
      { id: 'write', label: 'Save', type: 'platform', method: 'files.write' },
      { id: 'tool', label: 'Tool', type: 'mcp_tool', mcp: 'video-tools', tool: 'probe_media' },
      { id: 'ask', label: 'Ask', type: 'send_prompt', prompt: 'Look at {{what}}' },
      { id: 'edit', label: 'Edit a new video', type: 'send_prompt', prompt: 'Start a video edit', new_chat: true },
    ],
    actions_sig: 'sig', actions_approved: true, approval_stale: false,
    can_approve: true, can_manage: true, hidden_for_me: false, viewer_role: 'manager', ...over,
  }
}

function Loc() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

function renderFrame(props: Partial<Parameters<typeof AppFrame>[0]> = {}) {
  const utils = render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={['/chat/a1/c-1']}>
        <AppFrame app={mkApp()} agent="a1" {...props} />
        <Loc />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  const frame = () => utils.container.querySelector('iframe') as HTMLIFrameElement
  const posted: Record<string, unknown>[] = []
  ;(frame().contentWindow as any).postMessage = (m: Record<string, unknown>) => { posted.push(m) }
  const post = (data: Record<string, unknown>) =>
    window.dispatchEvent(new MessageEvent('message', { data: { source: 'otodock-artifact', v: 1, ...data }, source: frame().contentWindow }))
  return { ...utils, frame, posted, post }
}

const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }
const setActivation = (active: boolean | null) => {
  if (active === null) { delete (navigator as any).userActivation; return }
  Object.defineProperty(navigator, 'userActivation', { value: { isActive: active }, configurable: true })
}
const results = (posted: Record<string, unknown>[], type: string) => posted.filter((m) => m.type === type)

/** Hand the frame a link, as a `files.media` answer does. */
async function handLink(t: ReturnType<typeof renderFrame>) {
  method.result = { ok: true, result: { path: 'workspace/media/a.mp4', url: LINK, expires_in: 120, kind: 'video' } }
  await act(async () => { t.post({ type: 'platform_call', call_id: 'p1', method: 'files.media', args: { path: 'workspace/media/a.mp4' } }) })
  await flush()
  expect(results(t.posted, 'platform_result')[0]).toMatchObject({ call_id: 'p1', ok: true, result: { url: LINK } })
  t.posted.length = 0
  method.calls.length = 0
}

describe('AppFrame: media links and files over the app', () => {
  beforeEach(() => {
    method.calls.length = 0
    method.result = { ok: true, result: {} }
    fireAppActions.mockClear()
    localStorage.clear()
    setActivation(true)
  })
  afterEach(() => setActivation(null))

  it('opens a workspace file over the app and closes back to it, with no navigation', async () => {
    const t = renderFrame()
    await act(async () => { t.post({ type: 'open_target', target: { kind: 'file', path: 'workspace/media/brief.pdf' } }) })
    const preview = t.getByTestId('file-preview')
    expect(preview.dataset).toMatchObject({ agent: 'a1', path: 'workspace/media/brief.pdf', name: 'brief.pdf' })
    expect(t.getByTestId('loc').textContent).toBe('/chat/a1/c-1')       // the person stayed where they were
    expect(results(t.posted, 'open_ack')[0]).toMatchObject({ status: 'opened' })
    const before = t.frame()
    await act(async () => { t.getByText('Close preview').click() })
    expect(t.queryByTestId('file-preview')).toBeNull()
    expect(t.frame()).toBe(before)                                      // the app was never reloaded
    // The same gates as every move: a real click, a checked path, a host that can open.
    setActivation(false)
    await act(async () => { t.post({ type: 'open_target', target: { kind: 'file', path: 'workspace/x.pdf' } }) })
    setActivation(true)
    await act(async () => { t.post({ type: 'open_target', target: { kind: 'file', path: '../x.pdf' } }) })
    expect(t.queryByTestId('file-preview')).toBeNull()
    const readOnly = renderFrame({ readOnly: true })
    await act(async () => { readOnly.post({ type: 'open_target', target: { kind: 'file', path: 'workspace/x.pdf' } }) })
    expect(readOnly.queryByTestId('file-preview')).toBeNull()
  })

  it('closes the preview on Back, the browser\'s and the phone apps\', and keeps the app', async () => {
    const w = window as unknown as { __otodockLightboxActive?: boolean; __otodockLightboxClose?: () => void }
    const t = renderFrame()
    t.frame().focus()
    await act(async () => { t.post({ type: 'open_target', target: { kind: 'file', path: 'workspace/media/brief.pdf' } }) })
    expect(t.getByTestId('file-preview')).toBeTruthy()
    // Focus left the frame, so an Escape reaches the preview.
    expect(document.activeElement).not.toBe(t.frame())
    expect(w.__otodockLightboxActive).toBe(true)
    expect((window.history.state as Record<string, unknown>)?.otoLightbox).toBe(true)
    // The Android app's back button and the iOS swipe call the close.
    await act(async () => { w.__otodockLightboxClose?.() })
    expect(t.queryByTestId('file-preview')).toBeNull()
    expect(w.__otodockLightboxActive).toBe(false)
    expect(t.getByTestId('loc').textContent).toBe('/chat/a1/c-1')
    // The browser's Back pops the entry the preview pushed.
    await act(async () => { t.post({ type: 'open_target', target: { kind: 'file', path: 'workspace/media/brief.pdf' } }) })
    expect(t.getByTestId('file-preview')).toBeTruthy()
    await act(async () => { window.dispatchEvent(new PopStateEvent('popstate')) })
    expect(t.queryByTestId('file-preview')).toBeNull()
    expect(t.getByTestId('loc').textContent).toBe('/chat/a1/c-1')
    expect(w.__otodockLightboxClose).toBeUndefined()
  })

  it('passes on no page text that carries a link it handed the frame', async () => {
    const onSendPrompt = vi.fn(async () => ({ status: 'sent' }))
    const t = renderFrame({ onSendPrompt })
    await handLink(t)
    const elsewhere = `https://other.example/get?u=${encodeURIComponent('x')}&v=${SECRET}`
    await act(async () => { t.post({ type: 'open_url', url: elsewhere, call_id: 'o1' }) })
    await act(async () => { t.post({ type: 'open_url', url: `${window.location.origin}${LINK}`, call_id: 'o2' }) })
    expect(results(t.posted, 'open_url_ack')).toEqual([
      expect.objectContaining({ call_id: 'o1', status: 'denied', reason: MEDIA_STAYS }),
      expect.objectContaining({ call_id: 'o2', status: 'denied' }),
    ])
    await act(async () => {
      t.post({ type: 'platform_call', call_id: 'p2', method: 'files.write', args: { path: 'workspace/n.txt', content: `see ${LINK}` } })
    })
    await flush()
    expect(results(t.posted, 'platform_result')[0]).toMatchObject({ call_id: 'p2', ok: false, reason: MEDIA_STAYS })
    expect(method.calls).toEqual([])
    await act(async () => {
      t.post({ type: 'app_actions', calls: [
        { call_id: 'c1', id: 'tool', args: { path: LINK } },
        { call_id: 'c2', id: 'ask', args: { what: SECRET } },
        { call_id: 'c3', id: 'tool', args: { path: 'workspace/media/a.mp4' } },
      ] })
    })
    await act(async () => { await new Promise((r) => setTimeout(r, 10)) })   // the batch goes out a tick later
    const acks = results(t.posted, 'action_ack')
    expect(acks.find((a) => a.call_id === 'c1')).toMatchObject({ status: 'denied', reason: MEDIA_STAYS })
    expect(acks.find((a) => a.call_id === 'c2')).toMatchObject({ status: 'denied', reason: MEDIA_STAYS })
    expect(results(t.posted, 'action_result').find((a) => a.call_id === 'c1')).toMatchObject({ ok: false })
    expect(onSendPrompt).not.toHaveBeenCalled()
    expect(fireAppActions).toHaveBeenCalledTimes(1)                     // the honest call still goes
    expect(fireAppActions.mock.calls[0][1].map((c) => c.call_id)).toEqual(['c3'])
  })

  it('opens no target that carries a link, and refuses a value it cannot read', async () => {
    const onSendPrompt = vi.fn(async () => ({ status: 'sent' }))
    const t = renderFrame({ onSendPrompt })
    await handLink(t)
    // A target's path reaches the dashboard's own address and a files request: checked like the rest.
    await act(async () => { t.post({ type: 'open_target', target: { kind: 'file', path: `workspace/media/${SECRET}.pdf` } }) })
    expect(results(t.posted, 'open_ack')).toEqual([expect.objectContaining({ status: 'denied', reason: MEDIA_STAYS })])
    expect(t.queryByTestId('file-preview')).toBeNull()
    await act(async () => { t.post({ type: 'open_target', target: { kind: 'file', path: 'workspace/media/brief.pdf' } }) })
    expect(t.getByTestId('file-preview').dataset.path).toBe('workspace/media/brief.pdf')   // an honest one still opens
    // A value JSON cannot write may hold a link the host cannot see: it is refused, never passed on.
    const loop: Record<string, unknown> = { what: SECRET }
    loop.self = loop
    await act(async () => { t.post({ type: 'app_actions', calls: [{ call_id: 'c1', id: 'ask', args: loop }] }) })
    await act(async () => { await new Promise((r) => setTimeout(r, 10)) })
    expect(results(t.posted, 'action_ack').find((a) => a.call_id === 'c1')).toMatchObject({ status: 'denied', reason: MEDIA_STAYS })
    expect(onSendPrompt).not.toHaveBeenCalled()
  })

  it('answers files.media itself in the rendered check, with no request', async () => {
    const t = renderFrame({ renderSha: 'abc123' })
    await act(async () => { t.post({ type: 'platform_call', call_id: 'p1', method: 'files.media', args: { path: 'workspace/a.mp4' } }) })
    await flush()
    expect(results(t.posted, 'platform_result')[0]).toMatchObject({ ok: false, reason: 'not available in the rendered check' })
    expect(method.calls).toEqual([])
  })

  it('tells the page how long to wait when the second try is refused too', async () => {
    const t = renderFrame()
    method.result = { ok: false, reason: 'Too fast — try again in a moment', retry_after_ms: 750 }
    await act(async () => { t.post({ type: 'platform_call', call_id: 'p1', method: 'files.media', args: { path: 'workspace/a.mp4' } }) })
    await flush()
    expect(results(t.posted, 'platform_result')[0]).toMatchObject({ ok: false, retry_after_ms: 750 })
  })

  it('posts no result into a frame that navigated since the call', async () => {
    const t = renderFrame()
    let answer: (v: typeof method.result) => void = () => {}
    const { callPlatformMethod } = await import('@/api/apps')
    vi.mocked(callPlatformMethod).mockImplementationOnce(() => new Promise((r) => { answer = r }))
    await act(async () => { fireEvent.load(t.frame()) })                 // the load the host caused
    await act(async () => { t.post({ type: 'platform_call', call_id: 'p1', method: 'files.media', args: { path: 'workspace/a.mp4' } }) })
    await act(async () => { fireEvent.load(t.frame()) })                 // one nobody asked for
    expect(t.container.querySelector('[data-testid="app-navigated-away"]')).not.toBeNull()
    await act(async () => { answer({ ok: true, result: { url: LINK } }) })
    await flush()
    expect(results(t.posted, 'platform_result')).toEqual([])
  })

  it('takes a real click and the burst window for a button that starts a new chat', async () => {
    const onSendPrompt = vi.fn(async (_app: PinnedApp, _action: unknown, _args: unknown) => ({ status: 'sent' }))
    const t = renderFrame({ onSendPrompt })
    const press = async (id: string, callId: string) => {
      await act(async () => { t.post({ type: 'app_actions', calls: [{ call_id: callId, id, args: null }] }) })
      await flush()
      return results(t.posted, 'action_ack').find((a) => a.call_id === callId)
    }
    setActivation(false)
    expect(await press('edit', 'n1')).toMatchObject({ status: 'denied', reason: 'no user gesture' })
    setActivation(null)                                                 // a browser that cannot vouch refuses
    expect(await press('edit', 'n2')).toMatchObject({ status: 'denied', reason: 'no user gesture' })
    expect(onSendPrompt).not.toHaveBeenCalled()
    setActivation(true)
    vi.useFakeTimers()
    try {
      const sent: unknown[] = []
      for (let i = 0; i < 4; i++) {
        sent.push((await press('edit', `b${i}`))?.status)
        vi.advanceTimersByTime(1100)
      }
      expect(sent).toEqual(['sent', 'sent', 'sent', 'denied'])          // three in ten seconds, as every move
      expect(onSendPrompt).toHaveBeenCalledTimes(3)
      expect(onSendPrompt.mock.calls[0][1]).toEqual({ id: 'edit', label: 'Edit a new video', prompt: 'Start a video edit', new_chat: true })
      // A plain prompt button asks for neither and carries no flag.
      setActivation(false)
      vi.advanceTimersByTime(1100)
      expect(await press('ask', 'a1')).toMatchObject({ status: 'sent' })
      expect(onSendPrompt.mock.calls[3][1]).toEqual({ id: 'ask', label: 'Ask', prompt: 'Look at {{what}}' })
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('lib/appMedia', () => {
  it('remembers a link or a batch of them, and forgets the oldest past its bound', () => {
    const held = new Set<string>()
    rememberMediaSecrets(held, { url: LINK })
    rememberMediaSecrets(held, { links: [{ url: `/v1/apps/x/client/media?t=${'b'.repeat(43)}` }, { path: 'p', error: 'not a file' }] })
    rememberMediaSecrets(held, null)
    rememberMediaSecrets(held, { url: '/v1/apps/x/client/media?t=short' })
    expect([...held]).toEqual([SECRET, 'b'.repeat(43)])
    for (let i = 0; i < 600; i++) rememberMediaSecrets(held, { url: `/m?t=${String(i).padStart(30, 'x')}` })
    expect(held.size).toBe(512)
    expect(held.has(SECRET)).toBe(false)
  })

  it('finds a secret in a string or in anything JSON writes, and trusts no value it cannot read', () => {
    const held = new Set([SECRET])
    expect(carriesMediaSecret(held, `https://x.example/?a=${SECRET}`)).toBe(true)
    expect(carriesMediaSecret(held, { deep: [{ v: LINK }] })).toBe(true)
    expect(carriesMediaSecret(held, { path: 'workspace/media/a.mp4' })).toBe(false)
    expect(carriesMediaSecret(held, null)).toBe(false)
    expect(carriesMediaSecret(new Set(), LINK)).toBe(false)
    // A value JSON cannot write is not read, so it counts as carrying one. With no link handed out, nothing can.
    const loop: Record<string, unknown> = {}
    loop.self = loop
    expect(carriesMediaSecret(held, loop)).toBe(true)
    expect(carriesMediaSecret(held, { n: 10n })).toBe(true)
    expect(carriesMediaSecret(new Set(), loop)).toBe(false)
  })

  it('gives the preview the viewer\'s own write right for the path', () => {
    const person = (roles: Record<string, string>, role = 'member') =>
      ({ sub: 's', email: 'e', name: 'n', role, username: 'alice', agent_roles: roles }) as unknown as User
    expect(fileCanWrite(person({ a1: 'contributor' }), 'a1', 'workspace/media/a.pdf')).toBe(true)
    expect(fileCanWrite(person({ a1: 'viewer' }), 'a1', 'workspace/media/a.pdf')).toBe(false)
    expect(fileCanWrite(person({ a1: 'viewer' }), 'a1', 'users/alice/workspace/a.pdf')).toBe(true)
    expect(fileCanWrite(person({ a1: 'manager' }), 'a1', 'users/bob/workspace/a.pdf')).toBe(false)
    expect(fileCanWrite(person({ a1: 'editor' }), 'a1', 'knowledge/a.pdf')).toBe(false)
    expect(fileCanWrite(person({ a1: 'manager' }), 'a1', 'knowledge/a.pdf')).toBe(true)
    expect(fileCanWrite(null, 'a1', 'workspace/a.pdf')).toBe(false)
    expect(fileCanWrite(person({ a1: 'manager' }), 'a1', 'config/agent.md')).toBe(false)
  })
})
