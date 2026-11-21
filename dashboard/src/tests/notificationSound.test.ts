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

// A notification's chime on an iPhone or an iPad plays through Web Audio
// alone, which Apple's WebKit plays in the "ambient" session: the Ring/Silent
// switch mutes it. A media element plays in "playback", which the switch
// leaves on. The chime asks for no session type (a spoken answer's hold and a
// live microphone stay as they are), and every other device, the danger alarm
// and the turn's ping keep the media element.
const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
const IPHONE_APP = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'
const MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15'
const ANDROID = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36'
const DESKTOP_CHROME = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
const DEVICE = ['userAgent', 'platform', 'maxTouchPoints', 'audioSession']

class ClipSource {
  buffer: unknown = null
  onended: (() => void) | null = null
  started = 0
  connect() {}
  start() { this.started++; setTimeout(() => this.onended?.(), 0) }
}

class ClipContext {
  static made: ClipContext[] = []
  static decodes = true
  closed = false
  resumed = 0
  destination = {}
  gains: { gain: { value: number } }[] = []
  sources: ClipSource[] = []
  constructor() { ClipContext.made.push(this) }
  resume() { this.resumed++; return Promise.resolve() }
  close() { this.closed = true; return Promise.resolve() }
  decodeAudioData() {
    return ClipContext.decodes ? Promise.resolve({ duration: 0.4 }) : Promise.reject(new Error('not audio'))
  }
  createGain() {
    const node = { gain: { value: 1 }, connect() {} }
    this.gains.push(node)
    return node
  }
  createBufferSource() {
    const s = new ClipSource()
    this.sources.push(s)
    return s
  }
}

function device(userAgent: string, opts: { platform?: string; touch?: number } = {}): string[] {
  const writes: string[] = []
  let type = 'auto'
  const audioSession = {
    get type() { return type },
    set type(v: string) { writes.push(v); type = v },
  }
  const values: Record<string, unknown> = {
    userAgent, platform: opts.platform ?? '', maxTouchPoints: opts.touch ?? 0, audioSession,
  }
  for (const [k, v] of Object.entries(values)) {
    Object.defineProperty(navigator, k, { value: v, configurable: true })
  }
  return writes
}

describe('a notification chime on an iPhone or an iPad', () => {
  beforeEach(() => {
    FakeAudio.made = []
    ClipContext.made = []
    ClipContext.decodes = true
    vi.stubGlobal('Audio', FakeAudio)
    vi.stubGlobal('AudioContext', ClipContext)
    vi.stubGlobal('speechSynthesis', { speak: vi.fn(), cancel: vi.fn(), getVoices: () => [] })
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8),
    }) as unknown as Response))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    for (const k of DEVICE) delete (navigator as unknown as Record<string, unknown>)[k]
  })

  async function chime(severity: 'info' | 'success' | 'warning') {
    const { useNotificationSound } = await freshHook()
    const { result } = renderHook(() => useNotificationSound())
    result.current.playForSeverity(severity, 't', 'b')
    return result
  }

  it('plays through Web Audio alone, at its volume, and closes its context at the end', async () => {
    for (const [ua, severity, file] of [
      [IPHONE, 'info', '/sounds/success.wav'],
      [IPHONE_APP, 'success', '/sounds/success.wav'],
      [IPHONE, 'warning', '/sounds/warning.wav'],
    ] as const) {
      ClipContext.made = []
      vi.mocked(fetch).mockClear()
      const writes = device(ua)
      await chime(severity)
      await vi.waitFor(() => expect(ClipContext.made[0]?.closed).toBe(true))
      expect(FakeAudio.made).toHaveLength(0)
      expect(ClipContext.made).toHaveLength(1)
      const ctx = ClipContext.made[0]
      expect(ctx.sources.map(s => s.started)).toEqual([1])
      expect(ctx.gains.map(g => g.gain.value)).toEqual([0.7])
      expect(vi.mocked(fetch).mock.calls.map(c => c[0])).toContain(file)
      // No session type asked: WebKit picks "ambient", which the switch mutes.
      expect(writes).toEqual([])
    }
  })

  it('plays through Web Audio on an iPad in desktop mode too', async () => {
    const writes = device(MAC, { platform: 'MacIntel', touch: 5 })
    await chime('info')
    await vi.waitFor(() => expect(ClipContext.made[0]?.closed).toBe(true))
    expect(FakeAudio.made).toHaveLength(0)
    expect(writes).toEqual([])
  })

  it('leaves an open wake microphone alone', async () => {
    const writes = device(IPHONE)
    const { useNotificationSound } = await freshHook()
    const mic = await import('@/audio/micCoordinator')
    mic.noteCapture('wake', true)
    const { result } = renderHook(() => useNotificationSound())
    result.current.playForSeverity('info', 't', 'b')
    await vi.waitFor(() => expect(ClipContext.made[0]?.closed).toBe(true))
    expect(mic.captureLive()).toBe(true)
    expect(mic.micBusy()).toBe(false)
    expect(writes).toEqual([])
    mic.noteCapture('wake', false)
  })

  it('keeps the media element on Android, a Mac and a desktop browser', async () => {
    for (const ua of [ANDROID, MAC, DESKTOP_CHROME]) {
      FakeAudio.made = []
      device(ua)
      await chime('info')
      expect(FakeAudio.made.map(a => [a.src, a.volume])).toEqual([['/sounds/success.wav', 0.7]])
    }
    expect(ClipContext.made).toHaveLength(0)
  })

  it('keeps the media element for the danger alarm and the turn ping on an iPhone', async () => {
    device(IPHONE)
    const { useNotificationSound } = await freshHook()
    const { result } = renderHook(() => useNotificationSound())
    result.current.playPing()
    result.current.playForSeverity('danger', 't', 'b')
    await result.current.stopDangerAlarm()
    expect(FakeAudio.made.map(a => a.src)).toEqual(['/sounds/ping.wav', '/sounds/danger.wav'])
    expect(ClipContext.made).toHaveLength(0)
  })

  it('plays the media element as before on an iPhone with no Web Audio', async () => {
    device(IPHONE)
    vi.stubGlobal('AudioContext', undefined)
    await chime('info')
    expect(FakeAudio.made.map(a => a.src)).toEqual(['/sounds/success.wav'])
  })

  it('closes its context and throws nothing when the file cannot be played', async () => {
    device(IPHONE)
    ClipContext.decodes = false
    await chime('info')
    await vi.waitFor(() => expect(ClipContext.made[0]?.closed).toBe(true))
    expect(ClipContext.made[0].sources).toHaveLength(0)
    expect(FakeAudio.made).toHaveLength(0)
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
