// The one-line detail of a tool call (the collapsed pill's title, the
// activity group's rows) and the two reads the expanded card shares with it:
// a patch's paths and a skill's or workflow's name. Keyed on the call's role
// and payload kind (roles.ts), never on its name.

import { PAYLOAD, ROLE, toolPayload, toolRole, payloadText } from './roles'
import { inputText } from './input'
import { isRecord } from '../json/guards'

/** A patch's file paths: the app-server item's `changes[].path`, else the
 *  `*** Add|Update|Delete File:` headers of the patch text. */
export function patchPaths(input: unknown): string[] {
  const changes: unknown[] = isRecord(input) && Array.isArray(input.changes) ? input.changes : []
  const fromItem = changes.map((c) => (isRecord(c) && typeof c.path === 'string' ? c.path : '')).filter(Boolean)
  if (fromItem.length) return fromItem
  const text = payloadText('apply_patch', input)
  const out: string[] = []
  for (const m of text.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$/gm)) out.push(m[1].trim())
  return out
}

/** A skill or workflow's name: the input's `name`, else the meta block of an
 *  inline workflow script ("Workflow review-changes"). */
export function nameOf(name: string, input: unknown): string {
  const direct = payloadText(name, input)
  if (direct) return direct
  const script = inputText(input, 'script')
  return script ? (script.match(/name:\s*['"]([^'"]+)['"]/)?.[1] ?? '') : ''
}

export function getToolDetail(name: string, summary: string | undefined, toolInput: unknown): string {
  // A shell: the model-written description says WHAT the command does —
  // that's the collapsed title (the command itself is the expanded detail).
  // Wins over `summary`, which older rows carry as the raw command. Generous
  // cap: collapsed rendering clips to one line via CSS, and the expanded
  // pill un-truncates this same text (see ToolActivity), so keep the full
  // sentence.
  const role = toolRole(name)
  const description = inputText(toolInput, 'description')
  if (role === ROLE.SHELL && description) {
    return truncate(description, 300)
  }
  if (summary) return summary
  if (!toolInput) return ''
  switch (toolPayload(name)) {
    case PAYLOAD.COMMAND:
      return truncate(payloadText(name, toolInput), 120)
    case PAYLOAD.FILE_PATH:
      return payloadText(name, toolInput)
    case PAYLOAD.PATCH: {
      const paths = patchPaths(toolInput)
      return paths.length ? paths.map((p) => p.split('/').pop() || p).join(', ') : ''
    }
    case PAYLOAD.SEARCH_PATH:
      // A content search reads "pattern in path"; a glob is its pattern.
      return role === ROLE.SEARCH
        ? [inputText(toolInput, 'pattern'), payloadText(name, toolInput)].filter(Boolean).join(' in ')
        : inputText(toolInput, 'pattern')
    case PAYLOAD.QUERY:
      return payloadText(name, toolInput)
    case PAYLOAD.URL:
      return truncate(payloadText(name, toolInput), 100)
    case PAYLOAD.DESCRIPTION:
      return payloadText(name, toolInput)
    case PAYLOAD.NAME:
      return nameOf(name, toolInput)
    default:
      return ''
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '...' : s
}
