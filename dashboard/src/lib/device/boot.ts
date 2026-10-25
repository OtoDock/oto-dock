// The boot screen's end: the page's first real screen is on. The overlay
// itself is boot.js's (painted before any module runs); this side tells the
// app, lets the overlay go and runs what waited for it (the native theme).

import { callNative } from '../nativeBridge'

declare global {
  interface Window {
    __otodockBoot?: { done: boolean; finish(): void }
  }
}

/** A placeholder that is not yet a real screen carries this attribute: the
 *  boot screen stays until the first commit without one. */
export const BOOT_WAIT_ATTR = 'data-boot-wait'

const BOOT_DONE_EVENT = 'otodock:bootdone'
const SHOWS_SOMETHING = 'svg, img, canvas, video, iframe, input, button'

let finished = false

/** The first real screen is painted. Idempotent. */
export function finishBoot(): void {
  if (finished) return
  finished = true
  callNative('firstScreen')
  window.__otodockBoot?.finish()
}

/** Whether a boot screen is up: boot.js ran and has not let it go. */
function bootUp(): boolean {
  const boot = window.__otodockBoot
  return boot !== undefined && !boot.done
}

/** Run `fn` once the boot screen is leaving (at once when none is up). */
export function onBootDone(fn: () => void): void {
  if (!bootUp()) {
    fn()
    return
  }
  document.addEventListener(BOOT_DONE_EVENT, () => fn(), { once: true })
}

/** A tree that is a screen: no placeholder waiting, and something to see. */
export function isScreen(root: Element): boolean {
  if (root.querySelector(`[${BOOT_WAIT_ATTR}]`)) return false
  if (root.querySelector(SHOWS_SOMETHING)) return true
  return (root.textContent ?? '').trim().length > 0
}

// A page that renders no screen within this of its script's start lets the
// boot go rather than hide the failure (the shell's own watchdog is longer,
// for a script that never runs at all).
const FIRST_SCREEN_WAIT_MS = 10_000

/** End the boot at the first commit under `root` that is a screen, or when
 *  none comes within FIRST_SCREEN_WAIT_MS. */
export function watchFirstScreen(root: Element): void {
  const observer = new MutationObserver(() => {
    if (!isScreen(root)) return
    observer.disconnect()
    finishBoot()
  })
  observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: [BOOT_WAIT_ATTR] })
  setTimeout(() => {
    observer.disconnect()
    finishBoot()
  }, FIRST_SCREEN_WAIT_MS)
}
