import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useAgents } from '../api/agents'

export default function DefaultAgentRedirect() {
  const { user } = useAuth()
  const { data: agents, isLoading } = useAgents()

  if (!user) return null  // RequireAuth handles unauthenticated

  // Try user's explicit default, then first assigned agent
  const defaultAgent = user.default_agent
    || (user.agents && user.agents.length > 0 ? user.agents[0] : null)

  if (defaultAgent) {
    return <Navigate to={`/chat/${defaultAgent}`} replace />
  }

  // Admin with no assigned agents — pick first available agent from system
  if (user.role === 'admin' && agents && agents.length > 0) {
    return <Navigate to={`/chat/${agents[0].name}`} replace />
  }

  // Still loading agent list
  if (isLoading) {
    return <div className="flex h-screen items-center justify-center text-gray-400 text-sm">Loading...</div>
  }

  // No agents available at all
  return <Navigate to="/agents" replace />
}
