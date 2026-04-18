/**
 * Open a URL outside the dashboard. On web that's a new tab; inside the
 * Android app the WebView's window.open is a dead end, so links open in a
 * Chrome Custom Tab exactly like the OAuth flows (lib/oauth.ts). Only http(s)
 * ever opens — terminal- and agent-surfaced URLs are untrusted text.
 */

import { Capacitor } from '@capacitor/core'

export async function openExternalUrl(url: string): Promise<void> {
  if (!/^https?:\/\//i.test(url)) return
  if (Capacitor.isNativePlatform()) {
    const { Browser } = await import('@capacitor/browser')
    await Browser.open({ url })
    return
  }
  window.open(url, '_blank', 'noopener,noreferrer')
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
