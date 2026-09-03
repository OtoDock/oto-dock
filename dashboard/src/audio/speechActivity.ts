// Speech-activity signal — "is agent TTS audibly playing right now?"
//
// The turn-end UI burst (message-list rebuild, markdown re-parse, query
// refetches, mini-app iframe reloads) runs on the same main thread that
// schedules TTS audio; the duplex player holds only ~250ms of lookahead, so
// a long task at exactly turn end is an audible gap. Heavy non-visible work
// routes through onIdle(): it runs SYNCHRONOUSLY when nothing is speaking
// (same-tick — callers' semantics are unchanged in the common case) and is
// queued until speech ends otherwise, with a hard cap so a wedged flag can
// never starve updates forever.
//
// Setters: useDuplexVoice flags its 'speaking' phase edges (phase-based on
// purpose — the player's drained() edge fires at every natural
// inter-segment silence and can hang after stop()); SoundIcon flags
// one-shot replay around backend.play().

type IdleEntry = { fn: () => void; cap: number }

let speakingCount = 0
const queue: IdleEntry[] = []

function runEntry(entry: IdleEntry) {
  window.clearTimeout(entry.cap)
  try { entry.fn() } catch { /* deferred work never breaks the flusher */ }
}

function flush() {
  while (queue.length > 0) runEntry(queue.shift()!)
}

/** Flag a speaking source on/off. Counted — duplex and a one-shot replay
 * can overlap; idle means ZERO active sources. */
export function setSpeaking(on: boolean): void {
  speakingCount = Math.max(0, speakingCount + (on ? 1 : -1))
  if (speakingCount === 0) flush()
}

export function isSpeaking(): boolean {
  return speakingCount > 0
}

/** Run `fn` now when idle (synchronously), else after speech ends or capMs
 * elapses — whichever comes first. */
export function onIdle(fn: () => void, capMs = 10_000): void {
  if (speakingCount === 0) {
    fn()
    return
  }
  const entry: IdleEntry = { fn, cap: 0 }
  entry.cap = window.setTimeout(() => {
    const i = queue.indexOf(entry)
    if (i >= 0) queue.splice(i, 1)
    try { entry.fn() } catch { /* see runEntry */ }
  }, capMs)
  queue.push(entry)
}

// ─── Dictation interlocks (1.5) ─────────────────────────────────────────────
// Dictation now captures WITHOUT echo cancellation (the hardware AEC flips
// Android into in-call volume — see backends/platformStt.ts), so any TTS
// playing during a dictation session would transcribe itself into the
// composer. Two interlocks close the real overlap windows:
//   * stopActivePlayback() — a dictation start silences in-flight one-shot
//     replays (SoundIcon registers its stopper around each play);
//   * isDictationActive() — the turn-complete chime suppresses while the
//     user dictates (AgentChat mirrors its existing duplex suppression).

const stoppables = new Set<() => void>()

/** Register a stopper for an interruptible playback. Returns the
 * unregister function (call it when the playback ends on its own). */
export function registerStoppablePlayback(stop: () => void): () => void {
  stoppables.add(stop)
  return () => stoppables.delete(stop)
}

/** Silence every registered playback (dictation start). Stoppers must be
 * idempotent; failures never break the caller. */
export function stopActivePlayback(): void {
  for (const s of Array.from(stoppables)) {
    try { s() } catch { /* stopper never breaks dictation */ }
  }
  stoppables.clear()
}

let dictationActive = false

/** Flagged by useSpeechSession around the whole dictation session. */
export function setDictationActive(on: boolean): void {
  dictationActive = on
}

export function isDictationActive(): boolean {
  return dictationActive
}

/** Test hook: hard-reset the module state between cases. */
export function _resetSpeechActivity(): void {
  speakingCount = 0
  queue.splice(0).forEach((e) => window.clearTimeout(e.cap))
  stoppables.clear()
  dictationActive = false
}
