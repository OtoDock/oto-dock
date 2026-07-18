/**
 * Community agents catalog hooks.
 *
 * Mirrors `./community.ts` (which serves the MCPs catalog) — same React
 * Query patterns, just pointed at the new `/v1/community/agents*`
 * endpoints. Used by `CommunityAgentsBrowser` + `AgentInstallModal`.
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import { useAuth } from '../contexts/AuthContext'

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
  platform_min_version: string | null
  deprecated: boolean
  deprecation_note: string | null
  // Augmented by the proxy:
  installed_as: string[]
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
    { template_slug: string; target_slug?: string; manager_user?: string }
  >({
    mutationFn: body =>
      apiFetch('/v1/agents/install-from-community', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(async r => {
        if (!r.ok) {
          // Read the body once, then try to parse it as JSON (a Response
          // body can only be consumed once — calling .json() then .text()
          // on the same response throws "body already read").
          const raw = await r.text()
          let detail: any = raw
          try {
            detail = JSON.parse(raw)
          } catch {
            /* non-JSON body — keep the raw text */
          }
          const err = new Error(typeof detail === 'string' ? detail : (detail?.detail?.message || detail?.message || `HTTP ${r.status}`))
          ;(err as any).status = r.status
          ;(err as any).body = detail
          throw err
        }
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
