import { useEffect, useRef, type MutableRefObject } from 'react'

// Apps-UI open/close rules (operator, 2026-07-12 + 2026-08-15): (a) ARRIVING
// on an agent's HOME with pinned apps opens the overlay — every arrival,
// including agent switches (the old sessionStorage once-per-session guard
// suppressed those) and chat→home; (b) entering ANY chat closes it and never
// auto-opens (deep links, Active-now clicks — ActiveChatsPanel navigates
// directly, bypassing handleSelectChat's own close — and AgentChat's
// send-time close stays) — EXCEPT a phone-mode chat entry (a wake hit or the
// mic-button duplex start from home): when `keepOnChatEntryRef` is armed,
// the close consumes it and keeps/opens the panel instead (spoken mode wants
// the dashboards visible; a manual close afterwards sticks). The keep is
// consumed AT THE CLOSE SITE on purpose: the duplex mint sets internal chat
// state in one commit and react-router lands the URL rewrite in a later
// startTransition commit, so an effect-side "re-open after the close"
// one-shot always loses that race (live-hit 2026-08-14). Keyed on the ROUTE
// param, not the internal chat id: the page component is reused across
// navigations and the internal id can be stale on a cross-chat switch. The
// pending-arrival refs bridge the async pins load and are consumed exactly
// once, so a pins refetch can't re-open the overlay after the user closed
// it by hand.
export function useAppsAutoOpen(
  agentName: string | undefined,
  urlChatId: string | undefined,
  pinnedApps: readonly unknown[] | undefined,
  setAppsOpen: (open: boolean) => void,
  keepOnChatEntryRef?: MutableRefObject<boolean>,
): void {
  const prevLocRef = useRef<{ agent?: string; chat?: string }>({})
  const pendingHomeOpenRef = useRef(false)
  const pendingChatOpenRef = useRef(false)
  useEffect(() => {
    const prev = prevLocRef.current
    prevLocRef.current = { agent: agentName, chat: urlChatId }
    if (!agentName) return
    if (urlChatId) {
      pendingHomeOpenRef.current = false
      if (urlChatId !== prev.chat) {
        if (keepOnChatEntryRef?.current) {
          // Phone-mode entry: consume the keep, then open (or keep open)
          // once pins are known — the pending ref bridges a deep-link mount
          // where the panel starts closed and pins load later.
          keepOnChatEntryRef.current = false
          pendingChatOpenRef.current = true
        } else {
          pendingChatOpenRef.current = false
          setAppsOpen(false)
        }
      }
      if (pendingChatOpenRef.current && pinnedApps) {
        pendingChatOpenRef.current = false
        if (pinnedApps.length) setAppsOpen(true)
      }
      return
    }
    pendingChatOpenRef.current = false
    if (prev.agent !== agentName || prev.chat) pendingHomeOpenRef.current = true
    if (pendingHomeOpenRef.current && pinnedApps) {
      pendingHomeOpenRef.current = false
      if (pinnedApps.length) setAppsOpen(true)
    }
    // setAppsOpen is a useState setter (stable); listing it keeps the linter
    // honest without re-running the effect. keepOnChatEntryRef is a ref —
    // deliberately NOT a dep: it is read at close time only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName, urlChatId, pinnedApps, setAppsOpen])
}
