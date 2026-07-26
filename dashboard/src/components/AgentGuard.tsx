import { useEffect, useRef } from 'react'
import { Outlet, Navigate, useParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useAgents } from '../api/agents'

export default function AgentGuard() {
  const { name } = useParams<{ name: string }>()
  const { user, refreshUser } = useAuth()
  const isAdmin = user?.role === 'admin'
  // Live agent list (admins see all; others see their assignments). The
  // session snapshot (`user.agents`, fetched once at app mount) drifts in
  // BOTH directions: a deleted agent lingers in it until the JWT refreshes,
  // and an agent GRANTED from another session (an admin assigning a member)
  // is missing from it — while /v1/agents reflects the DB immediately. For
  // non-admins the live list only ever contains accessible agents
  // (discovery filters on can_access_agent per request), so membership in
  // EITHER source proves access.
  const { data: agents, isLoading, isError } = useAgents({ all: isAdmin })

  const inSnapshot = !isAdmin && !!user && !!name && user.agents.includes(name)
  const inLive = !!name && !!agents?.some((a) => a.name === name)

  // Grant-direction drift detected (live list has it, snapshot doesn't):
  // heal the snapshot ONCE per slug — `user.agent_roles` rides the same
  // /auth/me payload and drives the manage UI (Configuration tab etc.), so
  // without this a freshly granted manager gets a viewer-looking page until
  // a full reload. The latch never refires if /auth/me still lacks the slug
  // (grant revoked mid-flight — the existence gate bounces on a later poll).
  const healedForRef = useRef<string | null>(null)
  useEffect(() => {
    if (!user || isAdmin || !name || inSnapshot || !inLive) return
    if (healedForRef.current === name) return
    healedForRef.current = name
    refreshUser().catch(() => { /* transient — manage UI heals on reload */ })
  }, [user, isAdmin, name, inSnapshot, inLive, refreshUser])

  if (!user || !name) return <Navigate to="/agents" replace />

  // Non-admins must have the agent in their assignments. Snapshot says no →
  // consult the live list before bouncing: wait for it (brief; usually
  // already cached by the grid), pass when it contains the slug. A query
  // ERROR keeps the old instant bounce — never blank-screen until the next
  // poll on the say-so of an unreachable API.
  if (!isAdmin && !inSnapshot) {
    if (isError) return <Navigate to="/agents" replace />
    if (isLoading || !agents) return null
    if (!inLive) return <Navigate to="/agents" replace />
  }

  // Existence gate: once the live list has loaded, a slug that isn't in it has
  // been deleted — send the user back to /agents rather than render a dead
  // chat URL (its chats are gone server-side and warmup is refused anyway).
  // Wait for load so we don't bounce on first paint.
  if (!isLoading && agents && !agents.some(a => a.name === name)) {
    return <Navigate to="/agents" replace />
  }

  return <Outlet />
}
