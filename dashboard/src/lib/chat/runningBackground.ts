/**
 * The work of a chat that still runs after its turn: background sub-agents
 * and commands, by spawn id, and delegations, by task id. A view rebuilt
 * from rows reads every stored spawn as finished, and holds no delegate
 * block for a delegation started before its page: the history frame's
 * `running` field says what still runs, every rebuild marks the spawns
 * running again, and the status bar shows what no loaded block carries.
 */
import type { DisplayMessage, MessageBlock } from '../../components/chat/types'
import { BG_COMMAND_BLOCK, DELEGATE_BLOCK } from '../kinds/block'
import { RUN_STATUS } from '../status/run'
import type { RunningBackgroundEntry, RunningBackgroundWire } from '../../api/wireEvents'

/** The lists of `RunningBackground`, one per kind of work. */
export type RunningKind = 'agents' | 'commands' | 'delegates'
const KINDS: readonly RunningKind[] = ['agents', 'commands', 'delegates']

/** The work of the chat that still runs: what a view rebuilt from rows
 *  cannot know, since every stored spawn reads finished and a delegation's
 *  spawn may be older than the page. */
export interface RunningBackground {
  agents: RunningBackgroundEntry[]
  commands: RunningBackgroundEntry[]
  /** Delegations whose report comes to this chat, by task id. */
  delegates: RunningBackgroundEntry[]
  /** The chat the list belongs to: another chat (a new one, the next one
   *  picked before its history arrives) reads nothing from it. */
  chatId?: string | null
}

export const NO_RUNNING_BACKGROUND: RunningBackground = { agents: [], commands: [], delegates: [] }

/** A block of work that outlives its turn (a background sub-agent's, a
 *  command's, a delegation's) as its running list, id and whether it shows
 *  running, else null. A sub-agent's block is the one that carries
 *  `_background`; a delegate block without a task id (an old row, the live
 *  state's by-name fallback) is no list's. */
export function backgroundSpawnOf(b: MessageBlock): { list: RunningKind; id: string; active: boolean; description: string; agent?: string } | null {
  if ('_background' in b && b._background && b._toolId) {
    return { list: 'agents', id: b._toolId, active: !!b.isActive, description: b.description || '' }
  }
  if (b.type === BG_COMMAND_BLOCK && b._toolId) {
    return { list: 'commands', id: b._toolId, active: !!b.isActive, description: b.description || b.command || '' }
  }
  if (b.type === DELEGATE_BLOCK && b._taskId) {
    return { list: 'delegates', id: b._taskId, active: b.status === RUN_STATUS.RUNNING, description: b.taskName || '', agent: b.agent || '' }
  }
  return null
}

/** The block of a spawn that still runs, active again. Callers pass a block
 *  `backgroundSpawnOf` names: the key test alone also holds for a foreground
 *  sub-agent's block, whose `_background` is false. */
export function runningAgain(b: MessageBlock): MessageBlock {
  return '_background' in b || b.type === BG_COMMAND_BLOCK ? { ...b, isActive: true } : b
}

/** A history frame's `running` field (absent: nothing runs; a list a proxy
 *  before delegations never sends reads empty). */
export function runningBackgroundOf(wire: RunningBackgroundWire | null | undefined): RunningBackground {
  const read = (list: RunningBackgroundEntry[] | undefined) => (Array.isArray(list) ? list.filter((e) => e && e.id) : [])
  const out = { agents: read(wire?.agents), commands: read(wire?.commands), delegates: read(wire?.delegates) }
  return KINDS.some((k) => out[k].length) ? out : NO_RUNNING_BACKGROUND
}

/** The list when it belongs to `chatId`, else nothing. */
export function runningFor(running: RunningBackground, chatId: string | null | undefined): RunningBackground {
  return chatId && running.chatId === chatId ? running : NO_RUNNING_BACKGROUND
}

/** The rebuilt view with the listed background sub-agents and commands
 *  running again (their stored spawn blocks read finished). A delegate
 *  block is left as its rows say: they carry its end. The list is returned
 *  as it was when nothing is listed or nothing matched. */
export function markRunningBackground(messages: DisplayMessage[], running: RunningBackground): DisplayMessage[] {
  if (!running.agents.length && !running.commands.length) return messages
  const ids = { agents: new Set(running.agents.map((e) => e.id)), commands: new Set(running.commands.map((e) => e.id)) }
  const listed = (b: MessageBlock): boolean => {
    const spawn = backgroundSpawnOf(b)
    return !!spawn && spawn.list !== 'delegates' && !spawn.active && ids[spawn.list].has(spawn.id)
  }
  let changed = false
  const next = messages.map((msg) => {
    if (!msg.blocks.some(listed)) return msg
    changed = true
    return { ...msg, blocks: msg.blocks.map((b) => (listed(b) ? runningAgain(b) : b)) }
  })
  return changed ? next : messages
}

/** What a view shows running now, added to `running`: a rebuild from REST rows
 *  (they carry no `running` list: a refetch after new rows, a scroll up) keeps
 *  the badges the live frames lit, the status bar's too when the spawn's row
 *  is older than the page. `ended` names the delegations whose result is
 *  among the rows being rebuilt: their entries go, since the rows carry a
 *  delegation's end. A history frame's own list replaces it. The list is
 *  returned as it was when nothing changed. */
export function withRunningInView(
  running: RunningBackground, messages: DisplayMessage[], ended: ReadonlySet<string> = new Set(),
): RunningBackground {
  const seen = {
    agents: new Set(running.agents.map((e) => e.id)),
    commands: new Set(running.commands.map((e) => e.id)),
    delegates: new Set(running.delegates.map((e) => e.id)),
  }
  const out = { agents: [...running.agents], commands: [...running.commands], delegates: [...running.delegates] }
  let changed = false
  for (const msg of messages) {
    for (const b of msg.blocks) {
      const spawn = backgroundSpawnOf(b)
      if (spawn?.active && !seen[spawn.list].has(spawn.id)) {
        seen[spawn.list].add(spawn.id)
        out[spawn.list].push({ id: spawn.id, description: spawn.description, ...(spawn.agent !== undefined ? { agent: spawn.agent } : {}) })
        changed = true
      }
    }
  }
  if (ended.size && out.delegates.some((e) => ended.has(e.id))) {
    out.delegates = out.delegates.filter((e) => !ended.has(e.id))
    changed = true
  }
  return changed ? { ...running, ...out } : running
}
