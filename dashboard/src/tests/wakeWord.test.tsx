import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { FakeAudioContext, installMicEnv, uninstallMicEnv } from './fakeMicEnv'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

import { acquireMic, releaseMic, micBusy, subscribeMic } from '@/audio/micCoordinator'
import { WakeWordSection } from '@/pages/UserSettings.general'
import { useWakeWord } from '@/hooks/useWakeWord'
import * as authApi from '@/api/auth'

vi.mock('@/hooks/useNotificationSound', () => ({
  useNotificationSound: () => ({ playPing: vi.fn(), playForSeverity: vi.fn(), stopDangerAlarm: vi.fn() }),
}))

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const CAP_DUPLEX_OK = {
  tts: 'either', stt: 'either', tts_provider_id: 1, stt_provider_id: 1,
  reason: 'ok', icons_enabled: true, duplex: { available: true, reason: 'ok' },
}

const KW_PAYLOAD = {
  enabled: true, reason: 'ok', threshold: 0.35,
  keywords: '▁HE Y ▁A L P HA @alpha\n▁HE Y ▁O T O ▁DO CK :2 #0.2 @alpha',
  agents: [{ slug: 'alpha', display_name: 'Alpha Helper', wakeable: true }],
  platform_target: 'alpha',
}

function mockApi(opts: { wakeEnabled?: boolean; duplex?: boolean; kw?: object } = {}) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.startsWith('/v1/users/me/audio-prefs')) {
      if (init?.method === 'PUT') {
        return { ok: true, json: async () => JSON.parse(String(init.body)) } as Response
      }
      return {
        ok: true,
        json: async () => ({ stt_mode: 'auto', tts_mode: 'auto', tts_voice_map: {},
          stt_language: null, wake_word_enabled: opts.wakeEnabled ?? false }),
      } as Response
    }
    if (path.startsWith('/v1/audio/capability')) {
      return {
        ok: true,
        json: async () => (opts.duplex === false
          ? { ...CAP_DUPLEX_OK, duplex: { available: false, reason: 'duplex engine not connected' } }
          : CAP_DUPLEX_OK),
      } as Response
    }
    if (path.startsWith('/v1/users/me/wake-keywords')) {
      return { ok: true, json: async () => (opts.kw ?? KW_PAYLOAD) } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

// ─── micCoordinator — the first mic arbiter ─────────────────────────────────

describe('micCoordinator', () => {
  afterEach(() => { releaseMic('a'); releaseMic('b') })

  it('busy while any owner holds; idempotent acquire/release', () => {
    expect(micBusy()).toBe(false)
    acquireMic('a'); acquireMic('a')
    expect(micBusy()).toBe(true)
    acquireMic('b'); releaseMic('a')
    expect(micBusy()).toBe(true) // b still holds
    releaseMic('b')
    expect(micBusy()).toBe(false)
  })

  it('notifies subscribers on ownership edges only', () => {
    const cb = vi.fn()
    const unsub = subscribeMic(cb)
    acquireMic('a')
    acquireMic('a') // no-op, no extra notify
    releaseMic('a')
    expect(cb).toHaveBeenCalledTimes(2)
    unsub()
    acquireMic('a')
    expect(cb).toHaveBeenCalledTimes(2)
    releaseMic('a')
  })
})

// ─── WakeWordSection (user settings, General tab) ───────────────────────────

describe('WakeWordSection', () => {
  afterEach(() => fetchSpy.mockReset())

  it('renders with Off active by default and saves opt-in on On', async () => {
    mockApi()
    render(<WakeWordSection />, { wrapper })
    await waitFor(() => expect(screen.getByText('Wake word')).toBeInTheDocument())
    const on = screen.getByRole('button', { name: 'On' })
    fireEvent.click(on)
    await waitFor(() => {
      const put = fetchSpy.mock.calls.find(
        ([p, i]) => String(p).startsWith('/v1/users/me/audio-prefs') && (i as RequestInit)?.method === 'PUT')
      expect(put).toBeTruthy()
      expect(JSON.parse(String((put![1] as RequestInit).body))).toEqual({ wake_word_enabled: true })
    })
  })

  it('when enabled, lists the platform word target and wakeable phrases', async () => {
    mockApi({ wakeEnabled: true })
    render(<WakeWordSection />, { wrapper })
    await waitFor(() =>
      expect(screen.getByText(/opens Alpha Helper \(your favorite agent\)/)).toBeInTheDocument())
    expect(screen.getByText(/“Hey Alpha Helper”/)).toBeInTheDocument()
  })

  it('surfaces duplex unavailability instead of silently arming', async () => {
    mockApi({ wakeEnabled: true, duplex: false })
    render(<WakeWordSection />, { wrapper })
    await waitFor(() =>
      expect(screen.getByText(/Voice conversations are currently unavailable/)).toBeInTheDocument())
    expect(screen.getByText(/duplex engine not connected/)).toBeInTheDocument()
  })
})

// ─── useWakeWord (app-root listener) ────────────────────────────────────────

function Harness({ navigate, entry }: { navigate: (to: string) => void; entry: string }) {
  function Probe() {
    useWakeWord(navigate as never, true)
    return <div>probe</div>
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

class FakeWorker {
  static instances: FakeWorker[] = []
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onerror: ((e: { message: string }) => void) | null = null
  posted: unknown[] = []
  constructor(public url: string) { FakeWorker.instances.push(this) }
  postMessage(msg: unknown) { this.posted.push(msg) }
  terminate() { /* noop */ }
}

describe('useWakeWord', () => {
  beforeEach(() => { FakeWorker.instances = [] })
  afterEach(() => {
    fetchSpy.mockReset()
    vi.unstubAllGlobals()
  })

  it('is inert in an unsupported environment (jsdom has no AudioContext)', async () => {
    mockApi({ wakeEnabled: true })
    const navigate = vi.fn()
    render(<Harness navigate={navigate} entry="/agents" />)
    await waitFor(() => expect(screen.getByText('probe')).toBeInTheDocument())
    expect(navigate).not.toHaveBeenCalled()
  })

  it('?wakeDebug=<slug> triggers the full wake action without a mic', async () => {
    mockApi()
    const navigate = vi.fn()
    render(<Harness navigate={navigate} entry="/agents?wakeDebug=alpha" />)
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat/alpha?wake=1'))
    // The detection-time pre-warm fired fire-and-forget (never gating the
    // navigate — that stays synchronous with the detection).
    const prewarm = fetchSpy.mock.calls.find(
      ([path]) => path === '/v1/duplex/prewarm')
    expect(prewarm).toBeTruthy()
    expect(prewarm?.[1]?.method).toBe('POST')
    expect(JSON.parse(String(prewarm?.[1]?.body))).toEqual({ agent: 'alpha' })
  })

  it('spawns the worker when every gate passes and navigates on detection', async () => {
    mockApi({ wakeEnabled: true })
    const navigate = vi.fn()
    // Fake full browser support.
    vi.stubGlobal('Worker', FakeWorker as unknown as typeof Worker)
    const mic = installMicEnv()
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
    try {
      render(<Harness navigate={navigate} entry="/agents" />)
      // No prior mic owner → the listener starts with no resume delay.
      await waitFor(() => expect(FakeWorker.instances.length).toBe(1), { timeout: 4000 })
      const worker = FakeWorker.instances[0]
      expect(worker.url).toBe('/wake-word-worker.js')
      const init = worker.posted.find((m) => (m as { type: string }).type === 'init') as
        { keywords: string; threshold: number } | undefined
      expect(init?.keywords).toContain('@alpha')
      expect(init?.threshold).toBe(0.35)

      // The wake mic deliberately drops NS/AGC (fresh-stream convergence
      // mangled the "Hey" onset — the "say it twice" bug) and keeps AEC
      // (self-wake guard during chat-audio replay).
      await waitFor(() => expect(mic.getUserMedia).toHaveBeenCalled(), { timeout: 4000 })
      expect(mic.getUserMedia).toHaveBeenCalledWith({
        audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: false },
      })

      worker.onmessage?.({ data: { type: 'detect', keyword: 'alpha' } })
      expect(navigate).toHaveBeenCalledWith('/chat/alpha?wake=1')
    } finally {
      uninstallMicEnv()
    }
  })

  it('waits 300ms after a non-dictation owner releases the mic', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('Worker', FakeWorker as unknown as typeof Worker)
    const mic = installMicEnv()
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
    mockApi({ wakeEnabled: true })
    acquireMic('duplex')
    try {
      render(<Harness navigate={vi.fn()} entry="/agents" />)
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })
      expect(mic.getUserMedia).not.toHaveBeenCalled()
      act(() => releaseMic('duplex'))
      await act(async () => { await vi.advanceTimersByTimeAsync(299) })
      expect(mic.getUserMedia).not.toHaveBeenCalled()
      await act(async () => { await vi.advanceTimersByTimeAsync(2) })
      expect(mic.getUserMedia).toHaveBeenCalledTimes(1)
    } finally {
      releaseMic('duplex')
      vi.useRealTimers()
      uninstallMicEnv()
    }
  })

  it('watchdog resumes a suspended AudioContext on foreground return', async () => {
    mockApi({ wakeEnabled: true })
    vi.stubGlobal('Worker', FakeWorker as unknown as typeof Worker)
    const mic = installMicEnv()
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
    try {
      render(<Harness navigate={vi.fn()} entry="/agents" />)
      await waitFor(() => expect(mic.getUserMedia).toHaveBeenCalled(), { timeout: 4000 })
      await waitFor(() => {
        expect(FakeAudioContext.instances.length).toBe(1)
        expect(FakeAudioContext.instances[0].onstatechange).toBeTruthy()
      })
      const ctx = FakeAudioContext.instances[0]
      ctx.resume.mockClear()
      ctx.state = 'suspended'
      // Android MainActivity's onResume dispatch — the reliable foreground
      // signal for short screen cycles.
      act(() => { window.dispatchEvent(new Event('otodock:force-health-check')) })
      expect(ctx.resume).toHaveBeenCalled()
      // Still suspended after the grace → full capture cycle on a fresh
      // context (the old one closed, a second one opened).
      await waitFor(() => {
        expect(ctx.close).toHaveBeenCalled()
        expect(FakeAudioContext.instances.length).toBe(2)
      }, { timeout: 2000 })
    } finally {
      uninstallMicEnv()
    }
  })

  it('keeps the engine warm but never opens the mic while another owner holds it', async () => {
    mockApi({ wakeEnabled: true })
    vi.stubGlobal('Worker', FakeWorker as unknown as typeof Worker)
    vi.stubGlobal('AudioContext', class { sampleRate = 48000 })
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
    const getUserMedia = vi.fn(async () => ({ getTracks: () => [] }))
    Object.defineProperty(navigator, 'mediaDevices', {
      value: { getUserMedia },
      configurable: true,
    })
    acquireMic('duplex')
    try {
      const navigate = vi.fn()
      render(<Harness navigate={navigate} entry="/agents" />)
      // Engine boots regardless (it persists across mic pauses)…
      await waitFor(() => expect(FakeWorker.instances.length).toBe(1), { timeout: 4000 })
      // …but capture must not start while the mic has another owner.
      // (Plain settle sleep — `capturing` is false here so the capture
      // effect never runs at all; this is NOT the resume grace elapsing.)
      await new Promise((r) => setTimeout(r, 50))
      expect(getUserMedia).not.toHaveBeenCalled()
    } finally {
      releaseMic('duplex')
    }
  })
})
