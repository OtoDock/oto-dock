/**
 * Community MCP catalog hooks.
 *
 * The dashboard fetches `registry.json` (and per-MCP detail on demand) via the
 * proxy, which proxies to GitHub raw. This file also holds the install/update
 * and request-flow hooks that act on catalog entries.
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface CommunityMcpEntry {
  name: string
  label: string
  description: string
  category: 'community'
  version: string
  runtime: 'python' | 'node' | 'docker'
  source: string
  // Optional (absent on a stale cached registry.json): node/python auto-update
  // bound (PEP 440; "" = unbounded) + a hash of the integration manifest used to
  // detect catalog changes.
  version_constraint?: string
  manifest_hash?: string
  manifest_url: string
  readme_url: string
  icon_url: string | null
  tags: string[]
  author: string
  author_url: string | null
  license: string
  requires_credentials: boolean
  requires_system_packages: string[]
  platform_min_version: string | null
  assignment_mode: 'auto' | 'explicit'
  size_bytes: number
  deprecated: boolean
  patched: boolean
  patch_note: string | null
  // Augmentation added by the proxy (local platform state).
  installed: boolean
  installed_version: string | null
  update_available: boolean
  enabled_for_agents: string[]
  // Manager scope: open request id for this (mcp, agent). Admin scope:
  // always null (admins don't request).
  pending_request: number | null
  // Count of open requests across all agents (admin-side badge).
  pending_request_count: number
}

export interface CommunityMcpsResponse {
  registry_version: string
  updated_at: string
  platform_min_version: string | null
  fetched_from: string
  mcps: CommunityMcpEntry[]
}

export interface CommunityMcpDetail {
  entry: CommunityMcpEntry
  // The full manifest.json shape varies by MCP; treat as opaque JSON for now.
  manifest: Record<string, unknown> | null
  readme: string | null
}

/**
 * Fetch the augmented community catalog. Refetched on window focus +
 * every 60s while the drawer is open, so a newly published MCP shows up
 * without forcing a page reload.
 *
 * Pass ``agentSlug`` to scope ``pending_request`` to a specific agent —
 * the manager-side drawer uses this so a row's Request button can flip
 * to "Pending" without another round trip.
 */
export function useCommunityMcps(enabled: boolean = true, agentSlug?: string) {
  const url = agentSlug
    ? `/v1/community/mcps?agent=${encodeURIComponent(agentSlug)}`
    : '/v1/community/mcps'
  return useQuery<CommunityMcpsResponse>({
    queryKey: ['community-mcps', agentSlug ?? null],
    queryFn: () => apiFetch(url).then(r => r.json()),
    enabled,
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  })
}

// ---------------------------------------------------------------------------
// Install / Update — admin only
// ---------------------------------------------------------------------------

/**
 * A single admin catalog-install job (the unit the progress bar renders).
 * Keyed server-side by MCP name; surfaced via the installs poll below.
 */
export interface CatalogInstallJob {
  name: string
  label: string
  runtime: 'python' | 'node' | 'docker' | ''
  status: 'running' | 'done' | 'failed'
  // fetch | prepare | install | image | start | finalize | done | failed
  phase: string
  pct: number
  message: string
  error: string | null
  elapsed_s: number
}

export interface CatalogInstallsResponse {
  installs: CatalogInstallJob[]
}

/** Response of the POST install endpoint — it now returns 202 + the running job. */
export interface InstallStartResponse {
  job: CatalogInstallJob
  started: boolean
}

/**
 * Start a background install (or update) of a community MCP. Admin-only;
 * managers see a Request button instead.
 *
 * The POST returns immediately (202) with the running job; progress is read
 * from {@link useCatalogInstallJobs}. We kick the installs poll on start so the
 * bar appears at once. The catalog list refreshes when the poll sees the job go
 * terminal (see CommunityMcpsBrowser), not here — the install isn't done yet.
 */
export function useInstallCommunityMcp() {
  const qc = useQueryClient()
  return useMutation<InstallStartResponse, Error, string>({
    mutationFn: (name: string) =>
      apiFetch(`/v1/admin/community/mcps/${name}/install`, { method: 'POST' })
        .then(async r => {
          if (!r.ok) {
            const body = await r.text()
            throw new Error(body || `Install failed (HTTP ${r.status})`)
          }
          return r.json()
        }),
    onSuccess: (data) => {
      // Seed the returned running job into the installs cache so the progress
      // bar appears immediately, without waiting for the next poll tick.
      qc.setQueryData<CatalogInstallsResponse>(['catalog-installs'], old => {
        const others = (old?.installs ?? []).filter(j => j.name !== data.job.name)
        return { installs: [...others, data.job] }
      })
    },
  })
}

/**
 * Poll in-flight + recently-completed catalog installs while the Browse drawer
 * is open. Drives the per-card progress bar. 1.5s cadence; no polling when
 * disabled (drawer closed). A completed install lingers briefly server-side so
 * a poll reliably catches the terminal state.
 */
export function useCatalogInstallJobs(enabled: boolean) {
  return useQuery<CatalogInstallsResponse>({
    queryKey: ['catalog-installs'],
    queryFn: () => apiFetch('/v1/admin/community/mcps/installs').then(r => r.json()),
    enabled,
    refetchInterval: enabled ? 1500 : false,
    staleTime: 0,
  })
}

// ---------------------------------------------------------------------------
// Request flow — managers create + cancel; admins approve/reject
// ---------------------------------------------------------------------------

export type RequestStatus =
  | 'pending'
  | 'approved'
  | 'installing'
  | 'installed'
  | 'install_failed'
  | 'rejected'
  | 'cancelled'

export interface McpRequest {
  id: number
  mcp_name: string
  agent_slug: string
  requested_by: string
  requested_by_name: string | null
  requested_by_email: string | null
  /** Optional human justification. Empty string when the request was
   *  created without one (e.g. dashboard Request button left blank). */
  reason: string
  status: RequestStatus
  admin_note: string
  install_log: string
  /** When populated, this request was created as part of a community-agent
   *  install cascade. The admin Requests page groups rows with the same
   *  batch_id into one collapsible card. */
  batch_id: string | null
  created_at: string
  updated_at: string
  resolved_at: string | null
  resolved_by: string | null
  resolved_by_name: string | null
  resolved_by_email: string | null
}

/** Body the manager-side Request flow POSTs. ``reason`` is optional in the
 *  UI (low friction); the ``mcps-mcp`` MCP tool marks it required so LLMs
 *  always compose one. */
export interface CreateMcpRequestBody {
  mcp_name: string
  reason?: string
}

export interface AdminRequestsResponse {
  requests: McpRequest[]
  pending_count: number
}

/** Manager creates a new MCP request for one of their agents. */
export function useCreateMcpRequest(agentSlug: string) {
  const qc = useQueryClient()
  return useMutation<McpRequest, Error, CreateMcpRequestBody>({
    mutationFn: (body: CreateMcpRequestBody) =>
      apiFetch(`/v1/agents/${agentSlug}/mcp-requests`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: body.mcp_name,
          reason: (body.reason ?? '').trim(),
        }),
      }).then(async r => {
        if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`)
        return r.json()
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['community-mcps'] })
      qc.invalidateQueries({ queryKey: ['agent-mcp-requests', agentSlug] })
      qc.invalidateQueries({ queryKey: ['admin-mcp-requests'] })
    },
  })
}

/** Manager cancels their own pending request. */
export function useCancelMcpRequest(agentSlug: string) {
  const qc = useQueryClient()
  return useMutation<McpRequest, Error, number>({
    mutationFn: (requestId: number) =>
      apiFetch(`/v1/agents/${agentSlug}/mcp-requests/${requestId}/cancel`, {
        method: 'POST',
      }).then(async r => {
        if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`)
        return r.json()
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['community-mcps'] })
      qc.invalidateQueries({ queryKey: ['agent-mcp-requests', agentSlug] })
      qc.invalidateQueries({ queryKey: ['admin-mcp-requests'] })
    },
  })
}

/** Admin's full request log. ``openOnly`` filters out resolved entries. */
export function useAdminMcpRequests(openOnly: boolean = false) {
  return useQuery<AdminRequestsResponse>({
    queryKey: ['admin-mcp-requests', { openOnly }],
    queryFn: () =>
      apiFetch(`/v1/admin/mcp-requests${openOnly ? '?open_only=true' : ''}`).then(r => r.json()),
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })
}

/** Admin approves a request. Also serves as retry for install_failed. */
export function useApproveMcpRequest() {
  const qc = useQueryClient()
  return useMutation<McpRequest, Error, { id: number; admin_note?: string }>({
    mutationFn: ({ id, admin_note = '' }) =>
      apiFetch(`/v1/admin/mcp-requests/${id}/approve`, {
        method: 'POST',
        body: JSON.stringify({ admin_note }),
      }).then(async r => {
        if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`)
        return r.json()
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-mcp-requests'] })
      qc.invalidateQueries({ queryKey: ['community-mcps'] })
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
    },
  })
}

/** Admin rejects a request. */
export function useRejectMcpRequest() {
  const qc = useQueryClient()
  return useMutation<McpRequest, Error, { id: number; admin_note?: string }>({
    mutationFn: ({ id, admin_note = '' }) =>
      apiFetch(`/v1/admin/mcp-requests/${id}/reject`, {
        method: 'POST',
        body: JSON.stringify({ admin_note }),
      }).then(async r => {
        if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`)
        return r.json()
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-mcp-requests'] })
      qc.invalidateQueries({ queryKey: ['community-mcps'] })
    },
  })
}
