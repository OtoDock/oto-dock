import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchActiveChats, type ActiveChat } from '../api/chats'
import { useChatStore, NEW_CHAT_PREFIX, type ChatStreamPhase } from '../store/chatStore'

// One row of the cross-agent "Active now" widget (sidebar + agent home).
export interface ActiveChatRow {
  id: string
  agent: string
  title: string
  phase: 'streaming' | 'warming' | 'finished'
  /** From the seed metadata: 'task' rows render purple and click through to
      the agent's task history instead of the chat page. Undefined until the
      seed supplies it (store-only rows) — treated as a plain chat. */
  sourceType?: string
}

// How long a just-finished row lingers before it leaves the panel — so a chat
// doesn't vanish under the user's cursor the instant its turn closes. A row
// that finished UNREAD stays past the linger (fixed "done" styling + dot)
// until the viewer actually opens it — finishing must not hide a result the
// user never saw (operator ask, 2026-07-11).
export const FINISHED_LINGER_MS = 4_000
// Foreign active ids (streaming per the store, unknown to the last seed —
// e.g. a chat that STARTED after the seed) trigger one seed refetch, at most
// this often.
const RESEED_MIN_INTERVAL_MS = 2_000

/** Live cross-agent active-chats feed.
 *
 * No polling: the WS events the client already ingests (`chat_status`,
 * `chat_status_snapshot`, `warmup_*`) keep `chatStore` current for EVERY chat
 * this user may see; a single `GET /v1/chats/active` seed supplies the
 * metadata (title/agent) those events don't carry. Foreign chats that start
 * mid-session re-seed once (debounced). Rows whose turn closes linger briefly
 * as `finished`, then drop.
 *
 * `enabled=false` returns [] without fetching — for conditional consumers
 * (AppFrame enables it only when the app declares the `active_chats` feed).
 */
export function useActiveChats(enabled = true): ActiveChatRow[] {
  const seed = useQuery({
    queryKey: ['active-chats'],
    queryFn: fetchActiveChats,
    staleTime: 10_000,
    enabled,
  })
  const byChat = useChatStore((s) => s.byChat)

  // (id, phase) pairs the store says are live right now. New-chat draft
  // slices (no real chat id yet) are skipped.
  const storeActive = useMemo(() => {
    const out: { id: string; phase: 'streaming' | 'warming' }[] = []
    for (const [cid, slice] of Object.entries(byChat)) {
      if (cid.startsWith(NEW_CHAT_PREFIX)) continue
      const st: ChatStreamPhase = slice.status
      if (st === 'streaming' || st === 'warming') out.push({ id: cid, phase: st })
    }
    return out
  }, [byChat])

  const metaById = useMemo(() => {
    const m = new Map<string, ActiveChat>()
    for (const row of seed.data || []) m.set(row.id, row)
    return m
  }, [seed.data])

  // Seed rows the store has no slice for yet (page just loaded, snapshot not
  // processed): trust the server — it asserted "streaming". A slice that
  // exists and says ready/failed WINS over a stale seed row. Seed rows the
  // server reports as 'finished' (finished-unread backfill) never count as
  // active — they render through the finished path below.
  const activePairs = useMemo(() => {
    const pairs = new Map<string, 'streaming' | 'warming'>()
    for (const { id, phase } of storeActive) pairs.set(id, phase)
    for (const row of seed.data || []) {
      if (row.status === 'finished') continue
      if (!pairs.has(row.id) && !byChat[row.id]) pairs.set(row.id, 'streaming')
    }
    return pairs
  }, [storeActive, seed.data, byChat])

  // Re-seed (debounced) when an active id has no SEED metadata — any chat
  // that went live after the last seed. The store's WS slice carries the
  // agent but never the TITLE, so gating this on "store knows the agent"
  // (the old condition) left own-agent rows renderable but permanently
  // titled "New chat" — visible in the sidebar's task mode, where own live
  // chats stay in the strip (chat mode dedups them into the list below).
  // Once per id: the seed enumerates currently-streaming ids server-side,
  // so a single refetch after the id appears either supplies the metadata
  // or never will (visibility-filtered) — retrying would be polling.
  const lastReseedRef = useRef(0)
  const reseededIdsRef = useRef(new Set<string>())
  useEffect(() => {
    if (!enabled) return
    const unknown = [...activePairs.keys()].filter(
      (id) => !metaById.has(id) && !reseededIdsRef.current.has(id),
    )
    if (unknown.length === 0) return
    const now = Date.now()
    if (now - lastReseedRef.current < RESEED_MIN_INTERVAL_MS) return
    lastReseedRef.current = now
    for (const id of unknown) reseededIdsRef.current.add(id)
    void seed.refetch()
  }, [activePairs, metaById, seed, enabled])

  // Finished retention: ids that WERE shown and just left the active set stay
  // as phase 'finished' — for FINISHED_LINGER_MS always, and PAST the linger
  // while the chat is still unread (the store's unread flips false the moment
  // the viewer opens it, dropping the row). The sweep timer only prunes
  // entries that are both past-linger AND read; render re-checks the same
  // predicate, so a stale map entry can never paint a row.
  const [finished, setFinished] = useState<Map<string, number>>(new Map())
  const prevActiveRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    const current = new Set(activePairs.keys())
    const left = [...prevActiveRef.current].filter((id) => !current.has(id))
    prevActiveRef.current = current
    if (left.length === 0) return
    const leaveAt = Date.now() + FINISHED_LINGER_MS
    setFinished((prev) => {
      const next = new Map(prev)
      for (const id of left) next.set(id, leaveAt)
      return next
    })
    const t = setTimeout(() => {
      setFinished((prev) => {
        const now = Date.now()
        const live = useChatStore.getState().byChat
        const next = new Map(
          [...prev].filter(([id, at]) => at > now || live[id]?.unread),
        )
        return next.size === prev.size ? prev : next
      })
    }, FINISHED_LINGER_MS + 50)
    return () => clearTimeout(t)
  }, [activePairs])

  return useMemo(() => {
    if (!enabled) return []
    const now = Date.now()
    const rows: ActiveChatRow[] = []
    const shown = new Set<string>()
    const add = (id: string, phase: ActiveChatRow['phase']) => {
      const meta = metaById.get(id)
      const agent = meta?.agent || byChat[id]?.agent || ''
      if (!agent) return // nothing renderable yet; the re-seed will supply it
      shown.add(id)
      rows.push({
        id, agent, title: meta?.title || 'New chat', phase,
        sourceType: meta?.source_type,
      })
    }
    for (const [id, phase] of activePairs) add(id, phase)
    for (const [id, at] of finished) {
      if (activePairs.has(id)) continue
      if (at > now || byChat[id]?.unread) add(id, 'finished')
    }
    // Reload backfill: seed rows that arrived already finished-unread (the
    // in-session leaver path above never saw them stream). A store read echo
    // (unread === false) retires them before the next seed refetch.
    for (const row of seed.data || []) {
      if (row.status !== 'finished' || shown.has(row.id) || activePairs.has(row.id)) continue
      if (byChat[row.id]?.unread === false || !row.unread) continue
      add(row.id, 'finished')
    }
    const order = { streaming: 0, warming: 1, finished: 2 } as const
    rows.sort(
      (a, b) => order[a.phase] - order[b.phase] || a.agent.localeCompare(b.agent),
    )
    return rows
  }, [activePairs, finished, metaById, byChat, enabled, seed.data])
}
