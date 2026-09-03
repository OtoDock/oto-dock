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
  /** 'task' rows render purple and click through to the run view instead of
      the chat page. Derived from the durable `task-run-` id prefix first,
      the seed's source_type second — so a live task classifies correctly
      before its seed row exists. Undefined (store-only, non-task rows) is
      treated as a plain chat. */
  sourceType?: string
  /** Backend `owner_is_shared`: legacy `agent::`-owned rows a visibility flip
      left behind. Undefined until the seed row supplies it. */
  ownerIsShared?: boolean
}

// How long a just-finished row lingers before it leaves the panel — so a chat
// doesn't vanish under the user's cursor the instant its turn closes. A row
// that finished UNREAD stays past the linger (fixed "done" styling + dot)
// until the viewer actually opens it — finishing must not hide a result the
// user never saw (operator ask, 2026-07-11).
export const FINISHED_LINGER_MS = 4_000
// Active ids with no seed metadata (a chat that went live after the last
// seed) re-trigger the seed fetch — bounded per id: attempts spaced at least
// this far apart, at most RESEED_MAX_ATTEMPTS in total. A warming chat's
// first refetch can legitimately return empty (the endpoint's warming set
// races the registry write), so a single shot is not enough — but an id the
// server will NEVER include (visibility-filtered) must not poll forever.
const RESEED_MIN_INTERVAL_MS = 2_000
const RESEED_MAX_ATTEMPTS = 4
// MODULE-scoped, not per-mount: the hook is live in ~4-6 places at once
// (ResponsiveDrawer double-mounts its children, ChatHistory mounts it twice,
// plus home + AppFrame) and every instance resolves against the same
// ['active-chats'] cache — per-mount maps would multiply the per-id cap by
// the mount census. Exported for tests only.
export const _reseedAttempts = new Map<string, { count: number; lastAt: number }>()

/** Live cross-agent active-chats feed.
 *
 * No polling: the WS events the client already ingests (`chat_status`,
 * `chat_status_snapshot`, `warmup_*`) keep `chatStore` current for EVERY chat
 * this user may see; a single `GET /v1/chats/active` seed supplies the
 * metadata (title/agent) those events don't carry. Chats that start
 * mid-session re-seed with a bounded per-id retry (see `_reseedAttempts`).
 * Rows whose turn closes linger briefly as `finished`, then drop.
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
  // processed): trust the server — but ONLY its 'streaming' assertion. A
  // slice that exists and says ready/failed WINS over a stale seed row.
  // 'finished' rows (finished-unread backfill) render through the finished
  // path below. 'warming' rows are metadata-only: warmup frames are
  // per-socket, never broadcast, so a non-initiating tab has no store slice
  // to retire the row with — asserting it as active would paint a phantom
  // that lingers after a failed warmup. Their title/agent still feed
  // metaById, which is what the initiating tab's placeholder row needs.
  const activePairs = useMemo(() => {
    const pairs = new Map<string, 'streaming' | 'warming'>()
    for (const { id, phase } of storeActive) pairs.set(id, phase)
    for (const row of seed.data || []) {
      if (row.status !== 'streaming') continue
      if (!pairs.has(row.id) && !byChat[row.id]) pairs.set(row.id, 'streaming')
    }
    return pairs
  }, [storeActive, seed.data, byChat])

  // Re-seed when an active id has no SEED metadata — any chat that went live
  // after the last seed. The store's WS slice carries the agent but never the
  // TITLE, so without the seed the row stays "New chat". A one-shot burn is
  // not enough (the Bug A shape): the refetch fired while the chat was still
  // warming used to return empty and permanently spend the id's only chance —
  // when the turn actually opened, nothing retried. Now each unresolved id
  // may retry on every status transition (this effect re-runs on activePairs)
  // AND on a short timer (covers "no transition arrives"), spaced and capped
  // by the module-scoped attempt map above.
  useEffect(() => {
    if (!enabled) return
    const attempt = () => {
      const now = Date.now()
      let due = false
      for (const id of activePairs.keys()) {
        if (metaById.has(id)) continue
        const rec = _reseedAttempts.get(id) || { count: 0, lastAt: 0 }
        if (rec.count >= RESEED_MAX_ATTEMPTS) continue
        if (now - rec.lastAt < RESEED_MIN_INTERVAL_MS) continue
        _reseedAttempts.set(id, { count: rec.count + 1, lastAt: now })
        due = true
      }
      // One refetch covers every due id; sibling mounts see the updated map
      // synchronously, so concurrent instances stay within the shared cap.
      if (due) void seed.refetch()
    }
    attempt()
    const t = setInterval(attempt, RESEED_MIN_INTERVAL_MS + 100)
    return () => clearInterval(t)
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
        // The id prefix is the durable task marker (scheduler chats are
        // `task-run-…`), so classification never waits on the seed — a live
        // task with no seed row yet must not render (or filter) as a chat.
        sourceType: id.startsWith('task-run-') ? 'task' : meta?.source_type,
        ownerIsShared: meta?.owner_is_shared,
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
