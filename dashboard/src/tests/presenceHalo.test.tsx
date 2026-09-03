/**
 * PresenceHalo (Stage Mode Phase B): the composer's audio-reactive glow.
 * jsdom has no canvas 2D, matchMedia, or ResizeObserver — all stubbed here;
 * the component itself must survive their absence (null-ctx guard).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'

import { PresenceHalo } from '@/components/chat/PresenceHalo'

class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function fakeCtx() {
  const gradient = { addColorStop: vi.fn() }
  return {
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    createRadialGradient: vi.fn(() => gradient),
    createLinearGradient: vi.fn(() => gradient),
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    arcTo: vi.fn(),
    closePath: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    arc: vi.fn(),
    setLineDash: vi.fn(),
  } as unknown as CanvasRenderingContext2D
}

let reducedMotion = false

describe('PresenceHalo', () => {
  beforeEach(() => {
    reducedMotion = false
    vi.stubGlobal('ResizeObserver', RO)
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: reducedMotion,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders nothing at phase off/error (composer as today)', () => {
    const off = render(<PresenceHalo phase="off" />)
    expect(off.container.firstChild).toBeNull()
    const err = render(<PresenceHalo phase="error" />)
    expect(err.container.firstChild).toBeNull()
  })

  it('mounts an inert canvas while live and runs the rAF loop', () => {
    const ctx = fakeCtx()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx)
    const raf = vi.fn(() => 1)
    const caf = vi.fn()
    vi.stubGlobal('requestAnimationFrame', raf)
    vi.stubGlobal('cancelAnimationFrame', caf)

    const { container, unmount } = render(
      <PresenceHalo phase="listening" getLevels={() => ({ mic: 0.5, out: 0 })} />,
    )
    const canvas = container.querySelector('canvas')
    expect(canvas).not.toBeNull()
    expect(canvas!.getAttribute('aria-hidden')).toBe('true')
    expect(canvas!.className).toContain('pointer-events-none')
    expect(canvas!.dataset.phase).toBe('listening')
    expect(raf).toHaveBeenCalled()

    unmount()
    expect(caf).toHaveBeenCalled()
  })

  it('reduced motion: the loop still runs (calmer, never absent)', () => {
    // OEMs (Honor et al) force animator-scale off → the WebView reports
    // reduce without user intent. Reduce means no sparks/orbit/breathing,
    // NOT a dead halo — the live loop must still be scheduled.
    reducedMotion = true
    const ctx = fakeCtx()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx)
    const raf = vi.fn(() => 1)
    vi.stubGlobal('requestAnimationFrame', raf)
    vi.stubGlobal('cancelAnimationFrame', vi.fn())

    render(<PresenceHalo phase="speaking" getLevels={() => ({ mic: 0, out: 1 })} />)
    expect(raf).toHaveBeenCalled()
  })

  it('survives a null 2D context (jsdom / ancient browsers)', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    const raf = vi.fn(() => 1)
    vi.stubGlobal('requestAnimationFrame', raf)
    const { container } = render(<PresenceHalo phase="thinking" />)
    expect(container.querySelector('canvas')).not.toBeNull()
    expect(raf).not.toHaveBeenCalled() // no ctx → no loop, no crash
  })
})
