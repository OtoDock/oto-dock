import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface Chat {
  id: string
  user_sub: string
  agent: string
  title: string
  session_id: string | null
  permission_mode: string
  execution_path: string
  source_type?: 'chat' | 'phone' | 'task'
  // otodock-CLI: how the chat was started — 'dashboard' (default) or 'otodock'
  // (an `otodock` CLI session on the remote machine). Surfaced as a badge.
  origin?: string
  // the absolute remote folder an otodock session ran in (for resume).
  work_cwd?: string
  total_cost?: number
  created_at: string
  updated_at: string
  // Server-computed: a response landed after this viewer's read marker
  // (shared-only chats clear once ANY user opens them). The live WS flip in
  // chatStore wins between refetches — see ChatHistory's ChatRow.
  unread?: boolean
  // When the last assistant response landed — drives the unread row tint's age fade.
  last_response_at?: string | null
  // Delegation lineage (delegate surface=chat): the chat that spawned this
  // worker, the project slug grouping related lanes, and this chat's role in
  // it ('' | 'orchestrator' | 'worker'). Drive the sidebar linkage accents.
  parent_chat_id?: string | null
  project_id?: string | null
  delegate_role?: string
  // Server-computed mutation flags (listing AND search endpoints) — the
  // kebab renders Rename/Delete from these; the role matrix lives entirely
  // backend-side. Optional: absent on stale caches from an older proxy.
  can_rename?: boolean
  can_delete?: boolean
}

export interface ChatMessage {
  id: number
  chat_id: string
  role: 'user' | 'assistant' | 'event'
  content: string
  event_type: string
  event_data: string
  created_at: string
}

export function useChats(agent?: string, pollPaused = false) {
  return useQuery({
    queryKey: ['chats', agent],
    queryFn: async () => {
      const params = agent ? `?agent=${agent}` : ''
      const res = await apiFetch(`/v1/chats${params}`)
      if (!res.ok) throw new Error('Failed to fetch chats')
      const data = await res.json()
      return data.chats as Chat[]
    },
    // Paused while an inline rename is being typed: a poll-driven reorder
    // moves the row's DOM node, which blurs the input and would silently
    // discard the user's in-progress edit (blur cancels).
    refetchInterval: pollPaused ? false : 30000,
  })
}

// One row of GET /v1/chats?kind=tasks — a task-run chat joined with its
// latest run. `unread` is always false server-side (tasks carry no unread
// state anywhere — notifications cover completion).
export interface TaskChat extends Chat {
  run_id?: string
  run_status?: string
  run_task_type?: string | null
  task_name?: string
  // The dynamic task behind the latest run — the rename target for rows
  // labeled by task_name ("rename what you see": PATCH /v1/tasks/{task_id}).
  task_id?: string | null
}

/** The sidebar's task mode: the agent's task-run chats, newest first,
 * permission-shaped like the task history was (agent-scoped runs for anyone
 * with agent access, user-scoped runs only for their creator). */
export function useTaskChats(agent?: string, enabled = true, pollPaused = false) {
  return useQuery({
    queryKey: ['task-chats', agent],
    queryFn: async () => {
      const res = await apiFetch(`/v1/chats?agent=${agent}&kind=tasks`)
      if (!res.ok) throw new Error('Failed to fetch task chats')
      const data = await res.json()
      return data.chats as TaskChat[]
    },
    enabled: !!agent && enabled,
    // Paused while an inline rename is being typed (see useChats).
    refetchInterval: pollPaused ? false : 30000,
  })
}

// One row of GET /v1/chats/active — the cross-agent "Active now" widget seed.
// The server composes the streaming set (pump + interactive turns) and
// filters each row with the same rule that guards opening the chat, so
// whatever arrives here is safe to show this user.
export interface ActiveChat {
  id: string
  agent: string
  title: string
  // 'streaming' (open turn right now) or 'finished' (recent finished-unread
  // backfill — the widget keeps it until someone opens the chat).
  status: string
  source_type?: string
  owner_is_shared?: boolean
  last_response_at?: string | null
  unread?: boolean
}

export async function fetchActiveChats(): Promise<ActiveChat[]> {
  const res = await apiFetch('/v1/chats/active')
  if (!res.ok) throw new Error('Failed to fetch active chats')
  const data = await res.json()
  return data.chats as ActiveChat[]
}

/** FTS search — follows the sidebar mode: kind 'chats' (default) searches the
 * viewer's chat history, 'tasks' searches the agent's task-run chats under
 * the same run permission rules as the task listing. */
export function useSearchChats(agent: string | undefined, query: string, kind: 'chats' | 'tasks' = 'chats') {
  return useQuery({
    queryKey: ['chat-search', agent, query, kind],
    queryFn: async () => {
      if (!query.trim() || !agent) return null
      const params = new URLSearchParams({ q: query, agent })
      if (kind !== 'chats') params.set('kind', kind)
      const res = await apiFetch(`/v1/chats/search?${params}`)
      if (!res.ok) throw new Error('Search failed')
      const data = await res.json()
      return data.chats as Chat[]
    },
    enabled: !!query.trim() && !!agent,
    staleTime: 5000,
  })
}

/** Imperative fetch of one older page of messages (lazy scroll-back). Returns the
 * older rows (chronological, with id < beforeId) + whether still-older rows remain.
 * Event-driven (called on scroll), so it's a plain function, not a useQuery hook. */
export async function fetchOlderMessages(
  chatId: string, beforeId: number, limit = 50,
): Promise<{ messages: ChatMessage[]; has_more: boolean }> {
  const params = new URLSearchParams({ before_id: String(beforeId), limit: String(limit) })
  const res = await apiFetch(`/v1/chats/${chatId}?${params}`)
  if (!res.ok) throw new Error('Failed to fetch older messages')
  const data = await res.json()
  return { messages: (data.messages ?? []) as ChatMessage[], has_more: !!data.has_more }
}

/** Imperative fetch of the NEWEST page of a chat (+ has_more) — the rich-view
 * toggle's snapshot. Pages like the lazy scroll-back so no path is capped/un-lazy. */
export async function fetchChatPage(
  chatId: string, limit = 50,
): Promise<{ messages: ChatMessage[]; has_more: boolean }> {
  const res = await apiFetch(`/v1/chats/${chatId}?limit=${limit}`)
  if (!res.ok) throw new Error('Failed to fetch chat history')
  const data = await res.json()
  return { messages: (data.messages ?? []) as ChatMessage[], has_more: !!data.has_more }
}

export function useDeleteChat() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (chatId: string) => {
      const res = await apiFetch(`/v1/chats/${chatId}`, { method: 'DELETE' })
      if (!res.ok) {
        const detail = await res.json().then(d => d?.detail).catch(() => '')
        throw new Error(detail || `Failed to delete chat (HTTP ${res.status})`)
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['chats'] })
      queryClient.invalidateQueries({ queryKey: ['task-chats'] })
      queryClient.invalidateQueries({ queryKey: ['active-chats'] })
    },
  })
}

/** Inline rename of a chat / task-run history entry (PATCH /v1/chats).
 * Optimistic: the one row's `title` patches in every list cache; the server
 * echo (sanitized title) settles via invalidation. */
export function useRenameChat() {
  const queryClient = useQueryClient()
  const patchTitle = (chatId: string, title: string) => {
    queryClient.setQueriesData<Chat[]>({ queryKey: ['chats'] }, (old) =>
      old?.map((c) => (c.id === chatId ? { ...c, title } : c)))
    queryClient.setQueriesData<TaskChat[]>({ queryKey: ['task-chats'] }, (old) =>
      old?.map((c) => (c.id === chatId ? { ...c, title } : c)))
  }
  return useMutation({
    mutationFn: async ({ chatId, title }: { chatId: string; title: string }) => {
      const res = await apiFetch(`/v1/chats/${chatId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      })
      if (!res.ok) {
        const detail = await res.json().then(d => d?.detail).catch(() => '')
        throw new Error(detail || `Failed to rename (HTTP ${res.status})`)
      }
      return res.json() as Promise<{ title?: string }>
    },
    onMutate: async ({ chatId, title }) => {
      // Stop in-flight polls from resurrecting the old title mid-patch.
      await queryClient.cancelQueries({ queryKey: ['chats'] })
      await queryClient.cancelQueries({ queryKey: ['task-chats'] })
      patchTitle(chatId, title)
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['chats'] })
      queryClient.invalidateQueries({ queryKey: ['task-chats'] })
      queryClient.invalidateQueries({ queryKey: ['active-chats'] })
    },
  })
}

/** Rename a task DEFINITION from its history row ("rename what you see":
 * name-labeled rows hit PATCH /v1/tasks, so every sibling run row and the
 * Schedules page update together). Optimistic on `task_name` across ALL
 * cached rows sharing the task_id — recurring tasks list one row per run. */
export function useRenameTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ taskId, name }: { taskId: string; name: string }) => {
      const res = await apiFetch(`/v1/tasks/${taskId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      if (!res.ok) {
        const detail = await res.json().then(d => d?.detail).catch(() => '')
        throw new Error(detail || `Failed to rename (HTTP ${res.status})`)
      }
    },
    onMutate: async ({ taskId, name }) => {
      await queryClient.cancelQueries({ queryKey: ['task-chats'] })
      queryClient.setQueriesData<TaskChat[]>({ queryKey: ['task-chats'] }, (old) =>
        old?.map((c) => (c.task_id === taskId ? { ...c, task_name: name } : c)))
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['task-chats'] })
      queryClient.invalidateQueries({ queryKey: ['active-chats'] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}

// --- Agent Conversations (Conversations tab) — external sessions (phone /
// future webhook) for any agent the viewer can manage. Dashboard chats are
// excluded server-side and live on the chat-history page. ---

export function useAgentConversations(agent: string, sourceType?: string, offset = 0, limit = 50) {
  return useQuery({
    queryKey: ['agent-conversations', agent, sourceType, offset],
    queryFn: async () => {
      const params = new URLSearchParams({ offset: String(offset), limit: String(limit) })
      if (sourceType) params.set('source_type', sourceType)
      const res = await apiFetch(`/v1/agents/${agent}/conversations?${params}`)
      if (!res.ok) throw new Error('Failed to fetch conversations')
      return res.json() as Promise<{ conversations: Chat[]; total: number }>
    },
    enabled: !!agent,
  })
}

export function useChatDetail(chatId: string | null) {
  return useQuery({
    queryKey: ['chat-detail', chatId],
    queryFn: async () => {
      if (!chatId) return null
      const res = await apiFetch(`/v1/chats/${chatId}/detail`)
      if (res.status === 403) throw new ForbiddenError()
      if (!res.ok) throw new Error('Failed to fetch chat')
      return res.json() as Promise<Chat>
    },
    enabled: !!chatId,
  })
}

export class ForbiddenError extends Error {
  constructor() { super('Forbidden') }
}
