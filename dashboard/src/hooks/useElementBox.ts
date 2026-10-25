// The box of a DOM element as external state: its viewport rectangle and
// the viewport's size, read again when the element resizes or the window
// does. Null until the element exists (the caller renders hidden until
// then, as a popover does). A snapshot keeps its identity while its numbers
// hold, so the subscriber re-renders only when the box changes. React reads
// the snapshot once more at the commit, so an element measured right after
// it mounts is placed before the browser paints.
import { useCallback, useRef, useSyncExternalStore } from 'react'

export interface ElementBox {
  top: number
  left: number
  width: number
  height: number
  viewportWidth: number
  viewportHeight: number
}

function same(a: ElementBox, b: ElementBox): boolean {
  return a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height
    && a.viewportWidth === b.viewportWidth && a.viewportHeight === b.viewportHeight
}

const nothing = () => null

/** `getElement` must keep its identity while the element does (a
 * `useCallback`): a new getter re-subscribes. */
export function useElementBox(getElement: () => HTMLElement | null, enabled = true): ElementBox | null {
  const last = useRef<ElementBox | null>(null)
  const subscribe = useCallback((notify: () => void) => {
    if (!enabled) return () => {}
    const el = getElement()
    const ro = el && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(notify) : null
    if (el) ro?.observe(el)
    window.addEventListener('resize', notify)
    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', notify)
    }
  }, [getElement, enabled])
  const getSnapshot = useCallback((): ElementBox | null => {
    if (!enabled) return null
    const el = getElement()
    if (!el) return null
    const r = el.getBoundingClientRect()
    const next: ElementBox = {
      top: r.top, left: r.left, width: r.width, height: r.height,
      viewportWidth: window.innerWidth, viewportHeight: window.innerHeight,
    }
    const prev = last.current
    if (prev && same(prev, next)) return prev
    last.current = next
    return next
  }, [getElement, enabled])
  return useSyncExternalStore(subscribe, getSnapshot, nothing)
}
