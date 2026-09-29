// The external link's page (SHARING.md "External links"), built to
// dist/ui-kit/share-host.js and loaded by the HTML the proxy serves at
// /s/<token>. It renders the password form when the link has one, then the
// app in the same scripts-only sandbox the dashboard uses, and answers the
// frame's messages through `host.ts`. Nothing here reads a dashboard
// cookie; the only requests are the link's own routes, same-origin.

import { batchUrl, classify, frameSrc, linkQuery, openAllowed, readConfig, resultMessage, sessionUrl, settleBatch, snapshotMediaUrl, snapshotUiUrl, snapshotUrl, stateUrl, unlockUrl, viewerTokenUrl, webUrl, type ActionCall, type ShareConfig, type SnapshotMessage } from './host'
import { WIRE } from '../api/wireEvents'

const root = document.getElementById('root')
const cfg = readConfig(document.getElementById('share-config')?.textContent, location.pathname)

// Cloudflare Turnstile (SHARING.md): loaded into the HOST document once —
// the frame's CSP cannot — for the password form and for a page's
// otodock.challenge(). Null when the script does not load.
interface TurnstileApi { render: (n: HTMLElement, o: Record<string, unknown>) => void }
let turnstileLoading: Promise<TurnstileApi | null> | null = null
function loadTurnstile(): Promise<TurnstileApi | null> {
  if (!turnstileLoading) {
    turnstileLoading = new Promise((resolve) => {
      const have = (window as unknown as { turnstile?: TurnstileApi }).turnstile
      if (have) { resolve(have); return }
      const s = document.createElement('script')
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
      s.async = true
      s.onload = () => resolve((window as unknown as { turnstile?: TurnstileApi }).turnstile ?? null)
      s.onerror = () => resolve(null)
      document.head.appendChild(s)
    })
  }
  return turnstileLoading
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Record<string, string> = {}, text = ''): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v)
  if (text) node.textContent = text
  return node
}

const theme = (): 'light' | 'dark' =>
  window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'

function renderUnavailable(message: string): void {
  if (!root) return
  root.replaceChildren()
  const p = el('p', { style: 'margin:auto;max-width:28rem;text-align:center;padding:0 1.5rem;opacity:.8' }, message)
  root.appendChild(p)
}

function renderPasswordForm(c: ShareConfig): void {
  if (!root) return
  root.replaceChildren()
  const wrap = el('div', { style: 'margin:auto;width:min(92vw,22rem);padding:1.5rem;border-radius:1rem;border:1px solid rgba(128,128,128,.25)' })
  wrap.appendChild(el('h1', { style: 'font-size:1rem;margin:0 0 .25rem' }, c.title))
  wrap.appendChild(el('p', { style: 'font-size:.8rem;opacity:.7;margin:0 0 1rem' }, 'This page is protected. Enter the password you were given.'))
  const form = el('form')
  const input = el('input', { type: 'password', autocomplete: 'current-password', 'aria-label': 'Password', required: 'true',
    style: 'width:100%;box-sizing:border-box;padding:.6rem .7rem;border-radius:.5rem;border:1px solid rgba(128,128,128,.4);background:transparent;color:inherit;font:inherit' })
  const err = el('p', { style: 'font-size:.8rem;color:#c33;min-height:1.2em;margin:.5rem 0 0', role: 'alert' })
  const btn = el('button', { type: 'submit', style: 'margin-top:.75rem;width:100%;padding:.6rem;border-radius:.5rem;border:0;background:#2f6feb;color:#fff;font:inherit;cursor:pointer' }, 'Open')
  form.append(input, err, btn)
  let turnstileToken = ''
  if (c.turnstile_site_key) {
    const holder = el('div', { style: 'margin-top:.75rem' })
    form.appendChild(holder)
    void loadTurnstile().then((ts) => {
      ts?.render(holder, { sitekey: c.turnstile_site_key, callback: (t: string) => { turnstileToken = t } })
    })
  }
  form.onsubmit = async (e) => {
    e.preventDefault()
    btn.setAttribute('disabled', 'true')
    err.textContent = ''
    try {
      const res = await fetch(unlockUrl(c), {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: input.value, turnstile_token: turnstileToken || undefined }),
      })
      if (res.ok) {
        // The release hash is withheld from a locked page; the unlock answer carries it.
        const ok = await res.json().catch(() => null) as { release_sha?: unknown } | null
        const sha = typeof ok?.release_sha === 'string' ? ok.release_sha : c.release_sha
        renderTarget({ ...c, needs_password: false, release_sha: sha })
        return
      }
      const body = await res.json().catch(() => null) as { detail?: string } | null
      err.textContent = res.status === 404 ? 'This link is no longer available.' : (body?.detail || 'Wrong password')
    } catch {
      err.textContent = 'Could not reach the server'
    } finally {
      btn.removeAttribute('disabled')
    }
  }
  wrap.appendChild(form)
  root.appendChild(wrap)
  input.focus()
}

function renderApp(c: ShareConfig): void {
  if (!root) return
  root.replaceChildren()
  // The app fills the page like a site of its own: no bar above it (the
  // operator on the VM pass, 2026-09-18 — an external link is the app's
  // public face; the platform's name belongs on the dashboard, not here).
  // The page title carries the app's name; a chat snapshot keeps its header
  // because "shared by X, read-only" is what that page is.
  const frame = el('iframe', {
    sandbox: 'allow-scripts', referrerpolicy: 'no-referrer', title: c.title,
    src: frameSrc(c, theme()),
    style: 'flex:1;width:100%;height:100%;border:0;background:transparent',
  })
  root.append(frame)

  const post = (msg: Record<string, unknown>) => {
    try { frame.contentWindow?.postMessage({ source: 'otodock-host', ...msg }, '*') } catch { /* frame gone */ }
  }

  frame.addEventListener('load', async () => {
    // `url` is the link's own address without its query: what a vendor's
    // return URL points back to (APPS.md "External links").
    post({ type: 'shared_link', actions: c.actions, links: c.external_links,
      url: location.origin + location.pathname, query: linkQuery(location.search) })
    try {
      const res = await fetch(stateUrl(c), { credentials: 'same-origin' })
      if (res.ok) {
        const st = await res.json() as { doc?: unknown; rev?: number }
        post({ type: 'state', doc: st.doc && typeof st.doc === 'object' ? st.doc : {}, rev: Number(st.rev || 0) })
      }
    } catch { /* the page keeps what it baked in */ }
  })

  let lastBatchAt = 0
  const runCalls = async (calls: ActionCall[]) => {
    const now = Date.now()
    const wait = Math.max(0, 1000 - (now - lastBatchAt))
    lastBatchAt = now + wait
    if (wait) await new Promise((r) => setTimeout(r, wait))
    let lines: string[] = []
    try {
      const res = await fetch(batchUrl(c), {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ calls: calls.map((k) => ({ call_id: k.call_id, action_id: k.id, args: k.args })) }),
      })
      lines = res.ok ? (await res.text()).split('\n') : []
      if (!res.ok) lines = calls.map((k) => JSON.stringify({ call_id: k.call_id, status: 'denied', reason: res.status === 401 ? 'password required' : 'not available on shared links' }))
    } catch {
      lines = calls.map((k) => JSON.stringify({ call_id: k.call_id, status: 'denied', reason: 'network error' }))
    }
    const settled = settleBatch(calls, lines)
    for (const k of calls) {
      const r = settled[k.call_id]
      post(resultMessage(k, r))
      post({ type: 'action_ack', status: r.status === 'ok' || r.status === 'sent' ? 'sent' : 'denied', reason: r.reason, call_id: k.call_id })
    }
  }

  // A folder app's page gets an external viewer claim for the app's own
  // API (APPS.md "External links"): minted here behind the link's own
  // gate, posted on `ready`, re-minted before it expires. The mint answer
  // also says whether the link's cookie holds the app's own session
  // (`app_session`, posted right after the claim). A refused mint (the
  // pace, a hiccup) is retried with a widening wait, six times, as the
  // dashboard does; a 401 means the password step is due again.
  interface Claim { token?: string; exp?: number; ttl?: number; session?: string | null }
  let tokenTimer: ReturnType<typeof setTimeout> | null = null
  let mintTries = 0
  const postClaim = (t: Claim, status: string) => {
    post({ type: 'viewer_token', token: t.token || '', exp: Number(t.exp || 0) })
    post({ type: 'app_session', token: t.session || null, status })
    if (tokenTimer) clearTimeout(tokenTimer)
    tokenTimer = setTimeout(() => { void postToken() }, Math.min(8 * 60 * 1000, Math.max(30_000, (t.ttl || 600) * 800)))
  }
  const retryMint = () => {
    if (mintTries >= 6) return
    mintTries += 1
    if (tokenTimer) clearTimeout(tokenTimer)
    tokenTimer = setTimeout(() => { void postToken(true) }, Math.min(30_000, 2000 * 2 ** (mintTries - 1)))
  }
  const postToken = async (retry = false) => {
    if (!c.folder) return
    if (tokenTimer) clearTimeout(tokenTimer)
    if (!retry) mintTries = 0
    try {
      const res = await fetch(viewerTokenUrl(c), { method: 'POST', credentials: 'same-origin' })
      if (res.status === 401) { renderPasswordForm({ ...c, needs_password: true }); return }
      if (!res.ok) { retryMint(); return }
      mintTries = 0
      const t = await res.json() as Claim
      postClaim(t, t.session ? 'ok' : 'none')
    } catch { retryMint() }
  }

  // otodock.session.set(token) / .clear(): the app's own session token into
  // (or out of) the link's cookie, through the proxy — the host cannot read
  // an HttpOnly cookie — and a fresh claim carrying it back to the page.
  const setSession = async (token: string) => {
    try {
      const res = token
        ? await fetch(sessionUrl(c), { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) })
        : await fetch(sessionUrl(c), { method: 'DELETE', credentials: 'same-origin' })
      if (!res.ok) {
        post({ type: 'app_session', token: null, status: res.status === 401 ? 'password required' : 'unavailable' })
        return
      }
      postClaim(await res.json() as Claim, token ? 'ok' : 'cleared')
    } catch {
      post({ type: 'app_session', token: null, status: 'unavailable' })
    }
  }

  // otodock.challenge(): the bot check in an overlay over the frame (the
  // frame's CSP cannot load it); the token goes back to the page, which
  // sends it on its own request. No site key on this install: unavailable.
  const challenge = async (callId: string) => {
    const answer = (token: string | null, status: string) =>
      post({ type: 'challenge_result', call_id: callId, token, status })
    if (!c.turnstile_site_key || !root) return answer(null, 'unavailable')
    const ts = await loadTurnstile()
    if (!ts) return answer(null, 'unavailable')
    const overlay = el('div', { role: 'dialog', 'aria-label': 'Verification',
      style: 'position:fixed;inset:0;z-index:10;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.45)' })
    const box = el('div', { style: 'width:min(92vw,22rem);padding:1.25rem;border-radius:1rem;background:#f6f7f9;color:#223' })
    box.appendChild(el('p', { style: 'font-size:.85rem;margin:0 0 .75rem' }, 'A quick check that you are a person.'))
    const holder = el('div')
    box.appendChild(holder)
    const cancel = el('button', { type: 'button', style: 'margin-top:.75rem;width:100%;padding:.5rem;border-radius:.5rem;border:1px solid rgba(128,128,128,.4);background:transparent;color:inherit;font:inherit;cursor:pointer' }, 'Cancel')
    box.appendChild(cancel)
    overlay.appendChild(box)
    root.appendChild(overlay)
    let done = false
    const finish = (token: string | null, status: string) => {
      if (done) return
      done = true
      overlay.remove()
      answer(token, status)
    }
    cancel.onclick = () => finish(null, 'cancelled')
    try {
      ts.render(holder, {
        sitekey: c.turnstile_site_key,
        callback: (t: string) => finish(t, 'ok'),
        'error-callback': () => finish(null, 'failed'),
      })
    } catch {
      finish(null, 'unavailable')
    }
  }

  // The host-mediated open (APPS.md "External links"): a declared host, from
  // a user gesture, three per ten seconds, in a new tab with no opener and
  // no referrer (the link's token must not ride to the vendor). The frame
  // itself never navigates.
  const openTimes: number[] = []
  const openUrl = (raw: string, callId: string) => {
    const ack = (status: string, reason?: string) =>
      post({ type: 'open_url_ack', status, url: raw, ...(reason ? { reason } : {}), ...(callId ? { call_id: callId } : {}) })
    const v = openAllowed(c, raw, location.origin)
    if ('error' in v) return ack('blocked', v.error)
    const ua = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation
    if (ua && !ua.isActive) return ack('denied', 'no user gesture')
    const now = Date.now()
    while (openTimes.length && now - openTimes[0] > 10_000) openTimes.shift()
    if (openTimes.length >= 3) return ack('denied', 'rate limited')
    openTimes.push(now)
    const w = window.open(v.url, '_blank', 'noopener,noreferrer')
    ack(w === null && !ua ? 'denied' : 'opened', w === null && !ua ? 'popup blocked' : undefined)
  }

  window.addEventListener('message', (e: MessageEvent) => {
    if (e.source !== frame.contentWindow) return
    const reply = classify(e.data)
    if (reply.kind === 'height') return
    if (reply.kind === 'token') return void postToken()
    if (reply.kind === 'session') return void setSession(reply.token)
    if (reply.kind === 'challenge') return void challenge(reply.call_id)
    if (reply.kind === 'open') return openUrl(reply.url, reply.call_id)
    if (reply.kind === 'ack') return post({ type: reply.type, ...reply.payload })
    if (reply.kind === 'actions') void runCalls(reply.calls)
  })
}

function block(m: SnapshotMessage, c: ShareConfig): HTMLElement | null {
  if (m.role === 'user' || m.role === 'assistant') {
    const mine = m.role === 'user'
    const row = el('div', { style: `display:flex;justify-content:${mine ? 'flex-end' : 'flex-start'}` })
    const bubble = el('div', {
      style: `max-width:85%;padding:.5rem .75rem;border-radius:1rem;font-size:.9rem;white-space:pre-wrap;overflow-wrap:anywhere;${
        mine ? 'background:#2f6feb;color:#fff' : 'background:rgba(128,128,128,.12)'}`,
    }, m.content || '')
    row.appendChild(bubble)
    return row
  }
  const d = (m.data ?? {}) as Record<string, any>
  const card = el('div', { style: 'border:1px solid rgba(128,128,128,.25);border-radius:.75rem;overflow:hidden' })
  switch (m.event_type) {
    case WIRE.UI: {
      if (d.title) card.appendChild(el('p', { style: 'margin:0;padding:.4rem .75rem;font-size:.8rem;font-weight:600;border-bottom:1px solid rgba(128,128,128,.2)' }, String(d.title)))
      const h = Math.min(Math.max(Number(d.height) || 320, 120), 900)
      card.appendChild(el('iframe', { sandbox: 'allow-scripts', referrerpolicy: 'no-referrer', title: String(d.title || 'Artifact'),
        src: snapshotUiUrl(c, String(d.token || '')), style: `display:block;width:100%;height:${h}px;border:0;background:transparent` }))
      return card
    }
    case WIRE.IMAGES: {
      const wrap = el('div', { style: 'display:flex;flex-wrap:wrap;gap:.5rem' })
      for (const img of (d.images as Array<Record<string, string>> | undefined) ?? []) {
        const fig = el('figure', { style: 'margin:0;max-width:20rem;border:1px solid rgba(128,128,128,.25);border-radius:.75rem;overflow:hidden' })
        fig.appendChild(el('img', { src: img.token ? snapshotMediaUrl(c, img.token) : webUrl(img.url), alt: img.caption || '', style: 'display:block;max-height:18rem;width:100%;object-fit:contain' }))
        if (img.caption) fig.appendChild(el('figcaption', { style: 'padding:.25rem .5rem;font-size:.7rem;opacity:.7' }, img.caption))
        wrap.appendChild(fig)
      }
      return wrap
    }
    case WIRE.VIDEO:
    case WIRE.AUDIO: {
      const src = d.token ? snapshotMediaUrl(c, String(d.token)) : webUrl(d.url)
      const media = el(m.event_type, { controls: 'true', src, style: m.event_type === WIRE.VIDEO ? 'display:block;width:100%;max-height:24rem' : 'display:block;width:100%' })
      card.appendChild(media)
      if (d.caption || d.title) card.appendChild(el('p', { style: 'margin:0;padding:.3rem .75rem;font-size:.7rem;opacity:.7' }, String(d.caption || d.title)))
      return card
    }
    case WIRE.FILE: {
      const a = el('a', { href: snapshotMediaUrl(c, String(d.token || '')), download: String(d.filename || ''), style: 'display:inline-block;padding:.5rem .75rem;border:1px solid rgba(128,128,128,.25);border-radius:.75rem;font-size:.85rem;color:inherit;text-decoration:none' }, String(d.filename || 'file'))
      return a
    }
    case WIRE.URL: {
      const href = webUrl(d.url)
      if (!href) return el('p', { style: 'margin:0;font-size:.85rem' }, String(d.title || d.url || ''))
      const a = el('a', { href, target: '_blank', rel: 'noreferrer noopener', style: 'display:block;padding:.5rem .75rem;border:1px solid rgba(128,128,128,.25);border-radius:.75rem;font-size:.85rem;color:#2f6feb;text-decoration:none' }, String(d.title || d.url || ''))
      return a
    }
    default: {
      const details = el('details', { style: 'font-size:.7rem;opacity:.7;padding:.3rem .75rem;border:1px dashed rgba(128,128,128,.3);border-radius:.5rem' })
      details.appendChild(el('summary', {}, m.event_type || 'event'))
      details.appendChild(el('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:12rem;overflow:auto;font-size:.65rem' }, m.event_data || ''))
      return details
    }
  }
}

async function renderChat(c: ShareConfig): Promise<void> {
  if (!root) return
  root.replaceChildren()
  const header = el('header', { style: 'display:flex;align-items:center;gap:.5rem;padding:.5rem .75rem;border-bottom:1px solid rgba(128,128,128,.2);font-size:.85rem' })
  header.appendChild(el('span', { style: 'font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, c.title))
  const sub = el('span', { style: 'margin-left:auto;font-size:.7rem;opacity:.6;white-space:nowrap' }, 'Shared with you · read-only')
  header.appendChild(sub)
  const main = el('main', { style: 'flex:1;overflow-y:auto;padding:1rem .75rem' })
  const list = el('div', { style: 'max-width:48rem;margin:0 auto;display:flex;flex-direction:column;gap:.75rem' })
  main.appendChild(list)
  root.append(header, main)
  try {
    const res = await fetch(snapshotUrl(c), { credentials: 'same-origin' })
    if (!res.ok) {
      renderUnavailable(res.status === 401 ? 'Enter the password first.' : 'This link is not available.')
      return
    }
    const snap = await res.json() as { title?: string; shared_by_name?: string; messages?: SnapshotMessage[] }
    if (snap.shared_by_name) sub.textContent = `Shared by ${snap.shared_by_name} · read-only`
    for (const m of snap.messages ?? []) {
      const node = block(m, c)
      if (node) list.appendChild(node)
    }
    if (!list.childElementCount) list.appendChild(el('p', { style: 'text-align:center;opacity:.6;font-size:.8rem' }, 'Nothing to show.'))
  } catch {
    renderUnavailable('Could not reach the server.')
  }
}

function renderTarget(c: ShareConfig): void {
  if (c.kind === 'chat') void renderChat(c)
  else renderApp(c)
}

if (!cfg) {
  renderUnavailable('This link is not available.')
} else if (cfg.needs_password) {
  renderPasswordForm(cfg)
} else {
  renderTarget(cfg)
}
