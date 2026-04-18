import { useEffect, useRef } from 'react'
import { onIframeSwipe } from '../lib/iframeGestures'

interface SwipeOptions {
  onSwipeLeft?: () => void
  onSwipeRight?: () => void
  /** Minimum horizontal distance in px (default: 80) */
  minDistance?: number
  /** Minimum velocity in px/ms (default: 0.3) */
  minVelocity?: number
  /** Horizontal/vertical ratio threshold (default: 1.5) */
  directionRatio?: number
  /** Only enable on mobile (default: true) */
  mobileOnly?: boolean
  /** Tags to exclude — swipes starting inside these elements are ignored */
  excludeTags?: string[]
  /** CSS classes to exclude — swipes starting inside elements with these classes are ignored */
  excludeClasses?: string[]
}

const DEFAULT_EXCLUDE_TAGS = ['PRE', 'CODE', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TD', 'TH']
const DEFAULT_EXCLUDE_CLASSES = ['overflow-x-auto', 'overflow-x-scroll', 'code-block']

/**
 * Detect horizontal swipe gestures on a container element.
 * Designed for mobile drawer toggle — excludes code blocks, tables,
 * and any horizontally-scrollable content.
 */
export function useSwipeGesture(
  ref: React.RefObject<HTMLElement | null>,
  options: SwipeOptions = {},
) {
  const {
    onSwipeLeft,
    onSwipeRight,
    minDistance = 50,
    minVelocity = 0.25,
    directionRatio = 1.5,
    mobileOnly = true,
    excludeTags = DEFAULT_EXCLUDE_TAGS,
    excludeClasses = DEFAULT_EXCLUDE_CLASSES,
  } = options

  // Store callbacks in refs to avoid re-attaching listeners
  const callbacksRef = useRef({ onSwipeLeft, onSwipeRight })
  callbacksRef.current = { onSwipeLeft, onSwipeRight }

  useEffect(() => {
    const el = ref.current
    if (!el) return

    let startX = 0
    let startY = 0
    let startTime = 0
    let tracking = false

    const isMobile = () => !mobileOnly || window.innerWidth < 768

    const isExcluded = (target: EventTarget | null): boolean => {
      if (!target || !(target instanceof HTMLElement)) return false
      let node: HTMLElement | null = target

      while (node && node !== el) {
        // Check tag name
        if (excludeTags.includes(node.tagName)) return true

        // Check classes
        for (const cls of excludeClasses) {
          if (node.classList.contains(cls)) return true
        }

        // Genuinely horizontally-scrollable ancestor (the Setup tab bar, a
        // table wrapped in overflow-x-auto, etc.) — let it own the gesture.
        // Require an actual scrollable overflow-x: a raw scrollWidth>clientWidth
        // alone also fires on INCIDENTAL overflow (a slightly-too-wide card or
        // form), which was wrongly swallowing swipes on dense pages like Setup.
        if (node.scrollWidth > node.clientWidth + 2) {
          const ox = getComputedStyle(node).overflowX
          if (ox === 'auto' || ox === 'scroll') return true
        }

        node = node.parentElement
      }
      return false
    }

    const isInteractiveElement = (target: EventTarget | null): boolean => {
      if (!target || !(target instanceof HTMLElement)) return false
      const tag = target.tagName
      // Don't intercept swipes on form elements or buttons
      return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
    }

    const onTouchStart = (e: TouchEvent) => {
      if (!isMobile()) return
      if (e.touches.length > 1) return // ignore multi-touch
      if (isExcluded(e.target)) return
      if (isInteractiveElement(e.target)) return

      startX = e.touches[0].clientX
      startY = e.touches[0].clientY
      startTime = Date.now()
      tracking = true
    }

    const onTouchEnd = (e: TouchEvent) => {
      if (!tracking) return
      tracking = false

      const touch = e.changedTouches[0]
      if (!touch) return

      const deltaX = touch.clientX - startX
      const deltaY = touch.clientY - startY
      const elapsed = Date.now() - startTime

      // Sanity: ignore if touch lasted too long (>800ms = not a swipe, maybe a drag)
      if (elapsed > 800) return
      // Sanity: ignore very short touches (<50ms = probably a tap)
      if (elapsed < 50) return

      const absDx = Math.abs(deltaX)
      const absDy = Math.abs(deltaY)

      // Must exceed minimum distance
      if (absDx < minDistance) return
      // Must be primarily horizontal
      if (absDx < absDy * directionRatio) return
      // Must exceed minimum velocity
      const velocity = absDx / elapsed
      if (velocity < minVelocity) return

      if (deltaX > 0) {
        callbacksRef.current.onSwipeRight?.()
      } else {
        callbacksRef.current.onSwipeLeft?.()
      }
    }

    const onTouchCancel = () => {
      tracking = false
    }

    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchend', onTouchEnd, { passive: true })
    el.addEventListener('touchcancel', onTouchCancel, { passive: true })

    // Swipes forwarded out of sandboxed artifact/app iframes (which swallow
    // touches): accept only gestures whose frame lives INSIDE this container
    // — the containment check gives them the same routing a direct touch
    // would get by bubbling. The frame's runtime already applied the same
    // thresholds; only the mobile gate re-applies here.
    const offIframe = onIframeSwipe(({ el: frame, dir }) => {
      if (!isMobile()) return
      if (!el.contains(frame)) return
      if (dir === 'right') callbacksRef.current.onSwipeRight?.()
      else callbacksRef.current.onSwipeLeft?.()
    })

    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchend', onTouchEnd)
      el.removeEventListener('touchcancel', onTouchCancel)
      offIframe()
    }
  }, [ref, minDistance, minVelocity, directionRatio, mobileOnly, excludeTags, excludeClasses])
}
