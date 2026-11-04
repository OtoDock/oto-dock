import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'

// ─── The device recognizer ──────────────────────────────────────────────────
// The Capacitor plugin in the Android app (the continuous session that keeps
// listening across pauses, its phrases, the plugin's own restart and the
// session's, the stop's bounded wait, the on-device language fallback, the
// one-owner rule) and in the iOS app (Apple's codes, the plugin's own, a
// refused start, the whole language tag), the Web Speech session (its
// errors, the replayed fixtures), the dictation recorder, the Apple WebKit
// detector and the resolver that picks the engine: every piece of the
// device path, in one module.

const debug = vi.hoisted(() => ({ on: true }))
vi.mock('@/lib/debug/log', () => ({
  isDebugOn: () => debug.on,
  debugLog: () => {},
  debugWarn: () => {},
  logError: () => {},
}))

interface PluginEvent {
  matches?: string[]; accumulated?: string; accumulatedText?: string; isRestarting?: boolean; forced?: boolean
  code?: string; message?: string; state?: string; status?: string; reason?: string; errorCode?: string; sessionId?: number
}
type Listener = (d: PluginEvent) => void
// The device's language list, sorted as the plugin lists it: the first entry
// of a language is an odd region (en-AE, de-AT, fr-BE), which the tag rule
// must not pick.
const PLUGIN_LANGUAGES = vi.hoisted(() => ['de-AT', 'de-CH', 'de-DE', 'el-GR', 'en-AE', 'en-AU', 'en-GB', 'en-IE', 'en-IN',
  'en-US', 'es-ES', 'es-MX', 'fr-BE', 'fr-CA', 'fr-FR', 'pt-BR', 'pt-PT'])

// The plugin's fake: a test sets what start answers (per call), which
// permission state the device reports, which languages it lists and which
// have an on-device recognizer, whether a stop emits its end, and reads the
// calls back. Every start opens a plugin session with its own id, announced
// as the plugin does; events carry the current id unless a test says another.
const sr = vi.hoisted(() => ({
  listeners: {} as Record<string, Listener[]>,
  startImpl: (async () => ({})) as (opts: { language?: string }) => Promise<unknown>,
  startCalls: [] as { language?: string; useOnDeviceRecognition?: boolean; continuousPTT?: boolean; partialResults?: boolean; popup?: boolean }[],
  onDeviceChecks: 0,
  onDevice: ((_lang: string) => true) as (lang: string) => boolean,
  stopCalls: 0,
  stopEmitsAfter: null as number | null,   // a stop emits `stopped` after this many ms (null: never)
  held: [] as boolean[],
  setPTTHold: null as Promise<void> | null,   // a held call that waits (the bridge's round trip)
  permission: 'granted',
  languages: [] as string[],
  languageReads: 0,
  current: 0,
  emit(event: string, d: PluginEvent) {
    const data = 'sessionId' in d ? d : { ...d, sessionId: sr.current }
    for (const cb of [...(sr.listeners[event] ?? [])]) cb(data)
  },
}))

vi.mock('@capgo/capacitor-speech-recognition', () => ({
  SpeechRecognition: {
    available: async () => ({ available: true }),
    isOnDeviceRecognitionAvailable: async (o: { language?: string }) => { sr.onDeviceChecks += 1; return { available: sr.onDevice(o.language ?? '') } },
    checkPermissions: async () => ({ speechRecognition: sr.permission }),
    requestPermissions: async () => ({ speechRecognition: sr.permission }),
    getSupportedLanguages: async () => { sr.languageReads += 1; return { languages: sr.languages } },
    setPTTState: async (o: { held: boolean }) => { sr.held.push(o.held); if (sr.setPTTHold) await sr.setPTTHold },
    start: (opts: { language?: string }) => {
      sr.startCalls.push(opts)
      sr.current += 1
      sr.emit('listeningState', { state: 'startingListening', reason: 'userStart' })
      return sr.startImpl(opts)
    },
    stop: async () => {
      sr.stopCalls += 1
      if (sr.stopEmitsAfter !== null) {
        const id = sr.current
        setTimeout(() => sr.emit('listeningState', { state: 'stopped', reason: 'userStop', sessionId: id }), sr.stopEmitsAfter)
      }
    },
    getLastPartialResult: async () => ({ available: false, text: '' }),
    removeAllListeners: async () => { sr.listeners = {} },
    // The plugin the shell registers answers with the handle itself, not a
    // promise (Capacitor's native bridge), which is what the app runs.
    addListener: (event: string, cb: Listener) => {
      ;(sr.listeners[event] ??= []).push(cb)
      return { remove: async () => { sr.listeners[event] = (sr.listeners[event] ?? []).filter((x) => x !== cb) } }
    },
  },
}))


import {
  nativeStt, _resetNativeSttProbeForTests, probeNativeSttAvailable, nativeSttProbeSettling, tailAfter,
  DEVICE_IDLE_END_MS, DEVICE_QUICK_END_MS, DEVICE_RESTART_DELAY_MS, DEVICE_SILENT_AFTER_MS, DEVICE_STOP_WAIT_MS,
  ON_DEVICE_START_WAIT_MS,
} from '@/audio/backends/nativeStt'
import { DICTATION_LOST, DICTATION_NO_CONNECT } from '@/audio/backends/dictationWords'
import * as words from '@/audio/backends/dictationWords'
import {
  _resetSttDiagForTests, exportSttDiag, openSttDiagSession, saveSttDiag, sttDiagCount, STT_DIAG_MAX_EVENTS,
} from '@/audio/sttDiag'
import { installFakeNativeChannel } from './fixtures/nativeChannel'
import { appleWebKitBrowser, isAppleWebKit } from '@/audio/device'
import { capabilityQuery } from '@/hooks/useChatAudioCapability'
import { resolveSttBackend } from '@/audio/resolver'
import type { ChatAudioCapability } from '@/audio/types'

const DENIED_ANDROID = "Microphone access was denied. Allow it in your phone's Settings, Apps, OtoDock, Permissions."
const fakeTimers = () => vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] })
const settle = () => new Promise<void>((r) => setTimeout(r, 0))

type Seen = ReturnType<typeof watch>
function watch(session: ReturnType<typeof nativeStt.create>) {
  const seen = {
    session, finals: [] as string[], partials: [] as string[], errors: [] as string[], notices: [] as (string | null)[],
    ended: [] as string[], startError: '' as string, startDone: false,
  }
  session.onFinal((t) => { seen.finals.push(t) })
  session.onPartial((t) => { seen.partials.push(t) })
  session.onError((e) => { seen.errors.push(e.message) })
  session.onEnd((r) => { seen.ended.push(r ?? '') })
  session.onNotice?.((t) => { seen.notices.push(t) })
  return seen
}
/** A session whose start resolved (the plugin listens). */
async function started(language = 'el-GR'): Promise<Seen & { starting: Promise<void> }> {
  const seen = watch(nativeStt.create({ language }))
  const starting = seen.session.start().then(() => { seen.startDone = true }, (e: Error) => { seen.startError = e.message })
  await starting
  return Object.assign(seen, { starting })
}
/** A session whose start is under way (the fake's start is pending). */
function starting(language = 'el-GR'): Seen & { starting: Promise<void> } {
  const seen = watch(nativeStt.create({ language }))
  const p = seen.session.start().then(() => { seen.startDone = true }, (e: Error) => { seen.startError = e.message })
  return Object.assign(seen, { starting: p })
}
const stoppedNow = (reason = 'silence') => sr.emit('listeningState', { state: 'stopped', reason })
/** Ends a session so the next one may own the plugin. */
async function ended(seen: Seen) {
  const before = sr.stopEmitsAfter
  sr.stopEmitsAfter = 0
  await seen.session.stop()
  sr.stopEmitsAfter = before
}
/** The language tag one start sends, the session ended after it. */
async function tagFor(language: string): Promise<string | undefined> {
  const seen = await started(language)
  await ended(seen)
  return sr.startCalls[sr.startCalls.length - 1]?.language
}

beforeEach(() => {
  sr.listeners = {}
  sr.startImpl = async () => ({})
  sr.startCalls = []
  sr.stopCalls = 0
  sr.stopEmitsAfter = null
  sr.held = []
  sr.setPTTHold = null
  sr.permission = 'granted'
  sr.languages = [...PLUGIN_LANGUAGES]
  sr.languageReads = 0
  sr.onDeviceChecks = 0
  sr.onDevice = () => true
  sr.current = 0
  _resetNativeSttProbeForTests()
  vi.stubGlobal('Capacitor', { isNativePlatform: () => true, getPlatform: () => 'android' })
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('the device recognizer', () => {
  it('starts the continuous mode held, and releases it at the stop, which waits for the plugin\'s end', async () => {
    sr.stopEmitsAfter = 10
    const seen = await started('en-US')
    expect(sr.startCalls[0]).toMatchObject({ language: 'en-US', partialResults: true, popup: false, continuousPTT: true, useOnDeviceRecognition: true })
    expect(sr.held).toEqual([true])
    sr.emit('partialResults', { matches: ['hello there'] })
    let stopped = false
    void seen.session.stop().then(() => { stopped = true })
    await settle()
    expect(stopped).toBe(false)
    // The last result lands before the plugin's end.
    sr.emit('partialResults', { matches: ['hello there'] })
    await vi.waitFor(() => expect(stopped).toBe(true))
    expect(seen.finals).toEqual(['hello there'])
    expect(seen.ended).toEqual(['stopped'])
    expect(sr.held).toEqual([true, false])
    expect(sr.stopCalls).toBe(1)
  })

  it('a network error keeps the words heard and says the dictation stopped', async () => {
    const seen = await started()
    sr.emit('partialResults', { matches: ['καλημέρα σε όλους'] })
    sr.emit('error', { code: 'NETWORK', message: 'Network error' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
    expect(seen.errors).toEqual([DICTATION_LOST])
    expect(seen.finals).toEqual(['καλημέρα σε όλους'])
    expect(sr.held).toEqual([true, false])
  })

  it('a network error before any word says it could not connect', async () => {
    const seen = await started()
    sr.emit('error', { code: 'NETWORK_TIMEOUT', message: 'Network timeout' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
    expect(seen.errors).toEqual([DICTATION_NO_CONNECT])
  })

  it('a SERVER refusal before any word (a locale its service will not serve) says the same', async () => {
    const seen = await started()
    sr.emit('error', { code: 'SERVER', message: 'Server error' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
    expect(seen.errors).toEqual([DICTATION_NO_CONNECT])
  })

  it('Android\'s unnamed language codes read as the language sentence', async () => {
    for (const code of ['UNKNOWN_12', 'UNKNOWN_13']) {
      const seen = await started()
      sr.emit('error', { code, message: 'Didn\'t understand, please try again. Error code: 13' })
      await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
      expect(seen.errors, code).toEqual([words.DICTATION_NO_LANGUAGE])
    }
  })

  it('keeps listening across pauses: each closed phrase is a final, the next one shows after it', async () => {
    sr.stopEmitsAfter = 10
    const seen = await started()
    sr.emit('partialResults', { matches: ['ναι'] })
    expect(seen.partials).toEqual(['ναι'])
    // The plugin closed the phrase and keeps the session alive.
    sr.emit('partialResults', { matches: ['ναι.'], accumulated: 'ναι.', isRestarting: true })
    sr.emit('listeningState', { state: 'started', reason: 'results' })
    expect(seen.finals).toEqual(['ναι.'])
    sr.emit('partialResults', { matches: ['καλημέρα'], accumulated: 'ναι.' })
    expect(seen.partials).toEqual(['ναι', 'καλημέρα'])
    // A pause: no error, no end; the plugin appended the partial silently.
    sr.emit('error', { code: 'SPEECH_TIMEOUT', message: 'No speech input' })
    sr.emit('listeningState', { state: 'started', reason: 'results' })
    expect(seen.errors).toEqual([])
    expect(seen.ended).toEqual([])
    sr.emit('partialResults', { matches: ['σε όλους'], accumulated: 'ναι. καλημέρα' })
    expect(seen.finals).toEqual(['ναι.', 'καλημέρα'])
    expect(seen.partials[seen.partials.length - 1]).toBe('σε όλους')
    // Stop: the last result, then the plugin's end.
    const stopping = seen.session.stop()
    sr.emit('partialResults', { matches: ['σε όλους.'], accumulatedText: 'ναι. καλημέρα σε όλους.' })
    await stopping
    expect(seen.finals).toEqual(['ναι.', 'καλημέρα', 'σε όλους.'])
    expect(seen.ended).toEqual(['stopped'])
  })

  it('a phrase out of step with the session is delivered once, and an empty result keeps the shown words', async () => {
    const seen = await started()
    sr.emit('partialResults', { matches: ['ναι'], accumulated: 'ναι', isRestarting: true })
    sr.emit('partialResults', { matches: ['όχι'], accumulated: 'κάτι άλλο όχι', isRestarting: true })
    expect(seen.finals).toEqual(['ναι', 'όχι'])
    sr.emit('partialResults', { matches: ['ακόμα'], accumulated: 'κάτι άλλο όχι' })
    sr.emit('partialResults', { matches: [''], accumulated: 'κάτι άλλο όχι', isRestarting: true })
    expect(seen.finals).toEqual(['ναι', 'όχι', 'ακόμα'])
    expect(tailAfter('a b', 'a')).toBe('b')
    expect(tailAfter('a', 'a')).toBe('')
    expect(tailAfter('ab', 'a')).toBeNull()
    expect(tailAfter('x', '')).toBe('x')
  })

  it('a plugin that ends the session while held is started again, the words kept', async () => {
    fakeTimers()
    const seen = await started()
    sr.emit('partialResults', { matches: ['ναι'] })
    await vi.advanceTimersByTimeAsync(3000)
    stoppedNow('silence')
    expect(seen.finals).toEqual(['ναι'])
    expect(seen.ended).toEqual([])
    await vi.advanceTimersByTimeAsync(DEVICE_RESTART_DELAY_MS + 10)
    expect(sr.startCalls).toHaveLength(2)
    expect(sr.held).toEqual([true, true])
    // The new plugin session's words start from zero.
    sr.emit('partialResults', { matches: ['πάλι'], accumulated: 'πάλι', isRestarting: true })
    expect(seen.finals).toEqual(['ναι', 'πάλι'])
    // A failed restart ends quietly.
    sr.startImpl = () => Promise.reject(new Error('Speech recognition is already running.'))
    await vi.advanceTimersByTimeAsync(3000)
    stoppedNow('silence')
    await vi.advanceTimersByTimeAsync(DEVICE_RESTART_DELAY_MS + 10)
    expect(seen.ended).toEqual(['silence'])
    expect(seen.errors).toEqual([])

    // A stop that lands while the restart's held call is on the bridge:
    // no recognizer starts.
    sr.startImpl = async () => ({})
    sr.startCalls = []
    let release!: () => void
    const held = new Promise<void>((r) => { release = r })
    const again = await started()
    sr.setPTTHold = held   // the restart's held call waits on the bridge
    stoppedNow('silence')
    await vi.advanceTimersByTimeAsync(DEVICE_RESTART_DELAY_MS + 10)
    expect(sr.startCalls).toHaveLength(1)   // the restart waits on its held call
    sr.stopEmitsAfter = 0
    const stopping = again.session.stop()
    await vi.advanceTimersByTimeAsync(10)
    release()
    await vi.advanceTimersByTimeAsync(50)
    await stopping
    expect(sr.startCalls).toHaveLength(1)
    expect(again.ended).toEqual(['stopped'])
    sr.setPTTHold = null
  })

  it('two quick ends, and two quick pauses, end the dictation with a sentence', async () => {
    fakeTimers()
    let seen = await started()
    stoppedNow('silence')
    await vi.advanceTimersByTimeAsync(DEVICE_RESTART_DELAY_MS + 10)
    expect(sr.startCalls).toHaveLength(2)
    stoppedNow('silence')
    await vi.advanceTimersByTimeAsync(10)
    expect(seen.errors).toEqual([words.DICTATION_STOPPED])
    expect(seen.ended).toEqual(['quick-ends'])
    expect(sr.startCalls).toHaveLength(2)

    // The plugin's own loop: a recognizer that refuses the language at once
    // answers NO_MATCH right after each start and never sends `stopped`.
    sr.startCalls = []
    seen = await started()
    sr.emit('error', { code: 'NO_MATCH', message: 'No match' })
    sr.emit('listeningState', { state: 'started', reason: 'results' })
    expect(seen.ended).toEqual([])
    sr.emit('error', { code: 'NO_MATCH', message: 'No match' })
    await vi.advanceTimersByTimeAsync(10)
    expect(seen.errors).toEqual([words.DICTATION_STOPPED])
    expect(seen.ended).toEqual(['quick-ends'])
    expect(sr.stopCalls).toBeGreaterThan(0)
    expect(sr.held[sr.held.length - 1]).toBe(false)

    // A pause after a word is never counted.
    seen = await started()
    sr.emit('partialResults', { matches: ['ναι'] })
    sr.emit('error', { code: 'NO_MATCH', message: 'No match' })
    sr.emit('listeningState', { state: 'started', reason: 'results' })
    sr.emit('error', { code: 'SPEECH_TIMEOUT', message: 'No speech input' })
    await vi.advanceTimersByTimeAsync(DEVICE_QUICK_END_MS)
    expect(seen.ended).toEqual([])
  })

  it('the recognizer\'s own hiccup after a restart is a pause, not an error', async () => {
    fakeTimers()
    const seen = await started()
    sr.emit('partialResults', { matches: ['ναι'] })
    await vi.advanceTimersByTimeAsync(3000)
    sr.emit('error', { code: 'CLIENT', message: 'Client side error' })
    stoppedNow('error')
    await vi.advanceTimersByTimeAsync(DEVICE_RESTART_DELAY_MS + 10)
    expect(seen.errors).toEqual([])
    expect(seen.finals).toEqual(['ναι'])
    expect(sr.startCalls).toHaveLength(2)
  })

  it('a minute without a word ends with the idle sentence and stops the plugin', async () => {
    fakeTimers()
    const seen = await started()
    await vi.advanceTimersByTimeAsync(DEVICE_IDLE_END_MS - 1000)
    sr.emit('partialResults', { matches: ['ναι'] })
    await vi.advanceTimersByTimeAsync(DEVICE_IDLE_END_MS - 1000)
    expect(seen.ended).toEqual([])
    await vi.advanceTimersByTimeAsync(1100)
    expect(seen.errors).toEqual([words.DICTATION_IDLE])
    expect(seen.ended).toEqual(['idle'])
    expect(seen.finals).toEqual(['ναι'])
    expect(sr.stopCalls).toBe(1)
  })

  it('events of another plugin session are ignored, and a recognizer nobody owns is stopped', async () => {
    const seen = await started()
    sr.emit('error', { code: 'NETWORK', message: 'Network error', sessionId: 999 })
    sr.emit('listeningState', { state: 'stopped', reason: 'silence', sessionId: 999 })
    await settle()
    expect(seen.errors).toEqual([])
    expect(seen.ended).toEqual([])
    sr.stopEmitsAfter = 0
    await seen.session.stop()
    expect(seen.ended).toEqual(['stopped'])
    const stops = sr.stopCalls
    // A model download's late success starts a recognizer after its session ended.
    sr.emit('listeningState', { state: 'started', reason: 'userStart', sessionId: 777 })
    await settle()
    expect(sr.stopCalls).toBe(stops + 1)
  })

  it('a second session waits for the first\'s plugin session to end', async () => {
    fakeTimers()
    const first = await started()
    const second = starting()
    await vi.advanceTimersByTimeAsync(50)
    expect(sr.startCalls).toHaveLength(1)
    void first.session.stop()
    await vi.advanceTimersByTimeAsync(50)
    expect(sr.startCalls).toHaveLength(1)   // the first still waits for its end
    stoppedNow('userStop')
    await vi.advanceTimersByTimeAsync(50)
    expect(first.ended).toEqual(['stopped'])
    expect(sr.startCalls).toHaveLength(2)
    await second.starting
    expect(second.startDone).toBe(true)
    // Past the bound a first that never ends frees the plugin too.
    const third = starting()
    await vi.advanceTimersByTimeAsync(50)
    expect(sr.startCalls).toHaveLength(2)
    void second.session.stop()
    await vi.advanceTimersByTimeAsync(DEVICE_STOP_WAIT_MS + 100)
    expect(sr.startCalls).toHaveLength(3)
    await third.starting
  })

  it('a stop with nothing open resolves at once', async () => {
    fakeTimers()
    const seen = await started()
    stoppedNow('silence')   // the plugin's own end, the restart pending
    let done = false
    void seen.session.stop().then(() => { done = true })
    await vi.advanceTimersByTimeAsync(1)
    expect(done).toBe(true)
    expect(seen.ended).toEqual(['stopped'])
    expect(sr.startCalls).toHaveLength(1)
  })

  it('a hidden page ends on the next pause or phrase, the plugin\'s own or the session\'s', async () => {
    // The session's own restart: the plugin ended the session.
    let hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    let seen = await started()
    sr.emit('partialResults', { matches: ['ναι'] })
    stoppedNow('silence')
    hidden.mockRestore()
    await vi.waitFor(() => expect(seen.ended).toEqual(['silence']))
    expect(seen.finals).toEqual(['ναι'])
    expect(sr.startCalls).toHaveLength(1)

    // The plugin's own pause while hidden: the plugin is stopped, the held
    // flag released, and nothing more lands.
    sr.stopEmitsAfter = 0
    hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    seen = await started()
    const stops = sr.stopCalls
    sr.emit('partialResults', { matches: ['καλημέρα'] })
    sr.emit('error', { code: 'NO_MATCH', message: 'No match' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['silence']))
    expect(seen.finals).toEqual(['καλημέρα'])
    expect(sr.stopCalls).toBe(stops + 1)
    expect(sr.held[sr.held.length - 1]).toBe(false)
    sr.emit('partialResults', { matches: ['όχι'], accumulated: 'καλημέρα' })
    expect(seen.partials).toEqual(['καλημέρα'])
    hidden.mockRestore()

    // A phrase closed while hidden: the final kept, then the end.
    hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    seen = await started()
    sr.emit('partialResults', { matches: ['ναι.'], accumulated: 'ναι.', isRestarting: true })
    await vi.waitFor(() => expect(seen.ended).toEqual(['silence']))
    expect(seen.finals).toEqual(['ναι.'])
    hidden.mockRestore()
  })

  it('asks for the on-device recognizer and takes it when the phone has one, else goes online at once', async () => {
    const seen = await started('en-US')
    expect(sr.onDeviceChecks).toBe(1)
    expect(sr.startCalls[0]?.useOnDeviceRecognition).toBe(true)
    await ended(seen)
    sr.onDevice = () => false
    await started('el-GR')
    expect(sr.startCalls[1]?.useOnDeviceRecognition).toBe(false)
  })

  it('an on-device start the phone refuses goes online in the same press, and the phone is asked again next time', async () => {
    let calls = 0
    sr.startImpl = async () => {
      calls += 1
      if (calls === 1) {
        const id = sr.current
        setTimeout(() => sr.emit('listeningState', { state: 'stopped', reason: 'error', errorCode: 'MODEL_DOWNLOAD_SCHEDULED', sessionId: id }), 5)
        sr.emit('error', { code: 'MODEL_DOWNLOAD_SCHEDULED', message: 'On-device speech model download was scheduled. Try again once it finishes.' })
        throw new Error('On-device speech model download was scheduled. Try again once it finishes.')
      }
      return {}
    }
    const seen = await started('de-DE')
    expect(seen.startDone).toBe(true)
    expect(seen.errors).toEqual([])
    expect(seen.notices).toEqual([])
    expect(sr.startCalls.map((c) => c.useOnDeviceRecognition)).toEqual([true, false])
    sr.emit('partialResults', { matches: ['guten Tag'] })
    expect(seen.partials).toEqual(['guten Tag'])
    // The next press asks the phone again (the download offered again).
    await ended(seen)
    const checks = sr.onDeviceChecks
    await started('de-DE')
    expect(sr.onDeviceChecks).toBe(checks + 1)
    expect(sr.startCalls[2]?.useOnDeviceRecognition).toBe(true)
  })

  it('an online recognizer that sent neither a word nor a pause says so at the stop', async () => {
    fakeTimers()
    sr.onDevice = () => false
    sr.stopEmitsAfter = 0
    // The fake's `stopped` rides a timer: the stop is awaited after a tick.
    const stopped = async (s: Seen) => { const p = s.session.stop(); await vi.advanceTimersByTimeAsync(50); await p }
    let seen = await started('el-GR')
    await vi.advanceTimersByTimeAsync(DEVICE_SILENT_AFTER_MS + 500)
    await stopped(seen)
    expect(seen.errors).toEqual([words.DICTATION_DEVICE_SILENT])
    expect(seen.ended).toEqual(['stopped'])

    // A recognizer that reported a pause hears: no sentence.
    seen = await started('el-GR')
    sr.emit('error', { code: 'NO_MATCH', message: 'No match' })
    await vi.advanceTimersByTimeAsync(DEVICE_SILENT_AFTER_MS + 500)
    await stopped(seen)
    expect(seen.errors).toEqual([])

    // A short press, or a word, or the on-device recognizer: no sentence.
    seen = await started('el-GR')
    await vi.advanceTimersByTimeAsync(1000)
    await stopped(seen)
    expect(seen.errors).toEqual([])
    seen = await started('el-GR')
    sr.emit('partialResults', { matches: ['ναι'] })
    await vi.advanceTimersByTimeAsync(DEVICE_SILENT_AFTER_MS + 500)
    await stopped(seen)
    expect(seen.errors).toEqual([])
    sr.onDevice = () => true
    seen = await started('en-US')
    await vi.advanceTimersByTimeAsync(DEVICE_SILENT_AFTER_MS + 500)
    await stopped(seen)
    expect(seen.errors).toEqual([])
  })

  it('a refused permission or a busy recognizer is said, never retried online', async () => {
    sr.startImpl = () => Promise.reject(new Error('User denied microphone access.'))
    let seen = await started('de-DE')
    expect(seen.startError).toBe(DENIED_ANDROID)
    // The rejection is the word: no end beside it (the hook would read an
    // end before the rejection as a stop and say nothing).
    expect(seen.ended).toEqual([])
    expect(sr.startCalls).toHaveLength(1)
    expect(sr.held).toEqual([true, false])
    sr.startImpl = () => Promise.reject(new Error('Speech recognition is already running.'))
    seen = await started('de-DE')
    expect(seen.startError).toBe(words.DICTATION_NO_START)
    expect(sr.startCalls).toHaveLength(2)
    expect(seen.ended).toEqual([])
  })

  it('a slow on-device start (a download accepted) goes online after the bound with the notice, the language not remembered', async () => {
    fakeTimers()
    let calls = 0
    sr.startImpl = (() => { calls += 1; return calls === 1 ? new Promise(() => {}) : Promise.resolve({}) }) as typeof sr.startImpl
    sr.stopEmitsAfter = 20
    const seen = starting('de-DE')
    await vi.advanceTimersByTimeAsync(ON_DEVICE_START_WAIT_MS - 100)
    expect(sr.stopCalls).toBe(0)
    await vi.advanceTimersByTimeAsync(200)
    expect(sr.stopCalls).toBe(1)   // the pending attempt is cut
    await vi.advanceTimersByTimeAsync(100)
    await seen.starting
    expect(seen.startDone).toBe(true)
    expect(sr.startCalls.map((c) => c.useOnDeviceRecognition)).toEqual([true, false])
    expect(seen.notices).toEqual([words.DICTATION_DOWNLOADING])
    // The late end of the cut attempt changes nothing for the live session.
    sr.emit('listeningState', { state: 'stopped', reason: 'userStop', sessionId: 1 })
    await vi.advanceTimersByTimeAsync(10)
    expect(seen.ended).toEqual([])
    sr.emit('partialResults', { matches: ['guten Tag'] })
    expect(seen.partials).toEqual(['guten Tag'])
    void seen.session.stop()
    await vi.advanceTimersByTimeAsync(50)
    expect(seen.ended).toEqual(['stopped'])
    // The next press asks the device again: the model may be there now.
    const checks = sr.onDeviceChecks
    const again = starting('de-DE')
    await vi.advanceTimersByTimeAsync(50)
    await again.starting
    expect(sr.onDeviceChecks).toBe(checks + 1)
  })

  it('a stop during the start ends quietly with no retry', async () => {
    sr.startImpl = () => new Promise(() => {})
    const seen = starting('de-DE')
    await vi.waitFor(() => expect(sr.startCalls).toHaveLength(1))
    sr.stopEmitsAfter = 0
    await seen.session.stop()
    await seen.starting
    expect(seen.ended).toEqual(['stopped'])
    expect(seen.errors).toEqual([])
    expect(seen.startError).toBe('')
    expect(sr.startCalls).toHaveLength(1)
  })

  it('passes the language as asked and names the phone\'s settings when the microphone is refused', async () => {
    const seen = await started('el')
    expect(sr.startCalls[0]?.language).toBe('el')
    expect(sr.languageReads).toBe(0)
    await ended(seen)
    sr.permission = 'denied'
    const session = nativeStt.create({ language: 'el' })
    await expect(session.start()).rejects.toThrow(DENIED_ANDROID)
    expect(seen.ended).toEqual(['stopped'])
  })
})

describe('the device recognizer in the iOS app', () => {
  const DENIED_IOS = 'Microphone access was denied. Allow the microphone and speech recognition in Settings, Apps, OtoDock.'

  beforeEach(() => {
    vi.stubGlobal('Capacitor', { isNativePlatform: () => true, getPlatform: () => 'ios' })
  })

  it('Apple\'s retry and lost-service codes and a URL error mean the link went', async () => {
    let seen = await started()
    sr.emit('partialResults', { matches: ['καλημέρα'] })
    sr.emit('error', { code: 'KAFASSISTANTERRORDOMAIN_203', message: 'Retry' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
    expect(seen.errors).toEqual([DICTATION_LOST])
    expect(seen.finals).toEqual(['καλημέρα'])

    seen = await started()
    sr.emit('error', { code: 'KAFASSISTANTERRORDOMAIN_1101', message: 'Speech service unavailable' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
    expect(seen.errors).toEqual([DICTATION_NO_CONNECT])

    seen = await started()
    sr.emit('error', { code: 'NSURLERRORDOMAIN_-1009', message: 'The Internet connection appears to be offline.' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
    expect(seen.errors).toEqual([DICTATION_NO_CONNECT])
  })

  it('no speech heard restarts while held with the words kept, is quiet after the stop, and our own cancel ends quietly', async () => {
    fakeTimers()
    const seen = await started()
    sr.emit('partialResults', { matches: ['ναι'] })
    await vi.advanceTimersByTimeAsync(3000)
    sr.emit('error', { code: 'KAFASSISTANTERRORDOMAIN_1110', message: 'No speech detected' })
    stoppedNow('error')
    await vi.advanceTimersByTimeAsync(DEVICE_RESTART_DELAY_MS + 10)
    expect(seen.errors).toEqual([])
    expect(seen.finals).toEqual(['ναι'])
    expect(seen.ended).toEqual([])
    expect(sr.startCalls).toHaveLength(2)

    const stopping = seen.session.stop()
    sr.emit('error', { code: 'KAFASSISTANTERRORDOMAIN_1110', message: 'No speech detected' })
    sr.emit('error', { code: 'KAFASSISTANTERRORDOMAIN_216', message: 'Recognition request was canceled' })
    stoppedNow('userStop')
    await vi.advanceTimersByTimeAsync(10)
    await stopping
    expect(seen.errors).toEqual([])
    expect(seen.ended).toEqual(['stopped'])
  })

  it('the plugin\'s own codes read in plain words', async () => {
    const cases: [string, string][] = [
      ['MICROPHONE_PERMISSION_DENIED', DENIED_IOS],
      ['UNSUPPORTED_LOCALE', words.DICTATION_NO_LANGUAGE],
      ['RECOGNIZER_UNAVAILABLE', words.DICTATION_SERVICE_OFF],
      ['MODERN_START_FAILED', words.DICTATION_SERVICE_OFF],
      // A busy or refused audio session, never a missing microphone: the
      // plain start sentence keeps the platform's fallback open under Auto.
      ['AUDIO_SESSION_FAILED', words.DICTATION_NO_START],
      ['AUDIO_ENGINE_START_FAILED', words.DICTATION_NO_START],
    ]
    for (const [code, sentence] of cases) {
      const seen = await started()
      sr.emit('error', { code, message: 'the plugin\'s own text' })
      await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
      expect(seen.errors, code).toEqual([sentence])
    }
  })

  it('a code with no words of its own keeps the plugin\'s message, only before a word', async () => {
    let seen = await started()
    sr.emit('error', { code: 'SOMETHING_ELSE', message: 'Something else happened' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
    expect(seen.errors).toEqual(['Something else happened'])

    seen = await started()
    sr.emit('partialResults', { matches: ['ναι'] })
    sr.emit('error', { code: 'SOMETHING_ELSE', message: 'Something else happened' })
    await vi.waitFor(() => expect(seen.ended).toEqual(['error']))
    expect(seen.errors).toEqual([])
    expect(seen.finals).toEqual(['ναι'])
  })

  it('a start the plugin refuses is said once, in the same words, its event quiet', async () => {
    sr.onDevice = () => false
    // The event first, then the rejection: the rejection says it.
    let reject!: (e: Error) => void
    sr.startImpl = () => new Promise((_, r) => { reject = r })
    const again = starting()
    await vi.waitFor(() => expect(sr.startCalls).toHaveLength(1))
    sr.emit('error', { code: 'MICROPHONE_PERMISSION_DENIED', message: 'User denied microphone access.' })
    reject(new Error('User denied microphone access.'))
    await again.starting
    expect(again.startError).toBe(DENIED_IOS)
    expect(again.errors).toEqual([])
    expect(again.ended).toEqual([])

    // A rejection with no event (the recognizer already running): start() rejects in plain words.
    sr.startImpl = () => Promise.reject(new Error('Speech recognition is already running.'))
    let seen = await started()
    expect(seen.startError).toBe(words.DICTATION_NO_START)
    expect(seen.errors).toEqual([])

    sr.startImpl = () => Promise.reject(new Error('Unsupported locale: xx-XX'))
    seen = await started()
    expect(seen.startError).toBe(words.DICTATION_NO_LANGUAGE)

    // The rejection first, then the event (the iOS target's order for every
    // code but a denied microphone): the rejection said it, the event is quiet.
    sr.startImpl = () => Promise.reject(new Error('Speech recognizer is currently unavailable.'))
    seen = await started()
    expect(seen.startError).toBe(words.DICTATION_SERVICE_OFF)
    sr.emit('error', { code: 'RECOGNIZER_UNAVAILABLE', message: 'Speech recognizer is currently unavailable.' })
    await new Promise((r) => setTimeout(r, 20))
    expect(seen.errors).toEqual([])
    expect(seen.ended).toEqual([])
  })

  it('an on-device refusal of the modern path goes online in the same press', async () => {
    let calls = 0
    sr.startImpl = async () => {
      calls += 1
      if (calls === 1) throw new Error('On-device recognition is not available for this locale.')
      return {}
    }
    const seen = await started('el-GR')
    expect(seen.startDone).toBe(true)
    expect(sr.startCalls.map((c) => c.useOnDeviceRecognition)).toEqual([true, false])
  })

  it('a refused permission names the app\'s settings and never runs the browser primer', async () => {
    sr.permission = 'denied'
    const session = nativeStt.create({ language: 'el-GR' })
    await expect(session.start()).rejects.toThrow(DENIED_IOS)
    expect(sr.startCalls).toEqual([])
  })

  it('passes the whole tag Apple\'s recognizer takes, read from the device once', async () => {
    vi.stubGlobal('navigator', { language: 'en-GB' })
    expect(await tagFor('el')).toBe('el-GR')
    expect(await tagFor('en')).toBe('en-GB')
    expect(await tagFor('en-US')).toBe('en-US')
    expect(await tagFor('fr-CA')).toBe('fr-CA')
    expect(await tagFor('fr-LU')).toBe('fr-FR')
    expect(await tagFor('xx')).toBe('xx')
    expect(sr.languageReads).toBe(1)
  })

  it('a bare language takes the device\'s region, else its likely one, never the first listed', async () => {
    // A Greek iPad: English is en-US, not the alphabet's en-AE; German de-DE,
    // French fr-FR, Portuguese pt-BR.
    vi.stubGlobal('navigator', { language: 'el-GR' })
    expect(await tagFor('en')).toBe('en-US')
    expect(await tagFor('de')).toBe('de-DE')
    expect(await tagFor('fr')).toBe('fr-FR')
    expect(await tagFor('pt')).toBe('pt-BR')
    // A device in Ireland or Canada speaks its own region's English or French.
    vi.stubGlobal('navigator', { language: 'en-IE' })
    expect(await tagFor('en')).toBe('en-IE')
    vi.stubGlobal('navigator', { language: 'fr-CA' })
    expect(await tagFor('fr')).toBe('fr-CA')
    // A language whose likely region the device does not list: the first it has.
    vi.stubGlobal('navigator', { language: 'el-GR' })
    sr.languages = ['es-MX', 'es-US']
    _resetNativeSttProbeForTests()
    expect(await tagFor('es')).toBe('es-MX')
  })
})

describe('Web Speech', () => {
  class FakeRecognition {
    static last: FakeRecognition | null = null
    lang = ''
    continuous = false
    interimResults = false
    onresult: ((e: unknown) => void) | null = null
    onerror: ((e: { error: string }) => void) | null = null
    onend: (() => void) | null = null
    onstart?: (() => void) | null
    constructor() { FakeRecognition.last = this }
    start() {}
    stop() {}
  }

  beforeEach(() => {
    vi.stubGlobal('Capacitor', { isNativePlatform: () => false })
    vi.stubGlobal('SpeechRecognition', FakeRecognition)
  })

  async function webStarted() {
    const seen = { errors: [] as string[] }
    const session = nativeStt.create({ language: 'el-GR' })
    session.onError((e) => { seen.errors.push(e.message) })
    const started = session.start()
    FakeRecognition.last!.onstart!()   // the recognizer is ready before anything is said
    await started
    return { seen, recog: FakeRecognition.last! }
  }

  it('a network error at the start says it could not connect', async () => {
    const { seen, recog } = await webStarted()
    recog.onerror?.({ error: 'network' })
    expect(seen.errors).toEqual([DICTATION_NO_CONNECT])
  })

  it('a network error after words says the connection was lost', async () => {
    const { seen, recog } = await webStarted()
    recog.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: 'γεια' }], { isFinal: false })] })
    recog.onerror?.({ error: 'network' })
    expect(seen.errors).toEqual([DICTATION_LOST])
  })
})


interface FixtureEvent {
  event: string
  t_ms?: number
  ms?: number
  resultIndex?: number
  results?: { isFinal: boolean; text: string }[]
  error?: string
}
interface Fixture {
  source: string
  lang?: string
  events?: FixtureEvent[]
  sessions?: { lang?: string; events: FixtureEvent[] }[]
  expect: {
    start: string
    finals: string[]
    replaces: string[]
    interims: string[]
    errors: string[]
    ended: string[]
    recognizers: number
  }
}

const fixtures = import.meta.glob('./fixtures/web-speech/*.json', { eager: true, import: 'default' }) as Record<string, Fixture>

class ReplayedRecognition {
  static last: ReplayedRecognition | null = null
  static made = 0
  lang = ''
  continuous = false
  interimResults = false
  stopped = false
  aborted = false
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  onstart?: (() => void) | null
  onaudiostart?: (() => void) | null
  constructor() { ReplayedRecognition.last = this; ReplayedRecognition.made += 1 }
  start() {}
  stop() { this.stopped = true }
  abort() { this.aborted = true }
}

const WORD = (key: string) => (words as Record<string, string>)[`DICTATION_${key}`] ?? key

const tick = async () => { for (let i = 0; i < 6; i++) await Promise.resolve() }

async function play(fx: Fixture) {
  const events = fx.sessions?.[0]?.events ?? fx.events ?? []
  const lang = fx.sessions?.[0]?.lang ?? fx.lang ?? 'en-US'
  const seen = {
    start: 'pending', finals: [] as string[], replaces: [] as string[], interims: [] as string[],
    errors: [] as string[], ended: [] as string[],
  }
  const session = nativeStt.create({ language: lang })
  session.onFinal((t) => { seen.finals.push(t) })
  session.onReplace?.((t) => { seen.replaces.push(t) })
  session.onPartial((t) => { seen.interims.push(t) })
  session.onError((e) => { seen.errors.push(e.message) })
  session.onEnd((reason) => { seen.ended.push(reason ?? '') })
  void session.start().then(() => { seen.start = 'ok' }, (e: Error) => { seen.start = e.message })
  await tick()
  let last: number | null = null
  for (const ev of events) {
    if (typeof ev.t_ms === 'number') {
      if (last !== null && ev.t_ms > last) await vi.advanceTimersByTimeAsync(ev.t_ms - last)
      last = ev.t_ms
    }
    const r = ReplayedRecognition.last!
    switch (ev.event) {
      case 'start': r.onstart?.(); break
      case 'result':
        r.onresult?.({
          resultIndex: ev.resultIndex ?? 0,
          results: (ev.results ?? []).map((x) => Object.assign([{ transcript: x.text }], { isFinal: x.isFinal })),
        })
        break
      case 'error': r.onerror?.({ error: ev.error ?? '' }); break
      case 'end': r.onend?.(); break
      case 'stop_called': void session.stop(); break
      case 'wait': await vi.advanceTimersByTimeAsync(ev.ms ?? 0); break
      default: break   // the recorder's other marks carry nothing
    }
    await tick()
  }
  await vi.advanceTimersByTimeAsync(0)
  return { seen, recognizers: ReplayedRecognition.made }
}

describe('Web Speech, replayed', () => {
  beforeEach(() => {
    ReplayedRecognition.last = null
    ReplayedRecognition.made = 0
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] })
    vi.stubGlobal('Capacitor', { isNativePlatform: () => false })
    vi.stubGlobal('SpeechRecognition', ReplayedRecognition)
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  for (const [path, fx] of Object.entries(fixtures)) {
    const name = path.split('/').pop()!.replace(/\.json$/, '')
    it(name, async () => {
      const { seen, recognizers } = await play(fx)
      const want = fx.expect
      expect(seen.start).toBe(want.start === 'ok' ? 'ok' : WORD(want.start))
      expect(seen.finals).toEqual(want.finals)
      expect(seen.replaces).toEqual(want.replaces)
      expect(seen.interims).toEqual(want.interims)
      expect(seen.errors).toEqual(want.errors.map(WORD))
      expect(seen.ended).toEqual(want.ended)
      expect(recognizers).toBe(want.recognizers)
    })
  }

  it('a hidden page ends on the pause instead of restarting', async () => {
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    const { seen, recognizers } = await play({
      source: 'test', expect: { start: 'ok', finals: [], replaces: [], interims: [], errors: [], ended: [], recognizers: 1 },
      events: [{ event: 'start' }, { event: 'result', resultIndex: 0, results: [{ isFinal: true, text: 'a.' }] }, { event: 'end' }, { event: 'wait', ms: 300 }],
    })
    hidden.mockRestore()
    expect(seen.ended).toEqual(['silence'])
    expect(seen.errors).toEqual([])
    expect(recognizers).toBe(1)
  })

  it('a stop waits for the recognizer\'s end, at most the stop bound', async () => {
    const session = nativeStt.create({ language: 'en-US' })
    const ended: string[] = []
    session.onEnd((r) => { ended.push(r ?? '') })
    const started = session.start()
    ReplayedRecognition.last!.onstart!()
    await started
    let stopped = false
    void session.stop().then(() => { stopped = true })
    await tick()
    expect(ReplayedRecognition.last!.stopped).toBe(true)
    expect(stopped).toBe(false)
    ReplayedRecognition.last!.onend!()
    await tick()
    expect(stopped).toBe(true)
    expect(ended).toEqual(['stopped'])

    // Past the bound: the interim still shown becomes the last final and
    // the recognizer is cut.
    const again = nativeStt.create({ language: 'en-US' })
    const finals: string[] = []
    again.onFinal((t) => { finals.push(t) })
    const p = again.start()
    const slow = ReplayedRecognition.last!
    slow.onstart!()
    await p
    slow.onresult!({ resultIndex: 0, results: [Object.assign([{ transcript: 'still showing' }], { isFinal: false })] })
    let bounded = false
    void again.stop().then(() => { bounded = true })
    await vi.advanceTimersByTimeAsync(1600)
    expect(bounded).toBe(true)
    expect(finals).toEqual(['still showing'])
    expect(slow.aborted).toBe(true)
  })

  it('a stop the error handler calls ends the session under the error, not as a stop', async () => {
    // The hook's fallback stops the device session from inside onError; the
    // recorder must still read the end as the error it was.
    const session = nativeStt.create({ language: 'en-US' })
    const ended: string[] = []
    session.onEnd((r) => { ended.push(r ?? '') })
    session.onError(() => { void session.stop() })
    const started = session.start()
    ReplayedRecognition.last!.onstart!()
    await started
    ReplayedRecognition.last!.onerror!({ error: 'network' })
    await tick()
    expect(ended).toEqual(['error'])
  })
})


class RecordedRecognition {
  static last: RecordedRecognition | null = null
  lang = ''
  continuous = false
  interimResults = false
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: { error: string; message?: string }) => void) | null = null
  onend: (() => void) | null = null
  onstart?: (() => void) | null
  onaudiostart?: (() => void) | null
  onspeechend?: (() => void) | null
  onnomatch?: (() => void) | null
  constructor() { RecordedRecognition.last = this }
  start() {}
  stop() {}
}

const result = (resultIndex: number, ...rows: [string, boolean][]) => ({
  resultIndex,
  results: rows.map(([transcript, isFinal]) => Object.assign([{ transcript, confidence: 0.9 }], { isFinal })),
})

function sessions() {
  return JSON.parse(exportSttDiag().json).sessions as { lang: string; events: Record<string, unknown>[] }[]
}

describe('the dictation recorder', () => {
  beforeEach(() => {
    debug.on = true
    _resetSttDiagForTests()
    vi.stubGlobal('Capacitor', { isNativePlatform: () => false })
    vi.stubGlobal('SpeechRecognition', RecordedRecognition)
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('records a Web Speech session in order with every result', async () => {
    const session = nativeStt.create({ language: 'el-GR' })
    const started = session.start()
    const recog = RecordedRecognition.last!
    recog.onstart?.()
    await started
    recog.onaudiostart?.()
    recog.onresult?.(result(0, ['γεια', false]))
    recog.onresult?.(result(0, ['γεια σου', true], ['τι', false]))
    recog.onerror?.({ error: 'network', message: 'gone' })
    await session.stop()
    recog.onend?.()

    const [s] = sessions()
    expect(s.lang).toBe('el-GR')
    expect(s.events.map((e) => e.event)).toEqual([
      'start_called', 'start', 'audiostart', 'result', 'result', 'error', 'session_end', 'stop_called', 'end',
    ])
    expect(s.events[3]).toMatchObject({
      resultIndex: 0, length: 1, results: [{ isFinal: false, text: 'γεια', confidence: 0.9 }],
    })
    expect(s.events[4]).toMatchObject({
      resultIndex: 0, length: 2,
      results: [{ isFinal: true, text: 'γεια σου' }, { isFinal: false, text: 'τι' }],
    })
    expect(s.events[5]).toMatchObject({ error: 'network', message: 'gone' })
    expect(s.events.every((e) => typeof e.t_ms === 'number')).toBe(true)
    expect(sttDiagCount()).toBe(9)
  })

  it('with the flag off records nothing and sets no extra handler', async () => {
    debug.on = false
    const session = nativeStt.create({ language: 'en-US' })
    const started = session.start()
    const recog = RecordedRecognition.last!
    expect(recog.onnomatch).toBeUndefined()   // the recorder's marks are not set
    recog.onstart?.()                         // the ready mark is always set
    await started
    recog.onresult?.(result(0, ['hello', true]))
    recog.onend?.()
    expect(sttDiagCount()).toBe(0)
    expect(openSttDiagSession({ engine: 'web_speech', lang: 'en', continuous: true, interimResults: true })).toBeNull()
    expect(saveSttDiag()).toBe(false)
  })

  it('keeps the newest events under the cap, the oldest session first to go', () => {
    const a = openSttDiagSession({ engine: 'web_speech', lang: 'en', continuous: true, interimResults: true })!
    const b = openSttDiagSession({ engine: 'web_speech', lang: 'el', continuous: true, interimResults: true })!
    a.event('one')
    a.event('two')
    for (let i = 0; i < STT_DIAG_MAX_EVENTS; i++) b.event('tick', { i })
    expect(sttDiagCount()).toBe(STT_DIAG_MAX_EVENTS)
    const kept = sessions()
    expect(kept).toHaveLength(1)
    expect(kept[0].lang).toBe('el')
    expect(kept[0].events[0]).toMatchObject({ event: 'tick', i: 0 })
  })

  it('saves one local file: a browser download, or the Android app Downloads bridge', () => {
    openSttDiagSession({ engine: 'web_speech', lang: 'en', continuous: true, interimResults: true })!.event('start')
    const urls: string[] = []
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: () => { urls.push('blob:x'); return 'blob:x' },
      revokeObjectURL: () => {},
    }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    expect(saveSttDiag()).toBe(true)
    expect(click).toHaveBeenCalledTimes(1)
    expect(urls).toEqual(['blob:x'])

    const channel = installFakeNativeChannel()
    try {
      expect(saveSttDiag()).toBe(true)
      const post = channel.posted.find((p) => p.m === 'saveImageFromBase64')!
      expect(String(post.a[0])).toMatch(/^dictation-log-.*\.json$/)
      expect(post.a[1]).toBe('application/json')
      const doc = JSON.parse(atob(post.body!))
      expect(doc.kind).toBe('otodock-dictation-log')
      expect(doc.sessions[0].events[0].event).toBe('start')
    } finally {
      channel.uninstall()
    }
  })
})


const UA = {
  safariMac: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
  safariIphone: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
  safariIpadMobile: 'Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
  chromeIos: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.54 Mobile/15E148 Safari/604.1',
  firefoxIos: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/127.0 Mobile/15E148 Safari/605.1.15',
  edgeIos: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 EdgiOS/126.0.2592.56 Mobile/15E148 Safari/605.1.15',
  chromeMac: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
  chromeLinux: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
  edgeWindows: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.2849.56',
  firefoxLinux: 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
  firefoxAndroid: 'Mozilla/5.0 (Android 14; Mobile; rv:128.0) Gecko/128.0 Firefox/128.0',
  chromeAndroid: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36',
  samsung: 'Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36',
  androidWebView: 'Mozilla/5.0 (Linux; Android 14; FCP-N49 Build/HONORFCP-N49; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36',
  jsdom: 'Mozilla/5.0 (linux) AppleWebKit/537.36 (KHTML, like Gecko) jsdom/26.0.0',
}

describe('isAppleWebKit', () => {
  it.each(['safariMac', 'safariIphone', 'safariIpadMobile', 'chromeIos', 'firefoxIos', 'edgeIos'] as const)('%s is Apple WebKit', (k) => {
    expect(isAppleWebKit(UA[k])).toBe(true)
  })
  it.each([
    'chromeMac', 'chromeLinux', 'edgeWindows', 'firefoxLinux', 'firefoxAndroid', 'chromeAndroid', 'samsung', 'androidWebView', 'jsdom',
  ] as const)('%s is not', (k) => {
    expect(isAppleWebKit(UA[k])).toBe(false)
  })

  it('the page\'s answer is false inside a Capacitor app, whatever the user agent', () => {
    vi.stubGlobal('navigator', { ...navigator, userAgent: UA.safariIphone })
    vi.stubGlobal('Capacitor', { isNativePlatform: () => false })
    expect(appleWebKitBrowser()).toBe(true)
    vi.stubGlobal('Capacitor', { isNativePlatform: () => true })
    expect(appleWebKitBrowser()).toBe(false)
  })
})

describe('the capability request', () => {
  it('names Apple WebKit beside a device recognizer only', () => {
    expect(capabilityQuery(true, true, true)).toBe('/v1/audio/capability?has_native_tts=true&has_native_stt=true&apple_webkit=true')
    expect(capabilityQuery(true, true, false)).toBe('/v1/audio/capability?has_native_tts=true&has_native_stt=true')
    expect(capabilityQuery(false, false, true)).toBe('/v1/audio/capability?has_native_tts=false&has_native_stt=false')
  })
})


// ─── Which recognizer a dictation gets ──────────────────────────────────────
// The capability's answer, the person's mode and the stored language decide:
// the Automatic language runs on the platform whenever the capability says a
// provider is there for it, unless the person picked the device; on Apple's
// WebKit the platform is the default where both are allowed; a missing
// backend falls to the other. The backends are the real modules: the device
// one is there with a recognizer constructor, the platform one with a
// microphone capture.

const cap = (stt: ChatAudioCapability['stt'], automatic: 'platform' | 'device' | undefined = 'device'): ChatAudioCapability => ({
  tts: 'unavailable', stt, tts_provider_id: null, stt_provider_id: 1, reason: '', icons_enabled: true,
  ...(automatic ? { stt_automatic: automatic } : {}),
})
const kind = (b: ReturnType<typeof resolveSttBackend>) => b?.kind ?? null
function backends(device: boolean, platform: boolean) {
  vi.stubGlobal('Capacitor', { isNativePlatform: () => false })
  vi.stubGlobal('SpeechRecognition', device ? class { start() {} stop() {} } : undefined)
  Object.defineProperty(navigator, 'mediaDevices', { value: platform ? { getUserMedia: async () => ({}) } : undefined, configurable: true })
  _resetNativeSttProbeForTests()
}

describe('resolveSttBackend', () => {
  beforeEach(() => { backends(true, true) })

  it('unavailable and platform answers', () => {
    expect(kind(resolveSttBackend(cap('unavailable'), 'auto', 'auto', false))).toBeNull()
    expect(kind(resolveSttBackend(cap('platform'), 'native', 'en-US', false))).toBe('platform')
  })

  it('a device-only answer is the device, except the Automatic language with a provider behind it', () => {
    expect(kind(resolveSttBackend(cap('native_only'), 'auto', 'en-US', false))).toBe('native')
    expect(kind(resolveSttBackend(cap('native_only', 'platform'), 'auto', 'auto', false))).toBe('platform')
    // A stored device pick wins over the Automatic rule here too (the pick
    // follows the person across devices): the device, in its own language.
    expect(kind(resolveSttBackend(cap('native_only', 'platform'), 'native', 'auto', false))).toBe('native')
    expect(kind(resolveSttBackend(cap('native_only', 'platform'), 'auto', 'el-GR', false))).toBe('native')
    expect(kind(resolveSttBackend(cap('native_only', 'device'), 'auto', 'auto', false))).toBe('native')
    // An older proxy without the field: the device, as before.
    expect(kind(resolveSttBackend(cap('native_only', undefined), 'auto', 'auto', false))).toBe('native')
  })

  it('either: the mode decides, Automatic goes to the platform unless the device was picked', () => {
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'auto', 'en-US', false))).toBe('native')
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'auto', 'auto', false))).toBe('platform')
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'native', 'auto', false))).toBe('native')
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'platform', 'en-US', false))).toBe('platform')
  })

  it('either on Apple WebKit: the platform by default, the device on a pick', () => {
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'auto', 'en-US', true))).toBe('platform')
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'native', 'en-US', true))).toBe('native')
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'platform', 'en-US', true))).toBe('platform')
  })

  it('a missing backend falls to the other', () => {
    backends(false, true)
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'native', 'en-US', false))).toBe('platform')
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'auto', 'en-US', false))).toBe('platform')
    expect(kind(resolveSttBackend(cap('native_only'), 'auto', 'en-US', false))).toBeNull()
    backends(true, false)
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'auto', 'en-US', true))).toBe('native')
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'auto', 'auto', false))).toBe('native')
    expect(kind(resolveSttBackend(cap('either', 'platform'), 'platform', 'en-US', false))).toBeNull()
    expect(kind(resolveSttBackend(cap('platform'), 'auto', 'en-US', false))).toBeNull()
  })
})

describe('the device probe inside the app', () => {
  // The shell registers the plugin natively, so its proxy is on the global
  // already: the probe asks that proxy and never loads the module (whose
  // import never settled on one Android WebView). The module mock above
  // answers true, so a false here proves the proxy was the one asked.
  it('asks the plugin the shell registered, not the module', async () => {
    vi.stubGlobal('Capacitor', {
      isNativePlatform: () => true, getPlatform: () => 'android',
      Plugins: { SpeechRecognition: { available: async () => ({ available: false }) } },
    })
    _resetNativeSttProbeForTests()
    expect(await probeNativeSttAvailable()).toBe(false)
    expect(nativeSttProbeSettling()).toBeNull()
  })

  it('a recognizer that answers after the wait is reported late', async () => {
    vi.useFakeTimers()
    let answer: (v: { available: boolean }) => void = () => {}
    vi.stubGlobal('Capacitor', {
      isNativePlatform: () => true, getPlatform: () => 'android',
      Plugins: { SpeechRecognition: { available: () => new Promise<{ available: boolean }>((r) => { answer = r }) } },
    })
    _resetNativeSttProbeForTests()
    const first = probeNativeSttAvailable()
    await vi.advanceTimersByTimeAsync(4000)
    expect(await first).toBe(false)
    const late = nativeSttProbeSettling()
    expect(late).not.toBeNull()
    answer({ available: true })
    expect(await late).toBe(true)
    expect(await probeNativeSttAvailable()).toBe(true)
    expect(nativeSttProbeSettling()).toBeNull()
  })
})
