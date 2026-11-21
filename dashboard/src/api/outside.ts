import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiJson, withMessage } from '../lib/http/apiJson'
import { clickDownload } from '../lib/http/download'
import { agentConversationsKey } from './chats'
import type { FilesAccess, LimitAccess, LimitColumn, OutsideAccess } from '../lib/outside'

/** One function of a tool as its server lists it, with whether its own
 *  labels say it only reads. */
export interface OutsideFunction {
  name: string
  reads_only: boolean
}

/** One tool a manager may give external identities, with its pick ('' when
 *  not added) and the functions its server listed (null until listed). */
export interface OutsideTool {
  mcp: string
  label: string
  access: OutsideAccess | ''
  functions: string[]
  known_functions: OutsideFunction[] | null
  /** The tool's maker changed which functions only read since the pick. */
  changed: boolean
}

/** A model the wrap-up after a conversation may run on. */
export interface WrapupModel {
  id: string
  label: string
}

/** An app of the agent a manager may open to external identities: its
 *  methods open to them (none yet is a reason, not a refusal). */
export interface OutsideAppChoice {
  id: string
  slug: string
  title: string
  methods: string[]
}

/** An app opened to external identities, with why a conversation gets
 *  nothing from it now ('' when it serves). */
export interface OutsideApp extends OutsideAppChoice {
  /** A new release changed what the app opens to them since it was added. */
  changed: boolean
  problem: string
}

export interface OutsidePeople {
  tools: OutsideTool[]
  files: FilesAccess
  web: boolean
  /** The folders picked, paths inside the knowledge folder. */
  knowledge: string[]
  /** The roots on offer: any of them or any folder inside may be picked. */
  knowledge_choices: string[]
  /** The agent writes down what it learned after each conversation. */
  wrapup_enabled: boolean
  /** The agent may leave one line for the team after each conversation. */
  shared_notes: boolean
  /** The wrap-up's model, '' for the conversation's own. */
  wrapup_model: string
  /** The models of the agent's engine the wrap-up may run on. */
  wrapup_models: WrapupModel[]
  /** The apps opened to external identities. */
  apps: OutsideApp[]
  /** The agent's apps a manager may add. */
  app_choices: OutsideAppChoice[]
}

/** One change of the picks: what is left out stays as it is, so a change
 *  never undoes what the agent's own tool or another manager set meanwhile. */
export interface OutsidePeopleChange {
  /** Each named tool's choice, written alone. */
  tools?: { mcp: string; access: OutsideAccess; functions: string[] }[]
  remove_tools?: string[]
  files?: FilesAccess
  web?: boolean
  add_knowledge?: string[]
  remove_knowledge?: string[]
  wrapup_enabled?: boolean
  shared_notes?: boolean
  wrapup_model?: string
  /** Apps opened or taken away, by id. */
  add_apps?: string[]
  remove_apps?: string[]
}

export const outsideKey = (agent: string) => ['outside-people', agent] as const

export function useOutsidePeople(agent: string) {
  return useQuery({
    queryKey: outsideKey(agent),
    queryFn: () => apiJson<OutsidePeople>(`/v1/agents/${agent}/outside`)
      .catch(withMessage('The external identities settings were not loaded.')),
    enabled: !!agent,
  })
}

export function useChangeOutsidePeople(agent: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: OutsidePeopleChange) => {
      // A read that began before this change must not land after it.
      await qc.cancelQueries({ queryKey: outsideKey(agent) })
      return apiJson<OutsidePeople>(`/v1/agents/${agent}/outside`, { method: 'PUT', json: body })
        .catch(withMessage('The external identities settings were not saved.'))
    },
    onSuccess: (page) => { qc.setQueryData(outsideKey(agent), page) },
  })
}

// The outside people who reached an agent (the Conversations tab's People
// view): who they are, what the agent keeps about them, their export and
// forgetting them. Open to the agent's editors and managers.

/** One row of the People view. */
export interface OutsidePerson {
  id: string
  channel: string
  /** The number they called from. */
  number: string
  first_seen: string
  last_seen: string
  /** They entered the line's PIN. */
  verified: boolean
  conversations: number
  last_outcome: string
}

/** One person with what the agent keeps about them. */
export interface OutsidePersonDetail {
  id: string
  channel: string
  number: string
  first_seen: string
  last_seen: string
  verified: boolean
  /** Their memory topics, each with its text. */
  memory: { name: string; text: string }[]
  files: { path: string; size: number }[]
  conversations: {
    id: string
    started: string
    line: string
    direction: string
    outcome: string
    follow_ups: string[]
  }[]
  /** The notes for the team their conversations left. */
  notes: { day: string; text: string }[]
}

export const peopleKey = (agent: string) => ['agent-people', agent] as const
export const personKey = (agent: string, id: string) => ['agent-person', agent, id] as const

export function usePeople(agent: string) {
  return useQuery({
    queryKey: peopleKey(agent),
    queryFn: () => apiJson<{ people: OutsidePerson[] }>(`/v1/agents/${agent}/people`)
      .then((r) => r.people)
      .catch(withMessage('Failed to load the people')),
    enabled: !!agent,
  })
}

export function usePerson(agent: string, id: string) {
  return useQuery({
    queryKey: personKey(agent, id),
    queryFn: () => apiJson<OutsidePersonDetail>(`/v1/agents/${agent}/people/${encodeURIComponent(id)}`),
    enabled: !!agent && !!id,
  })
}

/** The zip of everything the agent holds about one person: a signed address,
 *  as the workspace's zip, since Android's download manager takes no fetched
 *  file. */
export function useExportPerson(agent: string) {
  return useMutation({
    mutationFn: async (id: string) => {
      const { download_url, filename } = await apiJson<{ download_url: string; filename: string }>(
        `/v1/agents/${agent}/people/${encodeURIComponent(id)}/export-url`,
        { method: 'POST' },
        'The export was not prepared.',
      )
      clickDownload(download_url, filename)
    },
  })
}

/** Forget one person. A conversation with them still running answers 409,
 *  whose sentence the error carries. */
export function useForgetPerson(agent: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      apiJson<Record<string, unknown>>(`/v1/agents/${agent}/people/${encodeURIComponent(id)}`, { method: 'DELETE' },
        'This person was not forgotten.'),
    onSuccess: (_stats, id) => {
      qc.removeQueries({ queryKey: personKey(agent, id) })
      void qc.invalidateQueries({ queryKey: peopleKey(agent) })
      void qc.invalidateQueries({ queryKey: agentConversationsKey(agent) })
    },
  })
}

// --- What the agent may use: the limits of its own runs and of people's chats ---

/** One column's limit of a tool: Read only, or some of its functions. */
export interface ToolLimit {
  access: LimitAccess | 'read_write'
  functions: string[]
  /** The tool's maker changed which functions only read since the limit was saved. */
  changed: boolean
}

/** One enabled tool with its listed functions (null until listed) and, per
 *  column, its limit or null for all tools. The outside column is the
 *  External identities card's pick, read here and set there. */
export interface LimitedTool {
  mcp: string
  label: string
  known_functions: OutsideFunction[] | null
  outside: ToolLimit | null
  runs: ToolLimit | null
  chats: ToolLimit | null
}

export interface ToolLimitsPage {
  tools: LimitedTool[]
}

/** One change: each named tool's columns, written alone; a removal takes a
 *  column's limit away. What is left out stays as it is. */
export interface ToolLimitsChange {
  tools?: { mcp: string; runs?: { access: LimitAccess; functions: string[] }; chats?: { access: LimitAccess; functions: string[] } }[]
  remove?: { mcp: string; governs: LimitColumn }[]
}

export const toolLimitsKey = (agent: string) => ['tool-limits', agent] as const

export function useToolLimits(agent: string) {
  return useQuery({
    queryKey: toolLimitsKey(agent),
    queryFn: () => apiJson<ToolLimitsPage>(`/v1/agents/${agent}/tool-limits`)
      .catch(withMessage('What the agent may use was not loaded.')),
    enabled: !!agent,
  })
}

export function useChangeToolLimits(agent: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: ToolLimitsChange) => {
      await qc.cancelQueries({ queryKey: toolLimitsKey(agent) })
      return apiJson<ToolLimitsPage>(`/v1/agents/${agent}/tool-limits`, { method: 'PUT', json: body })
        .catch(withMessage('What the agent may use was not saved.'))
    },
    onSuccess: (page) => { qc.setQueryData(toolLimitsKey(agent), page) },
  })
}
