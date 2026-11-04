// Device STT: @capgo/capacitor-speech-recognition in the Android and iOS
// apps (a maintained, Capacitor-8-native fork of the community plugin), and
// the browser's own Web Speech recognizer (webkitSpeechRecognition)
// elsewhere: Chrome, Edge and Apple's WebKit (Safari, and every browser on
// iOS) expose it, Firefox does not. Which recognizer a dictation gets is the
// capability's and the resolver's rule (`audio/resolver.ts`), not this
// file's. On Android the plugin recognizes on the device where its
// on-device check answers true for the language (no server round-trip, no
// provider cost), else through the phone's online recognizer, which needs
// the network; it needs RECORD_AUDIO (declared in AndroidManifest) and an
// APK that bundles the plugin's native module. On
// iOS it drives Apple's recognizer (the microphone and speech recognition
// usage strings in the app's Info.plist); the plugin names its own failures
// by code and Apple's by their error domain and number.

import { type STTBackend, type STTSession, type STTCreateOptions, isNativePlatform, probeWithTimeout } from '../types'
import { ensureNativeMicPermission } from '../micPermission'
import {
  DICTATION_DEVICE_SILENT, DICTATION_DOWNLOADING, DICTATION_IDLE, DICTATION_LOST, DICTATION_NO_CONNECT, DICTATION_NO_LANGUAGE,
  DICTATION_NO_MIC, DICTATION_NO_START, DICTATION_NOT_ALLOWED, DICTATION_SERVICE_OFF, DICTATION_STOPPED,
} from './dictationWords'
import { openSttDiagSession, type SttDiagRecorder } from '../sttDiag'
import { debugLog, debugWarn } from '../../lib/debug/log'

// The recognizer's errors that mean the link went: Greek and many other
// languages need the network even for the device recognizer. Android's
// codes, then Apple's (the plugin writes an Apple error as its domain and
// number): 203 is the recognizer's retry, 1101 its lost service, and a URL
// error is the network itself.
const NETWORK_ERRORS = new Set([
  'NETWORK', 'NETWORK_TIMEOUT', 'SERVER', 'SERVER_DISCONNECTED',
  'KAFASSISTANTERRORDOMAIN_203', 'KAFASSISTANTERRORDOMAIN_1101',
])
const isLostLink = (code: string) => NETWORK_ERRORS.has(code) || code.startsWith('NSURLERRORDOMAIN_')

// Apple's recognizer ends of its own: no speech heard (1110) is a pause
// (onError reads it with the soft codes), the request cancelled by our stop
// (216) ends the session quietly.
const QUIET_ENDS = new Set(['KAFASSISTANTERRORDOMAIN_216'])

/** Which app the plugin runs in: `android`, `ios`, or `web` off the apps. */
function nativePlatform(): string {
  const cap = (window as unknown as { Capacitor?: { getPlatform?: () => string } }).Capacitor
  return cap?.getPlatform?.() ?? 'web'
}

// The two start failures said in the app's own words: where the permission
// lives on each system (an app's switches sit under Settings, Apps on iOS
// 18 and later), and what to do when the device cannot recognize.
const MIC_DENIED: Record<string, string> = {
  android: "Microphone access was denied. Allow it in your phone's Settings, Apps, OtoDock, Permissions.",
  ios: 'Microphone access was denied. Allow the microphone and speech recognition in Settings, Apps, OtoDock.',
}
const RECOGNIZER_OFF: Record<string, string> = {
  android: 'On-device speech recognition is unavailable here. Install/enable Google’s speech services, or set Dictation to “platform” in Audio settings.',
  ios: 'This device cannot recognize speech right now. Turn on Dictation in Settings, General, Keyboard, or set Dictation to Platform in User Settings, Audio.',
}
const micDenied = () => MIC_DENIED[nativePlatform()] ?? MIC_DENIED.android
const recognizerOff = () => RECOGNIZER_OFF[nativePlatform()] ?? RECOGNIZER_OFF.android

// The plugin's own failure codes (its iOS target names them; Android's
// recognizer reports numbers the plugin maps itself) in plain words, null
// for a quiet end, undefined for a code with no words of its own.
function deviceWords(code: string, heard: boolean): string | null | undefined {
  if (isLostLink(code)) return heard ? DICTATION_LOST : DICTATION_NO_CONNECT
  if (QUIET_ENDS.has(code)) return null
  switch (code) {
    case 'MICROPHONE_PERMISSION_DENIED': return micDenied()
    // Android's own codes for a language the recognizer does not serve (12
    // and 13), which the plugin passes unnamed.
    case 'UNSUPPORTED_LOCALE': case 'UNKNOWN_12': case 'UNKNOWN_13': return DICTATION_NO_LANGUAGE
    case 'RECOGNIZER_UNAVAILABLE': case 'MODERN_START_FAILED': return DICTATION_SERVICE_OFF
    // The audio session was busy or refused (a call, another app recording,
    // a route change), never a missing microphone: the plain start sentence,
    // which leaves the platform free to take over under Auto.
    case 'AUDIO_SESSION_FAILED': case 'AUDIO_ENGINE_START_FAILED': return DICTATION_NO_START
    default: return undefined
  }
}

// A start the plugin refused, in the same words: the plugin rejects with a
// sentence of its own (the iOS target's, before or beside its error event).
function startWords(message: string): string {
  const m = message.toLowerCase()
  if (m.includes('permission') || m.includes('denied')) return micDenied()
  if (m.includes('already running')) return DICTATION_NO_START
  if (m.includes('unsupported locale')) return DICTATION_NO_LANGUAGE
  if (m.includes('unavailable')) return DICTATION_SERVICE_OFF
  if (m.includes('audio')) return DICTATION_NO_START
  return message || DICTATION_NO_START
}

// ── Browser (Web Speech) ──────────────────────────────────────────

interface SpeechResultLike {
  isFinal: boolean
  0: { transcript: string; confidence?: number }
}

interface SpeechRecognitionLike {
  lang: string
  continuous: boolean
  interimResults: boolean
  start(): void
  stop(): void
  abort(): void
  onresult: ((e: { resultIndex: number; results: SpeechResultLike[] }) => void) | null
  onerror: ((e: { error: string; message?: string }) => void) | null
  onend: (() => void) | null
  // The recorder's events (handlers set only while the debug flag is on).
  onstart?: (() => void) | null
  onaudiostart?: (() => void) | null
  onaudioend?: (() => void) | null
  onsoundstart?: (() => void) | null
  onsoundend?: (() => void) | null
  onspeechstart?: (() => void) | null
  onspeechend?: (() => void) | null
  onnomatch?: (() => void) | null
}

const RECORDED_MARKS = [
  'onstart', 'onaudiostart', 'onaudioend', 'onsoundstart', 'onsoundend', 'onspeechstart', 'onspeechend', 'onnomatch',
] as const

function getRecognitionCtor(): (new () => SpeechRecognitionLike) | null {
  const w = window as unknown as {
    webkitSpeechRecognition?: new () => SpeechRecognitionLike
    SpeechRecognition?: new () => SpeechRecognitionLike
  }
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

// The recognizer's own pause ends it (`end` with no error, or `no-speech`
// then `end`); while the person still holds the mic a fresh one starts after
// this delay (WebKit refuses a synchronous restart). Two ends in a row within
// the quick bound with no result, or no result at all for the idle bound,
// end the dictation with a sentence instead. A stop waits for the
// recognizer's `end` (its last final comes first) at most the stop bound.
export const WEB_SPEECH_RESTART_DELAY_MS = 150
export const WEB_SPEECH_QUICK_END_MS = 1500
export const WEB_SPEECH_IDLE_END_MS = 60_000
export const WEB_SPEECH_STOP_WAIT_MS = 1500

type WebSpeechEnd = 'stopped' | 'silence' | 'error' | 'idle' | 'quick-ends'
// The recognizer's own code for a recognition cut short (our stop, or the
// browser taking the microphone away); not a wire frame.
const WEB_SPEECH_ABORTED = 'aborted'

// The recognizer's error codes in plain words; null for a silence end
// (`no-speech`), which restarts while the mic is held. A `not-allowed` on a
// restarted recognizer (no gesture behind it) is a plain stop.
function webSpeechWords(code: string, heard: boolean, restarted: boolean): string | null {
  switch (code) {
    case 'no-speech': return null
    case 'network': return heard ? DICTATION_LOST : DICTATION_NO_CONNECT
    case 'not-allowed': return restarted ? DICTATION_STOPPED : DICTATION_NOT_ALLOWED
    case 'service-not-allowed': return DICTATION_SERVICE_OFF
    case 'audio-capture': return DICTATION_NO_MIC
    case 'language-not-supported': return DICTATION_NO_LANGUAGE
    default: return DICTATION_STOPPED
  }
}

const joinWords = (parts: Iterable<string>) => Array.from(parts).filter(Boolean).join(' ')

// `text` without a leading copy of `lead` (a recognizer whose every result
// repeats what it already finalised).
function stripLead(text: string, lead: string): string {
  if (!lead || !text.startsWith(lead)) return text
  const rest = text.slice(lead.length)
  return rest && !/^\s/.test(rest) ? text : rest.trim()
}

// One recognizer's results list, read event by event. Web Speech keeps every
// result of the session in `results` (finals, then the interim entries), and
// Chrome and WebKit never revise a final nor start the list over within one
// recognizer; the other shapes here (a verbatim re-send, a final revised in
// place, the finalised text repeated inside a later final, a list started
// over at 0) are defensive, for an engine the recorder has not shown yet.
// Finals are keyed by index AND text: a verbatim re-send is skipped, a
// revision rewrites every word the session delivered through `replace` (the
// hook replaces the words the attempt placed, so the text must carry them
// all: the earlier recognizers', the entries a new list dropped, the interims
// kept as finals), a new list clears what it replaced, and an interim the
// recognizer leaves behind is kept as a final.
class WebSpeechResults {
  private raw = new Map<number, string>()   // the recognizer's text per final index
  private kept = new Map<number, string>()  // the words delivered for it
  // The words this recognizer delivered that its list no longer holds: the
  // entries a new list dropped, and the interims kept as finals.
  private settled = ''
  interim = ''                              // the last interim join no final covered

  private keptInOrder(): string[] {
    return Array.from(this.kept.entries()).sort((a, b) => a[0] - b[0]).map(([, v]) => v)
  }

  private deliveredBefore(index: number): string {
    return joinWords(Array.from(this.kept.entries()).filter(([k]) => k < index).sort((a, b) => a[0] - b[0]).map(([, v]) => v))
  }

  /** Every word this recognizer delivered, in order: what a revision rewrites. */
  joined(): string {
    return joinWords([this.settled, ...this.keptInOrder()])
  }

  /** The orphaned interim, promoted: the caller delivers it as a final. */
  takeOrphan(): string {
    const t = this.interim
    this.interim = ''
    if (t) this.settled = joinWords([this.settled, t])
    return t
  }

  read(results: SpeechResultLike[]): { finals: string[]; replace: string | null; interim: string } {
    const finals: string[] = []
    let replace = false
    // A non-final at a held index, or a list shorter than a held index: the
    // recognizer started a new list. The old interim is orphaned unless the
    // new list begins with it (the same utterance, re-sent).
    let rebuiltAt = -1
    for (const index of this.raw.keys()) {
      if (index >= results.length || !results[index].isFinal) rebuiltAt = rebuiltAt < 0 ? index : Math.min(rebuiltAt, index)
    }
    if (rebuiltAt >= 0) {
      const orphan = this.interim
      const dropped = Array.from(this.kept.entries()).filter(([k]) => k >= rebuiltAt).sort((a, b) => a[0] - b[0]).map(([, v]) => v)
      for (const index of Array.from(this.raw.keys())) {
        if (index >= rebuiltAt) { this.raw.delete(index); this.kept.delete(index) }
      }
      this.settled = joinWords([this.settled, ...dropped])
      const fresh = joinWords(Array.from(results, (r) => (r[0]?.transcript ?? '').trim()))
      if (orphan && !fresh.startsWith(orphan)) {
        finals.push(orphan)
        this.settled = joinWords([this.settled, orphan])
      }
      this.interim = ''
    }
    const interimParts: string[] = []
    for (let i = 0; i < results.length; i++) {
      const text = (results[i][0]?.transcript ?? '').trim()
      if (!results[i].isFinal) { interimParts.push(text); continue }
      const before = this.raw.get(i)
      if (before === undefined) {
        const words = stripLead(text, this.deliveredBefore(i))
        this.raw.set(i, text)
        this.kept.set(i, words)
        if (words) finals.push(words)
        this.interim = ''
      } else if (before !== text) {
        this.raw.set(i, text)
        this.kept.set(i, stripLead(text, this.deliveredBefore(i)))
        replace = true
      }
    }
    const interim = stripLead(joinWords(interimParts), joinWords(this.keptInOrder()))
    if (interim) this.interim = interim
    return { finals, replace: replace ? this.joined() : null, interim }
  }
}

function webSpeechSession(opts: STTCreateOptions): STTSession {
  const Ctor = getRecognitionCtor()
  const h = {
    partial: (_: string) => {}, final: (_: string) => {}, replace: (_: string) => {},
    error: (_: Error) => {}, end: (_reason?: string) => {},
  }
  let recog: SpeechRecognitionLike | null = null   // the live recognizer
  let results: WebSpeechResults | null = null      // its list (a stop past its bound promotes the interim)
  let diag: SttDiagRecorder | null = null
  let held = false        // start() until stop(): a pause restarts
  let ended = false       // h.end delivered once
  let ending: WebSpeechEnd | null = null   // a fatal under way: its reason, whatever ends the session
  let heard = false       // a result landed in this session, any recognizer
  let restarted = false
  let earlier = ''        // every word the recognizers before the live one delivered
  let lastResultAt = 0
  let startedAt = 0
  let quickEnds = 0
  let restartTimer: ReturnType<typeof setTimeout> | null = null
  let stopWaiters: (() => void)[] = []

  const finish = (why: WebSpeechEnd) => {
    if (ended) return
    const reason = ending ?? why
    ended = true
    held = false
    recog = null
    results = null
    if (restartTimer) { clearTimeout(restartTimer); restartTimer = null }
    diag?.event('session_end', { reason })
    debugLog('[stt] device dictation ended:', reason)
    const waiters = stopWaiters
    stopWaiters = []
    waiters.forEach((w) => w())
    h.end(reason)
  }
  // Fatal: the sentence, then the end. `held` goes first so the hook's idle
  // state never leaves a recognizer that would restart; the reason is set
  // first so a stop() the error handler calls (the hook's fallback) ends the
  // session under it, not as a stop.
  const fatal = (message: string, reason: WebSpeechEnd = 'error') => {
    ending = reason
    held = false
    recog = null
    h.error(new Error(message))
    finish(reason)
  }

  const spawn = (ready?: { resolve: () => void; reject: (e: Error) => void }) => {
    if (!Ctor) throw new Error('No native speech recognition')
    const r = new Ctor()
    recog = r
    r.lang = opts.language
    r.continuous = true
    r.interimResults = true
    const own = new WebSpeechResults()
    results = own
    const spawnedAt = performance.now()
    let sawResult = false
    let isReady = !ready
    const mine = () => recog === r
    const markReady = () => { if (!isReady) { isReady = true; ready?.resolve() } }
    const recorder = diag
    if (recorder) {
      for (const mark of RECORDED_MARKS) r[mark] = () => recorder.event(mark.slice(2))
    }
    r.onstart = () => { recorder?.event('start'); if (mine()) markReady() }
    r.onaudiostart = () => { recorder?.event('audiostart'); if (mine()) markReady() }
    r.onresult = (e) => {
      if (recorder) {
        recorder.event('result', {
          resultIndex: e.resultIndex,
          length: e.results.length,
          results: Array.from({ length: e.results.length }, (_, i) => ({
            isFinal: e.results[i].isFinal, text: e.results[i][0]?.transcript ?? '', confidence: e.results[i][0]?.confidence ?? null,
          })),
        })
      }
      if (!mine()) return
      markReady()
      heard = true
      sawResult = true
      lastResultAt = performance.now()
      const out = own.read(Array.from({ length: e.results.length }, (_, i) => e.results[i]))
      if (out.replace !== null) h.replace(joinWords([earlier, out.replace]))
      for (const t of out.finals) h.final(t)
      if (out.interim) h.partial(out.interim)
    }
    r.onerror = (e) => {
      recorder?.event('error', { error: e.error, message: e.message ?? null })
      if (!mine()) return
      const code = e.error || ''
      if (!isReady) {
        // Before the recognizer started: the start fails, nothing is said
        // here (the hook's start path says it, and may fall back).
        recog = null
        isReady = true
        ready?.reject(new Error(webSpeechWords(code, false, false) ?? DICTATION_STOPPED))
        return
      }
      if (code === WEB_SPEECH_ABORTED && !held) return   // our stop: the end follows
      const words = code === WEB_SPEECH_ABORTED ? null : webSpeechWords(code, heard, restarted)
      if (words === null) return                 // a silence: the end that follows decides
      fatal(words)
    }
    r.onend = () => {
      recorder?.event('end')
      if (!mine()) return
      recog = null
      if (!isReady) {
        isReady = true
        ready?.reject(new Error(DICTATION_STOPPED))
        return
      }
      const orphan = own.takeOrphan()
      if (orphan) h.final(orphan)
      if (!held) { finish('stopped'); return }
      const now = performance.now()
      quickEnds = !sawResult && now - spawnedAt < WEB_SPEECH_QUICK_END_MS ? quickEnds + 1 : 0
      if (quickEnds >= 2) { fatal(DICTATION_STOPPED, 'quick-ends'); return }
      if (now - (lastResultAt || startedAt) >= WEB_SPEECH_IDLE_END_MS) { fatal(DICTATION_IDLE, 'idle'); return }
      if (typeof document !== 'undefined' && document.hidden) { finish('silence'); return }
      restartTimer = setTimeout(() => {
        restartTimer = null
        if (!held || ended) return
        restarted = true
        earlier = joinWords([earlier, own.joined()])
        recorder?.event('restart')
        try { spawn() } catch { finish('silence') }
      }, WEB_SPEECH_RESTART_DELAY_MS)
    }
    recorder?.event('start_called')
    r.start()
  }

  return {
    start() {
      diag = openSttDiagSession({ engine: 'web_speech', lang: opts.language, continuous: true, interimResults: true })
      held = true
      ended = false
      ending = null
      startedAt = performance.now()
      return new Promise<void>((resolve, reject) => {
        try { spawn({ resolve, reject }) } catch (e) { reject(e instanceof Error ? e : new Error(String(e))) }
      })
    },
    async stop() {
      diag?.event('stop_called')
      held = false
      if (restartTimer) { clearTimeout(restartTimer); restartTimer = null }
      const r = recog
      if (!r) { finish('stopped'); return }
      const gone = new Promise<void>((resolve) => { stopWaiters.push(resolve) })
      try { r.stop() } catch { finish('stopped'); return }
      await Promise.race([gone, new Promise<void>((resolve) => setTimeout(resolve, WEB_SPEECH_STOP_WAIT_MS))])
      if (ended) return
      // Past the bound: the words the recognizer still showed are kept (on
      // WebKit a recognizer's words stay interim until its task ends) and
      // the recognizer is cut, so it never keeps the microphone.
      const orphan = results?.takeOrphan()
      if (orphan) h.final(orphan)
      try { r.abort() } catch { /* ending anyway */ }
      finish('stopped')
    },
    onPartial(fn) { h.partial = fn },
    onFinal(fn) { h.final = fn },
    onReplace(fn) { h.replace = fn },
    onError(fn) { h.error = fn },
    onEnd(fn) { h.end = fn },
  }
}

// ── Native (Capacitor @capgo plugin) ──────────────────────────────
// Minimal interface over the bits we use — the dynamic import keeps the plugin
// out of the web bundle path.

interface PluginEvent {
  matches?: string[]
  accumulated?: string          // the phrases the plugin kept before the current one
  accumulatedText?: string      // every phrase, the current one included (a last result, a forced stop)
  isRestarting?: boolean        // the plugin keeps the session alive after this phrase
  forced?: boolean
  status?: string; state?: string; reason?: string; errorCode?: string; sessionId?: number
  code?: string; message?: string
}

interface CapgoSR {
  available(): Promise<{ available: boolean }>
  isOnDeviceRecognitionAvailable(opts?: { language?: string }): Promise<{ available: boolean }>
  checkPermissions(): Promise<{ speechRecognition: string }>
  requestPermissions(): Promise<{ speechRecognition: string }>
  getSupportedLanguages(): Promise<{ languages?: string[] }>
  start(opts: {
    language?: string; partialResults?: boolean; popup?: boolean; useOnDeviceRecognition?: boolean; continuousPTT?: boolean
  }): Promise<{ matches?: string[] } | void>
  stop(): Promise<void>
  setPTTState(opts: { held: boolean }): Promise<void>
  getLastPartialResult(): Promise<{ available: boolean; text: string; matches?: string[] }>
  removeAllListeners(): Promise<void>
  // The plugin the shell registers answers with the handle itself; the
  // module's proxy (the web build, the tests) with a promise of it.
  addListener(event: string, cb: (data: PluginEvent) => void): ListenerHandle | Promise<ListenerHandle>
}
interface ListenerHandle { remove: () => Promise<void> | void }

const listen = (sr: CapgoSR, event: string, cb: (data: PluginEvent) => void): Promise<ListenerHandle> =>
  Promise.resolve(sr.addListener(event, cb))
const unlisten = (x: ListenerHandle) => Promise.resolve().then(() => x.remove()).catch(() => {})

async function loadSR(): Promise<CapgoSR> {
  // The shell registers the plugin natively, so its proxy is on the global
  // already; the module import serves the web build and the tests. On an
  // Android 16 WebView that import never settled, so the probe's wait ran
  // out and the chat had no microphone.
  const cap = (window as unknown as { Capacitor?: { Plugins?: Record<string, unknown> } }).Capacitor
  const registered = cap?.Plugins?.SpeechRecognition
  if (registered) return registered as CapgoSR
  const mod = await import('@capgo/capacitor-speech-recognition')
  return (mod as unknown as { SpeechRecognition: CapgoSR }).SpeechRecognition
}

// Apple's recognizer takes a whole tag (`el-GR`), never a bare code, and
// spells the region itself: the device's list, read once per page, gives
// the tag for the language asked. A tag the device knows as written passes
// as written. For a bare language the device's own tag wins when it speaks
// that language, then the language in the device's region, then the
// language's likely region (en to US, de to DE, fr to FR, pt to BR), then
// the first the device lists: the list is alphabetical, so the first entry
// alone would hand a Greek iPad `en-AE` for English. A language the device
// does not know passes as asked, and the plugin names the refusal.
let supportedTags: Promise<string[]> | null = null

/** The region a bare language most likely means, from the locale data. */
function likelyRegion(base: string): string {
  try { return new Intl.Locale(base).maximize().region ?? '' } catch { return '' }
}

async function iosLanguageTag(sr: CapgoSR, language: string): Promise<string> {
  supportedTags ??= sr.getSupportedLanguages().then((r) => r.languages ?? [], () => [])
  const tags = await supportedTags
  const same = (a: string, b: string) => a.replace(/_/g, '-').toLowerCase() === b.replace(/_/g, '-').toLowerCase()
  const exact = tags.find((t) => same(t, language))
  if (exact) return exact
  const base = language.split(/[-_]/)[0].toLowerCase()
  const spoken = tags.filter((t) => t.split(/[-_]/)[0].toLowerCase() === base)
  if (spoken.length === 0) return language
  const device = typeof navigator === 'undefined' ? '' : navigator.language || ''
  const regions = [device.split(/[-_]/)[1] ?? '', likelyRegion(base)].filter(Boolean)
  const wanted = [device, ...regions.map((r) => `${base}-${r}`)]
  for (const tag of wanted) {
    const found = spoken.find((t) => same(t, tag))
    if (found) return found
  }
  return spoken[0]
}

// The plugin keeps one recognizer session alive across the recognizer's own
// ends (`continuousPTT` while the button is held): on each phrase it appends
// the result to its accumulated text, sends it as a restart payload and
// starts again; on a silence it appends the last partial with no event and
// starts again. Where a device ends the session anyway, this session starts
// the recognizer itself after the delay, as the Web Speech loop does, under
// the same guards: two ends within the quick bound with no result end the
// dictation with a sentence, no result at all for the idle bound too. A stop
// waits for the plugin's end (its last result comes first) at most the stop
// bound. A model the phone offers to download gets this bound before the
// online recognizer takes the same press.
export const DEVICE_RESTART_DELAY_MS = 150
export const DEVICE_QUICK_END_MS = 1500
export const DEVICE_IDLE_END_MS = 60_000
export const DEVICE_STOP_WAIT_MS = 2500
export const ON_DEVICE_CHECK_WAIT_MS = 1500
export const ON_DEVICE_START_WAIT_MS = 4000
// An online recognizer that sent neither a word nor a pause in a press this
// long is deaf to the language (a phone whose default recognizer connects
// and stays silent): the stop says so.
export const DEVICE_SILENT_AFTER_MS = 3000

type DeviceEnd = 'stopped' | 'silence' | 'error' | 'idle' | 'quick-ends'
type DevicePhase = 'starting' | 'listening' | 'ended'

// The recognizer's codes for a pause: the plugin restarts on them while the
// button is held, and ends with reason `silence` otherwise.
const SILENCE_CODES = new Set(['NO_MATCH', 'SPEECH_TIMEOUT'])
// The recognizer's codes for its own hiccup after a cancel or a restart: the
// plugin ends the session, and this one starts it again.
const SOFT_CODES = new Set(['CLIENT', 'RECOGNIZER_BUSY'])
// The plugin's answer to a start it refuses because one is already open.
const ALREADY_RUNNING = 'already running'

// One session owns the plugin at a time: the owner frees it at its end (the
// plugin's `stopped`, or the stop bound). A quick tap after Send never
// re-arms the ended session's restart or gets "already running".
let pluginFree: Promise<void> = Promise.resolve()
// The plugin session ids a live session owns, and the largest id seen: the
// plugin's ids only grow, so an event with a smaller id is a late one.
const owned = new Set<number>()
let lastSeenId = -1
let watcher: Promise<unknown> | null = null

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/** `whole` past `lead` at a word boundary: '' when equal, null when `whole`
 *  does not start with `lead` (the session and the plugin out of step). */
export function tailAfter(whole: string, lead: string): string | null {
  if (!lead) return whole
  if (whole === lead) return ''
  if (whole.startsWith(`${lead} `)) return whole.slice(lead.length + 1).trim()
  return null
}

// A `started` for a session id nobody owns is a recognizer running on its
// own (the plugin's late success of a model download it was told to stop
// starts whatever recognizer is current, with no session check): it is
// stopped when no session is live, so no microphone stays on unowned.
function ensureWatcher(sr: CapgoSR): void {
  watcher ??= listen(sr, 'listeningState', (d) => {
    const state = d.state ?? d.status
    if (state !== 'started' || typeof d.sessionId !== 'number') return
    if (owned.size === 0 && !owned.has(d.sessionId)) {
      debugLog('[stt] a recognizer started that no dictation owns, stopping it')
      void sr.stop().catch(() => {})
    }
  }).catch(() => null)
}

class StartTimeout extends Error {}

/** The plugin's held flag; an older build without the method is left be. */
const setHeld = (sr: CapgoSR, held: boolean) =>
  Promise.resolve().then(() => sr.setPTTState({ held })).catch(() => {})

function capacitorSession(opts: STTCreateOptions): STTSession {
  const h = {
    partial: (_: string) => {}, final: (_: string) => {}, error: (_: Error) => {},
    end: (_reason?: string) => {}, notice: (_: string | null) => {},
  }
  let sr: CapgoSR | null = null
  let diag: SttDiagRecorder | null = null
  let held = false            // start() until stop(): a pause restarts
  let phase: DevicePhase = 'starting'
  let stopping = false        // stop() called: an in-flight start abandons at its next checkpoint
  let ended = false
  let ending: DeviceEnd | null = null   // a fatal under way: its reason, whatever ends the session
  let finalizing: Promise<void> | null = null
  let id: number | null = null          // the plugin's session id, from its first event after our start call
  let starting = false                  // a plugin start call is pending: its first event names our id
  let open = false                      // the plugin session is open, as far as the page knows
  let pluginFinals = ''                 // the plugin's accumulated text, as delivered (reset per plugin session)
  let interim = ''                      // the current phrase shown, not yet final
  let heard = false                     // a result landed in this session, any plugin session
  let resultSinceStart = false
  let lastStartedAt = 0
  let quickEnds = 0
  let silenceEnd = false                // the coming `stopped` is a pause, not an error
  let idleTimer: ReturnType<typeof setTimeout> | null = null
  let restartTimer: ReturnType<typeof setTimeout> | null = null
  let handles: ListenerHandle[] = []
  let stoppedWaiters: (() => void)[] = []
  let freePlugin: (() => void) | null = null
  let startOpts: { language: string; partialResults: true; popup: false; continuousPTT: true } | null = null
  let onDevice = false
  let released = false        // the plugin's held flag was let go
  let listeningSince = 0      // the start resolved
  let sawSilence = false      // the recognizer reported a pause: it hears

  const now = () => performance.now()
  const clearTimers = () => {
    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null }
    if (restartTimer) { clearTimeout(restartTimer); restartTimer = null }
  }
  const armIdle = () => {
    if (idleTimer) clearTimeout(idleTimer)
    idleTimer = setTimeout(() => {
      idleTimer = null
      if (held && !ended) fatal(DICTATION_IDLE, 'idle')
    }, DEVICE_IDLE_END_MS)
  }
  const wakeStopped = () => { const w = stoppedWaiters; stoppedWaiters = []; w.forEach((r) => r()) }
  /** The plugin session's end (its `stopped`), or the bound. */
  const waitStopped = async (ms: number) => {
    if (!open) return
    await Promise.race([new Promise<void>((r) => { stoppedWaiters.push(r) }), sleep(ms)])
  }

  // `announce` false: a start the plugin refused cleans up and rejects,
  // and the hook reads the rejection (an end would read as a stop).
  const finalize = (why: DeviceEnd, announce = true): Promise<void> => {
    finalizing ??= (async () => {
      ended = true
      phase = 'ended'
      held = false
      const reason = ending ?? why
      clearTimers()
      if (sr) {
        if (!released) { released = true; void setHeld(sr, false) }
        if (open) { open = false; await sr.stop().catch(() => {}) }
        // The recognizer caches its last transcript: read it only when the
        // session heard nothing through its events (the legacy path).
        if (!heard && !interim) {
          const lp = await sr.getLastPartialResult().catch(() => null)
          if (lp?.available && lp.text) interim = lp.text
        }
      }
      const text = interim.trim(); interim = ''
      if (text) { heard = true; h.final(text) }
      const mine = handles; handles = []
      await Promise.all(mine.map(unlisten))
      if (id !== null) owned.delete(id)
      freePlugin?.(); freePlugin = null
      wakeStopped()
      diag?.event('session_end', { reason })
      debugLog('[stt] device dictation ended:', reason)
      if (announce) h.end(reason)
    })()
    return finalizing
  }
  // Fatal: the sentence, then the end. `held` goes first so nothing restarts;
  // the reason is set first so a stop() the error handler calls (the hook's
  // fallback) ends the session under it, not as a stop.
  const fatal = (message: string, reason: DeviceEnd = 'error') => {
    ending = reason
    held = false
    h.error(new Error(message))
    void finalize(reason)
  }

  const deliverFinal = (text: string) => {
    const t = text.trim()
    if (!t) return
    heard = true
    h.final(t)
  }
  // The plugin's text past what this session delivered; when the two are out
  // of step, the phrase the plugin names, else the interim it showed.
  const phraseFrom = (whole: string, named: string) => {
    if (!whole) return named || interim
    const tail = tailAfter(whole, pluginFinals)
    const phrase = tail === null ? named : tail
    return phrase || interim
  }

  // A page put away ends the dictation at the next pause or phrase, the
  // plugin's own restart included: a phone in a pocket must not keep
  // typing what it hears.
  const hidden = () => typeof document !== 'undefined' && document.hidden

  const onPartial = (d: PluginEvent) => {
    diag?.event('plugin_partial', { matches: d.matches?.length ?? 0, accumulated: d.accumulated ?? null,
      accumulatedText: d.accumulatedText ?? null, restarting: !!d.isRestarting, forced: !!d.forced })
    if (phase !== 'listening') return
    resultSinceStart = true
    armIdle()
    const named = (d.matches?.[0] ?? '').trim()
    if (d.isRestarting || d.accumulatedText !== undefined || d.forced) {
      // A phrase the plugin closed: with the restart payload its accumulated
      // text includes it; a last result and a forced stop carry everything.
      const whole = (d.accumulatedText ?? d.accumulated ?? '').trim()
      deliverFinal(phraseFrom(whole, named))
      pluginFinals = whole
      interim = ''
      if (d.isRestarting && held && hidden()) void finalize('silence')
      return
    }
    const acc = d.accumulated?.trim()
    if (acc !== undefined && acc !== pluginFinals) {
      // A pause closed the last phrase with no event of its own.
      deliverFinal(phraseFrom(acc, ''))
      pluginFinals = acc
      interim = ''
    }
    if (named) { heard = true; interim = named; h.partial(named) }
  }

  const ownsEvent = (d: PluginEvent): boolean => {
    if (typeof d.sessionId !== 'number') return true
    if (id === null) {
      if (!starting || d.sessionId <= lastSeenId) return false
      id = d.sessionId
      lastSeenId = id
      owned.add(id)
      return true
    }
    return d.sessionId === id
  }

  const onListening = (d: PluginEvent) => {
    const state = d.state ?? d.status
    diag?.event('plugin_state', { state: state ?? null, reason: d.reason ?? null, code: d.errorCode ?? null, id: d.sessionId ?? null })
    if (!ownsEvent(d)) return
    if (state === 'started') {
      lastStartedAt = now()
      resultSinceStart = false
      if (d.reason === 'results') debugLog('[stt] the recognizer went on after a phrase')
      return
    }
    if (state !== 'stopped') return
    open = false
    wakeStopped()
    if (phase !== 'listening') return     // starting: the start's outcome decides; ended: nothing
    if (!held) { void finalize('stopped'); return }
    if (d.reason === 'error' && !silenceEnd) return   // the error handler ended the session
    silenceEnd = false
    selfRestart()
  }

  // The plugin ended the session on its own while the button is held (a
  // device whose recognizer ends it anyway, an older plugin build): the
  // words shown stay, and a fresh recognizer starts after the delay.
  const selfRestart = () => {
    const orphan = interim; interim = ''
    if (orphan) deliverFinal(orphan)
    const t = now()
    quickEnds = !resultSinceStart && t - lastStartedAt < DEVICE_QUICK_END_MS ? quickEnds + 1 : 0
    if (quickEnds >= 2) { fatal(DICTATION_STOPPED, 'quick-ends'); return }
    if (hidden()) { void finalize('silence'); return }
    restartTimer = setTimeout(() => {
      restartTimer = null
      if (!held || ended) return
      diag?.event('restart')
      pluginFinals = ''
      startRecognizer(onDevice).catch(() => { void finalize('silence') })
    }, DEVICE_RESTART_DELAY_MS)
  }

  const onError = (d: PluginEvent) => {
    const code = d.code || d.errorCode || ''
    diag?.event('plugin_error', { code, message: d.message ?? null, id: d.sessionId ?? null })
    if (!ownsEvent(d) || phase !== 'listening') return
    if (SILENCE_CODES.has(code)) {
      // A pause. The plugin starts again while the button is held; a
      // recognizer that refuses at once (twice within the quick bound, no
      // word) is not let loop.
      sawSilence = true
      if (!held) return
      if (hidden()) { void finalize('silence'); return }
      const t = now()
      quickEnds = !resultSinceStart && t - lastStartedAt < DEVICE_QUICK_END_MS ? quickEnds + 1 : 0
      if (quickEnds >= 2) fatal(DICTATION_STOPPED, 'quick-ends')
      return
    }
    if (SOFT_CODES.has(code) || code === 'KAFASSISTANTERRORDOMAIN_1110') {
      // The plugin ends the session: its `stopped` is read as a pause.
      if (held) silenceEnd = true
      return
    }
    if (QUIET_ENDS.has(code)) return   // our own cancel: the `stopped` follows
    // A lost link is said whether or not a word was heard (never a silent
    // stop mid-sentence), other codes only before a word.
    const words = deviceWords(code, heard)
    if (isLostLink(code)) { fatal(words as string); return }
    if (heard) { void finalize('error'); return }
    fatal(words ?? d.message ?? `Speech recognition error${code ? ` (${code})` : ''}`)
  }

  /** One plugin start; `bound` cuts an on-device start that waits on a model
   *  download (the person accepted it), so the online recognizer can take
   *  the press. The pending call is left to settle on its own. */
  const startRecognizer = async (useOnDevice: boolean, bound?: number) => {
    if (!sr || !startOpts) throw new Error(DICTATION_NO_START)
    await setHeld(sr, true)
    // A stop that landed during that round trip: no recognizer starts.
    if (ended || stopping) throw new Error(DICTATION_STOPPED)
    if (id !== null) owned.delete(id)
    id = null
    starting = true
    open = true
    resultSinceStart = false
    lastStartedAt = now()
    diag?.event('start_called', { language: startOpts.language, on_device: useOnDevice })
    const call = sr.start({ ...startOpts, useOnDeviceRecognition: useOnDevice })
    try {
      if (bound === undefined) { await call; return }
      const settled = await Promise.race([call.then(() => true), sleep(bound).then(() => false)])
      if (!settled) {
        call.catch(() => {})
        throw new StartTimeout()
      }
    } finally {
      starting = false
    }
  }

  // The start the plugin refused, in the same words, after the session's
  // own cleanup.
  const refused = async (e: unknown): Promise<never> => {
    open = false
    await finalize('error', false).catch(() => {})
    throw new Error(startWords(e instanceof Error ? e.message : String(e ?? '')), { cause: e })
  }
  const isPermissionOrBusy = (e: unknown) => {
    const m = (e instanceof Error ? e.message : String(e ?? '')).toLowerCase()
    return m.includes('permission') || m.includes('denied') || m.includes(ALREADY_RUNNING)
  }

  return {
    async start() {
      interim = ''; ended = false; ending = null; finalizing = null; released = false
      held = true; stopping = false; phase = 'starting'
      diag = openSttDiagSession({ engine: 'capacitor', lang: opts.language, continuous: true, interimResults: true })
      const plugin = await loadSR()
      sr = plugin
      const ios = nativePlatform() === 'ios'
      // Permission check first; ensureNativeMicPermission below is the
      // last-ditch getUserMedia primer (Capacitor's own dialog flow) and
      // runs ONLY while still ungranted — once granted, a dictation start
      // opens no browser capture at all. On iOS the plugin's own request
      // shows the system's dialogs, so the primer never runs there.
      let perm = await plugin.checkPermissions().catch(() => ({ speechRecognition: 'prompt' }))
      if (perm.speechRecognition !== 'granted') {
        perm = await plugin.requestPermissions().catch(() => ({ speechRecognition: 'denied' }))
      }
      if (perm.speechRecognition !== 'granted' && !ios) {
        // Last resort only (not the hot path): grant via Capacitor's getUserMedia
        // flow, then let the WebView mic fully release before the recognizer
        // claims it — opening getUserMedia right before start() starves the
        // recognizer of audio (the "mic active, no words" bug).
        await ensureNativeMicPermission()
        await sleep(400)
        perm = await plugin.checkPermissions().catch(() => ({ speechRecognition: 'denied' }))
      }
      if (perm.speechRecognition !== 'granted') throw new Error(micDenied())
      if (stopping) return
      const avail = await plugin.available().catch(() => ({ available: false }))
      if (!avail.available) throw new Error(recognizerOff())
      const language = ios ? await iosLanguageTag(plugin, opts.language) : opts.language
      // One owner at a time: the previous dictation's plugin session ends
      // first (its last result, then its `stopped`).
      await pluginFree
      if (stopping) return
      pluginFree = new Promise<void>((r) => { freePlugin = r })
      ensureWatcher(plugin)
      handles = await Promise.all([
        listen(plugin, 'partialResults', onPartial),
        listen(plugin, 'listeningState', onListening),
        listen(plugin, 'error', onError),
      ])
      if (stopping) { await finalize('stopped'); return }
      // The newer on-device path streams partials on modern Android (the
      // legacy path does not on Android 13+) and needs no network. Asked at
      // every press: a language the phone can download is offered again
      // after a cancel, since a language on the phone dictates best. A check
      // that hangs answers false.
      onDevice = await probeWithTimeout(
        plugin.isOnDeviceRecognitionAvailable({ language }).then((r) => !!r.available, () => false), ON_DEVICE_CHECK_WAIT_MS,
      )
      if (stopping) { await finalize('stopped'); return }
      startOpts = { language, partialResults: true, popup: false, continuousPTT: true }
      try {
        await startRecognizer(onDevice, onDevice ? ON_DEVICE_START_WAIT_MS : undefined)
      } catch (e) {
        if (stopping) { await finalize('stopped'); return }
        if (!onDevice || isPermissionOrBusy(e)) return refused(e)
        // The on-device path failed for this language (the phone's download
        // prompt cancelled, a download that failed, a locale it refuses) or
        // waits on a download the person accepted: the online recognizer
        // takes the same press, and the next press asks the phone again.
        const cut = e instanceof StartTimeout
        debugWarn(`[stt] the on-device recognizer ${cut ? 'waits on a download' : 'refused'} for ${language}, the online one listens`, e)
        if (cut) void plugin.stop().catch(() => {})
        await waitStopped(DEVICE_STOP_WAIT_MS)
        if (stopping) { await finalize('stopped'); return }
        onDevice = false
        if (cut) h.notice(DICTATION_DOWNLOADING)
        try {
          await startRecognizer(false)
        } catch (e2) {
          const busy = (e2 instanceof Error ? e2.message : '').toLowerCase().includes(ALREADY_RUNNING)
          if (!busy) return refused(e2)
          // The cut attempt is still ending: one stop, one wait, one retry.
          void plugin.stop().catch(() => {})
          await waitStopped(DEVICE_STOP_WAIT_MS)
          if (stopping) { await finalize('stopped'); return }
          try { await startRecognizer(false) } catch (e3) { return refused(e3) }
        }
      }
      phase = 'listening'
      listeningSince = now()
      armIdle()
    },
    async stop() {
      if (ended) return
      stopping = true
      held = false
      if (restartTimer) { clearTimeout(restartTimer); restartTimer = null }
      diag?.event('stop_called')
      if (!sr || !open) { await finalize('stopped'); return }
      // The bound starts before the plugin's stop is awaited (iOS resolves
      // it late); the last result lands before the plugin's `stopped`, whose
      // handler may finalize the session before this returns.
      const wasListening = phase === 'listening'
      const pressed = now() - listeningSince
      const gone = new Promise<void>((r) => { stoppedWaiters.push(r) })
      const bound = sleep(DEVICE_STOP_WAIT_MS)
      released = true
      void setHeld(sr, false)
      void sr.stop().catch(() => {})
      await Promise.race([gone, bound])
      // The online recognizer connected and sent neither a word nor a pause:
      // deaf to the language on this phone, said so instead of nothing.
      if (wasListening && !onDevice && !heard && !sawSilence && pressed >= DEVICE_SILENT_AFTER_MS) {
        h.error(new Error(DICTATION_DEVICE_SILENT))
      }
      await finalize('stopped')
    },
    onPartial(fn) { h.partial = fn },
    onFinal(fn) { h.final = fn },
    onError(fn) { h.error = fn },
    onEnd(fn) { h.end = fn },
    onNotice(fn) { h.notice = fn },
  }
}

// ── Availability probe ────────────────────────────────────────────
// Capacitor's presence says nothing about the DEVICE being able to
// recognize speech (no RecognitionService installed: start() always fails).
// The probe asks the plugin itself once and memoizes; useChatAudioCapability
// awaits it so the server-side capability resolve sees an honest
// has_native_stt (and falls back to cloud providers where policy allows).

let probedNativeStt: boolean | null = null
let sttProbeInFlight: Promise<boolean> | null = null

export async function probeNativeSttAvailable(): Promise<boolean> {
  if (typeof window === 'undefined') return false
  if (probedNativeStt !== null) return probedNativeStt
  if (!isNativePlatform()) {
    probedNativeStt = getRecognitionCtor() !== null
    return probedNativeStt
  }
  // Bounded like the TTS probe: a stalled RecognitionService binding must not
  // wedge the capability resolve — report false now, memoize when it settles.
  sttProbeInFlight ??= (async () => {
    try {
      const sr = await loadSR()
      const a = await sr.available()
      probedNativeStt = !!a.available
    } catch {
      probedNativeStt = false
    }
    return probedNativeStt
  })()
  return probeWithTimeout(sttProbeInFlight)
}

/** The probe still running after its bounded wait answered false, so a
 *  late answer can refresh what was reported; null once settled or never
 *  started. */
export function nativeSttProbeSettling(): Promise<boolean> | null {
  return probedNativeStt === null ? sttProbeInFlight : null
}

/** Test hook: forget the memoized probe and the device's language list (a
 *  test stubs the recognizer). */
export function _resetNativeSttProbeForTests(): void {
  probedNativeStt = null
  sttProbeInFlight = null
  supportedTags = null
  owned.clear()
  lastSeenId = -1
  pluginFree = Promise.resolve()
  watcher = null
}

export const nativeStt: STTBackend = {
  kind: 'native',

  isAvailable() {
    if (typeof window === 'undefined') return false
    // The memoized probe is the truth once known (the capability hook runs
    // it; until it settles, the app answers true and the browser by its
    // recognizer).
    if (probedNativeStt !== null) return probedNativeStt
    // On device the Capacitor plugin provides STT; in the browser it's Web Speech.
    return isNativePlatform() || getRecognitionCtor() !== null
  },

  create(opts: STTCreateOptions): STTSession {
    return isNativePlatform() ? capacitorSession(opts) : webSpeechSession(opts)
  },
}
