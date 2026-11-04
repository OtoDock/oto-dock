// The dictation recorder: every event of the browser's own speech
// recognizer (webkitSpeechRecognition) while the debug flag is on
// (`lib/debug/log.ts`: a development build, `?debug=1`, or the
// `otodock.debug` session flag), so a device whose dictation loses words can
// be read offline from one file. A module-scope store on the wakeDiag shape:
// the Web Speech session feeds it, the mic menu offers the save. The file
// holds the dictated text: it is written locally (a browser download, or the
// Android app's Downloads bridge) and nothing is uploaded.

import { isDebugOn } from '../lib/debug/log'
import { hasNativeBridge, saveNativeFile } from '../lib/nativeBridge'

/** The events kept over the tab's sessions; the oldest go first. */
export const STT_DIAG_MAX_EVENTS = 2000

export interface SttDiagEvent {
  t_ms: number
  event: string
  [key: string]: unknown
}

export interface SttDiagSession {
  id: number
  started_at: string
  engine: string
  lang: string
  continuous: boolean
  interim_results: boolean
  events: SttDiagEvent[]
}

export interface SttDiagRecorder {
  event(name: string, data?: Record<string, unknown>): void
}

type Listener = () => void
const listeners = new Set<Listener>()

let sessions: SttDiagSession[] = []
let total = 0
let nextId = 1

function emit() {
  listeners.forEach((l) => {
    try { l() } catch { /* subscriber errors never break the store */ }
  })
}

export function isSttDiagOn(): boolean {
  return isDebugOn()
}

export function subscribeSttDiag(cb: Listener): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** The events held right now (the menu shows the save while there are any). */
export function sttDiagCount(): number {
  return total
}

function trim() {
  while (total > STT_DIAG_MAX_EVENTS && sessions.length) {
    const first = sessions[0]
    const drop = Math.min(total - STT_DIAG_MAX_EVENTS, first.events.length)
    first.events.splice(0, drop)
    total -= drop
    if (!first.events.length) sessions.shift()
  }
}

/** A recorder for one recognizer session, or null while the flag is off. A
 * recorder call never throws: a logging failure must not reach the
 * dictation. */
export function openSttDiagSession(info: {
  engine: string; lang: string; continuous: boolean; interimResults: boolean
}): SttDiagRecorder | null {
  if (!isSttDiagOn()) return null
  const t0 = performance.now()
  const session: SttDiagSession = {
    id: nextId++,
    started_at: new Date().toISOString(),
    engine: info.engine,
    lang: info.lang,
    continuous: info.continuous,
    interim_results: info.interimResults,
    events: [],
  }
  sessions.push(session)
  return {
    event(name, data) {
      try {
        session.events.push({ t_ms: Math.round(performance.now() - t0), event: name, ...(data ?? {}) })
        total += 1
        trim()
        emit()
      } catch { /* never the dictation's problem */ }
    },
  }
}

/** The saved document: the sessions in order with their events. */
export function exportSttDiag(): { name: string; json: string } {
  const doc = {
    kind: 'otodock-dictation-log',
    version: 1,
    saved_at: new Date().toISOString(),
    ua: typeof navigator === 'undefined' ? '' : navigator.userAgent,
    platform: typeof navigator === 'undefined' ? '' : navigator.platform,
    android_app: hasNativeBridge(),
    sessions,
  }
  const stamp = doc.saved_at.replace(/[:.]/g, '-')
  return { name: `dictation-log-${stamp}.json`, json: JSON.stringify(doc) }
}

/** Save to a LOCAL file: the Android app's Downloads bridge when present
 * (the file is handed over; the app reports the write), else a browser
 * download. True when the file went out; false with nothing to save or a
 * save that failed. */
export function saveSttDiag(): boolean {
  if (!total) return false
  const { name, json } = exportSttDiag()
  if (hasNativeBridge()) {
    const bytes = new TextEncoder().encode(json)
    let s = ''
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + 0x8000)))
    }
    return saveNativeFile(btoa(s), name, 'application/json')
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
    return true
  } catch {
    return false
  }
}

/** Test/reset hook: forget every session. */
export function _resetSttDiagForTests(): void {
  sessions = []
  total = 0
  nextId = 1
  emit()
}
