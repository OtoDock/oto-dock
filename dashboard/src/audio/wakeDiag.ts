// Wake-word diagnostics recorder — module-scope store (the micCoordinator
// shape: subscribe + snapshot; a hook feeds it, a badge renders it).
//
// OFF by default. Switched on by the "Record diagnostics" button under the
// closed "Troubleshooting" disclosure of the wake-word settings (the phone
// app has no address bar) or by the desktop shortcut `?wakeDiag=1`; both write one sessionStorage key, which is what
// survives the wake navigation (it replaces the search string) and a
// WebView reload. While on, the listener hands this store the exact
// 16 kHz frames it feeds the spotter (kept as the last 30 s in an Int16
// ring, ~1 MB) and an event log with timestamps; Save writes ONE local
// file (JSON with the audio embedded as base64 WAV) through a browser
// download or, in the Android app, the native Downloads bridge. Nothing is
// uploaded by this module — the privacy model of the feature holds: audio
// leaves the device only when the operator sends the saved file.

import { hasNativeBridge, saveNativeFile } from '../lib/nativeBridge'

const KEY = 'otodock.wakeDiag'
export const WAKE_DIAG_RATE = 16000
export const WAKE_DIAG_RING_SECONDS = 30

export interface WakeDiagSnapshot {
  on: boolean
  listening: boolean
  events: number
  seconds: number
}

interface DiagEvent {
  t_ms: number
  sample: number
  event: string
  data?: Record<string, unknown>
}

interface DiagContext {
  keywords?: string
  threshold?: number
  base?: string
}

type Listener = () => void
const listeners = new Set<Listener>()

let ring: Int16Array | null = null
let ringPos = 0
let ringFilled = 0
let totalWritten = 0
let events: DiagEvent[] = []
let context: DiagContext = {}
let listening = false
let t0 = 0
let lastSecond = -1
let snapshot: WakeDiagSnapshot = { on: false, listening: false, events: 0, seconds: 0 }

function readKey(): boolean {
  try { return sessionStorage.getItem(KEY) === '1' } catch { return false }
}

function emit() {
  snapshot = {
    on: readKey(),
    listening,
    events: events.length,
    seconds: Math.min(WAKE_DIAG_RING_SECONDS, Math.floor(totalWritten / WAKE_DIAG_RATE)),
  }
  listeners.forEach((l) => {
    try { l() } catch { /* subscriber errors never break the store */ }
  })
}

export function isWakeDiagOn(): boolean {
  return readKey()
}

export function subscribeWakeDiag(cb: Listener): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

export function getWakeDiagSnapshot(): WakeDiagSnapshot {
  return snapshot
}

/** The ring exists exactly while the switch is on. Allocated lazily as well:
 * a reload (a new build, a WebView restart) boots this module with the key
 * already set, and the listener's first frame must find a recorder. */
function ensureRing(): boolean {
  if (ring) return true
  if (!readKey()) return false
  ring = new Int16Array(WAKE_DIAG_RING_SECONDS * WAKE_DIAG_RATE)
  t0 = performance.now()
  events = [{ t_ms: 0, sample: totalWritten, event: 'recorder on' }]
  emit()
  return true
}

/** Turn the recorder on/off. Off discards the buffer and the log. */
export function setWakeDiag(on: boolean): void {
  try {
    if (on) sessionStorage.setItem(KEY, '1')
    else sessionStorage.removeItem(KEY)
  } catch { /* storage unavailable → the recorder simply stays off */ }
  if (on && readKey()) {
    ensureRing()
  } else {
    ring = null
    ringPos = 0
    ringFilled = 0
    totalWritten = 0
    events = []
    lastSecond = -1
  }
  emit()
}

/** What the listener booted the spotter with (goes into the saved file). */
export function wakeDiagContext(ctx: DiagContext): void {
  context = { ...context, ...ctx }
}

export function wakeDiagListening(on: boolean): void {
  if (listening === on) return
  listening = on
  if (ensureRing()) emit()
}

export function wakeDiagEvent(event: string, data?: Record<string, unknown>): void {
  if (!ensureRing()) return
  events.push({ t_ms: Math.round(performance.now() - t0), sample: totalWritten, event, data })
  if (events.length > 5000) events.splice(0, events.length - 5000)
  emit()
}

/** The exact frames posted to the worker (call BEFORE the transferring
 * postMessage — the transfer detaches the buffer). */
export function wakeDiagFrames(samples: Float32Array): void {
  if (!ensureRing()) return
  const r = ring
  if (!r) return
  for (let i = 0; i < samples.length; i++) {
    const v = samples[i]
    r[ringPos] = v <= -1 ? -32768 : v >= 1 ? 32767 : (v * 32767) | 0
    ringPos = ringPos + 1 === r.length ? 0 : ringPos + 1
  }
  ringFilled = Math.min(r.length, ringFilled + samples.length)
  totalWritten += samples.length
  const second = Math.floor(totalWritten / WAKE_DIAG_RATE)
  if (second !== lastSecond) { lastSecond = second; emit() }
}

/** The ring in chronological order. */
export function wakeDiagPcm(): Int16Array {
  const r = ring
  if (!r) return new Int16Array(0)
  if (ringFilled < r.length) return r.slice(0, ringFilled)
  const out = new Int16Array(r.length)
  out.set(r.subarray(ringPos), 0)
  out.set(r.subarray(0, ringPos), r.length - ringPos)
  return out
}

export function encodeWav(pcm: Int16Array, rate = WAKE_DIAG_RATE): Uint8Array {
  const buf = new ArrayBuffer(44 + pcm.length * 2)
  const v = new DataView(buf)
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)) }
  str(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); str(8, 'WAVE')
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true)
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true)
  str(36, 'data'); v.setUint32(40, pcm.length * 2, true)
  new Int16Array(buf, 44).set(pcm)
  return new Uint8Array(buf)
}

function base64(bytes: Uint8Array): string {
  let s = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) {
    s += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CHUNK)))
  }
  return btoa(s)
}

/** The saved document: events aligned to the embedded audio (`wav_s` is the
 * offset inside the WAV; negative = before the 30 s window). */
export function exportWakeDiag(): { name: string; json: string } {
  const pcm = wakeDiagPcm()
  const wavStart = totalWritten - pcm.length
  const doc = {
    kind: 'otodock-wake-diag',
    version: 1,
    saved_at: new Date().toISOString(),
    ua: typeof navigator === 'undefined' ? '' : navigator.userAgent,
    android_app: hasNativeBridge(),
    rate: WAKE_DIAG_RATE,
    wav_seconds: Math.round((pcm.length / WAKE_DIAG_RATE) * 100) / 100,
    engine_base: context.base ?? null,
    threshold: context.threshold ?? null,
    keywords: context.keywords ?? null,
    events: events.map((e) => ({
      t_ms: e.t_ms,
      wav_s: Math.round(((e.sample - wavStart) / WAKE_DIAG_RATE) * 100) / 100,
      event: e.event,
      ...(e.data ? { data: e.data } : {}),
    })),
    wav_base64: base64(encodeWav(pcm)),
  }
  const stamp = doc.saved_at.replace(/[:.]/g, '-')
  return { name: `wake-diag-${stamp}.json`, json: JSON.stringify(doc) }
}

/** Save to a LOCAL file: the Android app's Downloads bridge when present,
 * else a browser download. Returns a short status for the badge. */
export function saveWakeDiag(): string {
  if (!ensureRing()) return 'recorder is off'
  const { name, json } = exportWakeDiag()
  if (hasNativeBridge()) {
    if (!saveNativeFile(base64(new TextEncoder().encode(json)), name, 'application/json')) return 'save failed'
    wakeDiagEvent('saved', { name })
    return `saved ${name} to Downloads`
  }
  try {
    const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }))
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
    wakeDiagEvent('saved', { name })
    return `saved ${name}`
  } catch {
    return 'save failed'
  }
}

/** Test/reset hook: forget everything and read the key again. */
export function _resetWakeDiagForTests(): void {
  ring = null; ringPos = 0; ringFilled = 0; totalWritten = 0; events = []; context = {}
  listening = false; lastSecond = -1
  emit()
}
