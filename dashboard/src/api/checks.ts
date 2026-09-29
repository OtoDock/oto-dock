import { apiFetch } from './auth'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

// Checks (proxy CHECKS.md): named units that judge an agent's work at the
// end of a turn. The agent's checks live in its config (managers write
// them); a person's own in their tree; the verdicts in the verdict store.

export type CheckKind = 'schema' | 'script' | 'handler' | 'judge'

export interface CheckCondition {
  always?: boolean
  kinds?: string[]
  events?: string[]
  places?: string[]
  globs?: string[]
  commands?: string[]
}

export interface CheckJudge {
  rubric: string
  engine?: string
  model?: string
  threshold?: number | null
  mcps?: string[]
  judge_on?: 'auto' | 'platform'
  timeout?: number
}

export interface CheckDoc {
  name: string
  description?: string
  mandatory?: boolean
  applies?: string[]
  condition?: CheckCondition
  rounds?: number
  inputs?: string[]
  schema?: Record<string, unknown> | null
  script?: { run: string; timeout?: number } | null
  handler?: { app: string; handler: string } | null
  judge?: CheckJudge | null
}

export interface CheckItem {
  ref: string
  name: string
  owner: string
  description: string
  mandatory: boolean
  applies: string[]
  condition: CheckCondition
  rounds: number
  inputs: string[]
  sections: CheckKind[]
  /** The full document — managers and the owner of a private check. */
  doc?: CheckDoc
  doc_sha256?: string
  script_sha256?: string
  updated_by?: string
  updated_at?: string
  /** A broken document on disk, in words (managers). */
  problems?: string[]
}

export interface CheckFinding {
  location: string
  severity: 'error' | 'warning' | 'note'
  text: string
}

export interface CheckVerdict {
  id: string
  agent: string
  owner: string
  check_name: string
  section: CheckKind | ''
  status: 'pass' | 'fail' | 'error' | 'skipped'
  pass: boolean
  score: number | null
  findings: CheckFinding[]
  summary: string
  reason: string
  session_id: string
  chat_id: string
  run_id: string
  judge_run_id: string
  user_sub: string
  round: number
  ran_on: string
  engine: string
  model: string
  cost_usd: number
  duration_ms: number
  created_at: string
}

export interface CheckSettings {
  agent: string
  daily_cap_usd: number | null
  platform_default_usd: number | null
  can_manage: boolean
}

export const CHECK_KINDS_WORDS: Record<string, string> = {
  code: 'code', document: 'documents', spreadsheet: 'spreadsheets', presentation: 'presentations',
  image: 'images', video: 'video', audio: 'audio', data: 'data files', text: 'text', any: 'any file',
}
export const CHECK_EVENTS = ['commit', 'push', 'build', 'test', 'render', 'publish']
export const CHECK_PLACES: Record<string, string> = { git: 'inside a git checkout', project: "inside the chat's project folder" }

export function useChecks(agent: string) {
  return useQuery({
    queryKey: ['checks', agent],
    // `tool_enabled`: whether the checks MCP is on for the agent (a core MCP
    // reaches only agents created after its release; older ones turn it on).
    queryFn: async (): Promise<{ checks: CheckItem[]; can_manage: boolean; username: string; tool_enabled?: boolean }> => {
      const res = await apiFetch(`/v1/agents/${agent}/checks`)
      if (!res.ok) throw new Error('Failed to fetch checks')
      return res.json()
    },
    enabled: !!agent,
  })
}

async function _fail(res: Response, fallback: string): Promise<never> {
  const e = await res.json().catch(() => ({}))
  throw new Error(e.detail || fallback)
}

export function useSaveCheck() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, name, doc, script, own }: {
      agent: string; name: string; doc: CheckDoc; script?: string | null; own?: boolean
    }) => {
      const res = await apiFetch(`/v1/agents/${agent}/${own ? 'user-checks' : 'checks'}/${name}`, {
        method: 'PUT', body: JSON.stringify({ doc, script: script ?? null }),
      })
      if (!res.ok) return _fail(res, 'Failed to save the check')
      return res.json() as Promise<CheckItem>
    },
    onSuccess: (_, { agent }) => qc.invalidateQueries({ queryKey: ['checks', agent] }),
  })
}

export function useDeleteCheck() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, name, own }: { agent: string; name: string; own?: boolean }) => {
      const res = await apiFetch(`/v1/agents/${agent}/${own ? 'user-checks' : 'checks'}/${name}`, { method: 'DELETE' })
      if (!res.ok) return _fail(res, 'Failed to remove the check')
      return res.json()
    },
    onSuccess: (_, { agent }) => qc.invalidateQueries({ queryKey: ['checks', agent] }),
  })
}

export function useCheckVerdicts(agent: string, opts: { chat_id?: string; check?: string; limit?: number; before?: string } = {}) {
  const params = new URLSearchParams()
  if (opts.chat_id) params.set('chat_id', opts.chat_id)
  if (opts.check) params.set('check', opts.check)
  if (opts.limit) params.set('limit', String(opts.limit))
  // The page cursor: rows older than this time (the last row of the page before).
  if (opts.before) params.set('before', opts.before)
  const qs = params.toString()
  return useQuery({
    queryKey: ['check-verdicts', agent, qs],
    queryFn: async (): Promise<CheckVerdict[]> => {
      const res = await apiFetch(`/v1/agents/${agent}/check-verdicts${qs ? `?${qs}` : ''}`)
      if (!res.ok) throw new Error('Failed to fetch verdicts')
      const data = await res.json()
      return data.verdicts ?? []
    },
    enabled: !!agent,
  })
}

export function useCheckSettings(agent: string) {
  return useQuery({
    queryKey: ['check-settings', agent],
    queryFn: async (): Promise<CheckSettings> => {
      const res = await apiFetch(`/v1/agents/${agent}/check-settings`)
      if (!res.ok) throw new Error('Failed to fetch the check settings')
      return res.json()
    },
    enabled: !!agent,
  })
}

export function useSaveCheckSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ agent, daily_cap_usd }: { agent: string; daily_cap_usd: number | null }) => {
      const res = await apiFetch(`/v1/agents/${agent}/check-settings`, {
        method: 'PATCH', body: JSON.stringify({ daily_cap_usd }),
      })
      if (!res.ok) return _fail(res, 'Failed to save the check settings')
      return res.json()
    },
    onSuccess: (_, { agent }) => qc.invalidateQueries({ queryKey: ['check-settings', agent] }),
  })
}

/** The condition in words, for lists and cards: a phrase that follows
 *  "runs" — "after every turn", "when it writes code; on a commit". */
export function conditionWords(c: CheckCondition | undefined): string {
  if (!c || Object.keys(c).length === 0) return 'when it writes any file or runs any action'
  if (c.always) return 'after every turn'
  const parts: string[] = []
  if (c.kinds?.length) parts.push(`writes ${c.kinds.map((k) => CHECK_KINDS_WORDS[k] || k).join(', ')}`)
  if (c.events?.length) parts.push(`on ${c.events.map((e) => (e.startsWith('tool:') ? `the tool ${e.slice(5)}` : `a ${e}`)).join(', ')}`)
  if (c.places?.length) parts.push(`writes ${c.places.map((p) => CHECK_PLACES[p] || p).join(', ')}`)
  if (c.globs?.length) parts.push(`under ${c.globs.join(', ')}`)
  if (c.commands?.length) parts.push(`commands matching ${c.commands.join(' | ')}`)
  return `when it ${parts.join('; ')}`
}
