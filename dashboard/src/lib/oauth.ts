/**
 * OAuth utility — handles popups on web, deep links on native Android.
 *
 * On Android (Capacitor), OAuth flows that need a callback use deep links:
 * the system browser redirects to otodock://... which Android routes back
 * to the app. For flows without a redirect (Claude OAuth code-paste), Chrome
 * Custom Tab is used instead.
 */

import { Capacitor } from '@capacitor/core'
import { hasNativeBridge, openNativeBrowser } from './nativeBridge'

// ---------------------------------------------------------------------------
// Deep link callback system (native only)
// ---------------------------------------------------------------------------

interface DeepLinkWaiter {
  promise: Promise<string>
  resolve: (url: string) => void
  reject: (err: Error) => void
  /** Start the timeout; a no-op once started or settled. */
  arm: (timeoutMs: number) => void
}

/** How long a flow's link may take once the browser opened (the server's state TTL). */
const DEEP_LINK_TIMEOUT_MS = 300_000

// The waiter the next otodock://oauth/* link resolves.
let pendingDeepLink: DeepLinkWaiter | null = null
// The waiter openOAuthWindow made for its caller, handed over by the caller's
// next waitForDeepLink() (settled or not).
let handedOver: DeepLinkWaiter | null = null

/** True while a deep-link callback is awaited — a page reload in that
 * window would drop the link silently (the resolver lives in memory). */
export function deepLinkPending(): boolean {
  return pendingDeepLink !== null
}

/** Called from Android native via evaluateJavascript when an otodock://oauth/* deep link fires. */
;(window as any)._handleDeepLink = (url: string) => {
  pendingDeepLink?.resolve(url)
}

/** A waiter for the next link; untimed until armed when `timeoutMs` is null
 * (it counts for deepLinkPending either way). */
function createWaiter(timeoutMs: number | null): DeepLinkWaiter {
  let resolvePromise!: (url: string) => void
  let rejectPromise!: (err: Error) => void
  const promise = new Promise<string>((res, rej) => { resolvePromise = res; rejectPromise = rej })
  // Handled here so a decline settled before the caller awaits is never reported
  // as unhandled; the caller's own await still receives it.
  promise.catch(() => {})
  let timer: ReturnType<typeof setTimeout> | undefined
  let settled = false
  const settle = () => {
    settled = true
    clearTimeout(timer)
    if (pendingDeepLink === waiter) pendingDeepLink = null
  }
  const waiter: DeepLinkWaiter = {
    promise,
    resolve: (url) => { settle(); resolvePromise(url) },
    reject: (err) => { settle(); rejectPromise(err) },
    arm: (ms) => {
      if (settled || timer !== undefined) return
      timer = setTimeout(() => waiter.reject(new Error('OAuth timeout: no response from browser')), ms)
    },
  }
  pendingDeepLink?.reject(new Error('Superseded by a newer sign-in'))
  pendingDeepLink = waiter
  if (timeoutMs !== null) waiter.arm(timeoutMs)
  return waiter
}

/**
 * Wait for a deep link callback from Android.
 * Returns the full callback URL (e.g., "otodock://oauth/google?code=X&state=Y").
 * Rejects after timeout (default 5 min, matching server state TTL), and at once
 * when the person declined the app's confirm for the flow just opened.
 */
export function waitForDeepLink(timeoutMs = DEEP_LINK_TIMEOUT_MS): Promise<string> {
  const mine = handedOver
  handedOver = null
  return mine ? mine.promise : createWaiter(timeoutMs).promise
}

// ---------------------------------------------------------------------------
// OAuth window opener
// ---------------------------------------------------------------------------

/**
 * Open an OAuth authorization URL in the appropriate context.
 *
 * @param url - The OAuth authorization URL to open
 * @param name - Window name (used for web popups)
 * @param onBrowserClosed - Callback fired when the in-app browser is closed (native, non-deep-link only).
 * @param options.useDeepLink - If true, opens system browser (for deep link redirect flows).
 *                              If false/omitted, uses Chrome Custom Tab (for code-paste flows).
 * @returns true if opened successfully
 */
export async function openOAuthWindow(
  url: string,
  name: string,
  onBrowserClosed?: () => void,
  options?: { useDeepLink?: boolean },
): Promise<boolean> {
  if (Capacitor.isNativePlatform()) {
    if (options?.useDeepLink && hasNativeBridge()) {
      // Deep link mode: the app opens the system browser (after the person
      // confirms another site); the browser redirects to otodock://, which the
      // app routes back via onNewIntent → handleDeepLink. The waiter exists
      // before the ask, so a build reload waits while the confirm is open,
      // and its timeout starts only once the browser opened: the confirm may
      // stay open for NATIVE_BROWSER_TIMEOUT_MS. Every way out of the ask
      // either arms it or rejects it, so the reload is never held for good.
      const waiter = createWaiter(null)
      handedOver = waiter
      let opened = false
      try {
        opened = await openNativeBrowser(url, 'oauth')
      } catch { /* no answer: a cancel */ }
      if (opened) {
        waiter.arm(DEEP_LINK_TIMEOUT_MS)
        return true
      }
      waiter.reject(new Error('Sign-in cancelled'))
      return false
    }

    // Chrome Custom Tab mode (used for Claude OAuth code-paste flow)
    const { Browser } = await import('@capacitor/browser')
    await Browser.open({ url, presentationStyle: 'popover' })
    if (onBrowserClosed) {
      const listener = await Browser.addListener('browserFinished', () => {
        listener.remove()
        onBrowserClosed()
      })
    }
    return true
  }

  // Web: standard popup
  const popup = window.open(url, name, 'popup,width=500,height=700')
  return !!popup
}
