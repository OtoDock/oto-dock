/**
 * Browse Community Agents — centered modal + card grid.
 *
 * Mirrors `CommunityMcpsBrowser.tsx` layout (centered modal, not a side
 * drawer) so it renders well on phones and small screens. Clicking a
 * card's "Install" button opens `AgentInstallModal` in install mode. The
 * card itself is expandable via a "Show details" toggle that reveals
 * the full description + the full required-MCPs list with skill chips.
 */

import { useMemo, useState } from 'react'

import {
  CommunityAgentRegistryEntry,
  useCommunityAgents,
} from '../api/communityAgents'
import AgentInstallModal from './AgentInstallModal'

interface Props {
  open: boolean
  onClose: () => void
}

export default function CommunityAgentsBrowser({ open, onClose }: Props) {
  const { data, isLoading, isError, error } = useCommunityAgents(open)
  const [search, setSearch] = useState('')
  const [categoryFilter, setCategoryFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [selectedTemplate, setSelectedTemplate] = useState<CommunityAgentRegistryEntry | null>(null)

  const filtered = useMemo(() => {
    const agents = data?.agents ?? []
    const q = search.trim().toLowerCase()
    return agents.filter(a => {
      if (categoryFilter !== 'all' && a.category !== categoryFilter) return false
      if (statusFilter === 'installed' && (a.installed_as ?? []).length === 0) return false
      if (statusFilter === 'not_installed' && (a.installed_as ?? []).length > 0) return false
      if (q) {
        const hay = `${a.slug} ${a.display_name} ${a.description} ${a.tags.join(' ')} ${a.author}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      return true
    })
  }, [data, search, categoryFilter, statusFilter])

  const categories = useMemo(() => {
    const seen = new Set<string>()
    ;(data?.agents ?? []).forEach(a => seen.add(a.category))
    return Array.from(seen).sort()
  }, [data])

  if (!open) return null

  return (
    <>
      <div
        className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-8 pb-8"
        onClick={onClose}
      >
        <div
          className="bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-p-border-light w-full max-w-6xl mx-4 max-h-[calc(100vh-4rem)] flex flex-col overflow-hidden"
          onClick={e => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-4 border-b border-p-border-light">
            <div>
              <h3 className="text-base font-semibold text-p-text">Browse Community Agents</h3>
              <p className="text-xs text-p-text-light mt-0.5">
                Pre-built agent templates with their MCP requirements + setup guides.
                {data?.updated_at && (
                  <> Catalog updated {new Date(data.updated_at).toLocaleString()}.</>
                )}
              </p>
            </div>
            <button
              onClick={onClose}
              className="text-p-text-light hover:text-p-text text-lg leading-none"
            >
              &times;
            </button>
          </div>

          {/* Filters — mobile: search on its own line, the two dropdowns share
              the line below; desktop: all three in one row. */}
          <div className="flex flex-wrap items-center gap-2 px-5 py-3 border-b border-p-border-light bg-gray-50/50 dark:bg-gray-900/30">
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search by name, description, tag, author..."
              className="w-full sm:flex-1 sm:min-w-[200px] text-sm px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-gray-800 text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/50"
            />
            <div className="flex gap-2 w-full sm:w-auto">
              <select
                value={categoryFilter}
                onChange={e => setCategoryFilter(e.target.value)}
                className="flex-1 sm:flex-none text-sm px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-gray-800 text-p-text"
              >
                <option value="all">All categories</option>
                {categories.map(c => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
              <select
                value={statusFilter}
                onChange={e => setStatusFilter(e.target.value)}
                className="flex-1 sm:flex-none text-sm px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-gray-800 text-p-text"
              >
                <option value="all">All statuses</option>
                <option value="installed">Installed</option>
                <option value="not_installed">Not installed</option>
              </select>
            </div>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto px-5 py-4">
            {isLoading && (
              <div className="text-sm text-p-text-light">Loading catalog...</div>
            )}
            {isError && (
              <div className="rounded-lg border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-3 text-sm text-red-700 dark:text-red-400">
                Failed to load catalog: {(error as Error)?.message || 'unknown error'}
              </div>
            )}
            {data && filtered.length === 0 && (
              <div className="text-sm text-p-text-light">No agents match the current filters.</div>
            )}

            {/* items-start: an expanded card grows on its own — its row-mates
                keep their natural height instead of stretching to match. */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 items-start">
              {filtered.map(agent => (
                <AgentCard
                  key={agent.slug}
                  agent={agent}
                  onInstall={() => setSelectedTemplate(agent)}
                />
              ))}
            </div>
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between px-5 py-3 border-t border-p-border-light bg-gray-50/50 dark:bg-gray-900/30">
            <p className="text-[11px] text-p-text-light">
              {data ? `${filtered.length} of ${data.agents.length} agents` : ''}
              {data?.fetched_from && (
                <>
                  {' · '}<a
                    className="underline"
                    href="https://github.com/OtoDock/community-agents"
                    target="_blank" rel="noreferrer"
                  >
                    catalog source
                  </a>
                </>
              )}
            </p>
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors"
            >
              Close
            </button>
          </div>
        </div>
      </div>

      <AgentInstallModal
        open={!!selectedTemplate}
        mode="install"
        template={selectedTemplate}
        onClose={() => setSelectedTemplate(null)}
      />
    </>
  )
}


// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

function AgentCard({
  agent,
  onInstall,
}: {
  agent: CommunityAgentRegistryEntry
  onInstall: () => void
}) {
  const installedAs = agent.installed_as ?? []
  const installed = installedAs.length > 0
  const [expanded, setExpanded] = useState(false)

  // Heuristic: ~150 chars or two lines is roughly when "Show details" becomes
  // useful. We always offer the toggle when the description is long OR the
  // MCP list is non-trivial (≥3), so users can inspect the full feature set.
  const hasMoreContent =
    agent.description.length > 140 || agent.required_mcps.length >= 3 || agent.has_setup

  return (
    <div className="rounded-xl border border-p-border-light bg-white dark:bg-gray-900/40 p-4 flex flex-col gap-3 hover:border-brand/40 transition-colors">
      <div className="flex items-start gap-3">
        <div
          className="w-11 h-11 rounded-lg shrink-0 flex items-center justify-center text-white text-base font-semibold"
          style={{ background: agent.color }}
        >
          {agent.display_name.charAt(0)}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-p-text truncate">{agent.display_name}</div>
          <div className="text-[11px] text-p-text-light">v{agent.version} · {agent.author}</div>
          {agent.deprecated && (
            <div className="text-[11px] text-red-500 mt-1">⚠ Deprecated</div>
          )}
        </div>
      </div>

      {/* Description — clamped to 3 lines until expanded */}
      <p
        className={
          'text-xs text-p-text-secondary ' +
          (expanded ? '' : 'line-clamp-3')
        }
      >
        {agent.description}
      </p>

      {/* Tags */}
      {agent.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {agent.tags.slice(0, 6).map(tag => (
            <span
              key={tag}
              className="px-1.5 py-0.5 rounded-sm text-[10px] bg-p-surface text-p-text-secondary"
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* Quick stats line */}
      <div className="text-[11px] text-p-text-secondary flex flex-wrap items-center gap-x-3 gap-y-1">
        <span>
          <span className="font-medium text-p-text">{agent.required_mcps.length}</span>{' '}
          MCP{agent.required_mcps.length !== 1 ? 's' : ''} required
        </span>
        {agent.has_setup && <span>· setup guide</span>}
        {agent.has_tasks && <span>· seeded tasks</span>}
        {agent.has_triggers && <span>· seeded triggers</span>}
        {agent.has_notifications && <span>· seeded notifications</span>}
        {agent.has_context && <span>· auto-context files</span>}
      </div>

      {/* Expanded section */}
      {expanded && (
        <div className="rounded-lg border border-p-border-light bg-gray-50/50 dark:bg-gray-900/40 p-3 text-xs">
          <div className="text-[10px] uppercase tracking-wider text-p-text-light mb-1.5 font-medium">
            Bundled MCPs
          </div>
          <ul className="space-y-1">
            {agent.required_mcps.map(mcp => (
              <li key={mcp.name} className="flex items-start gap-2">
                <span className="text-brand">•</span>
                <div className="flex-1 min-w-0">
                  <span className="font-mono">{mcp.name}</span>
                  {mcp.min_version && (
                    <span className="text-p-text-light"> ≥{mcp.min_version}</span>
                  )}
                  {mcp.skills && mcp.skills.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-0.5">
                      {mcp.skills.map(skill => (
                        <span
                          key={skill}
                          className="px-1 py-px rounded-sm text-[9px] bg-brand/10 text-brand"
                        >
                          {skill}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
          {agent.category && (
            <div className="mt-2 pt-2 border-t border-p-border-light/50 text-[10px] text-p-text-light">
              Category: {agent.category}
            </div>
          )}
        </div>
      )}

      {installed && (
        <div className="text-[11px] text-green-600 dark:text-green-400 truncate">
          ✓ Installed as: {installedAs.join(', ')}
        </div>
      )}

      <div className="flex items-center gap-2 mt-auto">
        {hasMoreContent && (
          <button
            onClick={() => setExpanded(e => !e)}
            className="text-[11px] px-2 py-1 rounded-sm border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors"
          >
            {expanded ? 'Hide details' : 'Show details'}
          </button>
        )}
        <button
          onClick={onInstall}
          className="flex-1 px-3 py-1.5 rounded-lg text-xs font-medium text-white bg-brand hover:bg-brand-hover transition-colors"
        >
          Install
        </button>
      </div>
    </div>
  )
}
