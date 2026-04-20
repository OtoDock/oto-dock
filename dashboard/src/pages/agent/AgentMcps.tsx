import { useState, useEffect, useRef } from 'react'
import { useParams } from 'react-router-dom'
import {
  useAgentMcps,
  useSetAgentMcps,
  useAgentSkills,
  AgentSkill,
  AgentMcpsNotVisibleError,
} from '../../api/mcps'
import CommunityMcpsBrowser from '../../components/CommunityMcpsBrowser'
import { ServiceAccountBindingDropdown } from '../../components/ServiceAccountBindingDropdown'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'

export default function AgentMcps() {
  const { name } = useParams<{ name: string }>()
  const { data: mcpData, isLoading, refetch } = useAgentMcps(name!)
  const { data: skills } = useAgentSkills(name!)
  const setMcps = useSetAgentMcps()
  const { user } = useAuth()
  const canManage = canManageAgent(user, name!)
  const agentRole = user?.agent_roles?.[name!]

  // `selected` mirrors the in-flight enabled set the manager is editing.
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [saved, setSaved] = useState(false)
  const [showBrowse, setShowBrowse] = useState(false)
  const [query, setQuery] = useState('')
  // When the backend rejects a name (admin revoked between fetch and save),
  // surface the offending names in a banner so the manager understands what
  // changed. We re-fetch automatically; the banner stays for ~5s.
  const [staleNames, setStaleNames] = useState<string[] | null>(null)

  // Autosave bookkeeping: one PUT in flight at a time — rapid clicks queue
  // the LATEST set so an out-of-order older write can't regress a newer one.
  const busyRef = useRef(false)
  const queuedRef = useRef<Set<string> | null>(null)

  useEffect(() => {
    // Don't clobber a just-clicked toggle while its autosave is in flight;
    // the post-save refetch lands with the write settled and syncs cleanly.
    if (mcpData && !busyRef.current && !queuedRef.current) {
      setSelected(new Set(mcpData.mcps.filter(m => m.enabled).map(m => m.name)))
    }
  }, [mcpData])

  if (isLoading || !mcpData) {
    return <div className="text-sm text-p-text-light">Loading...</div>
  }

  // Autosave per toggle (platform convention — the Save button lived at the
  // top of a long list and scrolled out of view; un-saved toggles got
  // silently lost).
  const saveSet = (next: Set<string>) => {
    if (busyRef.current) { queuedRef.current = next; return }
    busyRef.current = true
    setMcps.mutate(
      { agent: name!, mcps: Array.from(next) },
      {
        onSuccess: () => {
          setSaved(true)
          setTimeout(() => setSaved(false), 2000)
        },
        onError: err => {
          if (err instanceof AgentMcpsNotVisibleError) {
            setStaleNames(err.notVisible)
            setTimeout(() => setStaleNames(null), 5000)
          }
          // Resync the toggles to the server's truth on any failure.
          refetch()
        },
        onSettled: () => {
          busyRef.current = false
          const queued = queuedRef.current
          queuedRef.current = null
          if (queued) saveSet(queued)
        },
      },
    )
  }

  const handleToggle = (mcpName: string) => {
    const next = new Set(selected)
    if (next.has(mcpName)) next.delete(mcpName)
    else next.add(mcpName)
    setSelected(next)
    saveSet(next)
  }

  const q = query.trim().toLowerCase()
  const filteredMcps = q
    ? mcpData.mcps.filter(m =>
        (m.label || '').toLowerCase().includes(q) ||
        (m.description || '').toLowerCase().includes(q) ||
        m.name.toLowerCase().includes(q),
      )
    : mcpData.mcps
  // The search also narrows the Skills list — match a skill's id, its parent
  // MCP label, or its description.
  const filteredSkills = q && skills
    ? skills.filter(s =>
        s.id.toLowerCase().includes(q) ||
        (s.mcp_label || '').toLowerCase().includes(q) ||
        (s.description || '').toLowerCase().includes(q),
      )
    : (skills || [])

  return (
    <div>
      {!canManage && (
        <div className="mb-6 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-xl px-4 py-3 text-xs text-amber-800 dark:text-amber-200">
          <strong>Read-only.</strong> MCP assignments and service-account bindings
          are owner-only.{' '}
          {agentRole === 'editor'
            ? 'As an editor you can collaborate on the agent\'s shared workspace; agent behavior is curated by an owner.'
            : 'As a viewer you can see which MCPs the agent uses but only owners can change them.'}
        </div>
      )}
      {/* MCP Assignments */}
      <div className="mb-8">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center mb-4">
          <div className="flex items-center gap-3">
            <h2 className="text-base font-bold text-p-text">MCP Assignments</h2>
            {saved && <span className="text-xs text-green-600">Saved</span>}
          </div>
          <div className="flex items-center gap-2 sm:ml-auto sm:flex-1 sm:max-w-md">
            <div className="relative flex-1 min-w-0">
              <svg className="w-4 h-4 absolute left-2.5 top-1/2 -translate-y-1/2 text-p-text-light pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
              </svg>
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search MCPs…"
                className="w-full pl-8 pr-7 py-1.5 text-sm rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
              />
              {query && (
                <button
                  onClick={() => setQuery('')}
                  aria-label="Clear search"
                  className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 flex items-center justify-center rounded-sm text-p-text-light hover:text-p-text hover:bg-p-surface-hover"
                >
                  ×
                </button>
              )}
            </div>
            <button
              onClick={() => setShowBrowse(true)}
              className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text hover:bg-p-surface-hover transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
              </svg>
              Browse<span className="hidden sm:inline"> Community</span>
            </button>
          </div>
        </div>

        {staleNames && staleNames.length > 0 && (
          <div className="mb-3 px-3 py-2 rounded-lg border border-amber-300 bg-amber-50 dark:border-amber-900/50 dark:bg-amber-900/20 text-xs text-amber-800 dark:text-amber-300">
            These MCPs are no longer available for this agent (admin revoked
            access): <strong>{staleNames.join(', ')}</strong>. The list has been
            refreshed.
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {filteredMcps.map(mcp => (
            <div
              key={mcp.name}
              className="px-3 py-2.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface hover:bg-p-surface-hover/50 transition-colors"
            >
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={selected.has(mcp.name)}
                  onChange={() => handleToggle(mcp.name)}
                  className="w-4 h-4 rounded-sm border-gray-300 text-brand focus:ring-brand accent-brand"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center flex-wrap gap-1">
                    <span className="text-sm text-p-text">{mcp.label}</span>
                    {mcp.authorized_by === 'admin' && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded-sm bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400"
                        title="Made available by admin via an instance assignment. You can still enable or disable it for this agent."
                      >
                        via admin
                      </span>
                    )}
                  </div>
                  {mcp.description && (
                    <p className="text-xs text-p-text-light mt-0.5 line-clamp-2">
                      {mcp.description}
                    </p>
                  )}
                </div>
              </label>
              {/* Service-account binding dropdown — only for MCPs whose
                  manifest declares one (probing every row painted a 400 in
                  the console per non-capable MCP; the dropdown still
                  self-hides if its options call fails). */}
              {mcp.has_service_account && selected.has(mcp.name) && user && name && (
                <ServiceAccountBindingDropdown
                  agentName={name}
                  mcpName={mcp.name}
                  callerSub={user.sub}
                />
              )}
            </div>
          ))}
        </div>
        {filteredMcps.length === 0 && (
          <p className="text-sm text-p-text-light py-4">No MCPs match your search.</p>
        )}
      </div>

      {/* Skills (read-only — MCP skills are tied to their MCP) */}
      {filteredSkills.length > 0 && (
        <div>
          <h2 className="text-base font-bold text-p-text mb-2">Skills</h2>
          <p className="text-xs text-p-text-light mb-3">Skills are auto-activated when their MCP is assigned.</p>
          <div className="space-y-2">
            {filteredSkills.map((skill: AgentSkill) => (
              <div
                key={skill.id}
                className="flex items-center gap-3 px-3 py-2.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface"
              >
                <span className="w-2 h-2 rounded-full bg-green-500 shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-mono text-brand">{skill.id}</span>
                    <span className="text-xs text-p-text-light">from {skill.mcp_label}</span>
                  </div>
                  {skill.description && (
                    <p className="text-xs text-p-text-secondary mt-0.5">{skill.description}</p>
                  )}
                </div>
                {skill.exclude_from.length > 0 && (
                  <span className="text-[10px] text-p-text-light shrink-0">
                    excl: {skill.exclude_from.join(', ')}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <CommunityMcpsBrowser
        open={showBrowse}
        onClose={() => setShowBrowse(false)}
        agentSlug={name}
      />
    </div>
  )
}
