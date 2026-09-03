/**
 * PcmPlayer.getLevel (Stage Mode Phase B): the output tap the presence halo
 * polls while the agent speaks. jsdom has no AudioContext — faked from
 * scratch here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { createPcmPlayer } from '@/audio/webaudioPlayer'

class FakeAnalyser {
  fftSize = 2048
  fill = 128 // flat = silence
  connected: unknown[] = []
  connect(node: unknown) { this.connected.push(node) }
  getByteTimeDomainData(a: Uint8Array) { a.fill(this.fill) }
}

class FakeSource {
  buffer: unknown = null
  connected: unknown[] = []
  onended: (() => void) | null = null
  connect(node: unknown) { this.connected.push(node) }
  start() {}
  stop() {}
}

class FakeAC {
  static last: FakeAC | null = null
  currentTime = 0
  destination = { kind: 'destination' }
  analysers: FakeAnalyser[] = []
  sources: FakeSource[] = []
  constructor() { FakeAC.last = this }
  async resume() {}
  close() {}
  createAnalyser() { const a = new FakeAnalyser(); this.analysers.push(a); return a }
  createBuffer(_ch: number, len: number, rate: number) {
    return { duration: len / rate, copyToChannel() {} }
  }
  createBufferSource() { const s = new FakeSource(); this.sources.push(s); return s }
}

describe('PcmPlayer.getLevel', () => {
  beforeEach(() => {
    FakeAC.last = null
    vi.stubGlobal('AudioContext', FakeAC as unknown as typeof AudioContext)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('routes chunks through the analyser tap, tap feeds the destination', () => {
    const player = createPcmPlayer(24000)
    const ac = FakeAC.last!
    expect(ac.analysers).toHaveLength(1)
    expect(ac.analysers[0].connected).toEqual([ac.destination])

    player.enqueue(new Uint8Array([0, 0, 255, 127]))
    expect(ac.sources).toHaveLength(1)
    // Source → analyser (NOT straight to destination): the tap hears
    // exactly what plays, and adds nothing to the path.
    expect(ac.sources[0].connected).toEqual([ac.analysers[0]])
  })

  it('reflects the analyser signal, and is 0 after stop()', () => {
    const player = createPcmPlayer(24000)
    const analyser = FakeAC.last!.analysers[0]

    expect(player.getLevel()).toBe(0) // flat 128 = silence

    analyser.fill = 160 // |160-128|/128 = 0.25 RMS → ×3 normalization
    expect(player.getLevel()).toBeCloseTo(0.75, 5)

    player.stop()
    expect(player.getLevel()).toBe(0)
  })
})
