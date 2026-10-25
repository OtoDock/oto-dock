import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiJson, apiOk, withMessage } from '../lib/http/apiJson'
import { isRecord } from '../lib/json/guards'
import { placePhrase } from './remoteMachines'
import type { McpRuntime } from '../lib/kinds/mcpRuntime'
import { TARGET_LOCAL } from '../lib/placement'
import type { DockerStatus, EnableDockerResult } from '../lib/status/docker'

export interface ConfigField {
  key: string
  label: string
  input_type: string
  default: string
}

export interface McpSkill {
  id: string
  description: string
}

export interface McpInstanceField {
  key: string
  label: string
  input_type: string
  default: string
  required: boolean
  secret: boolean
}

export interface McpInstancesConfig {
  delivery: 'env' | 'config_file'
  fields: McpInstanceField[]
  max_instances: number
}

export interface McpInstance {
  id: number
  mcp_name: string
  instance_name: string
  field_values: Record<string, string>
  agents: string[]
  // When true, every current and future agent is authorized to use this
  // instance. The `agents` list is still persisted (so toggling off restores
  // the prior selection) but is ignored at runtime per the precedence rules.
  assigned_to_all: boolean
  // Hosted relay. `hosted_mode='hosted'` routes the MCP through the
  // OtoDock relay (no field_values injected). `managed_by='system'` marks the
  // platform-auto-created instance — admin may rename/scope/delete it but not
  // flip it to self_managed.
  hosted_mode?: 'self_managed' | 'hosted'
  managed_by?: 'admin' | 'system'
  configured_keys?: string[]
}

export interface McpInstancesResponse {
  instances: McpInstance[]
  fields: McpInstanceField[]
  delivery: 'env' | 'config_file'
  max_instances: number
}

export interface McpAppKind {
  kind: string
  label: string
  mcp_token?: string
  fields: { key: string; label: string; input_type: string; help?: string }[]
}

export interface McpServer {
  name: string
  label: string
  description: string
  version: string
  // 'skill' = standalone skill package (runtime none) — listed on the admin
  // Skills page, filtered out of the MCP Servers page.
  category: 'core' | 'custom' | 'community' | 'skill'
  // Who wrote the server code the MCP wraps and where it lives (community
  // manifests); empty on bundled MCPs. `icon`: the install dir ships icon.png.
  author?: string
  author_url?: string
  icon?: boolean
  // lib/kinds/mcpRuntime.ts: a skill package's manifest says `none`.
  runtime: McpRuntime
  transport: 'stdio' | 'sse'
  source: string
  enabled: boolean
  // Core MCPs are locked on, EXCEPT the platform-disableable ones
  // (meetings/delegation kill-switches) — the server decides.
  can_disable: boolean
  patched: boolean
  patch_note: string
  credential_type: 'per_user' | 'infra' | 'none'
  credential_label: string | null
  skills: McpSkill[]
  config_fields: ConfigField[]
  config_values: Record<string, string>
  assignment_mode: 'auto' | 'explicit'
  instances?: McpInstancesConfig
  // Skill packages only: the package bundles executable scripts/ content
  // (allowed since 2026-08-27; always surfaced as a provenance badge).
  has_scripts?: boolean
  docker_status?: DockerStatus
  // The running container serves another image than the manifest names (an
  // update whose recreate did not happen): a Restart switches it.
  docker_image_stale?: boolean
  // false = operator-owned compose sibling (core file-tools on containerized
  // installs) — no status pill, no start/stop/restart controls.
  docker_managed?: boolean
  agents: string[]
  credential_fields?: { key: string; label: string; input_type: string }[]
  server_config_fields?: { key: string; label: string; input_type: string }[]
  credential_configured?: boolean
  credential_configured_keys?: string[]
  // App credential (OAuth app credentials managed by admin)
  app_credential?: string
  app_credential_fields?: { key: string; label: string; input_type: string; help?: string }[]
  app_credential_configured?: boolean
  app_credential_configured_keys?: string[]
  /** The kinds of vendor application an admin may register (lib/kinds/oauthApp.ts)
   * and the one the bundle is in. */
  app_kinds?: { key: string; kinds: McpAppKind[] }
  app_credential_kind?: string
  // OAuth provider id (e.g. "google", "linear", "notion") — drives the
  // callback URL `/v1/oauth/{provider_id}/callback` and per-provider
  // help text in the app-credentials form.
  provider_id?: string
  // Hosted relay. `oauth_app` drives the per-MCP OAuth toggle;
  // `api_key_relay` is per-instance (surfaced via the instance manager).
  hosted?: {
    oauth_app?: { available: boolean; default_mode: 'self_managed' | 'hosted' }
    api_key_relay?: {
      available: boolean
      default_mode: 'self_managed' | 'hosted'
      relay_path: string
      min_balance_to_enable_usd: number
      billing_setup_url: string
    }
  }
  hosted_oauth_mode?: 'self_managed' | 'hosted'
  // Internal-network access (homelab MCPs declaring network_targets). When on,
  // the agent sandbox carves egress to the MCP's configured target host.
  has_network_targets?: boolean
  network_access?: boolean
  // Generic tool filter. Dashboard renders the
  // regex field when `tool_filter_supported` is true; greys out with a
  // tooltip otherwise. `tool_filter_arg_name` lets the UI preview which
  // CLI flag will be appended (e.g. "--enabled-tools '<your-regex>'").
  tool_filter_supported?: boolean
  tool_filter_arg_name?: string
  tool_filter_regex?: string
}

export function useAdminMcps() {
  return useQuery({
    queryKey: ['admin-mcps'],
    queryFn: async (): Promise<McpServer[]> => {
      const data = await apiJson<{ mcps: McpServer[] }>('/v1/admin/mcps').catch(withMessage('Failed to fetch MCPs'))
      return data.mcps
    },
  })
}

export interface EnableMcpResult {
  status: 'enabled'
  name: string
  docker_status?: EnableDockerResult | null
  docker_error?: string | null
}

export function useEnableMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string): Promise<EnableMcpResult> => {
      return apiJson<EnableMcpResult>(`/v1/admin/mcps/${name}/enable`, { method: 'PATCH' })
        .catch(withMessage('Failed to enable MCP'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useDisableMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string) => {
      await apiOk(`/v1/admin/mcps/${name}/disable`, { method: 'PATCH' }, 'Failed to disable MCP')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useSetMcpConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, values }: { name: string; values: Record<string, string> }) => {
      await apiOk(`/v1/admin/mcps/${name}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values }),
      }).catch(withMessage('Could not save the settings'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

/**
 * Update the per-MCP runtime tool filter regex.
 * Empty string clears the filter. Backend rejects when the manifest
 * doesn't declare `tool_filter` (the runtime would silently ignore the
 * regex — better to fail loudly).
 *
 * For Docker MCPs the backend restarts the container so the new
 * --enabled-tools flag takes effect at the next launch.
 */
export function useSetMcpToolFilter() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { name, regex }: { name: string; regex: string },
    ): Promise<{
      status: string
      name: string
      tool_filter_regex: string
      docker_restarted: boolean
    }> => {
      return apiJson(`/v1/admin/mcps/${name}/tool-filter`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ regex }),
      }, 'Failed to save tool filter')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useSetHostedServiceMode() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, mode }: { name: string; mode: 'self_managed' | 'hosted' }) => {
      await apiOk(`/v1/admin/mcps/${name}/hosted-service-mode`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      }).catch(withMessage('Failed to save hosted service mode'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useSetNetworkAccess() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, enabled }: { name: string; enabled: boolean }) => {
      await apiOk(`/v1/admin/mcps/${name}/network-access`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      }).catch(withMessage('Failed to save network access'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useDockerAction() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, action }: { name: string; action: 'start' | 'stop' | 'restart' }) => {
      // The server's sentence (a missing image says to run Update again).
      await apiOk(`/v1/admin/mcps/${name}/docker/${action}`, { method: 'POST' }, `Failed to ${action} container`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useDeleteMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string) => {
      return apiJson(`/v1/admin/mcps/${name}`, { method: 'DELETE' }, 'Delete failed')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

// One end of a source change: the identity the updater judges and the
// form the page shows (a registry page, an image reference or a host).
export interface McpSourceEnd {
  kind: string
  identity: string
  url: string
  runtime: string
  version?: string
}

export interface McpSourceChangePlan {
  runtime?: { from: string; to: string }
  credentials?: {
    carry: string[]
    rename: Record<string, string>
    reconnect: string[]
    oauth: 'none' | 'carry' | 'reconnect'
  }
  bearer_host_change?: boolean
}

export interface McpSourceChangeResult {
  version?: string
  renamed?: Record<string, string>
  kept?: string[]
  reconnect_needed?: string[]
  oauth?: string
  container_started?: boolean | null
  switched_at?: string
  // A failed or interrupted switch: the change is still pending.
  error?: string
  failed_at?: string
}

// A community MCP whose catalog source differs from the installed one
// (proxy COMMUNITY-MARKETPLACE.md "Source changes"): shown until the admin
// switches it or the catalog reverts, then as the switch's result until
// dismissed.
export interface McpSourceChange {
  status: 'pending' | 'switching' | 'switched'
  from: McpSourceEnd
  to: McpSourceEnd
  declared: boolean
  plan: McpSourceChangePlan
  // The catalog manifest the card describes; the Switch sends it back so the
  // proxy refuses a switch onto an entry that changed since the page loaded.
  to_manifest_hash: string
  detected_at: string
  accepted_at: string
  result: McpSourceChangeResult
}

export interface McpUpdateInfo {
  current: string
  latest: string
  // 'catalog' = docker MCP compared against the catalog version tag;
  // 'skills-catalog' = a skill package; a source change carries the
  // catalog kind of its new source ('npm' | 'pypi' | 'git' | 'image' | 'remote').
  registry: string
  package: string
  // Why an entry exists: a newer package version ('package'), a changed
  // catalog integration manifest ('manifest'), or both; the catalog source
  // moved ('source', a pending switch); a finished switch not yet dismissed
  // ('switched'); the install is ahead of the catalog ('ahead', no update).
  reason?: 'package' | 'manifest' | 'both' | 'source' | 'switched' | 'ahead'
  source_change?: McpSourceChange
  // npm/pypi offers: the version the weekly job applies now ("" none) and,
  // while the newest release waits out its first seven days, when it may
  // ("" when its release time is unknown: an admin applies it).
  settled?: string
  auto_after?: string
  // The install dir carries .hold: the weekly job skips it.
  held?: boolean
  // The venv runs on an interpreter the sandbox cannot use: Update rebuilds it.
  rebuild?: boolean
  // git+ offers: whether the catalog's source pins a full commit (the weekly
  // job builds only that; the catalog install refuses a tag or a branch), and
  // the ref it names.
  pinned_commit?: boolean
  catalog_ref?: string
}

export interface McpUpdateState {
  updates: Record<string, McpUpdateInfo>
  checked: number
  // Empty until the first check.
  checked_at: string
  auto_update_enabled?: boolean
}

// What the weekly job will do with an offer, in one line (null: nothing to
// add). An admin's Update applies the newest release at once either way.
export function automaticUpdateNote(info: McpUpdateInfo, autoUpdateEnabled: boolean): string | null {
  if (info.registry === 'git') {
    if (info.pinned_commit === false) {
      return 'Its source in the community list names a tag or a branch, not a commit. It can be installed once the list pins a commit.'
    }
    if (info.held) return 'Held: automatic updates skip it.'
    if (!autoUpdateEnabled) return 'Automatic updates are off.'
    return `On ${placePhrase(TARGET_LOCAL, '')}, the next automatic run builds the commit the community list pins. Paired machines rebuild it at their next sync.`
  }
  if (info.settled === undefined || info.reason === 'manifest') return null
  if (info.held) return 'Held: automatic updates skip it.'
  if (!autoUpdateEnabled) return 'Automatic updates are off.'
  if (info.auto_after === undefined) return null
  if (!info.auto_after) return 'Its release date is unknown: it applies when an admin updates it.'
  const when = new Date(info.auto_after).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
  // A catalog edit beside the waiting release waits with it ('both'): only a
  // package offer applies its settled release first.
  return info.settled && info.reason === 'package'
    ? `Automatic updates apply ${info.settled} now and ${info.latest} at the first run after ${when}.`
    : `Applies at the first automatic run after ${when}, seven days after its release.`
}

// An update offer the page counts and badges: never a finished switch nor an
// install ahead of the catalog.
export function isUpdateOffer(info: McpUpdateInfo | undefined): boolean {
  if (!info) return false
  const reason = info.reason ?? 'package'
  return reason === 'package' || reason === 'manifest' || reason === 'both' || reason === 'source'
}

// The last check as the proxy persisted it: the MCP Servers page shows it on
// load, after a navigation or a restart, without Check Updates pressed.
export function useMcpUpdateState() {
  return useQuery({
    queryKey: ['mcp-update-state'],
    queryFn: async (): Promise<McpUpdateState> => {
      return apiJson<McpUpdateState>('/v1/admin/mcps/update-state').catch(withMessage('Failed to load the update state'))
    },
    staleTime: 60_000,
  })
}

export function useCheckMcpUpdates() {
  return useQuery({
    queryKey: ['mcp-updates'],
    queryFn: async (): Promise<McpUpdateState> => {
      return apiJson<McpUpdateState>('/v1/admin/mcps/check-updates').catch(withMessage('Failed to check updates'))
    },
    staleTime: 300_000, // 5 min cache
    enabled: false, // only fetch on demand
  })
}

function dropFromUpdates(qc: ReturnType<typeof useQueryClient>, name: string) {
  for (const key of (['mcp-updates', 'mcp-update-state'] as const)) {
    qc.setQueryData([key], (old: McpUpdateState | undefined) => {
      if (!old?.updates) return old
      const { [name]: _, ...rest } = old.updates
      return { ...old, updates: rest }
    })
  }
}

/** An update's answer; the row shows its install log. */
export interface McpUpdateResult {
  install_log?: string
}

export function useUpdateMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string): Promise<McpUpdateResult> => {
      return apiJson<McpUpdateResult>(`/v1/admin/mcps/${name}/update`, { method: 'POST' }, 'Update failed')
    },
    onSuccess: (_data, name) => {
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
      // Remove this MCP from the cached updates
      dropFromUpdates(qc, name)
    },
  })
}

export interface AcceptSourceResult {
  status: 'switched'
  name: string
  from: string
  to: string
  version: string
  install_log: string
  result: McpSourceChangeResult
}

// The admin's switch: the body names exactly the pair the card showed.
export function useAcceptMcpSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, from, to, manifest_hash }: { name: string; from: string; to: string; manifest_hash: string }): Promise<AcceptSourceResult> => {
      return apiJson<AcceptSourceResult>(`/v1/admin/mcps/${name}/accept-source`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from, to, manifest_hash }),
      }, 'Switch failed')
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
      qc.invalidateQueries({ queryKey: ['mcp-update-state'] })
      qc.removeQueries({ queryKey: ['mcp-updates'] })
    },
  })
}

export function useDismissMcpSourceChange() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string) => {
      return apiJson(`/v1/admin/mcps/${name}/source-change`, { method: 'DELETE' }, 'Dismiss failed')
    },
    onSuccess: (_data, name) => {
      dropFromUpdates(qc, name)
      qc.invalidateQueries({ queryKey: ['mcp-update-state'] })
    },
  })
}

// Automatic MCP updates — run-history log for the Setup → System Settings card.
export interface McpAutoUpdateRow {
  run_id: string
  mcp_name: string
  runtime: string
  old_version: string
  new_version: string
  status: 'updated' | 'no_change' | 'skipped_in_use' | 'failed' | 'held' | 'needs_approval'
  error: string
  trigger: string
  ts: string
}

export function useMcpAutoUpdateLog() {
  return useQuery({
    queryKey: ['mcp-auto-update-log'],
    queryFn: async (): Promise<{ runs: McpAutoUpdateRow[]; last_run_at: string }> => {
      return apiJson<{ runs: McpAutoUpdateRow[]; last_run_at: string }>('/v1/admin/mcps/auto-update-log')
        .catch(withMessage('Failed to fetch auto-update log'))
    },
  })
}

export interface InstallResult {
  status: 'installed' | 'updated'
  name: string
  version: string
  old_version: string | null
  runtime: string
  install_log: string
}

export function useInstallMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (file: File): Promise<InstallResult> => {
      const form = new FormData()
      form.append('file', file)
      return apiJson<InstallResult>('/v1/admin/mcps/install', {
        method: 'POST',
        body: form,
      }, 'Install failed')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export interface SkillInstallResult {
  status: 'installed' | 'updated'
  name: string
  version: string
  old_version: string | null
  kind: 'skill'
  has_scripts: boolean
}

// Skill-package zip upload (admin Skills page). Accepts BOTH a platform
// skill package (manifest.json) and a bare spec-standard skill folder
// (SKILL.md is the manifest — the server synthesizes the package).
export function useInstallSkillZip() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (file: File): Promise<SkillInstallResult> => {
      const form = new FormData()
      form.append('file', file)
      return apiJson<SkillInstallResult>('/v1/admin/skills/install', {
        method: 'POST',
        body: form,
      }, 'Install failed')
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
      qc.invalidateQueries({ queryKey: ['community-skills'] })
    },
  })
}

// MCP Instance management
export function useMcpInstances(mcpName: string) {
  return useQuery({
    queryKey: ['mcp-instances', mcpName],
    queryFn: async (): Promise<McpInstancesResponse> => {
      return apiJson<McpInstancesResponse>(`/v1/admin/mcps/${mcpName}/instances`).catch(withMessage('Failed to fetch instances'))
    },
    enabled: !!mcpName,
  })
}

export function useCreateMcpInstance() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ mcpName, data }: { mcpName: string; data: { instance_name: string; field_values: Record<string, string>; agents: string[]; assigned_to_all?: boolean; hosted_mode?: 'self_managed' | 'hosted' } }) => {
      return apiJson(`/v1/admin/mcps/${mcpName}/instances`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }).catch(withMessage('Failed to create instance'))
    },
    onSuccess: (_, { mcpName }) => {
      qc.invalidateQueries({ queryKey: ['mcp-instances', mcpName] })
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
      // Saving an instance can auto-retry install_failed requests
      // (server-side _retry_install_failed_for_instance hook). Refetch
      // the admin Requests page so any flipped statuses show up without
      // a manual refresh.
      qc.invalidateQueries({ queryKey: ['admin-mcp-requests'] })
      qc.invalidateQueries({ queryKey: ['community-mcps'] })
    },
  })
}

export function useUpdateMcpInstance() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ mcpName, instanceId, data }: { mcpName: string; instanceId: number; data: { instance_name: string; field_values: Record<string, string>; agents: string[]; assigned_to_all?: boolean; hosted_mode?: 'self_managed' | 'hosted' } }) => {
      return apiJson(`/v1/admin/mcps/${mcpName}/instances/${instanceId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }).catch(withMessage('Failed to update instance'))
    },
    onSuccess: (_, { mcpName }) => {
      qc.invalidateQueries({ queryKey: ['mcp-instances', mcpName] })
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
      // Same auto-retry sweep as create — see useCreateMcpInstance.
      qc.invalidateQueries({ queryKey: ['admin-mcp-requests'] })
      qc.invalidateQueries({ queryKey: ['community-mcps'] })
    },
  })
}

export function useDeleteMcpInstance() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ mcpName, instanceId }: { mcpName: string; instanceId: number }) => {
      await apiOk(`/v1/admin/mcps/${mcpName}/instances/${instanceId}`, { method: 'DELETE' })
        .catch(withMessage('Failed to delete instance'))
    },
    onSuccess: (_, { mcpName }) => {
      qc.invalidateQueries({ queryKey: ['mcp-instances', mcpName] })
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
    },
  })
}

// SSH Keys (still file-based, used by ssh_key_select input type)
export interface SshKey {
  name: string
  size: number
}

export function useSshKeys() {
  return useQuery({
    queryKey: ['ssh-keys'],
    queryFn: async (): Promise<SshKey[]> => {
      return (await apiJson<{ keys: SshKey[] }>('/v1/admin/ssh/keys').catch(withMessage('Failed to fetch SSH keys'))).keys
    },
  })
}

export function useUploadSshKey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      await apiOk('/v1/admin/ssh/keys', { method: 'POST', body: form }).catch(withMessage('Failed to upload key'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ssh-keys'] }),
  })
}

export function useDeleteSshKey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string) => {
      await apiOk(`/v1/admin/ssh/keys/${encodeURIComponent(name)}`, { method: 'DELETE' })
        .catch(withMessage('Failed to delete key'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ssh-keys'] }),
  })
}

// Agent MCP assignments — visibility/enablement two-state model.
//
// `enabled`        = manager has toggled this MCP on for this agent (agent_mcps row)
// `authorized_by`  = "auto" (always visible to every agent) or "admin" (admin-authorized
//                    via instance, may be assigned_to_all or per-agent)
//
// Both authorized_by states are toggleable by the manager. The "via admin" hint is
// purely informational, not gating.
export interface AgentMcp {
  name: string
  label: string
  description: string
  category: string
  author?: string
  author_url?: string
  /** The install dir ships an icon.png. */
  icon?: boolean
  assignment_mode: 'auto' | 'explicit'
  credential_type: string
  /** Manifest declares a service account — the binding dropdown only
   *  renders (and queries its options) where this is true. */
  has_service_account: boolean
  enabled: boolean
  authorized_by: 'auto' | 'admin'
}

export interface AgentMcpData {
  mcps: AgentMcp[]
}

export class AgentMcpsNotVisibleError extends Error {
  notVisible: string[]
  constructor(notVisible: string[]) {
    super(`MCPs not visible to this agent: ${notVisible.join(', ')}`)
    this.notVisible = notVisible
  }
}

// The list is owner-only server-side; a page a non-owner can reach by URL
// passes `enabled: false` instead of collecting a 403.
export function useAgentMcps(agent: string, enabled = true) {
  return useQuery({
    queryKey: ['agent-mcps', agent],
    queryFn: async (): Promise<AgentMcpData> => {
      return apiJson<AgentMcpData>(`/v1/agents/${agent}/mcps`).catch(withMessage('Failed to fetch agent MCPs'))
    },
    enabled: !!agent && enabled,
  })
}

export function useSetAgentMcps() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, mcps }: { agent: string; mcps: string[] }) => {
      await apiOk(`/v1/agents/${agent}/mcps`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mcps }),
      }).catch((e: unknown) => {
        // A 400 names the MCPs the agent cannot see, or says why it refused.
        if (e instanceof ApiError && e.status === 400) {
          const detail = e.detail
          if (isRecord(detail) && Array.isArray(detail.not_visible)) {
            throw new AgentMcpsNotVisibleError(detail.not_visible)
          }
          throw new Error(String((isRecord(detail) && detail.error) || 'Failed to save agent MCPs'))
        }
        return withMessage('Failed to save agent MCPs')(e)
      })
    },
    // Resolves with the agent's own list, the one the saving page shows.
    onSuccess: (_, { agent }) => {
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
      qc.invalidateQueries({ queryKey: ['agent-info', agent] })
      // Skills depend on the manager-enabled set; refetch so the skills tab updates
      qc.invalidateQueries({ queryKey: ['agent-skills', agent] })
      // Per-user integrations depend on the visibility set per agent; if a manager
      // enables a per-user-creds MCP, the integrations panel should refresh.
      qc.invalidateQueries({ queryKey: ['my-integrations'] })
      return qc.invalidateQueries({ queryKey: ['agent-mcps', agent] })
    },
  })
}

// Agent skills
export interface AgentSkill {
  id: string
  mcp_name: string
  mcp_label: string
  description: string
  /** True when the provider is a standalone skill package (category "skill")
   *  rather than a skill bundled inside a regular MCP. */
  standalone: boolean
  /** 'always' (body inlined into the system prompt) or 'on_demand'
   *  (progressive disclosure via the CLI's skills dir). */
  loading: string
  /** Whether the provider package/MCP is currently assigned to the agent.
   *  Unassigned standalone packages still list (enabled=false) so the tab
   *  can offer enabling them — the PATCH auto-assigns server-side. */
  assigned: boolean
  enabled: boolean
  exclude_from: string[]
  default_exclude_from: string[]
}

export function useAgentSkills(agent: string, enabled = true) {
  return useQuery({
    queryKey: ['agent-skills', agent],
    queryFn: async (): Promise<AgentSkill[]> => {
      const data = await apiJson<{ skills: AgentSkill[] }>(`/v1/agents/${agent}/skills`).catch(withMessage('Failed to fetch agent skills'))
      return data.skills
    },
    enabled: !!agent && enabled,
  })
}

/**
 * Toggle a skill / edit its context exclusions for an agent.
 * Manager-gated like MCP assignment. Enabling a skill whose provider is an
 * unassigned standalone package auto-assigns the package server-side, so we
 * also refresh the community-skills catalog augmentation (enabled_for_agents).
 */
export function useSetAgentSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, skillId, enabled, exclude_from }: {
      agent: string
      skillId: string
      enabled: boolean
      exclude_from: string[]
    }) => {
      return apiJson(
        `/v1/agents/${agent}/skills/${encodeURIComponent(skillId)}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled, exclude_from }),
        },
        'Failed to save skill',
      )
    },
    onSuccess: (_, { agent }) => {
      qc.invalidateQueries({ queryKey: ['community-skills'] })
      return qc.invalidateQueries({ queryKey: ['agent-skills', agent] })
    },
  })
}

