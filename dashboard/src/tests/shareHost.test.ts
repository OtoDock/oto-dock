import { describe, it, expect } from 'vitest'
import { classify, frameSrc, linkQuery, openAllowed, readConfig, resultMessage, sessionUrl, settleBatch, snapshotMediaUrl, snapshotUiUrl, snapshotUrl, webUrl } from '@/sharehost/host'

// The external link's host is a smaller AppFrame: a link is a viewer of one
// page. The pure half is pinned here; the DOM half is exercised on T1.

const cfg = { token: 'tok', share_id: 's1', kind: 'app' as const, title: 'Ops', needs_password: false, actions: false, turnstile_site_key: '', folder: false, release_sha: '', external_links: [] as string[], session_days: 30 }
// The served config never carries the token: the host takes it from the
// page's own address, so nothing request-derived is written into the HTML.
const { token: _t, ...served } = cfg

describe('share host', () => {
  it('reads its config from the page and the token from the address, and refuses a malformed one', () => {
    expect(readConfig(JSON.stringify(served), '/s/tok')).toEqual(cfg)
    expect(readConfig(JSON.stringify(served), '/s/t%2Fok/')?.token).toBe('t/ok')
    expect(readConfig(JSON.stringify({ ...served, kind: 'chat' }), '/s/tok')?.kind).toBe('chat')
    expect(readConfig(JSON.stringify({ title: 'x' }), '/s/tok')).toBeNull()
    expect(readConfig(JSON.stringify(served), '/shared/x')).toBeNull()
    expect(readConfig('not json', '/s/tok')).toBeNull()
    expect(readConfig(null, '/s/tok')).toBeNull()
  })

  it('addresses only the link\'s own routes', () => {
    expect(frameSrc(cfg, 'dark', 2)).toBe('/s/tok/html?theme=dark&v=2')
    expect(snapshotUrl(cfg)).toBe('/s/tok/snapshot')
    expect(snapshotUiUrl(cfg, 'a b')).toBe('/s/tok/ui/a%20b')
    expect(snapshotMediaUrl(cfg, 'img-1')).toBe('/s/tok/media/img-1')
  })

  it('runs declared actions, sizes on content_height, and answers everything else as unavailable', () => {
    expect(classify({ source: 'otodock-artifact', type: 'content_height', height: 120.4 })).toEqual({ kind: 'height', height: 121 })
    expect(classify({ source: 'otodock-artifact', type: 'app_actions', calls: [{ call_id: 'c1', id: 'go' }] }))
      .toEqual({ kind: 'actions', calls: [{ call_id: 'c1', id: 'go', args: null }] })
    expect(classify({ source: 'otodock-artifact', type: 'feed_subscribe', feed: 'sessions' }))
      .toEqual({ kind: 'ack', type: 'feed_update', payload: { feed: 'sessions', error: 'not available on shared links' } })
    expect(classify({ source: 'otodock-artifact', type: 'open_target', target: { kind: 'chat' } }).kind).toBe('ack')
    expect(classify({ source: 'otodock-artifact', type: 'open_url', url: 'https://x', call_id: 'o1' }))
      .toEqual({ kind: 'open', url: 'https://x', call_id: 'o1' })
    expect(classify({ source: 'otodock-artifact', type: 'action', payload: {} }).kind).toBe('ack')
    // otodock.platform on a link ends in its terminal result, never a wait.
    expect(classify({ source: 'otodock-artifact', type: 'platform_call', call_id: 'p1', method: 'viewer.me' }))
      .toEqual({ kind: 'ack', type: 'platform_result', payload: { call_id: 'p1', ok: false, reason: 'not available on shared links' } })
    // Not from an artifact frame: ignored, never answered.
    expect(classify({ source: 'other', type: 'app_actions' })).toEqual({ kind: 'ignore' })
    expect(classify(null)).toEqual({ kind: 'ignore' })
  })

  it('keeps a snapshot URL only when it is absolute http(s)', () => {
    expect(webUrl('https://example.com/a?b=1')).toBe('https://example.com/a?b=1')
    expect(webUrl('http://example.com')).toBe('http://example.com/')
    expect(webUrl('javascript:alert(1)')).toBe('')
    expect(webUrl('JAVASCRIPT:alert(1)')).toBe('')
    expect(webUrl('data:text/html,<b>x</b>')).toBe('')
    expect(webUrl('/v1/auth/me')).toBe('')
    expect(webUrl(undefined)).toBe('')
  })

  it('settles every call of a batch, a torn or missing line as denied', () => {
    const calls = [{ call_id: 'a', id: 'go' }, { call_id: 'b', id: 'stop' }, { call_id: 'c', id: 'x' }]
    const settled = settleBatch(calls, [
      JSON.stringify({ call_id: 'a', status: 'ok', run_id: 'r1' }),
      '{"call_id":"b",',
    ])
    expect(settled.a.status).toBe('ok')
    expect(settled.b).toEqual({ status: 'denied', reason: 'no answer' })
    expect(settled.c).toEqual({ status: 'denied', reason: 'no answer' })
    expect(resultMessage(calls[0], settled.a)).toMatchObject({ type: 'action_result', id: 'go', call_id: 'a', ok: true })
    expect(resultMessage(calls[1], settled.b)).toMatchObject({ ok: false, result: 'no answer' })
  })
})

describe('share host — folder apps (APPS.md)', () => {
  it('addresses a folder app\'s document by its release hash and asks for the claim on ready', () => {
    const folder = { ...cfg, folder: true, release_sha: 'abc123' }
    expect(frameSrc(folder, 'light', 1)).toBe('/s/tok/client/abc123/?theme=light&v=1')
    expect(frameSrc({ ...folder, release_sha: '' }, 'light')).toBe('/s/tok/html?theme=light&v=0')
    expect(readConfig(JSON.stringify({ ...served, folder: true, release_sha: 'abc123' }), '/s/tok')).toEqual(folder)
    expect(classify({ source: 'otodock-artifact', type: 'ready' })).toEqual({ kind: 'token' })
    expect(classify({ source: 'otodock-artifact', type: 'viewer_token_expired' })).toEqual({ kind: 'token' })
    expect(classify({ source: 'otodock-artifact', type: 'server_status', state: 'starting' })).toEqual({ kind: 'ignore' })
  })

  // Accounts kept by the host (APPS.md "External links"): the page's
  // session asks and its challenge ask are the host's to answer; the
  // config carries the hosts the page may open and the session's lifetime,
  // with defaults for an older proxy.
  it('classifies the session and challenge asks, and reads the link\'s external config with defaults', () => {
    expect(classify({ source: 'otodock-artifact', type: 'session_set', token: 'abc' })).toEqual({ kind: 'session', token: 'abc' })
    expect(classify({ source: 'otodock-artifact', type: 'session_set', token: 'x'.repeat(600) })).toEqual({ kind: 'session', token: 'x'.repeat(512) })
    expect(classify({ source: 'otodock-artifact', type: 'session_set' })).toEqual({ kind: 'session', token: '' })
    expect(classify({ source: 'otodock-artifact', type: 'session_clear' })).toEqual({ kind: 'session', token: '' })
    expect(classify({ source: 'otodock-artifact', type: 'challenge', call_id: 'h1' })).toEqual({ kind: 'challenge', call_id: 'h1' })
    expect(sessionUrl(cfg)).toBe('/s/tok/session')
    const rich = readConfig(JSON.stringify({ ...served, external_links: ['checkout.stripe.com', 7], session_days: 7 }), '/s/tok')
    expect(rich?.external_links).toEqual(['checkout.stripe.com'])
    expect(rich?.session_days).toBe(7)
    expect(readConfig(JSON.stringify({ ...served, session_days: 0 }), '/s/tok')?.session_days).toBe(30)
  })

  // The host-mediated open (APPS.md "External links"): a link opens only
  // an absolute http(s) URL on a host its manifest declares, never our own
  // origin, never a port; the page's query string rides to the page capped.
  it('opens a declared host only, and hands the page the link\'s query', () => {
    const declared = { ...cfg, external_links: ['checkout.stripe.com'] }
    expect(openAllowed(declared, 'https://checkout.stripe.com/pay/x?y=1', 'https://dock.example'))
      .toEqual({ url: 'https://checkout.stripe.com/pay/x?y=1' })
    expect(openAllowed(declared, 'https://CHECKOUT.stripe.com/', 'https://dock.example')).toEqual({ url: 'https://checkout.stripe.com/' })
    expect(openAllowed(declared, 'https://other.example/', 'https://dock.example')).toEqual({ error: 'host not declared by the app' })
    expect(openAllowed(cfg, 'https://checkout.stripe.com/', 'https://dock.example')).toEqual({ error: 'this app opens no sites from a link' })
    expect(openAllowed(declared, 'https://dock.example/v1/agents', 'https://dock.example')).toEqual({ error: 'platform URLs never open from a link' })
    expect(openAllowed(declared, 'https://checkout.stripe.com:8443/', 'https://dock.example')).toEqual({ error: 'a declared host has no port' })
    expect(openAllowed(declared, 'javascript:alert(1)', 'https://dock.example')).toEqual({ error: 'not an http(s) URL' })
    expect(openAllowed(declared, '/relative', 'https://dock.example')).toEqual({ error: 'not an absolute URL' })
    expect(linkQuery('?paid=1&b=2')).toBe('paid=1&b=2')
    expect(linkQuery('')).toBe('')
    expect(linkQuery('?' + 'x'.repeat(600))).toHaveLength(512)
  })
})
