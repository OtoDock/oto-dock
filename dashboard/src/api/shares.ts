import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch } from './auth'

// Shares of apps and chats (SHARING.md): internal grants to platform users
// and, later, external links. Every write here is human-only on the server.

export interface ShareGrantee {
  sub: string
  name: string
  username: string
}

export interface Share {
  id: string
  target_kind: 'app' | 'chat'
  target_id: string
  scope: 'internal' | 'external'
  state: 'active' | 'suspended'
  grantee: ShareGrantee | null
  hidden_by_grantee: boolean
  public: boolean
  allow_actions: boolean
  created_by: string
  created_by_name: string
  created_at: string
  expires_at: string | null
  last_access_at: string | null
  access_count: number
}

/** A share granted TO the caller ("Shared with me"), with the page that opens it. */
export interface MyShare {
  id: string
  target_kind: 'app' | 'chat'
  target_id: string
  title: string
  agent: string
  shared_by: string
  shared_by_name: string
  created_at: string
  expires_at: string | null
  hidden: boolean
  href: string
}

export interface DirectoryUser {
  sub: string
  name: string
  username: string
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

export const useMyShares = (enabled = true) =>
  useQuery({
    queryKey: ['shares', 'mine'],
    queryFn: async (): Promise<MyShare[]> => {
      const res = await apiFetch('/v1/shares/mine')
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load shares'))
      return (await res.json()).shares ?? []
    },
    enabled,
    staleTime: 30_000,
  })

/** The people a share can go to; `null` when an admin closed the directory
 * to members (then the form takes an exact username or email). */
export const useUserDirectory = (enabled = true) =>
  useQuery({
    queryKey: ['users', 'directory'],
    queryFn: async (): Promise<DirectoryUser[] | null> => {
      const res = await apiFetch('/v1/users/directory')
      if (res.status === 404) return null
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load the directory'))
      return (await res.json()).users ?? []
    },
    enabled,
    staleTime: 60_000,
  })

/** The admin's sharing settings a share form needs: the longest expiry in
 * days, `null` when nothing caps it (then a link may never expire). */
export interface SharingSettings {
  max_expiry_days: number | null
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
  grantee?: string
  /** "" = no expiry (links default to 30 days), "never" = no expiry on a
   * link too, "<n>d" = n days, or an ISO instant. An admin cap clamps ""
   * on a person and refuses "never". */
  expires_in?: string
  /** External links. */
  public?: boolean
  allow_actions?: boolean
  link_password?: string
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
    },
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

/** Every external link on the platform (admins). */
export interface AdminShare extends Share {
  title: string
  agent: string
}

export const useAdminShares = (enabled = true) =>
  useQuery({
    queryKey: ['shares', 'admin'],
    queryFn: async (): Promise<AdminShare[]> => {
      const res = await apiFetch('/v1/admin/shares')
      if (!res.ok) throw new Error(await detailOf(res, 'Could not load the links'))
      return (await res.json()).shares ?? []
    },
    enabled,
    staleTime: 15_000,
  })
