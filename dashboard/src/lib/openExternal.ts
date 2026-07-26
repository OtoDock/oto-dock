/**
 * Open a URL outside the dashboard. On web that's a new tab; inside the
 * Android app the WebView's window.open is a dead end, so links open in a
 * Chrome Custom Tab exactly like the OAuth flows (lib/oauth.ts). Only http(s)
 * ever opens — terminal- and agent-surfaced URLs are untrusted text.
 */

import { Capacitor } from '@capacitor/core'

export async function openExternalUrl(url: string): Promise<'opened' | 'blocked'> {
  if (!/^https?:\/\//i.test(url)) return 'blocked'
  if (Capacitor.isNativePlatform()) {
    const { Browser } = await import('@capacitor/browser')
    await Browser.open({ url })
    return 'opened'
  }
  // A null return means the popup blocker ate it — callers that bridge for
  // sandboxed frames ack this back so the page can tell the user.
  const w = window.open(url, '_blank', 'noopener,noreferrer')
  return w ? 'opened' : 'blocked'
}

/**
 * Validate a URL bridged out of a sandboxed artifact/mini-app frame before
 * opening it. Absolute http(s) only, and NEVER same-origin: a same-origin
 * `/v1/...` link would open a cookie-carrying top-level GET to any proxy
 * route from agent-authored HTML. Returns the destination origin (the
 * consent chip shows the origin, not the full URL).
 */
export function validateBridgedUrl(
  raw: unknown,
): { url: string; origin: string } | { error: string } {
  if (typeof raw !== 'string' || !raw) return { error: 'invalid url' }
  let u: URL
  try { u = new URL(raw) } catch { return { error: 'invalid url' } }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') {
    return { error: 'only http(s) links can open' }
  }
  if (u.origin === window.location.origin) {
    return { error: 'platform links cannot be opened from generated content' }
  }
  return { url: u.href, origin: u.origin }
}

/**
 * True when a real user gesture is (still) active — the sandboxed child's
 * click propagates activation to the parent, and a page cannot forge it.
 * Browsers without the User Activation API pass (consent + burst guards
 * still hold there).
 */
export function hasUserActivation(): boolean {
  const ua = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation
  return ua ? ua.isActive : true
}

/**
 * WebLinksAddon handler for the interactive terminal. Desktop requires
 * Ctrl/Cmd+click — the CLIs run with mouse tracking on, so a plain click is
 * TUI input, not link activation. Native has no modifier keys; a plain tap
 * opens the link.
 */
export function openTerminalLink(event: MouseEvent, uri: string): void {
  if (!Capacitor.isNativePlatform() && !event.ctrlKey && !event.metaKey) return
  void openExternalUrl(uri)
}
