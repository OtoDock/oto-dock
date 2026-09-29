// The widget kit (proxy APPS.md "The widget kit"): standard shapes for app
// pages — a session list, lane cards, a task table, a notification list, a
// checks table, a file link and a connect button — bound to the catalog feeds through the
// runtime the host injects (`otodock.feed`, `otodock.platform`,
// `otodock.open`). Built to /ui-kit/otodock-widgets.js; an app page adds
// `<script src="/ui-kit/otodock-widgets.js">` and calls
// `otodockWidgets.mount(el, {widget: 'sessions'})`. Byte-static: nothing
// here is per row or per viewer; the feed a widget binds must be declared
// as a `data_feed` action of the app (the host refuses the rest, and the
// widget shows that refusal in place). Themed through the token vars only
// (`--p-*`), so `.dark` and `otodock:theme` need no re-render.

export type Row = Record<string, unknown>
export type FeedCallback = (rows: Row[], error: string | null) => void

interface Otodock {
  feed?: (name: string, cb: FeedCallback) => void
  platform?: (method: string, args?: unknown) => Promise<unknown>
  open?: (target: Record<string, unknown>) => void
}

declare global {
  interface Window {
    otodock?: Otodock
    otodockWidgets?: WidgetsApi
  }
}

export type WidgetName = 'sessions' | 'lanes' | 'tasks' | 'notifications' | 'checks' | 'file' | 'connect'

export interface MountOptions {
  widget: WidgetName
  /** A heading above the widget; none by default. */
  title?: string
  /** Rows shown at most (lists and tables). Default 20. */
  limit?: number
  /** `lanes`: read `project_lanes` (inside a project Dock) instead of `sessions`. */
  feed?: 'sessions' | 'project_lanes'
  /** `file`: the workspace path the link opens, and the label. */
  path?: string
  label?: string
  /** `connect`: one provider (default: every provider the agent's MCPs use). */
  provider?: string
}

export interface Handle {
  destroy: () => void
}

export interface WidgetsApi {
  version: number
  mount: (el: HTMLElement, opts: MountOptions) => Handle
  render: {
    sessions: (rows: Row[], limit?: number) => HTMLElement
    lanes: (rows: Row[], limit?: number) => HTMLElement
    tasks: (rows: Row[], limit?: number) => HTMLElement
    notifications: (rows: Row[], limit?: number) => HTMLElement
    checks: (rows: Row[], limit?: number) => HTMLElement
  }
}

const STYLE_ID = 'otodock-widgets-style'
const CSS = `
.odw{font:13px/1.4 system-ui,sans-serif;color:var(--p-text,#111);}
.odw h3{margin:0 0 6px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--p-text-secondary,#555)}
.odw ul{list-style:none;margin:0;padding:0}
.odw li{display:flex;align-items:baseline;gap:8px;padding:6px 0;border-bottom:1px solid var(--p-border-light,#e5e5e5)}
.odw li:last-child{border-bottom:0}
.odw .odw-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.odw .odw-muted{color:var(--p-text-light,#888);font-size:12px}
.odw .odw-pill{font-size:11px;padding:1px 6px;border-radius:999px;background:var(--p-primary,#2563eb);color:#fff;opacity:.85}
.odw .odw-pill[data-s="idle"],.odw .odw-pill[data-s="completed"],.odw .odw-pill[data-s="done"]{opacity:.45}
.odw .odw-pill[data-s="failed"],.odw .odw-pill[data-s="danger"],.odw .odw-pill[data-s="fail"],.odw .odw-pill[data-s="error"]{background:#dc2626}
.odw .odw-pill[data-s="warning"]{background:#d97706}
.odw .odw-pill[data-s="pass"]{background:#16a34a}
.odw .odw-pill[data-s="skipped"]{opacity:.45}
.odw button.odw-btn{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--p-border-light,#e5e5e5);background:var(--p-surface,#fff);color:var(--p-text,#111);cursor:pointer}
.odw button.odw-btn.odw-primary{background:var(--p-primary,#2563eb);color:#fff;border-color:transparent}
.odw button.odw-btn:disabled{opacity:.5;cursor:default}
.odw table{width:100%;border-collapse:collapse}
.odw th,.odw td{text-align:left;padding:6px 4px;border-bottom:1px solid var(--p-border-light,#e5e5e5);vertical-align:top}
.odw th{font-weight:600;font-size:12px;color:var(--p-text-secondary,#555)}
.odw .odw-scroll{max-width:100%;overflow-x:auto}
.odw table.odw-tasks{table-layout:auto;min-width:0}
.odw table.odw-tasks td:first-child .odw-title{white-space:normal;overflow-wrap:anywhere}
.odw table.odw-tasks td:nth-child(2){white-space:normal;max-width:14em;overflow-wrap:anywhere}
.odw table.odw-tasks td:nth-child(3){white-space:nowrap;width:1%}
.odw .odw-next{font-size:11px;opacity:.8}
.odw .odw-lanes{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.odw .odw-card{border:1px solid var(--p-border-light,#e5e5e5);border-radius:10px;padding:10px;background:var(--p-surface,#fff)}
.odw .odw-card h4{margin:0 0 6px;font-size:12px;color:var(--p-text-secondary,#555)}
.odw .odw-empty{padding:10px 0;color:var(--p-text-light,#888)}
.odw a.odw-row{color:inherit;text-decoration:none;cursor:pointer}
.odw a.odw-row:hover .odw-title{text-decoration:underline}
`

function ensureStyle(): void {
  if (typeof document === 'undefined' || document.getElementById(STYLE_ID)) return
  const style = document.createElement('style')
  style.id = STYLE_ID
  style.textContent = CSS
  document.head.appendChild(style)
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text = ''): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  if (cls) node.className = cls
  if (text) node.textContent = text
  return node
}

function str(v: unknown): string {
  return v === null || v === undefined ? '' : String(v)
}

/** A time as "12:04" today, "Mon 12:04" within six days either way (a last
 * run behind, a next run ahead), else the date. */
export function when(iso: unknown): string {
  const s = str(iso)
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return hm
  if (Math.abs(now.getTime() - d.getTime()) < 6 * 86400_000) return `${d.toLocaleDateString([], { weekday: 'short' })} ${hm}`
  return d.toLocaleDateString()
}

function open(target: Record<string, unknown>): void {
  window.otodock?.open?.(target)
}

function rowLink(kind: string, id: string, extra: Record<string, unknown> = {}): HTMLAnchorElement {
  const a = el('a', 'odw-row')
  a.setAttribute('role', 'link')
  a.tabIndex = 0
  const target = { kind, ...(id ? { id } : {}), ...extra }
  a.addEventListener('click', (e) => { e.preventDefault(); open(target) })
  a.addEventListener('keydown', (e) => { if ((e as KeyboardEvent).key === 'Enter') open(target) })
  return a
}

function empty(text: string): HTMLElement {
  return el('div', 'odw-empty', text)
}

export function renderSessions(rows: Row[], limit = 20): HTMLElement {
  const ul = el('ul')
  const shown = rows.slice(0, limit)
  if (!shown.length) return empty('No chats yet.')
  for (const r of shown) {
    const li = el('li')
    const a = rowLink('chat', str(r.id))
    a.appendChild(el('span', 'odw-title', str(r.title) || 'Untitled chat'))
    li.appendChild(a)
    const pill = el('span', 'odw-pill', str(r.status) || 'idle')
    pill.dataset.s = str(r.status) || 'idle'
    li.appendChild(pill)
    if (r.updated_at) li.appendChild(el('span', 'odw-muted', when(r.updated_at)))
    ul.appendChild(li)
  }
  return ul
}

/** The `sessions` rows grouped by project: one card per project, the
 * lanes inside; chats with no project under "Chats". */
export function renderLanes(rows: Row[], limit = 20): HTMLElement {
  const groups = new Map<string, Row[]>()
  for (const r of rows) {
    const key = str(r.project_id) || ''
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(r)
  }
  if (!rows.length) return empty('No lanes right now.')
  const grid = el('div', 'odw-lanes')
  for (const [project, lanes] of groups) {
    const card = el('div', 'odw-card')
    card.appendChild(el('h4', '', project ? `Project ${project.slice(0, 8)}` : 'Chats'))
    card.appendChild(renderSessions(lanes, limit))
    grid.appendChild(card)
  }
  return grid
}

export function renderTasks(rows: Row[], limit = 20): HTMLElement {
  const shown = rows.slice(0, limit)
  if (!shown.length) return empty('No tasks.')
  // On a phone the three columns do not fit a nowrap name: the name wraps,
  // the schedule wraps inside a bounded column, the last-run column keeps
  // its width, and the table scrolls inside its own box before the page
  // ever grows wider than the screen (found on the developer dashboard's
  // Stats tab on a phone, 2026-09-15). The schedule is the feed's words
  // (`schedule_text`, in the task's zone) with the next run under it; the
  // raw cron and the type are the fallback for a delta row the snapshot
  // never carried (an unknown id lands as a partial) and an older proxy.
  const table = el('table', 'odw-tasks')
  const head = el('thead')
  const hr = el('tr')
  for (const h of ['Task', 'Schedule', 'Last run']) hr.appendChild(el('th', '', h))
  head.appendChild(hr)
  table.appendChild(head)
  const body = el('tbody')
  for (const r of shown) {
    const tr = el('tr')
    const name = el('td')
    // A task has no page of its own: its name opens the agent's tasks
    // (the host's settings kind, behind its first-use chip).
    const a = rowLink('agent_settings', '', { tab: 'scheduled-tasks' })
    a.appendChild(el('span', 'odw-title', str(r.name) || str(r.id)))
    name.appendChild(a)
    if (r.enabled === false) name.appendChild(el('span', 'odw-muted', ' (paused)'))
    tr.appendChild(name)
    const sched = el('td', 'odw-muted', str(r.schedule_text) || str(r.schedule) || str(r.task_type))
    if (r.next_run_time) sched.appendChild(el('div', 'odw-next', `next ${when(r.next_run_time)}`))
    tr.appendChild(sched)
    const last = (r.last_run || null) as Row | null
    const cell = el('td')
    if (last) {
      const pill = el('span', 'odw-pill', str(last.status))
      pill.dataset.s = str(last.status)
      const link = rowLink('run', str(last.id))
      link.appendChild(pill)
      cell.appendChild(link)
      if (last.completed_at) cell.appendChild(el('span', 'odw-muted', ` ${when(last.completed_at)}`))
    } else {
      cell.appendChild(el('span', 'odw-muted', 'never'))
    }
    tr.appendChild(cell)
    body.appendChild(tr)
  }
  table.appendChild(body)
  const scroll = el('div', 'odw-scroll')
  scroll.appendChild(table)
  return scroll
}

/** When a check runs, in a few words (CHECKS.md "condition"): the Checks
 * page says it at length; the kit keeps to one cell. */
export function runsWords(condition: unknown): string {
  const c = (condition && typeof condition === 'object' ? condition : {}) as Record<string, unknown>
  const list = (k: string): string[] => (Array.isArray(c[k]) ? (c[k] as unknown[]).map(str) : [])
  if (c.always) return 'every turn'
  const parts: string[] = []
  if (list('kinds').length) parts.push(`writes ${list('kinds').join(', ')}`)
  if (list('events').length) parts.push(`on ${list('events').map((e) => (e.startsWith('tool:') ? e.slice(5) : e)).join(', ')}`)
  if (list('places').length) parts.push(`writes ${list('places').join(', ')}`)
  if (list('globs').length) parts.push(`under ${list('globs').join(', ')}`)
  if (list('commands').length) parts.push('on matching commands')
  return parts.length ? parts.join('; ') : 'any change'
}

/** The `checks` feed (CHECKS.md): one row per check the viewer may see —
 * the name with its standing, when it runs, and the last verdict as a
 * pill that opens the judged chat or run. */
export function renderChecks(rows: Row[], limit = 20): HTMLElement {
  const shown = rows.slice(0, limit)
  if (!shown.length) return empty('No checks.')
  const table = el('table', 'odw-tasks')
  const head = el('thead')
  const hr = el('tr')
  for (const h of ['Check', 'Runs', 'Last verdict']) hr.appendChild(el('th', '', h))
  head.appendChild(hr)
  table.appendChild(head)
  const body = el('tbody')
  for (const r of shown) {
    const tr = el('tr')
    const name = el('td')
    name.appendChild(el('span', 'odw-title', str(r.name) || str(r.id)))
    const notes = [r.mandatory ? 'mandatory' : '', r.owner ? 'yours' : '', r.broken ? 'broken' : ''].filter(Boolean)
    if (notes.length) name.appendChild(el('span', 'odw-muted', ` (${notes.join(', ')})`))
    tr.appendChild(name)
    tr.appendChild(el('td', 'odw-muted', runsWords(r.condition)))
    const last = (r.last_verdict || null) as Row | null
    const cell = el('td')
    if (last) {
      const pill = el('span', 'odw-pill', str(last.status) || 'pass')
      pill.dataset.s = str(last.status) || 'pass'
      if (last.chat_id || last.run_id) {
        const link = last.chat_id ? rowLink('chat', str(last.chat_id)) : rowLink('run', str(last.run_id))
        link.appendChild(pill)
        cell.appendChild(link)
      } else {
        cell.appendChild(pill)
      }
      if (typeof last.score === 'number') cell.appendChild(el('span', 'odw-muted', ` ${last.score.toFixed(2)}`))
      if (last.created_at) cell.appendChild(el('span', 'odw-muted', ` ${when(last.created_at)}`))
    } else {
      cell.appendChild(el('span', 'odw-muted', 'never'))
    }
    tr.appendChild(cell)
    body.appendChild(tr)
  }
  table.appendChild(body)
  const scroll = el('div', 'odw-scroll')
  scroll.appendChild(table)
  return scroll
}

export function renderNotifications(rows: Row[], limit = 20): HTMLElement {
  const shown = rows.slice(0, limit)
  if (!shown.length) return empty('Nothing new.')
  const ul = el('ul')
  for (const r of shown) {
    const li = el('li')
    const pill = el('span', 'odw-pill', str(r.severity) || 'info')
    pill.dataset.s = str(r.severity) || 'info'
    li.appendChild(pill)
    const text = el('span', 'odw-title', str(r.title))
    if (r.read) text.style.opacity = '.6'
    if (r.chat_id) {
      // The inbox spans agents: the chat is the notification's agent's.
      const a = rowLink('chat', str(r.chat_id), r.agent_slug ? { agent: str(r.agent_slug) } : {})
      a.appendChild(text)
      li.appendChild(a)
    } else {
      li.appendChild(text)
    }
    if (r.delivered_at) li.appendChild(el('span', 'odw-muted', when(r.delivered_at)))
    ul.appendChild(li)
  }
  return ul
}

function shell(el_: HTMLElement, title?: string): HTMLElement {
  ensureStyle()
  el_.classList.add('odw')
  el_.replaceChildren()
  if (title) el_.appendChild(el('h3', '', title))
  const body = el('div')
  el_.appendChild(body)
  return body
}

function feedWidget(el_: HTMLElement, opts: MountOptions, feed: string,
                    render: (rows: Row[], limit?: number) => HTMLElement): Handle {
  const body = shell(el_, opts.title)
  let alive = true
  body.appendChild(empty('Loading…'))
  const otodock = window.otodock
  if (!otodock?.feed) {
    body.replaceChildren(empty('This widget needs the app runtime (open the page as an app).'))
    return { destroy: () => { alive = false } }
  }
  otodock.feed(feed, (rows, error) => {
    if (!alive) return
    body.replaceChildren(error ? empty(error) : render(rows || [], opts.limit ?? 20))
  })
  return { destroy: () => { alive = false; el_.replaceChildren() } }
}

function fileWidget(el_: HTMLElement, opts: MountOptions): Handle {
  const body = shell(el_, opts.title)
  const path = opts.path || ''
  const btn = el('button', 'odw-btn', opts.label || path.split('/').pop() || 'Open file')
  btn.disabled = !path
  btn.addEventListener('click', () => open({ kind: 'file', path }))
  body.appendChild(btn)
  const stamp = el('span', 'odw-muted')
  body.appendChild(stamp)
  let alive = true
  window.otodock?.feed?.('file_changes', (rows, error) => {
    if (!alive || error) return
    const mine = (rows || []).filter((r) => str(r.rel_path) === path || str(r.rel_path).endsWith('/' + path))
    if (mine.length) stamp.textContent = ` updated ${when(mine[mine.length - 1].at)}`
  })
  return { destroy: () => { alive = false; el_.replaceChildren() } }
}

function connectWidget(el_: HTMLElement, opts: MountOptions): Handle {
  const body = shell(el_, opts.title)
  body.appendChild(empty('Checking…'))
  const otodock = window.otodock
  let alive = true
  if (!otodock?.platform) {
    body.replaceChildren(empty('This widget needs the app runtime (open the page as an app).'))
    return { destroy: () => { alive = false } }
  }
  otodock.platform('integrations.status').then((res) => {
    if (!alive) return
    const providers = (((res as Row) || {}).providers || []) as Row[]
    const wanted = opts.provider ? providers.filter((p) => str(p.provider) === opts.provider) : providers
    body.replaceChildren()
    if (!wanted.length) {
      body.appendChild(empty(opts.provider ? `No MCP of this agent uses ${opts.provider}.` : 'Nothing to connect.'))
      return
    }
    for (const p of wanted) {
      // `identity` (since 1.7) says whose account the app's buttons run
      // with: the viewer's own on a personal app, the agent's service
      // account on a shared one — where a manager binds it in the agent's
      // settings, not the viewer in theirs.
      const identity = str(p.identity)
      const account = str(p.account)
      const agents = identity === 'the agent'
      const label = p.connected
        ? (agents ? `${str(p.provider)}: the agent's account${account ? ` ${account}` : ''}`
          : identity === 'the owner' ? `${str(p.provider)}: the owner's account${account ? ` ${account}` : ''}`
            : `${str(p.provider)} connected${account ? ` as ${account}` : ''}`)
        : (agents ? `Bind a ${str(p.provider)} account to this agent`
          : identity === 'the owner' ? `${str(p.provider)}: the owner has not connected one`
            : `Connect ${str(p.provider)}`)
      const btn = el('button', 'odw-btn' + (p.connected ? '' : ' odw-primary'), label)
      btn.disabled = !!p.connected || identity === 'the owner'
      btn.addEventListener('click', () => open(agents
        ? { kind: 'agent_settings', tab: 'mcps' }
        : { kind: 'user_settings', tab: 'integrations', provider: str(p.provider) }))
      body.appendChild(btn)
    }
  }, (err: unknown) => {
    if (alive) body.replaceChildren(empty(str((err as Error)?.message || err) || 'not available'))
  })
  return { destroy: () => { alive = false; el_.replaceChildren() } }
}

export function mount(target: HTMLElement, opts: MountOptions): Handle {
  switch (opts.widget) {
    case 'sessions': return feedWidget(target, opts, 'sessions', renderSessions)
    case 'lanes': return feedWidget(target, opts, opts.feed === 'project_lanes' ? 'project_lanes' : 'sessions', renderLanes)
    case 'tasks': return feedWidget(target, opts, 'tasks', renderTasks)
    case 'notifications': return feedWidget(target, opts, 'notifications', renderNotifications)
    case 'checks': return feedWidget(target, opts, 'checks', renderChecks)
    case 'file': return fileWidget(target, opts)
    case 'connect': return connectWidget(target, opts)
    default: {
      const body = shell(target, opts.title)
      body.appendChild(empty(`Unknown widget ${String((opts as MountOptions).widget)}`))
      return { destroy: () => target.replaceChildren() }
    }
  }
}

// 2: the tasks table prints the feed's `schedule_text` and `next_run_time`
// (a page on 1 saw the raw cron in that column).
export const api: WidgetsApi = {
  version: 2,
  mount,
  render: {
    sessions: renderSessions, lanes: renderLanes, tasks: renderTasks, notifications: renderNotifications,
    checks: renderChecks,
  },
}

if (typeof window !== 'undefined') {
  window.otodockWidgets = api
}
