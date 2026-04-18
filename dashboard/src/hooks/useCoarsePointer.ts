import { useEffect, useState } from 'react'

/**
 * True on touch / coarse-pointer devices (phones, tablets) or narrow viewports.
 * Used to switch the media players to mobile behaviour (tap toggles controls,
 * always-visible volume slider, double-tap seek).
 */
export function useCoarsePointer(): boolean {
  const [coarse, setCoarse] = useState(false)
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const mq = window.matchMedia('(pointer: coarse)')
    const update = () => setCoarse(mq.matches || window.innerWidth < 768)
    update()
    mq.addEventListener?.('change', update)
    window.addEventListener('resize', update)
    return () => {
      mq.removeEventListener?.('change', update)
      window.removeEventListener('resize', update)
    }
  }, [])
  return coarse
}
