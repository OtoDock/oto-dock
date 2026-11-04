import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSpeechSession } from '@/hooks/useSpeechSession'

import { AUTO_LANGUAGE, isAutoLanguage } from '@/audio/lang'
import type { STTSession } from '@/audio/types'
import { currentSttLanguage, STT_MENU_OPTIONS } from '@/lib/chat/sttLanguages'
import { DICTATION_DEVICE_FALLBACK, DICTATION_IDLE, DICTATION_NO_CONNECT, DICTATION_NO_MIC, DICTATION_NOT_ALLOWED } from '@/audio/backends/dictationWords'

// ─── The engine a dictation starts on, and the language it gets ─────────────
// The Automatic language, stored as "auto": the platform engine gets it and
// identifies the language; the device recognizer needs a code and gets the
// device's own language. A device recognizer that fails before a word, under
// mode auto on an either capability, hands the attempt to the platform once
// with the notice; after words, on an explicit device pick, or on a failure
// the platform shares, the error shows. A device pick the resolver could not
// honour says the notice at the start, only where the device has no
// recognizer (never under Platform only with one).

const prefs = vi.hoisted(() => ({ stt_mode: 'auto' as string, stt_language: 'auto' as string | null }))
const picked = vi.hoisted(() => ({ kind: 'native' as 'native' | 'platform' }))
// The capability the hook reads, and the one a tap before it fetches.
const capability = vi.hoisted(() => ({
  data: { stt: 'either', stt_provider_id: 1 } as { stt: string; stt_provider_id: number } | undefined,
  later: { stt: 'either', stt_provider_id: 1 } as { stt: string; stt_provider_id: number } | undefined,
  fetches: 0,
}))

vi.mock('@/hooks/useChatAudioCapability', () => ({
  useChatAudioCapability: () => ({
    data: capability.data,
    refetch: async () => { capability.fetches += 1; capability.data = capability.later; return { data: capability.later } },
  }),
}))
vi.mock('@/api/userAudio', () => ({
  useMyAudioPrefs: () => ({ data: prefs }),
}))

interface Fake {
  language: string
  session: STTSession
  emit: { partial: (t: string) => void; final: (t: string) => void; end: () => void; error: (m: string) => void }
  resolveStart: () => void
  rejectStart: (message: string) => void
  stop: ReturnType<typeof vi.fn>
}
const made = vi.hoisted(() => ({ device: [] as Fake[], platform: [] as Fake[] }))

function fake(language: string): Fake {
  const h = { partial: (_: string) => {}, final: (_: string) => {}, error: (_: Error) => {}, end: () => {} }
  let resolveStart = () => {}
  let rejectStart = (_: Error) => {}
  const stop = vi.fn(() => Promise.resolve())
  const session: STTSession = {
    start: () => new Promise<void>((res, rej) => { resolveStart = res; rejectStart = rej }),
    stop,
    onPartial: (f) => { h.partial = f },
    onFinal: (f) => { h.final = f },
    onError: (f) => { h.error = f },
    onEnd: (f) => { h.end = f },
  }
  return {
    language,
    session,
    emit: { partial: (t) => h.partial(t), final: (t) => h.final(t), end: () => h.end(), error: (m) => h.error(new Error(m)) },
    resolveStart: () => resolveStart(),
    rejectStart: (message) => rejectStart(new Error(message)),
    stop,
  }
}

vi.mock('@/audio/backends/platformStt', () => ({
  platformStt: {
    kind: 'platform',
    isAvailable: () => true,
    create: (opts: { language: string }) => { const f = fake(opts.language); made.platform.push(f); return f.session },
  },
  STOP_FINAL_WAIT_MS: 10000,
  STOP_BACKLOG_MAX_MS: 20000,
}))
vi.mock('@/audio/resolver', async () => {
  const platform = await import('@/audio/backends/platformStt')
  return {
    resolveSttBackend: () => picked.kind === 'platform' ? platform.platformStt : {
      kind: 'native',
      isAvailable: () => true,
      create: (opts: { language: string }) => { const f = fake(opts.language); made.device.push(f); return f.session },
    },
  }
})

const connect = () => act(async () => { await vi.advanceTimersByTimeAsync(600) })
const started = () => [...made.device, ...made.platform]

beforeEach(() => {
  made.device = []
  made.platform = []
  prefs.stt_mode = 'auto'
  prefs.stt_language = 'auto'
  picked.kind = 'native'
  capability.data = { stt: 'either', stt_provider_id: 1 }
  capability.later = { stt: 'either', stt_provider_id: 1 }
  capability.fetches = 0
  Object.defineProperty(navigator, 'language', { value: 'el-GR', configurable: true })
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('the Automatic dictation language', () => {
  it('is its own menu row, first, and ticks when stored', () => {
    expect(STT_MENU_OPTIONS[0]).toEqual({ value: AUTO_LANGUAGE, label: 'Automatic' })
    expect(currentSttLanguage('auto')).toBe(AUTO_LANGUAGE)
    expect(isAutoLanguage(' AUTO ')).toBe(true)
    // No pick keeps the browser default row, not Automatic.
    expect(currentSttLanguage(null)).toBe('el-GR')
    expect(currentSttLanguage('en-GB')).toBe('en-GB')
  })

  it('reaches the platform engine as "auto"', async () => {
    picked.kind = 'platform'
    const { result } = renderHook(() => useSpeechSession({}))
    act(() => { void result.current.start() })
    expect(started()[0].language).toBe('auto')
  })

  it('is the device\'s own language on the device recognizer', async () => {
    const { result } = renderHook(() => useSpeechSession({}))
    act(() => { void result.current.start() })
    expect(started()[0].language).toBe('el-GR')
  })

  it('a person who never picked gets the browser code on the platform and the device its own language', async () => {
    prefs.stt_language = null
    picked.kind = 'platform'
    const { result } = renderHook(() => useSpeechSession({}))
    act(() => { void result.current.start() })
    expect(started()[0].language).toBe('el')
    picked.kind = 'native'
    const { result: device } = renderHook(() => useSpeechSession({}))
    act(() => { void device.current.start() })
    expect(made.device[0].language).toBe('el-GR')   // the whole tag: WebKit refuses a bare code
  })
})

describe('the microphone before the capability answers', () => {
  it('shows where a device recognizer is present, and a tap waits for the answer', async () => {
    capability.data = undefined
    vi.stubGlobal('SpeechRecognition', class { start() {} stop() {} abort() {} })
    const onError = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError }))
    expect(result.current.available).toBe(true)
    act(() => { void result.current.start() })
    expect(result.current.status).toBe('connecting')
    expect(made.device).toHaveLength(0)
    await act(async () => { await Promise.resolve() })
    await act(async () => { await Promise.resolve() })
    expect(capability.fetches).toBe(1)
    expect(made.device).toHaveLength(1)
    act(() => { made.device[0].resolveStart() })
    await connect()
    expect(result.current.status).toBe('recording')
    expect(onError).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('hides without one, and a fetch that fails says the microphone is unavailable', async () => {
    capability.data = undefined
    const { result } = renderHook(() => useSpeechSession({}))
    expect(result.current.available).toBe(false)
    capability.later = undefined
    const onError = vi.fn()
    const second = renderHook(() => useSpeechSession({ onError }))
    act(() => { void second.result.current.start() })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(onError).toHaveBeenCalledWith('Microphone unavailable.')
    expect(second.result.current.status).toBe('idle')
    expect(made.device).toHaveLength(0)
  })
})

describe('the platform takes over a device dictation that fails before a word', () => {
  beforeEach(() => { prefs.stt_language = 'en' })

  it('an error right after the device start: the notice, the platform session, one commit', async () => {
    const onError = vi.fn(); const onNotice = vi.fn(); const onCommit = vi.fn(); const onActive = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError, onNotice, onCommit, onActive }))
    act(() => { void result.current.start() })
    act(() => { made.device[0].resolveStart() })
    await connect()
    expect(result.current.status).toBe('recording')
    act(() => { made.device[0].emit.error(DICTATION_NO_CONNECT) })
    expect(onNotice).toHaveBeenLastCalledWith(DICTATION_DEVICE_FALLBACK)
    expect(result.current.status).toBe('connecting')
    expect(made.device[0].stop).toHaveBeenCalled()
    expect(made.platform).toHaveLength(1)
    act(() => { made.device[0].emit.end() })          // the device session's end commits nothing
    expect(onCommit).not.toHaveBeenCalled()
    act(() => { made.platform[0].resolveStart() })
    await connect()
    expect(result.current.status).toBe('recording')
    act(() => { made.platform[0].emit.final('hello'); made.platform[0].emit.end() })
    expect(onCommit).toHaveBeenCalledTimes(1)
    expect(onCommit).toHaveBeenCalledWith('hello')
    expect(onError).not.toHaveBeenCalled()
    expect(onActive.mock.calls.filter(([a]) => a === true)).toHaveLength(1)   // one attempt, one snapshot
  })

  it('a device start that is refused falls back the same way', async () => {
    // WebKit refuses a start before any event (an unsupported locale, a
    // recognizer that is unavailable): the catch path hands over too.
    const onError = vi.fn(); const onNotice = vi.fn(); const onCommit = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError, onNotice, onCommit }))
    act(() => { void result.current.start() })
    await act(async () => { made.device[0].rejectStart(DICTATION_NO_CONNECT); await Promise.resolve() })
    expect(onNotice).toHaveBeenLastCalledWith(DICTATION_DEVICE_FALLBACK)
    expect(made.platform).toHaveLength(1)
    act(() => { made.platform[0].resolveStart() })
    await connect()
    expect(result.current.status).toBe('recording')
    act(() => { made.platform[0].emit.final('words'); made.platform[0].emit.end() })
    expect(onCommit).toHaveBeenCalledWith('words')
    expect(onError).not.toHaveBeenCalled()
  })

  it('the microphone refused is not retried on the platform either', async () => {
    const onError = vi.fn(); const onNotice = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError, onNotice }))
    act(() => { void result.current.start() })
    act(() => { made.device[0].resolveStart() })
    await connect()
    act(() => { made.device[0].emit.error(DICTATION_NOT_ALLOWED) })
    expect(onError).toHaveBeenCalledWith(DICTATION_NOT_ALLOWED)
    expect(onNotice).not.toHaveBeenCalledWith(DICTATION_DEVICE_FALLBACK)
    expect(made.platform).toHaveLength(0)
  })

  it('after words the error shows and nothing takes over', async () => {
    const onError = vi.fn(); const onNotice = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError, onNotice }))
    act(() => { void result.current.start() })
    act(() => { made.device[0].resolveStart() })
    await connect()
    act(() => { made.device[0].emit.final('some words'); made.device[0].emit.error(DICTATION_NO_CONNECT) })
    expect(onError).toHaveBeenCalledWith(DICTATION_NO_CONNECT)
    expect(onNotice).not.toHaveBeenCalledWith(DICTATION_DEVICE_FALLBACK)
    expect(made.platform).toHaveLength(0)
    expect(result.current.status).toBe('idle')
  })

  it('an explicit device pick keeps its error', async () => {
    prefs.stt_mode = 'native'
    const onError = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError }))
    act(() => { void result.current.start() })
    act(() => { made.device[0].resolveStart() })
    await connect()
    act(() => { made.device[0].emit.error(DICTATION_NO_CONNECT) })
    expect(onError).toHaveBeenCalledWith(DICTATION_NO_CONNECT)
    expect(made.platform).toHaveLength(0)
  })

  it('a minute of silence on the device is not a failure the platform takes over', async () => {
    const onError = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError }))
    act(() => { void result.current.start() })
    act(() => { made.device[0].resolveStart() })
    await connect()
    act(() => { made.device[0].emit.error(DICTATION_IDLE) })
    expect(onError).toHaveBeenCalledWith(DICTATION_IDLE)
    expect(made.platform).toHaveLength(0)
    expect(result.current.status).toBe('idle')
  })

  it('a failure the platform shares, no microphone, is not retried there', async () => {
    const onError = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError }))
    act(() => { void result.current.start() })
    act(() => { made.device[0].resolveStart() })
    await connect()
    act(() => { made.device[0].emit.error(DICTATION_NO_MIC) })
    expect(onError).toHaveBeenCalledWith(DICTATION_NO_MIC)
    expect(made.platform).toHaveLength(0)
  })

  it('a device pick the resolver could not honour says the notice as the platform starts', async () => {
    prefs.stt_mode = 'native'
    picked.kind = 'platform'
    const onNotice = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onNotice }))
    act(() => { void result.current.start() })
    expect(made.platform).toHaveLength(1)
    expect(onNotice).toHaveBeenLastCalledWith(DICTATION_DEVICE_FALLBACK)
    act(() => { made.platform[0].resolveStart() })
    await connect()
    expect(result.current.status).toBe('recording')
  })

  it('a device pick under Platform only, with a recognizer present, says nothing', async () => {
    // The policy gave the platform: the device could listen, so the notice
    // that it cannot would be untrue.
    prefs.stt_mode = 'native'
    picked.kind = 'platform'
    capability.data = { stt: 'platform', stt_provider_id: 1 }
    vi.stubGlobal('SpeechRecognition', class { start() {} stop() {} abort() {} })
    const onNotice = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onNotice }))
    act(() => { void result.current.start() })
    expect(made.platform).toHaveLength(1)
    expect(onNotice).not.toHaveBeenCalledWith(DICTATION_DEVICE_FALLBACK)
    act(() => { made.platform[0].resolveStart() })
    await connect()
    expect(result.current.status).toBe('recording')
    vi.unstubAllGlobals()
  })
})
