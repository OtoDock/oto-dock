import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

import {
  _resetWakeDiagForTests, encodeWav, exportWakeDiag, getWakeDiagSnapshot, isWakeDiagOn,
  saveWakeDiag, setWakeDiag, wakeDiagContext, wakeDiagEvent, wakeDiagFrames, wakeDiagListening,
  wakeDiagPcm, WAKE_DIAG_RATE, WAKE_DIAG_RING_SECONDS,
} from '@/audio/wakeDiag'
import WakeDiagBadge from '@/components/WakeDiagBadge'

function ramp(n: number, start: number): Float32Array {
  const out = new Float32Array(n)
  for (let i = 0; i < n; i++) out[i] = ((start + i) % 1000) / 1000
  return out
}

beforeEach(() => {
  sessionStorage.clear()
  _resetWakeDiagForTests()
})
afterEach(() => {
  setWakeDiag(false)
  vi.restoreAllMocks()
})

describe('wake diagnostics store', () => {
  it('is off by default and records nothing while off', () => {
    expect(isWakeDiagOn()).toBe(false)
    wakeDiagFrames(new Float32Array(1600))
    wakeDiagEvent('ignored')
    expect(wakeDiagPcm()).toHaveLength(0)
    expect(getWakeDiagSnapshot()).toMatchObject({ on: false, events: 0, seconds: 0 })
  })

  it('a reload finds the switch on and records from the first frame', () => {
    // The page reloaded onto a new build (or the WebView restarted) with the
    // key already set: the module boots without a ring and must allocate
    // it when the listener speaks, or the switch reads on while nothing is
    // kept and the badge never appears.
    sessionStorage.setItem('otodock.wakeDiag', '1')
    _resetWakeDiagForTests()
    expect(isWakeDiagOn()).toBe(true)
    wakeDiagListening(true)
    expect(getWakeDiagSnapshot()).toMatchObject({ on: true, listening: true })
    wakeDiagFrames(new Float32Array(16000))
    expect(wakeDiagPcm()).toHaveLength(16000)
    expect(getWakeDiagSnapshot()).toMatchObject({ on: true, seconds: 1 })
    expect(saveWakeDiag()).not.toBe('recorder is off')
  })

  it('persists the switch in sessionStorage and forgets everything when switched off', () => {
    setWakeDiag(true)
    expect(sessionStorage.getItem('otodock.wakeDiag')).toBe('1')
    expect(isWakeDiagOn()).toBe(true)
    wakeDiagFrames(new Float32Array(16000))
    wakeDiagEvent('listening')
    expect(getWakeDiagSnapshot()).toMatchObject({ on: true, seconds: 1 })
    expect(getWakeDiagSnapshot().events).toBeGreaterThanOrEqual(2) // 'recorder on' + 'listening'
    setWakeDiag(false)
    expect(sessionStorage.getItem('otodock.wakeDiag')).toBeNull()
    expect(wakeDiagPcm()).toHaveLength(0)
    expect(getWakeDiagSnapshot()).toMatchObject({ on: false, events: 0, seconds: 0 })
  })

  it('keeps the last 30 s in order across the ring wrap', () => {
    setWakeDiag(true)
    const total = (WAKE_DIAG_RING_SECONDS + 1) * WAKE_DIAG_RATE // 31 s
    let written = 0
    while (written < total) {
      const n = Math.min(1365, total - written)
      wakeDiagFrames(ramp(n, written))
      written += n
    }
    const pcm = wakeDiagPcm()
    expect(pcm).toHaveLength(WAKE_DIAG_RING_SECONDS * WAKE_DIAG_RATE)
    // First kept sample is input sample #16000 (one second in), the last is
    // the final input sample.
    const expectAt = (idx: number) => Math.round(((idx % 1000) / 1000) * 32767)
    expect(pcm[0]).toBe(expectAt(WAKE_DIAG_RATE))
    expect(pcm[pcm.length - 1]).toBe(expectAt(total - 1))
    expect(getWakeDiagSnapshot().seconds).toBe(30)
  })

  it('encodes a PCM16 mono WAV', () => {
    const pcm = Int16Array.from([1, -2, 32767, -32768])
    const wav = encodeWav(pcm)
    const v = new DataView(wav.buffer)
    const tag = (o: number) => String.fromCharCode(...wav.subarray(o, o + 4))
    expect(tag(0)).toBe('RIFF')
    expect(tag(8)).toBe('WAVE')
    expect(tag(12)).toBe('fmt ')
    expect(v.getUint16(20, true)).toBe(1) // PCM
    expect(v.getUint16(22, true)).toBe(1) // mono
    expect(v.getUint32(24, true)).toBe(WAKE_DIAG_RATE)
    expect(v.getUint16(34, true)).toBe(16)
    expect(tag(36)).toBe('data')
    expect(v.getUint32(40, true)).toBe(8)
    expect(Array.from(new Int16Array(wav.buffer, 44))).toEqual([1, -2, 32767, -32768])
  })

  it('exports one document with the audio embedded and the events aligned to it', () => {
    setWakeDiag(true)
    wakeDiagContext({ keywords: '▁HE Y @alpha', threshold: 0.3, base: '/kws-assets/v/' })
    wakeDiagFrames(new Float32Array(WAKE_DIAG_RATE)) // 1 s
    wakeDiagListening(true)
    wakeDiagEvent('detected', { keyword: 'alpha' })
    wakeDiagFrames(new Float32Array(WAKE_DIAG_RATE / 2)) // +0.5 s
    const { name, json } = exportWakeDiag()
    expect(name).toMatch(/^wake-diag-.*\.json$/)
    const doc = JSON.parse(json)
    expect(doc.kind).toBe('otodock-wake-diag')
    expect(doc.rate).toBe(WAKE_DIAG_RATE)
    expect(doc.wav_seconds).toBe(1.5)
    expect(doc.keywords).toBe('▁HE Y @alpha')
    expect(doc.threshold).toBe(0.3)
    expect(doc.engine_base).toBe('/kws-assets/v/')
    const detected = doc.events.find((e: { event: string }) => e.event === 'detected')
    expect(detected.wav_s).toBe(1)
    expect(detected.data).toEqual({ keyword: 'alpha' })
    const bytes = Uint8Array.from(atob(doc.wav_base64), (c) => c.charCodeAt(0))
    expect(String.fromCharCode(...bytes.subarray(0, 4))).toBe('RIFF')
    expect(bytes).toHaveLength(44 + 1.5 * WAKE_DIAG_RATE * 2)
  })

  it('saves through the Android Downloads bridge when the app exposes it', () => {
    setWakeDiag(true)
    wakeDiagFrames(new Float32Array(1600))
    const save = vi.fn()
    ;(window as unknown as { Android?: unknown }).Android = { saveImageFromBase64: save }
    try {
      const status = saveWakeDiag()
      expect(status).toMatch(/saved wake-diag-.*\.json to Downloads/)
      expect(save).toHaveBeenCalledTimes(1)
      const [b64, filename, mime] = save.mock.calls[0]
      expect(filename).toMatch(/^wake-diag-.*\.json$/)
      expect(mime).toBe('application/json')
      expect(JSON.parse(atob(b64)).kind).toBe('otodock-wake-diag')
    } finally {
      delete (window as unknown as { Android?: unknown }).Android
    }
  })

  it('saves as a browser download otherwise', () => {
    setWakeDiag(true)
    const createObjectURL = vi.fn(() => 'blob:wake')
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const status = saveWakeDiag()
    expect(status).toMatch(/^saved wake-diag-.*\.json$/)
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
  })
})

describe('WakeDiagBadge', () => {
  it('renders nothing while the recorder is off', () => {
    render(<WakeDiagBadge />)
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('shows the listener state while on, and Stop switches the recorder off', () => {
    render(<WakeDiagBadge />)
    act(() => { setWakeDiag(true) })
    expect(screen.getByRole('status')).toHaveTextContent('microphone idle')
    act(() => { wakeDiagListening(true); wakeDiagFrames(new Float32Array(2 * WAKE_DIAG_RATE)) })
    expect(screen.getByRole('status')).toHaveTextContent('listening')
    expect(screen.getByRole('status')).toHaveTextContent('2s kept')
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(isWakeDiagOn()).toBe(false)
    expect(screen.queryByRole('status')).toBeNull()
  })
})
