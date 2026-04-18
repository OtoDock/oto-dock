// One-shot sticky Ctrl modifier for the interactive terminal. Mobile/remote
// keyboards have no Ctrl key, so the control bar arms this and the NEXT
// single-character keystroke from the device keyboard (xterm onData) is
// translated to its control byte (c → ^C) and the modifier disarms. Bar
// buttons (arrows/Esc/…) send through their own path and never consume it.
// Module-level singleton: one interactive terminal is mounted per page.

let armed = false
const listeners = new Set<(v: boolean) => void>()

export function isCtrlHeld(): boolean {
  return armed
}

export function setCtrlHold(v: boolean) {
  if (armed === v) return
  armed = v
  listeners.forEach((l) => l(v))
}

/** Subscribe to arm/disarm; returns the unsubscriber. */
export function subscribeCtrlHold(fn: (v: boolean) => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

/**
 * Apply the sticky Ctrl to one xterm onData chunk. Single characters map to
 * their control byte (letters via uppercase; Space → NUL, ? → DEL) and
 * disarm. Escape sequences pass through WITHOUT consuming: with the TUI's
 * mouse/focus tracking on, merely clicking into the terminal emits CSI
 * chunks through onData, and those must not eat the armed modifier before
 * the actual keystroke. Other multi-char chunks (paste) disarm untouched so
 * the modifier never sticks through bulk input.
 */
export function applyCtrlHold(d: string): string {
  if (!armed) return d
  if (d.startsWith('\x1b') && d.length > 1) return d
  setCtrlHold(false)
  if (d.length !== 1) return d
  if (d === ' ') return '\x00'
  if (d === '?') return '\x7f'
  const c = d.toUpperCase().charCodeAt(0)
  // '@'..'_' (64–95) is the classic Ctrl range: code & 0x1f.
  if (c >= 64 && c <= 95) return String.fromCharCode(c & 0x1f)
  return d
}
