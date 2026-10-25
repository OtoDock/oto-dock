// The dashboard's console writer, the one module that calls `console`
// (eslint.config.mjs exempts this file from no-console).
//
// Breadcrumbs print only while debugging is on: a development build, or the
// tab's `otodock.debug` flag, which `?debug=1` on any page sets and
// `?debug=0` clears (read when this module loads; it survives a navigation
// and a reload, not the tab). The flag only adds console lines, so unlike the
// wake recorder's switch any page may set it. The Android app has no address
// bar: `sessionStorage.setItem('otodock.debug', '1')` from chrome://inspect,
// then a reload.

const KEY = 'otodock.debug'

function readParam(): void {
  try {
    const v = new URLSearchParams(window.location.search).get('debug')
    if (v === '1') sessionStorage.setItem(KEY, '1')
    else if (v === '0') sessionStorage.removeItem(KEY)
  } catch {
    // No window or no storage: the flag stays as it was.
  }
}
readParam()

export function isDebugOn(): boolean {
  // The share host and the widget kit are built without vite's env object.
  if (import.meta.env?.DEV) return true
  try {
    return sessionStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

/** A breadcrumb, printed while debugging is on. */
export function debugLog(...args: unknown[]): void {
  if (isDebugOn()) console.log(...args)
}

/** A warning breadcrumb, printed while debugging is on. */
export function debugWarn(...args: unknown[]): void {
  if (isDebugOn()) console.warn(...args)
}

/** Always printed: a failure that must never pass unseen (a socket frame
 *  whose handler threw is lost either way; losing it silently turns a bug
 *  into "the chat froze"). Keep the callers few. */
export function logError(...args: unknown[]): void {
  console.error(...args)
}
