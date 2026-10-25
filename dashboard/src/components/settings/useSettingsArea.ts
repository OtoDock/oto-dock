import { useCallback, useEffect, useRef } from 'react'
import { useLocation, useNavigate, useNavigationType } from 'react-router-dom'
import {
  areaEntryBehind, currentIdx, fallbackChat, planBackToChat, readTrail, saveEntry,
} from './history'
import { isNarrowViewport, useNarrow } from './useNarrow'

/** How long "Back to Chat" waits for the entry under the areas to show
 *  before it pushes the chat from where it is instead. */
const TRAVEL_WAIT_MS = 1000

export interface SettingsAreaNav {
  /** Under 768 px: the index shows the list, a section its own page. */
  narrow: boolean
  /** On the area's index path. */
  onIndex: boolean
  /** The section page's back arrow and swipe right. */
  goBack: () => void
  /** The "Back to Chat" button. */
  backToChat: () => void
}

/**
 * The history behaviour of one settings area (`history.ts` has the model).
 * On a phone a section is its own history entry, so the back arrow,
 * browser back, the Android back gesture and swipe right all pop one entry;
 * a section that opens with no entry of this area behind it (a cold deep
 * link, the Setup wizard's landing, a link from a chat or from another
 * area) gets the area's list injected behind it first. `agent` is the
 * agent area's slug: its "Back to Chat" stays on that agent's chats.
 */
export function useSettingsArea(base: string, agent?: string): SettingsAreaNav {
  const location = useLocation()
  const action = useNavigationType()
  const navigate = useNavigate()
  const narrow = useNarrow()
  const onIndex = location.pathname === base
  // A travel of Back to Chat in flight: a second tap does nothing, and the
  // travel ends when the browser shows another location (the arrival, the
  // fallback's push, or anything the person did meanwhile).
  const travellingRef = useRef(false)
  useEffect(() => {
    travellingRef.current = false
  }, [location.key])

  // The injected list entry. Only on a navigation (never on a fold: the
  // effect keys on the location), never on a traversal (the entries behind
  // exist), and only while the browser still shows this location (a second
  // run, StrictMode's or a sibling's navigation, finds the key moved on).
  useEffect(() => {
    if (action === 'POP' || location.pathname === base || !isNarrowViewport()) return
    const idx = currentIdx(location.key)
    if (idx === null) return
    const trail = readTrail()
    if (areaEntryBehind(trail, idx, base)) return
    const here = location.pathname + location.search
    if (idx === 0) {
      // Nothing behind at all (the Setup wizard's replace landing): the
      // root goes first, so back from the list reaches the chat.
      navigate('/', { replace: true })
      saveEntry(0, '/', 'REPLACE')
      navigate(base)
      saveEntry(1, base, 'PUSH')
      navigate(here, { state: location.state })
      saveEntry(2, location.pathname, 'PUSH')
      return
    }
    navigate(base, { replace: true })
    saveEntry(idx, base, 'REPLACE')
    navigate(here, { state: location.state })
    saveEntry(idx + 1, location.pathname, 'PUSH')
  }, [location.key, location.pathname, location.search, location.state, action, base, navigate])

  const goBack = useCallback(() => {
    const idx = currentIdx(location.key)
    if (idx !== null && areaEntryBehind(readTrail(), idx, base)) navigate(-1)
    else navigate(base, { replace: true })
  }, [location.key, base, navigate])

  const backToChat = useCallback(() => {
    if (travellingRef.current) return
    const trail = readTrail()
    const target = fallbackChat(trail, agent)
    const idx = currentIdx(location.key)
    if (idx === null) {
      navigate(target, { replace: true })
      return
    }
    const plan = planBackToChat(trail, idx, target)
    if (plan.kind === 'pop') {
      travellingRef.current = true
      navigate(plan.delta)
      return
    }
    if (plan.delta === 0) {
      navigate(plan.target, { replace: plan.kind === 'replace' })
      return
    }
    // Travel to the entry under the areas first. The cold root is replaced
    // by the chat, so it stays entry 0 and the Android back gesture on it
    // minimizes the app as before the settings were opened; any other page
    // stays behind the chat, pushed over the areas. If the browser never
    // arrives (an entry the tab no longer holds), the chat is pushed from
    // the entry the tap was made on, and nothing is erased; after a move
    // of the person's own meanwhile, nothing happens at all.
    const at = idx + plan.delta
    const keyAtTap = (window.history.state as { key?: string } | null)?.key
    travellingRef.current = true
    let done = false
    const finish = (arrived: boolean) => {
      if (done) return
      done = true
      window.removeEventListener('popstate', onPop)
      window.clearTimeout(timer)
      if (arrived) {
        navigate(plan.target, { replace: plan.kind === 'replace' })
        return
      }
      const st = window.history.state as { key?: string } | null
      if (st?.key === keyAtTap) navigate(plan.target)
    }
    const onPop = () => {
      const st = window.history.state as { idx?: number } | null
      if (st?.idx === at) finish(true)
    }
    const timer = window.setTimeout(() => finish(false), TRAVEL_WAIT_MS)
    window.addEventListener('popstate', onPop)
    navigate(plan.delta)
  }, [location.key, agent, navigate])

  return { narrow, onIndex, goBack, backToChat }
}
