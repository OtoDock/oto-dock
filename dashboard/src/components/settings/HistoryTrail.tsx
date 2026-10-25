import { useEffect, useRef } from 'react'
import { useLocation, useNavigationType } from 'react-router-dom'
import { INITIAL_LOCATION_KEY, clearTrail, currentIdx, saveEntry } from './history'

/**
 * Records each rendered location's pathname into the tab's trail
 * (`history.ts`; the query never, a sign-in token could sit in it).
 * Mounted once in `App`, AFTER `NavigationGuard`: sibling effects run in
 * order, so on a cold load the guard has already moved the browser to its
 * own entries and this one skips the location it replaced (the key no
 * longer matches) and records the guard's entry on the next render. Its
 * effect also runs before any route's, so a settings area reading the
 * trail sees the current entry recorded.
 */
export default function HistoryTrail() {
  const location = useLocation()
  const action = useNavigationType()
  const mounted = useRef(false)

  useEffect(() => {
    // A load from outside the app (the first mount on an entry React Router
    // never keyed): a trail copied into a new tab, or left by what the tab
    // showed before, is not this tab's history. A later return to that
    // same unkeyed entry keeps the trail.
    if (!mounted.current) {
      mounted.current = true
      if (location.key === INITIAL_LOCATION_KEY) clearTrail()
    }
    const idx = currentIdx(location.key)
    if (idx === null) return
    saveEntry(idx, location.pathname, action)
  }, [location.key, location.pathname, action])

  return null
}
