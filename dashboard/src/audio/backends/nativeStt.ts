// Native STT — @capgo/capacitor-speech-recognition on device (a maintained,
// Capacitor-8-native fork of the community plugin), and the browser Web Speech
// API (webkitSpeechRecognition) on desktop Chrome/Edge.
//
// Firefox/Safari have no Web Speech STT → isAvailable() is false there and the
// resolver falls back to the platform WS backend. On Android the Capacitor
// plugin does on-device recognition (no server round-trip / no Deepgram cost);
// requires RECORD_AUDIO (declared in AndroidManifest) + an APK that bundles the
// plugin's native module.

import { type STTBackend, type STTSession, type STTCreateOptions, isNativePlatform, probeWithTimeout } from '../types'
import { ensureNativeMicPermission } from '../micPermission'

// ── Browser (Web Speech) ──────────────────────────────────────────

interface SpeechRecognitionLike {
  lang: string
  continuous: boolean
  interimResults: boolean
  start(): void
  stop(): void
  abort(): void
  onresult: ((e: { resultIndex: number; results: { isFinal: boolean; 0: { transcript: string } }[] }) => void) | null
  onerror: ((e: { error: string }) => void) | null
  onend: (() => void) | null
}

function getRecognitionCtor(): (new () => SpeechRecognitionLike) | null {
  const w = window as unknown as {
    webkitSpeechRecognition?: new () => SpeechRecognitionLike
    SpeechRecognition?: new () => SpeechRecognitionLike
  }
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

function webSpeechSession(opts: STTCreateOptions): STTSession {
  const Ctor = getRecognitionCtor()
  let recog: SpeechRecognitionLike | null = null
  const h = { partial: (_: string) => {}, final: (_: string) => {}, error: (_: Error) => {}, end: () => {} }
  return {
    async start() {
      if (!Ctor) throw new Error('No native speech recognition')
      recog = new Ctor()
      recog.lang = opts.language
      recog.continuous = true
      recog.interimResults = true
      // Web Speech accumulates ALL results in e.results (finals + the current
      // interim chunks). Build the live partial from EVERY non-final chunk joined
      // — not just the last changed one — so a multi-chunk sentence GROWS instead
      // of each new chunk replacing the previous (the "words get replaced while
      // talking" bug; Deepgram doesn't hit it because it streams finals). Each
      // final is delivered exactly once via finalizedCount.
      let finalizedCount = 0
      recog.onresult = (e) => {
        let interim = ''
        for (let i = 0; i < e.results.length; i++) {
          const t = e.results[i][0].transcript
          if (e.results[i].isFinal) {
            if (i >= finalizedCount) { if (t.trim()) h.final(t); finalizedCount = i + 1 }
          } else {
            interim += (interim && !interim.endsWith(' ') ? ' ' : '') + t
          }
        }
        if (interim) h.partial(interim)
      }
      recog.onerror = (e) => h.error(new Error(e.error || 'speech recognition error'))
      recog.onend = () => h.end()
      recog.start()
    },
    async stop() { try { recog?.stop() } catch { /* ignore */ } recog = null },
    onPartial(fn) { h.partial = fn },
    onFinal(fn) { h.final = fn },
    onError(fn) { h.error = fn },
    onEnd(fn) { h.end = fn },
  }
}

// ── Native (Capacitor @capgo plugin) ──────────────────────────────
// Minimal interface over the bits we use — the dynamic import keeps the plugin
// out of the web bundle path.

interface CapgoSR {
  available(): Promise<{ available: boolean }>
  isOnDeviceRecognitionAvailable(opts?: { language?: string }): Promise<{ available: boolean }>
  checkPermissions(): Promise<{ speechRecognition: string }>
  requestPermissions(): Promise<{ speechRecognition: string }>
  start(opts: { language?: string; partialResults?: boolean; popup?: boolean; useOnDeviceRecognition?: boolean }): Promise<{ matches?: string[] }>
  stop(): Promise<void>
  getLastPartialResult(): Promise<{ available: boolean; text: string; matches?: string[] }>
  removeAllListeners(): Promise<void>
  addListener(
    event: string,
    cb: (data: {
      matches?: string[]; accumulatedText?: string
      status?: string; state?: string; reason?: string; errorCode?: string
      code?: string; message?: string
    }) => void,
  ): Promise<{ remove: () => Promise<void> }>
}

async function loadSR(): Promise<CapgoSR> {
  const mod = await import('@capgo/capacitor-speech-recognition')
  return (mod as unknown as { SpeechRecognition: CapgoSR }).SpeechRecognition
}

function capacitorSession(opts: STTCreateOptions): STTSession {
  const h = { partial: (_: string) => {}, final: (_: string) => {}, error: (_: Error) => {}, end: () => {} }
  let last = ''
  let ended = false
  // stop() during an in-flight start() (the permission/availability awaits):
  // start() must abort at its next checkpoint or the recognizer opens as a
  // background session the UI no longer shows.
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | null = null

  const finalize = async () => {
    if (ended) return
    ended = true
    if (timer) { clearTimeout(timer); timer = null }
    const sr = await loadSR().catch(() => null)
    // The native recognizer caches its last transcript — grab it in case the
    // final arrived after (or instead of) a streamed partial.
    if (!last && sr) {
      const lp = await sr.getLastPartialResult().catch(() => null)
      if (lp?.available && lp.text) last = lp.text
    }
    sr?.removeAllListeners().catch(() => {})
    const text = last.trim(); last = ''
    if (text) h.final(text)
    h.end()
  }
  const scheduleFinalize = (ms: number) => {
    if (ended) return
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => { void finalize() }, ms)
  }

  return {
    async start() {
      last = ''; ended = false
      const sr = await loadSR()
      // Permission is primed by MicIcon (getUserMedia, the Capacitor way); this
      // is a read-only confirm + a last-ditch plugin request if still ungranted.
      let perm = await sr.checkPermissions().catch(() => ({ speechRecognition: 'prompt' }))
      if (perm.speechRecognition !== 'granted') {
        perm = await sr.requestPermissions().catch(() => ({ speechRecognition: 'denied' }))
      }
      if (perm.speechRecognition !== 'granted') {
        // Last resort only (not the hot path): grant via Capacitor's getUserMedia
        // flow, then let the WebView mic fully release before the recognizer
        // claims it — opening getUserMedia right before start() starves the
        // recognizer of audio (the "mic active, no words" bug).
        await ensureNativeMicPermission()
        await new Promise(r => setTimeout(r, 400))
        perm = await sr.checkPermissions().catch(() => ({ speechRecognition: 'denied' }))
        if (perm.speechRecognition !== 'granted') {
          throw new Error('Microphone permission denied. Enable it in Settings → Apps → OtoDock → Permissions.')
        }
      }
      if (stopped) return
      const avail = await sr.available().catch(() => ({ available: false }))
      if (!avail.available) {
        throw new Error('On-device speech recognition is unavailable here. Install/enable Google’s speech services, or set Dictation to “platform” in Audio settings.')
      }
      // Prefer the newer on-device path when the device/locale supports it — it
      // streams partial results on modern Android (the legacy path does not on
      // Android 13+).
      const onDevice = await sr.isOnDeviceRecognitionAvailable({ language: opts.language }).catch(() => ({ available: false }))
      if (stopped) return

      await sr.addListener('partialResults', (d) => {
        const m = d.matches?.[0] || d.accumulatedText
        if (typeof m === 'string' && m) { last = m; h.partial(m) }
      })
      await sr.addListener('listeningState', (d) => {
        if ((d.state || d.status) === 'stopped') scheduleFinalize(700)
      })
      await sr.addListener('error', (d) => {
        // Real recognizer errors are finally visible with this fork. If we
        // already captured a partial, just use it; otherwise surface the reason.
        if (last) { void finalize() }
        else { h.error(new Error(d.message || `Speech recognition error${d.code ? ` (${d.code})` : ''}`)); void finalize() }
      })
      if (stopped) { // stopped while connecting — never open the recognizer
        await sr.removeAllListeners().catch(() => {})
        return
      }
      scheduleFinalize(30000) // safety net — never leave the mic stuck
      const res = await sr.start({
        language: opts.language, partialResults: true, popup: false,
        useOnDeviceRecognition: !!onDevice.available,
      })
      // Legacy / non-streaming path resolves start() with the final matches.
      const m = res?.matches?.[0]
      if (typeof m === 'string' && m) { last = m; void finalize() }
    },
    async stop() {
      stopped = true
      const sr = await loadSR()
      try { await sr.stop() } catch { /* ignore */ }
      scheduleFinalize(600) // catch the trailing final, then finalize
    },
    onPartial(fn) { h.partial = fn },
    onFinal(fn) { h.final = fn },
    onError(fn) { h.error = fn },
    onEnd(fn) { h.end = fn },
  }
}

// ── Availability probe ────────────────────────────────────────────
// isAvailable() used to short-circuit TRUE inside the APK — but Capacitor
// presence says nothing about the DEVICE being able to recognize speech (no
// RecognitionService installed → start() always fails, the "native never
// works on this phone" report). The probe asks the plugin itself once and
// memoizes; useChatAudioCapability awaits it so the server-side capability
// resolve sees an honest has_native_stt (and falls back to cloud providers
// where policy allows).

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

export const nativeStt: STTBackend = {
  kind: 'native',

  isAvailable() {
    if (typeof window === 'undefined') return false
    // The memoized probe is the truth once known (the capability hook runs
    // it before anything user-facing resolves a backend).
    if (probedNativeStt !== null) return probedNativeStt
    // On device the Capacitor plugin provides STT; in the browser it's Web Speech.
    return isNativePlatform() || getRecognitionCtor() !== null
  },

  create(opts: STTCreateOptions): STTSession {
    return isNativePlatform() ? capacitorSession(opts) : webSpeechSession(opts)
  },
}
