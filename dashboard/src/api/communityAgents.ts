/**
 * Community agents catalog hooks.
 *
 * Mirrors `./community.ts` (which serves the MCPs catalog) — same React
 * Query patterns, just pointed at the new `/v1/community/agents*`
 * endpoints. Used by `CommunityAgentsBrowser` + `AgentInstallModal`.
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import type { PinnedApp } from './apps'
import { useAuth } from '../contexts/AuthContext'
import { apiJson, apiResponse, raiseApiError } from '../lib/http/apiJson'

export interface CommunityAgentRegistryEntry {
  slug: string
  display_name: string
  description: string
  long_description_url: string
  color: string
  version: string
  category: string
  tags: string[]
  author: string
  author_url: string | null
  license: string
  icon_url: string | null
  readme_url: string
  manifest_url: string
  required_mcps: { name: string; min_version?: string | null; skills?: string[] }[]
  has_triggers: boolean
  has_tasks: boolean
  has_notifications: boolean
  has_setup: boolean
  has_context: boolean
  /** Since 1.7 (COMMUNITY-AGENTS-REGISTRY.md): the template ships folder
   * apps (shared, or one per member) and agent checks; absent on an
   * older registry. */
  has_apps?: boolean
  has_user_apps?: boolean
  has_checks?: boolean
  platform_min_version: string | null
  deprecated: boolean
  deprecation_note: string | null
  // Augmented by the proxy:
  installed_as: string[]
  /** Since 1.7: each installed agent with its version and whether the
   * catalog is newer (the card's Update button). */
  installed?: InstalledRef[]
}

export interface InstalledRef {
  agent_slug: string
  version: string
  update_available: boolean
}

/** What a template update did, or would do (COMMUNITY-AGENTS-REGISTRY.md
 * "Updates"): a piece still as installed is replaced, an edited one kept
 * with the new version stored beside it, a new one added. */
export interface TemplateUpdateKept {
  what: string
  reason: string
  path: string
  new_path: string
}

export interface TemplateUpdateReport {
  agent_slug?: string
  from_version?: string
  to_version?: string
  replaced: string[]
  added: string[]
  kept: TemplateUpdateKept[]
  unchanged: number
  pending_apps: string[]
  offered_checks: string[]
  members?: Record<string, number>
  mcps?: { ready?: string[]; requested?: string[]; new?: string[] }
  ignored_fields?: string[]
  notes?: string[]
  /** The tasks whose model choice this agent cannot run, each a sentence
   * (they run on the agent's default model and engine). */
  task_pins_dropped?: string[]
}

/** The heading over the tasks whose model choice the agent cannot run. */
export const TASK_PINS_HEADING = 'Model choices left out'

/** An install's or an update's dropped task pins as sentences to show; an
 * older proxy sends none. */
export function taskPinNotes(from: { task_pins_dropped?: string[] } | null | undefined): string[] {
  return (from?.task_pins_dropped ?? [])
    .filter(n => typeof n === 'string' && n !== '')
    .map(n => n.charAt(0).toUpperCase() + n.slice(1))
}

export interface TemplateUpdatePlan extends TemplateUpdateReport {
  template_slug: string
  display_name: string
  apps: (PreviewApp & { change: 'new' | 'changed' | 'same' })[]
  checks: (PreviewCheck & { change: 'new' | 'changed' | 'same' })[]
  consent_scope: 'everyone' | 'own'
  has_baseline: boolean
}

export interface TemplateUpdateStatus {
  running: boolean
  id?: string
  status?: 'running' | 'done' | 'failed'
  to_version?: string
  report?: TemplateUpdateReport | null
  error?: string
}

/** One app a template ships, as the install dialog shows it: the approval
 * card's row shape over the raw manifest, the tasks its buttons run, and
 * the signature the consent carries. `owner_approval`: each member
 * approves their own copy whoever installs. */
export interface PreviewApp {
  slug: string
  title: string
  visibility: 'agent' | 'user'
  sig: string
  owner_approval: boolean
  row: PinnedApp
  blueprint_tasks: { slug: string; description: string; prompt: string }[]
  // The triggers the seed creates with each copy (one per member for a
  // personal app, the agent's for a shared one), aimed at its handlers.
  blueprint_triggers?: { slug: string; handler: string; description: string }[]
}

/** One check a template ships: what consenting to it lets run. */
export interface PreviewCheck {
  name: string
  description: string
  mandatory: boolean
  applies: string[]
  script: string
  sig: string
  words: string
}

export interface CommunityAgentsResponse {
  registry_version: string
  updated_at: string
  platform_min_version: string | null
  fetched_from: string
  agents: CommunityAgentRegistryEntry[]
}

export interface CommunityAgentDetail {
  entry: CommunityAgentRegistryEntry
  manifest: Record<string, unknown> | null
  readme: string | null
}

export interface InstallPreview {
  template_slug: string
  target_slug: string
  slug_available: boolean
  suggested_slug: string | null
  required_mcps: {
    name: string
    installed: boolean
    request_type: 'install' | 'access' | null
    blocked: boolean
    needs_request: boolean
    reason: string
  }[]
  will_create_tasks_agent_scope: number
  platform_compat_ok: boolean
  /** Since 1.7: the apps and checks the template ships and whose copies
   * this caller's consent may cover (`everyone` for an admin, else the
   * shared apps and their own copy). Absent on an older proxy. */
  apps?: PreviewApp[]
  checks?: PreviewCheck[]
  consent_scope?: 'everyone' | 'own'
}

/** Browse all community agent templates. */
export function useCommunityAgents(enabled: boolean = true) {
  return useQuery<CommunityAgentsResponse>({
    queryKey: ['community-agents'],
    queryFn: () => apiFetch('/v1/community/agents').then(r => r.json()),
    enabled,
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  })
}

/**
 * Dry-run an install — shows the cascade preview (which MCPs are ready,
 * which need admin work) + slug availability. Used by AgentInstallModal
 * before the user commits.
 */
export function useInstallPreview(
  templateSlug: string | null,
  targetSlug: string | null,
) {
  return useQuery<InstallPreview>({
    queryKey: ['community-agent-preview', templateSlug, targetSlug],
    queryFn: () => {
      const params = new URLSearchParams()
      if (targetSlug) params.set('target_slug', targetSlug)
      const qs = params.toString()
      return apiFetch(
        `/v1/community/agents/${templateSlug}/preview${qs ? `?${qs}` : ''}`,
      ).then(r => r.json())
    },
    enabled: !!templateSlug,
    staleTime: 5_000,
  })
}

/** What updating an installed agent to the catalog's version would do:
 * reads only, computed on request (a POST that writes nothing). */
export function useTemplateUpdatePlan(agentSlug: string | null) {
  return useQuery<TemplateUpdatePlan>({
    queryKey: ['template-update-plan', agentSlug],
    queryFn: () => apiJson<TemplateUpdatePlan>(`/v1/agents/${agentSlug}/update-template/plan`,
      { method: 'POST' }, 'Could not read the update plan'),
    enabled: !!agentSlug,
    staleTime: 0,
    retry: false,
  })
}

export function useApplyTemplateUpdate() {
  return useMutation<
    { job_id: string; to_version: string },
    Error,
    { agent_slug: string; from_version: string; approve_apps?: Record<string, string>; approve_checks?: Record<string, string> }
  >({
    mutationFn: ({ agent_slug, ...body }) =>
      apiJson<{ job_id: string; to_version: string }>(`/v1/agents/${agent_slug}/update-template`, {
        method: 'POST',
        json: body,
      }, 'Could not start the update'),
  })
}

/** Polled while an update runs; the report lands here and by notification. */
export function useTemplateUpdateStatus(agentSlug: string | null, enabled: boolean) {
  return useQuery<TemplateUpdateStatus>({
    queryKey: ['template-update-status', agentSlug],
    queryFn: () => apiJson<TemplateUpdateStatus>(`/v1/agents/${agentSlug}/update-template/status`, {},
      'Could not read the update'),
    enabled: enabled && !!agentSlug,
    refetchInterval: q => (q.state.data?.running ? 2000 : false),
  })
}

export function useTakeNewVersion() {
  const qc = useQueryClient()
  return useMutation<{ status: string; path: string }, Error, { agent_slug: string; new_path: string }>({
    mutationFn: ({ agent_slug, new_path }) =>
      apiJson<{ status: string; path: string }>(`/v1/agents/${agent_slug}/update-template/take`, {
        method: 'POST',
        json: { new_path },
      }, 'Could not take the new version'),
    onSuccess: (_r, v) => {
      qc.invalidateQueries({ queryKey: ['agent-files', v.agent_slug] })
    },
  })
}

export interface InstallFromCommunityResult {
  agent_slug: string
  batch_id: string | null
  created_requests: {
    id: number
    mcp_name: string
    agent_slug: string
    status: string
    batch_id: string
  }[]
  ready_mcps: string[]
  seeded_tasks: number
  seeded_triggers: number
  seeded_notifications: number
  copied_context: number
  setup_md_copied: boolean
  agent: Record<string, unknown>
  task_pins_dropped?: string[]
}

/**
 * Install a community agent template. Returns the install envelope —
 * including any batch_id of requests queued for admin approval.
 *
 * Slug collisions surface as a 409 with a body of
 * ``{error, suggested_slug, message}``; the calling component handles the
 * auto-suffix retry loop.
 */
export function useInstallCommunityAgent() {
  const qc = useQueryClient()
  const { refreshUser } = useAuth()
  return useMutation<
    InstallFromCommunityResult,
    Error,
    {
      template_slug: string
      target_slug?: string
      display_name?: string
      manager_user?: string
      /** The dialog's consent: the signature seen per app / check approved. */
      approve_apps?: Record<string, string>
      approve_checks?: Record<string, string>
    }
  >({
    mutationFn: body =>
      apiResponse('/v1/agents/install-from-community', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }, 'Could not install the agent').then(async r => {
        // The ApiError keeps the status and the parsed body: AgentInstallModal
        // reads a 409's suggested slug from them.
        if (!r.ok) await raiseApiError(r, 'Could not install the agent')
        return r.json()
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['community-agents'] })
      qc.invalidateQueries({ queryKey: ['agents'] })
      qc.invalidateQueries({ queryKey: ['admin-mcp-requests'] })
      // Installing a template assigns the installer as the new agent's
      // manager server-side — refresh the auth snapshot so
      // `user.agent_roles`-driven views (Remote Machines settings tab,
      // role gates) show the new agent without a page reload. Mirrors
      // `useCreateAgent` in `./agents.ts`.
      void refreshUser()
    },
  })
}
