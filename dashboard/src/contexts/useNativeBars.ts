// A page that paints its own bars (the agents map) hands the apps the colour
// its theme class gives it while it shows.
import { useEffect, type RefObject } from 'react'
import { endBarsOverride, startBarsOverride } from '../lib/device/nativeBars'

/** While `active`, the apps' bars take the page colour the element's own
 *  theme class gives it (`--ot-surface-page` read on the element), and the
 *  theme's colour comes back when it ends. */
export function useNativeBarsFrom(ref: RefObject<HTMLElement | null>, active: boolean) {
  useEffect(() => {
    if (!active || !ref.current) return
    const colour = getComputedStyle(ref.current).getPropertyValue('--ot-surface-page').trim()
    if (!colour) return
    startBarsOverride(colour)
    return endBarsOverride
  }, [ref, active])
}
