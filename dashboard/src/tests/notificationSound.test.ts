/**
 * The chime hook opens no sound output until a sound plays: the first tap
 * warms speech synthesis in a browser and nothing in the app shell (it used
 * to open an AudioContext it never played through, and keep it running for
 * the page's life); the files are warmed into memory at mount, on no media
 * element; a chime plays from memory once warmed, from the file before. And
 * every chime file stays under five seconds, the length at which the WebView
 * stops asking other apps to lower their sound and takes the focus instead.
 * The hook keeps module-level state, so each case imports it afresh; the
 * gesture cases run first, since a module's gesture listeners stay on the
 * document until a gesture removes them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

type Hook = typeof import('@/hooks/useNotificationSound')

async function freshHook(): Promise<Hook> {
  vi.resetModules()
  return await import('@/hooks/useNotificationSound')
}

const speak = vi.fn()
class FakeUtterance {
  text: string
  volume = 1
  constructor(text: string) { this.text = text }
}
class FakeAudio {
  static made: FakeAudio[] = []
  src: string
  volume = 1
  constructor(src: string) { this.src = src; FakeAudio.made.push(this) }
  play() { return Promise.resolve() }
}
const AudioContextSpy = vi.fn()

function soundResponse(ok = true, type = 'audio/wav') {
  return {
    ok, status: ok ? 200 : 404,
    headers: { get: (h: string) => (h === 'content-type' ? type : null) },
    blob: async () => new Blob(['riff']),
  } as unknown as Response
}

async function settle() {
  for (let i = 0; i < 6; i++) await Promise.resolve()
}

describe('the chime hook', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    speak.mockReset()
    FakeAudio.made = []
    AudioContextSpy.mockReset()
    vi.stubGlobal('speechSynthesis', { speak, cancel: vi.fn(), getVoices: () => [] })
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('Audio', FakeAudio)
    vi.stubGlobal('AudioContext', AudioContextSpy)
    vi.stubGlobal('fetch', vi.fn(async () => soundResponse()))
    URL.createObjectURL = vi.fn((b: Blob) => `blob:${b.size}`)
  })

  afterEach(() => {
    delete (window as unknown as { Capacitor?: unknown }).Capacitor
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('warms speech synthesis at the first tap in a browser, with no AudioContext', async () => {
    await freshHook()
    document.dispatchEvent(new Event('touchstart'))
    expect(AudioContextSpy).not.toHaveBeenCalled()
    expect(speak).toHaveBeenCalledTimes(1)
    const u = speak.mock.calls[0][0] as FakeUtterance
    expect(u.text).toBe('')
    expect(u.volume).toBe(0)
    document.dispatchEvent(new Event('click'))
    expect(speak).toHaveBeenCalledTimes(1)
  })

  it('warms nothing at the first tap inside the app shell', async () => {
    ;(window as unknown as { Capacitor?: unknown }).Capacitor = { isNativePlatform: () => true }
    await freshHook()
    document.dispatchEvent(new Event('touchstart'))
    expect(AudioContextSpy).not.toHaveBeenCalled()
    expect(speak).not.toHaveBeenCalled()
  })

  it('warms the four files into memory once at mount, on no media element', async () => {
    const { useNotificationSound } = await freshHook()
    renderHook(() => useNotificationSound())
    renderHook(() => useNotificationSound())
    expect(fetch).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1000)
    expect(fetch).toHaveBeenCalledTimes(4)
    const urls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map(c => c[0]).sort()
    expect(urls).toEqual(['/sounds/danger.wav', '/sounds/ping.wav', '/sounds/success.wav', '/sounds/warning.wav'])
    await settle()
    expect(URL.createObjectURL).toHaveBeenCalledTimes(4)
    expect(FakeAudio.made).toHaveLength(0)
  })

  it('plays a chime from the file before the warm and from memory after it', async () => {
    const { useNotificationSound } = await freshHook()
    const { result } = renderHook(() => useNotificationSound())
    result.current.playForSeverity('success', 't', 'b')
    expect(FakeAudio.made).toHaveLength(1)
    expect(FakeAudio.made[0].src).toBe('/sounds/success.wav')
    expect(FakeAudio.made[0].volume).toBe(0.7)
    vi.advanceTimersByTime(1000)
    await settle()
    result.current.playPing()
    expect(FakeAudio.made[1].src).toMatch(/^blob:/)
    expect(FakeAudio.made[1].volume).toBe(0.3)
  })

  it('plays from the file when the warm fails or answers something else', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) =>
      url.endsWith('ping.wav') ? soundResponse(true, 'text/html') : Promise.reject(new Error('offline'))))
    const { useNotificationSound } = await freshHook()
    const { result } = renderHook(() => useNotificationSound())
    vi.advanceTimersByTime(1000)
    await settle()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    result.current.playPing()
    result.current.playForSeverity('warning', 't', 'b')
    expect(FakeAudio.made.map(a => a.src)).toEqual(['/sounds/ping.wav', '/sounds/warning.wav'])
  })
})

/** The WAV header's duration: the fmt chunk's rate, channels and bits, the
 *  data chunk's size. */
function wavSeconds(bytes: Buffer): number {
  expect(bytes.toString('ascii', 0, 4)).toBe('RIFF')
  expect(bytes.toString('ascii', 8, 12)).toBe('WAVE')
  let at = 12
  let rate = 0, channels = 0, bits = 0, dataSize = 0
  while (at + 8 <= bytes.length) {
    const id = bytes.toString('ascii', at, at + 4)
    const size = bytes.readUInt32LE(at + 4)
    if (id === 'fmt ') {
      // The arithmetic below is right for PCM (1) and float (3) only.
      expect([1, 3]).toContain(bytes.readUInt16LE(at + 8))
      channels = bytes.readUInt16LE(at + 10)
      rate = bytes.readUInt32LE(at + 12)
      bits = bytes.readUInt16LE(at + 22)
    } else if (id === 'data') {
      dataSize = size
      break
    }
    at += 8 + size + (size % 2)
  }
  expect(rate).toBeGreaterThan(0)
  return dataSize / (rate * channels * (bits / 8))
}

describe('the chime files', () => {
  it('each stay under five seconds, so the WebView only asks other apps to lower their sound', () => {
    const dir = join(__dirname, '..', '..', 'public', 'sounds')
    const files = readdirSync(dir).filter(f => f.endsWith('.wav')).sort()
    expect(files).toEqual(['danger.wav', 'ping.wav', 'success.wav', 'warning.wav'])
    for (const f of files) {
      const seconds = wavSeconds(readFileSync(join(dir, f)))
      expect(seconds, f).toBeGreaterThan(0)
      expect(seconds, f).toBeLessThan(5)
    }
  })
})
