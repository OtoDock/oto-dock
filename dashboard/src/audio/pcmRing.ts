// Bounded FIFO of PCM16 frames for the duplex pre-ready mic buffer.
//
// Frames are buffered EXACTLY as they would go on the wire (post-downsample
// 16 kHz PCM16 ArrayBuffers) and flushed frame-by-frame, never concatenated
// — the daemon's inbound queue is frame-counted (100 frames, drop-oldest),
// so wire framing must be identical between the buffered burst and live
// streaming. The byte cap is rate-independent (bytes ARE time at a fixed
// output rate): 128 000 B = 4 s @ 16 kHz mono PCM16 ≈ 47 wire frames at a
// 48 kHz capture context — ~2× headroom under the daemon's cap.

export const PCM_RING_MAX_BYTES = 128_000

export interface PcmRing {
  push(frame: ArrayBuffer): void
  /** Drain all frames in capture order and empty the ring. */
  drain(): ArrayBuffer[]
  clear(): void
  readonly bytes: number
}

export function createPcmRing(maxBytes: number = PCM_RING_MAX_BYTES): PcmRing {
  let frames: ArrayBuffer[] = []
  let bytes = 0
  return {
    push(frame: ArrayBuffer) {
      frames.push(frame)
      bytes += frame.byteLength
      while (bytes > maxBytes && frames.length > 1) {
        const dropped = frames.shift()!
        bytes -= dropped.byteLength
      }
    },
    drain(): ArrayBuffer[] {
      const out = frames
      frames = []
      bytes = 0
      return out
    },
    clear() {
      frames = []
      bytes = 0
    },
    get bytes() {
      return bytes
    },
  }
}

/** A zeroed frame of identical length — the muted-mic transform (cadence
 *  preserved, no voice content; the engine's STT keepalive stays fed). */
export function zeroedFrame(frame: ArrayBuffer): ArrayBuffer {
  return new ArrayBuffer(frame.byteLength)
}
