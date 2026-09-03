/** The 3D map's pointer-tracking bookkeeping (gestures.ts) — the
 *  stuck-pinch machinery, headless (live-hit 2026-08-15: ghost pointers
 *  from cancelled/detached touches killed pinch until remount, and the
 *  single-gesture stage-exit threshold was physically unreachable). */
import { describe, it, expect } from 'vitest'
import {
  newestTwoIds,
  pinchOutPressure,
  pinchSpan,
  prunePointers,
  STALE_POINTER_MS,
  type PointerMap,
  type TrackedPointer,
} from '@/components/agents-map/gestures'

const pt = (x: number, y: number, t: number, el: Node | null = null): TrackedPointer =>
  ({ x, y, t, el })

describe('prunePointers', () => {
  const attached = new Set<Node>()
  const isAttached = (el: Node | null) => !el || attached.has(el)

  it('drops entries whose pointerdown target is detached (the chip-rebuild ghost)', () => {
    const chip = {} as Node
    const map: PointerMap = new Map([
      [1, pt(0, 0, 1000, chip)],
      [2, pt(5, 5, 1000, null)],
    ])
    prunePointers(map, 1010, isAttached) // chip not in `attached` → detached
    expect([...map.keys()]).toEqual([2])
  })

  it('drops stale entries but keeps fresh ones', () => {
    const map: PointerMap = new Map([
      [1, pt(0, 0, 0)],
      [2, pt(5, 5, 900)],
    ])
    prunePointers(map, STALE_POINTER_MS + 500, isAttached)
    expect([...map.keys()]).toEqual([2])
  })

  it('never drops a live gesture owner, however stale (a held pinch finger)', () => {
    const map: PointerMap = new Map([[7, pt(0, 0, 0)]])
    prunePointers(map, STALE_POINTER_MS * 10, isAttached, new Set([7]))
    expect(map.has(7)).toBe(true)
  })
})

describe('newestTwoIds / pinchSpan', () => {
  it('picks the two MOST RECENTLY tracked ids, not the first two entries', () => {
    const map: PointerMap = new Map([
      [9, pt(0, 0, 0)],   // a ghost that survived — must not join the pinch
      [1, pt(0, 0, 100)],
      [2, pt(30, 40, 100)],
    ])
    expect(newestTwoIds(map)).toEqual([1, 2])
    expect(pinchSpan(map, 1, 2)).toBe(50)
  })

  it('returns null when a pinch-owned finger vanished', () => {
    const map: PointerMap = new Map([[1, pt(0, 0, 0)]])
    expect(newestTwoIds(map)).toBeNull()
    expect(pinchSpan(map, 1, 2)).toBeNull()
  })
})

describe('pinchOutPressure', () => {
  it('is zero at or inside the clamp', () => {
    expect(pinchOutPressure(1.3, 1.3, 16.7)).toBe(0)
    expect(pinchOutPressure(1.0, 1.3, 16.7)).toBe(0)
  })

  it('is proportional past the clamp and dt-normalized', () => {
    const at60hz = pinchOutPressure(1.5, 1.3, 16.7)
    const at120hz = pinchOutPressure(1.5, 1.3, 16.7 / 2)
    expect(at60hz).toBeCloseTo(0.2 * 200, 5)
    expect(at120hz).toBeCloseTo(at60hz / 2, 5)
    // Sustained over-pinch crosses the wheel's 320 exit threshold within a
    // few hundred ms (~9 frames here); a soft touch never accumulates.
    expect(320 / at60hz).toBeLessThan(10)
  })

  it('clamps a huge dt (background tab wakeup) to 2 frames of pressure', () => {
    expect(pinchOutPressure(1.5, 1.3, 5000)).toBeCloseTo(0.2 * 200 * 2, 5)
  })
})
