import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch } from './auth'
import type { AgentRole } from '../lib/permissions'

// Shares of apps and chats (SHARING.md): a share names a person, an agent or
// a department and carries a role cap; a person's app share waits in their
// "Shared with you" section until they accept it into one of their agents;
// external links are for people without an account. Every write here is
// human-only on the server.

/** Who an internal share names. */
export const GRANTEE_KIND = { PERSON: 'person', AGENT: 'agent', DEPARTMENT: 'department' } as const
export type GranteeKind = (typeof GRANTEE_KIND)[keyof typeof GRANTEE_KIND]
/** A person's answer to an app share; a chat share is accepted when made,
 * a link carries ''. */
export const SHARE_DECISION = { PENDING: 'pending', ACCEPTED: 'accepted', DECLINED: 'declined' } as const
/** A share to people, agents and departments, or a link. */
export const SHARE_SCOPE = { INTERNAL: 'internal', EXTERNAL: 'external' } as const
export type ShareScope = (typeof SHARE_SCOPE)[keyof typeof SHARE_SCOPE]
/** What a share opens. */
export const SHARE_TARGET = { APP: 'app', CHAT: 'chat' } as const
export type ShareTarget = (typeof SHARE_TARGET)[keyof typeof SHARE_TARGET]

export interface ShareGrantee {
  sub: string
  name: string
  username: string
}

export interface Share {
  id: string
  target_kind: ShareTarget
  target_id: string
  scope: ShareScope
  state: 'active' | 'suspended'
  /** '' on a link. */
  grantee_kind: GranteeKind | ''
  grantee: ShareGrantee | null
  to_agent: { slug: string; name: string } | null
  to_department: { id: string; name: string } | null
  role_cap: AgentRole
  /** 'pending' | 'accepted' | 'declined'; '' on a link. */
  decision: string
  decided_by: string
  decided_by_name: string
  placed_agent: string
  hidden_by_grantee: boolean
  public: boolean
  allow_actions: boolean
  created_by: string
  created_by_name: string
  created_at: string
  expires_at: string | null
  last_access_at: string | null
  access_count: number
  /** On a create answer: the share placed the app at once (an agent or a
   * department share), as opposed to waiting for a person's decision. */
  landed?: boolean
}

export interface DirectoryUser {
  sub: string
  name: string
  username: string
}

export interface DirectoryAgent {
  slug: string
  display_name: string
  color: string
}

export interface DirectoryDepartment {
  id: string
  name: string
}

/** Who a share can go to. `users` is null while an admin closed the
 * directory to members (the form then takes an exact username or email);
 * `agents` is every agent while it is open, the viewer's own while it is
 * closed, empty with the switch off; `departments` for an admin. */
export interface ShareDirectory {
  users: DirectoryUser[] | null
  agents: DirectoryAgent[]
  departments: DirectoryDepartment[]
}

const detailOf = async (res: Response, fallback: string) =>
  (await res.json().catch(() => null))?.detail || fallback

export const useShares = (targetKind: 'app' | 'chat', targetId: string | undefined) =>
  useQuery({
    queryKey: ['shares', targetKind, targetId],
    queryFn: async (): Promise<Share[]> => {
      const res = await apiFetch(`/v1/shares?target_kind=${targetKind}&target=${encodeURIComponent(targetId ?? '')}`)
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load shares'))
      return (await res.json()).shares ?? []
    },
    enabled: !!targetId,
    staleTime: 10_000,
  })

export const useUserDirectory = (enabled = true) =>
  useQuery({
    queryKey: ['users', 'directory'],
    queryFn: async (): Promise<ShareDirectory> => {
      const res = await apiFetch('/v1/users/directory')
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load the directory'))
      const body = await res.json()
      return { users: body.users ?? null, agents: body.agents ?? [], departments: body.departments ?? [] }
    },
    enabled,
    staleTime: 60_000,
  })

/** The admin's sharing settings a share form needs: the longest expiry in
 * days (`null` when nothing caps it, then a link may never expire), the two
 * target switches and whether the directory is open to the viewer. */
export interface SharingSettings {
  max_expiry_days: number | null
  sharing_to_agents_enabled: boolean
  sharing_to_departments_enabled: boolean
  directory_open: boolean
}

export const useSharingSettings = (enabled = true) =>
  useQuery({
    queryKey: ['sharing', 'settings'],
    queryFn: async (): Promise<SharingSettings> => {
      const res = await apiFetch('/v1/sharing/settings')
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load the sharing settings'))
      return res.json()
    },
    enabled,
    staleTime: 60_000,
  })

export interface CreateShareInput {
  target_kind: 'app' | 'chat'
  target_id: string
  scope?: 'internal' | 'external'
  /** Internal: who receives it (a person by default). */
  grantee_kind?: GranteeKind
  /** A person's sub, username or email; an agent's slug; a department's id. */
  grantee?: string
  /** The role the recipient acts with on an app (viewer when omitted). */
  role_cap?: AgentRole
  /** "" = no expiry (a link: the server's 30 days), "never" = no expiry,
   * "<n>d" = n days, or an ISO instant. Under an admin cap "" on an
   * internal share (a person, an agent, a department) becomes the cap's
   * length, and "never" or a longer choice is refused. */
  expires_in?: string
  /** External links. */
  public?: boolean
  allow_actions?: boolean
  link_password?: string
  /** Chats: keep the tool-call blocks in the snapshot. */
  include_tools?: boolean
  /** The confirm: the account password, or a passkey or provider confirm token. */
  password?: string
  confirm_token?: string
}

export interface CreateShareResult {
  status: string
  share?: Share
  /** External links: the URL, shown once. */
  link?: string
  /** External password links: the generated password, shown once. */
  password?: string
}

/** The server asks for a confirm (428): which method it takes. `oidc` is a
 * round trip to the identity provider, named by `provider`. */
export type ConfirmMethod = 'password' | 'passkey' | 'oidc' | 'none'
export class ConfirmRequired extends Error {
  method: ConfirmMethod
  provider?: string
  constructor(method: ConfirmMethod, message: string, provider?: string) {
    super(message)
    this.method = method
    this.provider = provider
  }
}

async function raiseFor(res: Response, fallback: string): Promise<never> {
  const body = await res.json().catch(() => null) as { detail?: unknown } | null
  const detail = body?.detail
  if (res.status === 428 && detail && typeof detail === 'object') {
    const d = detail as { method?: string; message?: string; provider?: string }
    const method: ConfirmMethod = d.method === 'password' || d.method === 'passkey' || d.method === 'oidc' ? d.method : 'none'
    throw new ConfirmRequired(method, d.message || 'Confirmation required', d.provider)
  }
  throw new Error(typeof detail === 'string' ? detail : fallback)
}

export const useCreateShare = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (input: CreateShareInput): Promise<CreateShareResult> => {
      const res = await apiFetch('/v1/shares', { method: 'POST', body: JSON.stringify(input) })
      if (!res.ok) await raiseFor(res, 'Could not share')
      return res.json()
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['shares'] })
      qc.invalidateQueries({ queryKey: ['apps'] })
    },
  })
}

export interface PatchShareInput {
  id: string
  revoke?: boolean
  resume?: boolean
  expires_in?: string
  /** External links: the Buttons switch (turning it on confirms) and a new password. */
  allow_actions?: boolean
  link_password?: string
  password?: string
  confirm_token?: string
}

export const usePatchShare = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, ...body }: PatchShareInput): Promise<Share> => {
      const res = await apiFetch(`/v1/shares/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
      if (!res.ok) await raiseFor(res, 'Could not change the share')
      return res.json()
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['shares'] })
      qc.invalidateQueries({ queryKey: ['apps'] })
      qc.invalidateQueries({ queryKey: ['app'] })
    },
  })
}

/** One row of the viewer's "Shared with you" section. `actions` is what the
 * item offers: `accept` and `decline` while an app waits for the decision,
 * `open` and `hide` for a received app or a chat; the section renders by
 * it. A pending app still opens at `href`. */
export interface ShareInboxItem {
  id: string
  kind: 'app' | 'chat'
  target_id: string
  title: string
  /** The target's own agent (the app's, the chat's). */
  agent: string
  agent_name: string
  agent_color: string
  shared_by: string
  shared_by_name: string
  created_at: string
  expires_at: string | null
  role_cap: AgentRole
  href: string
  /** The agent the viewer accepted the app into, while they still hold it. */
  placed_agent: string
  actions: string[]
}

export interface ShareInbox {
  items: ShareInboxItem[]
  /** Decisions waiting on the viewer (apps only). */
  pending: number
}

export const INBOX_KEY = ['shares', 'inbox'] as const

export const useShareInbox = (enabled = true) =>
  useQuery({
    queryKey: INBOX_KEY,
    queryFn: async (): Promise<ShareInbox> => {
      const res = await apiFetch('/v1/shares/inbox')
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load what was shared with you'))
      const body = await res.json()
      return { items: body.items ?? [], pending: body.pending ?? 0 }
    },
    enabled,
    staleTime: 15_000,
  })

const settleShare = (qc: ReturnType<typeof useQueryClient>, agent?: string) => {
  qc.invalidateQueries({ queryKey: ['shares'] })
  qc.invalidateQueries({ queryKey: agent ? ['apps', agent] : ['apps'] })
  qc.invalidateQueries({ queryKey: ['app'] })
}

async function postShare(id: string, verb: string, body: Record<string, unknown>, fallback: string) {
  const res = await apiFetch(`/v1/shares/${id}/${verb}`, { method: 'POST', body: JSON.stringify(body) })
  if (!res.ok) await raiseFor(res, fallback)
  return res.json()
}

/** Accept an app into one of the viewer's agents (the server refuses one
 * they do not hold, and the app's own agent). */
export const useAcceptShare = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, agent }: { id: string; agent: string }) =>
      postShare(id, 'accept', { agent }, 'Could not accept the share'),
    onSettled: (_r, _e, v) => settleShare(qc, v.agent),
  })
}

export const useDeclineShare = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id }: { id: string }) => postShare(id, 'decline', {}, 'Could not decline the share'),
    onSettled: () => settleShare(qc),
  })
}

/** Hide a received share for the viewer alone: their own share leaves the
 * section (and an app its panel); a placed app leaves the panel of `agent`. */
export const useHideShare = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, agent }: { id: string; agent?: string }) =>
      postShare(id, 'hide', { agent: agent ?? '' }, 'Could not hide the share'),
    onSettled: (_r, _e, v) => settleShare(qc, v.agent),
  })
}

export const useUnhideShare = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, agent }: { id: string; agent?: string }) =>
      postShare(id, 'unhide', { agent: agent ?? '' }, 'Could not restore the share'),
    onSettled: (_r, _e, v) => settleShare(qc, v.agent),
  })
}

/** A chat share's snapshot (SHARING.md "Chat shares"): the messages as they
 * stood when it was shared, with the copies the page renders through
 * `/v1/shares/<id>/ui/<token>` and `/media/<token>`. */
export interface SnapshotMessage {
  role: 'user' | 'assistant' | 'event'
  content?: string
  created_at: string
  author_sub?: string
  event_type?: string
  data?: Record<string, unknown>
  event_data?: string
}

export interface ChatSnapshot {
  id: string
  title: string
  agent: string
  created_at: string
  shared_by_name: string
  include_tools: boolean
  messages: SnapshotMessage[]
}

export const useChatSnapshot = (shareId: string | undefined) =>
  useQuery({
    queryKey: ['shares', 'snapshot', shareId],
    queryFn: async (): Promise<ChatSnapshot | null> => {
      const res = await apiFetch(`/v1/shares/${shareId}/snapshot`)
      if (res.status === 404) return null
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load the shared chat'))
      return res.json()
    },
    enabled: !!shareId,
    staleTime: Infinity,
  })

/** A share's one word on the admin list, the first that holds: declined,
 * expired, paused (the app is unpinned), waiting (an app share a person has
 * not decided), else live (`share_store.ADMIN_STANDINGS`). */
export const SHARE_STANDING = {
  LIVE: 'live', WAITING: 'waiting', PAUSED: 'paused', EXPIRED: 'expired', DECLINED: 'declined',
} as const
export type ShareStanding = (typeof SHARE_STANDING)[keyof typeof SHARE_STANDING]
/** The admin list's `kind` query: both scopes, or one. */
export const ADMIN_SHARE_KIND = { ALL: 'all', ...SHARE_SCOPE } as const
export type AdminShareKind = (typeof ADMIN_SHARE_KIND)[keyof typeof ADMIN_SHARE_KIND]

/** One row of Admin → Shares: the share shape plus the target's title, its
 * own agent and the standing. */
export interface AdminShare extends Share {
  title: string
  agent: string
  standing: ShareStanding
}

export interface AdminShareList {
  shares: AdminShare[]
  /** The kinds cut at the newest 500 rows. */
  truncated: { internal?: boolean; external?: boolean }
}

export interface AdminShareFilters {
  kind?: AdminShareKind
  /** The app's or chat's own agent, or the agent a share places it in. */
  agent?: string
  standing?: ShareStanding | ''
}

/** Every share on the platform (admins): the shares to people, agents and
 * departments and the external links, the newest 500 of each kind asked,
 * filtered by the server. A filter change keeps the last list on screen
 * while the next one loads. */
export const useAdminShares = ({ kind = ADMIN_SHARE_KIND.ALL, agent = '', standing = '' }: AdminShareFilters = {}, enabled = true) =>
  useQuery({
    queryKey: ['shares', 'admin', kind, agent, standing],
    queryFn: async (): Promise<AdminShareList> => {
      const q = new URLSearchParams({ kind })
      if (agent) q.set('agent', agent)
      if (standing) q.set('standing', standing)
      const res = await apiFetch(`/v1/admin/shares?${q}`)
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load the shares'))
      const body = await res.json()
      return { shares: body.shares ?? [], truncated: body.truncated ?? {} }
    },
    enabled,
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
