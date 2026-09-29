// The external link's host (SHARING.md "External links"): the pure half.
// `main.ts` wires it to the page served at /s/<token>; this file holds what
// can be tested without a DOM — the routes, the message allowlist, the
// batch protocol and the open rule. The host is deliberately smaller than
// the dashboard's AppFrame: no warm, no feeds, no navigation, no live
// frames; a link opens only the hosts its manifest declares (APPS.md
// "External links"). A link is a viewer of one page.

export interface ShareConfig {
  token: string
  share_id: string
  /** What the link opens: an app in its sandbox, or a chat snapshot. */
  kind: 'app' | 'chat'
  title: string
  needs_password: boolean
  actions: boolean
  turnstile_site_key: string
  /** A folder app (APPS.md): its document is addressed by the release hash
      and its page gets a viewer claim for the app's own API. */
  folder: boolean
  release_sha: string
  /** APPS.md "External links": the public hosts the host page may open in
      a new tab (empty until the manifest is approved), and how long the
      app's session cookie lives. Absent from an older proxy's config. */
  external_links: string[]
  session_days: number
}

export interface SnapshotMessage {
  role: 'user' | 'assistant' | 'event'
  content?: string
  created_at?: string
  event_type?: string
  data?: Record<string, unknown>
  event_data?: string
}

export interface HostMessage {
  source?: string
  v?: number
  type?: string
  [k: string]: unknown
}

export interface ActionCall {
  call_id: string
  id: string
  args?: unknown
}

export const frameSrc = (cfg: ShareConfig, theme: 'light' | 'dark', nonce = 0): string =>
  cfg.folder && cfg.release_sha
    ? `/s/${encodeURIComponent(cfg.token)}/client/${encodeURIComponent(cfg.release_sha)}/?theme=${theme}&v=${nonce}`
    : `/s/${encodeURIComponent(cfg.token)}/html?theme=${theme}&v=${nonce}`

export const viewerTokenUrl = (cfg: ShareConfig): string => `/s/${encodeURIComponent(cfg.token)}/viewer-token`
export const sessionUrl = (cfg: ShareConfig): string => `/s/${encodeURIComponent(cfg.token)}/session`
export const stateUrl = (cfg: ShareConfig): string => `/s/${encodeURIComponent(cfg.token)}/state`
export const unlockUrl = (cfg: ShareConfig): string => `/s/${encodeURIComponent(cfg.token)}/unlock`
export const batchUrl = (cfg: ShareConfig): string => `/s/${encodeURIComponent(cfg.token)}/actions/batch`
export const snapshotUrl = (cfg: ShareConfig): string => `/s/${encodeURIComponent(cfg.token)}/snapshot`
export const snapshotUiUrl = (cfg: ShareConfig, token: string): string =>
  `/s/${encodeURIComponent(cfg.token)}/ui/${encodeURIComponent(token)}`
export const snapshotMediaUrl = (cfg: ShareConfig, token: string): string =>
  `/s/${encodeURIComponent(cfg.token)}/media/${encodeURIComponent(token)}`

/** The link's token is the page's own address (`/s/<token>`), never part of
 * the served HTML. */
export function tokenFromPath(pathname: string): string {
  const m = /^\/s\/([^/?#]+)/.exec(pathname || '')
  if (!m) return ''
  try {
    return decodeURIComponent(m[1])
  } catch {
    return ''
  }
}

export function readConfig(text: string | null | undefined, pathname: string): ShareConfig | null {
  if (!text) return null
  const token = tokenFromPath(pathname)
  if (!token) return null
  try {
    const raw = JSON.parse(text) as Omit<Partial<ShareConfig>, 'token'>
    if (typeof raw.share_id !== 'string') return null
    return {
      token,
      share_id: raw.share_id,
      kind: raw.kind === 'chat' ? 'chat' : 'app',
      title: typeof raw.title === 'string' ? raw.title : 'Shared app',
      needs_password: !!raw.needs_password,
      actions: !!raw.actions,
      turnstile_site_key: typeof raw.turnstile_site_key === 'string' ? raw.turnstile_site_key : '',
      folder: !!raw.folder,
      release_sha: typeof raw.release_sha === 'string' ? raw.release_sha : '',
      external_links: Array.isArray(raw.external_links)
        ? raw.external_links.filter((h): h is string => typeof h === 'string') : [],
      session_days: typeof raw.session_days === 'number' && raw.session_days > 0 ? raw.session_days : 30,
    }
  } catch {
    return null
  }
}

export type Reply =
  | { kind: 'height'; height: number }
  | { kind: 'actions'; calls: ActionCall[] }
  | { kind: 'ack'; type: string; payload: Record<string, unknown> }
  /** A folder app's page is ready for (or lost) its viewer claim. */
  | { kind: 'token' }
  /** The page asks the host to keep (a token) or drop ('') the app's own
      session in the link's cookie (APPS.md "External links"). */
  | { kind: 'session'; token: string }
  /** The page asks for a bot check; the host answers `challenge_result`. */
  | { kind: 'challenge'; call_id: string }
  /** The page asks to open a URL in a new tab (`otodock.openExternal` or a
      bridged <a href>); the host decides by `openAllowed`. */
  | { kind: 'open'; url: string; call_id: string }
  | { kind: 'ignore' }

/** The URL the host may open for the page, or the reason it may not: an
 * absolute http(s) URL whose host the manifest declares (`external_links`,
 * empty until the manifest is approved), on the default port, never our
 * own origin. */
export function openAllowed(cfg: ShareConfig, raw: string, ownOrigin = ''): { url: string } | { error: string } {
  let u: URL
  try {
    u = new URL(String(raw || ''))
  } catch {
    return { error: 'not an absolute URL' }
  }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return { error: 'not an http(s) URL' }
  if (ownOrigin && u.origin === ownOrigin) return { error: 'platform URLs never open from a link' }
  if (u.port) return { error: 'a declared host has no port' }
  const host = u.hostname.toLowerCase().replace(/\.$/, '')
  if (!cfg.external_links.some((h) => h.toLowerCase() === host)) {
    return { error: cfg.external_links.length ? 'host not declared by the app' : 'this app opens no sites from a link' }
  }
  return { url: u.href }
}

/** A snapshot's own URL (a link card, an image or media by address) as an
 * attribute value: absolute http(s) only, else '' (a `javascript:` card
 * would run on the platform origin). */
export function webUrl(raw: unknown): string {
  try {
    const u = new URL(String(raw || ''))
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : ''
  } catch {
    return ''
  }
}

/** The link page's own query string as data for the page (a vendor's
 * return trip carries it), capped. */
export function linkQuery(search: string): string {
  const s = String(search || '')
  return (s.startsWith('?') ? s.slice(1) : s).slice(0, 512)
}

/** What the host does with one message from the frame: sizes on
 * content_height, runs declared actions through the batch, and answers
 * everything else with the terminal "not available" the runtime expects,
 * so a page built for the dashboard never waits on a link. */
export function classify(d: HostMessage | null | undefined): Reply {
  if (!d || d.source !== 'otodock-artifact') return { kind: 'ignore' }
  switch (d.type) {
    case 'content_height': {
      const h = Number(d.height)
      return Number.isFinite(h) && h > 0 ? { kind: 'height', height: Math.ceil(h) } : { kind: 'ignore' }
    }
    case 'app_actions': {
      const raw = Array.isArray(d.calls) ? d.calls : []
      const calls: ActionCall[] = []
      for (const c of raw.slice(0, 16)) {
        const call = c as Partial<ActionCall>
        calls.push({
          call_id: typeof call.call_id === 'string' ? call.call_id.slice(0, 64) : '',
          id: typeof call.id === 'string' ? call.id : '',
          args: call.args === undefined ? null : call.args,
        })
      }
      return { kind: 'actions', calls }
    }
    case 'app_action':
      return { kind: 'actions', calls: [{ call_id: '', id: String(d.id || ''), args: d.args === undefined ? null : d.args }] }
    case 'feed_subscribe':
      return { kind: 'ack', type: 'feed_update', payload: { feed: String(d.feed || ''), error: 'not available on shared links' } }
    case 'open_target':
      return { kind: 'ack', type: 'open_ack', payload: { status: 'unavailable', reason: 'not available on shared links' } }
    case 'open_url':
      return { kind: 'open', url: String(d.url || ''), call_id: typeof d.call_id === 'string' ? d.call_id.slice(0, 64) : '' }
    case 'action':
      return { kind: 'ack', type: 'action_ack', payload: { status: 'unavailable', reason: 'not available on shared links' } }
    case 'platform_call':
      return { kind: 'ack', type: 'platform_result', payload: {
        call_id: typeof d.call_id === 'string' ? d.call_id.slice(0, 64) : '', ok: false, reason: 'not available on shared links' } }
    case 'ready':
    case 'viewer_token_expired':
      return { kind: 'token' }
    case 'session_set':
      return { kind: 'session', token: typeof d.token === 'string' ? d.token.slice(0, 512) : '' }
    case 'session_clear':
      return { kind: 'session', token: '' }
    case 'challenge':
      return { kind: 'challenge', call_id: typeof d.call_id === 'string' ? d.call_id.slice(0, 64) : '' }
    default:
      return { kind: 'ignore' }
  }
}

/** The per-call results a batch response must resolve to: one line per call
 * from the server, and a `denied` for any call the server never answered. */
export function settleBatch(calls: ActionCall[], lines: string[]): Record<string, { status: string; reason?: string; result?: string; run_id?: string }> {
  const out: Record<string, { status: string; reason?: string; result?: string; run_id?: string }> = {}
  for (const line of lines) {
    if (!line.trim()) continue
    try {
      const r = JSON.parse(line) as { call_id?: string; status?: string; reason?: string; result?: string; run_id?: string }
      if (typeof r.call_id === 'string') out[r.call_id] = { status: r.status || 'denied', reason: r.reason, result: r.result, run_id: r.run_id }
    } catch { /* a torn line: the call settles as denied below */ }
  }
  for (const c of calls) {
    if (!(c.call_id in out)) out[c.call_id] = { status: 'denied', reason: 'no answer' }
  }
  return out
}

/** The frame-facing result for one call, in the shape the runtime turns into
 * `otodock:action-result` (mcp_tool) or an action ack (fire_task). */
export function resultMessage(call: ActionCall, r: { status: string; reason?: string; result?: string }): Record<string, unknown> {
  const ok = r.status === 'ok' || r.status === 'sent'
  return {
    source: 'otodock-host', type: 'action_result', id: call.id, call_id: call.call_id,
    ok, result: ok ? (r.result || '') : (r.reason || r.status),
  }
}
