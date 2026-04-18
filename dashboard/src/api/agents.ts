import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface AgentSummary {
  name: string
  display_name: string
  admin_only: boolean
  execution_path: string
  execution_paths: string[]
  execution_target: string
  // Visibility mode column. Together with `default_scope` it maps to one of the
  // four modes — see `lib/visibility.ts`.
  collaborative: boolean
  default_model: string
  // v3: per-agent default scope for memory / tasks / notifications /
  // triggers / meetings. Drives the OTO_DEFAULT_SCOPE env var.
  default_scope: 'user' | 'agent'
  // Interactive-CLI per-agent default execution mode.
  default_execution_mode?: '' | 'interactive' | '-p'
  color: string
  description: string
  mcp_count: number
  mcp_names: string[]
  schedule_count: number
  trigger_count: number
  has_workspace: boolean
}

export interface DelegationTargetsData {
  targets: string[]
  available: { name: string; display_name: string; color: string }[]
}

export interface AgentInfo {
  name: string
  display_name: string
  admin_only: boolean
  execution_path: string
  execution_paths: string[]
  execution_target: string
  default_effort: string
  // Visibility mode column. Together with `default_scope` it maps to one of the
  // four modes — see `lib/visibility.ts`.
  collaborative: boolean
  default_model: string
  // v3: per-agent default scope for memory / tasks / notifications /
  // triggers / meetings. Drives the OTO_DEFAULT_SCOPE env var.
  default_scope: 'user' | 'agent'
  color: string
  mcps: string[]
  has_workspace: boolean
  description: string
  delegation_targets: string[]
  // Empty string when auto-attach is disabled for this agent;
  // otherwise one of 'viewer' / 'editor' / 'manager'. Admin-only field.
  default_for_new_users_role: '' | 'viewer' | 'editor' | 'manager'
  // Interactive-CLI per-agent default execution mode: '' (unset)
  // | 'interactive' | '-p'. Only meaningful for CLI execution layers.
  default_execution_mode?: '' | 'interactive' | '-p'
}

export interface FileNode {
  name: string
  type: 'file' | 'dir'
  path: string
  size: number
  modified: string
  children?: FileNode[]
}

export const useAgents = (opts?: { all?: boolean }) =>
  useQuery({
    queryKey: ['agents', opts?.all ?? false],
    queryFn: async (): Promise<AgentSummary[]> => {
      const params = opts?.all ? '?all=true' : ''
      const res = await apiFetch(`/v1/agents${params}`)
      const data = await res.json()
      return data.agents ?? []
    },
    refetchInterval: 60_000,
  })

// ---------------------------------------------------------------------------
// Execution layers (capabilities, models, providers)
// ---------------------------------------------------------------------------

export interface LayerCapabilities {
  name: string
  display_name: string
  supports_resume: boolean
  supports_permissions: boolean
  supports_plan_mode: boolean
  supports_todos: boolean
  supports_subagents: boolean
  supports_context_compression: boolean
  supports_control_commands: boolean
  supports_mcps: boolean
  permission_modes: string[]
  control_commands: string[]
  models: { value: string; label: string; provider?: string; supports_xhigh?: boolean; supports_ultra?: boolean }[]
  effort_levels: string[]
  effort_changeable_mid_session: boolean
  compression_threshold_pct: number | null
  mcp_delivery: string
  mcp_config_format: string | null
  providers: { id: string; label: string; requires_key?: boolean }[] | null
}

export const useExecutionLayers = () =>
  useQuery({
    queryKey: ['execution-layers'],
    queryFn: async (): Promise<Record<string, LayerCapabilities>> => {
      const res = await apiFetch('/v1/execution-layers')
      return res.json()
    },
    staleTime: 5 * 60_000, // refresh every 5 min
  })

// ---------------------------------------------------------------------------
// Agent info
// ---------------------------------------------------------------------------

export const useAgentInfo = (name: string) =>
  useQuery({
    queryKey: ['agent-info', name],
    queryFn: async (): Promise<AgentInfo> => {
      const res = await apiFetch(`/v1/agents/${name}/info`)
      return res.json()
    },
    enabled: !!name,
  })

// Users attached to an agent (with per-agent role) — agent-settings overview.
// Manager/admin-gated server-side; returns [] for non-managers.
export interface AgentUser {
  sub: string
  name: string
  email: string
  role: 'viewer' | 'editor' | 'manager' | 'admin' | string
}

export const useAgentUsers = (name: string) =>
  useQuery({
    queryKey: ['agent-users', name],
    queryFn: async (): Promise<AgentUser[]> => {
      const res = await apiFetch(`/v1/agents/${name}/users`)
      if (!res.ok) return []
      const data = await res.json()
      return data.users ?? []
    },
    enabled: !!name,
  })

// Live status of the agent's effective execution target for the current
// user (user override > agent default). Polled every 15s so the chat
// TopBar can show a connection dot next to the agent name. Returns
// `{state: null}` when the agent runs locally for this user. `scope`
// distinguishes the caller's own machine ('user' → amber when offline,
// since it soft-falls-back to local) from the agent's admin-paired
// default ('admin' → red when offline, blocks everyone on the agent).
export type AgentTargetStatus = {
  state: 'online' | 'stale' | 'disconnected' | 'never_connected' | null
  scope?: 'admin' | 'user'
  machine_name?: string
  last_heartbeat_age_s?: number | null
  last_seen_iso?: string | null
}

export const useAgentTargetStatus = (name: string) =>
  useQuery({
    queryKey: ['agent-target-status', name],
    queryFn: async (): Promise<AgentTargetStatus> => {
      const res = await apiFetch(`/v1/agents/${name}/target-status`)
      return res.json()
    },
    enabled: !!name,
    refetchInterval: 15_000,
    staleTime: 10_000,
  })

export const useAgentFiles = (name: string) =>
  useQuery({
    queryKey: ['agent-files', name],
    queryFn: async (): Promise<FileNode[]> => {
      const res = await apiFetch(`/v1/agents/${name}/files`)
      const data = await res.json()
      return data.tree ?? []
    },
    enabled: !!name,
  })

export const useAgentFileContent = (name: string, path: string | null) =>
  useQuery({
    queryKey: ['agent-file-content', name, path],
    queryFn: async (): Promise<string> => {
      const res = await apiFetch(`/v1/agents/${name}/files/${path}`)
      const data = await res.json()
      return data.content ?? ''
    },
    enabled: !!name && !!path,
  })

export const useSaveAgentFile = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, path, content }: { agent: string; path: string; content: string }) => {
      const res = await apiFetch(`/v1/agents/${agent}/files/${path}`, {
        method: 'PUT',
        body: JSON.stringify({ content }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-file-content', vars.agent, vars.path] })
      qc.invalidateQueries({ queryKey: ['agent-files', vars.agent] })
    },
  })
}

export const useCreateAgentDir = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, path }: { agent: string; path: string }) => {
      const res = await apiFetch(`/v1/agents/${agent}/mkdir`, {
        method: 'POST',
        body: JSON.stringify({ path }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-files', vars.agent] })
    },
  })
}

export const useCreateAgentFile = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, path, fileType }: { agent: string; path: string; fileType?: string }) => {
      const res = await apiFetch(`/v1/agents/${agent}/create-file`, {
        method: 'POST',
        body: JSON.stringify({ path, file_type: fileType || '' }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-files', vars.agent] })
    },
  })
}

export const useSetDefaultAgent = () => {
  return useMutation({
    mutationFn: async (agent: string) => {
      const res = await apiFetch('/v1/users/me/default-agent', {
        method: 'PUT',
        body: JSON.stringify({ default_agent: agent }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
  })
}

export const useDeleteAgentPath = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, path, recursive }: { agent: string; path: string; recursive?: boolean }) => {
      const res = await apiFetch(`/v1/agents/${agent}/delete`, {
        method: 'POST',
        body: JSON.stringify({ path, recursive: recursive ?? false }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-files', vars.agent] })
      qc.invalidateQueries({ queryKey: ['agent-file-content', vars.agent] })
    },
  })
}

export const useRenameAgentPath = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, oldPath, newPath }: { agent: string; oldPath: string; newPath: string }) => {
      const res = await apiFetch(`/v1/agents/${agent}/rename`, {
        method: 'POST',
        body: JSON.stringify({ old_path: oldPath, new_path: newPath }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-files', vars.agent] })
      qc.invalidateQueries({ queryKey: ['agent-file-content', vars.agent] })
    },
  })
}

// ---------------------------------------------------------------------------
// Batch ops: move (cut+paste), copy (copy+paste), zip (multi-/folder-download)
// ---------------------------------------------------------------------------

export interface BatchOpResult {
  moved?: Array<{ src: string; dest: string; noop?: boolean }>
  copied?: Array<{ src: string; dest: string }>
  failed: Array<{ src: string; reason: string }>
}

export const useMoveAgentPaths = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { agent, srcPaths, destDir }: { agent: string; srcPaths: string[]; destDir: string },
    ): Promise<BatchOpResult> => {
      const res = await apiFetch(`/v1/agents/${agent}/move`, {
        method: 'POST',
        body: JSON.stringify({ src_paths: srcPaths, dest_dir: destDir }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-files', vars.agent] })
      qc.invalidateQueries({ queryKey: ['agent-file-content', vars.agent] })
    },
  })
}

export const useCopyAgentPaths = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { agent, srcPaths, destDir }: { agent: string; srcPaths: string[]; destDir: string },
    ): Promise<BatchOpResult> => {
      const res = await apiFetch(`/v1/agents/${agent}/copy`, {
        method: 'POST',
        body: JSON.stringify({ src_paths: srcPaths, dest_dir: destDir }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-files', vars.agent] })
    },
  })
}

/**
 * Fetches a zip archive of the given paths and triggers a browser download.
 * Filename is taken from the Content-Disposition response header (the
 * backend computes a "smart" name — `<basename>.zip` for single source,
 * `workspace-files-<ts>.zip` for multi).
 */
export const useZipAgentPaths = () => {
  return useMutation({
    mutationFn: async ({ agent, paths }: { agent: string; paths: string[] }) => {
      // Two-step flow: POST to mint a short-lived signed URL, then navigate
      // to the URL via an `<a>` click. The GET endpoint streams the zip with
      // Content-Disposition so browsers trigger a download and Android's
      // DownloadManager handles it natively (it can't download blob: URLs
      // from the older POST→blob flow).
      const res = await apiFetch(`/v1/agents/${agent}/zip-url`, {
        method: 'POST',
        body: JSON.stringify({ paths }),
      })
      if (!res.ok) throw new Error(await res.text())
      const { download_url, filename } = await res.json() as {
        download_url: string
        filename: string
      }
      const a = document.createElement('a')
      a.href = download_url
      a.download = filename
      a.click()
      return { filename }
    },
  })
}

export function useCreateAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: {
      display_name: string
      slug?: string
      admin_only?: boolean
      // Visibility mode columns — see `lib/visibility.ts`. Omitted → the
      // backend defaults to Personal + shared (collaborative, user scope).
      collaborative?: boolean
      execution_path?: string
      default_scope?: 'user' | 'agent'
    }) => {
      const res = await apiFetch('/v1/agents', { method: 'POST', body: JSON.stringify(data) })
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Failed to create agent') }
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['agents'] }) },
  })
}

export function useUpdateAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, ...fields }: { name: string; [key: string]: any }) => {
      const res = await apiFetch(`/v1/agents/${name}`, { method: 'PATCH', body: JSON.stringify(fields) })
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Failed to update') }
      return res.json()
    },
    onSuccess: (_, { name }) => {
      qc.invalidateQueries({ queryKey: ['agents'] })
      qc.invalidateQueries({ queryKey: ['agent-info', name] })
    },
  })
}

export function useDeleteAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ name, confirm_slug }: { name: string; confirm_slug: string }) => {
      const res = await apiFetch(`/v1/agents/${name}`, { method: 'DELETE', body: JSON.stringify({ confirm_slug }) })
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Failed to delete') }
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['agents'] }) },
  })
}

export function useDelegationTargets(agent: string) {
  return useQuery({
    queryKey: ['delegation-targets', agent],
    queryFn: async (): Promise<DelegationTargetsData> => {
      const res = await apiFetch(`/v1/agents/${agent}/delegation-targets`)
      if (!res.ok) throw new Error('Failed to fetch delegation targets')
      return res.json()
    },
    enabled: !!agent,
  })
}

export function useSetDelegationTargets() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, targets }: { agent: string; targets: string[] }) => {
      const res = await apiFetch(`/v1/agents/${agent}/delegation-targets`, {
        method: 'PUT',
        body: JSON.stringify({ targets }),
      })
      if (!res.ok) throw new Error('Failed to save delegation targets')
      return res.json()
    },
    onSuccess: (_, { agent }) => {
      qc.invalidateQueries({ queryKey: ['delegation-targets', agent] })
      qc.invalidateQueries({ queryKey: ['agent-info', agent] })
    },
  })
}

// ---------------------------------------------------------------------------
// Default-for-new-users (admin-only)
// ---------------------------------------------------------------------------

export function useSetDefaultForNewUsers() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { agent, enabled, role }:
      { agent: string; enabled: boolean; role: 'viewer' | 'editor' | 'manager' | null },
    ) => {
      const res = await apiFetch(
        `/v1/admin/agents/${agent}/default-for-new-users`,
        {
          method: 'PUT',
          body: JSON.stringify({ enabled, role }),
        },
      )
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || 'Failed to update') }
      return res.json()
    },
    onSuccess: (_, { agent }) => {
      qc.invalidateQueries({ queryKey: ['agent-info', agent] })
    },
  })
}

// ---------------------------------------------------------------------------
// Workspace Recover Bin — recover files removed or overwritten involuntarily
// (sync reconcile on reconnect, proxy-wins overwrite) or deleted via the
// dashboard. Scope is server-enforced: a member sees only their own
// users/<slug>/ entries; a manager additionally sees shared workspace/
// knowledge/config entries. Entries expire after 7 days.
// ---------------------------------------------------------------------------

export interface RecoverBinEntry {
  entry_id: string
  rel_path: string
  original_name: string
  reason: 'deleted' | 'reconciled' | 'overwritten'
  scope: 'user' | 'shared'
  size: number
  binned_at: string
}

export interface RestoreResult {
  restored: { entry_id: string; rel_path: string }[]
  renamed: { entry_id: string; original: string; restored_as: string }[]
  denied: string[]
}

export const useRecoverBin = (agent: string) =>
  useQuery({
    queryKey: ['agent-recover-bin', agent],
    queryFn: async (): Promise<RecoverBinEntry[]> => {
      const res = await apiFetch(`/v1/agents/${agent}/recover-bin`)
      if (!res.ok) return []
      const data = await res.json()
      return data.entries ?? []
    },
    enabled: !!agent,
    staleTime: 10_000,
  })

export const useRestoreFiles = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { agent, entryIds }: { agent: string; entryIds: string[] },
    ): Promise<RestoreResult> => {
      const res = await apiFetch(`/v1/agents/${agent}/recover-bin/restore`, {
        method: 'POST',
        body: JSON.stringify({ entry_ids: entryIds }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-files', vars.agent] })
      qc.invalidateQueries({ queryKey: ['agent-recover-bin', vars.agent] })
    },
  })
}

export const useDiscardFiles = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { agent, entryIds }: { agent: string; entryIds: string[] },
    ): Promise<{ discarded: string[]; denied: string[] }> => {
      const res = await apiFetch(`/v1/agents/${agent}/recover-bin/discard`, {
        method: 'POST',
        body: JSON.stringify({ entry_ids: entryIds }),
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['agent-recover-bin', vars.agent] })
    },
  })
}
