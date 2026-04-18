import { useEffect, useRef } from 'react'

// Apps-UI open/close rules (operator, 2026-07-12): (a) ARRIVING on an
// agent's HOME with pinned apps opens the overlay — every arrival, including
// agent switches (the old sessionStorage once-per-session guard suppressed
// those) and chat→home; (b) entering ANY chat closes it and never
// auto-opens (deep links, Active-now clicks — ActiveChatsPanel navigates
// directly, bypassing handleSelectChat's own close — and AgentChat's
// send-time close stays). Keyed on the ROUTE param, not the internal chat
// id: the page component is reused across navigations and the internal id
// can be stale on a cross-chat switch. The pending-arrival ref bridges the
// async pins load and is consumed exactly once, so a pins refetch can't
// re-open the overlay after the user closed it by hand.
export function useAppsAutoOpen(
  agentName: string | undefined,
  urlChatId: string | undefined,
  pinnedApps: readonly unknown[] | undefined,
  setAppsOpen: (open: boolean) => void,
): void {
  const prevLocRef = useRef<{ agent?: string; chat?: string }>({})
  const pendingHomeOpenRef = useRef(false)
  useEffect(() => {
    const prev = prevLocRef.current
    prevLocRef.current = { agent: agentName, chat: urlChatId }
    if (!agentName) return
    if (urlChatId) {
      pendingHomeOpenRef.current = false
      if (urlChatId !== prev.chat) setAppsOpen(false)
      return
    }
    if (prev.agent !== agentName || prev.chat) pendingHomeOpenRef.current = true
    if (pendingHomeOpenRef.current && pinnedApps) {
      pendingHomeOpenRef.current = false
      if (pinnedApps.length) setAppsOpen(true)
    }
    // setAppsOpen is a useState setter (stable); listing it keeps the linter
    // honest without re-running the effect.
  }, [agentName, urlChatId, pinnedApps, setAppsOpen])
}
