/**
 * Browse Community Agents — centered modal + card grid.
 *
 * Mirrors `CommunityMcpsBrowser.tsx` layout (centered modal, not a side
 * drawer) so it renders well on phones and small screens. One list: the
 * departments first, then the agents, with a filter on top (All,
 * Departments, Agents). Each card is the template panel (`TemplatePanel`),
 * contracted until "Show details" opens it whole with its folded lines
 * (the tools by name, the apps and checks). A member agent says which
 * department it belongs to and opens it; a department's Show details opens
 * each agent of its team in place. "Install" opens `AgentInstallModal` or
 * `DepartmentInstallModal`, which show the same panel.
 */

import { useMemo, useState, type ReactNode } from 'react'

import {
  CommunityAgentRegistryEntry,
  type CommunityDepartmentSet,
  useCommunityAgents,
  useCommunitySets,
} from '../api/communityAgents'
import { safeHref } from '../lib/safeUrl'
import { memberOfLine } from '../lib/status/departmentSet'
import { appsCountLine, type TemplateApp } from '../lib/status/templateApps'
import AgentInstallModal from './AgentInstallModal'
import AgentUpdateModal from './AgentUpdateModal'
import DepartmentInstallModal from './departments/DepartmentInstallModal'
import RequiredTools from './RequiredTools'
import TemplateApps from './common/TemplateApps'
import TemplatePanel from './common/TemplatePanel'
import Dialog from './ui/Dialog'
import Button from './ui/Button'
import { Input, Select } from './ui/Field'
import { BUTTON_BASE, BUTTON_SIZE, CARD, DIALOG_PANEL, SEGMENT } from './ui/classes'

interface Props {
  open: boolean
  onClose: () => void
  /** `sets`: open on the departments (the Departments tab's link). */
  section?: 'agents' | 'sets'
}

type View = 'all' | 'departments' | 'agents'

/** The list's filter, the first the default. */
const VIEWS: [View, string][] = [['all', 'All'], ['departments', 'Departments'], ['agents', 'Agents']]

/** The catalog's apps of a template, as the apps line reads them. */
function storeApps(entry: CommunityAgentRegistryEntry | undefined): TemplateApp[] {
  return (entry?.apps ?? []).map(a => ({
    slug: a.slug, title: a.title, visibility: a.visibility, description: a.description, adds_server: a.has_server,
    picture: a.picture_url ? `/v1/community/agents/${entry!.slug}/pictures/${a.slug}.png` : undefined,
  }))
}

/** The two folded lines of one template, as the store has them: the tools
 *  by name, the apps and checks. */
function storeLines(entry: CommunityAgentRegistryEntry | undefined): ReactNode {
  if (!entry) return null
  return (
    <>
      <RequiredTools names={entry.required_mcps} />
      <TemplateApps store apps={storeApps(entry)} checks={entry.checks ?? []} />
    </>
  )
}

export default function CommunityAgentsBrowser({ open, onClose, section = 'agents' }: Props) {
  const { data, isLoading, isError } = useCommunityAgents(open)
  const sets = useCommunitySets(open)
  const [search, setSearch] = useState('')
  const [categoryFilter, setCategoryFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [view, setView] = useState<View>(section === 'sets' ? 'departments' : VIEWS[0][0])
  const [expandedSet, setExpandedSet] = useState<string | null>(null)
  const [selectedTemplate, setSelectedTemplate] = useState<CommunityAgentRegistryEntry | null>(null)
  const [selectedSet, setSelectedSet] = useState<CommunityDepartmentSet | null>(null)
  const [updateSlug, setUpdateSlug] = useState<string | null>(null)
  const onlyInstalled = statusFilter === 'installed'
  const onlyNew = statusFilter === 'not_installed'
  const setsOf = useMemo(() => {
    const out: Record<string, CommunityDepartmentSet[]> = {}
    for (const s of sets.data?.departments ?? []) {
      for (const m of s.members) (out[m.template] ??= []).push(s)
    }
    return out
  }, [sets.data])
  const templatesBySlug = useMemo(
    () => Object.fromEntries((data?.agents ?? []).map((a) => [a.slug, a])), [data])
  const setList = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (sets.data?.departments ?? []).filter((s) => {
      if (onlyInstalled && s.installed.length === 0) return false
      if (onlyNew && s.installed.length > 0) return false
      if (!q) return true
      const hay = `${s.slug} ${s.name} ${s.description} ${s.tags.join(' ')} ${s.members.map((m) => m.display_name).join(' ')}`.toLowerCase()
      return hay.includes(q)
    })
  }, [sets.data, search, onlyInstalled, onlyNew])

  const filtered = useMemo(() => {
    const agents = data?.agents ?? []
    const q = search.trim().toLowerCase()
    return agents.filter(a => {
      if (categoryFilter !== 'all' && a.category !== categoryFilter) return false
      if (onlyInstalled && (a.installed_as ?? []).length === 0) return false
      if (onlyNew && (a.installed_as ?? []).length > 0) return false
      if (q) {
        const hay = `${a.slug} ${a.display_name} ${a.description} ${a.tags.join(' ')} ${a.author}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      return true
    })
  }, [data, search, categoryFilter, onlyInstalled, onlyNew])

  const categories = useMemo(() => {
    const seen = new Set<string>()
    ;(data?.agents ?? []).forEach(a => seen.add(a.category))
    return Array.from(seen).sort()
  }, [data])

  if (!open) return null

  const showSets = view !== 'agents' && setList.length > 0
  const showAgents = view !== 'departments'
  // A member agent's link: the department's card, opened and in view.
  const openDepartment = (s: CommunityDepartmentSet) => {
    if (view === 'agents') setView('all')
    setExpandedSet(s.slug)
    setTimeout(() => document.querySelector(`[data-testid="set-card-${s.slug}"]`)?.scrollIntoView?.({ block: 'start' }), 0)
  }
  const installDepartmentOf = (template: CommunityAgentRegistryEntry | null) => {
    const s = template ? setsOf[template.slug]?.[0] : undefined
    return s ? { name: s.name, onInstall: () => { setSelectedTemplate(null); setSelectedSet(s) } } : null
  }

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
                Ready-made agents and departments to install.
                {data?.updated_at && (
                  <> Updated {new Date(data.updated_at).toLocaleString()}.</>
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

          {/* Filters — the view on its own line, then search and the two
              dropdowns: mobile wraps them, desktop keeps one row. */}
          <div className="flex flex-wrap items-center gap-2 px-5 py-3 border-b border-subtle">
            <div className="flex gap-1 w-full" role="group" aria-label="Show" data-testid="store-view">
              {VIEWS.map(([v, label]) => (
                <button
                  key={v}
                  type="button"
                  aria-pressed={view === v}
                  onClick={() => setView(v)}
                  className={`${BUTTON_BASE} ${BUTTON_SIZE.xs} ${view === v ? SEGMENT.on : SEGMENT.off}`}
                >
                  {label}
                </button>
              ))}
            </div>
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
          <div className="flex-1 overflow-y-auto px-5 py-4 bg-surface-page">
            {isLoading && (
              <div className="text-sm text-tertiary">Loading the community list...</div>
            )}
            {isError && (
              <div className="rounded-card border border-status-danger-border bg-status-danger-bg p-3 text-sm text-status-danger-fg">
                Could not load the community list. Try again in a minute.
              </div>
            )}

            {showSets && (
              <section className="mb-5" data-testid="community-sets" aria-labelledby="community-sets-heading">
                <h4 id="community-sets-heading" className="text-sm font-semibold text-primary">Departments</h4>
                <p className="text-xs text-tertiary mt-0.5 mb-2">
                  A department is several agents installed together as one team.
                </p>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3 items-start">
                  {setList.map(s => (
                    <SetCard
                      key={s.slug}
                      set={s}
                      templates={templatesBySlug}
                      expanded={expandedSet === s.slug}
                      onToggle={() => setExpandedSet(e => (e === s.slug ? null : s.slug))}
                      onInstall={() => setSelectedSet(s)}
                      onInstallAgent={(t) => setSelectedTemplate(t)}
                    />
                  ))}
                </div>
              </section>
            )}

            {showAgents && (
              <section aria-labelledby="community-agents-heading">
                {showSets && <h4 id="community-agents-heading" className="text-sm font-semibold text-primary mb-2">Agents</h4>}
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
                      memberOf={setsOf[agent.slug]?.[0]}
                      onOpenDepartment={openDepartment}
                      onInstall={() => setSelectedTemplate(agent)}
                      onInstallDepartment={(s) => setSelectedSet(s)}
                      onUpdate={slug => setUpdateSlug(slug)}
                    />
                  ))}
                </div>
              </section>
            )}
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
        memberOf={installDepartmentOf(selectedTemplate)}
        onClose={() => setSelectedTemplate(null)}
      />
      {updateSlug && (
        <AgentUpdateModal open agentSlug={updateSlug} onClose={() => setUpdateSlug(null)} />
      )}
      <DepartmentInstallModal
        open={!!selectedSet}
        set={selectedSet}
        templates={templatesBySlug}
        onClose={() => setSelectedSet(null)}
      />
    </>
  )
}


// ---------------------------------------------------------------------------
// A department's card
// ---------------------------------------------------------------------------

function SetCard({ set, templates, expanded, onToggle, onInstall, onInstallAgent }: {
  set: CommunityDepartmentSet
  templates: Record<string, CommunityAgentRegistryEntry>
  expanded: boolean
  onToggle: () => void
  onInstall: () => void
  onInstallAgent: (template: CommunityAgentRegistryEntry) => void
}) {
  const [member, setMember] = useState<string | null>(null)
  const blocked = set.problems.length > 0 || set.missing_members.length > 0 || !set.platform_compat_ok
  const entries = set.members.map(m => templates[m.template]).filter((e): e is CommunityAgentRegistryEntry => Boolean(e))
  const tools = [...new Map(entries.flatMap(e => e.required_mcps).map(t => [t.name, t])).values()]
  const groups = set.members.map(m => ({
    key: m.template, title: m.display_name, apps: storeApps(templates[m.template]), checks: templates[m.template]?.checks ?? [],
  }))
  const opened = expanded && member ? templates[member] : undefined
  return (
    <div className={`${CARD} p-4 flex flex-col gap-3`} data-testid={`set-card-${set.slug}`}>
      {opened ? (
        <div className="flex flex-col gap-3" data-testid={`set-member-panel-${opened.slug}`}>
          <button type="button" className="self-start text-xs text-secondary underline" onClick={() => setMember(null)}>
            Back to {set.name}
          </button>
          <TemplatePanel
            kind="agent"
            variant="full"
            name={opened.display_name}
            color={opened.color}
            version={opened.version}
            author={opened.author}
            summary={opened.summary}
            description={opened.description}
            features={opened.highlights ?? []}
            memberOf={<p className="text-xs text-secondary">{memberOfLine(set.name)}</p>}
            folded={storeLines(opened)}
          />
          <Button size="xs" className="self-start" onClick={() => onInstallAgent(opened)}>Install only this agent</Button>
        </div>
      ) : (
        <TemplatePanel
          kind="department"
          variant={expanded ? 'full' : 'card'}
          name={set.name}
          color={set.color || set.members[0]?.color}
          version={set.version}
          author={set.author}
          agents={set.members.length}
          summary={set.summary}
          description={set.description}
          features={set.highlights ?? []}
          team={{
            members: set.members, levels: set.levels, colors: templates,
            onOpen: expanded ? (t) => setMember(t) : undefined,
          }}
          folded={(
            <>
              <RequiredTools names={tools} />
              <TemplateApps store apps={groups.flatMap(g => g.apps)} checks={groups.flatMap(g => g.checks)} groups={groups} />
            </>
          )}
        />
      )}
      {set.installed.length > 0 && (
        <div className="text-2xs text-status-success-fg truncate">✓ Installed as: {set.installed.map(d => d.name).join(', ')}</div>
      )}
      {blocked && (
        <div className="text-2xs text-status-danger-fg">
          {!set.platform_compat_ok ? 'Needs a newer OtoDock.'
            : set.problems.length > 0 ? 'This department has errors in the catalog.'
            : 'An agent of this department is not in the catalog.'}
        </div>
      )}
      <div className="flex items-center gap-2 mt-auto">
        <Button onClick={() => { setMember(null); onToggle() }}>
          {expanded ? 'Hide details' : 'Show details'}
        </Button>
        <Button variant="primary" size="xs" className="flex-1" onClick={onInstall} disabled={blocked}>
          Install the department
        </Button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// An agent's card
// ---------------------------------------------------------------------------

function AgentCard({
  agent,
  memberOf,
  onOpenDepartment,
  onInstall,
  onInstallDepartment,
  onUpdate,
}: {
  agent: CommunityAgentRegistryEntry
  memberOf?: CommunityDepartmentSet
  onOpenDepartment: (s: CommunityDepartmentSet) => void
  onInstall: () => void
  onInstallDepartment: (s: CommunityDepartmentSet) => void
  onUpdate: (agentSlug: string) => void
}) {
  const installedAs = agent.installed_as ?? []
  const installed = installedAs.length > 0
  const updatable = (agent.installed ?? []).filter(i => i.update_available)
  const [expanded, setExpanded] = useState(false)
  const author = safeHref(agent.author_url) ? (
    <a href={safeHref(agent.author_url)} target="_blank" rel="noreferrer" className="hover:underline" title="Template author">
      {agent.author}
    </a>
  ) : agent.author

  const memberLine = memberOf && (expanded ? (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs" data-testid={`sets-of-${agent.slug}`}>
      <span className="text-secondary">{memberOfLine(memberOf.name)}</span>
      <Button size="xs" onClick={() => onInstallDepartment(memberOf)}>Install the whole department</Button>
    </div>
  ) : (
    <button type="button" onClick={() => onOpenDepartment(memberOf)} data-testid={`sets-of-${agent.slug}`}
            className="self-start text-2xs text-secondary underline decoration-dotted underline-offset-2">
      {memberOfLine(memberOf.name)}
    </button>
  ))

  return (
    <div className={`${CARD} p-4 flex flex-col gap-3`} data-testid={`agent-card-${agent.slug}`}>
      <TemplatePanel
        kind="agent"
        variant={expanded ? 'full' : 'card'}
        name={agent.display_name}
        color={agent.color}
        version={agent.version}
        author={author}
        summary={agent.summary}
        description={agent.description}
        features={agent.highlights ?? []}
        memberOf={memberLine}
        folded={storeLines(agent)}
      />
      {agent.deprecated && (
        <div className="text-2xs text-status-danger-fg">⚠ Deprecated</div>
      )}

      {/* Tags, when no highlights say what it does */}
      {!expanded && agent.tags.length > 0 && !(agent.highlights?.length) && (
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

      {/* What it comes with; the tools, apps and checks have their own lines once opened */}
      <div className="text-2xs text-secondary flex flex-wrap items-center gap-x-3 gap-y-1">
        {!expanded && (
          <span>
            <span className="font-medium text-primary">{agent.required_mcps.length}</span>{' '}
            tool{agent.required_mcps.length !== 1 ? 's' : ''}
          </span>
        )}
        {agent.has_setup && <span>· a setup guide</span>}
        {agent.has_tasks && <span>· scheduled tasks</span>}
        {agent.has_triggers && <span>· triggers</span>}
        {agent.has_notifications && <span>· reminders</span>}
        {!expanded && agent.has_apps && <span>· shared apps</span>}
        {!expanded && agent.has_user_apps && <span>· an app for each member</span>}
        {!expanded && agent.has_checks && <span>· checks of its work</span>}
        {agent.has_context && <span>· notes it always reads</span>}
      </div>

      {/* A closed card names its apps in one line; each app's line and picture are under Show details */}
      {!expanded && (agent.apps?.length ?? 0) > 0 && (
        <p className="text-xs font-medium text-primary" data-testid={`apps-of-${agent.slug}`}>
          {appsCountLine(storeApps(agent))}
        </p>
      )}
      {expanded && agent.category && (
        <div className="text-3xs text-tertiary">Category: {agent.category}</div>
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
        <Button onClick={() => setExpanded(e => !e)}>
          {expanded ? 'Hide details' : 'Show details'}
        </Button>
        <Button variant="primary" size="xs" className="flex-1" onClick={onInstall}>
          Install
        </Button>
      </div>
    </div>
  )
}
