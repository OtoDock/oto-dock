import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useRef, useState } from 'react'
import { useSpeechSession } from '@/hooks/useSpeechSession'
import { useDictationDraft } from '@/hooks/useDictationDraft'
import type { STTSession } from '@/audio/types'
import { DICTATION_FINISHING } from '@/audio/backends/dictationWords'

// ─── useSpeechSession (lifecycle; backend faked at the resolver) ─────────────

vi.mock('@/hooks/useChatAudioCapability', () => ({
  useChatAudioCapability: () => ({ data: { stt: 'platform', stt_provider_id: 1 } }),
}))
vi.mock('@/api/userAudio', () => ({
  useMyAudioPrefs: () => ({ data: { stt_mode: 'auto', stt_language: 'en' } }),
}))
vi.mock('@/audio/backends/platformStt', () => ({
  platformStt: { kind: 'platform', isAvailable: () => false, create: () => { throw new Error('unused') } },
  STOP_FINAL_WAIT_MS: 10000,
  STOP_BACKLOG_MAX_MS: 20000,
}))

interface FakeSession {
  session: STTSession
  emit: {
    partial: (t: string) => void; final: (t: string) => void; replace: (t: string) => void; end: () => void
    error: (m: string) => void; notice: (t: string | null) => void
  }
  resolveStart: () => void
  stopSpy: ReturnType<typeof vi.fn>
  resolveStop: () => void
  finishing: () => void
}

// A test sets this before the session is created: its stop() then waits
// for resolveStop (the server's last final still on its way).
let deferStop = false

// A start after a stop waits for that stop (a few microtasks for a fake
// whose stop resolves at once).
const settle = () => act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve() })

let fakes: FakeSession[] = []

function makeFakeSession(): FakeSession {
  const h = {
    partial: (_: string) => {}, final: (_: string) => {}, replace: (_: string) => {}, error: (_: Error) => {}, end: () => {},
    notice: (_: string | null) => {}, finishing: () => {},
  }
  let resolveStart = () => {}
  let resolveStop = () => {}
  const deferred = deferStop
  const stopSpy = vi.fn(() => deferred ? new Promise<void>((res) => { resolveStop = res }) : Promise.resolve())
  const session: STTSession = {
    start: () => new Promise<void>((res) => { resolveStart = res }),
    stop: stopSpy,
    onPartial: (f) => { h.partial = f },
    onFinal: (f) => { h.final = f },
    onReplace: (f) => { h.replace = f },
    onError: (f) => { h.error = f },
    onEnd: (f) => { h.end = f },
    onNotice: (f) => { h.notice = f },
    onFinishing: (f) => { h.finishing = f },
  }
  const fake: FakeSession = {
    session,
    emit: {
      partial: (t) => h.partial(t), final: (t) => h.final(t), replace: (t) => h.replace(t), end: () => h.end(),
      error: (m) => h.error(new Error(m)), notice: (t) => h.notice(t),
    },
    resolveStart: () => resolveStart(),
    stopSpy,
    resolveStop: () => resolveStop(),
    finishing: () => h.finishing(),
  }
  fakes.push(fake)
  return fake
}

vi.mock('@/audio/resolver', () => ({
  resolveSttBackend: () => ({ kind: 'platform', isAvailable: () => true, create: () => makeFakeSession().session }),
}))

describe('useSpeechSession', () => {
  beforeEach(() => {
    fakes = []
    deferStop = false
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('a replace from the engine rewrites the session\'s words, and a discarded session drops a late one', async () => {
    const onFinal = vi.fn()
    const onReplace = vi.fn()
    const onCommit = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onFinal, onReplace, onCommit }))
    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { fakes[0].emit.final('yes sir'); fakes[0].emit.replace('Ναι κύριε') })
    expect(onReplace).toHaveBeenCalledWith('Ναι κύριε')
    act(() => { fakes[0].emit.end() })
    expect(onCommit).toHaveBeenCalledWith('Ναι κύριε')  // the commit carries the replaced words

    act(() => { void result.current.start() })
    act(() => { fakes[1].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { result.current.stop(true) })            // Send took the text
    act(() => { fakes[1].emit.replace('late words') })
    expect(onReplace).toHaveBeenCalledTimes(1)
  })

  it('a slow link\'s notice reaches the composer from the current session only, and clears at the stop', async () => {
    const onNotice = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onNotice }))
    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { fakes[0].emit.notice('The connection is slow.') })
    expect(onNotice).toHaveBeenLastCalledWith('The connection is slow.')
    act(() => { result.current.stop() })
    expect(onNotice).toHaveBeenLastCalledWith(DICTATION_FINISHING)   // its last words on the way
    await settle()
    expect(onNotice).toHaveBeenLastCalledWith(null)
    act(() => { fakes[0].emit.notice('late') })          // a stopped session says nothing more
    expect(onNotice).not.toHaveBeenCalledWith('late')
  })

  it('a stopped session\'s late error never lands over the next recording', async () => {
    const onError = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError }))
    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { result.current.stop() })
    act(() => { void result.current.start() })
    await settle()
    act(() => { fakes[1].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { fakes[0].emit.error('The connection was lost.') })
    expect(onError).not.toHaveBeenCalled()
    expect(result.current.status).toBe('recording')
    act(() => { fakes[1].emit.error('The connection was lost.') })
    expect(onError).toHaveBeenCalledWith('The connection was lost.')
  })

  it('a tap right after a stop waits for the stopped dictation\'s last words: one copy in the box', async () => {
    deferStop = true
    const { result } = renderHook(() => {
      const [text, setTextState] = useState('')
      const valueRef = useRef('')
      const keyRef = useRef<string | undefined>('chat-a')
      const setText = (t: string) => { valueRef.current = t; setTextState(t) }
      const draft = useDictationDraft({ valueRef, draftKeyRef: keyRef, setText, onStart: () => {}, afterChange: () => {} })
      const speech = useSpeechSession({
        onInterim: draft.showInterim, onFinal: draft.appendTranscript,
        onReplace: draft.replaceTranscript, onActive: draft.onMicActive,
      })
      return { text, speech }
    })
    act(() => { void result.current.speech.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { fakes[0].emit.partial('ένα δύο τρία') })
    expect(result.current.text).toBe('ένα δύο τρία')
    act(() => { result.current.speech.stop() })            // the server still owes the last final
    act(() => { void result.current.speech.start() })      // and the person taps at once
    expect(result.current.speech.status).toBe('connecting')
    expect(fakes.length).toBe(1)                            // no new attempt yet: no snapshot taken
    act(() => { fakes[0].emit.final('ένα δύο τρία') })     // the last words land
    act(() => { fakes[0].resolveStop() })
    await settle()
    expect(fakes.length).toBe(2)
    act(() => { fakes[1].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { fakes[1].emit.final('τέσσερα') })
    expect(result.current.text).toBe('ένα δύο τρία τέσσερα')
  })

  it('a tap after a stop on a slow uplink still waits past ten seconds for the last words', async () => {
    deferStop = true
    const { result } = renderHook(() => {
      const [text, setTextState] = useState('')
      const valueRef = useRef('')
      const keyRef = useRef<string | undefined>('chat-a')
      const setText = (t: string) => { valueRef.current = t; setTextState(t) }
      const draft = useDictationDraft({ valueRef, draftKeyRef: keyRef, setText, onStart: () => {}, afterChange: () => {} })
      const speech = useSpeechSession({
        onInterim: draft.showInterim, onFinal: draft.appendTranscript,
        onReplace: draft.replaceTranscript, onActive: draft.onMicActive,
      })
      return { text, speech }
    })
    act(() => { void result.current.speech.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { fakes[0].emit.partial('ένα δύο τρία') })
    act(() => { result.current.speech.stop() })
    await act(async () => { await vi.advanceTimersByTimeAsync(12_000) })   // past the plain bound
    act(() => { void result.current.speech.start() })
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000) })
    expect(result.current.speech.status).toBe('connecting')
    expect(fakes.length).toBe(1)                            // still waiting: no snapshot yet
    act(() => { fakes[0].emit.final('ένα δύο τρία') })     // the backlog's last words, 15 s in
    act(() => { fakes[0].resolveStop() })
    await settle()
    expect(fakes.length).toBe(2)
    act(() => { fakes[1].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { fakes[1].emit.final('τέσσερα') })
    expect(result.current.text).toBe('ένα δύο τρία τέσσερα')
  })

  it('a plain stop says it is finishing until its last words land, the composer free at once', async () => {
    deferStop = true
    const onNotice = vi.fn()
    const onActive = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onNotice, onActive }))
    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { result.current.stop() })
    expect(result.current.status).toBe('finishing')
    expect(onNotice).toHaveBeenLastCalledWith(DICTATION_FINISHING)
    expect(onActive).toHaveBeenLastCalledWith(false)        // edits are the person's again at once
    act(() => { result.current.stop() })                    // a focus while it finishes: nothing to stop
    expect(result.current.status).toBe('finishing')
    act(() => { fakes[0].resolveStop() })
    await settle()
    expect(result.current.status).toBe('idle')
    expect(onNotice).toHaveBeenLastCalledWith(null)
  })

  it('the finishing state ends at the bound when the last words never come', async () => {
    deferStop = true
    const onNotice = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onNotice }))
    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { result.current.stop() })
    await act(async () => { await vi.advanceTimersByTimeAsync(29_000) })
    expect(result.current.status).toBe('finishing')
    await act(async () => { await vi.advanceTimersByTimeAsync(1_100) })
    expect(result.current.status).toBe('idle')
    expect(onNotice).toHaveBeenLastCalledWith(null)
  })

  it('a tap while the last words are on the way starts the next dictation, which waits for them', async () => {
    deferStop = true
    const onNotice = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onNotice }))
    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { result.current.stop() })
    act(() => { result.current.toggle() })
    expect(result.current.status).toBe('connecting')
    act(() => { fakes[0].resolveStop() })
    await settle()
    expect(fakes.length).toBe(2)
    expect(onNotice).toHaveBeenLastCalledWith(null)        // the finishing note gives way
    act(() => { fakes[1].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    expect(result.current.status).toBe('recording')
  })

  it('a session the server ends leaves recording for finishing until its words land', async () => {
    const onError = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onError }))
    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    act(() => { fakes[0].finishing() })
    expect(result.current.status).toBe('finishing')
    act(() => { fakes[0].emit.error('Dictation reached its time limit and stopped.') })
    expect(result.current.status).toBe('idle')
    expect(onError).toHaveBeenCalledWith('Dictation reached its time limit and stopped.')
  })

  it('a timed-out start leaves NO zombie — late finals are dropped, the session is stopped', async () => {
    // Regression (2026-07-16): withTimeout rejected but the underlying start
    // kept running; its late 'ready' re-attached the mic and every sentence
    // of the NEXT session landed twice (paired 60s max_seconds usage rows).
    const onFinal = vi.fn()
    const onError = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onFinal, onError }))

    act(() => { void result.current.start() })
    await act(async () => { await vi.advanceTimersByTimeAsync(8_100) }) // past CONNECT_TIMEOUT_MS
    expect(onError).toHaveBeenCalled()
    expect(result.current.status).toBe('idle')
    expect(fakes[0].stopSpy).toHaveBeenCalled() // the mic/socket is released

    // The zombie wakes up late: ready + a transcript. It must go nowhere.
    act(() => {
      fakes[0].resolveStart()
      fakes[0].emit.final('hello from the zombie')
    })
    expect(onFinal).not.toHaveBeenCalled()

    // A fresh session works, and only IT delivers finals.
    act(() => { void result.current.start() })
    act(() => { fakes[1].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) }) // MIN_CONNECTING_MS floor
    expect(result.current.status).toBe('recording')
    act(() => { fakes[1].emit.final('real sentence') })
    expect(onFinal).toHaveBeenCalledTimes(1)
    expect(onFinal).toHaveBeenCalledWith('real sentence')
  })

  it('stop while connecting leaves NO zombie — the session is killed when start settles', async () => {
    // Regression (2026-07-16, second window): stop() during an in-flight
    // start() had nothing to tear down (mic/socket not created yet), so the
    // session came up in the BACKGROUND while the UI showed idle — each
    // retry stacked another live recorder and sentences duplicated N times.
    const onFinal = vi.fn()
    const onError = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onFinal, onError }))

    act(() => { void result.current.start() })
    expect(result.current.status).toBe('connecting')
    // Second click mid-connect: toggle routes to stop() while start is in flight.
    act(() => { result.current.stop() })
    expect(result.current.status).toBe('idle')
    // The backend start completes late anyway (resources came up in background).
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    // Post-await guard: never flips to recording, and stop() ran again now
    // that the session's resources actually exist.
    expect(result.current.status).toBe('idle')
    expect(fakes[0].stopSpy.mock.calls.length).toBeGreaterThanOrEqual(2)
    // Whatever it heard goes nowhere, and a user stop is not an error.
    act(() => { fakes[0].emit.final('background zombie words') })
    expect(onFinal).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it("a stopped session's late end never clobbers its successor (stale-guard)", async () => {
    // Regression (2026-07-16, third window — the common one): stopping a
    // session makes the server flush the final before its socket closes, so
    // its end event lands ~1.5s LATE — usually while the NEXT session is
    // connecting. The stale handler reset status to idle and cleared
    // sessionRef: the new session came up with a dead icon while recording
    // in the background.
    const onFinal = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onFinal }))
    // Session A runs normally, user stops it.
    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    expect(result.current.status).toBe('recording')
    act(() => { result.current.stop() })
    // Session B starts immediately; A's close event lands mid-connect.
    act(() => { void result.current.start() })
    expect(result.current.status).toBe('connecting')
    act(() => { fakes[0].emit.end() })
    expect(result.current.status).toBe('connecting') // NOT knocked back to idle
    await settle()                                     // A's stop resolved: B is created
    // B completes and is fully functional.
    act(() => { fakes[1].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    expect(result.current.status).toBe('recording')
    act(() => { fakes[1].emit.final('works') })
    expect(onFinal).toHaveBeenCalledWith('works')
  })

  it('a user-stopped session still delivers its tail final (the stop flush)', async () => {
    const onFinal = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onFinal }))

    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    expect(result.current.status).toBe('recording')

    act(() => { result.current.stop() })
    // The server's stop-flush final arrives after stop() — it must commit.
    act(() => { fakes[0].emit.final('tail sentence') })
    expect(onFinal).toHaveBeenCalledWith('tail sentence')
  })

  it('stop(true) discards the tail final (send already took the text)', async () => {
    const onFinal = vi.fn()
    const { result } = renderHook(() => useSpeechSession({ onFinal }))

    act(() => { void result.current.start() })
    act(() => { fakes[0].resolveStart() })
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    expect(result.current.status).toBe('recording')

    act(() => { result.current.stop(true) })
    // The stop-flush final still arrives — but Send consumed the input, so
    // delivering it would re-fill the cleared composer. It must be dropped.
    act(() => { fakes[0].emit.final('tail sentence') })
    expect(onFinal).not.toHaveBeenCalled()
  })
})
