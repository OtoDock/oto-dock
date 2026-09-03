// Shared mic-environment stubs for the duplex hook tests: jsdom has neither
// AudioContext nor navigator.mediaDevices, and useDuplexVoice opens the mic
// at start() time (the first-words fix) — every test that calls start()
// needs these installed or the hook tears down with 'microphone unavailable'.
import { vi } from 'vitest'

export class FakeScriptProcessor {
  onaudioprocess: ((e: { inputBuffer: { getChannelData: (c: number) => Float32Array } }) => void) | null = null
  connect = vi.fn()
  disconnect = vi.fn()
}

export class FakeAudioContext {
  static instances: FakeAudioContext[] = []
  sampleRate = 48000
  destination = {}
  processor = new FakeScriptProcessor()
  state: AudioContextState = 'running'
  onstatechange: (() => void) | null = null
  resume = vi.fn(async () => {})
  close = vi.fn(async () => {})
  createMediaStreamSource = vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() }))
  createScriptProcessor = vi.fn(() => this.processor)
  createAnalyser = vi.fn(() => ({
    fftSize: 0,
    connect: vi.fn(),
    getByteTimeDomainData: vi.fn(),
  }))
  constructor() { FakeAudioContext.instances.push(this) }
}

export interface FakeTrack { stop: ReturnType<typeof vi.fn> }

export interface MicEnv {
  getUserMedia: ReturnType<typeof vi.fn>
  tracks: FakeTrack[]
  /** Replace getUserMedia with a manually-resolved promise (race tests). */
  defer(): { resolve: () => void; reject: (e?: unknown) => void }
}

export function installMicEnv(): MicEnv {
  FakeAudioContext.instances = []
  const tracks: FakeTrack[] = []
  const makeStream = () => {
    const track: FakeTrack = { stop: vi.fn() }
    tracks.push(track)
    return { getTracks: () => [track] }
  }
  const getUserMedia = vi.fn(async () => makeStream())
  vi.stubGlobal('AudioContext', FakeAudioContext as unknown as typeof AudioContext)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia },
    configurable: true,
  })
  return {
    getUserMedia,
    tracks,
    defer() {
      let resolveFn!: () => void
      let rejectFn!: (e?: unknown) => void
      const gate = new Promise<void>((res, rej) => { resolveFn = res; rejectFn = rej })
      getUserMedia.mockImplementation(async () => {
        await gate
        return makeStream()
      })
      return { resolve: resolveFn, reject: rejectFn }
    },
  }
}

export function uninstallMicEnv(): void {
  // vi.unstubAllGlobals() restores AudioContext; mediaDevices was defined
  // with configurable:true so it can simply be removed.
  delete (navigator as unknown as Record<string, unknown>).mediaDevices
}

/** Fire one ScriptProcessor tick with a constant-valued 4096-sample buffer
 *  (value is recoverable from the PCM16 output — order assertions). */
export function emitFrame(ctx: FakeAudioContext, value = 0.25, samples = 4096): void {
  const data = new Float32Array(samples).fill(value)
  ctx.processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => data } })
}
