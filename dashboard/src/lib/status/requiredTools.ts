/**
 * What the install dialogs say about the tools a template needs, read from
 * the preview's rows (`required_mcps`): what each tool still needs, the
 * sentence for it, and the line that counts them.
 */

import type { InstallPreview } from '../../api/communityAgents'

export type RequiredTool = InstallPreview['required_mcps'][number]

/** Nothing to do, added by the install itself, waiting for an admin, or not
 *  usable here. The last three are never folded away. */
export type ToolNeed = 'none' | 'adds' | 'approval' | 'blocked'

export function toolNeed(tool: RequiredTool): ToolNeed {
  if (tool.blocked) return 'blocked'
  if (tool.needs_request) return 'approval'
  return tool.request_type === 'install' ? 'adds' : 'none'
}

/** How much a tool needs, most first: a department names a tool once, at
 *  what its neediest agent needs. */
const NEED_RANK: Record<ToolNeed, number> = { blocked: 3, approval: 2, adds: 1, none: 0 }

/** The tools of several agents, each named once at its greatest need. */
export function unionTools(lists: RequiredTool[][]): RequiredTool[] {
  const byName = new Map<string, RequiredTool>()
  for (const tool of lists.flat()) {
    const seen = byName.get(tool.name)
    if (!seen || NEED_RANK[toolNeed(tool)] > NEED_RANK[toolNeed(seen)]) byName.set(tool.name, tool)
  }
  return [...byName.values()]
}

/** The sentence beside a tool that needs something, empty for a ready one. */
export function toolNeedSentence(tool: RequiredTool): string {
  switch (toolNeed(tool)) {
    case 'blocked':
      return tool.installed ? 'An admin must set it up first.' : 'It is not in the community list, so it cannot be added.'
    case 'approval':
      return tool.request_type === 'install'
        ? 'It waits for an admin to add it to your OtoDock.'
        : "It waits for an admin's approval."
    case 'adds':
      return 'The install adds it to your OtoDock.'
    default:
      return ''
  }
}

/** How a share of the tools reads in the count line, for one and for more. */
const COUNTED: [ToolNeed, (n: number) => string][] = [
  ['none', n => `${n} ready`],
  ['adds', n => (n === 1 ? '1 is added to your OtoDock' : `${n} are added to your OtoDock`)],
  ['approval', n => (n === 1 ? "1 needs an admin's approval" : `${n} need an admin's approval`)],
  ['blocked', n => `${n} cannot be used here`],
]

/** The block's one line: how many tools, and how many of them need what.
 *  A single tool that needs something is counted alone, its row says the rest. */
export function toolsCountLine(tools: RequiredTool[]): string {
  const total = tools.length === 1 ? '1 tool' : `${tools.length} tools`
  const shares = COUNTED
    .map(([need, words]) => [tools.filter(t => toolNeed(t) === need).length, words] as const)
    .filter(([n]) => n > 0)
  if (shares.length === 1 && toolNeed(tools[0]) === 'none') return tools.length === 1 ? '1 tool, ready' : `${total}, all ready`
  if (tools.length === 1) return total
  return `${total}: ${shares.map(([n, words]) => words(n)).join(', ')}`
}

/** The store's line: how many tools, with no readiness. */
export function toolsNamedCountLine(n: number): string {
  return n === 1 ? '1 tool' : `${n} tools`
}
