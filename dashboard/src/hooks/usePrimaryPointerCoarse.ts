import { useEffect, useState } from 'react'

const QUERY = '(pointer: coarse)'

function matchesNow(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia(QUERY).matches
}

/**
 * True when the device's PRIMARY pointer is a finger: phones, tablets, the
 * Android app. Unlike useCoarsePointer there is no width heuristic, so a
 * narrow desktop window keeps its keyboard behaviour. Read on the first
 * render, so a phone never paints the desktop variant first.
 */
export function usePrimaryPointerCoarse(): boolean {
  const [coarse, setCoarse] = useState(matchesNow)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(QUERY)
    const update = () => setCoarse(mq.matches)
    update()
    mq.addEventListener?.('change', update)
    return () => mq.removeEventListener?.('change', update)
  }, [])
  return coarse
}
