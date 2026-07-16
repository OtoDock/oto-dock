import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSpeechSession } from '@/hooks/useSpeechSession'
import type { STTSession } from '@/audio/types'

// ─── useSpeechSession (lifecycle; backend faked at the resolver) ─────────────

vi.mock('@/hooks/useChatAudioCapability', () => ({
  useChatAudioCapability: () => ({ data: { stt: 'platform', stt_provider_id: 1 } }),
}))
vi.mock('@/api/userAudio', () => ({
  useMyAudioPrefs: () => ({ data: { stt_mode: 'auto', stt_language: 'en' } }),
}))
vi.mock('@/audio/backends/platformStt', () => ({
  platformStt: { kind: 'platform', isAvailable: () => false, create: () => { throw new Error('unused') } },
}))

interface FakeSession {
  session: STTSession
  emit: { partial: (t: string) => void; final: (t: string) => void; end: () => void }
  resolveStart: () => void
  stopSpy: ReturnType<typeof vi.fn>
}

let fakes: FakeSession[] = []

function makeFakeSession(): FakeSession {
  const h = {
    partial: (_: string) => {}, final: (_: string) => {}, error: (_: Error) => {}, end: () => {},
  }
  let resolveStart = () => {}
  const stopSpy = vi.fn(async () => {})
  const session: STTSession = {
    start: () => new Promise<void>((res) => { resolveStart = res }),
    stop: stopSpy,
    onPartial: (f) => { h.partial = f },
    onFinal: (f) => { h.final = f },
    onError: (f) => { h.error = f },
    onEnd: (f) => { h.end = f },
  }
  const fake: FakeSession = {
    session,
    emit: { partial: (t) => h.partial(t), final: (t) => h.final(t), end: () => h.end() },
    resolveStart: () => resolveStart(),
    stopSpy,
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
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
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
})
