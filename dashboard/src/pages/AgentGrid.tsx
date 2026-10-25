import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { isCreatorOrAbove } from '../lib/permissions'
import { toastError } from '../lib/http/apiJson'
import { useAgents, useSetDefaultAgent, type AgentSummary } from '../api/agents'
import { useDepartments, type Department } from '../api/departments'
import AgentCard from '../components/AgentCard'
import AgentInstallModal from '../components/AgentInstallModal'
import CommunityAgentsBrowser from '../components/CommunityAgentsBrowser'
import AlertDialog from '../components/ui/AlertDialog'
import Button from '../components/ui/Button'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT, CARD } from '../components/ui/classes'

const CARD_GRID = 'grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6'

/** One department as a collapsible group (the AI-Engines/DepartmentsEditor
 * pill idiom — header row is the summary, chevron toggles the body), holding
 * its member cards with their department role. Default EXPANDED on purpose:
 * a grid of agents must show its cards without a click (deliberate
 * divergence from the idiom's collapsed default). */
function DepartmentGroup({ dept, members, defaultAgent, onSetDefault }: {
  dept: Department
  members: { agent: AgentSummary; roleLabel?: string }[]
  defaultAgent: string | undefined
  onSetDefault: (name: string) => void
}) {
  const [expanded, setExpanded] = useState(true)
  return (
    // The group is the card; its member cards sit inside it on the page's
    // own colour. overflow-hidden clips the header's hover to the radius.
    <section className={`${CARD} overflow-hidden`}>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded(v => !v)}
        className="w-full flex items-center justify-between gap-3 p-4 text-left hover:bg-surface-hover transition-colors"
      >
        <div className="min-w-0">
          <div className="font-display text-lg font-semibold text-primary truncate">{dept.name}</div>
          <div className="text-sm text-secondary">
            {members.length} {members.length === 1 ? 'agent' : 'agents'}
          </div>
        </div>
        <svg
          className={`w-4 h-4 shrink-0 text-tertiary transition-transform ${expanded ? 'rotate-180' : ''}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {expanded && (
        <div className="px-4 pb-4 border-t border-subtle pt-4">
          {members.length === 0 ? (
            <p className="text-sm text-tertiary">
              No agents yet. Move one here from the company map or the
              agent's Configuration section.
            </p>
          ) : (
            <div className={CARD_GRID}>
              {members.map(({ agent, roleLabel }) => (
                <AgentCard
                  key={agent.name}
                  agent={agent}
                  isDefault={defaultAgent === agent.name}
                  onSetDefault={onSetDefault}
                  roleLabel={roleLabel}
                  surface="item"
                />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

export default function AgentGrid({ embedded = false }: { embedded?: boolean }) {
  const { data: agents, isLoading } = useAgents()
  const { data: departments } = useDepartments()
  const { user, setUser } = useAuth()
  const setDefaultAgent = useSetDefaultAgent()
  const [confirmAgent, setConfirmAgent] = useState<string | null>(null)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [showBrowse, setShowBrowse] = useState(false)

  const canManage = isCreatorOrAbove(user)

  const handleSetDefault = (name: string) => {
    setConfirmAgent(name)
  }

  const handleConfirm = () => {
    if (!confirmAgent) return
    const agent = confirmAgent
    setConfirmAgent(null)
    setDefaultAgent.mutateAsync(agent)
      .then(() => {
        // Update user in auth context so the UI reflects immediately
        if (user) setUser({ ...user, default_agent: agent })
      })
      .catch(toastError('The default agent was not set'))
  }

  const displayName = (name: string) =>
    name.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())

  // Departments as collapsible groups above the plain grid (operator
  // 2026-08-15). Grouping keys ride the agents payload itself
  // (department_id / department_level_id); useDepartments supplies names,
  // level labels, and the display order (name-ascending from the API). In-
  // group order matches the 3D map's stage: level rank asc, then slug asc
  // (the agents list arrives slug-sorted; the rank sort is stable). Members
  // with no/dangling level sort last with no role badge — the map DROPS
  // those from its amphitheater, the grid must not.
  const groups = useMemo(() => {
    if (!agents || !departments) return []
    const byDept = new Map<string, AgentSummary[]>()
    for (const a of agents) {
      if (!a.department_id) continue
      const list = byDept.get(a.department_id)
      if (list) list.push(a)
      else byDept.set(a.department_id, [a])
    }
    const out: { dept: Department; members: { agent: AgentSummary; roleLabel?: string }[] }[] = []
    for (const dept of departments) {
      const members = byDept.get(dept.id)
      // Zero VISIBLE members renders nothing (real for admins whose
      // checkbox set excludes a whole department they can still list) —
      // unless the department is empty for everyone (the feed's own
      // `members`, the server's truth): that one shows as a group with
      // no cards, as the map shows its dais, so it can be filled.
      if (!members?.length && dept.members.length > 0) continue
      const levels = [...dept.levels].sort((a, b) => a.rank - b.rank)
      const rankOf = new Map(levels.map(l => [l.id, l.rank]))
      const nameOf = new Map(levels.map(l => [l.id, l.name]))
      const sorted = [...(members ?? [])].sort((a, b) =>
        (rankOf.get(a.department_level_id ?? '') ?? Number.MAX_SAFE_INTEGER)
        - (rankOf.get(b.department_level_id ?? '') ?? Number.MAX_SAFE_INTEGER))
      out.push({
        dept,
        members: sorted.map(agent => ({
          agent,
          roleLabel: nameOf.get(agent.department_level_id ?? ''),
        })),
      })
    }
    return out
  }, [agents, departments])

  // Independent = no department OR a department that isn't rendered (the
  // map's own two-part test — an agent in a filtered-out department must
  // not silently vanish). Favorite-first applies HERE only; a favorite
  // inside a department stays in its group (the ★ still marks it).
  const sortedAgents = useMemo(() => {
    if (!agents) return agents
    const groupedIds = new Set(groups.map(g => g.dept.id))
    const independents = agents.filter(
      a => !a.department_id || !groupedIds.has(a.department_id),
    )
    const fav = user?.default_agent
    if (!fav) return independents
    return independents.sort((a, b) =>
      a.name === fav ? -1 : b.name === fav ? 1 : 0,
    )
  }, [agents, groups, user?.default_agent])

  return (
    <div className={embedded ? '' : 'min-h-screen bg-surface-page'}>
      {/* Top bar — standard "Back to Chat" button (matches agent settings /
          admin). Hidden when embedded in AgentsPage, which brings its own. */}
      {!embedded && (
        <div className="flex items-center h-12 px-4 bg-surface-page border-b border-subtle">
          <Link
            to="/"
            className={`${BUTTON_BASE} ${BUTTON_VARIANT.primary} ${BUTTON_SIZE.sm} w-full sm:w-auto sm:px-8`}
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
            </svg>
            Back to Chat
          </Link>
        </div>
      )}

      {/* Content */}
      <main className="p-6 max-w-5xl mx-auto">
        {/* Title + actions. On mobile the buttons drop to their own line below. */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between mb-6">
          <div>
            <h1 className="font-display text-lg font-bold text-primary">OtoDock</h1>
            <p className="text-sm text-secondary">Select an agent</p>
          </div>
          {canManage && (
            <div className="flex items-center gap-2">
              {/* Browse Community is the primary action — most users install a
                  pre-built agent far more often than they build one from scratch. */}
              <Button
                variant="primary"
                size="md"
                onClick={() => setShowBrowse(true)}
                title="Install a pre-built agent from the community list"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35m1.85-5.65a7.5 7.5 0 11-15 0 7.5 7.5 0 0115 0z" />
                </svg>
                Browse Community
              </Button>
              <Button size="md" onClick={() => setShowCreateModal(true)}>
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                </svg>
                Create Agent
              </Button>
            </div>
          )}
        </div>

        {isLoading ? (
          <p className="text-sm text-secondary">Loading agents...</p>
        ) : groups.length === 0 && (!sortedAgents || sortedAgents.length === 0) ? (
          <p className="text-sm text-secondary">No agents found.</p>
        ) : (
          <div className="space-y-6">
            {groups.map(({ dept, members }) => (
              <DepartmentGroup
                key={dept.id}
                dept={dept}
                members={members}
                defaultAgent={user?.default_agent}
                onSetDefault={handleSetDefault}
              />
            ))}
            {sortedAgents && sortedAgents.length > 0 && (
              <div className={CARD_GRID}>
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
          </div>
        )}
      </main>

      {confirmAgent && (
        <AlertDialog
          title="Set Default Agent"
          confirmLabel="Confirm"
          onConfirm={handleConfirm}
          onCancel={() => setConfirmAgent(null)}
        >
          Set <span className="font-medium text-primary">
            {agents?.find(a => a.name === confirmAgent)?.display_name || displayName(confirmAgent)}
          </span> as your default agent? This will be the agent loaded when you open the dashboard.
        </AlertDialog>
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
