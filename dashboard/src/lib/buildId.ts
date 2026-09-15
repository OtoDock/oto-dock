// Reload once when the server serves a different build than this page.
//
// The build stamps `<meta name="otodock-build" content="<id>">` into
// index.html; the proxy reads that stamp from its dist and repeats it as
// `server_info` on the dashboard socket, inside every `pong`, and on
// /health. This module compares the server's id with the page's OWN stamp
// (never an id adopted from the server at first connect — a page loaded
// just before a deploy would adopt the new id and never reload) and
// reloads at most once per server id, guarded in sessionStorage so a stale
// intermediary that keeps serving the old page cannot loop the tab.
//
// The reload is deferred, never skipped, while the page holds work a
// reload would silently destroy: an OAuth deep-link wait, a live meeting
// or voice call (the native switch-busy hint), a chat upload in flight.
// Streaming is deliberately NOT in that list: a redeploy restarts the
// proxy and kills the turn anyway, and a reload mid-stream is the existing
// recovery path.

import { deepLinkPending } from './oauth'
import { isNativeSwitchBusy } from './nativeBridge'
import { chatUploadActive } from './chatUploadQueue'

export const BUILD_META_NAME = 'otodock-build'
const GUARD_KEY = 'oto-build-reloaded-for'

export type BuildCheck = 'unknown' | 'same' | 'reloading' | 'deferred' | 'stale'

let reloadImpl: () => void = () => window.location.reload()
let warnedStale = false
let deferredFor = ''

/** The id of the page that is actually running ('' on a dev server). */
export function pageBuildId(): string {
  try {
    return document.querySelector(`meta[name="${BUILD_META_NAME}"]`)?.getAttribute('content') ?? ''
  } catch {
    return ''
  }
}

/** Work a reload would destroy silently; re-checked on the next signal. */
export function canReloadNow(): boolean {
  return !deepLinkPending() && !isNativeSwitchBusy() && !chatUploadActive()
}

function guardedFor(): string {
  try { return sessionStorage.getItem(GUARD_KEY) ?? '' } catch { return '' }
}

function setGuard(id: string): void {
  try { sessionStorage.setItem(GUARD_KEY, id) } catch { /* no storage → reload anyway, once per page life */ }
}

/** Compare the server's build id with this page's; reload once if newer. */
export function noteServerBuild(serverId: unknown, ready: () => boolean = canReloadNow): BuildCheck {
  const server = typeof serverId === 'string' ? serverId : ''
  const page = pageBuildId()
  if (!server || !page) return 'unknown'
  if (server === page) return 'same'
  if (guardedFor() === server) {
    if (!warnedStale) {
      warnedStale = true
      console.warn(`[build] server build ${server} still differs from this page (${page}) after a reload — not reloading again`)
    }
    return 'stale'
  }
  if (!ready()) {
    if (deferredFor !== server) {
      deferredFor = server
      console.log(`[build] new build ${server} — reload deferred until the current work finishes`)
    }
    return 'deferred'
  }
  setGuard(server)
  console.log(`[build] new build ${server} (this page: ${page}) — reloading`)
  reloadImpl()
  return 'reloading'
}

/** Test seam: replace the reload and forget the once-only state. */
export function _setReloadForTests(fn: (() => void) | null): void {
  reloadImpl = fn ?? (() => window.location.reload())
  warnedStale = false
  deferredFor = ''
}
