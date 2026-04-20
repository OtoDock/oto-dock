import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useAgents, useSetDefaultAgent } from '../api/agents'
import AgentCard from '../components/AgentCard'
import AgentInstallModal from '../components/AgentInstallModal'
import CommunityAgentsBrowser from '../components/CommunityAgentsBrowser'

export default function AgentGrid() {
  const { data: agents, isLoading } = useAgents()
  const { user, setUser } = useAuth()
  const setDefaultAgent = useSetDefaultAgent()
  const [confirmAgent, setConfirmAgent] = useState<string | null>(null)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [showBrowse, setShowBrowse] = useState(false)

  const canManage = user?.role === 'admin' || user?.role === 'creator'

  const handleSetDefault = (name: string) => {
    setConfirmAgent(name)
  }

  const handleConfirm = () => {
    if (!confirmAgent) return
    const agent = confirmAgent
    setConfirmAgent(null)
    setDefaultAgent.mutate(agent, {
      onSuccess: () => {
        // Update user in auth context so the UI reflects immediately
        if (user) setUser({ ...user, default_agent: agent })
      },
    })
  }

  const displayName = (name: string) =>
    name.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())

  // Show the user's favorite (default) agent first, keeping every other agent
  // in its normal (backend) order. Unfavoriting restores the natural position.
  const sortedAgents = useMemo(() => {
    if (!agents) return agents
    const fav = user?.default_agent
    if (!fav) return agents
    return [...agents].sort((a, b) =>
      a.name === fav ? -1 : b.name === fav ? 1 : 0,
    )
  }, [agents, user?.default_agent])

  return (
    <div className="min-h-screen bg-p-bg">
      {/* Top bar — standard "Back to Chat" button (matches agent settings / admin) */}
      <div className="flex items-center h-12 px-4 bg-white/80 dark:bg-gray-900/80 backdrop-blur-sm border-b border-p-border-light">
        <Link
          to="/"
          className="flex items-center justify-center gap-1.5 w-full sm:w-auto px-3 sm:px-8 py-1.5 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
          </svg>
          Back to Chat
        </Link>
      </div>

      {/* Content */}
      <main className="p-6 max-w-5xl mx-auto">
        {/* Title + actions. On mobile the buttons drop to their own line below. */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between mb-6">
          <div>
            <h1 className="text-lg font-bold text-p-text">OtoDock</h1>
            <p className="text-sm text-p-text-secondary">Select an agent</p>
          </div>
          {canManage && (
            <div className="flex items-center gap-2">
              {/* Browse Community is the primary action — most users install a
                  pre-built agent far more often than they build one from scratch. */}
              <button
                onClick={() => setShowBrowse(true)}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors flex items-center gap-1.5"
                title="Install a pre-built agent from the community catalog"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35m1.85-5.65a7.5 7.5 0 11-15 0 7.5 7.5 0 0115 0z" />
                </svg>
                Browse Community
              </button>
              <button
                onClick={() => setShowCreateModal(true)}
                className="px-4 py-2 rounded-lg text-sm font-medium text-p-text-secondary bg-p-surface hover:bg-p-surface-hover transition-colors flex items-center gap-1.5"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                </svg>
                Create Agent
              </button>
            </div>
          )}
        </div>

        {isLoading ? (
          <p className="text-sm text-p-text-secondary">Loading agents...</p>
        ) : !sortedAgents || sortedAgents.length === 0 ? (
          <p className="text-sm text-p-text-secondary">No agents found.</p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
            {sortedAgents.map((agent) => (
              <AgentCard
                key={agent.name}
                agent={agent}
                isDefault={user?.default_agent === agent.name}
                onSetDefault={handleSetDefault}
              />
            ))}
          </div>
        )}
      </main>

      {/* Confirmation popup */}
      {confirmAgent && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs"
          onClick={() => setConfirmAgent(null)}
        >
          <div
            className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light shadow-xl p-6 max-w-sm mx-4"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-semibold text-p-text mb-2">Set Default Agent</h3>
            <p className="text-sm text-p-text-secondary mb-5">
              Set <span className="font-medium text-p-text">
                {agents?.find(a => a.name === confirmAgent)?.display_name || displayName(confirmAgent)}
              </span> as your default agent? This will be the agent loaded when you open the dashboard.
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setConfirmAgent(null)}
                className="px-4 py-2 rounded-lg text-sm font-medium text-p-text-secondary
                           bg-p-surface hover:bg-p-surface-hover transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleConfirm}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white
                           bg-brand hover:bg-brand-hover transition-colors"
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Create Agent modal (unified create + install) */}
      <AgentInstallModal
        open={showCreateModal}
        mode="create"
        onClose={() => setShowCreateModal(false)}
      />

      {/* Browse Community Agents drawer */}
      <CommunityAgentsBrowser
        open={showBrowse}
        onClose={() => setShowBrowse(false)}
      />
    </div>
  )
}
