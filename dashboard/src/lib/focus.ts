// Viewer focus (APPS.md "Live apps"): what this tab is looking at,
// reported to the server so an agent's turn can carry one line while an app
// is on screen. Derived, not declared: every AppFrame registers its app
// while mounted (the only component that puts an app on screen, whichever
// host renders it: the overlay, the Dock, the full-screen page), and the
// page supplies the chat-or-home fallback for the moments no app is up.
// One module-level value per tab; the sender is debounced so a fast tab
// toggle or the Dock's pair of mounts produces a single frame, and the last
// selection always wins. The server keeps the value per connection and
// drops it with the connection, so a reconnect must send it again.

export type FocusSurface = 'chat' | 'home' | 'app'

export interface ViewerFocus {
  surface: FocusSurface
  app_id?: string
  chat_id?: string
}

interface MountedApp {
  id: string
  title: string
  slug: string
  /** A chat-scoped Dock pin: the anchor chat's own board wins over a
   *  project board mounted beside it. */
  chatScoped: boolean
  at: number
}

const DEBOUNCE_MS = 250
const TITLE_MAX = 120

type Sender = (frame: Record<string, unknown>) => void

let page: ViewerFocus = { surface: 'home' }
let mounted: MountedApp[] = []
let seq = 0
// The user's "agents know which app I am looking at" setting; the server
// applies it to its own turns, this copy applies it to the interactive rail
// the dashboard stamps itself. Absent means on.
let sharing = true
let sender: Sender | null = null
let timer: ReturnType<typeof setTimeout> | null = null
let lastSent = ''

function topApp(): MountedApp | null {
  const scoped = mounted.filter((m) => m.chatScoped)
  const pool = scoped.length ? scoped : mounted
  let top: MountedApp | null = null
  for (const m of pool) if (!top || m.at > top.at) top = m
  return top
}

/** What the tab shows now: the newest mounted app, else the page. */
export function currentFocus(): ViewerFocus {
  const top = topApp()
  if (top) return { surface: 'app', app_id: top.id, ...(page.chat_id ? { chat_id: page.chat_id } : {}) }
  return page
}

function frameOf(f: ViewerFocus): Record<string, unknown> {
  const out: Record<string, unknown> = { type: 'focus', surface: f.surface }
  if (f.app_id) out.app_id = f.app_id
  if (f.chat_id) out.chat_id = f.chat_id
  return out
}

function flush(): void {
  timer = null
  if (!sender) return
  const frame = frameOf(currentFocus())
  const key = JSON.stringify(frame)
  if (key === lastSent) return
  lastSent = key
  try { sender(frame) } catch { /* socket gone; resendFocus covers the reconnect */ }
}

function schedule(): void {
  if (timer) clearTimeout(timer)
  timer = setTimeout(flush, DEBOUNCE_MS)
}

/** Installed once by useDashboardWs; null while no socket is open. */
export function setFocusSender(s: Sender | null): void {
  sender = s
}

/** A closing socket lets go of its own sender only: a page's old socket can
 *  close after the next page's socket installed one. */
export function releaseFocusSender(s: Sender): void {
  if (sender === s) sender = null
}

/** The page-level fallback: the chat the page shows, or home. */
export function setPageFocus(f: ViewerFocus): void {
  page = f
  schedule()
}

export function appMounted(app: { id: string; title: string; slug: string }, chatScoped = false): void {
  mounted.push({ id: app.id, title: app.title, slug: app.slug, chatScoped, at: ++seq })
  schedule()
}

export function appUnmounted(id: string): void {
  for (let i = mounted.length - 1; i >= 0; i--) {
    if (mounted[i].id === id) { mounted.splice(i, 1); break }
  }
  schedule()
}

export function setFocusSharing(on: boolean): void {
  sharing = on
}

/** The prompt line, the same shape the server builds: the title flattened
 *  and unable to close the bracket, the slug as the handle. */
export function formatFocusLine(title: string, slug: string): string {
  const kept = Array.from(title || '')
    .filter((c) => c !== '[' && c !== ']' && (c.charCodeAt(0) >= 32 || c === '\t' || c === '\n' || c === '\r'))
    .join('')
  const clean = kept.split(/\s+/).filter(Boolean).join(' ').slice(0, TITLE_MAX).trim() || slug
  return `[The user is looking at the app "${clean}" (${slug}) right now.]`
}

/** The line an interactive send carries after the time stamp, or ''. */
export function focusPreludeLine(): string {
  if (!sharing) return ''
  const top = topApp()
  return top ? formatFocusLine(top.title, top.slug) : ''
}

/** After a (re)connect the server knows nothing: send the value now. */
export function resendFocus(): void {
  if (timer) { clearTimeout(timer); timer = null }
  lastSent = ''
  flush()
}

/** Tests only. */
export function _resetFocusForTests(): void {
  page = { surface: 'home' }
  mounted = []
  seq = 0
  sharing = true
  sender = null
  lastSent = ''
  if (timer) { clearTimeout(timer); timer = null }
}
