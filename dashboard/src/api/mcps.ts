import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

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

export interface McpServer {
  name: string
  label: string
  description: string
  version: string
  category: 'core' | 'custom' | 'community'
  runtime: 'python' | 'node' | 'docker'
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
  docker_status?: 'running' | 'unhealthy' | 'starting' | 'stopped' | 'not_found' | 'not_checked' | 'unknown' | 'error'
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
      const res = await apiFetch('/v1/admin/mcps')
      if (!res.ok) throw new Error('Failed to fetch MCPs')
      const data = await res.json()
      return data.mcps
    },
  })
}

export interface EnableMcpResult {
  status: 'enabled'
  name: string
  docker_status?: 'started' | 'failed' | null
  docker_error?: string | null
}

export function useEnableMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string): Promise<EnableMcpResult> => {
      const res = await apiFetch(`/v1/admin/mcps/${name}/enable`, { method: 'PATCH' })
      if (!res.ok) throw new Error('Failed to enable MCP')
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useDisableMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string) => {
      const res = await apiFetch(`/v1/admin/mcps/${name}/disable`, { method: 'PATCH' })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to disable MCP')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useSetMcpConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, values }: { name: string; values: Record<string, string> }) => {
      const res = await apiFetch(`/v1/admin/mcps/${name}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values }),
      })
      if (!res.ok) throw new Error('Failed to save config')
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
      const res = await apiFetch(`/v1/admin/mcps/${name}/tool-filter`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ regex }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to save tool filter')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useSetHostedServiceMode() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, mode }: { name: string; mode: 'self_managed' | 'hosted' }) => {
      const res = await apiFetch(`/v1/admin/mcps/${name}/hosted-service-mode`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      })
      if (!res.ok) throw new Error('Failed to save hosted service mode')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useSetNetworkAccess() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, enabled }: { name: string; enabled: boolean }) => {
      const res = await apiFetch(`/v1/admin/mcps/${name}/network-access`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      if (!res.ok) throw new Error('Failed to save network access')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useDockerAction() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, action }: { name: string; action: 'start' | 'stop' | 'restart' }) => {
      const res = await apiFetch(`/v1/admin/mcps/${name}/docker/${action}`, { method: 'POST' })
      if (!res.ok) throw new Error(`Failed to ${action} container`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export function useDeleteMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string) => {
      const res = await apiFetch(`/v1/admin/mcps/${name}`, { method: 'DELETE' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Delete failed' }))
        throw new Error(err.detail || `Delete failed (${res.status})`)
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

export interface McpUpdateInfo {
  current: string
  latest: string
  // 'catalog' = docker MCP compared against the catalog version tag.
  registry: 'npm' | 'pypi' | 'catalog'
  package: string
  // Why an update is offered: a newer package version ('package'), a changed
  // catalog integration manifest ('manifest'), or both. For 'manifest' the
  // package version is unchanged (current === latest).
  reason?: 'package' | 'manifest' | 'both'
}

export function useCheckMcpUpdates() {
  return useQuery({
    queryKey: ['mcp-updates'],
    queryFn: async (): Promise<{ updates: Record<string, McpUpdateInfo>; checked: number }> => {
      const res = await apiFetch('/v1/admin/mcps/check-updates')
      if (!res.ok) throw new Error('Failed to check updates')
      return res.json()
    },
    staleTime: 300_000, // 5 min cache
    enabled: false, // only fetch on demand
  })
}

export function useUpdateMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string) => {
      const res = await apiFetch(`/v1/admin/mcps/${name}/update`, { method: 'POST' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Update failed' }))
        throw new Error(err.detail || `Update failed (${res.status})`)
      }
      return res.json()
    },
    onSuccess: (_data, name) => {
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
      // Remove this MCP from the cached updates
      qc.setQueryData(['mcp-updates'], (old: any) => {
        if (!old?.updates) return old
        const { [name]: _, ...rest } = old.updates
        return { ...old, updates: rest }
      })
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
  status: 'updated' | 'no_change' | 'skipped_in_use' | 'failed' | 'held'
  error: string
  trigger: string
  ts: string
}

export function useMcpAutoUpdateLog() {
  return useQuery({
    queryKey: ['mcp-auto-update-log'],
    queryFn: async (): Promise<{ runs: McpAutoUpdateRow[]; last_run_at: string }> => {
      const res = await apiFetch('/v1/admin/mcps/auto-update-log')
      if (!res.ok) throw new Error('Failed to fetch auto-update log')
      return res.json()
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
      const res = await apiFetch('/v1/admin/mcps/install', {
        method: 'POST',
        body: form,
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Install failed' }))
        throw new Error(err.detail || `Install failed (${res.status})`)
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-mcps'] }),
  })
}

// MCP Instance management
export function useMcpInstances(mcpName: string) {
  return useQuery({
    queryKey: ['mcp-instances', mcpName],
    queryFn: async (): Promise<McpInstancesResponse> => {
      const res = await apiFetch(`/v1/admin/mcps/${mcpName}/instances`)
      if (!res.ok) throw new Error('Failed to fetch instances')
      return res.json()
    },
    enabled: !!mcpName,
  })
}

export function useCreateMcpInstance() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ mcpName, data }: { mcpName: string; data: { instance_name: string; field_values: Record<string, string>; agents: string[]; assigned_to_all?: boolean; hosted_mode?: 'self_managed' | 'hosted' } }) => {
      const res = await apiFetch(`/v1/admin/mcps/${mcpName}/instances`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to create instance')
      return res.json()
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
      const res = await apiFetch(`/v1/admin/mcps/${mcpName}/instances/${instanceId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to update instance')
      return res.json()
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
      const res = await apiFetch(`/v1/admin/mcps/${mcpName}/instances/${instanceId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete instance')
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
      const res = await apiFetch('/v1/admin/ssh/keys')
      if (!res.ok) throw new Error('Failed to fetch SSH keys')
      return (await res.json()).keys
    },
  })
}

export function useUploadSshKey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      const res = await apiFetch('/v1/admin/ssh/keys', { method: 'POST', body: form })
      if (!res.ok) throw new Error('Failed to upload key')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ssh-keys'] }),
  })
}

export function useDeleteSshKey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (name: string) => {
      const res = await apiFetch(`/v1/admin/ssh/keys/${encodeURIComponent(name)}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete key')
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

export function useAgentMcps(agent: string) {
  return useQuery({
    queryKey: ['agent-mcps', agent],
    queryFn: async (): Promise<AgentMcpData> => {
      const res = await apiFetch(`/v1/agents/${agent}/mcps`)
      if (!res.ok) throw new Error('Failed to fetch agent MCPs')
      return res.json()
    },
    enabled: !!agent,
  })
}

export function useSetAgentMcps() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, mcps }: { agent: string; mcps: string[] }) => {
      const res = await apiFetch(`/v1/agents/${agent}/mcps`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mcps }),
      })
      if (res.status === 400) {
        const body = await res.json().catch(() => ({}))
        const detail = body?.detail
        if (detail && Array.isArray(detail.not_visible)) {
          throw new AgentMcpsNotVisibleError(detail.not_visible)
        }
        throw new Error(detail?.error || 'Failed to save agent MCPs')
      }
      if (!res.ok) throw new Error('Failed to save agent MCPs')
    },
    onSuccess: (_, { agent }) => {
      qc.invalidateQueries({ queryKey: ['agent-mcps', agent] })
      qc.invalidateQueries({ queryKey: ['admin-mcps'] })
      qc.invalidateQueries({ queryKey: ['agent-info', agent] })
      // Skills depend on the manager-enabled set; refetch so the skills tab updates
      qc.invalidateQueries({ queryKey: ['agent-skills', agent] })
      // Per-user integrations depend on the visibility set per agent; if a manager
      // enables a per-user-creds MCP, the integrations panel should refresh.
      qc.invalidateQueries({ queryKey: ['my-integrations'] })
    },
  })
}

// Agent skills
export interface AgentSkill {
  id: string
  mcp_name: string
  mcp_label: string
  description: string
  enabled: boolean
  exclude_from: string[]
  default_exclude_from: string[]
}

export function useAgentSkills(agent: string) {
  return useQuery({
    queryKey: ['agent-skills', agent],
    queryFn: async (): Promise<AgentSkill[]> => {
      const res = await apiFetch(`/v1/agents/${agent}/skills`)
      if (!res.ok) throw new Error('Failed to fetch agent skills')
      const data = await res.json()
      return data.skills
    },
    enabled: !!agent,
  })
}

