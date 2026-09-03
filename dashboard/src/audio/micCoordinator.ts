// Microphone ownership arbiter — module-scope, app-wide.
//
// Three features open getUserMedia: duplex voice, dictation (platform or
// native STT) and the wake-word listener. The first two are user-initiated
// and always win; the wake listener is ambient and must yield — on Android
// WebView holding a second stream open starves the native recognizer
// (micPermission.ts documents the never-reopen constraint), and an open
// wake stream during duplex would double-capture the room.
//
// Duplex and dictation register here while active; the wake listener
// subscribes and pauses whenever any owner is present.

type Listener = () => void

const owners = new Set<string>()
const listeners = new Set<Listener>()
let lastReleased = ''

function emit() {
  listeners.forEach((l) => {
    try { l() } catch { /* subscriber errors never break the arbiter */ }
  })
}

export function acquireMic(owner: string): void {
  if (owners.has(owner)) return
  owners.add(owner)
  emit()
}

export function releaseMic(owner: string): void {
  if (!owners.delete(owner)) return
  lastReleased = owner
  emit()
}

/** Who released the mic most recently — the wake listener sizes its resume
 * grace by this (native dictation's stop is an async bridge + finalize
 * window; the device mic frees well after releaseMic fires). */
export function lastReleasedMicOwner(): string {
  return lastReleased
}

export function micBusy(): boolean {
  return owners.size > 0
}

export function subscribeMic(cb: Listener): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}
