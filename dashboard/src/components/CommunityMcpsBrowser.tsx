/**
 * Browse Community MCPs drawer.
 *
 * Reusable across the admin MCP Servers page and the agent MCP tab. Pass an
 * `agentSlug` to scope status badges to that agent — without one, the
 * component renders in admin/global mode (counts of agents the MCP is
 * enabled on).
 *
 * Cards render the catalog + state badges along with inline Install / Request
 * actions.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  useCommunityMcps,
  useInstallCommunityMcp,
  MCP_TRUST_TIERS,
  useCatalogInstallJobs,
  useCreateMcpRequest,
  useCancelMcpRequest,
  CommunityMcpEntry,
  CatalogInstallJob,
} from '../api/community'
import { useAgentMcps, useSetAgentMcps } from '../api/mcps'
import { useAuth } from '../contexts/AuthContext'
import { isAdmin as isPlatformAdmin } from '../lib/permissions'
import { safeHref } from '../lib/safeUrl'
import McpIcon from './McpIcon'
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

export default function CommunityMcpsBrowser({ open, onClose, agentSlug }: Props) {
  const { data, isLoading, isError } = useCommunityMcps(open, agentSlug)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')

  // Admin-only: poll in-flight catalog installs while the drawer is open and
  // index them by MCP name so each card can render its own progress bar. The
  // poll is server-side state keyed by name, so reopening the drawer mid-install
  // shows the live bar with no client state to rehydrate.
  const qc = useQueryClient()
  const installJobsQuery = useCatalogInstallJobs(open && !agentSlug)
  const installJobs = useMemo(() => {
    const m = new Map<string, CatalogInstallJob>()
    for (const j of installJobsQuery.data?.installs ?? []) m.set(j.name, j)
    return m
  }, [installJobsQuery.data])

  // When a job goes terminal (running → done/failed), refresh the catalog +
  // admin MCP lists so the card flips Install → Installed (or shows the failure).
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
      qc.invalidateQueries({ queryKey: ['community-mcps'] })
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
    }
  }, [installJobsQuery.data, qc])

  const filtered = useMemo(() => {
    if (!data) return []
    const q = search.trim().toLowerCase()
    return data.mcps.filter(mcp => {
      if (statusFilter !== 'all') {
        const enabledHere = !!agentSlug && mcp.enabled_for_agents.includes(agentSlug)
        if (statusFilter === 'installed' && !mcp.installed) return false
        if (statusFilter === 'not_installed' && mcp.installed) return false
        if (statusFilter === 'enabled_here' && !enabledHere) return false
        if (statusFilter === 'pending_request' && !mcp.pending_request) return false
      }
      if (!q) return true
      const haystack = [
        mcp.name, mcp.label, mcp.description, mcp.author,
        ...mcp.tags,
      ].join(' ').toLowerCase()
      return haystack.includes(q)
    })
  }, [data, search, statusFilter, agentSlug])

  if (!open) return null

  return (
    <Dialog
      onClose={onClose}
      placement="top"
      aria-label="Browse Community MCPs"
      panelClassName={`${DIALOG_PANEL} max-w-6xl max-h-[calc(100vh-4rem)] flex flex-col overflow-hidden`}
    >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-subtle">
          <div>
            <h3 className="text-base font-semibold text-primary">Browse Community MCPs</h3>
            <p className="text-xs text-tertiary mt-0.5">
              {agentSlug
                ? `Showing MCPs available for ${agentSlug}.`
                : 'MCPs you can install, from the OtoDock community.'}
              {data?.fetched_from && (
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
          {data && filtered.length === 0 && (
            <div className="text-sm text-tertiary">No MCPs match the current filters.</div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {filtered.map(mcp => (
              <Card key={mcp.name} mcp={mcp} agentSlug={agentSlug} job={installJobs.get(mcp.name)} />
            ))}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-5 py-3 border-t border-subtle">
          <p className="text-2xs text-tertiary">
            {data ? `${filtered.length} of ${data.mcps.length} MCPs` : ''}
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

function Card({ mcp, agentSlug, job }: { mcp: CommunityMcpEntry; agentSlug?: string; job?: CatalogInstallJob }) {
  const enabledHere = !!agentSlug && mcp.enabled_for_agents.includes(agentSlug)
  const install = useInstallCommunityMcp()
  // The card is "installing" while the POST is in flight OR the polled job is
  // still running. Driven by the poll, this survives a drawer close/reopen.
  const installing = install.isPending || job?.status === 'running'
  // Hooks must be called unconditionally even when agentSlug is undefined.
  // The empty-string fallback yields hook instances the manager paths
  // never actually call (showAdminInstall short-circuits them).
  const createRequest = useCreateMcpRequest(agentSlug || '')
  const cancelRequest = useCancelMcpRequest(agentSlug || '')
  // Visibility check — when the MCP is in this agent's visible MCP list
  // (auto-mode, or explicit-mode with an instance authorizing this agent),
  // the manager can self-serve the enable without an admin request. We
  // use the existing /v1/agents/{slug}/mcps endpoint for this so we share
  // a cache with the agent's MCPs tab.
  const agentMcps = useAgentMcps(agentSlug || '')
  const setAgentMcps = useSetAgentMcps()
  const { user, loading: authLoading } = useAuth()
  // Admin requesters skip the queue entirely — the backend auto-approves
  // their POST to /v1/agents/{slug}/mcp-requests, returning the row
  // already in ``installed`` (or ``install_failed`` with admin_note
  // when explicit-mode + no instance). The UI mirrors that: action
  // button labels become "Install" / "Enable" (depending on state),
  // reason modal is skipped (no second pair of eyes will read the
  // justification — the admin is already the resolver). Manager UX is
  // unchanged.
  //
  // ``authLoading`` is the AuthContext's initial /auth/me round-trip.
  // Gating the action buttons on it prevents the manager "Request"
  // button from briefly rendering for admin sessions during the window
  // between mount and the auth response. Without this guard, a fast
  // click during that window opens the (admin-unwanted) reason modal
  // — the backend still resolves correctly because role is checked
  // server-side, but the UX feels wrong.
  const isAdmin = isPlatformAdmin(user)
  const showAdminInstall = !agentSlug
  const [error, setError] = useState<string | null>(null)
  const clearError = () => {
    setError(null)
    setDismissedJob(job ?? null)
  }
  // Reason modal is a 2-step: click Request → modal opens → optional
  // reason typed → Submit fires the mutation. Optional in the UI, but
  // the admin sees it on the Requests page when present. Only shown
  // when the MCP isn't directly enable-able by the manager AND the
  // caller isn't admin (admin requests auto-approve server-side so the
  // modal is just extra friction).
  const [showReasonModal, setShowReasonModal] = useState(false)
  const [reasonInput, setReasonInput] = useState('')

  const visibleEntry = agentSlug
    ? agentMcps.data?.mcps.find(m => m.name === mcp.name)
    : undefined
  // Manager can self-serve when the MCP is in the agent's visible list
  // AND not yet enabled. Otherwise the only way to add it is via the
  // request → admin-approve flow.
  const canEnableDirectly = !!visibleEntry && !visibleEntry.enabled

  const handleInstall = () => {
    clearError()
    // The mutation only reports an *initial-POST* failure (e.g. 403/502). The
    // install runs in the background; its failure surfaces via the polled job
    // below. 202 leaves the bar to take over.
    install.mutate(mcp.name, {
      onError: e => setError((e as Error)?.message || 'Install failed'),
    })
  }

  // A background-install failure (from the poll) shows in the same inline
  // banner the synchronous path uses, until the next install attempt
  // dismisses that job.
  const [dismissedJob, setDismissedJob] = useState<CatalogInstallJob | null>(null)
  const jobError = job?.status === 'failed' && job.error && job !== dismissedJob ? job.error : null
  const shownError = error ?? jobError

  const handleEnableDirectly = () => {
    if (!agentSlug || !agentMcps.data) return
    clearError()
    const next = agentMcps.data.mcps
      .filter(m => m.enabled || m.name === mcp.name)
      .map(m => m.name)
    if (!next.includes(mcp.name)) next.push(mcp.name)
    setAgentMcps.mutate(
      { agent: agentSlug, mcps: next },
      {
        onError: e => setError((e as Error)?.message || 'Enable failed'),
      },
    )
  }

  const openRequestModal = () => {
    clearError()
    setReasonInput('')
    setShowReasonModal(true)
  }

  const submitRequest = () => {
    createRequest.mutate(
      { mcp_name: mcp.name, reason: reasonInput.trim() },
      {
        onSuccess: () => setShowReasonModal(false),
        onError: e => setError((e as Error)?.message || 'Request failed'),
      },
    )
  }

  // Admin one-click path: POST the same endpoint but with no reason
  // (server-side auto-approve runs the full install + attach + enable
  // cascade synchronously and returns the resolved row). No modal —
  // admin shouldn't have to justify a request to themselves.
  const submitAdminDirect = () => {
    clearError()
    createRequest.mutate(
      { mcp_name: mcp.name, reason: '' },
      {
        onError: e => setError((e as Error)?.message || 'Install failed'),
      },
    )
  }

  const handleCancel = () => {
    if (!mcp.pending_request) return
    clearError()
    cancelRequest.mutate(mcp.pending_request, {
      onError: e => setError((e as Error)?.message || 'Cancel failed'),
    })
  }

  return (
    <div className="rounded-card border border-subtle p-3 flex flex-col gap-2">
      <div className="flex items-start gap-2">
        <McpIcon name={mcp.name} label={mcp.label} category="community" author={mcp.author} hasIcon={!!mcp.icon_url} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <h4 className="text-sm font-medium text-primary truncate">{mcp.label}</h4>
            {/* node/python catalog entries are unpinned (version ""); show the
                installed version when present, the catalog version otherwise,
                and fall back to "latest" so we never render a bare "v". */}
            <span className="text-3xs text-tertiary">
              {mcp.installed_version || mcp.version ? `v${mcp.installed_version || mcp.version}` : 'latest'}
            </span>
            {/* Where the server code comes from — the catalog entry's upstream project. */}
            {mcp.author && (
              <a
                href={safeHref(mcp.author_url)}
                target="_blank"
                rel="noreferrer"
                onClick={e => { if (!safeHref(mcp.author_url)) e.preventDefault() }}
                className={`${CHIP} ${BADGE_TONE.neutral} hover:underline`}
                title={`Server code by ${mcp.author}`}
              >
                by {mcp.author}
              </a>
            )}
            {mcp.trust && (
              <span
                className={`${CHIP} ${BADGE_TONE[mcp.trust === 'third-party' ? 'neutral' : 'success']}`}
                title={MCP_TRUST_TIERS[mcp.trust]?.title}
              >
                {MCP_TRUST_TIERS[mcp.trust]?.label ?? mcp.trust}
              </span>
            )}
            {mcp.patched && (
              <span
                className={`${CHIP} ${BADGE_TONE.warning}`}
                title={mcp.patch_note || 'Includes OtoDock patches'}
              >
                patched
              </span>
            )}
            {mcp.deprecated && (
              <span className={`${CHIP} ${BADGE_TONE.danger}`}>
                deprecated
              </span>
            )}
          </div>
          <p className="text-xs text-tertiary line-clamp-2 mt-0.5">{mcp.description}</p>
        </div>
      </div>

      <div className="flex items-center flex-wrap gap-1">
        {mcp.requires_credentials && (
          <span className={`${CHIP} ${BADGE_TONE.info}`}>
            Per user credentials
          </span>
        )}
        {mcp.assignment_mode === 'explicit' && (
          <span
            className={`${CHIP} ${BADGE_TONE.neutral}`}
            title="An admin must add an instance with a URL and a token before agents can use it"
          >
            Admin assignment
          </span>
        )}
        {mcp.tags.slice(0, 3).map(tag => (
          <span key={tag} className={`${CHIP} ${BADGE_TONE.neutral}`}>
            {tag}
          </span>
        ))}
      </div>

      <div className="mt-auto pt-2 border-t border-subtle flex items-center justify-between gap-2">
        <StatusPill
          mcp={mcp}
          enabledHere={enabledHere}
          agentScoped={!!agentSlug}
          canEnableDirectly={canEnableDirectly}
        />
        <div className="flex items-center gap-1.5">
          {showAdminInstall && !installing && !mcp.installed && (
            <Button variant="primary" size="xs" onClick={handleInstall}>
              Install
            </Button>
          )}
          {showAdminInstall && !installing && mcp.source_changed && (
            <span
              className={`${CHIP} ${BADGE_TONE.warning}`}
              title="Its community list entry moved to another source. Run Check Updates on the MCP Servers page and switch it there."
            >
              Source changed
            </span>
          )}
          {showAdminInstall && !installing && mcp.catalog_unpinned && (
            <span
              className={`${CHIP} ${BADGE_TONE.warning}`}
              title="The community list pins this MCP to a tag or a branch, not a commit. It can be updated once the list pins a commit."
            >
              No pinned commit
            </span>
          )}
          {showAdminInstall && !installing && mcp.update_available && (
            <Button variant="secondary" size="xs" onClick={handleInstall}>
              Update
            </Button>
          )}
          {!showAdminInstall && !enabledHere && !mcp.pending_request && authLoading && (
            <span className="text-3xs text-tertiary italic">Loading…</span>
          )}
          {!showAdminInstall && !enabledHere && !mcp.pending_request && !authLoading && canEnableDirectly && (
            <Button
              variant="primary"
              size="xs"
              onClick={handleEnableDirectly}
              disabled={setAgentMcps.isPending}
              title="This MCP is installed and available. Enabling it needs no admin approval."
            >
              {setAgentMcps.isPending ? 'Enabling...' : 'Enable'}
            </Button>
          )}
          {!showAdminInstall && !enabledHere && !mcp.pending_request && !authLoading && !canEnableDirectly && isAdmin && (
            // Admin → one-click; backend auto-approves (install + instance
            // attach + enable) without a request row sitting pending.
            <Button
              variant="primary"
              size="xs"
              onClick={submitAdminDirect}
              disabled={createRequest.isPending || agentMcps.isLoading}
              title={mcp.installed
                ? 'Attaches this agent to an existing instance. It fails if there is no instance yet.'
                : 'Installs the MCP and enables it for this agent in one step'}
            >
              {createRequest.isPending ? (mcp.installed ? 'Enabling...' : 'Installing...') : (mcp.installed ? 'Enable' : 'Install')}
            </Button>
          )}
          {!showAdminInstall && !enabledHere && !mcp.pending_request && !authLoading && !canEnableDirectly && !isAdmin && (
            <Button
              variant="primary"
              size="xs"
              onClick={openRequestModal}
              disabled={createRequest.isPending || agentMcps.isLoading}
              title={mcp.installed
                ? 'Requires admin to authorize this agent via an instance'
                : 'Requires admin to install the MCP on the platform first'}
            >
              {createRequest.isPending ? 'Requesting...' : 'Request'}
            </Button>
          )}
          {!showAdminInstall && mcp.pending_request && (
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
          mcpLabel={mcp.label}
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
// Catalog install progress bar (admin)
// ---------------------------------------------------------------------------

/**
 * Thin per-card progress bar for an in-flight catalog install. Style mirrors
 * the satellite `chat/InstallProgressBar` (h-1 track, p-text-light accent) but
 * reads the polled `CatalogInstallJob` rather than the satellite Zustand store.
 * The docker image-pull phase has no fine %, so it renders an indeterminate
 * pulse; every other phase shows a real percentage.
 */
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
  mcpLabel, agentSlug, reason, onChangeReason, onSubmit, onClose, submitting,
}: {
  mcpLabel: string
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
            Request <span className="font-mono">{mcpLabel}</span>
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
              placeholder="Need to search nearby restaurants for the user"
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
  mcp, enabledHere, agentScoped, canEnableDirectly,
}: {
  mcp: CommunityMcpEntry
  enabledHere: boolean
  agentScoped: boolean
  canEnableDirectly?: boolean
}) {
  if (agentScoped) {
    if (enabledHere) {
      return <Pill tone="success">Enabled for this agent</Pill>
    }
    if (mcp.pending_request) {
      return <Pill tone="warning">Request pending admin approval</Pill>
    }
    if (canEnableDirectly) {
      // Installed + visible to this agent (auto-mode, or explicit-mode with
      // instance authorizing this agent) — manager can self-serve. No
      // admin request needed for this case.
      return <Pill tone="success">Available: click Enable</Pill>
    }
    if (mcp.installed) {
      // Installed but NOT visible to this agent → explicit-mode that needs
      // admin to attach an instance, OR community-only without the manager's
      // auto-mode authorization in place. Falls through to the Request flow.
      return <Pill tone="warning">Installed: needs admin authorization</Pill>
    }
    return <Pill tone="neutral">Not installed</Pill>
  }
  // Admin (global) context.
  if (mcp.update_available) {
    // node/python catalog `version` is "" (unbounded) → an update here is an
    // integration-manifest change; only docker shows a version transition.
    return (
      <Pill tone="warning">
        {mcp.version
          ? `Update available: ${mcp.installed_version} → ${mcp.version}`
          : 'Update available: integration change'}
      </Pill>
    )
  }
  if (mcp.installed) {
    const n = mcp.enabled_for_agents.length
    const reqLabel = mcp.pending_request_count > 0 ? ` · ${mcp.pending_request_count} pending` : ''
    return <Pill tone="success">{n > 0 ? `Installed · enabled on ${n} agent${n === 1 ? '' : 's'}${reqLabel}` : `Installed${reqLabel}`}</Pill>
  }
  if (mcp.pending_request_count > 0) {
    return <Pill tone="warning">Not installed · {mcp.pending_request_count} pending request{mcp.pending_request_count === 1 ? '' : 's'}</Pill>
  }
  return <Pill tone="neutral">Not installed</Pill>
}

function Pill({ tone, children }: { tone: 'success' | 'warning' | 'neutral'; children: React.ReactNode }) {
  return <span className={`${CHIP} ${BADGE_TONE[tone]}`}>{children}</span>
}

