import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'

// ─── The device recognizer on a link that goes ──────────────────────────────
// Greek needs the network even for the device recognizer on many phones. A
// network error keeps the words heard so far and says the dictation stopped,
// never a silent stop mid-sentence.

type Listener = (d: { matches?: string[]; code?: string; message?: string; state?: string }) => void
const sr = vi.hoisted(() => ({ listeners: {} as Record<string, Listener> }))

vi.mock('@capgo/capacitor-speech-recognition', () => ({
  SpeechRecognition: {
    available: async () => ({ available: true }),
    isOnDeviceRecognitionAvailable: async () => ({ available: true }),
    checkPermissions: async () => ({ speechRecognition: 'granted' }),
    requestPermissions: async () => ({ speechRecognition: 'granted' }),
    start: () => new Promise(() => {}),
    stop: async () => {},
    getLastPartialResult: async () => ({ available: false, text: '' }),
    removeAllListeners: async () => {},
    addListener: async (event: string, cb: Listener) => {
      sr.listeners[event] = cb
      return { remove: async () => {} }
    },
  },
}))

import { nativeStt } from '@/audio/backends/nativeStt'
import { DICTATION_LOST, DICTATION_NO_CONNECT } from '@/audio/backends/dictationWords'

async function started() {
  const seen = { finals: [] as string[], errors: [] as string[], ended: 0 }
  const session = nativeStt.create({ language: 'el-GR' })
  session.onFinal((t) => { seen.finals.push(t) })
  session.onError((e) => { seen.errors.push(e.message) })
  session.onEnd(() => { seen.ended += 1 })
  void session.start()
  await vi.waitFor(() => expect(sr.listeners.error).toBeDefined())
  return seen
}

beforeEach(() => {
  sr.listeners = {}
  vi.stubGlobal('Capacitor', { isNativePlatform: () => true })
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the device recognizer', () => {
  it('a network error keeps the words heard and says the dictation stopped', async () => {
    const seen = await started()
    sr.listeners.partialResults({ matches: ['καλημέρα σε όλους'] })
    sr.listeners.error({ code: 'NETWORK', message: 'Network error' })
    await vi.waitFor(() => expect(seen.ended).toBe(1))
    expect(seen.errors).toEqual([DICTATION_LOST])
    expect(seen.finals).toEqual(['καλημέρα σε όλους'])
  })

  it('a network error before any word says it could not connect', async () => {
    const seen = await started()
    sr.listeners.error({ code: 'NETWORK_TIMEOUT', message: 'Network timeout' })
    await vi.waitFor(() => expect(seen.ended).toBe(1))
    expect(seen.errors).toEqual([DICTATION_NO_CONNECT])
  })

  it('a SERVER refusal before any word (a locale its service will not serve) says the same', async () => {
    const seen = await started()
    sr.listeners.error({ code: 'SERVER', message: 'Server error' })
    await vi.waitFor(() => expect(seen.ended).toBe(1))
    expect(seen.errors).toEqual([DICTATION_NO_CONNECT])
  })

  it('an utterance that simply ended keeps its words with no error', async () => {
    const seen = await started()
    sr.listeners.partialResults({ matches: ['ναι'] })
    sr.listeners.error({ code: 'NO_MATCH', message: 'No match' })
    await vi.waitFor(() => expect(seen.ended).toBe(1))
    expect(seen.errors).toEqual([])
    expect(seen.finals).toEqual(['ναι'])
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
    await session.start()
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
