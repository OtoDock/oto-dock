/**
 * The platform's tool vocabulary, by role — the mirror of the proxy's
 * `core/events/tool_roles.py`. The lock-step test
 * `proxy/tests/execution/test_tool_roles.py` reads this file and asserts the
 * two tables are equal entry for entry (keep one entry per line, a quoted
 * key, a single-quoted value). The dashboard never compares a tool NAME: a
 * card asks the tool's role (what the call does) and its payload (what the
 * call carries, and under which input keys). The names are the platform's
 * canonical ones — what the wire's tool frames and the persisted rows carry;
 * every engine maps its own into them on the proxy.
 */

export const TOOL_ROLES: Record<string, string> = {
  'Bash': 'shell',
  'Monitor': 'shell',
  'PowerShell': 'shell',
  'Read': 'read',
  'Glob': 'glob',
  'Grep': 'search',
  'Write': 'write',
  'Edit': 'write',
  'MultiEdit': 'write',
  'NotebookEdit': 'write',
  'apply_patch': 'write',
  'Delete': 'delete',
  'WebFetch': 'web_fetch',
  'WebSearch': 'web_search',
  'web_search': 'web_search',
  'Agent': 'subagent',
  'Task': 'subagent',
  'TodoWrite': 'todo',
  'TodoRead': 'todo',
  'TaskGet': 'task_read',
  'TaskList': 'task_read',
  'TaskOutput': 'task_read',
  'TaskCreate': 'task_write',
  'TaskUpdate': 'task_write',
  'TaskStop': 'task_write',
  'ToolSearch': 'discovery',
  'tool_search': 'discovery',
  'Skill': 'skill',
  'Workflow': 'workflow',
  'EnterPlanMode': 'plan_enter',
  'ExitPlanMode': 'plan_exit',
  'AskUserQuestion': 'question',
  'request_user_input': 'question',
  'CodexEscalation': 'escalation',
}

export const TOOL_PAYLOADS: Record<string, string> = {
  'Bash': 'command',
  'Monitor': 'command',
  'PowerShell': 'command',
  'Read': 'file_path',
  'Glob': 'path',
  'Grep': 'path',
  'Write': 'file_path',
  'Edit': 'file_path',
  'MultiEdit': 'file_path',
  'NotebookEdit': 'file_path',
  'apply_patch': 'patch',
  'Delete': 'file_path',
  'WebFetch': 'url',
  'WebSearch': 'query',
  'web_search': 'query',
  'Agent': 'description',
  'Task': 'description',
  'TodoWrite': 'todos',
  'TodoRead': '',
  'TaskGet': '',
  'TaskList': '',
  'TaskOutput': '',
  'TaskCreate': '',
  'TaskUpdate': '',
  'TaskStop': '',
  'ToolSearch': 'query',
  'tool_search': 'query',
  'Skill': 'name',
  'Workflow': 'name',
  'EnterPlanMode': '',
  'ExitPlanMode': '',
  'AskUserQuestion': '',
  'request_user_input': '',
  'CodexEscalation': '',
}

/** The input keys a payload kind is read from, in order (the first present
 *  wins): a notebook edit names its file `notebook_path`; a Codex patch
 *  arrives under `command` on the hook wire, `input` in the rollout, or as
 *  the app-server item's `changes`. */
export const PAYLOAD_KEYS: Record<string, string[]> = {
  command: ['command'],
  file_path: ['file_path', 'notebook_path'],
  path: ['path'],
  patch: ['command', 'patch', 'input', 'patch_text'],
  url: ['url'],
  query: ['query'],
  name: ['name'],
  description: ['description'],
  todos: ['todos'],
}

/** The roles and the payload kinds as named constants — what a component
 *  compares against (never a bare word). */
export const ROLE = {
  SHELL: 'shell', READ: 'read', GLOB: 'glob', SEARCH: 'search', WRITE: 'write', DELETE: 'delete',
  WEB_FETCH: 'web_fetch', WEB_SEARCH: 'web_search', SUBAGENT: 'subagent', CHECKLIST: 'todo',
  TASK_READ: 'task_read', TASK_WRITE: 'task_write', DISCOVERY: 'discovery', SKILL: 'skill',
  WORKFLOW: 'workflow', PLAN_ENTER: 'plan_enter', PLAN_EXIT: 'plan_exit', QUESTION: 'question',
  ESCALATION: 'escalation',
} as const

export const PAYLOAD = {
  COMMAND: 'command', FILE_PATH: 'file_path', SEARCH_PATH: 'path', PATCH: 'patch', URL: 'url',
  QUERY: 'query', NAME: 'name', DESCRIPTION: 'description', TODOS: 'todos', NONE: '',
} as const

/** The persisted block name every engine's checklist snapshot carries. */
export const TODO_SNAPSHOT = 'TodoWrite'

/** The platform's own delegate MCP tool: rendered as the delegate pill from
 *  the proxy's `delegate_spawn`, never as a generic tool block (the pump
 *  skips it the same way). */
export const DELEGATE_TOOL = 'mcp__delegation-mcp__delegate'

export function toolRole(name: string | undefined | null): string {
  return TOOL_ROLES[name || ''] || ''
}

export function toolPayload(name: string | undefined | null): string {
  return TOOL_PAYLOADS[name || ''] || ''
}

/** The tool's payload — the value under the first present key of its
 *  kind — or undefined. */
export function payloadValue(name: string, input: any): unknown {
  if (!input || typeof input !== 'object') return undefined
  for (const key of PAYLOAD_KEYS[toolPayload(name)] || []) {
    const v = input[key]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return undefined
}

/** The payload as text ('' when absent or not a string). */
export function payloadText(name: string, input: any): string {
  const v = payloadValue(name, input)
  return typeof v === 'string' ? v : ''
}

/** The call writes the paths it names (a write or a delete). */
export function toolWrites(name: string | undefined | null): boolean {
  const role = toolRole(name)
  return role === ROLE.WRITE || role === ROLE.DELETE
}
