import { useEffect, useRef } from 'react'
import type { AgentSummary } from '../../api/agents'
import { isAdmin } from '../../lib/permissions'

/**
 * Heal the auth snapshot an admin's map grays by.
 *
 * An admin's grayed chip means "not a member", decided by `user.agents` —
 * the `/auth/me` snapshot fetched once at app mount. Every in-app path that
 * grants a role calls `refreshUser()`; an agent created by another agent
 * through the Agent Creator MCP (the creator is made its manager in the
 * same request) has no client to do so, and the map showed it gray until a
 * hard reload. No server event exists for "your agents changed", and the
 * map page holds no socket — so the polled agents list is the signal.
 *
 * Under "Mine only" the list IS the server's member set (discovery filters
 * on the admin's checkbox agents per request), so a slug in it but not in
 * the snapshot is drift. When the checkbox set is empty, and under
 * "Everything", the list holds genuine non-members too: those heal once on
 * the first settled data (one `/auth/me` per map mount — the reload the
 * operator had to do) and again whenever a slug APPEARS later (the poll
 * bringing an agent created meanwhile). Latched per slug, so a genuine
 * non-member never refires; a failed refresh un-latches its round so the
 * next poll retries.
 */
export function useSnapshotHeal({ agents, user, refreshUser, settled }: {
  agents: AgentSummary[]
  user: { role: string; agents: string[] } | null
  refreshUser: () => Promise<void>
  /** False while the list is pending or `keepPrevious` placeholder data. */
  settled: boolean
}) {
  const healed = useRef(new Set<string>())
  const snapshotKey = (user?.agents ?? []).join('\0')
  useEffect(() => {
    if (!user || !isAdmin(user) || !settled) return
    const inSnapshot = new Set(user.agents)
    const round: string[] = []
    for (const a of agents) {
      if (inSnapshot.has(a.name) || healed.current.has(a.name)) continue
      healed.current.add(a.name)
      round.push(a.name)
    }
    if (round.length === 0) return
    refreshUser().catch(() => {
      for (const slug of round) healed.current.delete(slug)
    })
    // `user` re-renders on every refresh; the joined snapshot is the
    // membership identity that matters here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, user?.role, snapshotKey, settled, refreshUser])
}
