// The widget kit (proxy APPS.md "The widget kit"): standard shapes for app
// pages — a session list, lane cards, a task table, a notification list, a
// checks table, a file link, a connect button and a media player — bound to the catalog
// feeds and methods through the runtime the host injects (`otodock.feed`,
// `otodock.platform`, `otodock.open`). Built to /ui-kit/otodock-widgets.js; an app page adds
// `<script src="/ui-kit/otodock-widgets.js">` and calls
// `otodockWidgets.mount(el, {widget: 'sessions'})`. Byte-static: nothing
// here is per row or per viewer; the feed a widget binds must be declared
// as a `data_feed` action of the app (the host refuses the rest, and the
// widget shows that refusal in place). Themed through the token vars only
// (`--p-*`), so `.dark` and `otodock:theme` need no re-render.

import { WIDGET_TOKENS } from './tokens.generated'

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

export type WidgetName = 'sessions' | 'lanes' | 'tasks' | 'notifications' | 'checks' | 'file' | 'connect' | 'media'

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
  /** `media`: the kit's own bar under the player (default true), the frames
      a second `step` counts with (default 30), a second picture to compare
      with a split, and how a picture fills its box. */
  controls?: boolean
  fps?: number
  compare?: string
  fit?: 'contain' | 'cover' | 'actual'
  muted?: boolean
  autoplay?: boolean
}

export interface Handle {
  destroy: () => void
}

/** What `files.media` answers for one file. */
export interface MediaInfo {
  path: string
  url: string
  expires_in: number
  kind: 'video' | 'audio' | 'image'
  mime: string
  size: number
}

/** A file's media link, kept fresh for as long as it is used. */
export interface MediaLink {
  url: string
  kind: string
  ready: Promise<MediaInfo>
  onchange: ((url: string) => void) | null
  /** The file failed on its element and the platform refused a new link:
   *  the viewer may no longer read it. Never called for a passing failure. */
  onlost: (() => void) | null
  attach: (el: HTMLMediaElement | HTMLImageElement) => void
  refresh: () => Promise<MediaInfo>
  release: () => void
}

export type MediaEvent = 'time' | 'ready' | 'play' | 'pause' | 'ended' | 'error'

/** The media widget's handle: what a timeline drives. */
export interface MediaHandle extends Handle {
  el: HTMLMediaElement | HTMLImageElement | null
  play: () => void
  pause: () => void
  seek: (t: number) => void
  step: (frames: number) => void
  loop: (start: number | null, end?: number) => void
  rate: (r?: number) => number
  zoom: (z: number) => void
  split: (fraction: number) => void
  on: (event: MediaEvent, cb: (time: number) => void) => () => void
  readonly currentTime: number
  readonly duration: number
  readonly paused: boolean
}

export interface WidgetsApi {
  version: number
  mount: (el: HTMLElement, opts: MountOptions) => Handle
  mediaLink: (path: string) => MediaLink
  render: {
    sessions: (rows: Row[], limit?: number) => HTMLElement
    lanes: (rows: Row[], limit?: number) => HTMLElement
    tasks: (rows: Row[], limit?: number) => HTMLElement
    notifications: (rows: Row[], limit?: number) => HTMLElement
    checks: (rows: Row[], limit?: number) => HTMLElement
  }
}

const STYLE_ID = 'otodock-widgets-style'
// Each colour reads the kit's variable first; the fallback is the token's
// light value, for a page that has not loaded the kit's tokens.
const T = WIDGET_TOKENS
const CSS = `
.odw{font:14px/20px var(--p-font,${T.font});color:var(--p-text,${T.text});}
.odw h3{margin:0 0 8px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--p-text-secondary,${T.textSecondary})}
.odw ul{list-style:none;margin:0;padding:0}
.odw li{display:flex;align-items:baseline;gap:8px;padding:8px 0;border-bottom:1px solid var(--p-border-light,${T.border})}
.odw li:last-child{border-bottom:0}
.odw .odw-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.odw li>.odw-pill,.odw li>.odw-muted{flex:none;white-space:nowrap}
.odw li>a.odw-row{display:flex;min-width:0}
.odw .odw-muted{color:var(--p-text-light,${T.textTertiary});font-size:12px}
.odw .odw-pill{font-size:11px;line-height:16px;padding:2px 8px;border-radius:9999px;background:var(--p-surface,${T.surface});color:var(--p-text-secondary,${T.textSecondary})}
.odw .odw-pill[data-s="generating"]{background:var(--p-brand-surface,${T.brandSurface});color:var(--p-link,${T.link})}
.odw .odw-pill[data-s="info"],.odw .odw-pill[data-s="running"],.odw .odw-pill[data-s="pending"]{background:var(--p-status-info-bg,${T.infoBg});color:var(--p-status-info-fg,${T.infoFg})}
.odw .odw-pill[data-s="failed"],.odw .odw-pill[data-s="danger"],.odw .odw-pill[data-s="fail"],.odw .odw-pill[data-s="error"]{background:var(--p-status-danger-bg,${T.dangerBg});color:var(--p-status-danger-fg,${T.dangerFg})}
.odw .odw-pill[data-s="warning"],.odw .odw-pill[data-s="awaiting_user"],.odw .odw-pill[data-s="limit_exceeded"]{background:var(--p-status-warning-bg,${T.warningBg});color:var(--p-status-warning-fg,${T.warningFg})}
.odw .odw-pill[data-s="pass"],.odw .odw-pill[data-s="success"],.odw .odw-pill[data-s="completed"]{background:var(--p-status-success-bg,${T.successBg});color:var(--p-status-success-fg,${T.successFg})}
.odw button.odw-btn{font:inherit;min-height:32px;padding:4px 12px;margin:0 8px 8px 0;border-radius:var(--p-radius-sm,6px);border:1px solid var(--p-border,${T.borderDefault});background:transparent;color:var(--p-text-secondary,${T.textSecondary});cursor:pointer}
.odw button.odw-btn:hover{background:var(--p-surface,${T.surface});color:var(--p-text,${T.text})}
.odw button.odw-btn.odw-primary{background:var(--p-action,${T.action});color:var(--p-action-fg,${T.actionFg});border-color:transparent}
.odw button.odw-btn.odw-primary:hover{background:var(--p-action-hover,${T.actionHover});color:var(--p-action-fg,${T.actionFg})}
.odw button.odw-btn:disabled{opacity:.5;cursor:not-allowed}
.odw a.odw-row:focus-visible,.odw button:focus-visible{outline:2px solid var(--p-focus,${T.focus});outline-offset:2px}
.odw table{width:100%;border-collapse:collapse}
.odw th,.odw td{text-align:left;padding:8px 4px;border-bottom:1px solid var(--p-border-light,${T.border});vertical-align:top}
.odw th{font-weight:600;font-size:12px;color:var(--p-text-secondary,${T.textSecondary})}
.odw .odw-scroll{max-width:100%;overflow-x:auto}
.odw table.odw-tasks{table-layout:auto;min-width:0}
.odw table.odw-tasks td:first-child .odw-title{white-space:normal;overflow-wrap:anywhere}
.odw table.odw-tasks td:nth-child(2){white-space:normal;max-width:14em;overflow-wrap:anywhere}
.odw table.odw-tasks td:nth-child(3){white-space:nowrap;width:1%}
.odw .odw-next{font-size:11px}
@media (max-width:767px){.odw table.odw-tasks thead{display:none}.odw table.odw-tasks tr{display:block;padding:8px 0;border-bottom:1px solid var(--p-border-light,${T.border})}.odw table.odw-tasks tr:last-child{border-bottom:0}.odw table.odw-tasks td:nth-child(n){display:block;max-width:none;width:auto;border:0;padding:2px 0;white-space:normal}}
.odw .odw-lanes{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.odw .odw-card{border:1px solid var(--p-border-light,${T.border});border-radius:var(--p-radius,8px);padding:12px;background:var(--p-card,${T.card})}
.odw .odw-card h4{margin:0 0 8px;font-size:12px;color:var(--p-text-secondary,${T.textSecondary})}
.odw .odw-empty{padding:8px 0;color:var(--p-text-light,${T.textTertiary})}
.odw a.odw-row{color:inherit;text-decoration:none;cursor:pointer}
.odw a.odw-row:hover .odw-title{text-decoration:underline}
.odw-media{display:flex;flex-direction:column;min-width:0;border:1px solid var(--p-border-light,${T.border});border-radius:var(--p-radius,8px);overflow:hidden;background:var(--p-card,${T.card})}
.odw-media .odw-stage{position:relative;display:flex;align-items:center;justify-content:center;min-height:0;overflow:hidden;background:var(--p-surface,${T.surface})}
.odw-media[data-kind="audio"] .odw-stage{display:none}
.odw-media video,.odw-media img{display:block;max-width:100%;max-height:70vh;object-fit:contain;transform-origin:center}
.odw-media[data-fit="cover"] img{width:100%;height:100%;object-fit:cover}
.odw-media[data-fit="actual"] .odw-stage{overflow:auto;justify-content:flex-start;align-items:flex-start}
.odw-media[data-fit="actual"] img{max-width:none;max-height:none}
.odw-media .odw-over{position:absolute;inset:0;width:100%;height:100%;max-height:none;object-fit:contain}
.odw-media .odw-line{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:var(--p-action,${T.action});pointer-events:none}
.odw-media input.odw-split{position:absolute;inset:0;width:100%;height:100%;margin:0;opacity:0;cursor:ew-resize}
.odw-media .odw-bar{display:flex;align-items:center;gap:8px;padding:6px 8px;min-width:0}
.odw-media .odw-bar button{flex:none;display:inline-flex;align-items:center;justify-content:center;min-width:32px;height:32px;padding:0 6px;border:0;border-radius:var(--p-radius-sm,6px);background:transparent;color:var(--p-text-secondary,${T.textSecondary});font:inherit;font-size:12px;cursor:pointer}
.odw-media .odw-bar button:hover{background:var(--p-surface,${T.surface});color:var(--p-text,${T.text})}
.odw-media .odw-bar svg{width:16px;height:16px;fill:currentColor}
.odw-media input.odw-seek{flex:1 1 0;width:0;min-width:40px;height:16px;margin:0;accent-color:var(--p-action,${T.action})}
.odw-media .odw-time{flex:none;font-size:12px;font-variant-numeric:tabular-nums;color:var(--p-text-secondary,${T.textSecondary});white-space:nowrap}
.odw-media .odw-note{padding:16px;color:var(--p-text-light,${T.textTertiary})}
.odw-media[data-gone] .odw-bar{display:none}
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

/** A status as a word: the map's, else the raw value with a capital and
 * its underscores as spaces. The pill keeps the raw value in `data-s`. */
function word(raw: unknown, map: Record<string, string>): string {
  const s = str(raw)
  if (map[s]) return map[s]
  const plain = s.replace(/_/g, ' ')
  return plain ? plain.charAt(0).toUpperCase() + plain.slice(1) : ''
}

const SESSION_WORDS = { generating: 'Working', awaiting_user: 'Needs you', idle: 'Idle' }
// The run's tones and words follow the dashboard's status badge.
const RUN_WORDS = { completed: 'Done', failed: 'Failed', running: 'Running', pending: 'Queued', cancelled: 'Cancelled', limit_exceeded: 'Over the limit' }
const VERDICT_WORDS = { pass: 'Passed', fail: 'Failed', error: 'Error', skipped: 'Skipped' }
const SEVERITY_WORDS = { info: 'Note', success: 'Done', warning: 'Warning', danger: 'Problem' }
const KIND_WORDS = { trigger: 'On trigger', schedule: 'On a schedule', scheduled: 'On a schedule', one_time: 'Once' }

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
    const pill = el('span', 'odw-pill', word(str(r.status) || 'idle', SESSION_WORDS))
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
  if (!rows.length) return empty('No project work right now.')
  const grid = el('div', 'odw-lanes')
  for (const [project, lanes] of groups) {
    const card = el('div', 'odw-card')
    // The project's title when the feed carries one; never its id.
    card.appendChild(el('h4', '', project ? str(lanes[0].project_title) || 'A project' : 'Chats'))
    card.appendChild(renderSessions(lanes, limit))
    grid.appendChild(card)
  }
  return grid
}

export function renderTasks(rows: Row[], limit = 20): HTMLElement {
  const shown = rows.slice(0, limit)
  if (!shown.length) return empty('No tasks.')
  // Under 768 px each row is a block of its cells; above it the name wraps,
  // the schedule wraps inside a bounded column, the last-run column keeps
  // its width, and the table scrolls inside its own box before the page
  // ever grows wider than the screen. The schedule is the feed's words
  // (`schedule_text`, in the task's zone) with the next run under it; a row
  // without them (a delta row the snapshot never carried, an older proxy)
  // says only what kind of task it is.
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
    a.appendChild(el('span', 'odw-title', str(r.name) || 'Untitled task'))
    name.appendChild(a)
    if (r.enabled === false) name.appendChild(el('span', 'odw-muted', ', paused'))
    tr.appendChild(name)
    const sched = el('td', 'odw-muted', str(r.schedule_text) || word(str(r.task_type) || 'schedule', KIND_WORDS))
    if (r.next_run_time) sched.appendChild(el('div', 'odw-next', `Next run ${when(r.next_run_time)}`))
    tr.appendChild(sched)
    const last = (r.last_run || null) as Row | null
    const cell = el('td')
    if (last) {
      const pill = el('span', 'odw-pill', word(last.status, RUN_WORDS))
      pill.dataset.s = str(last.status)
      const link = rowLink('run', str(last.id))
      link.appendChild(pill)
      cell.appendChild(link)
      if (last.completed_at) cell.appendChild(el('span', 'odw-muted', ` ${when(last.completed_at)}`))
    } else {
      cell.appendChild(el('span', 'odw-muted', 'Not run yet'))
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
  if (c.always) return 'Every turn'
  const parts: string[] = []
  if (list('kinds').length) parts.push(`writes ${list('kinds').join(', ')}`)
  if (list('events').length) parts.push(`on ${list('events').map((e) => (e.startsWith('tool:') ? e.slice(5) : e)).join(', ')}`)
  if (list('places').length) parts.push(`writes ${list('places').join(', ')}`)
  if (list('globs').length) parts.push(`under ${list('globs').join(', ')}`)
  if (list('commands').length) parts.push('on matching commands')
  if (!parts.length) return 'Any change'
  const text = parts.join(', ')
  return text.charAt(0).toUpperCase() + text.slice(1)
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
    name.appendChild(el('span', 'odw-title', str(r.name) || 'Untitled check'))
    const notes = [r.mandatory ? 'mandatory' : '', r.owner ? 'yours' : '', r.broken ? 'broken' : ''].filter(Boolean)
    if (notes.length) name.appendChild(el('span', 'odw-muted', `, ${notes.join(', ')}`))
    tr.appendChild(name)
    tr.appendChild(el('td', 'odw-muted', runsWords(r.condition)))
    const last = (r.last_verdict || null) as Row | null
    const cell = el('td')
    if (last) {
      const pill = el('span', 'odw-pill', word(str(last.status) || 'pass', VERDICT_WORDS))
      pill.dataset.s = str(last.status) || 'pass'
      if (last.chat_id || last.run_id) {
        const link = last.chat_id ? rowLink('chat', str(last.chat_id)) : rowLink('run', str(last.run_id))
        link.appendChild(pill)
        cell.appendChild(link)
      } else {
        cell.appendChild(pill)
      }
      if (typeof last.score === 'number') cell.appendChild(el('span', 'odw-muted', ` Score ${Math.round(last.score * 100)}%`))
      if (last.created_at) cell.appendChild(el('span', 'odw-muted', ` ${when(last.created_at)}`))
    } else {
      cell.appendChild(el('span', 'odw-muted', 'Not run yet'))
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
    const pill = el('span', 'odw-pill', word(str(r.severity) || 'info', SEVERITY_WORDS))
    pill.dataset.s = str(r.severity) || 'info'
    li.appendChild(pill)
    const text = el('span', 'odw-title', str(r.title))
    if (!r.read) text.style.fontWeight = '600'
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
    body.replaceChildren(empty('This widget is not ready. Open the page as an app, or wait a moment for it to load.'))
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
    body.replaceChildren(empty('This widget is not ready. Open the page as an app, or wait a moment for it to load.'))
    return { destroy: () => { alive = false } }
  }
  otodock.platform('integrations.status').then((res) => {
    if (!alive) return
    const providers = (((res as Row) || {}).providers || []) as Row[]
    const wanted = opts.provider ? providers.filter((p) => str(p.provider) === opts.provider) : providers
    body.replaceChildren()
    if (!wanted.length) {
      body.appendChild(empty(opts.provider ? `This agent does not use ${opts.provider}.` : 'Nothing to connect.'))
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
        : (agents ? `Connect a ${str(p.provider)} account for this agent`
          : identity === 'the owner' ? `${str(p.provider)}: the owner has not connected one`
            : `Connect ${str(p.provider)}`)
      // A connected account, or one only the owner can connect, is a line;
      // a dead button says nothing a line does not.
      if (p.connected || identity === 'the owner') {
        body.appendChild(el('div', 'odw-muted', label))
        continue
      }
      const btn = el('button', 'odw-btn odw-primary', label)
      btn.addEventListener('click', () => open(agents
        ? { kind: 'agent_settings', tab: 'mcps' }
        : { kind: 'user_settings', tab: 'integrations', provider: str(p.provider) }))
      body.appendChild(btn)
    }
  }, (err: unknown) => {
    if (alive) body.replaceChildren(empty(str((err as Error)?.message || err) || 'Not available right now.'))
  })
  return { destroy: () => { alive = false; el_.replaceChildren() } }
}

// ── Media ────────────────────────────────────────────────────────────────────
// A page shows a workspace video, audio file or picture through a short
// lease on an address (`files.media`). `mediaLink` keeps that lease for as
// long as the element is used and no longer: while it plays, or for a minute
// after the person last touched it in a tab that is showing. A lapsed lease
// is asked again on a touch, on play and on the element's error, and comes
// back on the same address, so a player's `src` changes at most once in half
// an hour (then at a pause, with its place kept).

const NOT_READY = 'This widget is not ready. Open the page as an app, or wait a moment for it to load.'
const GONE = 'This file is not available.'
const LOST = 'This file is no longer available to you.'
// What the host says when a call did not reach the platform or got no
// answer from it: a passing failure, never a refusal.
const PASSING = new Set(['network error', 'unavailable', NOT_READY])
const VIDEO_EXT = /\.(mp4|m4v|webm)$/i
const AUDIO_EXT = /\.(mp3|m4a|wav|ogg|oga|opus|flac|weba)$/i
const ICON: Record<string, string> = {
  play: '<svg viewBox="0 0 16 16"><path d="M4 2.5v11l9-5.5z"/></svg>',
  pause: '<svg viewBox="0 0 16 16"><path d="M3.5 2.5h3v11h-3zm6 0h3v11h-3z"/></svg>',
  sound: '<svg viewBox="0 0 16 16"><path d="M2 6v4h3l4 3V3L5 6zm9.5 2a3 3 0 0 0-1.5-2.6v5.2A3 3 0 0 0 11.5 8z"/></svg>',
  mute: '<svg viewBox="0 0 16 16"><path d="M2 6v4h3l4 3V3L5 6zm12.5-.1-.9-.9L12 6.6 10.4 5l-.9.9L11.1 7.5 9.5 9.1l.9.9L12 8.4l1.6 1.6.9-.9-1.6-1.6z"/></svg>',
}

interface Ask { path: string; tries: number; done: (info: MediaInfo | null, error: string, passing?: boolean) => void }
let asks: Ask[] = []

// Asks made in the same run of the page's code go out as one call (a list
// of thumbnails is one call, not twenty).
function flushAsks(): void {
  const batch = asks
  asks = []
  const platform = window.otodock?.platform
  for (let i = 0; i < batch.length; i += 32) {
    const part = batch.slice(i, i + 32)
    if (!platform) { part.forEach((a) => a.done(null, NOT_READY, true)); continue }
    const one = part.length === 1
    platform('files.media', one ? { path: part[0].path } : { paths: part.map((a) => a.path) }).then((res) => {
      const links = one ? [res as Row] : (((res as Row) || {}).links as Row[]) || []
      part.forEach((a, j) => {
        const l = links[j] || {}
        if (l.url) a.done(l as unknown as MediaInfo, '')
        else a.done(null, str(l.error) || GONE)
      })
    }, (err: unknown) => {
      // The platform paced the call and said how long to wait (the host
      // already waited once): the kit asks again then, three times at most.
      const wait = Number((err as { retry_after_ms?: number })?.retry_after_ms) || 0
      const again = wait > 0 ? part.filter((a) => a.tries < 3) : []
      const words = str((err as Error)?.message || err) || GONE
      part.filter((a) => !again.includes(a)).forEach((a) => a.done(null, words, wait > 0 || PASSING.has(words)))
      again.forEach((a) => { a.tries += 1 })
      if (again.length) window.setTimeout(() => again.forEach(queueAsk), Math.min(5000, wait) + 50)
    })
  }
}

function queueAsk(ask: Ask): void {
  asks.push(ask)
  if (asks.length === 1) Promise.resolve().then(flushAsks)
}

function askMedia(path: string): Promise<MediaInfo> {
  return new Promise((resolve, reject) => {
    queueAsk({
      path, tries: 0,
      done: (info, error, passing) => (info ? resolve(info) : reject(Object.assign(new Error(error), { passing: !!passing }))),
    })
  })
}

export function mediaLink(path: string): MediaLink {
  let el: HTMLMediaElement | HTMLImageElement | null = null
  let timer = 0
  let dead = false
  let touched = Date.now()
  let leaseEnd = 0
  let lastRepair = 0
  let waiting = false                // a new address waits for a pause
  let asking: Promise<MediaInfo> | null = null
  const offs: (() => void)[] = []
  const player = () => (el && 'play' in el ? el : null)

  const ask = (): Promise<MediaInfo> => {
    if (!asking) {
      asking = askMedia(path).then((info) => {
        asking = null
        if (!dead) adopt(info)
        return info
      }, (e: unknown) => { asking = null; throw e })
    }
    return asking
  }
  function adopt(info: MediaInfo): void {
    const changed = info.url !== link.url
    link.url = info.url
    link.kind = info.kind
    leaseEnd = Date.now() + info.expires_in * 1000
    schedule(Math.max(20, info.expires_in * 0.6) * 1000)
    if (changed) { link.onchange?.(info.url); place(false) }
  }
  function schedule(ms: number): void {
    window.clearTimeout(timer)
    timer = window.setTimeout(tick, ms)
  }
  // The lease is asked again only for a file in use; otherwise it is left
  // to run out, and `touch` asks when the person comes back.
  function tick(): void {
    timer = 0
    const m = player()
    const playing = m ? !m.paused && !m.ended : false
    if (!dead && (playing || (!document.hidden && Date.now() - touched < 60_000))) ask().catch(() => {})
  }
  function touch(): void {
    touched = Date.now()
    if (dead || !link.url) return
    const left = leaseEnd - Date.now()
    if (left < 20_000) ask().catch(() => {})
    else if (!timer) schedule(left - 20_000)
  }
  // Put the address on the element. A player that is playing keeps its old
  // address until a pause, a stall or an error; then it is swapped and its
  // time, speed and playing state are put back.
  function place(force: boolean): void {
    if (!el || !link.url) return
    const m = player()
    const same = el.getAttribute('src') === link.url
    if (!m) { if (!same || force) { el.removeAttribute('src'); el.src = link.url }; return }
    if (!el.getAttribute('src')) { m.src = link.url; return }
    if (same && !force) return
    if (!force && !m.paused && !m.error) { waiting = true; return }
    waiting = false
    const t = m.currentTime
    const speed = m.playbackRate
    const playing = !m.paused && !m.ended
    const back = () => {
      m.removeEventListener('loadedmetadata', back)
      try { m.currentTime = t } catch { /* not seekable yet */ }
      m.playbackRate = speed
      if (playing) m.play().catch(() => {})
    }
    m.addEventListener('loadedmetadata', back)
    m.src = link.url
    m.load()
  }
  function repair(): void {
    if (dead || Date.now() - lastRepair < 3000) return
    lastRepair = Date.now()
    // The element failed. A new link the platform refuses means the viewer
    // lost the file. A call that did not get through says nothing.
    ask().then(() => place(true), (e: unknown) => {
      if (!dead && !(e as { passing?: boolean })?.passing) link.onlost?.()
    })
  }

  const link: MediaLink = {
    url: '',
    kind: '',
    onchange: null,
    onlost: null,
    ready: Promise.resolve(null as unknown as MediaInfo),
    refresh: ask,
    attach(target) {
      el = target
      el.crossOrigin = 'anonymous'
      const on = (name: string, fn: () => void) => {
        target.addEventListener(name, fn)
        offs.push(() => target.removeEventListener(name, fn))
      }
      const m = player()
      if (m) {
        m.setAttribute('playsinline', '')
        m.setAttribute('disableremoteplayback', '')
        m.setAttribute('controlslist', 'nodownload noremoteplayback')
        on('play', touch)
        on('seeking', touch)
        on('pause', () => { if (waiting) place(true) })
        on('stalled', () => { if (waiting) place(true) })
      }
      on('pointerdown', touch)
      on('error', repair)
      place(false)
    },
    release() {
      dead = true
      window.clearTimeout(timer)
      offs.forEach((off) => off())
    },
  }
  link.ready = ask()
  link.ready.catch(() => {})
  return link
}

function clock(t: number): string {
  if (!Number.isFinite(t) || t < 0) t = 0
  const s = Math.floor(t % 60)
  return `${Math.floor(t / 60)}:${s < 10 ? '0' : ''}${s}`
}

function mediaWidget(el_: HTMLElement, opts: MountOptions): MediaHandle {
  const body = shell(el_, opts.title)
  const path = opts.path || ''
  const kind = VIDEO_EXT.test(path) ? 'video' : AUDIO_EXT.test(path) ? 'audio' : 'image'
  const box = el('div', 'odw-media')
  box.dataset.kind = kind
  box.dataset.fit = opts.fit || 'contain'
  const stage = el('div', 'odw-stage')
  box.appendChild(stage)
  body.appendChild(box)
  const listeners: Partial<Record<MediaEvent, ((time: number) => void)[]>> = {}
  const emit = (name: MediaEvent, time = 0) => (listeners[name] || []).forEach((cb) => { try { cb(time) } catch { /* the page's own */ } })
  const links: MediaLink[] = []
  let alive = true
  let raf = 0
  let loopAt: [number, number] | null = null
  const fail = (words: string) => {
    if (!alive) return
    stage.replaceChildren(el('div', 'odw-note', words))
    stage.style.display = 'block'
    box.dataset.gone = ''
    emit('error')
  }
  const open = (file: string, node: HTMLMediaElement | HTMLImageElement) => {
    const link = mediaLink(file)
    links.push(link)
    link.attach(node)
    link.onlost = () => fail(LOST)
    link.ready.catch((e: unknown) => fail(str((e as Error)?.message) || GONE))
    return link
  }
  const destroy = () => {
    alive = false
    window.cancelAnimationFrame(raf)
    links.forEach((l) => l.release())
    el_.replaceChildren()
  }
  const on = (name: MediaEvent, cb: (time: number) => void) => {
    (listeners[name] = listeners[name] || []).push(cb)
    return () => { listeners[name] = (listeners[name] || []).filter((x) => x !== cb) }
  }
  const idle = { play() {}, pause() {}, seek() {}, step() {}, loop() {}, rate: () => 1, currentTime: 0, duration: 0, paused: true }

  if (kind === 'image') {
    const img = el('img')
    img.alt = opts.label || path.split('/').pop() || ''
    img.addEventListener('load', () => emit('ready'))
    stage.appendChild(img)
    open(path, img)
    let over: HTMLImageElement | null = null
    let line: HTMLElement | null = null
    const split = (fraction: number) => {
      const f = Math.min(1, Math.max(0, fraction))
      if (over) over.style.clipPath = `inset(0 0 0 ${f * 100}%)`
      if (line) line.style.left = `${f * 100}%`
    }
    if (opts.compare) {
      over = el('img', 'odw-over')
      over.alt = ''
      line = el('div', 'odw-line')
      const drag = el('input', 'odw-split')
      drag.type = 'range'
      drag.min = '0'
      drag.max = '1000'
      drag.value = '500'
      drag.setAttribute('aria-label', 'Compare the two pictures')
      drag.addEventListener('input', () => split(Number(drag.value) / 1000))
      stage.append(over, line, drag)
      open(opts.compare, over)
      split(0.5)
    }
    return {
      ...idle, el: img, destroy, on, split,
      zoom: (z: number) => {
        const scale = `scale(${Math.min(8, Math.max(0.1, z))})`
        img.style.transform = scale
        if (over) over.style.transform = scale
      },
    }
  }

  const m = el(kind === 'video' ? 'video' : 'audio')
  m.preload = 'auto'
  m.muted = !!opts.muted
  m.autoplay = !!opts.autoplay
  stage.appendChild(m)
  const fps = opts.fps && opts.fps > 0 ? opts.fps : 30
  const bar = el('div', 'odw-bar')
  const playBtn = el('button')
  const seekBar = el('input', 'odw-seek')
  const time = el('span', 'odw-time', '0:00 / 0:00')
  const rateBtn = el('button', 'odw-rate', '1x')
  const muteBtn = el('button')
  seekBar.type = 'range'
  seekBar.min = '0'
  seekBar.max = '1000'
  seekBar.value = '0'
  seekBar.setAttribute('aria-label', 'Position')
  bar.append(playBtn, seekBar, time, rateBtn, muteBtn)
  if (opts.controls !== false) box.appendChild(bar)
  const paint = () => {
    playBtn.innerHTML = m.paused ? ICON.play : ICON.pause
    playBtn.setAttribute('aria-label', m.paused ? 'Play' : 'Pause')
    muteBtn.innerHTML = m.muted ? ICON.mute : ICON.sound
    muteBtn.setAttribute('aria-label', m.muted ? 'Sound on' : 'Sound off')
    rateBtn.textContent = `${m.playbackRate}x`
    rateBtn.setAttribute('aria-label', `Speed ${m.playbackRate}x`)
    const d = m.duration || 0
    time.textContent = `${clock(m.currentTime)} / ${clock(d)}`
    seekBar.setAttribute('aria-valuetext', `${clock(m.currentTime)} of ${clock(d)}`)
    if (d && document.activeElement !== seekBar) seekBar.value = String(Math.round((m.currentTime / d) * 1000))
  }
  const seek = (t: number) => {
    const d = m.duration || 0
    try { m.currentTime = Math.min(d || t, Math.max(0, t)) } catch { /* not loaded yet */ }
  }
  // While it plays, a `time` on every frame the screen draws, so a timeline
  // bar moves smoothly; a loop turns back at its end here too.
  const frame = () => {
    if (!alive || m.paused) return
    if (loopAt && m.currentTime >= loopAt[1]) seek(loopAt[0])
    paint()
    emit('time', m.currentTime)
    raf = window.requestAnimationFrame(frame)
  }
  m.addEventListener('play', () => { paint(); emit('play', m.currentTime); raf = window.requestAnimationFrame(frame) })
  m.addEventListener('pause', () => { paint(); emit('pause', m.currentTime) })
  m.addEventListener('ended', () => { paint(); emit('ended', m.currentTime) })
  m.addEventListener('loadedmetadata', () => { paint(); emit('ready', m.currentTime) })
  for (const name of ['seeked', 'timeupdate', 'ratechange', 'volumechange']) {
    m.addEventListener(name, () => { paint(); if (m.paused && name !== 'volumechange') emit('time', m.currentTime) })
  }
  playBtn.addEventListener('click', () => { if (m.paused) m.play().catch(() => {}); else m.pause() })
  muteBtn.addEventListener('click', () => { m.muted = !m.muted })
  rateBtn.addEventListener('click', () => { m.playbackRate = { 1: 1.5, 1.5: 2, 2: 0.5, 0.5: 1 }[m.playbackRate] || 1 })
  seekBar.addEventListener('input', () => seek((Number(seekBar.value) / 1000) * (m.duration || 0)))
  open(path, m)
  paint()
  return {
    el: m, destroy, on, seek, zoom() {}, split() {},
    play: () => { m.play().catch(() => {}) },
    pause: () => m.pause(),
    step: (frames: number) => { if (!m.paused) m.pause(); seek(m.currentTime + frames / fps) },
    loop: (start: number | null, end?: number) => { loopAt = start === null ? null : [start, end ?? m.duration ?? start] },
    rate: (r?: number) => { if (r && r > 0) m.playbackRate = r; return m.playbackRate },
    get currentTime() { return m.currentTime },
    get duration() { return m.duration || 0 },
    get paused() { return m.paused },
  }
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
    case 'media': return mediaWidget(target, opts)
    default: {
      const body = shell(target, opts.title)
      body.appendChild(empty(`Unknown widget ${String((opts as MountOptions).widget)}`))
      return { destroy: () => target.replaceChildren() }
    }
  }
}

// 2: the tasks table prints the feed's `schedule_text` and `next_run_time`
// (a page on 1 saw the raw cron in that column).
// 3: every pill says a word, the tables fold into blocks under 768 px, and a
// connected account is a line rather than a disabled button.
// 4: the media widget and `mediaLink` (a workspace video, audio file or
// picture inside the app's own layout, through `files.media`).
export const api: WidgetsApi = {
  version: 4,
  mount,
  mediaLink,
  render: {
    sessions: renderSessions, lanes: renderLanes, tasks: renderTasks, notifications: renderNotifications,
    checks: renderChecks,
  },
}

if (typeof window !== 'undefined') {
  window.otodockWidgets = api
}
