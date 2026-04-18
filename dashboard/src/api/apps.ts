import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch } from './auth'

export interface AppArgsSchema {
  properties?: Record<string, { type?: string; description?: string; enum?: unknown[] }>
  required?: string[]
}

export interface AppAction {
  id: string
  label: string
  type: 'fire_task' | 'send_prompt' | 'mcp_tool' | 'data_feed'
  task_id?: string
  task_name?: string
  prompt?: string
  mcp?: string
  tool?: string
  fixed_args?: Record<string, unknown>
  args_schema?: AppArgsSchema
  /** mcp_tool only: whether the target MCP is currently assigned+enabled. */
  mcp_available?: boolean
  /** data_feed only: the read-only platform feed the page may subscribe to
      (otodock.feed) — answered by the host page, viewer-scoped. */
  feed?: string
}

export interface PinnedApp {
  id: string
  slug: string
  title: string
  scope: 'shared' | 'personal'
  /** Where the pin lives: the standing apps strip, or a chat/project Dock. */
  pin_scope?: 'standing' | 'chat' | 'project'
  /** Set on Dock pin rows (a project pin may come from another agent). */
  agent?: string
  position: number
  rel_path: string
  updated_at: string
  actions: AppAction[]
  actions_sig: string
  actions_approved: boolean
  approval_stale: boolean
  can_approve: boolean
  can_manage: boolean
}

/** A Dock FILE pin — a reference only: the Dock reads the content through
 * the files API, where the viewer's own role decides what renders. */
export interface PinnedFileRef {
  id: string
  agent: string
  rel_path: string
  title: string
  pin_scope: 'chat' | 'project'
  updated_at: string
}

/** The chat's Dock pins: its own chat-scoped app, (project chats) the
 * project-scoped one, and the scope's FILE pins. App rows are shaped exactly
 * like /v1/apps rows, so AppFrame + the approval card work unchanged. */
export interface ChatPins {
  chat: PinnedApp | null
  project: PinnedApp | null
  files?: PinnedFileRef[]
}

export const useChatPins = (chatId: string | undefined) =>
  useQuery({
    queryKey: ['chat-pins', chatId],
    queryFn: async (): Promise<ChatPins> => {
      const res = await apiFetch(`/v1/chats/${chatId}/pins`)
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    enabled: !!chatId,
    staleTime: 15_000,
  })

export const useApps = (agent: string) =>
  useQuery({
    queryKey: ['apps', agent],
    queryFn: async (): Promise<PinnedApp[]> => {
      const res = await apiFetch(`/v1/apps?agent=${encodeURIComponent(agent)}`)
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      return data.apps ?? []
    },
    enabled: !!agent,
    staleTime: 30_000,
  })

export const useApproveApp = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ appId, sig }: { appId: string; sig: string }) => {
      const res = await apiFetch(`/v1/apps/${appId}/approve`, {
        method: 'POST',
        body: JSON.stringify({ sig }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Approve failed')
    },
    // 409 (manifest changed) also lands here — the refetch shows the new card.
    // Dock pins render off ['chat-pins'] — same approval endpoint, so both
    // caches refresh (the extra invalidate is a no-op without mounted pins).
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['apps', agent] })
      qc.invalidateQueries({ queryKey: ['chat-pins'] })
    },
  })
}

export const useUnpinApp = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (appId: string) => {
      const res = await apiFetch(`/v1/apps/${appId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Unpin failed')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['apps', agent] }),
  })
}

export const useReorderApps = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (ids: string[]) => {
      const res = await apiFetch('/v1/apps/order', {
        method: 'PUT',
        body: JSON.stringify({ agent, ids }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Reorder failed')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['apps', agent] }),
  })
}

export interface AppActionResult {
  status: string
  reason?: string
  result?: string
  run_id?: string
}

/** Execute a declared fire_task or mcp_tool action over REST (send_prompt
 * rides the chat WS). fire_task acks `sent`; mcp_tool resolves to `done`
 * with the tool's result text (or `error`). Args pass the server's
 * user-approved schema gate — never trusted client-side. */
export async function fireAppAction(appId: string, actionId: string, args?: unknown): Promise<AppActionResult> {
  const res = await apiFetch(`/v1/apps/${appId}/actions/${actionId}`, {
    method: 'POST',
    body: JSON.stringify({ args: args ?? null }),
  })
  if (res.ok) {
    const body = await res.json().catch(() => null)
    if (body && typeof body.status === 'string') {
      return body.status === 'ok' ? { ...body, status: 'sent' } : body
    }
    return { status: 'sent' }
  }
  const detail = (await res.json().catch(() => null))?.detail || `HTTP ${res.status}`
  return { status: 'denied', reason: String(detail) }
}
