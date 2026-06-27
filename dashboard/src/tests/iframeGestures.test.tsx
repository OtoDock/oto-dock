// Swipes forwarded out of sandboxed artifact/app iframes: the bus fan-out
// and useSwipeGesture's containment routing (a forwarded gesture drives the
// SAME drawer a direct touch on that spot would — the frame must live inside
// the hook's container).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { act } from 'react'
import { useRef } from 'react'
import { emitIframeSwipe, onIframeSwipe } from '../lib/iframeGestures'
import { useSwipeGesture } from '../hooks/useSwipeGesture'

describe('iframeGestures bus', () => {
  it('fans out to subscribers and unsubscribes cleanly', () => {
    const seen: string[] = []
    const off = onIframeSwipe((s) => seen.push(s.dir))
    const el = document.createElement('iframe')
    emitIframeSwipe({ el, dir: 'left' })
    expect(seen).toEqual(['left'])
    off()
    emitIframeSwipe({ el, dir: 'right' })
    expect(seen).toEqual(['left'])
  })

  it('one throwing subscriber does not break the fan-out', () => {
    const seen: string[] = []
    const off1 = onIframeSwipe(() => { throw new Error('boom') })
    const off2 = onIframeSwipe((s) => seen.push(s.dir))
    emitIframeSwipe({ el: document.createElement('iframe'), dir: 'right' })
    expect(seen).toEqual(['right'])
    off1(); off2()
  })
})

describe('useSwipeGesture iframe routing', () => {
  const onSwipeLeft = vi.fn()
  const onSwipeRight = vi.fn()
  let container: HTMLDivElement
  let innerFrame: HTMLIFrameElement
  let outsideFrame: HTMLIFrameElement
  const originalWidth = window.innerWidth

  beforeEach(() => {
    vi.clearAllMocks()
    // The hook is mobileOnly by default — emulate a phone viewport.
    Object.defineProperty(window, 'innerWidth', { value: 500, configurable: true })
    container = document.createElement('div')
    innerFrame = document.createElement('iframe')
    container.appendChild(innerFrame)
    outsideFrame = document.createElement('iframe')
    document.body.append(container, outsideFrame)
  })

  afterEach(() => {
    Object.defineProperty(window, 'innerWidth', { value: originalWidth, configurable: true })
    container.remove()
    outsideFrame.remove()
  })

  function mountHook() {
    return renderHook(() => {
      const ref = useRef<HTMLElement | null>(container)
      useSwipeGesture(ref, { onSwipeLeft, onSwipeRight })
    })
  }

  it('routes a contained frame swipe to the callbacks', () => {
    mountHook()
    act(() => emitIframeSwipe({ el: innerFrame, dir: 'right' }))
    expect(onSwipeRight).toHaveBeenCalledTimes(1)
    act(() => emitIframeSwipe({ el: innerFrame, dir: 'left' }))
    expect(onSwipeLeft).toHaveBeenCalledTimes(1)
  })

  it('ignores frames outside its container', () => {
    mountHook()
    act(() => emitIframeSwipe({ el: outsideFrame, dir: 'right' }))
    expect(onSwipeRight).not.toHaveBeenCalled()
  })

  it('respects the mobile gate', () => {
    Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true })
    mountHook()
    act(() => emitIframeSwipe({ el: innerFrame, dir: 'right' }))
    expect(onSwipeRight).not.toHaveBeenCalled()
  })

  it('stops listening after unmount', () => {
    const h = mountHook()
    h.unmount()
    act(() => emitIframeSwipe({ el: innerFrame, dir: 'left' }))
    expect(onSwipeLeft).not.toHaveBeenCalled()
  })
})
