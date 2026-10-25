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
import { safeHref } from '../lib/safeUrl'
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
import { isAdmin as isPlatformAdmin } from '../lib/permissions'
import Dialog from './ui/Dialog'
import Button from './ui/Button'
import { Input, Select } from './ui/Field'
import { BADGE_TONE, DIALOG_PANEL, FIELD, FIELD_BORDER, FIELD_REGULAR } from './ui/classes'

// The small chips of a catalog card: a badge's tones at a dense size.
const CHIP = 'inline-flex items-center rounded-chip px-1.5 py-0.5 text-3xs font-medium'

interface Props {
  open: boolean
  onClose: () => void
  /** When set, status badges render per-agent. Otherwise admin/global. */
  agentSlug?: string
}

export default function CommunitySkillsBrowser({ open, onClose, agentSlug }: Props) {
  const { data, isLoading, isError } = useCommunitySkills(open, agentSlug)
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
    <Dialog
      onClose={onClose}
      placement="top"
      aria-label="Browse Community Skills"
      panelClassName={`${DIALOG_PANEL} max-w-6xl max-h-[calc(100vh-4rem)] flex flex-col overflow-hidden`}
    >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-subtle">
          <div>
            <h3 className="text-base font-semibold text-primary">Browse Community Skills</h3>
            <p className="text-xs text-tertiary mt-0.5">
              {agentSlug
                ? `Showing skill packages available for ${agentSlug}.`
                : 'Skill packages you can install, from the OtoDock community.'}
              {data?.fetched_from && !data?.catalog_unreachable && (
                <>
                  {' '}Community list updated {data.updated_at ? new Date(data.updated_at).toLocaleString() : ''}.
                </>
              )}
            </p>
          </div>
          <button onClick={onClose} className="text-tertiary hover:text-primary text-lg leading-none">&times;</button>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-2 px-5 py-3 border-b border-subtle">
          <Input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search by name, label, description, tag..."
            className="flex-1 min-w-[200px]"
          />
          <Select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            className="sm:w-auto"
          >
            <option value="all">All statuses</option>
            <option value="installed">Installed</option>
            <option value="not_installed">Not installed</option>
            {agentSlug && <option value="enabled_here">Enabled for this agent</option>}
            {agentSlug && <option value="pending_request">Request pending</option>}
          </Select>
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
          {data?.catalog_unreachable && (
            <div className="mb-3 rounded-card border border-status-warning-border bg-status-warning-bg p-3 text-sm text-status-warning-fg">
              Could not reach the community skills list. Check the network or try again later.
            </div>
          )}
          {data && !data.catalog_unreachable && filtered.length === 0 && (
            <div className="text-sm text-tertiary">No skill packages match the current filters.</div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {filtered.map(pkg => (
              <Card key={pkg.name} pkg={pkg} agentSlug={agentSlug} job={installJobs.get(pkg.name)} />
            ))}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-5 py-3 border-t border-subtle">
          <p className="text-2xs text-tertiary">
            {data ? `${filtered.length} of ${data.skills.length} skill packages` : ''}
          </p>
          <Button onClick={onClose}>
            Close
          </Button>
        </div>
    </Dialog>
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
  const isAdmin = isPlatformAdmin(user)
  const showAdminInstall = !agentSlug
  const [error, setError] = useState<string | null>(null)
  const clearError = () => {
    setError(null)
    setDismissedJob(job ?? null)
  }
  const [enabling, setEnabling] = useState(false)
  const [showReasonModal, setShowReasonModal] = useState(false)
  const [reasonInput, setReasonInput] = useState('')

  const pkgSkills = agentSlug
    ? (agentSkills.data ?? []).filter(s => s.mcp_name === pkg.name)
    : []
  // Manager can self-serve when the package is installed and visible to the
  // agent (its skills list in the tab) and not yet enabled. Otherwise the
  // only way in is via the request → admin-approve flow.
  const canEnableDirectly = pkg.installed && pkgSkills.length > 0 && !enabledHere

  const handleInstall = () => {
    clearError()
    // 202 semantics — the initial POST failing surfaces here; a background
    // failure surfaces via the polled job below.
    install.mutate(pkg.name, {
      onError: e => setError((e as Error)?.message || 'Install failed'),
    })
  }

  // A background-install failure (from the poll) shows in the same inline
  // banner the synchronous path uses, until the next install attempt
  // dismisses that job.
  const [dismissedJob, setDismissedJob] = useState<CatalogInstallJob | null>(null)
  const jobError = job?.status === 'failed' && job.error && job !== dismissedJob ? job.error : null
  const shownError = error ?? jobError

  // Direct enable (manager with visible installed package): PATCH-enable each
  // of the package's skills — the first PATCH auto-assigns the package
  // server-side. exclude_from is preserved as-is.
  const handleEnableDirectly = async () => {
    if (!agentSlug || pkgSkills.length === 0) return
    clearError()
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
    clearError()
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
    clearError()
    createRequest.mutate(
      { mcp_name: pkg.name, reason: '', kind: 'skill' },
      {
        onError: e => setError((e as Error)?.message || 'Install failed'),
      },
    )
  }

  const handleCancel = () => {
    if (!pkg.pending_request) return
    clearError()
    cancelRequest.mutate(pkg.pending_request, {
      onError: e => setError((e as Error)?.message || 'Cancel failed'),
    })
  }

  return (
    <div className="rounded-card border border-subtle p-3 flex flex-col gap-2">
      <div className="flex items-start gap-2">
        <Icon pkg={pkg} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <h4 className="text-sm font-medium text-primary truncate">{pkg.label}</h4>
            <span className="text-3xs text-tertiary">
              {pkg.installed_version || pkg.version ? `v${pkg.installed_version || pkg.version}` : 'latest'}
            </span>
            {pkg.author && (
              <a
                href={safeHref(pkg.author_url)}
                target="_blank"
                rel="noreferrer"
                onClick={e => { if (!safeHref(pkg.author_url)) e.preventDefault() }}
                className={`${CHIP} ${BADGE_TONE.neutral} hover:underline`}
                title={`Curated from ${pkg.author}'s official skills repository.${pkg.license ? ` License: ${pkg.license}.` : ''}`}
              >
                by {pkg.author}
              </a>
            )}
            {pkg.has_scripts && (
              <span
                className={`${CHIP} ${BADGE_TONE.warning}`}
                title="This package bundles executable helper scripts the agent can run"
              >
                Bundles scripts
              </span>
            )}
            {pkg.deprecated && (
              <span className={`${CHIP} ${BADGE_TONE.danger}`}>
                deprecated
              </span>
            )}
          </div>
          <p className="text-xs text-tertiary line-clamp-2 mt-0.5">{pkg.description}</p>
        </div>
      </div>

      <div className="flex items-center flex-wrap gap-1">
        {(pkg.tags ?? []).slice(0, 3).map(tag => (
          <span key={tag} className={`${CHIP} ${BADGE_TONE.neutral}`}>
            {tag}
          </span>
        ))}
      </div>

      <div className="mt-auto pt-2 border-t border-subtle flex items-center justify-between gap-2">
        <StatusPill
          pkg={pkg}
          enabledHere={enabledHere}
          agentScoped={!!agentSlug}
          canEnableDirectly={canEnableDirectly}
          needsReinstall={needsReinstall}
        />
        <div className="flex items-center gap-1.5">
          {showAdminInstall && !installing && !pkg.installed && (
            <Button variant="primary" size="xs" onClick={handleInstall}>
              Install
            </Button>
          )}
          {showAdminInstall && !installing && pkg.update_available && (
            <Button variant="secondary" size="xs" onClick={handleInstall}>
              Update
            </Button>
          )}
          {!showAdminInstall && !enabledHere && !pkg.pending_request && authLoading && (
            <span className="text-3xs text-tertiary italic">Loading…</span>
          )}
          {!showAdminInstall && !enabledHere && !pkg.pending_request && !authLoading && canEnableDirectly && (
            <Button
              variant="primary"
              size="xs"
              onClick={handleEnableDirectly}
              disabled={enabling}
              title="This package is installed and available. Enabling turns on its skills. Fine-tune them on the Skills page."
            >
              {enabling ? 'Enabling...' : 'Enable'}
            </Button>
          )}
          {!showAdminInstall && needsReinstall && isAdmin && !installing && (
            <Button
              variant="primary"
              size="xs"
              onClick={handleInstall}
              title="The package files are missing. This reinstalls them from the community list. Agents that use it stay enabled."
            >
              Reinstall
            </Button>
          )}
          {!showAdminInstall && needsReinstall && !isAdmin && !authLoading && !pkg.pending_request && (
            <Button
              variant="primary"
              size="xs"
              onClick={openRequestModal}
              disabled={createRequest.isPending}
              title="The package files are missing. This asks an admin to reinstall them."
            >
              {createRequest.isPending ? 'Requesting...' : 'Request reinstall'}
            </Button>
          )}
          {!showAdminInstall && !enabledHere && !pkg.pending_request && !authLoading && !canEnableDirectly && isAdmin && (
            <Button
              variant="primary"
              size="xs"
              onClick={submitAdminDirect}
              disabled={createRequest.isPending || agentSkills.isLoading}
              title={pkg.installed
                ? 'Enables this skill package for the agent in one step'
                : 'Installs the skill package and enables it for this agent in one step'}
            >
              {createRequest.isPending ? (pkg.installed ? 'Enabling...' : 'Installing...') : (pkg.installed ? 'Enable' : 'Install')}
            </Button>
          )}
          {!showAdminInstall && !enabledHere && !pkg.pending_request && !authLoading && !canEnableDirectly && !isAdmin && (
            <Button
              variant="primary"
              size="xs"
              onClick={openRequestModal}
              disabled={createRequest.isPending || agentSkills.isLoading}
              title={pkg.installed
                ? 'Requires admin to authorize this agent'
                : 'Requires admin to install the skill package on the platform first'}
            >
              {createRequest.isPending ? 'Requesting...' : 'Request'}
            </Button>
          )}
          {!showAdminInstall && pkg.pending_request && (
            <Button
              size="xs"
              onClick={handleCancel}
              disabled={cancelRequest.isPending}
              title="Cancel pending request"
            >
              {cancelRequest.isPending ? 'Cancelling...' : 'Cancel request'}
            </Button>
          )}
        </div>
      </div>

      {showAdminInstall && installing && <CatalogInstallBar job={job} />}

      {shownError && (
        <div className="rounded-card border border-status-danger-border bg-status-danger-bg px-2 py-1.5">
          <p className="text-2xs text-status-danger-fg whitespace-pre-wrap">{shownError}</p>
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
    <div className="mt-2 flex flex-col gap-1 text-2xs text-tertiary">
      <div className="h-1 w-full bg-surface-muted rounded-full overflow-hidden">
        {indeterminate ? (
          <div className="h-full w-full bg-status-info-solid animate-pulse" />
        ) : (
          <div
            className="h-full bg-status-info-solid transition-[width] duration-300 ease-out"
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
    <Dialog
      onClose={onClose}
      busy={submitting}
      aria-label="Request"
      panelClassName={`${DIALOG_PANEL} max-w-md`}
    >
        <div className="flex items-center justify-between px-5 py-4 border-b border-subtle">
          <h3 className="text-base font-semibold text-primary">
            Request <span className="font-mono">{skillLabel}</span>
          </h3>
          <button onClick={onClose} className="text-tertiary hover:text-primary text-lg leading-none">&times;</button>
        </div>
        <div className="p-5 space-y-3">
          <p className="text-xs text-secondary">
            For agent <span className="font-mono">{agentSlug}</span>. The admin sees your reason on the request. It helps them prioritise.
          </p>
          <label className="block">
            <span className="text-xs text-secondary">
              Reason<span className="text-tertiary">, optional</span>
            </span>
            <textarea
              value={reason}
              onChange={e => onChangeReason(e.target.value)}
              rows={3}
              maxLength={500}
              className={`${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER} mt-1`}
              placeholder="Need PDF form-filling for the tax workflow"
            />
          </label>
        </div>
        <div className="flex justify-end gap-2 px-5 py-3 border-t border-subtle">
          <Button onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="primary" onClick={onSubmit} disabled={submitting}>
            {submitting ? 'Submitting...' : 'Submit request'}
          </Button>
        </div>
    </Dialog>
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
      return <Pill tone="warning">Enabled: package files missing, reinstall needed</Pill>
    }
    if (enabledHere) {
      return <Pill tone="success">Enabled for this agent</Pill>
    }
    if (pkg.pending_request) {
      return <Pill tone="warning">Request pending admin approval</Pill>
    }
    if (canEnableDirectly) {
      return <Pill tone="success">Available: click Enable</Pill>
    }
    if (pkg.installed) {
      return <Pill tone="warning">Installed: needs admin authorization</Pill>
    }
    return <Pill tone="neutral">Not installed</Pill>
  }
  // Admin (global) context.
  if (pkg.update_available) {
    return (
      <Pill tone="warning">
        {pkg.version
          ? `Update available: ${pkg.installed_version} → ${pkg.version}`
          : 'Update available'}
      </Pill>
    )
  }
  if (pkg.installed) {
    const n = pkg.enabled_for_agents.length
    const reqLabel = pkg.pending_request_count > 0 ? ` · ${pkg.pending_request_count} pending` : ''
    return <Pill tone="success">{n > 0 ? `Installed · enabled on ${n} agent${n === 1 ? '' : 's'}${reqLabel}` : `Installed${reqLabel}`}</Pill>
  }
  if (pkg.pending_request_count > 0) {
    return <Pill tone="warning">Not installed · {pkg.pending_request_count} pending request{pkg.pending_request_count === 1 ? '' : 's'}</Pill>
  }
  return <Pill tone="neutral">Not installed</Pill>
}

function Pill({ tone, children }: { tone: 'success' | 'warning' | 'neutral'; children: React.ReactNode }) {
  return <span className={`${CHIP} ${BADGE_TONE[tone]}`}>{children}</span>
}

// ---------------------------------------------------------------------------
// Icon — first-letter avatar, same shape as the MCP browser's.
// ---------------------------------------------------------------------------

function Icon({ pkg }: { pkg: CommunitySkillEntry }) {
  const letter = (pkg.label || pkg.name).charAt(0).toUpperCase()
  return (
    <div className="w-8 h-8 rounded-control bg-surface-muted text-secondary flex items-center justify-center shrink-0 font-semibold text-sm">
      {letter}
    </div>
  )
}
