/**
 * OAuth utility — handles popups on web, deep links on native Android.
 *
 * On Android (Capacitor), OAuth flows that need a callback use deep links:
 * the system browser redirects to otodock://... which Android routes back
 * to the app. For flows without a redirect (Claude OAuth code-paste), Chrome
 * Custom Tab is used instead.
 */

import { Capacitor } from '@capacitor/core'

// ---------------------------------------------------------------------------
// Deep link callback system (native only)
// ---------------------------------------------------------------------------

type DeepLinkResolver = (url: string) => void
let pendingDeepLink: DeepLinkResolver | null = null

/** Called from Android native via evaluateJavascript when an otodock://oauth/* deep link fires. */
;(window as any)._handleDeepLink = (url: string) => {
  if (pendingDeepLink) {
    pendingDeepLink(url)
    pendingDeepLink = null
  }
}

/**
 * Wait for a deep link callback from Android.
 * Returns the full callback URL (e.g., "otodock://oauth/google?code=X&state=Y").
 * Rejects after timeout (default 5 min, matching server state TTL).
 */
export function waitForDeepLink(timeoutMs = 300_000): Promise<string> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingDeepLink = null
      reject(new Error('OAuth timeout — no response from browser'))
    }, timeoutMs)

    pendingDeepLink = (url: string) => {
      clearTimeout(timer)
      resolve(url)
    }
  })
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
    if (options?.useDeepLink) {
      // Deep link mode: open system browser via Android JS interface.
      // The browser will redirect to otodock:// which Android routes
      // back to the app via onNewIntent → handleDeepLink.
      const android = (window as any).Android
      if (android?.openAuthBrowser) {
        android.openAuthBrowser(url)
        return true
      }
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
