/** The duplex pre-ready mic ring: byte-bounded FIFO of wire-identical PCM16
 *  frames (see audio/pcmRing.ts for why framing must be preserved). */
import { describe, it, expect } from 'vitest'
import { createPcmRing, zeroedFrame, PCM_RING_MAX_BYTES } from '@/audio/pcmRing'

const frame = (bytes: number, fill = 1): ArrayBuffer => {
  const buf = new ArrayBuffer(bytes)
  new Int16Array(buf).fill(fill)
  return buf
}

describe('pcmRing', () => {
  it('drains frames in push order with identity preserved (never concatenated)', () => {
    const ring = createPcmRing(1000)
    const a = frame(100, 1)
    const b = frame(200, 2)
    ring.push(a)
    ring.push(b)
    const out = ring.drain()
    expect(out).toEqual([a, b])
    expect(out[0]).toBe(a) // same ArrayBuffer, wire framing intact
    expect(ring.bytes).toBe(0)
    expect(ring.drain()).toEqual([])
  })

  it('evicts oldest frames once the byte cap is exceeded', () => {
    const ring = createPcmRing(500)
    const frames = [1, 2, 3, 4].map(n => frame(200, n))
    frames.forEach(f => ring.push(f))
    // 800 bytes pushed, cap 500 → the two oldest evicted (400 remain).
    const out = ring.drain()
    expect(out).toEqual([frames[2], frames[3]])
  })

  it('always keeps at least the newest frame, even oversized', () => {
    const ring = createPcmRing(100)
    ring.push(frame(400, 7))
    expect(ring.drain()).toHaveLength(1)
  })

  it('clear() empties without draining', () => {
    const ring = createPcmRing()
    ring.push(frame(100))
    ring.clear()
    expect(ring.bytes).toBe(0)
    expect(ring.drain()).toEqual([])
  })

  it('default cap is 4s of 16k PCM16', () => {
    expect(PCM_RING_MAX_BYTES).toBe(4 * 16000 * 2)
  })

  it('zeroedFrame keeps the byte length and zeroes the content', () => {
    const z = zeroedFrame(frame(64, 123))
    expect(z.byteLength).toBe(64)
    expect(new Int16Array(z).every(s => s === 0)).toBe(true)
  })
})
