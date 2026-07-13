import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useAgents } from '../api/agents'

export default function DefaultAgentRedirect() {
  const { user } = useAuth()
  const { data: agents, isLoading } = useAgents()

  if (!user) return null  // RequireAuth handles unauthenticated

  // Fast path while the agent list loads: the explicit favorite, then the
  // auth payload's first assignment (avoids a loading flash for the common
  // case of a set favorite).
  const claimed = user.default_agent
    || (user.agents && user.agents.length > 0 ? user.agents[0] : null)

  if (isLoading) {
    if (claimed) return <Navigate to={`/chat/${claimed}`} replace />
    return <div className="flex h-screen items-center justify-center text-gray-400 text-sm">Loading...</div>
  }

  // List loaded: the favorite wins if it still exists, then the first agent
  // the user can actually see — the same access-filtered list the agent grid
  // renders, for EVERY role. (A member whose agents weren't in the auth
  // payload used to bounce straight back to /agents here, which read as a
  // dead Back-to-Chat button.) A favorite pointing at a deleted agent falls
  // through instead of navigating to a broken chat.
  const names = (agents ?? []).map(a => a.name)
  const target = [user.default_agent, claimed, names[0]]
    .find(n => !!n && names.includes(n))

  if (target) {
    return <Navigate to={`/chat/${target}`} replace />
  }

  // No agents available at all
  return <Navigate to="/agents" replace />
}
