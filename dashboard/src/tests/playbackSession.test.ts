/**
 * The playback session on an iPhone or an iPad: every PCM player asks
 * Apple's WebKit for the "playback" audio session type while it plays, so a
 * spoken answer is heard with the Ring/Silent switch on silent, and puts
 * "auto" back when the last player stops. Never while a microphone is open
 * or opening (WebKit refuses a capture under "playback" and cuts a live
 * one), never on a Mac, Android or a browser without the API. jsdom has
 * neither an AudioContext nor navigator.audioSession: both are stand-ins.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { createPcmPlayer } from '@/audio/webaudioPlayer'
import { platformTts } from '@/audio/backends/platformTts'
import { acquireMic, releaseMic, noteCapture } from '@/audio/micCoordinator'
import { _resetPlaybackSession } from '@/audio/playbackSession'
import * as authApi from '@/api/auth'

const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
const IPHONE_APP = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'
const IPAD_DESKTOP = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15'
const MAC_SAFARI = IPAD_DESKTOP
const ANDROID = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36'
const DESKTOP_CHROME = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

class FakeSource {
  buffer: unknown = null
  onended: (() => void) | null = null
  connect() {}
  // The chunk plays out on the next task, so drained() settles.
  start() { setTimeout(() => this.onended?.(), 0) }
  stop() {}
}

class FakeAC {
  static last: FakeAC | null = null
  /** The session type WebKit would see as this context opens. */
  typeAtOpen: string | undefined
  currentTime = 0
  destination = {}
  closed = false
  constructor() {
    FakeAC.last = this
    this.typeAtOpen = (navigator as unknown as { audioSession?: { type: string } }).audioSession?.type
  }
  async resume() {}
  async suspend() {}
  close() { this.closed = true }
  createAnalyser() { return { fftSize: 0, connect() {}, getByteTimeDomainData() {} } }
  createBuffer(_ch: number, len: number, rate: number) { return { duration: len / rate, copyToChannel() {} } }
  createBufferSource() { return new FakeSource() }
}

interface StandIn { writes: string[]; session: { type: string } }

const DEFINED = ['userAgent', 'platform', 'maxTouchPoints', 'audioSession']

function standIn(userAgent: string, opts: { platform?: string; touch?: number; api?: boolean; type?: string } = {}): StandIn {
  const writes: string[] = []
  let type = opts.type ?? 'auto'
  const session = {
    get type() { return type },
    set type(v: string) { writes.push(v); type = v },
  }
  const values: Record<string, unknown> = {
    userAgent, platform: opts.platform ?? '', maxTouchPoints: opts.touch ?? 0,
  }
  if (opts.api !== false) values.audioSession = session
  for (const [k, v] of Object.entries(values)) {
    Object.defineProperty(navigator, k, { value: v, configurable: true })
  }
  return { writes, session }
}

beforeEach(() => {
  FakeAC.last = null
  vi.stubGlobal('AudioContext', FakeAC as unknown as typeof AudioContext)
  _resetPlaybackSession()
})

afterEach(() => {
  vi.unstubAllGlobals()
  for (const k of DEFINED) delete (navigator as unknown as Record<string, unknown>)[k]
  releaseMic('duplex')
  releaseMic('dictation')
  noteCapture('wake', false)
  _resetPlaybackSession()
})

describe('the player on an iPhone or an iPad', () => {
  it('asks for playback before its context opens, and puts auto back at its stop', () => {
    const s = standIn(IPHONE)
    const player = createPcmPlayer(24000)
    expect(FakeAC.last!.typeAtOpen).toBe('playback')
    expect(s.session.type).toBe('playback')
    player.stop()
    expect(s.session.type).toBe('auto')
    expect(s.writes).toEqual(['playback', 'auto'])
  })

  it('asks inside the iPhone app, and on an iPad in its desktop mode', () => {
    for (const [ua, platform, touch] of [[IPHONE_APP, 'iPhone', 5], [IPAD_DESKTOP, 'MacIntel', 5]] as const) {
      const s = standIn(ua, { platform, touch })
      const player = createPcmPlayer(24000)
      expect(s.session.type).toBe('playback')
      player.stop()
      expect(s.session.type).toBe('auto')
    }
  })

  it('keeps playback until the last of two players stops, and a second stop counts once', () => {
    const s = standIn(IPHONE)
    const a = createPcmPlayer(24000)
    const b = createPcmPlayer(24000)
    a.stop()
    a.stop()
    expect(s.session.type).toBe('playback')
    b.stop()
    expect(s.session.type).toBe('auto')
    expect(s.writes).toEqual(['playback', 'auto'])
  })

  it('never asks while a microphone is owned or a capture is noted', () => {
    const s = standIn(IPHONE)
    acquireMic('duplex')
    createPcmPlayer(24000).stop()
    releaseMic('duplex')
    noteCapture('wake', true)
    createPcmPlayer(24000).stop()
    expect(s.writes).toEqual([])
  })

  it('puts auto back the moment a microphone opens, and does not ask again for that player', () => {
    const s = standIn(IPHONE)
    const player = createPcmPlayer(24000)
    acquireMic('dictation')
    expect(s.session.type).toBe('auto')
    releaseMic('dictation')
    player.stop()
    expect(s.writes).toEqual(['playback', 'auto'])
  })

  it('puts auto back when a capture is noted while it plays', () => {
    const s = standIn(IPHONE)
    const player = createPcmPlayer(24000)
    noteCapture('wake', true)
    expect(s.session.type).toBe('auto')
    player.stop()
    expect(s.writes).toEqual(['playback', 'auto'])
  })

  it('puts auto back when its context cannot open', () => {
    const s = standIn(IPHONE)
    vi.stubGlobal('AudioContext', class { constructor() { throw new Error('too many contexts') } })
    expect(() => createPcmPlayer(24000)).toThrow('too many contexts')
    expect(s.writes).toEqual(['playback', 'auto'])
  })

  it('leaves a type someone else set, before and after', () => {
    const before = standIn(IPHONE, { type: 'play-and-record' })
    createPcmPlayer(24000).stop()
    expect(before.writes).toEqual([])

    const after = standIn(IPHONE)
    const player = createPcmPlayer(24000)
    after.session.type = 'ambient'
    player.stop()
    expect(after.session.type).toBe('ambient')
  })
})

describe('the player everywhere else', () => {
  it('never asks on a Mac, Android or a desktop browser that has the API', () => {
    for (const [ua, platform, touch] of [
      [MAC_SAFARI, 'MacIntel', 0], [ANDROID, 'Linux armv8l', 5], [DESKTOP_CHROME, 'Linux x86_64', 0],
    ] as const) {
      const s = standIn(ua, { platform, touch })
      const player = createPcmPlayer(24000)
      expect(FakeAC.last!.typeAtOpen).toBe('auto')
      player.stop()
      expect(s.writes).toEqual([])
    }
  })

  it('plays as before on an iPhone without the API', () => {
    standIn(IPHONE, { api: false })
    const player = createPcmPlayer(24000)
    player.enqueue(new Uint8Array([0, 0, 1, 0]))
    player.stop()
    expect(FakeAC.last!.closed).toBe(true)
    expect('audioSession' in navigator).toBe(false)
  })
})

// ─── The read-aloud button's backend ────────────────────────────────────────

function pcmResponse(chunks: Uint8Array[], failAfter = false): Response {
  let i = 0
  const reader = {
    async read() {
      if (i < chunks.length) return { done: false, value: chunks[i++] }
      if (failAfter) throw new TypeError('network connection was lost')
      return { done: true, value: undefined }
    },
  }
  return {
    ok: true, status: 200, body: { getReader: () => reader },
    headers: new Headers({ 'X-Audio-Sample-Rate': '24000' }),
  } as unknown as Response
}

describe('the read-aloud button on an iPhone', () => {
  const fetchSpy = vi.spyOn(authApi, 'apiFetch')
  afterEach(() => { platformTts.stop(); fetchSpy.mockReset() })

  it('plays in the playback session and puts auto back when the answer ends', async () => {
    const s = standIn(IPHONE)
    fetchSpy.mockImplementation(async () => pcmResponse([new Uint8Array([0, 0, 1, 0])]))
    await platformTts.play('Hello.', { language: 'en' })
    expect(FakeAC.last!.typeAtOpen).toBe('playback')
    expect(s.session.type).toBe('auto')
    expect(s.writes).toEqual(['playback', 'auto'])
  })

  it('puts auto back at a stop', async () => {
    const s = standIn(IPHONE)
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    fetchSpy.mockImplementation(async () => {
      const res = pcmResponse([new Uint8Array([0, 0, 1, 0])])
      const reader = res.body!.getReader()
      return { ...res, body: { getReader: () => ({ read: async () => { await gate; return reader.read() } }) } } as unknown as Response
    })
    const played = platformTts.play('Hello.', { language: 'en' })
    await vi.waitFor(() => expect(s.session.type).toBe('playback'))
    platformTts.stop()
    expect(s.session.type).toBe('auto')
    release()
    await played.catch(() => {})
  })

  it('plays what came of a stream that broke, then puts auto back and fails', async () => {
    const s = standIn(IPHONE)
    fetchSpy.mockImplementation(async () => pcmResponse([new Uint8Array([0, 0, 1, 0])], true))
    await expect(platformTts.play('Hello.', { language: 'en' })).rejects.toThrow('network connection was lost')
    expect(FakeAC.last!.closed).toBe(true)
    expect(s.session.type).toBe('auto')
  })
})
