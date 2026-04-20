import { Outlet, Navigate, useParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useAgents } from '../api/agents'

export default function AgentGuard() {
  const { name } = useParams<{ name: string }>()
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  // Live agent list (admins see all; others see their assignments). Used to
  // detect a deleted agent — its slug lingers in the session's `user.agents`
  // until the JWT refreshes, but it's gone from /v1/agents immediately.
  const { data: agents, isLoading } = useAgents({ all: isAdmin })

  if (!user || !name) return <Navigate to="/agents" replace />

  // Non-admins must have the agent in their assignments (cheap session check).
  if (!isAdmin && !user.agents.includes(name)) {
    return <Navigate to="/agents" replace />
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
