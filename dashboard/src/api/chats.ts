import { useCallback, useMemo, useSyncExternalStore } from 'react'
import { useQuery, useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { apiJson, apiOk, withMessage, withFallback, ApiError } from '../lib/http/apiJson'
import { type SourceType } from '../lib/session/kind'
import type { ActiveRowPhase } from '../lib/status/chat'
import { forgetDock } from '../store/artifactDockStore'

export interface Chat {
  id: string
  user_sub: string
  agent: string
  title: string
  // The title came from the title model or a person's rename (never the
  // stamped first words): a task row leads with it then.
  title_generated?: boolean
  session_id: string | null
  permission_mode: string
  execution_path: string
  source_type?: SourceType
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
  // Sharing as a snapshot (SHARING.md): the owner of a per-user chat,
  // editor and above on a shared-only agent's chat, never a task run.
  can_share?: boolean
  // Search rows only: the newest message holding every word of the query
  // (the result opens the chat there) and its words around the match, the
  // matched ones between SNIPPET_MARK_START and SNIPPET_MARK_END.
  match_message_id?: number | null
  match_snippet?: string
}

export const SNIPPET_MARK_START = '\u0002'
export const SNIPPET_MARK_END = '\u0003'

export interface ChatMessage {
  id: number
  chat_id: string
  role: 'user' | 'assistant' | 'event'
  content: string
  event_type: string
  event_data: string
  created_at: string
}

/** A sidebar list as the cache keeps it: every row loaded so far, newest
 * first, and whether older rows remain. GET /v1/chats pages by a cursor, the
 * last shown row's `updated_at` and id. */
export interface ChatList<T extends Chat = Chat> {
  chats: T[]
  has_more: boolean
}

type ListKind = 'chats' | 'tasks'

// Each list's cache key, and its `kind` on the route (none: the chat list).
const LIST_CACHE: Record<ListKind, string> = { chats: 'chats', tasks: 'task-chats' }
const LIST_ROUTE_KIND: Record<ListKind, string | null> = { chats: null, tasks: 'tasks' }

const listKey = (kind: ListKind, agent?: string) => [LIST_CACHE[kind], agent] as const

const LIST_FAILED: Record<ListKind, string> = {
  chats: 'Failed to fetch chats',
  tasks: 'Failed to fetch task chats',
}

async function fetchListPage<T extends Chat>(
  kind: ListKind, agent: string | undefined, after?: Chat,
): Promise<ChatList<T>> {
  // URLSearchParams, not a template: the cursor's `+00:00` must reach the
  // server as a `+`, which a raw query string decodes to a space.
  const params = new URLSearchParams()
  if (agent) params.set('agent', agent)
  const routeKind = LIST_ROUTE_KIND[kind]
  if (routeKind) params.set('kind', routeKind)
  if (after) {
    params.set('before_updated_at', after.updated_at)
    params.set('before_chat_id', after.id)
  }
  const qs = params.toString()
  const data = await apiJson<{ chats: T[]; has_more?: boolean }>(`/v1/chats${qs ? `?${qs}` : ''}`)
    .catch(withMessage(LIST_FAILED[kind]))
  return { chats: data.chats, has_more: !!data.has_more }
}

/** The first page fetched again, merged over the rows already loaded, by
 * position (never by comparing keys). When the fresh page's last row is
 * loaded with the same `updated_at`, it has not moved, so the loaded rows
 * after it are still the list's continuation: the list becomes the fresh
 * page and those rows, minus any the fresh page holds (a chat that moved to
 * the top appears once; one deleted above that row goes; the rows below stay
 * where they were). Otherwise (nothing loaded, the fresh page is the whole
 * list, or more than a page changed while the tab was hidden) the fresh page
 * is the list: a gap between it and the old rows could never be filled. */
export function mergeFirstPage<T extends Chat>(
  fresh: ChatList<T>, loaded: ChatList<T> | undefined,
): ChatList<T> {
  const last = fresh.chats[fresh.chats.length - 1]
  if (!loaded || !fresh.has_more || !last) return fresh
  const at = loaded.chats.findIndex((c) => c.id === last.id)
  if (at < 0 || loaded.chats[at].updated_at !== last.updated_at) return fresh
  const inFresh = new Set(fresh.chats.map((c) => c.id))
  const tail = loaded.chats.slice(at + 1).filter((c) => !inFresh.has(c.id))
  return { chats: [...fresh.chats, ...tail], has_more: loaded.has_more }
}

// One older page in flight per list, and its state, shared by every
// component that shows or pages that list.
interface PagingState { loading: boolean; failed: boolean }
const IDLE: PagingState = { loading: false, failed: false }
const pagingStates = new Map<string, PagingState>()
const pagingListeners = new Set<() => void>()
const inFlight = new Map<string, Promise<void>>()

function setPaging(hash: string, next: PagingState) {
  pagingStates.set(hash, next)
  pagingListeners.forEach((l) => l())
}

function subscribePaging(listener: () => void) {
  pagingListeners.add(listener)
  return () => { pagingListeners.delete(listener) }
}

/** Load the page after the last loaded row and append it. The page lands
 * only if the list still ends at the row it was asked after: a first-page
 * merge that replaced the list meanwhile drops it, and the sentinel asks
 * again from the new end. A failure keeps the list and marks it failed. */
export function loadOlderChats(qc: QueryClient, kind: ListKind, agent?: string): Promise<void> {
  const key = listKey(kind, agent)
  const hash = JSON.stringify(key)
  const running = inFlight.get(hash)
  if (running) return running
  const loaded = qc.getQueryData<ChatList>(key)
  const last = loaded?.chats[loaded.chats.length - 1]
  if (!loaded?.has_more || !last) return Promise.resolve()
  const job = (async () => {
    setPaging(hash, { loading: true, failed: false })
    try {
      const page = await fetchListPage(kind, agent, last)
      qc.setQueryData<ChatList>(key, (cur) => {
        const end = cur?.chats[cur.chats.length - 1]
        if (!cur || end?.id !== last.id || end.updated_at !== last.updated_at) return cur
        const have = new Set(cur.chats.map((c) => c.id))
        return { chats: [...cur.chats, ...page.chats.filter((c) => !have.has(c.id))], has_more: page.has_more }
      })
      setPaging(hash, IDLE)
    } catch {
      setPaging(hash, { loading: false, failed: true })
    } finally {
      inFlight.delete(hash)
    }
  })()
  inFlight.set(hash, job)
  return job
}

/** A sidebar list's paging, for the list that shows it. */
export interface ListPaging {
  hasMore: boolean
  loadingOlder: boolean
  /** The last older page failed: the list offers a press instead of asking
      again by itself. */
  failed: boolean
  loadOlder: () => void
}

export function useChatListPaging(kind: ListKind, agent?: string): ListPaging {
  const qc = useQueryClient()
  const hash = JSON.stringify(listKey(kind, agent))
  const subscribeCache = useCallback((cb: () => void) => qc.getQueryCache().subscribe(cb), [qc])
  const hasMore = useSyncExternalStore(
    subscribeCache, () => !!qc.getQueryData<ChatList>(listKey(kind, agent))?.has_more)
  const state = useSyncExternalStore(subscribePaging, () => pagingStates.get(hash) ?? IDLE)
  const loadOlder = useCallback(() => { void loadOlderChats(qc, kind, agent) }, [qc, kind, agent])
  return useMemo(() => ({ hasMore, loadingOlder: state.loading, failed: state.failed, loadOlder }),
    [hasMore, state, loadOlder])
}

const selectRows = <T extends Chat>(list: ChatList<T>): T[] => list.chats

// The poll fetches the FIRST page and merges it over the loaded rows
// (mergeFirstPage), read from the cache when the request resolves so a page
// appended meanwhile is kept: a person pages down without the poll moving
// them, and a refetch costs one request whatever was loaded. A plain query,
// not an infinite one: an infinite query's refetch asks every loaded page
// again in turn.
function useChatList<T extends Chat>(kind: ListKind, agent: string | undefined,
                                     enabled: boolean, pollPaused: boolean) {
  const qc = useQueryClient()
  return useQuery<ChatList<T>, Error, T[]>({
    queryKey: listKey(kind, agent),
    queryFn: async () => {
      const fresh = await fetchListPage<T>(kind, agent)
      return mergeFirstPage(fresh, qc.getQueryData<ChatList<T>>(listKey(kind, agent)))
    },
    select: selectRows,
    enabled,
    // Paused while an inline rename is being typed: a poll-driven reorder
    // moves the row's DOM node, which blurs the input and would silently
    // discard the user's in-progress edit (blur cancels).
    refetchInterval: pollPaused ? false : 30000,
  })
}

export function useChats(agent?: string, pollPaused = false) {
  return useChatList<Chat>('chats', agent, true, pollPaused)
}

// One row of GET /v1/chats?kind=tasks — a task-run chat joined with its
// latest run. `unread` is always false server-side (tasks carry no unread
// state anywhere — notifications cover completion).
export interface TaskChat extends Chat {
  run_id?: string
  run_status?: string
  run_task_type?: string | null
  task_name?: string
  // The dynamic task behind the latest run.
  task_id?: string | null
}

/** The sidebar's task mode: the agent's task-run chats, newest first,
 * permission-shaped like the task history was (agent-scoped runs for anyone
 * with agent access, user-scoped runs only for their creator). */
export function useTaskChats(agent?: string, enabled = true, pollPaused = false) {
  return useChatList<TaskChat>('tasks', agent, !!agent && enabled, pollPaused)
}

// One row of GET /v1/chats/active — the cross-agent "Active now" widget seed.
// The server composes the streaming set (pump + interactive turns) and
// filters each row with the same rule that guards opening the chat, so
// whatever arrives here is safe to show this user.
export interface ActiveChat {
  id: string
  agent: string
  title: string
  // streaming (an open turn right now), warming (a session registered as
  // warming with no turn yet — metadata for the initiating tab's row) or
  // finished (the recent finished-unread backfill — the widget keeps it
  // until someone opens the chat). The proxy's ws/chat_phase.py.
  status: ActiveRowPhase
  source_type?: SourceType
  owner_is_shared?: boolean
  last_response_at?: string | null
  unread?: boolean
}

export async function fetchActiveChats(): Promise<ActiveChat[]> {
  const data = await apiJson<{ chats: ActiveChat[] }>('/v1/chats/active', {}, 'Failed to fetch active chats')
  return data.chats
}

/** FTS search — follows the sidebar mode: kind 'chats' (default) searches the
 * viewer's chat history, 'tasks' searches the agent's task-run chats under
 * the same run permission rules as the task listing. The newest matches
 * come first (up to 50), every word of the query a prefix; the first rows
 * name their newest matching message (`match_message_id`, `match_snippet`). */
export function useSearchChats(agent: string | undefined, query: string, kind: 'chats' | 'tasks' = 'chats') {
  return useQuery({
    queryKey: ['chat-search', agent, query, kind],
    queryFn: async () => {
      if (!query.trim() || !agent) return null
      const params = new URLSearchParams({ q: query, agent })
      if (kind !== 'chats') params.set('kind', kind)
      const data = await apiJson<{ chats: Chat[] }>(`/v1/chats/search?${params}`, {}, 'Search failed')
      return data.chats
    },
    enabled: !!query.trim() && !!agent,
    staleTime: 5000,
  })
}

// --- resolve-path: the clickable file-path chips in chat markdown ---

/** A resolved chip path — POST /v1/chats/{id}/resolve-path with found:true.
 * `path` is agent-tree-relative; `previewable` mirrors the wopi-url
 * confinement (Collabora doc extension AND workspace/ or users/). */
export interface ResolvedChatPath {
  agent: string
  path: string
  filename: string
  size: number
  previewable: boolean
}

// Positive results only: a resolved path is stable for the chat's lifetime,
// but a miss is not — a satellite may sync the file up moments later — so
// found:false / errors are never cached and the next click re-resolves.
const resolvePathCache = new Map<string, ResolvedChatPath>()

/** Resolve a file-path string from chat markdown to a live file in the chat
 * agent's tree. Returns `null` for found:false (every non-match is a 200
 * `{found: false}` — no oracle); throws only on transport/HTTP errors.
 * Event-driven (called on chip click), so it's a plain function. */
export async function resolveChatPath(
  chatId: string, path: string,
): Promise<ResolvedChatPath | null> {
  const cacheKey = `${chatId}\n${path}`
  const cached = resolvePathCache.get(cacheKey)
  if (cached) return cached
  const data = await apiJson<{ found: boolean } & Omit<ResolvedChatPath, 'previewable'> & { previewable?: boolean }>(
    `/v1/chats/${chatId}/resolve-path`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    },
  ).catch(withMessage(() => 'Could not open that file'))
  if (!data.found) return null
  const resolved: ResolvedChatPath = {
    agent: data.agent,
    path: data.path,
    filename: data.filename,
    size: data.size,
    previewable: !!data.previewable,
  }
  resolvePathCache.set(cacheKey, resolved)
  return resolved
}

/** Imperative fetch of one older page of messages (lazy scroll-back). Returns the
 * older rows (chronological, with id < beforeId) + whether still-older rows remain.
 * Event-driven (called on scroll), so it's a plain function, not a useQuery hook. */
export async function fetchOlderMessages(
  chatId: string, beforeId: number, limit = 50,
): Promise<{ messages: ChatMessage[]; has_more: boolean }> {
  const params = new URLSearchParams({ before_id: String(beforeId), limit: String(limit) })
  const data = await apiJson<{ messages?: ChatMessage[]; has_more?: boolean }>(`/v1/chats/${chatId}?${params}`)
    .catch(withMessage('Failed to fetch older messages'))
  return { messages: data.messages ?? [], has_more: !!data.has_more }
}

/** Imperative fetch of the NEWEST page of a chat (+ has_more) — the rich-view
 * toggle's snapshot. Pages like the lazy scroll-back so no path is capped/un-lazy. */
export async function fetchChatPage(
  chatId: string, limit = 50,
): Promise<{ messages: ChatMessage[]; has_more: boolean }> {
  const data = await apiJson<{ messages?: ChatMessage[]; has_more?: boolean }>(`/v1/chats/${chatId}?limit=${limit}`)
    .catch(withMessage('Failed to fetch chat history'))
  return { messages: data.messages ?? [], has_more: !!data.has_more }
}

export function useDeleteChat() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (chatId: string) => {
      await apiOk(`/v1/chats/${chatId}`, { method: 'DELETE' })
        .catch(withFallback(() => 'Could not delete the chat'))
    },
    onSuccess: (_data, chatId) => {
      forgetDock(chatId)
      // Dropped here: the first-page merge keeps a row that sits below the
      // fresh page, deleted or not.
      const drop = (list?: ChatList) => list && { ...list, chats: list.chats.filter((c) => c.id !== chatId) }
      queryClient.setQueriesData<ChatList>({ queryKey: ['chats'] }, drop)
      queryClient.setQueriesData<ChatList>({ queryKey: ['task-chats'] }, drop)
      queryClient.invalidateQueries({ queryKey: ['chats'] })
      queryClient.invalidateQueries({ queryKey: ['task-chats'] })
      queryClient.invalidateQueries({ queryKey: ['active-chats'] })
    },
  })
}

/** Inline rename of a chat / task-run history entry (PATCH /v1/chats).
 * Optimistic: the one row's `title` patches in every list cache, marked as
 * a person's title like the server marks it; the server echo (sanitized
 * title) settles via invalidation. */
export function useRenameChat() {
  const queryClient = useQueryClient()
  const patchTitle = (chatId: string, title: string) => {
    const patch = (list?: ChatList) => list && {
      ...list, chats: list.chats.map((c) => (c.id === chatId ? { ...c, title, title_generated: true } : c)),
    }
    queryClient.setQueriesData<ChatList>({ queryKey: ['chats'] }, patch)
    queryClient.setQueriesData<ChatList>({ queryKey: ['task-chats'] }, patch)
  }
  return useMutation({
    mutationFn: async ({ chatId, title }: { chatId: string; title: string }) => {
      return apiJson<{ title?: string }>(`/v1/chats/${chatId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      }).catch(withFallback(() => 'Could not rename the chat'))
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

// --- Agent Conversations (Conversations tab) — external sessions (phone /
// future webhook) for any agent the viewer can manage. Dashboard chats are
// excluded server-side and live on the chat-history page. ---

export function useAgentConversations(agent: string, sourceType?: string, offset = 0, limit = 50) {
  return useQuery({
    queryKey: ['agent-conversations', agent, sourceType, offset],
    queryFn: async () => {
      const params = new URLSearchParams({ offset: String(offset), limit: String(limit) })
      if (sourceType) params.set('source_type', sourceType)
      return apiJson<{ conversations: Chat[]; total: number }>(`/v1/agents/${agent}/conversations?${params}`)
        .catch(withMessage('Failed to fetch conversations'))
    },
    enabled: !!agent,
  })
}

export function useChatDetail(chatId: string | null) {
  return useQuery({
    queryKey: ['chat-detail', chatId],
    queryFn: async () => {
      if (!chatId) return null
      return apiJson<Chat>(`/v1/chats/${chatId}/detail`).catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 403) throw new ForbiddenError()
        return withMessage('Failed to fetch chat')(e)
      })
    },
    enabled: !!chatId,
  })
}

export class ForbiddenError extends Error {
  constructor() { super('Forbidden') }
}
