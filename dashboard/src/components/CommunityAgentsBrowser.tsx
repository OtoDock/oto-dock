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
import { safeHref } from '../lib/safeUrl'
import AgentInstallModal from './AgentInstallModal'
import AgentUpdateModal from './AgentUpdateModal'
import Dialog from './ui/Dialog'
import Button from './ui/Button'
import { Input, Select } from './ui/Field'
import { DIALOG_PANEL, BADGE_TONE } from './ui/classes'

interface Props {
  open: boolean
  onClose: () => void
}

export default function CommunityAgentsBrowser({ open, onClose }: Props) {
  const { data, isLoading, isError } = useCommunityAgents(open)
  const [search, setSearch] = useState('')
  const [categoryFilter, setCategoryFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [selectedTemplate, setSelectedTemplate] = useState<CommunityAgentRegistryEntry | null>(null)
  const [updateSlug, setUpdateSlug] = useState<string | null>(null)

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
      <Dialog
        onClose={onClose}
        placement="top"
        aria-label="Browse Community Agents"
        panelClassName={`${DIALOG_PANEL} max-w-6xl max-h-[calc(100vh-4rem)] flex flex-col overflow-hidden`}
      >
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-4 border-b border-subtle">
            <div>
              <h3 className="text-base font-semibold text-primary">Browse Community Agents</h3>
              <p className="text-xs text-tertiary mt-0.5">
                Pre-built agent templates, with the MCPs they need and their setup guides.
                {data?.updated_at && (
                  <> Community list updated {new Date(data.updated_at).toLocaleString()}.</>
                )}
              </p>
            </div>
            <button
              onClick={onClose}
              className="text-tertiary hover:text-primary text-lg leading-none"
            >
              &times;
            </button>
          </div>

          {/* Filters — mobile: search on its own line, the two dropdowns share
              the line below; desktop: all three in one row. */}
          <div className="flex flex-wrap items-center gap-2 px-5 py-3 border-b border-subtle">
            <Input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search by name, description, tag, author..."
              className="sm:flex-1 sm:min-w-[200px]"
            />
            <div className="flex gap-2 w-full sm:w-auto">
              <Select
                value={categoryFilter}
                onChange={e => setCategoryFilter(e.target.value)}
                className="flex-1 sm:flex-none sm:w-auto"
              >
                <option value="all">All categories</option>
                {categories.map(c => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </Select>
              <Select
                value={statusFilter}
                onChange={e => setStatusFilter(e.target.value)}
                className="flex-1 sm:flex-none sm:w-auto"
              >
                <option value="all">All statuses</option>
                <option value="installed">Installed</option>
                <option value="not_installed">Not installed</option>
              </Select>
            </div>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto px-5 py-4">
            {isLoading && (
              <div className="text-sm text-tertiary">Loading the community list...</div>
            )}
            {isError && (
              <div className="rounded-card border border-status-danger-border bg-status-danger-bg p-3 text-sm text-status-danger-fg">
                Could not load the community list. Try again in a minute.
              </div>
            )}
            {data && filtered.length === 0 && (
              <div className="text-sm text-tertiary">No agents match the current filters.</div>
            )}

            {/* items-start: an expanded card grows on its own — its row-mates
                keep their natural height instead of stretching to match. */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 items-start">
              {filtered.map(agent => (
                <AgentCard
                  key={agent.slug}
                  agent={agent}
                  onInstall={() => setSelectedTemplate(agent)}
                  onUpdate={slug => setUpdateSlug(slug)}
                />
              ))}
            </div>
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between px-5 py-3 border-t border-subtle">
            <p className="text-2xs text-tertiary">
              {data ? `${filtered.length} of ${data.agents.length} agents` : ''}
              {data?.fetched_from && (
                <>
                  {' · '}<a
                    className="underline"
                    href="https://github.com/OtoDock/community-agents"
                    target="_blank" rel="noreferrer"
                  >
                    community list on GitHub
                  </a>
                </>
              )}
            </p>
            <Button onClick={onClose}>
              Close
            </Button>
          </div>
      </Dialog>

      <AgentInstallModal
        open={!!selectedTemplate}
        mode="install"
        template={selectedTemplate}
        onClose={() => setSelectedTemplate(null)}
      />
      {updateSlug && (
        <AgentUpdateModal open agentSlug={updateSlug} onClose={() => setUpdateSlug(null)} />
      )}
    </>
  )
}


// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

function AgentCard({
  agent,
  onInstall,
  onUpdate,
}: {
  agent: CommunityAgentRegistryEntry
  onInstall: () => void
  onUpdate: (agentSlug: string) => void
}) {
  const installedAs = agent.installed_as ?? []
  const installed = installedAs.length > 0
  const updatable = (agent.installed ?? []).filter(i => i.update_available)
  const [expanded, setExpanded] = useState(false)

  // Heuristic: ~150 chars or two lines is roughly when "Show details" becomes
  // useful. We always offer the toggle when the description is long OR the
  // MCP list is non-trivial (≥3), so users can inspect the full feature set.
  const hasMoreContent =
    agent.description.length > 140 || agent.required_mcps.length >= 3 || agent.has_setup

  return (
    <div className="rounded-card border border-subtle p-4 flex flex-col gap-3 hover:border-default transition-colors">
      <div className="flex items-start gap-3">
        <div
          // eslint-disable-next-line otodock/no-raw-palette -- white initial on the template's own colour
          className="w-11 h-11 rounded-card shrink-0 flex items-center justify-center text-white text-base font-semibold"
          style={{ background: agent.color }}
        >
          {agent.display_name.charAt(0)}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-primary truncate">{agent.display_name}</div>
          <div className="text-2xs text-tertiary">
            v{agent.version} ·{' '}
            {safeHref(agent.author_url) ? (
              <a href={safeHref(agent.author_url)} target="_blank" rel="noreferrer" className="hover:underline" title="Template author">
                {agent.author}
              </a>
            ) : agent.author}
          </div>
          {agent.deprecated && (
            <div className="text-2xs text-status-danger-fg mt-1">⚠ Deprecated</div>
          )}
        </div>
      </div>

      {/* Description — clamped to 3 lines until expanded */}
      <p
        className={
          'text-xs text-secondary ' +
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
              className="px-1.5 py-0.5 rounded-sm text-3xs bg-surface-muted text-secondary"
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* Quick stats line */}
      <div className="text-2xs text-secondary flex flex-wrap items-center gap-x-3 gap-y-1">
        <span>
          <span className="font-medium text-primary">{agent.required_mcps.length}</span>{' '}
          MCP{agent.required_mcps.length !== 1 ? 's' : ''} required
        </span>
        {agent.has_setup && <span>· setup guide</span>}
        {agent.has_tasks && <span>· seeded tasks</span>}
        {agent.has_triggers && <span>· seeded triggers</span>}
        {agent.has_notifications && <span>· seeded notifications</span>}
        {agent.has_apps && <span>· shared apps</span>}
        {agent.has_user_apps && <span>· a personal app for each member</span>}
        {agent.has_checks && <span>· checks</span>}
        {agent.has_context && <span>· auto-context files</span>}
      </div>

      {/* Expanded section */}
      {expanded && (
        <div className="rounded-card bg-surface-muted p-3 text-xs">
          <div className="text-3xs uppercase tracking-wider text-tertiary mb-1.5 font-medium">
            Bundled MCPs
          </div>
          <ul className="space-y-1">
            {agent.required_mcps.map(mcp => (
              <li key={mcp.name} className="flex items-start gap-2">
                <span className="text-tertiary">•</span>
                <div className="flex-1 min-w-0">
                  <span className="font-mono">{mcp.name}</span>
                  {mcp.min_version && (
                    <span className="text-tertiary"> ≥{mcp.min_version}</span>
                  )}
                  {mcp.skills && mcp.skills.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-0.5">
                      {mcp.skills.map(skill => (
                        <span
                          key={skill}
                          className={`rounded-chip px-1.5 py-px text-3xs ${BADGE_TONE.info}`}
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
            <div className="mt-2 pt-2 border-t border-subtle text-3xs text-tertiary">
              Category: {agent.category}
            </div>
          )}
        </div>
      )}

      {installed && (
        <div className="text-2xs text-status-success-fg truncate">
          ✓ Installed as: {installedAs.join(', ')}
        </div>
      )}
      {updatable.map(i => (
        <div key={i.agent_slug} className="flex items-center justify-between gap-2 text-2xs" data-testid={`update-row-${i.agent_slug}`}>
          <span className="text-secondary truncate">
            <span className="font-mono">{i.agent_slug}</span>: {i.version || 'unknown'} → {agent.version}
          </span>
          <Button variant="secondary" size="xs" onClick={() => onUpdate(i.agent_slug)} className="whitespace-nowrap">
            Update
          </Button>
        </div>
      ))}

      <div className="flex items-center gap-2 mt-auto">
        {hasMoreContent && (
          <Button onClick={() => setExpanded(e => !e)}>
            {expanded ? 'Hide details' : 'Show details'}
          </Button>
        )}
        <Button variant="primary" size="xs" className="flex-1" onClick={onInstall}>
          Install
        </Button>
      </div>
    </div>
  )
}
