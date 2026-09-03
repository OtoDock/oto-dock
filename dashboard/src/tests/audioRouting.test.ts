// ─── Android audio-routing fixes (1.5) ──────────────────────────────────────
// Chrome/WebView on Android flips the WHOLE app into MODE_IN_COMMUNICATION
// (in-call volume slider, quiet call-stream output) whenever a getUserMedia
// capture opens with the hardware AEC attached. Dictation has nothing to
// echo-cancel, so its captures must open with echoCancellation:false (NS/AGC
// stay on — they never trigger the flip and AGC keeps quiet speakers
// recognizable). Phone mode KEEPS AEC (open mic during playback); the user's
// call-volume slider is the loudness lever there. These tests pin the
// constraint contracts and the dictation interlocks.

import { afterEach, describe, expect, it, vi } from 'vitest'

import { installMicEnv } from './fakeMicEnv'
import { platformStt } from '@/audio/backends/platformStt'
import { ensureNativeMicPermission } from '@/audio/micPermission'
import {
  _resetSpeechActivity,
  isDictationActive,
  registerStoppablePlayback,
  setDictationActive,
  stopActivePlayback,
} from '@/audio/speechActivity'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = 0  // CONNECTING — never opens in this harness
  binaryType = ''
  onopen: (() => void) | null = null
  onmessage: unknown = null
  onclose: unknown = null
  onerror: unknown = null
  sent: unknown[] = []
  constructor(public url: string) { FakeWebSocket.instances.push(this) }
  send(data: unknown) { this.sent.push(data) }
  close() { this.readyState = FakeWebSocket.CLOSED }
  addEventListener() { /* noop */ }
  removeEventListener() { /* noop */ }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  _resetSpeechActivity()
})

describe('platform STT capture constraints', () => {
  it('opens the mic with echoCancellation OFF, NS/AGC ON', async () => {
    const mic = installMicEnv()
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({ ws_token: 'tok' }),
    })))

    const session = platformStt.create({ language: 'en' })
    session.onFinal = () => {}
    // start() blocks on the socket-open handshake, which never happens in
    // this harness — the constraint assertion only needs the getUserMedia
    // call, which precedes the socket. Never await the start.
    const started = session.start()
    void started.catch(() => {})
    await vi.waitFor(() => expect(mic.getUserMedia).toHaveBeenCalled())
    expect(mic.getUserMedia.mock.calls[0][0]).toEqual({
      audio: {
        echoCancellation: false,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })
    await session.stop()
  })
})

describe('native mic-permission primer', () => {
  it('primes with echoCancellation OFF when not yet granted', async () => {
    const mic = installMicEnv()
    ;(window as any).Capacitor = { isNativePlatform: () => true }
    Object.defineProperty(navigator, 'permissions', {
      value: { query: async () => ({ state: 'prompt' }) },
      configurable: true,
    })
    try {
      await ensureNativeMicPermission()
      expect(mic.getUserMedia).toHaveBeenCalledWith({
        audio: { echoCancellation: false },
      })
      // Grant-only: the stream is released immediately.
      expect(mic.tracks[0].stop).toHaveBeenCalled()
    } finally {
      delete (window as any).Capacitor
    }
  })

  it('opens NO capture at all once granted', async () => {
    const mic = installMicEnv()
    ;(window as any).Capacitor = { isNativePlatform: () => true }
    Object.defineProperty(navigator, 'permissions', {
      value: { query: async () => ({ state: 'granted' }) },
      configurable: true,
    })
    try {
      await ensureNativeMicPermission()
      expect(mic.getUserMedia).not.toHaveBeenCalled()
    } finally {
      delete (window as any).Capacitor
    }
  })
})

describe('dictation interlocks', () => {
  it('stopActivePlayback silences registered playbacks exactly once', () => {
    const stop = vi.fn()
    registerStoppablePlayback(stop)
    stopActivePlayback()
    stopActivePlayback()
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('an unregistered (naturally ended) playback is not re-stopped', () => {
    const stop = vi.fn()
    const unregister = registerStoppablePlayback(stop)
    unregister()
    stopActivePlayback()
    expect(stop).not.toHaveBeenCalled()
  })

  it('a throwing stopper never breaks the flush', () => {
    const bad = vi.fn(() => { throw new Error('boom') })
    const good = vi.fn()
    registerStoppablePlayback(bad)
    registerStoppablePlayback(good)
    stopActivePlayback()
    expect(good).toHaveBeenCalledTimes(1)
  })

  it('the dictation-active flag drives the chime suppression gate', () => {
    expect(isDictationActive()).toBe(false)
    setDictationActive(true)
    expect(isDictationActive()).toBe(true)
    setDictationActive(false)
    expect(isDictationActive()).toBe(false)
  })
})
