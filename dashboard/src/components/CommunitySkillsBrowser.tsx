/**
 * Browse Community Skills drawer.
 *
 * Sibling of CommunityMcpsBrowser over the community-skills catalog
 * (standalone SKILL.md packages). Same drawer UX: admin cards get inline
 * Install / Update with install-progress polling (skill installs share the
 * MCP catalog job registry); manager cards (agentSlug set) either enable
 * directly — installed packages are toggled via the agent-skills PATCH, which
 * auto-assigns the package — or fall back to a `kind: 'skill'` request through
 * the shared MCP request queue.
 *
 * One deliberate difference from the MCP catalog: an unreachable, uncached
 * skills registry degrades to an EMPTY catalog with `catalog_unreachable`
 * set — rendered here as a banner, never an error page.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  useCommunitySkills,
  useInstallCommunitySkill,
  useCatalogInstallJobs,
  useCreateMcpRequest,
  useCancelMcpRequest,
  CommunitySkillEntry,
  CatalogInstallJob,
} from '../api/community'
import { useAgentSkills, useSetAgentSkill } from '../api/mcps'
import { useAuth } from '../contexts/AuthContext'

interface Props {
  open: boolean
  onClose: () => void
  /** When set, status badges render per-agent. Otherwise admin/global. */
  agentSlug?: string
}

export default function CommunitySkillsBrowser({ open, onClose, agentSlug }: Props) {
  const { data, isLoading, isError, error } = useCommunitySkills(open, agentSlug)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')

  // Admin-only: poll in-flight catalog installs while the drawer is open —
  // skill packages ride the same job registry as MCP installs, so the
  // existing installs poll covers them (jobs keyed by name, names unique
  // across both catalogs).
  const qc = useQueryClient()
  const installJobsQuery = useCatalogInstallJobs(open && !agentSlug)
  const installJobs = useMemo(() => {
    const m = new Map<string, CatalogInstallJob>()
    for (const j of installJobsQuery.data?.installs ?? []) m.set(j.name, j)
    return m
  }, [installJobsQuery.data])

  // When a job goes terminal (running → done/failed), refresh the catalog +
  // admin MCP lists so the card flips Install → Installed (or shows failure).
  const prevStatus = useRef<Record<string, string>>({})
  useEffect(() => {
    let sawTerminal = false
    for (const j of installJobsQuery.data?.installs ?? []) {
      const prev = prevStatus.current[j.name]
      if (prev === 'running' && (j.status === 'done' || j.status === 'failed')) {
        sawTerminal = true
      }
      prevStatus.current[j.name] = j.status
    }
    if (sawTerminal) {
      qc.invalidateQueries({ queryKey: ['community-skills'] })
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
    }
  }, [installJobsQuery.data, qc])

  const filtered = useMemo(() => {
    if (!data) return []
    const q = search.trim().toLowerCase()
    return data.skills.filter(pkg => {
      if (statusFilter !== 'all') {
        const enabledHere = !!agentSlug && pkg.enabled_for_agents.includes(agentSlug)
        if (statusFilter === 'installed' && !pkg.installed) return false
        if (statusFilter === 'not_installed' && pkg.installed) return false
        if (statusFilter === 'enabled_here' && !enabledHere) return false
        if (statusFilter === 'pending_request' && !pkg.pending_request) return false
      }
      if (!q) return true
      const haystack = [
        pkg.name, pkg.label, pkg.description, pkg.author ?? '',
        ...(pkg.tags ?? []),
      ].join(' ').toLowerCase()
      return haystack.includes(q)
    })
  }, [data, search, statusFilter, agentSlug])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-8 pb-8" onClick={onClose}>
      <div
        className="bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-p-border-light w-full max-w-6xl mx-4 max-h-[calc(100vh-4rem)] flex flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-p-border-light">
          <div>
            <h3 className="text-base font-semibold text-p-text">Browse Community Skills</h3>
            <p className="text-xs text-p-text-light mt-0.5">
              {agentSlug
                ? `Showing skill packages available for ${agentSlug}.`
                : 'Catalog of installable skill packages from the OtoDock community.'}
              {data?.fetched_from && !data?.catalog_unreachable && (
                <>
                  {' '}Catalog updated {data.updated_at ? new Date(data.updated_at).toLocaleString() : ''}.
                </>
              )}
            </p>
          </div>
          <button onClick={onClose} className="text-p-text-light hover:text-p-text text-lg leading-none">&times;</button>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-2 px-5 py-3 border-b border-p-border-light bg-gray-50/50 dark:bg-gray-900/30">
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search by name, label, description, tag..."
            className="flex-1 min-w-[200px] text-sm px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-gray-800 text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/50"
          />
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            className="text-sm px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-gray-800 text-p-text"
          >
            <option value="all">All statuses</option>
            <option value="installed">Installed</option>
            <option value="not_installed">Not installed</option>
            {agentSlug && <option value="enabled_here">Enabled for this agent</option>}
            {agentSlug && <option value="pending_request">Request pending</option>}
          </select>
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
          {data?.catalog_unreachable && (
            <div className="mb-3 rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-3 text-sm text-amber-800 dark:text-amber-300">
              Skills catalog unreachable — showing nothing; check network / try later.
            </div>
          )}
          {data && !data.catalog_unreachable && filtered.length === 0 && (
            <div className="text-sm text-p-text-light">No skill packages match the current filters.</div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {filtered.map(pkg => (
              <Card key={pkg.name} pkg={pkg} agentSlug={agentSlug} job={installJobs.get(pkg.name)} />
            ))}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-5 py-3 border-t border-p-border-light bg-gray-50/50 dark:bg-gray-900/30">
          <p className="text-[11px] text-p-text-light">
            {data ? `${filtered.length} of ${data.skills.length} skill packages` : ''}
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
  )
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

function Card({ pkg, agentSlug, job }: { pkg: CommunitySkillEntry; agentSlug?: string; job?: CatalogInstallJob }) {
  const enabledHere = !!agentSlug && pkg.enabled_for_agents.includes(agentSlug)
  // Enablement rows live in the DB and can outlive the package files (e.g. a
  // container recreate before the skills volume existed, or a manual folder
  // delete). "Enabled but not installed" must offer a reinstall, never a
  // dead card — install re-links to the surviving rows.
  const needsReinstall = enabledHere && !pkg.installed
  const install = useInstallCommunitySkill()
  const installing = install.isPending || job?.status === 'running'
  // Hooks must be called unconditionally even when agentSlug is undefined —
  // the empty-string fallback yields hook instances the admin-global path
  // never actually calls (showAdminInstall short-circuits them).
  const createRequest = useCreateMcpRequest(agentSlug || '')
  const cancelRequest = useCancelMcpRequest(agentSlug || '')
  // Visibility check — when the package's skills appear in this agent's
  // skills list (visible standalone packages list even unassigned), the
  // manager can self-serve: PATCH-enabling a skill auto-assigns the package.
  const agentSkills = useAgentSkills(agentSlug || '')
  const setSkill = useSetAgentSkill()
  const { user, loading: authLoading } = useAuth()
  const isAdmin = user?.role === 'admin'
  const showAdminInstall = !agentSlug
  const [error, setError] = useState<string | null>(null)
  const [enabling, setEnabling] = useState(false)
  const [showReasonModal, setShowReasonModal] = useState(false)
  const [reasonInput, setReasonInput] = useState('')

  const pkgSkills = !!agentSlug
    ? (agentSkills.data ?? []).filter(s => s.mcp_name === pkg.name)
    : []
  // Manager can self-serve when the package is installed and visible to the
  // agent (its skills list in the tab) and not yet enabled. Otherwise the
  // only way in is via the request → admin-approve flow.
  const canEnableDirectly = pkg.installed && pkgSkills.length > 0 && !enabledHere

  const handleInstall = () => {
    setError(null)
    // 202 semantics — the initial POST failing surfaces here; a background
    // failure surfaces via the polled job below.
    install.mutate(pkg.name, {
      onError: e => setError((e as Error)?.message || 'Install failed'),
    })
  }

  useEffect(() => {
    if (job?.status === 'failed' && job.error) setError(job.error)
  }, [job?.status, job?.error])

  // Direct enable (manager with visible installed package): PATCH-enable each
  // of the package's skills — the first PATCH auto-assigns the package
  // server-side. exclude_from is preserved as-is.
  const handleEnableDirectly = async () => {
    if (!agentSlug || pkgSkills.length === 0) return
    setError(null)
    setEnabling(true)
    try {
      for (const s of pkgSkills) {
        await setSkill.mutateAsync({
          agent: agentSlug,
          skillId: s.id,
          enabled: true,
          exclude_from: s.exclude_from,
        })
      }
    } catch (e) {
      setError((e as Error)?.message || 'Enable failed')
    } finally {
      setEnabling(false)
    }
  }

  const openRequestModal = () => {
    setError(null)
    setReasonInput('')
    setShowReasonModal(true)
  }

  const submitRequest = () => {
    createRequest.mutate(
      { mcp_name: pkg.name, reason: reasonInput.trim(), kind: 'skill' },
      {
        onSuccess: () => setShowReasonModal(false),
        onError: e => setError((e as Error)?.message || 'Request failed'),
      },
    )
  }

  // Admin one-click path: same endpoint, no reason — the backend
  // auto-approves (install + enable cascade) synchronously.
  const submitAdminDirect = () => {
    setError(null)
    createRequest.mutate(
      { mcp_name: pkg.name, reason: '', kind: 'skill' },
      {
        onError: e => setError((e as Error)?.message || 'Install failed'),
      },
    )
  }

  const handleCancel = () => {
    if (!pkg.pending_request) return
    setError(null)
    cancelRequest.mutate(pkg.pending_request, {
      onError: e => setError((e as Error)?.message || 'Cancel failed'),
    })
  }

  return (
    <div className="rounded-lg border border-p-border-light bg-white dark:bg-p-surface p-3 flex flex-col gap-2">
      <div className="flex items-start gap-2">
        <Icon pkg={pkg} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <h4 className="text-sm font-medium text-p-text truncate">{pkg.label}</h4>
            <span className="text-[10px] text-p-text-light">
              {pkg.installed_version || pkg.version ? `v${pkg.installed_version || pkg.version}` : 'latest'}
            </span>
            {pkg.author && (
              <a
                href={pkg.author_url || undefined}
                target="_blank"
                rel="noreferrer"
                onClick={e => { if (!pkg.author_url) e.preventDefault() }}
                className="text-[10px] px-1.5 py-0.5 rounded-sm bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 border border-blue-200 dark:border-blue-800 hover:underline"
                title={`Curated from ${pkg.author}'s official skills repository${pkg.license ? ` — ${pkg.license}` : ''}`}
              >
                by {pkg.author}
              </a>
            )}
            {pkg.deprecated && (
              <span className="text-[10px] px-1 py-0.5 rounded-sm bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400">
                deprecated
              </span>
            )}
          </div>
          <p className="text-xs text-p-text-light line-clamp-2 mt-0.5">{pkg.description}</p>
        </div>
      </div>

      <div className="flex items-center flex-wrap gap-1">
        {(pkg.tags ?? []).slice(0, 3).map(tag => (
          <span key={tag} className="text-[10px] px-1.5 py-0.5 rounded-sm bg-gray-100 dark:bg-gray-800 text-p-text-light">
            {tag}
          </span>
        ))}
      </div>

      <div className="mt-auto pt-2 border-t border-p-border-light flex items-center justify-between gap-2">
        <StatusPill
          pkg={pkg}
          enabledHere={enabledHere}
          agentScoped={!!agentSlug}
          canEnableDirectly={canEnableDirectly}
          needsReinstall={needsReinstall}
        />
        <div className="flex items-center gap-1.5">
          {showAdminInstall && !installing && !pkg.installed && (
            <button
              onClick={handleInstall}
              className="text-xs px-2 py-1 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
            >
              Install
            </button>
          )}
          {showAdminInstall && !installing && pkg.update_available && (
            <button
              onClick={handleInstall}
              className="text-xs px-2 py-1 rounded-sm border border-amber-300 dark:border-amber-700 text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20 disabled:opacity-40 transition-colors"
            >
              Update
            </button>
          )}
          {!showAdminInstall && !enabledHere && !pkg.pending_request && authLoading && (
            <span className="text-[10px] text-p-text-light italic">Loading…</span>
          )}
          {!showAdminInstall && !enabledHere && !pkg.pending_request && !authLoading && canEnableDirectly && (
            <button
              onClick={handleEnableDirectly}
              disabled={enabling}
              className="text-xs px-2 py-1 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
              title="Package is installed and available — enables its skills directly, then fine-tune them on the Skills tab"
            >
              {enabling ? 'Enabling...' : 'Enable'}
            </button>
          )}
          {!showAdminInstall && needsReinstall && isAdmin && !installing && (
            <button
              onClick={handleInstall}
              className="text-xs px-2 py-1 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
              title="The package files are missing from the platform — reinstalls from the catalog; agent enablement is preserved"
            >
              Reinstall
            </button>
          )}
          {!showAdminInstall && needsReinstall && !isAdmin && !authLoading && !pkg.pending_request && (
            <button
              onClick={openRequestModal}
              disabled={createRequest.isPending}
              className="text-xs px-2 py-1 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
              title="The package files are missing from the platform — asks an admin to reinstall"
            >
              {createRequest.isPending ? 'Requesting...' : 'Request reinstall'}
            </button>
          )}
          {!showAdminInstall && !enabledHere && !pkg.pending_request && !authLoading && !canEnableDirectly && isAdmin && (
            <button
              onClick={submitAdminDirect}
              disabled={createRequest.isPending || agentSkills.isLoading}
              className="text-xs px-2 py-1 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
              title={pkg.installed
                ? 'Enables this skill package for the agent in one step'
                : 'Installs the skill package and enables it for this agent in one step'}
            >
              {createRequest.isPending ? (pkg.installed ? 'Enabling...' : 'Installing...') : (pkg.installed ? 'Enable' : 'Install')}
            </button>
          )}
          {!showAdminInstall && !enabledHere && !pkg.pending_request && !authLoading && !canEnableDirectly && !isAdmin && (
            <button
              onClick={openRequestModal}
              disabled={createRequest.isPending || agentSkills.isLoading}
              className="text-xs px-2 py-1 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
              title={pkg.installed
                ? 'Requires admin to authorize this agent'
                : 'Requires admin to install the skill package on the platform first'}
            >
              {createRequest.isPending ? 'Requesting...' : 'Request'}
            </button>
          )}
          {!showAdminInstall && pkg.pending_request && (
            <button
              onClick={handleCancel}
              disabled={cancelRequest.isPending}
              className="text-xs px-2 py-1 rounded-sm border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover disabled:opacity-40 transition-colors"
              title="Cancel pending request"
            >
              {cancelRequest.isPending ? 'Cancelling...' : 'Cancel request'}
            </button>
          )}
        </div>
      </div>

      {showAdminInstall && installing && <CatalogInstallBar job={job} />}

      {error && (
        <div className="rounded-sm border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 px-2 py-1.5">
          <p className="text-[11px] text-red-700 dark:text-red-400 whitespace-pre-wrap">{error}</p>
        </div>
      )}

      {showReasonModal && (
        <RequestReasonModal
          skillLabel={pkg.label}
          agentSlug={agentSlug || ''}
          reason={reasonInput}
          onChangeReason={setReasonInput}
          onSubmit={submitRequest}
          onClose={() => setShowReasonModal(false)}
          submitting={createRequest.isPending}
        />
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Catalog install progress bar (admin) — mirrors CommunityMcpsBrowser's.
// ---------------------------------------------------------------------------

function CatalogInstallBar({ job }: { job?: CatalogInstallJob }) {
  const pct = job?.pct ?? 0
  const message = job?.message || 'Starting…'
  const indeterminate = job?.phase === 'image'
  return (
    <div className="mt-2 flex flex-col gap-1 text-[11px] text-p-text-light">
      <div className="h-1 w-full bg-p-text-light/10 rounded-sm overflow-hidden">
        {indeterminate ? (
          <div className="h-full w-full bg-p-text-light/60 animate-pulse" />
        ) : (
          <div
            className="h-full bg-p-text-light/60 transition-[width] duration-300 ease-out"
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate min-w-0">{message}</span>
        {!indeterminate && <span className="shrink-0 tabular-nums opacity-60">{pct}%</span>}
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Reason modal (manager-side Request flow)
// ---------------------------------------------------------------------------

function RequestReasonModal({
  skillLabel, agentSlug, reason, onChangeReason, onSubmit, onClose, submitting,
}: {
  skillLabel: string
  agentSlug: string
  reason: string
  onChangeReason: (s: string) => void
  onSubmit: () => void
  onClose: () => void
  submitting: boolean
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-p-border-light w-full max-w-md mx-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-p-border-light">
          <h3 className="text-base font-semibold text-p-text">
            Request <span className="font-mono">{skillLabel}</span>
          </h3>
          <button onClick={onClose} className="text-p-text-light hover:text-p-text text-lg leading-none">&times;</button>
        </div>
        <div className="p-5 space-y-3">
          <p className="text-xs text-p-text-secondary">
            For agent <span className="font-mono">{agentSlug}</span>. The admin will see your reason on the request — helps them prioritise.
          </p>
          <label className="block">
            <span className="text-xs text-p-text-secondary">
              Reason <span className="text-p-text-light">(optional)</span>
            </span>
            <textarea
              value={reason}
              onChange={e => onChangeReason(e.target.value)}
              rows={3}
              maxLength={500}
              className="mt-1 w-full text-sm px-3 py-2 rounded-lg border border-p-border-light bg-white dark:bg-gray-900 text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/50"
              placeholder="e.g. need PDF form-filling for the tax workflow"
            />
          </label>
        </div>
        <div className="flex justify-end gap-2 px-5 py-3 border-t border-p-border-light bg-gray-50/50 dark:bg-gray-900/30">
          <button
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={onSubmit}
            disabled={submitting}
            className="px-3 py-1.5 text-sm rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors disabled:opacity-40"
          >
            {submitting ? 'Submitting...' : 'Submit request'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Status pill
// ---------------------------------------------------------------------------

function StatusPill({
  pkg, enabledHere, agentScoped, canEnableDirectly, needsReinstall,
}: {
  pkg: CommunitySkillEntry
  enabledHere: boolean
  agentScoped: boolean
  canEnableDirectly?: boolean
  needsReinstall?: boolean
}) {
  if (agentScoped) {
    if (needsReinstall) {
      return <Pill tone="amber">Enabled — package files missing, reinstall needed</Pill>
    }
    if (enabledHere) {
      return <Pill tone="green">Enabled for this agent</Pill>
    }
    if (pkg.pending_request) {
      return <Pill tone="amber">Request pending admin approval</Pill>
    }
    if (canEnableDirectly) {
      return <Pill tone="green">Available — click Enable</Pill>
    }
    if (pkg.installed) {
      return <Pill tone="amber">Installed — needs admin authorization</Pill>
    }
    return <Pill tone="gray">Not installed</Pill>
  }
  // Admin (global) context.
  if (pkg.update_available) {
    return (
      <Pill tone="amber">
        {pkg.version
          ? `Update available (${pkg.installed_version} → ${pkg.version})`
          : 'Update available'}
      </Pill>
    )
  }
  if (pkg.installed) {
    const n = pkg.enabled_for_agents.length
    const reqLabel = pkg.pending_request_count > 0 ? ` · ${pkg.pending_request_count} pending` : ''
    return <Pill tone="green">{n > 0 ? `Installed · enabled on ${n} agent${n === 1 ? '' : 's'}${reqLabel}` : `Installed${reqLabel}`}</Pill>
  }
  if (pkg.pending_request_count > 0) {
    return <Pill tone="amber">Not installed · {pkg.pending_request_count} pending request{pkg.pending_request_count === 1 ? '' : 's'}</Pill>
  }
  return <Pill tone="gray">Not installed</Pill>
}

function Pill({ tone, children }: { tone: 'green' | 'amber' | 'gray'; children: React.ReactNode }) {
  const classes = {
    green: 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400',
    amber: 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400',
    gray: 'bg-gray-100 dark:bg-gray-800 text-p-text-light',
  }[tone]
  return <span className={`text-[10px] px-1.5 py-0.5 rounded-sm ${classes}`}>{children}</span>
}

// ---------------------------------------------------------------------------
// Icon — first-letter avatar, same shape as the MCP browser's.
// ---------------------------------------------------------------------------

function Icon({ pkg }: { pkg: CommunitySkillEntry }) {
  const letter = (pkg.label || pkg.name).charAt(0).toUpperCase()
  return (
    <div className="w-8 h-8 rounded-md bg-brand/15 text-brand flex items-center justify-center shrink-0 font-semibold text-sm">
      {letter}
    </div>
  )
}
