/**
 * Thin wrappers over the Android (Capacitor) JS bridge used by the
 * multi-installation switcher. All no-ops off-native.
 *
 * - setNativeAuthInProgress: block an install switch (swipe / Settings) while an
 *   auth flow that holds in-WebView state is running (Claude code-paste, OpenAI
 *   device-code). Deep-link flows arm this natively in openAuthBrowser.
 * - setNativeSwitchBusy: hint that live work (a meeting/voice call, etc.) would be
 *   lost on a switch, so the switcher confirms first. LLM streaming is already
 *   reported via setStreaming.
 */

export function setNativeAuthInProgress(active: boolean) {
  try { (window as any).Android?.setAuthInProgress?.(active) } catch { /* not native */ }
}

let switchBusy = false

export function setNativeSwitchBusy(busy: boolean) {
  switchBusy = busy
  try { (window as any).Android?.setSwitchBusy?.(busy) } catch { /* not native */ }
}

/** The last switch-busy hint (a live meeting / voice call) — also what the
 * build-reload check defers on, native or not. */
export function isNativeSwitchBusy(): boolean {
  return switchBusy
}
