import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiJson } from '../lib/http/apiJson'

// The Notebook (the proxy's services/notebook/): dated entries agents post
// about their own work, read one delegation step away. The agent page reads
// the agent's read set, the department page the members the person can open.

export interface NotebookEntry {
  id: string
  agent: string
  created_at: string
  text: string
  /** The kind of session it was written in: chat, task, meeting, phone, or ''. */
  surface: string
  /** The person who drove that session, shown only to them, the agent's managers and admins. */
  author: string
  can_delete: boolean
}

export interface NotebookAgent {
  slug: string
  display_name: string
}

export interface NotebookPage {
  entries: NotebookEntry[]
  more: boolean
  agents: NotebookAgent[]
}

interface Cursor {
  before: string
  before_id: string
}

function pageUrl(base: string, agent: string, cursor: Cursor | null): string {
  const params = new URLSearchParams()
  if (agent) params.set('agent', agent)
  if (cursor) {
    params.set('before', cursor.before)
    params.set('before_id', cursor.before_id)
  }
  const query = params.toString()
  return query ? `${base}?${query}` : base
}

function nextCursor(page: NotebookPage): Cursor | null {
  const last = page.entries[page.entries.length - 1]
  return page.more && last ? { before: last.created_at, before_id: last.id } : null
}

function useNotebookPages(key: string[], base: string, agent: string, enabled: boolean, fallback: string) {
  return useInfiniteQuery({
    queryKey: ['notebook', ...key, agent],
    enabled,
    initialPageParam: null as Cursor | null,
    queryFn: ({ pageParam }) => apiJson<NotebookPage>(pageUrl(base, agent, pageParam), {}, fallback),
    getNextPageParam: nextCursor,
  })
}

/** The agent's read set: its own entries and those of the agents one delegation step away. */
export function useAgentNotebook(name: string, agent = '', enabled = true) {
  return useNotebookPages(['agent', name], `/v1/notebook/agents/${encodeURIComponent(name)}`,
    agent, enabled && !!name, "Could not load the agent's Notebook")
}

/** The entries of the department's members this person can open. */
export function useDepartmentNotebook(departmentId: string, agent = '', enabled = true) {
  return useNotebookPages(['department', departmentId],
    `/v1/notebook/departments/${encodeURIComponent(departmentId)}`,
    agent, enabled && !!departmentId, "Could not load the department's Notebook")
}

/** A manager of the posting agent deletes an entry: it leaves every list and prompt. */
export function useDeleteNotebookEntry() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => apiJson<void>(`/v1/notebook/entries/${encodeURIComponent(id)}`,
      { method: 'DELETE' }, 'Could not delete the entry'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['notebook'] }),
  })
}
